"""Bounded, asynchronous source-frame previews for the director's take list."""
from __future__ import annotations

from collections import OrderedDict
from fractions import Fraction
from io import BytesIO
from threading import Lock

from PyQt6.QtCore import QObject, QPoint, QRunnable, QSize, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap


def decode_thumbnail(path, seconds, width=144, height=81):
    """Decode only a source frame; never create a proxy or change source timing."""
    from core import probe, read_frame
    from PIL import Image
    info = probe(path)
    frame = max(0, min(int(Fraction(str(seconds)) * Fraction(info['fps'])), info['frames'] - 1))
    pixels = read_frame(path, frame)
    picture = Image.fromarray(pixels[..., ::-1])
    picture.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new('RGB', (width, height), '#17212b')
    canvas.paste(picture, ((width - picture.width)//2, (height - picture.height)//2))
    output = BytesIO()
    canvas.save(output, format='JPEG', quality=80)
    return output.getvalue()


class _Signals(QObject):
    ready = pyqtSignal(object)


class _LatestJob(QRunnable):
    """One runner drains a replaceable pending slot, never an unbounded Qt queue."""
    def __init__(self, state, signals):
        super().__init__()
        self.state, self.signals = state, signals

    def run(self):
        while True:
            try:
                from media_cache import frame_read_session
                # Created, used and closed on the same worker. Keep this runner
                # reserved until decoder cleanup finishes, including slow I/O.
                with frame_read_session(cancelled=lambda: self.state['closed']) as session:
                    self._drain(session)
            except Exception as exc:
                with self.state['lock']:
                    self.state['pending'] = None
                    generation = self.state['generation']
                if not self.state['closed']:
                    try:
                        self.signals.ready.emit((generation, None, str(exc)))
                    except RuntimeError:
                        pass
            with self.state['lock']:
                if self.state['closed'] or self.state['pending'] is None:
                    self.state['running'] = False
                    return
                # A new request arrived while the decoder session was closing.
                # Reuse this runner instead of queuing another pool job.

    def _drain(self, session):
        while True:
            with self.state['lock']:
                task = self.state['pending']
                self.state['pending'] = None
                if self.state['closed'] or task is None:
                    return
            generation, operation = task
            cancelled = lambda generation=generation: self.state['closed'] or generation != self.state['generation']
            session.cancelled = cancelled
            if cancelled():
                continue
            try:
                result, error = operation(cancelled), ''
            except Exception as exc:
                result, error = None, str(exc)
            if not cancelled():
                try:
                    self.signals.ready.emit((generation, result, error))
                except RuntimeError:
                    return  # The owning window has already been destroyed.


class LatestTaskLoader(QObject):
    """Latest-only background work with one active and one pending request."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = dict(closed=False, generation=0, running=False, pending=None, lock=Lock())
        self.signals = _Signals(self)
        self.signals.ready.connect(self._ready)
        self.callback = None

    def request(self, operation, callback):
        with self.state['lock']:
            if self.state['closed']:
                return
            self.state['generation'] += 1
            self.callback = callback
            self.state['pending'] = (self.state['generation'], operation)
            start = not self.state['running']
            self.state['running'] = True
        if start:
            QThreadPool.globalInstance().start(_LatestJob(self.state, self.signals))

    def _ready(self, value):
        generation, result, error = value
        if not self.state['closed'] and generation == self.state['generation'] and self.callback:
            callback = self.callback
            self.callback = None
            callback(result, error)

    def invalidate(self):
        with self.state['lock']:
            self.state['generation'] += 1
            self.state['pending'] = None
            self.callback = None

    def close(self):
        with self.state['lock']:
            self.state['closed'] = True
            self.state['generation'] += 1
            self.state['pending'] = None
            self.callback = None


class SourceFrameLoader(LatestTaskLoader):
    """Decode outside Qt's GUI thread; rapid scrubs keep only the newest frame."""
    def __init__(self, parent=None, decoder=None):
        super().__init__(parent)
        from core import read_frame
        self.decoder = decoder or read_frame

    def request(self, path, index, callback):
        decoder = self.decoder
        super().request(lambda cancelled: decoder(path, index), callback)


class _Decode(QRunnable):
    def __init__(self, state, signals, generation, index, key, decoder):
        super().__init__()
        self.state, self.signals = state, signals
        self.generation, self.index, self.key, self.decoder = generation, index, key, decoder

    def run(self):
        if self.state['closed']:
            return
        try:
            # A queued job may become obsolete before a decoder is available.
            # Still emit completion so it releases one of the two job slots.
            picture = self.decoder(self.key[0], self.key[1]) if self.key in self.state['keys'] else b''
        except Exception:
            picture = b''
        if not self.state['closed']:
            try:
                self.signals.ready.emit((self.generation, self.index, self.key, picture))
            except RuntimeError:
                pass  # The parent window was closed while a frame was decoding.


class ThumbnailLoader(QObject):
    CACHE_LIMIT = 256
    ICON_LIMIT = 64
    PREFETCH_ROWS = 2

    def __init__(self, list_widget, parent=None, decoder=None):
        super().__init__(parent or list_widget)
        self.widget, self.decoder = list_widget, decoder or decode_thumbnail
        self.rows, self.keys = [], []
        self.generation, self.pending = 0, set()
        self.cache = OrderedDict()
        self._icons = OrderedDict()
        self._active_indices = set()
        self._decorated_indices = set()
        self.state = {'closed': False, 'keys': set()}
        self.signals = _Signals(self)
        self.signals.ready.connect(self._ready)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(35)
        self.timer.timeout.connect(self._schedule)
        self.widget.setIconSize(QSize(144, 81))
        self.widget.verticalScrollBar().valueChanged.connect(self.request)
        self.widget.viewport().installEventFilter(self)
        self.placeholder = self._placeholder()

    @staticmethod
    def _placeholder():
        picture = QPixmap(144, 81)
        picture.fill(QColor('#e4eaf0'))
        painter = QPainter(picture)
        painter.setPen(QColor('#687b8e'))
        painter.drawText(picture.rect(), Qt.AlignmentFlag.AlignCenter, '画面预览')
        painter.end()
        return QIcon(picture)

    def set_rows(self, rows):
        from library_bridge import read_settings, remap_path
        self.generation += 1
        self.rows = list(rows)
        keys = []
        # A newly mounted NAS can recover from an earlier failed thumbnail.
        self.cache = OrderedDict((key, value) for key, value in self.cache.items() if value)
        mappings = read_settings().get('path_mappings', [])
        local_paths = {}
        for row in self.rows:
            stat = row.get('metadata', {}).get('source_stat') or {}
            path = row.get('media_path', '')
            if path not in local_paths:
                local_paths[path] = remap_path(path, mappings)
            key = (local_paths[path], float(row.get('start_seconds') or 0),
                   stat.get('size'), stat.get('mtime_ns'))
            keys.append(key)
        if keys != self.keys:
            self._clear_decorations()
        self.keys = keys
        self._active_indices.clear()
        self.state['keys'] = set(self.keys)
        self.request()

    @staticmethod
    def _icon(raw):
        picture = QPixmap()
        picture.loadFromData(raw)
        return QIcon(picture)

    def _cached_icon(self, key):
        icon = self._icons.get(key)
        if icon is None:
            icon = self._icon(self.cache[key])
            self._icons[key] = icon
            while len(self._icons) > self.ICON_LIMIT:
                self._icons.popitem(last=False)
        else:
            self._icons.move_to_end(key)
        self.cache.move_to_end(key)
        return icon

    def _clear_decorations(self, keep=()):
        keep = set(keep)
        for index in self._decorated_indices - keep:
            item = self.widget.item(index)
            if item is not None:
                item.setIcon(self.placeholder)
        self._decorated_indices.intersection_update(keep)

    def _viewport_indices(self):
        """Find the real visible rows, including short lists and card gaps.

        Only the viewport's two edges are inspected. The list may have many
        thousands of rows, but this never walks them or decodes hidden cards.
        """
        viewport = self.widget.viewport()
        height = viewport.height()
        if not self.keys or height <= 0 or viewport.width() <= 0:
            return range(0)
        x = min(8, viewport.width() - 1)
        edge = min(height, max(16, self.widget.spacing() + 4))
        first = next((index for y in range(edge)
                      if (index := self.widget.indexAt(QPoint(x, y)).row()) >= 0), -1)
        if first < 0:
            return range(0)
        last = next((index for y in range(height - 1, height - edge - 1, -1)
                     if (index := self.widget.indexAt(QPoint(x, y)).row()) >= 0), -1)
        if last < 0:
            # The last item ends above the viewport's lower edge.
            last = len(self.keys) - 1
        return range(max(0, first - self.PREFETCH_ROWS),
                     min(len(self.keys), last + self.PREFETCH_ROWS + 1))

    def request(self, *_args):
        if not self.state['closed']:
            self.timer.start()

    def eventFilter(self, watched, event):
        from PyQt6.QtCore import QEvent
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize):
            width = self.widget.viewport().width()
            size = QSize(56, 32) if width < 330 else QSize(96, 54) if width < 450 else QSize(144, 81)
            if self.widget.iconSize() != size:
                self.widget.setIconSize(size)
                self.widget.scheduleDelayedItemsLayout()
            self.request()
        return False

    def _schedule(self):
        if self.state['closed'] or not self.widget.isVisible() or not self.keys:
            return
        indices = self._viewport_indices()
        self._active_indices = set(indices)
        self._clear_decorations(self._active_indices)
        # Apply cached icons only to this viewport. Hidden QListWidgetItems do
        # not retain a full-resolution QIcon for every formerly visited source.
        for index in indices:
            key = self.keys[index]
            item = self.widget.item(index)
            if item is not None:
                if self.cache.get(key):
                    item.setIcon(self._cached_icon(key))
                    self._decorated_indices.add(index)
                elif index not in self._decorated_indices:
                    item.setIcon(self.placeholder)
        # Two decoding jobs at a time; at most one screen plus a short prefetch.
        for index in indices:
            key = self.keys[index]
            job = (self.generation, index, key)
            if key in self.cache:
                continue
            if any(pending[2] == key for pending in self.pending):
                # A status/selection refresh does not change this source frame.
                # Reuse its active decode even when the list generation changed.
                continue
            if len(self.pending) >= 2:
                break
            self.pending.add(job)
            QThreadPool.globalInstance().start(_Decode(self.state, self.signals, *job, self.decoder))

    def _ready(self, result):
        generation, index, key, picture = result
        self.pending.discard((generation, index, key))
        if self.state['closed']:
            return
        if generation != self.generation and not picture:
            # Failed reads may recover after a NAS remount/list refresh. An old
            # failure must not suppress a fresh attempt for that source key.
            self.request()
            return
        self.cache[key] = picture
        self.cache.move_to_end(key)
        self._icons.pop(key, None)
        while len(self.cache) > self.CACHE_LIMIT:
            removed, _picture = self.cache.popitem(last=False)
            self._icons.pop(removed, None)
        if picture:
            icon = None
            for current_index in self._active_indices:
                if current_index < len(self.keys) and self.keys[current_index] == key:
                    item = self.widget.item(current_index)
                    if item is not None:
                        if icon is None:
                            icon = self._cached_icon(key)
                        item.setIcon(icon)
                        self._decorated_indices.add(current_index)
        self.request()

    def close(self):
        self.state['closed'] = True
        self.timer.stop()
        self._clear_decorations()
        self._active_indices.clear()
        self._icons.clear()
        self.cache.clear()
        self.pending.clear()
        self.rows.clear()
        self.keys.clear()
        self.state['keys'].clear()
