"""Custom camera prefixes across early and later shoot labelling conventions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from cut_xml import analyze_cut_xml
from ocr import LabelReader, match_label


class FakeOCR:
    def __init__(self, text=None, confidence=1.):
        self.text = text
        self.confidence = confidence
        self.calls = 0

    def recognize(self, rgb):
        self.calls += 1
        return [] if self.text is None else [dict(text=self.text, confidence=self.confidence)]


class CameraLabelRuleTests(unittest.TestCase):
    early = {'B': 'B', 'C': '440', 'D': 'D'}
    later = {camera: camera for camera in 'ABCD'}

    def test_early_numbered_clips_match_stable_prefix(self):
        for counter in ('0001', '0002', '0003', '0149', '9999'):
            for camera, prefix in self.early.items():
                for separator in ('', '_'):
                    self.assertEqual(match_label(prefix + separator + counter, self.early,
                                                 match_mode='prefix'), camera)

    def test_later_reel_and_take_changes_match_camera_prefix(self):
        for camera in 'ABCD':
            for suffix in ('003C001', '004C001', '004C002', '123C456'):
                self.assertEqual(match_label(camera + suffix, self.later,
                                             match_mode='prefix'), camera)

    def test_prefix_is_only_the_beginning(self):
        for text in ('1440_0001', 'X440_0001', 'XB003C001'):
            self.assertIsNone(match_label(text, self.early, match_mode='prefix'))
        self.assertIsNone(match_label('B003C001', {'C': 'C'}, match_mode='prefix'))
        self.assertIsNone(match_label('C003C001', self.early, match_mode='prefix'))

    def test_prefix_obeys_custom_camera_mapping_even_for_sony_ids(self):
        custom = {'A': 'LEFT', 'B': 'SIDE2', 'C': 'B', 'D': 'RIGHT'}
        self.assertEqual(match_label('B003C001', custom, match_mode='prefix'), 'C')
        self.assertEqual(match_label('left_take001', custom, match_mode='prefix'), 'A')
        self.assertEqual(match_label('SIDE2TAKE54', custom, match_mode='prefix'), 'B')
        self.assertEqual(match_label('RIGHT27', custom, match_mode='prefix'), 'D')

    def test_prefix_normalization_and_numeric_ocr_correction(self):
        self.assertEqual(match_label('  b_0002 ', self.early, match_mode='prefix'), 'B')
        self.assertEqual(match_label('44O_0002', self.early, match_mode='prefix'), 'C')
        self.assertEqual(match_label('4IL_0002', {'C': '411'}, match_mode='prefix'), 'C')
        self.assertIsNone(match_label('0_0002', {'B': 'O'}, match_mode='prefix'))

    def test_exact_requires_entire_normalized_label(self):
        labels = {'B': 'B0001', 'C': '440_0001', 'D': 'D0001'}
        self.assertEqual(match_label('B O O O I', labels, match_mode='exact'), 'B')
        self.assertEqual(match_label('44O_OOOL', labels, match_mode='exact'), 'C')
        for text in ('B', 'B0002', '440_0002', 'D0002', 'B003C001'):
            self.assertIsNone(match_label(text, labels, True, match_mode='exact'))

    def test_overlapping_prefixes_are_unknown(self):
        for labels in ({'B': 'B', 'C': 'B003'}, {'B': 'B', 'C': 'b'},
                       {'B': '440', 'C': '44O'}):
            text = '440_0001' if labels['B'] == '440' else 'B003C001'
            # O in a letter-based target is not a digit correction; use OCR's O.
            if labels['B'] == '440':
                text = '44O_0001'
            self.assertIsNone(match_label(text, labels, match_mode='prefix'))

    def test_empty_rules_never_match_and_unknown_mode_is_rejected(self):
        self.assertIsNone(match_label('B0001', {'B': '', 'C': ' _ '}, match_mode='prefix'))
        with self.assertRaises(ValueError):
            match_label('B0001', self.early, match_mode='contains')
        with self.assertRaises(ValueError):
            LabelReader({'label_match_mode': 'contains'}, FakeOCR())

    def test_missing_mode_keeps_existing_legacy_behaviour(self):
        old = {'B': 'B0001', 'C': '440', 'D': 'D0001'}
        self.assertIsNone(match_label('B0002', old))
        self.assertEqual(match_label('B0002', old, True), 'B')
        self.assertIsNone(match_label('440_0002', old))
        self.assertEqual(match_label('C003C001', old), 'C')
        self.assertEqual(match_label('B003C001', {'C': 'B'}), 'B')


class CameraReaderRuleTests(unittest.TestCase):
    frame = np.zeros((50, 100, 3), np.uint8)

    def config(self, infer=True):
        return dict(labels={'A': 'A', 'B': 'B', 'C': '440', 'D': 'D'},
                    label_match_mode='prefix', infer_a_when_blank=infer)

    def test_unlabelled_a_requires_no_text_in_active_rois(self):
        rois = {camera: [0, 0, 1, 1] for camera in 'ABCD'}
        for text in (None, '', '  '):
            self.assertEqual(LabelReader(self.config(), FakeOCR(text)).inspect(self.frame, rois)[:2],
                             ('A', 'A_inferred_blank'))
        for text, confidence in (('XYZ', 1), ('B0002', .2), ('440_0002', .2), ('?', 1)):
            self.assertEqual(LabelReader(self.config(), FakeOCR(text, confidence)).inspect(
                self.frame, rois)[0], '?')

    def test_labelled_a_mode_never_infers_blank(self):
        rois = {camera: [0, 0, 1, 1] for camera in 'ABCD'}
        self.assertEqual(LabelReader(self.config(False), FakeOCR()).inspect(self.frame, rois)[:2],
                         ('?', 'no_label'))
        self.assertEqual(LabelReader(self.config(False), FakeOCR('A003C001')).inspect(
            self.frame, rois)[:2], ('A', 'label_read'))

    def test_inactive_a_rule_and_roi_do_not_contaminate_blank_mode(self):
        cfg = self.config()
        cfg['labels']['A'] = 'B'
        rois = {'A': [-1, -1, -1, -1], **{c: [0, 0, 1, 1] for c in 'BCD'}}
        backend = FakeOCR('B0002')
        reader = LabelReader(cfg, backend)
        self.assertEqual(reader.inspect(self.frame, rois)[:2], ('B', 'label_read'))
        self.assertEqual(backend.calls, 1)
        self.assertNotIn('A', reader.labels)

    def test_only_inactive_a_roi_cannot_infer_a(self):
        self.assertEqual(LabelReader(self.config(), FakeOCR()).inspect(
            self.frame, {'A': [0, 0, 1, 1]})[:2], ('?', 'no_label'))

    def test_unknown_text_in_any_active_roi_prevents_a_inference(self):
        class CropOCR:
            def recognize(self, rgb):
                return [] if rgb.shape[1] == 20 else [dict(text='UNKNOWN', confidence=1)]
        reader = LabelReader(self.config(), CropOCR())
        rois = {'B': [0, 0, .2, 1], 'C': [0, 0, .4, 1], 'D': [0, 0, .2, 1]}
        self.assertEqual(reader.inspect(self.frame, rois)[0], '?')

    def test_rule_conflict_and_multiple_labels_require_review(self):
        cfg = self.config()
        cfg['labels']['C'] = 'B00'
        rois = {camera: [0, 0, 1, 1] for camera in 'BCD'}
        self.assertEqual(LabelReader(cfg, FakeOCR('B0002')).inspect(self.frame, rois)[0], '?')
        backend = FakeOCR()
        backend.recognize = lambda rgb: [dict(text=label, confidence=1)
                                         for label in ('B0002', 'D0003')]
        self.assertEqual(LabelReader(self.config(), backend).inspect(self.frame, rois)[:2],
                         ('?', 'multiple_labels'))

    def test_shared_rois_and_exact_frame_cache_are_preserved(self):
        backend = FakeOCR('440_0002')
        reader = LabelReader(self.config(), backend)
        rois = {camera: [0, 0, 1, 1] for camera in 'BCD'}
        self.assertEqual(reader.inspect(self.frame, rois, 1)[0], 'C')
        self.assertEqual(reader.inspect(self.frame.copy(), rois, 2)[0], 'C')
        self.assertEqual(backend.calls, 1)
        self.assertEqual(reader.hits, 1)


class CutCameraRuleTests(unittest.TestCase):
    def make_case(self):
        labels = [None, 'B0001', 'B0002', '440_0001', '440_0002', 'D0003', 'B004C002']
        block = dict(path='test.mov', start=0, end=len(labels)*10, frames=len(labels)*10,
                     width=100, height=50, start_tc='01:00:00:00')
        shots = [dict(start=i*10, end=(i+1)*10, source_in=i*10, source_out=(i+1)*10,
                      pgm_path=block['path']) for i in range(len(labels))]
        cut = dict(fps=25, xml_digest='fixture', duration=block['end'], pgm_info=block,
                   pgm_sources=[block], shots=shots)
        cfg = dict(fps=25, labels={'A': '', 'B': 'B', 'C': '440', 'D': 'D'},
                   label_rois={camera: [0, 0, 1, 1] for camera in 'ABCD'},
                   infer_a_when_blank=True, label_match_mode='prefix')
        class FrameOCR:
            def recognize(self, rgb):
                text = labels[int(rgb[0, 0, 0]) // 10]
                return [] if text is None else [dict(text=text, confidence=1)]
        return cfg, cut, FrameOCR()

    def analyze(self, cfg, cut, backend, output):
        with patch('cut_xml.timeline_info', return_value=cut), patch(
                'cut_xml.read_frame', side_effect=lambda path, index: np.full((50,100,3), index, np.uint8)):
            return analyze_cut_xml(cfg, output, backend=backend)

    def test_new_rules_keep_every_cut_and_exported_settings(self):
        cfg, cut, backend = self.make_case()
        cfg['label_rois']['A'] = [-1, -1, -1, -1]
        with tempfile.TemporaryDirectory() as output:
            segments = self.analyze(cfg, cut, backend, output)
            self.assertEqual([s.camera for s in segments], ['A', 'B', 'B', 'C', 'C', 'D', 'B'])
            self.assertEqual([(s.start, s.end) for s in segments],
                             [(shot['start'], shot['end']) for shot in cut['shots']])
            saved = json.loads((Path(output)/'analysis.json').read_text())
            self.assertEqual(saved['config']['label_match_mode'], 'prefix')
            self.assertEqual(saved['ocr_stats']['sampled_frames'], 21)

    def test_active_missing_rules_and_invalid_mode_are_rejected(self):
        cfg, cut, backend = self.make_case()
        for key, value, message in (('label_match_mode', 'contains', '匹配方式'),
                                    ('label_rois', {'A': [0,0,1,1]}, 'B/C/D'),
                                    ('labels', {'A':'A', 'B':'B', 'D':'D'}, '非空值')):
            changed = copy.deepcopy(cfg)
            changed[key] = value
            with self.subTest(key=key), tempfile.TemporaryDirectory() as output:
                with self.assertRaisesRegex(ValueError, message):
                    self.analyze(changed, cut, backend, output)

    def test_later_mode_requires_a_roi_and_rule(self):
        cfg, cut, backend = self.make_case()
        cfg['infer_a_when_blank'] = False
        cfg['labels'] = dict(zip('ABCD', 'ABCD'))
        cfg['label_rois'].pop('A')
        with tempfile.TemporaryDirectory() as output:
            with self.assertRaisesRegex(ValueError, 'A/B/C/D'):
                self.analyze(cfg, cut, backend, output)


if __name__ == '__main__':
    unittest.main()
