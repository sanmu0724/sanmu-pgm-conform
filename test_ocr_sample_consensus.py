"""Shot-level recovery needs distinct, agreeing frames and preserves every cut."""
from contextlib import contextmanager
import csv
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from core import AnalysisError
from cut_xml import analyze_cut_xml


def labelled(camera='B'):
    return camera, 'label_read', .95, [camera]


def unknown(reason='unreadable_label'):
    return '?', reason, 0., ['unreadable']


BLANK_A = ('A', 'A_inferred_blank', 0., [])
MULTIPLE = ('?', 'multiple_labels', .95, ['B', 'C'])


class SampleConsensusTests(unittest.TestCase):
    @contextmanager
    def case(self, decisions=None, *, source_shots=((20,),), default=None,
             cache=False, on_inspect=None, read_error_at=None, inspect_error_at=None):
        """Exercise the public analyzer with deterministic source-frame evidence."""
        decisions = decisions or {}
        default = labelled() if default is None else default
        blocks, shots, cursor = [], [], 0
        for source_number, lengths in enumerate(source_shots):
            path = f'fixture-{source_number}.mov'
            block = dict(path=path, start=cursor, end=cursor + sum(lengths),
                         frames=sum(lengths), width=100, height=50,
                         start_tc='01:00:00:00')
            blocks.append(block)
            source_cursor = 0
            for length in lengths:
                shots.append(dict(start=cursor, end=cursor + length,
                                  source_in=source_cursor, source_out=source_cursor + length,
                                  pgm_path=path))
                source_cursor += length
                cursor += length
        cut = dict(fps=25, xml_digest='sample-consensus-fixture', duration=cursor,
                   pgm_info=blocks[0], pgm_sources=blocks, shots=shots)
        config = dict(fps=25, labels=dict(zip('ABCD', 'ABCD')),
                      label_rois={camera: [0, 0, 1, 1] for camera in 'ABCD'},
                      label_match_mode='prefix', infer_a_when_blank=True)
        reads, inspections, instances = [], [], []
        starts = {block['path']: block['start'] for block in blocks}

        def read_frame(path, index):
            reads.append((path, index))
            if starts[path] + index == read_error_at:
                raise OSError('source frame failed')
            return path, index

        class FakeLabelReader:
            def __init__(self, *args, **kwargs):
                self.calls = self.hits = 0
                self.cache = set()
                instances.append(self)

            def inspect(self, frame, rois, index=0):
                path, source_index = frame
                # The inspector receives the timeline position, while the
                # decoder must receive the original file's source position.
                if index != starts[path] + source_index:
                    raise AssertionError('source and timeline positions were mixed')
                inspections.append((path, source_index, index))
                if index == inspect_error_at:
                    raise RuntimeError('OCR failed')
                result = decisions.get(index, default)
                key = (result[0], result[1], result[2], tuple(result[3]))
                if cache and key in self.cache:
                    self.hits += 1
                else:
                    self.calls += 1
                    self.cache.add(key)
                if on_inspect:
                    on_inspect(index)
                return result

        with tempfile.TemporaryDirectory() as directory:
            fixture = SimpleNamespace(config=config, cut=cut, output=Path(directory),
                                      reads=reads, inspections=inspections, readers=instances)
            with patch('cut_xml.timeline_info', return_value=cut), patch(
                    'cut_xml.read_frame', side_effect=read_frame), patch(
                    'ocr.LabelReader', FakeLabelReader):
                yield fixture

    def analyze(self, fixture, **kwargs):
        segments = analyze_cut_xml(fixture.config, fixture.output, **kwargs)
        saved = json.loads((fixture.output / 'analysis.json').read_text(encoding='utf-8'))
        with (fixture.output / 'ocr_samples.csv').open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        # Every actual read is auditable, and duplicate reads cannot inflate
        # either the sample count or the number of agreeing source frames.
        self.assertEqual([(row['pgm_file'], int(row['pgm_frame'])) for row in rows], fixture.reads)
        self.assertEqual(len(fixture.reads), len(set(fixture.reads)))
        self.assertEqual(saved['ocr_stats']['sampled_frames'], len(fixture.reads))
        self.assertEqual(saved['ocr_stats']['calls'], fixture.readers[0].calls)
        self.assertEqual(saved['ocr_stats']['cache_hits'], fixture.readers[0].hits)
        expected_initial = sum(len({min(shot['source_in'] + offset, shot['source_out'] - 1)
                                    for offset in (0, 2, 5)}) for shot in fixture.cut['shots'])
        self.assertEqual(saved['ocr_stats']['retry_frames'], len(rows) - expected_initial)
        self.assertEqual([(segment.start, segment.end) for segment in segments],
                         [(shot['start'], shot['end']) for shot in fixture.cut['shots']])
        self.assertEqual([(row['start'], row['end']) for row in saved['segments']],
                         [(shot['start'], shot['end']) for shot in fixture.cut['shots']])
        for row in rows:
            shot = fixture.cut['shots'][int(row['shot']) - 1]
            self.assertEqual(row['pgm_file'], shot['pgm_path'])
            self.assertGreaterEqual(int(row['pgm_frame']), shot['source_in'])
            self.assertLess(int(row['pgm_frame']), shot['source_out'])
            self.assertEqual(int(row['timeline_frame']), shot['start'] +
                             int(row['pgm_frame']) - shot['source_in'])
        return segments, saved, rows

    def test_normal_unanimous_shot_keeps_three_original_samples(self):
        with self.case() as fixture:
            segments, saved, rows = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason), ('B', 'label_read'))
            self.assertEqual([int(row['pgm_frame']) for row in rows], [0, 2, 5])
            self.assertEqual(saved['ocr_stats']['retry_frames'], 0)

    def test_one_clear_sample_recovers_with_two_new_frames_without_merging_cuts(self):
        with self.case({2: unknown(), 5: unknown()}, source_shots=((20, 20),)) as fixture:
            segments, saved, rows = self.analyze(fixture)
            self.assertEqual([(segment.camera, segment.reason) for segment in segments],
                             [('B', 'label_read_consensus'), ('B', 'label_read')])
            self.assertEqual([int(row['pgm_frame']) for row in rows],
                             [0, 2, 5, 8, 12, 20, 22, 25])
            self.assertEqual(saved['ocr_stats']['retry_frames'], 2)
            self.assertEqual([row['camera'] for row in rows[:5]], ['B', '?', '?', 'B', 'B'])

    def test_two_clear_samples_need_additional_distinct_evidence(self):
        with self.case({5: unknown()}) as fixture:
            segments, _, rows = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason),
                             ('B', 'label_read_consensus'))
            indices = [int(row['pgm_frame']) for row in rows]
            self.assertEqual(indices[:3], [0, 2, 5])
            self.assertIn(indices[3:], ([8], [8, 12]))

    def test_two_clear_samples_are_not_enough_when_both_retries_are_unknown(self):
        decisions = {0: labelled(), 2: labelled()}
        with self.case(decisions, default=unknown()) as fixture:
            segments, saved, rows = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason),
                             ('?', 'sample_incomplete'))
            self.assertEqual([int(row['pgm_frame']) for row in rows], [0, 2, 5, 8, 12])
            self.assertEqual(saved['ocr_stats']['retry_frames'], 2)

    def test_all_unknown_preserves_first_reason_without_speculative_retries(self):
        for reason in ('no_label', 'unreadable_label'):
            with self.subTest(reason=reason), self.case(default=unknown(reason)) as fixture:
                segments, saved, _ = self.analyze(fixture)
                self.assertEqual((segments[0].camera, segments[0].reason), ('?', reason))
                self.assertEqual(saved['ocr_stats']['sampled_frames'], 3)
                self.assertEqual(saved['ocr_stats']['retry_frames'], 0)
        with self.case({0: unknown('no_label')}, default=unknown()) as fixture:
            segments, _, _ = self.analyze(fixture)
            self.assertEqual(segments[0].reason, 'no_label')

    def test_different_known_cameras_are_conflicts_not_majority_votes(self):
        for contradictory in (labelled('C'), BLANK_A):
            with self.subTest(contradictory=contradictory), self.case({5: contradictory}) as fixture:
                segments, saved, _ = self.analyze(fixture)
                self.assertEqual((segments[0].camera, segments[0].reason),
                                 ('?', 'sample_conflict'))
                self.assertEqual(saved['ocr_stats']['retry_frames'], 0)

    def test_multiple_labels_within_one_frame_always_blocks_consensus(self):
        for decisions, default in (({2: MULTIPLE}, labelled()), ({}, MULTIPLE)):
            with self.subTest(decisions=decisions), self.case(decisions, default=default) as fixture:
                segments, saved, _ = self.analyze(fixture)
                self.assertEqual((segments[0].camera, segments[0].reason),
                                 ('?', 'sample_conflict'))
                self.assertEqual(saved['ocr_stats']['retry_frames'], 0)

    def test_a_conflict_discovered_during_retry_cannot_be_outvoted(self):
        for contradictory in (labelled('C'), MULTIPLE, BLANK_A):
            decisions = {2: unknown(), 5: unknown(), 8: contradictory}
            with self.subTest(contradictory=contradictory), self.case(decisions) as fixture:
                segments, saved, _ = self.analyze(fixture)
                self.assertEqual((segments[0].camera, segments[0].reason),
                                 ('?', 'sample_conflict'))
                self.assertIn(saved['ocr_stats']['retry_frames'], (1, 2))

    def test_blank_a_plus_unknown_does_not_become_a_reliable_a(self):
        with self.case({2: unknown()}, default=BLANK_A) as fixture:
            segments, saved, _ = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason),
                             ('?', 'sample_incomplete'))
            self.assertEqual(saved['ocr_stats']['retry_frames'], 0)
        with self.case(default=BLANK_A) as fixture:
            segments, saved, _ = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason),
                             ('A', 'A_inferred_blank'))
            self.assertEqual(saved['ocr_stats']['retry_frames'], 0)

    def test_explicit_a_can_recover_using_three_explicit_a_frames(self):
        with self.case({2: unknown(), 5: unknown()}, default=labelled('A')) as fixture:
            fixture.config['infer_a_when_blank'] = False
            segments, _, _ = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason),
                             ('A', 'label_read_consensus'))

    def test_four_frame_shot_uses_remaining_frame_without_crossing_cut(self):
        with self.case({0: unknown()}, source_shots=((4, 20),)) as fixture:
            segments, saved, rows = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason),
                             ('B', 'label_read_consensus'))
            first_shot = [int(row['pgm_frame']) for row in rows if row['shot'] == '1']
            self.assertEqual(first_shot, [0, 2, 3, 1])
            self.assertEqual(saved['ocr_stats']['retry_frames'], 1)
            self.assertEqual((segments[1].camera, segments[1].reason), ('B', 'label_read'))

    def test_short_shots_cannot_reuse_cached_reads_as_three_clear_frames(self):
        for length, expected_reads, expected_retries in ((2, [0, 1], 0), (3, [0, 2, 1], 1)):
            with self.subTest(length=length), self.case(
                    {0: unknown()}, source_shots=((length,),), cache=True) as fixture:
                segments, saved, rows = self.analyze(fixture)
                self.assertEqual((segments[0].camera, segments[0].reason),
                                 ('?', 'sample_incomplete'))
                self.assertEqual([int(row['pgm_frame']) for row in rows], expected_reads)
                self.assertEqual(saved['ocr_stats']['retry_frames'], expected_retries)
                self.assertEqual(saved['ocr_stats']['calls'], 2)
                self.assertEqual(saved['ocr_stats']['cache_hits'], length - 2)

    def test_one_frame_unanimous_shot_keeps_existing_label_without_duplicate_reads(self):
        with self.case(source_shots=((1,),), cache=True) as fixture:
            segments, saved, _ = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason), ('B', 'label_read'))
            self.assertEqual(saved['ocr_stats']['sampled_frames'], 1)
            self.assertEqual(saved['ocr_stats']['retry_frames'], 0)

    def test_cached_ocr_results_still_need_three_distinct_source_frames(self):
        with self.case({2: unknown(), 5: unknown()}, cache=True) as fixture:
            segments, saved, rows = self.analyze(fixture)
            self.assertEqual((segments[0].camera, segments[0].reason),
                             ('B', 'label_read_consensus'))
            self.assertEqual([int(row['pgm_frame']) for row in rows if row['camera'] == 'B'],
                             [0, 8, 12])
            self.assertEqual(saved['ocr_stats']['calls'], 2)
            self.assertEqual(saved['ocr_stats']['cache_hits'], 3)

    def test_retry_positions_use_each_recordings_source_coordinates(self):
        with self.case({22: unknown(), 25: unknown()}, source_shots=((20,), (20,))) as fixture:
            segments, _, rows = self.analyze(fixture)
            self.assertEqual(segments[1].reason, 'label_read_consensus')
            self.assertEqual([(row['pgm_file'], int(row['pgm_frame']), int(row['timeline_frame']))
                              for row in rows[3:]],
                             [('fixture-1.mov', index, 20 + index) for index in (0, 2, 5, 8, 12)])

    def test_cancel_before_sampling_performs_no_reads_and_writes_no_analysis(self):
        with self.case() as fixture:
            with self.assertRaisesRegex(AnalysisError, '已取消'):
                analyze_cut_xml(fixture.config, fixture.output, cancelled=lambda: True)
            self.assertEqual(fixture.reads, [])
            self.assertFalse((fixture.output / 'analysis.json').exists())

    def test_cancel_during_retry_propagates_and_stops_before_next_frame(self):
        cancelled = [False]

        def on_inspect(index):
            if index == 8:
                cancelled[0] = True

        with self.case({2: unknown(), 5: unknown()}, on_inspect=on_inspect) as fixture:
            with self.assertRaisesRegex(AnalysisError, '已取消'):
                analyze_cut_xml(fixture.config, fixture.output, cancelled=lambda: cancelled[0])
            self.assertEqual([index for _, index in fixture.reads], [0, 2, 5, 8])
            self.assertFalse((fixture.output / 'analysis.json').exists())

    def test_retry_decoder_and_ocr_failures_propagate(self):
        cases = ((dict(read_error_at=8), OSError, 'source frame failed'),
                 (dict(inspect_error_at=8), RuntimeError, 'OCR failed'))
        for options, error, message in cases:
            with self.subTest(options=options), self.case({2: unknown(), 5: unknown()}, **options) as fixture:
                with self.assertRaisesRegex(error, message):
                    analyze_cut_xml(fixture.config, fixture.output)
                self.assertEqual([index for _, index in fixture.reads], [0, 2, 5, 8])
                self.assertFalse((fixture.output / 'analysis.json').exists())


if __name__ == '__main__':
    unittest.main()
