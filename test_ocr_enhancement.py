"""Conservative OCR recovery keeps confidence, conflicts and cache bounds intact."""
import unittest

import numpy as np

from ocr import LabelReader, match_label, normalize


def observations(text, confidence=.95):
    return [dict(text=text, confidence=confidence)]


class EnhancedOCR:
    def __init__(self, raw=None, enhanced=None):
        self.raw = [] if raw is None else raw
        self.enhanced = [] if enhanced is None else enhanced
        self.raw_inputs = []
        self.enhanced_inputs = []

    @staticmethod
    def result(values, marker):
        return values.get(marker, []) if isinstance(values, dict) else values

    def recognize(self, rgb):
        marker = int(rgb[0, 0, 0])
        self.raw_inputs.append(marker)
        return self.result(self.raw, marker)

    def recognize_enhanced(self, rgb):
        marker = int(rgb[0, 0, 0])
        self.enhanced_inputs.append(marker)
        return self.result(self.enhanced, marker)


class ConfusableNormalizationTests(unittest.TestCase):
    def test_nfkc_normalizes_full_width_camera_codes_and_digits(self):
        self.assertEqual(normalize(' Ｂ００３Ｃ００１＿ｂ '), 'B003C001B')
        self.assertEqual(normalize('４４０－０００１'), '4400001')
        self.assertEqual(match_label('Ｂ００３Ｃ００１', {'B': 'B'}, match_mode='prefix'), 'B')

    def test_visual_cyrillic_and_greek_latin_glyphs_are_preserved(self):
        for source, expected in (('АВСЕНКМОРТХ', 'ABCEHKMOPTX'),
                                 ('авсенкмортх', 'ABCEHKMOPTX'),
                                 ('ΑΒΕΗΚΜΟΡΤΧ', 'ABEHKMOPTX')):
            with self.subTest(source=source):
                self.assertEqual(normalize(source), expected)
        self.assertEqual(match_label('В003С001', {'B': 'B'}, match_mode='prefix'), 'B')
        self.assertEqual(match_label('в003с001', {'B': 'B'}, match_mode='prefix'), 'B')

    def test_numeric_camera_rules_accept_nfkc_without_rewriting_digits_as_letters(self):
        self.assertEqual(normalize('０８18ОВ'), '0818OB')
        self.assertEqual(normalize('B80001'), 'B80001')
        self.assertEqual(match_label('４４０', {'C': '440'}, match_mode='exact'), 'C')
        for source, target in (('0', 'O'), ('8', 'B'), ('80001', 'B'), ('00001', 'O')):
            with self.subTest(source=source, target=target):
                self.assertIsNone(match_label(source, {'B': target}, match_mode='prefix'))
        # Existing digit correction remains directional and target-dependent.
        self.assertEqual(match_label('44О', {'C': '440'}, match_mode='exact'), 'C')

    def test_unknown_leading_unicode_letters_do_not_expose_a_false_prefix(self):
        for source in ('ЖB003C001', '中B003C001'):
            with self.subTest(source=source):
                self.assertIsNone(match_label(source, {'B': 'B'}, match_mode='prefix'))
        self.assertEqual(match_label('ВООЗC001', {'B': 'B'}, match_mode='prefix'), 'B')


