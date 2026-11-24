"""Isolated native geometry test for the camera-label page."""
import unittest
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt,QPoint,QRect
from pgm_app import Window

class RoiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        with patch('library_bridge.read_settings',return_value={}):self.window=Window()
    def tearDown(self):self.window.close()

    def test_camera_label_controls_never_overlap_preview_in_small_windows(self):
        self.window.library_panel.set_workflow_stage('camera')
        self.window.tabs.setCurrentWidget(self.window.roi_page)
        controls = list(self.window.label_fields.values()) + [self.window.roi_copy_button, self.window.confidence]
        for width, height in ((1440, 900), (1024, 768), (1000, 648)):
            self.window.resize(width, height); self.window.show(); QApplication.processEvents()
            canvas = self.window.canvas
            preview_rect = QRect(canvas.mapTo(self.window.roi_page, QPoint()), canvas.size())
            self.assertGreaterEqual(canvas.width(), 320)
            self.assertGreaterEqual(canvas.height(), 180)
            for control in controls:
                self.assertTrue(self.window.roi_settings.isAncestorOf(control))
                rect = QRect(control.mapTo(self.window.roi_page, QPoint()), control.size())
                self.assertFalse(rect.intersects(preview_rect), (width, height, control, rect, preview_rect))
            scroll = self.window.roi_settings_scroll
            scroll.ensureWidgetVisible(self.window.confidence)
            QApplication.processEvents()
            point = self.window.confidence.mapTo(scroll.viewport(), QPoint())
            self.assertGreaterEqual(point.y(), 0)
            self.assertLessEqual(point.y()+self.window.confidence.height(), scroll.viewport().height())


if __name__=='__main__':unittest.main()
