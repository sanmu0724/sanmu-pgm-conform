"""Manual placements retain multicams/original audio and explicit independent roles."""
import copy
from fractions import Fraction as F
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from multicam_xml import write_multicam_xml
from reference_timeline import assemble_reference_xml, ReferenceTimelineError
from resolve_import_helper import build_resolve_manifest


def time(node, field):
    return F(node.get(field, '0s')[:-1])


def write_session(directory, identity, kind='stereo', parts=1):
    duration = parts * 250
    path = directory / (identity + '.fcpxml')
    sources = [dict(path=str(directory / (identity + '_' + camera + '.mov')), camera=camera,
        start=180000, end=180000 + duration, frames=duration, width=1920, height=1080,
        reel=identity + camera, audio_channels=[1, 2], source_id=identity + '-' + camera,
        session_id=identity, production_id='QA') for camera in 'AB']
    cut = dict(duration=duration, pgm_sources=[dict(path=str(directory / (identity + '_PGM.mov')),
        start=0, end=duration, frames=duration, start_tc='02:00:00:00')])
    events = [dict(start=first, end=min(first + 75, duration), camera='AB'[(first // 75) % 2], path=sources[0]['path'])
              for first in range(0, duration, 75)]
    wavs, audio = [], []
    if kind == 'pgm':
        name = str(directory / (identity + '_PGM.mov'))
        wavs = [dict(path=name, start_frames=180000, fps=25, frames=duration, channels=2,
            source_kind='pgm', original_audio_filename=Path(name).name)]
        audio = [dict(path=name, record_start=F(0), duration=F(duration, 25),
                      source_start=F(7200), src_channel='1, 2')]
    else:
        for section, (record, length) in enumerate(((0, 4), (4, F(duration, 25) - 4)), 1):
            # The strict Vn/An export receives a prepared single stereo file.
            # Actual lossless L/R interleaving is tested in test_stereo_merge;
            # historical two-file XML is covered by test_paired_sound_xml.
            for role in (('merged',) if kind == 'pair' else ('main',)):
                name = str(directory / f'{identity}_{section}_{role}.wav')
                wave = dict(path=name, start=section * 100 * 48000, samples=(duration // 25 + 1) * 48000,
                    channels=2, sample_rate=48000, depth=24, sound_roll='ROLL_' + identity,
                    original_audio_filename=Path(name).name, recording_date='2026-10-20', file_uid=identity + str(section))
                event = dict(path=name, record_start=F(record), duration=F(length),
                    source_start=F(section * 100) + F(section * 7, 48000),
                    src_channel='1, 2')
                if kind == 'pair':
                    wave.update(channel='main',stereo_merge_sidecar=str(directory/f'{identity}_{section}_stereo_merge.json'),
                        merged_from=[dict(role=channel,filename=f'{identity}_{section}_{channel}.wav',file_uid=f'{identity}-{section}-{channel}')
                                     for channel in ('L','R')])
                wavs.append(wave)
                audio.append(event)
    config = dict(fps=25, camera_ids=list('AB'), camera_count=2, show_id=identity,
                  timeline_start_tc='01:00:00:00')
    with patch('cut_xml.timeline_info', return_value=cut):
        write_multicam_xml(config, events, sources, path, identity, (wavs, audio))
    return path


def selection(identity, lane, target=0, source=0, duration=250, take=None):
    return dict(candidate_id=take or identity, session_id=identity, script_line_id=None,
        record_frame_in=source, record_frame_out_exclusive=source + duration,
        target_frame_in=target, target_frame_out_exclusive=target + duration,
        duration_frames=duration, lane=lane, whole_pgm=True)


def own_audio(node):
    for child in node:
        if child.get('lane') and int(child.get('lane')) > 0:
            continue
        if child.tag == 'audio':
            yield child
        elif child.tag in ('clip', 'gap'):
            yield from own_audio(child)


class ManualAudioRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.xmls = {name: write_session(self.folder, name, kind) for name, kind in
                     [('base', 'stereo'), ('stereo', 'stereo'), ('pair', 'pair'), ('pgm', 'pgm')]}

    def assemble(self, selections=(), include_baseline=True, zero=False):
        end = max([250] + [row['target_frame_out_exclusive'] for row in selections])
        start = min([0] + [row['target_frame_in'] for row in selections])
        plan = dict(manual_layout=True, resolve_zero_origin=zero, fps=25, baseline_session_id='base', baseline_duration_frames=250,
            timeline_start_frame=start, timeline_end_frame=end, duration_frames=end - start,
            lane_count=max([0] + [row['lane'] for row in selections]), selections=list(selections))
        output = self.folder / 'manual.fcpxml'
        result = assemble_reference_xml(self.xmls['base'], self.xmls, plan, output,
                                        include_baseline=include_baseline)
        return ET.parse(output).getroot(), result

    def test_all_three_export_modes_keep_only_multicam_picture_and_expected_source_assets(self):
        choices = [selection('stereo', 1), selection('pair', 2), selection('pgm', 3, 50)]
        for name, include, selected, sessions in [('base', True, [], {'base'}),
            ('combined', True, choices, {'base', 'stereo', 'pair', 'pgm'}),
            ('alternatives', False, choices, {'stereo', 'pair', 'pgm'})]:
            with self.subTest(name=name):
                root, result = self.assemble(selected, include)
                sequence = root.find('./library/event/project/sequence')
                self.assertTrue(sequence.findall('.//mc-clip'))
                self.assertFalse(sequence.findall('.//video'))
                self.assertEqual(len(result['manifest']['pieces']), sum(4 for _row in selected))
                pictures = root.findall('./resources/media')
                self.assertEqual({row.get('name').split('_')[0] for row in pictures}, sessions)

    def test_overlapping_prepared_stereo_sounds_follow_video_numbers_and_keep_silent_slot(self):
        choices = [selection('stereo', 1), selection('pair', 2), selection('pgm', 3, 50)]
        root, result = self.assemble(choices)
        routes = result['manifest']['audio_tracks']
        self.assertEqual([(row['index'], row['video_track'], row['subtype']) for row in routes],
                         [(1, 1, 'stereo'), (2, 2, 'stereo'), (3, 3, 'stereo'), (4, 4, 'stereo')])
        self.assertTrue(routes[3]['empty'])
        sequence = root.find('./library/event/project/sequence')
        primary = [node for node in sequence.find('spine') if node.tag == 'clip' and not node.get('lane')]
        self.assertEqual({row.get('role') for host in primary for row in own_audio(host)}, {'dialogue.pgm-track-1'})
        for lane, expected in [(1, {'dialogue.pgm-track-2'}), (2, {'dialogue.pgm-track-3'}),
                               (3, set())]:
            pieces = [node for node in sequence.iter('clip') if node.get('lane') == str(lane)]
            self.assertEqual({row.get('role') for piece in pieces for row in own_audio(piece)}, expected)
        # Stereo components stay unlaned so plain XML retains the linked A/V wrapper.
        self.assertTrue(all(node.get('lane') is None for host in primary for node in own_audio(host)))

    def test_alternative_wrappers_are_flat_spine_siblings_not_inside_baseline_multicam(self):
        root, result = self.assemble([selection('stereo', 1, 25), selection('pair', 2)])
        spine = root.find('./library/event/project/sequence/spine')
        baseline = [row for row in spine if not row.get('lane') and row.tag == 'clip']
        self.assertTrue(baseline)
        self.assertTrue(all(not row.findall('clip[@lane="1"]') and not row.findall('clip[@lane="2"]') for row in baseline))
        alternatives = [row for row in spine if int(row.get('lane', '0')) > 0]
        self.assertEqual(len(alternatives), result['piece_count'])
        stereo = next(row for row in alternatives if row.get('lane') == '1')
        self.assertEqual(time(stereo, 'offset'), F(3601))
        manifest = build_resolve_manifest(result['xml_path'])
        self.assertEqual({group['video']['track_index'] for group in manifest['groups'] if group['video']}, {1, 2, 3})

    def test_audio_resources_source_clocks_sample_offsets_reels_and_dates_are_immutable(self):
        source = ET.parse(self.xmls['stereo']).getroot()
        before_assets = {row.get('id'): copy.deepcopy(row) for row in source.findall('./resources/asset')}
        expected_audio = [dict(node.attrib) for node in source.findall('.//library//audio')]
        root, _result = self.assemble([selection('stereo', 1)])
        actual_assets = [node for node in root.findall('./resources/asset') if node.get('id', '').startswith('alt1-')]
        for actual in actual_assets:
            expected = before_assets[actual.get('id')[5:]]
            self.assertEqual(actual.get('start'), expected.get('start'))
            self.assertEqual(actual.get('src'), expected.get('src'))
            self.assertEqual(ET.tostring(actual.find('metadata')), ET.tostring(expected.find('metadata')))
        imported = [dict(node.attrib) for node in root.findall('.//library//audio') if node.get('ref', '').startswith('alt1-')]
        for actual, expected in zip(imported, expected_audio):
            self.assertEqual(actual['start'], expected['start'])
            self.assertEqual(actual['duration'], expected['duration'])
            self.assertEqual(actual['srcCh'], expected['srcCh'])
            self.assertEqual(actual['outCh'], expected['outCh'])
        self.assertEqual(time(root.find('.//library//clip[@lane="1"]/audio'), 'start'), F(100) + F(7, 48000))

    def test_prepared_split_recordings_use_one_stereo_reference_and_helper(self):
        root, result = self.assemble([selection('pair', 1)])
        manifest = build_resolve_manifest(result['xml_path'])
        groups = [group for group in manifest['groups'] if group['video'] and group['video']['name'].startswith('pair_')]
        self.assertTrue(groups)
        self.assertTrue(all(len(group['audio']) in (1, 2) for group in groups))
        self.assertEqual({row['role'] for group in groups for row in group['audio']}, {'main'})
        self.assertEqual({row['track_index'] for group in groups for row in group['audio']}, {2})
        self.assertTrue(Path(result['xml_path']).with_suffix('.resolve.lua').is_file())
        for asset in root.findall('./resources/asset'):
            if asset.get('id', '').startswith('alt1-sound'):
                self.assertEqual(asset.get('audioChannels'), '2')

    def test_alternatives_only_reserves_same_baseline_audio_slots_as_combined(self):
        self.xmls['base'] = write_session(self.folder, 'base', 'pair')
        choices = [selection('stereo', 1)]
        combined, combined_result = self.assemble(choices)
        alt, alt_result = self.assemble(choices, False)
        roles = lambda root: {row.get('role') for row in root.findall('.//library//audio')
                              if row.get('ref', '').startswith('alt1-')}
        self.assertEqual(roles(combined), {'dialogue.pgm-track-2'})
        self.assertEqual(roles(alt), roles(combined))
        self.assertEqual(alt_result['manifest']['audio_tracks'], combined_result['manifest']['audio_tracks'])

    def test_same_session_two_pgm_ranges_are_not_merged_or_guessed_by_filename(self):
        self.xmls['stereo'] = write_session(self.folder, 'stereo', parts=2)
        choices = [selection('stereo', 1, source=0, take='take-one'),
                   selection('stereo', 1, source=250, target=250, take='take-two')]
        root, result = self.assemble(choices)
        pieces = result['manifest']['pieces']
        self.assertEqual({row['candidate_id'] for row in pieces}, {'take-one', 'take-two'})
        self.assertTrue(all(row['script_line_id'] is None for row in pieces))
        self.assertEqual(len(root.findall('./resources/media/multicam')), 2)
        self.assertEqual({row.get('role') for row in root.findall('.//library//audio')
                          if row.get('ref', '').startswith('alt1-')}, {'dialogue.pgm-track-2'})
        self.assertEqual(sum(F(row['duration'][:-1]) for row in pieces), F(20))

    def test_overlap_on_same_video_lane_is_rejected_before_output(self):
        with self.assertRaisesRegex(ReferenceTimelineError, '轨道上的片段重叠'):
            self.assemble([selection('stereo', 1), selection('pair', 1)])

    def test_stereo_and_pgm_helper_preserves_exact_source_offsets_and_separate_group_routes(self):
        _root, result = self.assemble([selection('stereo', 1), selection('pgm', 2, 50)])
        manifest = build_resolve_manifest(result['xml_path'])
        self.assertTrue(manifest['routing_enabled'])
        self.assertTrue(Path(result['xml_path']).with_suffix('.resolve.lua').is_file())
        groups = manifest['groups']
        stereo = [row for row in groups if row['video'] and row['video']['name'].startswith('stereo_')]
        fallback = [row for row in groups if row['video'] and row['video']['name'].startswith('pgm_')]
        self.assertEqual(stereo[0]['video']['track_index'], 2)
        self.assertEqual(stereo[0]['audio'][0]['source_start'], F(7 * 25, 48000))
        self.assertEqual({audio['track_index'] for row in stereo for audio in row['audio']}, {2})
        self.assertTrue(fallback)
        self.assertTrue(all(not row['audio'] for row in fallback))
        self.assertFalse(any(path.endswith('pgm_PGM.mov') for path in manifest['media_paths']))

    def test_alternatives_only_helper_has_no_baseline_media_or_link_groups(self):
        _root, result = self.assemble([selection('stereo', 1), selection('pair', 2)], False)
        manifest = build_resolve_manifest(result['xml_path'])
        self.assertTrue(manifest['groups'])
        self.assertTrue(all(not row['video']['name'].startswith('base_') for row in manifest['groups'] if row['video']))
        self.assertTrue(all('/base_' not in path for path in manifest['media_paths']))

    def test_zero_clock_interchange_reconstructs_original_record_clock_and_explicit_av_groups(self):
        choices = [selection('stereo', 1, 25), selection('pair', 2), selection('pgm', 3, 50)]
        root, result = self.assemble(choices, zero=True)
        sequence = root.find('./library/event/project/sequence')
        self.assertEqual(time(sequence, 'tcStart'), 0)
        self.assertFalse(sequence.findall('.//clip'))
        self.assertEqual(len(sequence.findall('spine/mc-clip')), 16)
        self.assertTrue(sequence.findall('spine/asset-clip'))
        manifest = build_resolve_manifest(result['xml_path'])
        self.assertEqual((manifest['import_start'], manifest['timeline_start'], manifest['set_start_timecode']),
                         (0, 90000, '01:00:00:00'))
        self.assertEqual(len(manifest['groups']), 16)
        for name, track, first in [('base_', 1, 90000), ('stereo_', 2, 90025), ('pair_', 3, 90000), ('pgm_', 4, 90050)]:
            groups = [row for row in manifest['groups'] if row['video']['name'].startswith(name)]
            self.assertEqual(groups[0]['video']['record_start'], first)
            self.assertTrue(all(row['video']['track_index'] == track for row in groups))
            self.assertTrue(all(bool(row['audio'])==(name!='pgm_') for row in groups))
        pair = next(row for row in manifest['groups'] if row['video']['name'].startswith('pair_'))
        self.assertEqual([row['source_start'] for row in pair['audio']], [F(7 * 25, 48000)])
        self.assertEqual({row['role'] for row in pair['audio']}, {'main'})
        helper = Path(result['xml_path']).with_suffix('.resolve.lua').read_text()
        self.assertIn('timeline:SetStartTimecode(manifest.set_start_timecode)', helper)
        self.assertNotIn('AppendToTimeline', helper)

    def test_zero_clock_export_modes_keep_baseline_and_alternative_source_identity_separate(self):
        for include, choices, expected in [(True, [], {'base_'}),
                (False, [selection('stereo', 1)], {'stereo_'})]:
            root, result = self.assemble(choices, include, zero=True)
            self.assertEqual(time(root.find('./library/event/project/sequence'), 'tcStart'), 0)
            manifest = build_resolve_manifest(result['xml_path'])
            self.assertEqual({row['video']['name'].split('_')[0] + '_' for row in manifest['groups']}, expected)
            self.assertEqual(manifest['timeline_start'], 90000)
            self.assertTrue(all(audio['source_start'] >= 0 for row in manifest['groups'] for audio in row['audio']))

    def test_zero_clock_keeps_original_baseline_record_position_when_alternative_has_leader(self):
        _root, result = self.assemble([selection('stereo', 1, -25)], zero=True)
        manifest = build_resolve_manifest(result['xml_path'])
        self.assertEqual((manifest['timeline_start'], manifest['set_start_timecode']), (89975, '00:59:59:00'))
        baseline = next(row for row in manifest['groups'] if row['video']['name'].startswith('base_'))
        alternative = next(row for row in manifest['groups'] if row['video']['name'].startswith('stereo_'))
        self.assertEqual((baseline['video']['record_start'], alternative['video']['record_start']), (90000, 89975))
        self.assertEqual(baseline['audio'][0]['source_start'], F(7 * 25, 48000))


if __name__ == '__main__':
    unittest.main()
