"""Camera review keeps its editable list beside a large, fitted picture."""
import unittest
from unittest.mock import Mock,patch
from PyQt6.QtWidgets import QApplication,QPushButton
from PyQt6.QtCore import Qt,QPoint,QRect
from PyQt6.QtGui import QColor,QPixmap
from core import Segment
from pgm_app import Window


class ReviewLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('library_bridge.read_settings',return_value={}):self.window=Window()
        self.window.library_panel.set_workflow_stage('camera')
        self.window.tabs.setCurrentWidget(self.window.results_page)
        self.window.populate_results([Segment(0,100,'A','label_read'),Segment(100,200,'?','no_label')])

    def tearDown(self):self.window.close()

    def test_results_and_review_stay_horizontal_at_supported_window_sizes(self):
        window=self.window
        for width,height in ((1440,900),(1024,768),(1000,648)):
            window.resize(width,height);window.show();self.app.processEvents()
            self.assertEqual((window.width(),window.height()),(width,height))
            self.assertEqual(window.review_split.orientation(),Qt.Orientation.Horizontal)
            table=QRect(window.results.mapTo(window.results_page,QPoint()),window.results.size())
            picture=QRect(window.pgm_picture.mapTo(window.results_page,QPoint()),window.pgm_picture.size())
            self.assertLess(table.right(),picture.left())
            self.assertGreaterEqual(window.results.width(),295)
            self.assertGreaterEqual(window.pgm_picture.width(),480)
            self.assertGreaterEqual(window.pgm_picture.height(),160)
            self.assertTrue(window.results_page.rect().contains(picture))
            self.assertTrue(table.intersects(QRect(0,picture.top(),width,picture.height())))
            self.assertTrue(window.review_list_host.rect().contains(window.label_picture.geometry()))
            for button in window.review_list_host.findChildren(QPushButton):
                geometry=QRect(button.mapTo(window.results_page,QPoint()),button.size())
                self.assertFalse(geometry.intersects(picture))
                window.review_list_scroll.ensureWidgetVisible(button)
                self.app.processEvents()
                position=button.mapTo(window.review_list_scroll.viewport(),QPoint())
                self.assertGreaterEqual(position.y(),0)
                self.assertLessEqual(position.y()+button.height(),window.review_list_scroll.viewport().height())
            window.review_list_scroll.verticalScrollBar().setValue(0)

    def test_splitter_resizes_picture_without_decoding_or_losing_source_resolution(self):
        window=self.window;window.resize(1440,900);window.show();self.app.processEvents()
        original=QPixmap(1920,1080);original.fill(QColor('#43668a'))
        window.pgm_picture.setPixmap(original)
        first_size=window.pgm_picture.pixmap().size()
        window.review_split.setSizes([1100,200]);self.app.processEvents()
        self.assertEqual(window.pgm_picture._review_pixmap.size(),original.size())
        self.assertNotEqual(window.pgm_picture.pixmap().size(),first_size)
        self.assertLessEqual(window.pgm_picture.pixmap().width(),window.pgm_picture.width())
        self.assertLessEqual(window.pgm_picture.pixmap().height(),window.pgm_picture.height())
        self.assertAlmostEqual(window.pgm_picture.pixmap().width()/window.pgm_picture.pixmap().height(),16/9,delta=.015)

    def test_existing_frame_selection_camera_edit_and_confirmation_are_preserved(self):
        window=self.window
        window.results.setCurrentCell(1,2)
        self.assertEqual(window.review_frame.value(),100)
        window.results.item(1,2).setText('D')
        self.assertEqual(window.segments()[1].reason,'identity_unconfirmed')
        button=next(button for button in window.review_list_host.findChildren(QPushButton) if button.text()=='确认机位')
        button.click()
        self.assertEqual(window.segments()[1].camera,'D')
        self.assertEqual(window.segments()[1].reason,'manual')
        with patch.object(window,'show_review') as show:
            next(button for button in window.review_list_host.findChildren(QPushButton) if button.text()=='后一帧').click()
            self.assertEqual(window.review_frame.value(),101)
            show.assert_called_once_with()

    def test_combined_confirmation_routes_to_the_atomic_selection_workflow(self):
        panel=self.window.library_panel
        client=Mock();client.call.return_value=dict(reference_session_id='base',revision=7)
        request=dict(production_id='show',reference_session_id='base',candidate_id='candidate',
                     baseline_candidate_id='anchor',checked=True,confirm=True,expected_revision=7)
        with patch.object(panel,'ensure_workflow_context',return_value=(client,'show')), \
             patch.object(panel,'_run',side_effect=lambda title,operation,success:operation(lambda *_:None,lambda:False)), \
             patch('reference_workflow.ReferenceWorkflow') as workflow:
            self.window.reference_action('select_clip',request)
            workflow.return_value.confirm_and_select_clip.assert_called_once_with('candidate','anchor',expected_revision=7)
            workflow.return_value.select_clip.assert_not_called()
            request['confirm']=False
            self.window.reference_action('select_clip',request)
            workflow.return_value.select_clip.assert_called_once_with('candidate',True,'anchor',expected_revision=7)


if __name__=='__main__':unittest.main()