class EnhancementTests(unittest.TestCase):
    pixels = np.zeros((24, 72, 3), np.uint8)
    shared_rois = {camera: [0, 0, 1, 1] for camera in 'ABCD'}

    def reader(self, backend, **options):
        config = dict(labels=dict(zip('ABCD', 'ABCD')), label_match_mode='prefix',
                      infer_a_when_blank=False, ocr_confidence=.65)
        config.update(options)
        return LabelReader(config, backend)

    def separate_rois(self):
        pixels = np.empty((24, 72, 3), np.uint8)
        for marker, start in ((10, 0), (20, 24), (30, 48)):
            pixels[:, start:start + 24] = marker
        rois = {'B': [0, 0, 1 / 3, 1], 'C': [1 / 3, 0, 1 / 3, 1],
                'D': [2 / 3, 0, 1 / 3, 1]}
        return pixels, rois

    def test_clear_label_uses_only_original_recognition(self):
        backend = EnhancedOCR(observations('B003C001'), observations('D003C001'))
        reader = self.reader(backend)
        result = reader.inspect(self.pixels, self.shared_rois)
        self.assertEqual(result[:2], ('B', 'label_read'))
        self.assertEqual(backend.raw_inputs, [0])
        self.assertEqual(backend.enhanced_inputs, [])
        self.assertEqual(reader.enhanced_calls, 0)

    def test_clear_label_with_harmless_numeric_fragments_needs_no_enhancement(self):
        backend = EnhancedOCR(observations('B') + observations('40_00'), observations('D'))
        reader = self.reader(backend)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], 'B')
        self.assertEqual(backend.enhanced_inputs, [])

    def test_unknown_label_can_recover_after_one_enhancement(self):
        backend = EnhancedOCR(observations('NOISE'), observations('B003C001', .9))
        reader = self.reader(backend)
        result = reader.inspect(self.pixels, self.shared_rois)
        self.assertEqual(result[0], 'B')
        self.assertGreaterEqual(result[2], .65)
        self.assertEqual(backend.raw_inputs, [0])
        self.assertEqual(backend.enhanced_inputs, [0])
        self.assertEqual(reader.enhanced_calls, 1)

    def test_blank_without_a_inference_can_recover_an_explicit_label(self):
        backend = EnhancedOCR([], observations('C003C001'))
        reader = self.reader(backend)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], 'C')
        self.assertEqual(backend.enhanced_inputs, [0])

    def test_repeated_pixels_reuse_original_and_enhanced_results(self):
        backend = EnhancedOCR(observations('NOISE'), observations('B'))
        reader = self.reader(backend)
        results = [reader.inspect(self.pixels.copy(), self.shared_rois, index)
                   for index in (0, 2, 5, 8, 12)]
        self.assertTrue(all(result[0] == 'B' for result in results))
        self.assertEqual(backend.raw_inputs, [0])
        self.assertEqual(backend.enhanced_inputs, [0])
        self.assertEqual(reader.enhanced_calls, 1)

    def test_unsuccessful_enhancement_is_cached_too(self):
        backend = EnhancedOCR(observations('NOISE'), observations('SMUDGE'))
        reader = self.reader(backend)
        for index in (0, 2, 5):
            self.assertEqual(reader.inspect(self.pixels.copy(), self.shared_rois, index)[0], '?')
        self.assertEqual(backend.raw_inputs, [0])
        self.assertEqual(backend.enhanced_inputs, [0])
        self.assertEqual(reader.enhanced_calls, 1)

    def test_enhanced_label_must_meet_the_original_confidence_threshold(self):
        for confidence in (.2, .64):
            with self.subTest(confidence=confidence):
                backend = EnhancedOCR(observations('NOISE'), observations('B', confidence))
                reader = self.reader(backend)
                self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], '?')
                self.assertEqual(reader.enhanced_calls, 1)
        backend = EnhancedOCR(observations('NOISE'), observations('B', .7))
        reader = self.reader(backend, ocr_confidence=.8)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], '?')

    def test_low_confidence_label_can_recover_only_as_the_same_camera(self):
        backend = EnhancedOCR(observations('B003C001', .2), observations('B003C001', .9))
        reader = self.reader(backend)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], 'B')
        self.assertEqual(reader.enhanced_calls, 1)
        backend = EnhancedOCR(observations('B003C001', .2), observations('D003C001', .99))
        reader = self.reader(backend)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], '?')
        self.assertEqual(reader.enhanced_calls, 1)

    def test_enhancement_cannot_override_a_low_confidence_camera_in_another_roi(self):
        pixels, rois = self.separate_rois()
        backend = EnhancedOCR({10: observations('B', .2), 30: observations('NOISE', .9)},
                              {30: observations('D', .99)})
        reader = self.reader(backend, infer_a_when_blank=True)
        self.assertEqual(reader.inspect(pixels, rois)[0], '?')
        self.assertEqual(backend.enhanced_inputs, [30])

    def test_enhancement_cannot_choose_between_two_original_low_confidence_cameras(self):
        backend = EnhancedOCR(observations('B', .2) + observations('D', .3),
                              observations('D', .99))
        reader = self.reader(backend)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], '?')

    def test_original_multiple_camera_labels_are_never_enhanced_away(self):
        backend = EnhancedOCR(observations('B') + observations('D'), observations('B'))
        reader = self.reader(backend)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[:2], ('?', 'multiple_labels'))
        self.assertEqual(backend.enhanced_inputs, [])
        self.assertEqual(reader.enhanced_calls, 0)

    def test_enhanced_multiple_camera_labels_still_remain_unknown(self):
        backend = EnhancedOCR(observations('NOISE'), observations('B') + observations('D'))
        reader = self.reader(backend)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], '?')
        self.assertEqual(reader.enhanced_calls, 1)

    def test_existing_blank_a_inference_needs_no_enhancement(self):
        backend = EnhancedOCR([], observations('B'))
        reader = self.reader(backend, infer_a_when_blank=True)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[:2],
                         ('A', 'A_inferred_blank'))
        self.assertEqual(backend.enhanced_inputs, [])
        self.assertEqual(reader.enhanced_calls, 0)

    def test_enhancement_cannot_erase_unreadable_text_into_inferred_a(self):
        backend = EnhancedOCR(observations('NOISE'), [])
        reader = self.reader(backend, infer_a_when_blank=True)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[0], '?')
        self.assertEqual(reader.enhanced_calls, 1)

    def test_backends_without_enhancement_keep_existing_unknown_result(self):
        class OriginalOnlyOCR:
            def recognize(self, rgb):
                return observations('NOISE')

        reader = self.reader(OriginalOnlyOCR())
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois)[:2],
                         ('?', 'unreadable_label'))
        self.assertEqual(reader.enhanced_calls, 0)

    def test_roi_with_text_is_preferred_over_empty_rois(self):
        pixels, rois = self.separate_rois()
        backend = EnhancedOCR({10: observations(' ', 1.), 20: observations('NOISE', .1)},
                              {10: observations('B'), 20: observations('C'), 30: observations('D')})
        reader = self.reader(backend, infer_a_when_blank=True)
        self.assertEqual(reader.inspect(pixels, rois)[0], 'C')
        self.assertEqual(backend.enhanced_inputs, [20])
        self.assertEqual(reader.enhanced_calls, 1)

    def test_highest_confidence_text_roi_is_selected_once(self):
        pixels, rois = self.separate_rois()
        backend = EnhancedOCR({10: observations('NOISE', .4), 20: observations('SMUDGE', .9),
                               30: observations('TEXT', .5)},
                              {10: observations('B'), 20: observations('C'), 30: observations('D')})
        reader = self.reader(backend, infer_a_when_blank=True)
        self.assertEqual(reader.inspect(pixels, rois)[0], 'C')
        self.assertEqual(backend.enhanced_inputs, [20])
        self.assertEqual(reader.enhanced_calls, 1)

    def test_unresolved_frame_does_not_try_other_rois_when_repeated(self):
        pixels, rois = self.separate_rois()
        backend = EnhancedOCR({10: observations('NOISE', .4), 20: observations('SMUDGE', .9),
                               30: observations('TEXT', .5)}, observations('UNREADABLE'))
        reader = self.reader(backend, infer_a_when_blank=True)
        for index in (0, 2, 5):
            self.assertEqual(reader.inspect(pixels.copy(), rois, index)[0], '?')
        self.assertEqual(backend.raw_inputs, [10, 20, 30])
        self.assertEqual(backend.enhanced_inputs, [20])
        self.assertEqual(reader.enhanced_calls, 1)

    def test_oversized_rois_do_not_trigger_expensive_enhancement(self):
        backend = EnhancedOCR(observations('NOISE'), observations('B'))
        reader = self.reader(backend)
        pixels = np.zeros((1024, 513, 3), np.uint8)
        self.assertEqual(reader.inspect(pixels, self.shared_rois)[0], '?')
        self.assertEqual(backend.raw_inputs, [0])
        self.assertEqual(backend.enhanced_inputs, [])
        self.assertEqual(reader.enhanced_calls, 0)

    def test_enhancement_backend_errors_propagate(self):
        class FailedEnhancedOCR(EnhancedOCR):
            def recognize_enhanced(self, rgb):
                raise RuntimeError('enhancement OCR failed')

        reader = self.reader(FailedEnhancedOCR(observations('NOISE')))
        with self.assertRaisesRegex(RuntimeError, 'enhancement OCR failed'):
            reader.inspect(self.pixels, self.shared_rois)

    def test_enhanced_cache_shares_existing_bounded_crop_cache(self):
        backend = EnhancedOCR(observations('NOISE'), observations('B'))
        reader = self.reader(backend, ocr_cache_bytes=self.pixels.nbytes * 2)
        for index in range(12):
            pixels = np.full_like(self.pixels, index)
            self.assertEqual(reader.inspect(pixels, self.shared_rois, index)[0], 'B')
            self.assertLessEqual(reader.cache_bytes, reader.cache_limit)
            self.assertLessEqual(len(reader.cache), 2)
        self.assertEqual(len(backend.raw_inputs), 12)
        self.assertEqual(reader.enhanced_calls, 12)
        # Eviction releases both versions; retained pixels need neither pass.
        self.assertEqual(reader.inspect(pixels.copy(), self.shared_rois, 20)[0], 'B')
        self.assertEqual(len(backend.raw_inputs), 12)
        self.assertEqual(reader.enhanced_calls, 12)
        self.assertEqual(reader.inspect(self.pixels, self.shared_rois, 21)[0], 'B')
        self.assertEqual(len(backend.raw_inputs), 13)
        self.assertEqual(reader.enhanced_calls, 13)


if __name__ == '__main__':
    unittest.main()
