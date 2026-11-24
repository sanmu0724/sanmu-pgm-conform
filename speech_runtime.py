"""One lazily constructed CPU speech recognizer per application process.

Importing this module does not import a speech backend, create cache directories
or download models. The explicitly supplied factory retains responsibility for
loading/downloading when transcription is requested by the user.
"""
from pathlib import Path
from collections import OrderedDict
import copy
import threading


_MODEL_FILES = ('model.bin', 'config.json', 'tokenizer.json')
_lock = threading.Lock()
_cached_factory = None
_cached_key = None
_cached_recognizer = None
_transcripts = OrderedDict()


def _stat_signature(path):
    try:
        value = path.stat()
        return (value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)
    except OSError as exc:
        # A changed, missing or temporarily inaccessible file invalidates an
        # old model rather than silently reusing it. The backend reports any
        # actual loading error, so this helper does not invent validation rules.
        return ('unavailable', exc.errno)


def _model_key(selected_model, cache):
    name = str(selected_model)
    directory = Path(name).expanduser()
    cache_path = str(Path(cache).expanduser().resolve())
    if directory.is_dir() or directory.is_absolute() or name.startswith(('./', '../', '~')):
        directory = directory.resolve()
        return ('local', str(directory), cache_path, _stat_signature(directory),
                tuple((part, _stat_signature(directory / part)) for part in _MODEL_FILES))
    # Remote names cannot be resolved here: that might perform network access.
    # The chosen download root is part of their identity, even before it exists.
    return ('remote', name, cache_path)


def get_recognizer(factory, selected_model, cache):
    """Lazily load/reuse one model without changing inference parameters.

    Factory *identity* and local model-file stat signatures form the key. The
    lock prevents concurrent callers from initializing the same model twice.
    It protects construction/cache state only; inference remains the caller's
    responsibility. Changing a model drops the prior cached strong reference
    before loading its replacement. Existing callers can still finish using
    their own references; no backend-specific close/unload method is assumed.
    """
    global _cached_factory, _cached_key, _cached_recognizer
    with _lock:
        key = _model_key(selected_model, cache)
        if _cached_factory is factory and _cached_key == key and _cached_recognizer is not None:
            return _cached_recognizer
        # Never retain two cached models, and never cache a failed construction.
        _cached_factory = _cached_key = _cached_recognizer = None
        _transcripts.clear()
        recognizer = factory(selected_model, device='cpu', compute_type='int8', download_root=str(cache))
        _cached_factory, _cached_key, _cached_recognizer = factory, key, recognizer
        return recognizer


def clear_recognizer_cache():
    """Release the cached model reference without touching files or callers."""
    global _cached_factory, _cached_key, _cached_recognizer
    with _lock:
        _cached_factory = _cached_key = _cached_recognizer = None
        _transcripts.clear()


def cached_transcription(recognizer, key):
    """Reuse only exact source/range/settings with the current recognizer."""
    with _lock:
        if recognizer is not _cached_recognizer or key not in _transcripts:
            return None
        _transcripts.move_to_end(key)
        return copy.deepcopy(_transcripts[key])


def remember_transcription(recognizer, key, rows):
    with _lock:
        if recognizer is not _cached_recognizer or len(rows) > 50000:
            return
        _transcripts[key] = copy.deepcopy(rows)
        _transcripts.move_to_end(key)
        while len(_transcripts) > 128 or sum(len(value) for value in _transcripts.values()) > 50000:
            _transcripts.popitem(last=False)
