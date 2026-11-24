"""Queued Qt worker callbacks must never touch an already closed GUI tree."""
import unittest
from unittest.mock import Mock,patch
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication,QEvent,QThread,QTimer
from PyQt6.QtWidgets import QApplication
from library_ui import LibraryPanel,LibraryTask


class ClosedWorkerCallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def panel(self):
        return LibraryPanel(lambda:dict(config={},segments=[]),lambda _value:None,auto_restore=False)

    def test_logically_closed_panel_ignores_progress_result_failure_and_guarded_actions(self):
        panel=self.panel();callback=Mock();before=panel.status.text()
        panel._closed=True
        try:
            with patch('library_ui.QMessageBox.warning',side_effect=AssertionError('late modal')):
                panel._progress(55,'late progress')
                panel._completed(callback,{'late':True})
                panel._failed('late error')
                panel.guarded(callback)
            callback.assert_not_called();self.assertEqual(panel.status.text(),before)
        finally:
            panel._closed=False;panel.close();panel.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_destroyed_qobject_ignores_late_callbacks_without_accessing_deleted_children(self):
        panel=self.panel();panel.close();sip.delete(panel)
        callback=Mock()
        with patch('library_ui.QMessageBox.warning',side_effect=AssertionError('late modal')):
            panel._progress(1,'late')
            panel._completed(callback,{})
            panel._failed('late failure')
            panel.guarded(callback)
        callback.assert_not_called()

    def test_finished_late_thread_is_released_after_panel_close(self):
        panel=self.panel();worker=LibraryTask(lambda *_args:None)
        panel.worker=worker;panel._closed=True
        panel._finished(worker)
        self.assertIsNone(panel.worker)
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.assertTrue(sip.isdeleted(worker))
        panel._closed=False;panel.close();panel.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_real_queued_result_after_cpp_destruction_is_safely_discarded(self):
        panel=self.panel();callback=Mock()
        # Queue the same lambda route used by LibraryTask.result. The signal
        # arrives after C++ deletion, while Python still holds the wrapper.
        QTimer.singleShot(0,lambda:panel._completed(callback,{'queued':True}))
        panel.close();sip.delete(panel)
        with patch('library_ui.QMessageBox.warning',side_effect=AssertionError('late modal')):
            self.app.processEvents()
        callback.assert_not_called()


if __name__=='__main__':unittest.main()
