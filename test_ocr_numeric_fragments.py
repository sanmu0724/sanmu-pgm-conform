"""A valid camera label can coexist with unrelated counter/timecode fragments."""
import unittest

import numpy as np

from ocr import LabelReader


class FakeOCR:
    def __init__(self, observations):
        self.observations = observations

    def recognize(self, rgb):
        return self.observations


class NumericFragmentTests(unittest.TestCase):
    frame = np.zeros((50, 100, 3), np.uint8)
    rois = {camera: [0, 0, 1, 1] for camera in 'BCD'}

    def inspect(self, observations, **options):
        config = dict(labels={'B': 'B', 'C': '440', 'D': 'D'},
                      label_match_mode='exact', infer_a_when_blank=True,
                      ocr_confidence=.65)
        config.update(options)
        return LabelReader(config, FakeOCR(observations)).inspect(self.frame, self.rois)

    def observations(self, *texts, confidence=1.):
        return [dict(text=text, confidence=confidence) for text in texts]

    def test_historical_numeric_label_and_extra_digits_are_read(self):
        observations = self.observations('440', '1', '40_00', '10_00')
        for ordered in (observations, list(reversed(observations))):
            with self.subTest(order=[o['text'] for o in ordered]):
                result = self.inspect(ordered)
                self.assertEqual(result[:3], ('C', 'label_read', 1.))
                self.assertEqual(result[3], [o['text'] for o in ordered])

    def test_numeric_fragments_with_common_ocr_substitutions_are_ignored(self):
        for fragment in ('I', 'O', 'L', '4O_OO', 'lO_00', '00:01:23:04'):
            with self.subTest(fragment=fragment):
                observations = self.observations('B') + self.observations(fragment, confidence=.1)
                self.assertEqual(self.inspect(observations)[:2], ('B', 'label_read'))

    def test_fragments_without_a_reliable_label_never_infer_blank_a(self):
        for texts in (('1',), ('I',), ('40_00', '10_00')):
            with self.subTest(texts=texts):
                self.assertEqual(self.inspect(self.observations(*texts))[:2],
                                 ('?', 'unreadable_label'))
        self.assertEqual(self.inspect([])[:2], ('A', 'A_inferred_blank'))

    def test_low_confidence_label_is_not_rescued_by_fragments(self):
        observations = self.observations('440', confidence=.2) + self.observations('1', '10_00')
        self.assertEqual(self.inspect(observations)[:2], ('?', 'unreadable_label'))

    def test_low_confidence_matching_label_still_blocks_a_reliable_label(self):
        observations = self.observations('440') + self.observations('B', confidence=.2)
        self.assertEqual(self.inspect(observations)[:2], ('?', 'unreadable_label'))
        observations = self.observations('440') + self.observations('440', confidence=.2)
        self.assertEqual(self.inspect(observations)[:2], ('?', 'unreadable_label'))

    def test_unknown_non_numeric_text_still_requires_review(self):
        for text in ('XYZ', '440g', '?', '1A', '4S0', '编号1', '1?'):
            with self.subTest(text=text):
                self.assertEqual(self.inspect(self.observations('440', text))[:2],
                                 ('?', 'unreadable_label'))

    def test_two_reliable_camera_labels_are_still_a_conflict(self):
        self.assertEqual(self.inspect(self.observations('B', '440', '1'))[:2],
                         ('?', 'multiple_labels'))

    def test_configured_numeric_camera_is_never_treated_as_a_fragment(self):
        labels = {'B': '1', 'C': '440', 'D': 'D'}
        for text in ('1', 'I', 'L'):
            with self.subTest(text=text):
                self.assertEqual(self.inspect(self.observations('440', text), labels=labels)[:2],
                                 ('?', 'multiple_labels'))
                observations = self.observations('440') + self.observations(text, confidence=.2)
                self.assertEqual(self.inspect(observations, labels=labels)[:2],
                                 ('?', 'unreadable_label'))

    def test_configured_ocr_substitution_letter_is_a_real_camera_label(self):
        for letter in ('O', 'I', 'L'):
            with self.subTest(letter=letter):
                labels = {'B': letter, 'C': '440', 'D': 'D'}
                self.assertEqual(self.inspect(self.observations('440', letter), labels=labels)[:2],
                                 ('?', 'multiple_labels'))

    def test_ambiguous_numeric_rules_are_not_silently_ignored(self):
        cases = [('prefix', {'B': '1', 'C': '440', 'D': '10'}, '100'),
                 ('exact', {'B': '1', 'C': '440', 'D': '1'}, '1'),
                 ('legacy', {'B': '1', 'C': '440', 'D': '1'}, 'I')]
        for mode, labels, text in cases:
            with self.subTest(mode=mode):
                self.assertEqual(self.inspect(self.observations('440', text), labels=labels,
                                              label_match_mode=mode)[:2],
                                 ('?', 'unreadable_label'))

    def test_counter_fragment_support_preserves_each_matching_mode(self):
        for mode in ('legacy', 'prefix', 'exact'):
            with self.subTest(mode=mode):
                self.assertEqual(self.inspect(self.observations('440', '1', '40_00'),
                                              label_match_mode=mode)[:2], ('C', 'label_read'))


if __name__ == '__main__':
    unittest.main()
