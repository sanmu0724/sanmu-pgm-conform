import copy
from fractions import Fraction as F
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from multicam_xml import write_multicam_xml
from reference_timeline import (ReferenceTimelineError, assemble_reference_xml,
                                build_reference_plan)
from proxy_workflow import conform_edited_xml, file_path


def time(element, key):
    return F(element.get(key)[:-1])


def dataset():
    sessions = [dict(id='base', name='day01_通条', config={}),
                dict(id='alt', name='day02_分镜头', config={}),
                dict(id='second', name='day03_分镜头', config={})]
    scripts = [dict(id='line1', text='Good morning. 早上好！', order=1),
               dict(id='line2', text='The end. 结束了。', order=2)]
    transcripts, candidates = [], []
    for identity, session, line, begin, end in (
            ('base1', 'base', 'line1', 1, 4), ('base2', 'base', 'line2', 8, 10),
            ('alt1', 'alt', 'line1', 2, 7.2), ('alt2', 'alt', 'line2', 5, 10),
            ('second1', 'second', 'line1', 0, 3)):
        transcripts.append(dict(id='t-'+identity, session_id=session, kind='pgm', media_path='/'+session+'.mov',
                                start_seconds=begin, end_seconds=end, text=identity, active=True))
        candidates.append(dict(id=identity, transcript_id='t-'+identity, script_line_id=line,
                               status='confirmed', score=0.99))
    cuts = {row['id']: dict(fps=25, duration=250, pgm_sources=[
        dict(path='/'+row['id']+'.mov', start=0, end=250, frames=250)]) for row in sessions}
    return dict(sessions=sessions, scripts=scripts, transcripts=transcripts, candidates=candidates), cuts


class ReferencePlanTests(unittest.TestCase):
    def setUp(self):
        self.data, self.cuts = dataset()

    def plan(self, selected=('alt1',), **kwargs):
        return build_reference_plan(self.data, 'base', selected, cuts=self.cuts, **kwargs)

    def test_dialogue_anchor_uses_file_first_line_and_preserves_full_file(self):
        self.cuts['base']['pgm_sources'][0]['start_tc'] = '01:00:00:00'
        self.cuts['alt']['pgm_sources'][0]['start_tc'] = '22:44:00:00'
        row = self.plan()['selections'][0]
        self.assertEqual((row['target_frame_in'], row['target_frame_out_exclusive']), (-25, 225))
        self.assertEqual((row['record_frame_in'], row['record_frame_out_exclusive']), (0, 250))
        self.assertEqual((row['pgm_frame_in'], row['pgm_frame_out_exclusive']), (0, 250))
        self.assertEqual(row['duration_frames'], 250)
        self.assertEqual(row['target_frame_in']+row['dialogue_pgm_frame_in'], row['baseline_dialogue_frame_in'])

    def test_multiple_sentences_same_file_select_one_take_and_overlapping_files_get_lanes(self):
        plan = self.plan(('alt2', 'second1', 'alt1'))
        self.assertEqual([row['script_line_id'] for row in plan['selections']], ['line1', 'line1'])
        rows = {row['candidate_id']: row for row in plan['selections']}
        self.assertEqual(set(rows), {'alt1', 'second1'})
        self.assertNotEqual(rows['second1']['lane'], rows['alt1']['lane'])
        self.assertEqual(plan['baseline_duration_frames'], 250)
        self.assertEqual((plan['timeline_start_frame'], plan['timeline_end_frame'], plan['duration_frames']), (-25, 275, 300))

    def test_base_only_plan_is_valid(self):
        plan = self.plan(())
        self.assertEqual(plan['duration_frames'], 250)
        self.assertEqual(plan['lane_count'], 0)

    def test_duplicate_selections_not_duplicate_takes(self):
        self.assertEqual(len(self.plan(('alt1', 'alt1'))['selections']), 1)

    def test_duplicate_baseline_utterance_requires_explicit_occurrence(self):
        transcript = copy.deepcopy(self.data['transcripts'][0])
        transcript.update(id='t-repeat', start_seconds=5, end_seconds=7)
        self.data['transcripts'].append(transcript)
        candidate = copy.deepcopy(self.data['candidates'][0])
        candidate.update(id='repeat', transcript_id='t-repeat')
        self.data['candidates'].append(candidate)
        with self.assertRaisesRegex(ReferenceTimelineError, '出现多次'):
            self.plan()
        row = self.plan(baseline_candidate_ids={'line1': 'repeat'})['selections'][0]
        self.assertEqual(row['target_frame_in'], 75)
        explicit = self.plan([dict(candidate_id='alt1', baseline_candidate_id='base1')])
        self.assertEqual(explicit['selections'][0]['target_frame_in'], -25)

    def test_baseline_occurrence_must_belong_to_line_and_session(self):
        for wrong in ('alt1', 'base2', 'missing'):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ReferenceTimelineError, '不对应'):
                self.plan(baseline_candidate_ids={'line1': wrong})

    def test_unique_high_score_candidates_pass_without_manual_confirmation(self):
        self.data['candidates'][2]['status'] = 'candidate'
        self.data['candidates'][0]['status'] = 'candidate'
        row = self.plan()['selections'][0]
        self.assertEqual(row['candidate_id'], 'alt1')
        self.assertEqual(row['baseline_candidate_id'], 'base1')
        self.data['candidates'][2]['status'] = 'rejected'
        with self.assertRaisesRegex(ReferenceTimelineError, '尚未完成'):
            self.plan()

    def review_file(self, session_id):
        from pgm_editor_state import coverage_fingerprint
        from pgm_takes import PgmTakeIndex
        index = PgmTakeIndex(self.data)
        candidate = next(row for row in self.data['candidates'] if index.key(row)[0] == session_id)
        summary = index.file_summary(candidate)
        first = summary.get('first_candidate')
        config = next(row['config'] for row in self.data['sessions'] if row['id']==session_id)
        config.setdefault('library_pgm_editor', {})[index.key(candidate)[1]] = dict(pool='timeline',reviewed=True,
            coverage_fingerprint=coverage_fingerprint(summary,index.source(first) if first else None))

    def test_reviewed_low_score_whole_files_supply_unique_real_dialogue_anchors(self):
        from pgm_takes import PgmTakeIndex
        for row in self.data['candidates']:
            row.update(status='candidate',score=.31)
        for identity in ('base','alt'):
            self.review_file(identity)
        index = PgmTakeIndex(self.data)
        self.assertEqual(index.file_summary(self.data['candidates'][2])['match_status'],'review')
        self.assertTrue(index.association_ready(self.data['candidates'][0]))
        row = self.plan()['selections'][0]
        self.assertEqual((row['candidate_id'],row['baseline_candidate_id'],row['target_frame_in']),('alt1','base1',-25))
        self.assertEqual(row['duration_frames'],250)

    def test_changed_review_fingerprint_does_not_accept_old_low_score_alignment(self):
        for row in self.data['candidates']:
            row.update(status='candidate',score=.31)
        for identity in ('base','alt'):
            self.review_file(identity)
        self.data['transcripts'][2]['start_seconds'] = 2.04
        with self.assertRaisesRegex(ReferenceTimelineError,'第一句'):
            self.plan()

    def test_reviewed_baseline_anchor_can_be_later_utterance_without_changing_file_first(self):
        from pgm_takes import PgmTakeIndex
        for row in self.data['candidates']:
            row.update(status='candidate',score=.31)
        self.review_file('base')
        index = PgmTakeIndex(self.data)
        self.assertEqual(index.file_summary(self.data['candidates'][0])['first_candidate']['id'],'base1')
        self.assertTrue(index.association_ready(self.data['candidates'][1]))

    def test_file_review_cannot_invent_first_anchor_or_revive_removed_pgm(self):
        for row in self.data['candidates']:
            row.update(status='candidate',score=.31)
        self.data['transcripts'].append(dict(id='t-unmatched-opening',session_id='alt',kind='pgm',
            media_path='/alt.mov',start_seconds=.1,end_seconds=.9,text='unmatched opening'))
        self.review_file('alt')
        with self.assertRaisesRegex(ReferenceTimelineError,'第一句'):
            self.plan()
        # Even a high automatic score cannot silently undo an explicit remove.
        self.data['transcripts'].pop()
        self.data['candidates'][2].update(score=.99)
        self.data['sessions'][1]['config']['library_pgm_editor']['/alt.mov']['pool'] = 'removed'
        with self.assertRaisesRegex(ReferenceTimelineError,'轨道移出'):
            self.plan()

    def test_missing_baseline_is_not_inferred_from_clock(self):
        self.data['candidates'][0]['status'] = 'rejected'
        with self.assertRaisesRegex(ReferenceTimelineError, '没有已核对'):
            self.plan()

    def test_source_file_identity_does_not_match_by_basename(self):
        self.data['transcripts'][2]['media_path'] = '/different/alt.mov'
        with self.assertRaisesRegex(ReferenceTimelineError, '不在该场次'):
            self.plan()

    def test_pgm_files_in_same_session_map_through_actual_cut_blocks(self):
        cut = self.cuts['alt']
        cut['pgm_sources'] = [dict(path='/part1.mov', start=0, end=100, frames=100),
                              dict(path='/alt.mov', start=100, end=250, frames=150)]
        self.data['transcripts'][2].update(start_seconds=1.02, end_seconds=3.01)
        row = self.plan()['selections'][0]
        self.assertEqual((row['pgm_frame_in'], row['pgm_frame_out_exclusive']), (0, 150))
        self.assertEqual((row['record_frame_in'], row['record_frame_out_exclusive']), (100, 250))

    def test_invalid_cut_coverage_is_rejected(self):
        for change in ({'start': 1}, {'end': 249}, {'frames': 249}):
            data, self.cuts = dataset()
            self.cuts['alt']['pgm_sources'][0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ReferenceTimelineError, '不完整|时长'):
                self.plan()

    def test_source_outside_media_is_not_silently_trimmed(self):
        self.data['transcripts'][2]['end_seconds'] = 12
        with self.assertRaisesRegex(ReferenceTimelineError, '末端'):
            self.plan()

    def test_rate_mismatch_does_not_retime(self):
        self.cuts['alt']['fps'] = 30
        with self.assertRaisesRegex(ReferenceTimelineError, '帧率必须一致'):
            self.plan()

    def test_source_range_rounds_outward_to_video_frames(self):
        self.data['transcripts'][2].update(start_seconds=2.001, end_seconds=7.201)
        row = self.plan()['selections'][0]
        self.assertEqual((row['pgm_frame_in'], row['pgm_frame_out_exclusive']), (0, 250))

    def test_script_order_does_not_rearrange_repeated_reference_performance(self):
        self.data['transcripts'][0].update(start_seconds=8, end_seconds=10)
        self.data['transcripts'][1].update(start_seconds=1, end_seconds=3)
        self.data['candidates'][-1]['script_line_id'] = 'line2'
        plan = self.plan(('alt1', 'second1'))
        self.assertEqual([row['script_line_id'] for row in plan['selections']], ['line1', 'line2'])
        self.assertEqual([row['target_frame_in'] for row in plan['selections']], [150, 25])
        self.assertIn('保留', plan['warnings'][0])



def write_fixture(folder, session, sound=True, start_tc='01:00:00:00'):
    path = folder/(session+'.fcpxml')
    cut = dict(fps=25, duration=250, pgm_sources=[dict(path='/'+session+'.mov',
               start=0, end=250, frames=250, start_tc='02:00:00:00')])
    sources = [dict(path='/'+session+'_'+camera+'.mov', camera=camera, start=180000, end=180250,
                    frames=250, width=1920, height=1080, reel=session+'_'+camera,
                    audio_channels=[1, 2], source_id=session+'-source-'+camera,
                    session_id=session, production_id='show') for camera in 'ABCD']
    events = [dict(start=0, end=75, camera='A', path=sources[0]['path']),
              dict(start=75, end=150, camera='B', path=sources[1]['path']),
              dict(start=150, end=250, camera='C', path=sources[2]['path'])]
    if sound:
        # This synthetic source contract is explicitly 48 kHz / 24-bit PCM;
        # no real file exists at these deliberate metadata-only fixture paths.
        wavs = [dict(path='/'+session+'_'+str(i)+'.wav', start=i*4800000, samples=480000,
                     channels=2, sample_rate=48000, depth=24,
                     sound_roll='SOUND_'+session, original_audio_filename=session+'_'+str(i)+'.wav',
                     file_uid=session+'-UID-'+str(i), recording_date='2026-10-11') for i in (1, 2)]
        sound_data = (wavs, [dict(path=wavs[0]['path'], record_start=F(0), duration=F(4),
                                  source_start=F(100)+F(3, 48000), src_channel='1, 2'),
                            dict(path=wavs[1]['path'], record_start=F(4), duration=F(6),
                                  source_start=F(200)+F(7, 48000), src_channel='1, 2')])
    else:
        sound_data = None
    config = dict(fps=25, show_id=session, timeline_start_tc=start_tc)
    with patch('cut_xml.timeline_info', return_value=cut):
        write_multicam_xml(config, events, sources, path, session, sound_data)
    return path


class ReferenceXMLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.base = write_fixture(self.folder, 'base')
        self.alt = write_fixture(self.folder, 'alt', start_tc='02:00:00:00')
        self.second = write_fixture(self.folder, 'second')
        self.data, self.cuts = dataset()
        self.output = self.folder/'reference.fcpxml'

    def tearDown(self):
        self.tmp.cleanup()

    def merge(self, selected=('alt1',)):
        plan = build_reference_plan(self.data, 'base', selected, cuts=self.cuts)
        report = assemble_reference_xml(self.base, {'alt': self.alt, 'second': self.second}, plan,
                                        self.output, title='基准与分镜备选')
        return ET.parse(self.output).getroot(), report

    def test_baseline_primary_picture_and_sound_ranges_remain_unchanged(self):
        original = ET.parse(self.base).find('./library/event/project/sequence/spine')
        root, report = self.merge()
        spine = root.find('./library/event/project/sequence/spine')
        self.assertEqual(spine[0].tag, 'gap')
        self.assertEqual(time(spine[0], 'duration'), F(1))
        pictures = [row for row in spine if row.tag == 'clip']
        self.assertEqual(len(pictures), len(original))
        for before, after in zip(original, pictures):
            expected = dict(before.attrib, format='base-'+before.get('format'))
            self.assertEqual(expected, after.attrib)
            before_audio, after_audio = before.findall('audio'), after.findall('audio')
            self.assertEqual([time(row, 'start') for row in before_audio], [time(row, 'start') for row in after_audio])
            self.assertEqual([time(row, 'duration') for row in before_audio], [time(row, 'duration') for row in after_audio])
            self.assertEqual('base-'+before.find('mc-clip/mc-source').get('angleID'),
                             after.find('mc-clip/mc-source').get('angleID'))
        self.assertEqual(report['piece_count'], 3)

    def test_source_three_shot_selection_preserves_switches_and_real_wav_positions(self):
        root, report = self.merge()
        pieces = root.findall('./library/event/project/sequence/spine/*/clip[@lane="1"]')
        self.assertEqual([row.find('mc-clip/mc-source').get('angleID') for row in pieces],
                         ['alt1-angle-A', 'alt1-angle-B', 'alt1-angle-C'])
        self.assertEqual([time(row, 'duration') for row in pieces], [F(3), F(3), F(4)])
        audios = [row for piece in pieces for row in piece.findall('audio')]
        self.assertEqual([time(row, 'start') for row in audios],
                         [F(100)+F(3, 48000), F(103)+F(3, 48000), F(200)+F(7, 48000), F(202)+F(7, 48000)])
        self.assertTrue(all(row.get('srcCh') == '1, 2' and row.get('outCh') == 'L,R' for row in audios))
        self.assertTrue(all(row.get('lane') is None for row in audios))
        self.assertEqual(sum(time(row, 'duration') for row in audios), F(10))
        self.assertEqual([row['target_start'] for row in report['manifest']['pieces']], ['-1/1s', '2/1s', '5/1s'])

    def test_resource_ids_namespaced_source_identity_and_sound_metadata_preserved(self):
        root, report = self.merge(('alt1', 'second1'))
        resources = root.find('resources')
        ids = [row.get('id') for row in resources.iter() if row.get('id')]
        self.assertEqual(len(ids), len(set(ids)))
        for row in root.iter():
            for key in ('ref', 'format'):
                if row.get(key):
                    self.assertIn(row.get(key), ids)
        videos = [row for row in resources.findall('asset') if row.get('hasVideo') == '1']
        self.assertEqual(len(videos), 12)
        identities = [{item.get('key'): item.get('value') for item in row.findall('./metadata/md')} for row in videos]
        self.assertEqual({row['com.pgm.sessionID'] for row in identities}, {'base', 'alt', 'second'})
        sounds = [row for row in resources.findall('asset') if row.get('hasAudio') == '1']
        self.assertEqual(len(sounds), 6)
        for sound in sounds:
            metadata = {row.get('key'): row.get('value') for row in sound.findall('./metadata/md')}
            self.assertIn('SOUND_', metadata['com.apple.proapps.studio.reel'])
            self.assertIn('.wav', metadata['com.pgm.originalAudioFilename'])
        stored = json.loads(Path(report['manifest_path']).read_text())
        self.assertEqual(len(stored['assets']), 18)
        self.assertEqual(stored['baseline_session_id'], 'base')

    def test_alternative_tail_extends_sequence_with_gap_without_extending_base_picture(self):
        root, _ = self.merge(('second1',))
        seq = root.find('./library/event/project/sequence')
        self.assertEqual(time(seq, 'duration'), F(11))
        spine = seq.find('spine')
        self.assertEqual(spine[-1].tag, 'gap')
        self.assertEqual(time(spine[-1], 'duration'), F(1))
        self.assertEqual(sum(time(row, 'duration') for row in spine if row.tag == 'clip'), F(10))
        self.assertEqual(time(spine[2], 'duration'), F(4))

    def test_selected_alternatives_above_primary_on_separate_lanes(self):
        root, _ = self.merge(('alt1', 'second1'))
        seq = root.find('./library/event/project/sequence')
        connected = [row for row in seq.iter() if row.get('lane')]
        self.assertEqual({row.get('lane') for row in connected}, {'1', '2'})
        self.assertFalse(any(row.get('lane') for row in seq.find('spine')))

    def test_without_original_wav_clip_structure_is_preserved(self):
        self.base = write_fixture(self.folder, 'base', sound=False)
        self.alt = write_fixture(self.folder, 'alt', sound=False)
        root, report = self.merge()
        self.assertEqual(root.find('./library/event/project/sequence/spine')[1].tag, 'mc-clip')
        self.assertEqual(report['piece_count'], 3)
        self.assertEqual(len(root.findall('./resources/media/multicam')), 2)

    def test_base_only_export_has_no_alternative_resources_or_edits(self):
        root, report = self.merge(())
        self.assertEqual(report['piece_count'], 0)
        self.assertEqual(len(root.findall('./resources/media')), 1)
        self.assertEqual(len(root.findall('./resources/asset')), 6)

    def test_mismatched_baseline_duration_fails_without_output(self):
        plan = build_reference_plan(self.data, 'base', ['alt1'], cuts=self.cuts)
        plan['baseline_duration_frames'] = 249
        with self.assertRaisesRegex(ReferenceTimelineError, '时长'):
            assemble_reference_xml(self.base, {'alt': self.alt}, plan, self.output)
        self.assertFalse(self.output.exists())

    def test_incomplete_source_range_fails_without_partial_output(self):
        plan = build_reference_plan(self.data, 'base', ['alt1'], cuts=self.cuts)
        plan['selections'][0].update(record_frame_in=200, record_frame_out_exclusive=450)
        with self.assertRaisesRegex(ReferenceTimelineError, '未完整覆盖'):
            assemble_reference_xml(self.base, {'alt': self.alt}, plan, self.output)
        self.assertFalse(self.output.exists())

    def test_retimed_selection_is_rejected(self):
        root = ET.parse(self.alt).getroot()
        ET.SubElement(root.find('./library/event/project/sequence/spine/clip'), 'timeMap')
        plan = build_reference_plan(self.data, 'base', ['alt1'], cuts=self.cuts)
        with self.assertRaisesRegex(ReferenceTimelineError, '变速'):
            assemble_reference_xml(self.base, {'alt': root}, plan, self.output)

    def test_inputs_are_not_mutated(self):
        base, alt = ET.parse(self.base).getroot(), ET.parse(self.alt).getroot()
        before = ET.tostring(base), ET.tostring(alt)
        plan = build_reference_plan(self.data, 'base', ['alt1'], cuts=self.cuts)
        assemble_reference_xml(base, {'alt': alt}, plan, self.output)
        self.assertEqual((ET.tostring(base), ET.tostring(alt)), before)

    def test_cross_session_source_ids_reconform_originals_and_keep_all_wav_components(self):
        before, _ = self.merge()
        maps = []
        for session in ('base', 'alt'):
            rows = [dict(camera=camera, reel=session+'_'+camera,
                         proxy_path='/'+session+'_'+camera+'.mov', proxy_start=180000, frames=250,
                         original_path='/raw/'+session+'_'+camera+'.mov', original_start=180000,
                         source_id=session+'-source-'+camera, session_id=session, production_id='show')
                    for camera in 'ABCD']
            path = self.folder/(session+'_proxy_original_map.json')
            path.write_text(json.dumps(dict(version=1, fps=25, records=rows)))
            maps.append(path)
        original = self.folder/'original.fcpxml'
        info = dict(fps='25', start_tc='02:00:00:00', frames=250, width=3840, height=2160, audio_channels=[])
        with patch('core.probe', return_value=info):
            report = conform_edited_xml(self.output, maps, original, path_mappings=[])
        after = ET.parse(original).getroot()
        self.assertEqual(report['replaced_file_definitions'], 8)
        assets = {row.get('id'): row for row in after.findall('./resources/asset')}
        for video in after.iter('video'):
            asset = assets[video.get('ref')]
            self.assertTrue(file_path(asset.get('src')).startswith('/raw/'))
            metadata = {row.get('key'): row.get('value') for row in asset.findall('./metadata/md')}
            self.assertIn(metadata['com.pgm.sessionID'], ('base', 'alt'))
            self.assertTrue(metadata['com.pgm.sourceID'].startswith(metadata['com.pgm.sessionID']+'-source-'))
        self.assertEqual([ET.tostring(row) for row in before.iter('audio')],
                         [ET.tostring(row) for row in after.iter('audio')])
        self.assertEqual(ET.tostring(before.find('library')), ET.tostring(after.find('library')))

    def test_overlapping_tampered_lane_plan_rejected_without_output(self):
        plan = build_reference_plan(self.data, 'base', ['alt1', 'second1'], cuts=self.cuts)
        for row in plan['selections']:
            row['lane'] = 1
        with self.assertRaisesRegex(ReferenceTimelineError, '轨道上的片段重叠'):
            assemble_reference_xml(self.base, {'alt': self.alt, 'second': self.second}, plan, self.output)
        self.assertFalse(self.output.exists())

    def test_alternatives_only_has_same_clock_positions_sound_and_no_baseline_media(self):
        combined, combined_report = self.merge(('alt1', 'second1', 'alt2'))
        plan = build_reference_plan(self.data, 'base', ['alt1', 'second1', 'alt2'], cuts=self.cuts)
        report = assemble_reference_xml(self.base, {'alt': self.alt, 'second': self.second}, plan,
            self.output, include_baseline=False)
        root = ET.parse(self.output).getroot()
        seq = root.find('./library/event/project/sequence')
        old = combined.find('./library/event/project/sequence')
        self.assertEqual(seq.get('tcStart'), old.get('tcStart'))
        self.assertEqual(seq.get('duration'), old.get('duration'))
        self.assertEqual(report['manifest']['pieces'], combined_report['manifest']['pieces'])
        self.assertEqual([row.tag for row in seq.find('spine')], ['gap'])
        self.assertEqual(time(seq.find('spine/gap'), 'duration'), F(12))
        pieces = seq.findall('spine/gap/clip')
        self.assertEqual({row.get('lane') for row in pieces}, {'1', '2'})
        self.assertEqual([time(row, 'offset') for row in pieces[:3]], [F(0), F(3), F(6)])
        sounds = sorted(tuple(sorted(row.attrib.items())) for piece in pieces for row in piece.findall('audio'))
        combined_sounds = sorted(tuple(sorted(row.attrib.items())) for piece in old.iter('clip') if piece.get('lane')
                                 for row in piece.findall('audio'))
        self.assertEqual(sounds, combined_sounds)
        self.assertEqual(len(root.findall('./resources/media/multicam')), 2)
        assets = root.findall('./resources/asset')
        self.assertTrue(assets)
        self.assertFalse(any(row.get('id').startswith('base-') for row in assets))
        ids = {row.get('id') for row in root.find('resources').iter() if row.get('id')}
        for row in root.iter():
            for key in ('ref', 'format'):
                if row.get(key): self.assertIn(row.get(key), ids)
        self.assertFalse(report['manifest']['include_baseline'])

    def test_both_modes_preserve_source_clocks_and_metadata_despite_cross_scene_clock_difference(self):
        plan = build_reference_plan(self.data, 'base', ['alt1'], cuts=self.cuts)
        original = ET.parse(self.alt).getroot()
        original_resources = original.find('resources')
        def normalized(element):
            result = copy.deepcopy(element)
            for node in result.iter():
                node.text = (node.text or '').strip() or None
                node.tail = None
                for key in ('id', 'ref', 'format', 'angleID'):
                    if node.get(key, '').startswith('alt1-'):
                        node.set(key, node.get(key)[5:])
            return ET.tostring(result)
        for mode in (True, False):
            assemble_reference_xml(self.base, {'alt': self.alt}, plan, self.output, include_baseline=mode)
            root = ET.parse(self.output).getroot()
            # All angle/video/WAV resources, their native clocks, dates, rolls,
            # paths and stable IDs remain byte-equivalent after namespace removal.
            imported = [row for row in root.find('resources') if row.get('id', '').startswith('alt1-')]
            self.assertEqual([normalized(row) for row in imported],
                             [normalized(row) for row in original_resources])
            self.assertEqual(root.find('./library/event/project/sequence').get('tcStart'), '3599/1s')
            self.assertEqual(original.find('./library/event/project/sequence').get('tcStart'), '7200/1s')
            audio_assets = {row.get('id'): row for row in original_resources.findall('asset')}
            for row in root.findall('.//library//audio'):
                if row.get('ref', '').startswith('alt1-'):
                    asset = audio_assets[row.get('ref')[5:]]
                    self.assertGreaterEqual(time(row, 'start'), time(asset, 'start'))
                    self.assertLessEqual(time(row, 'start')+time(row, 'duration'),
                                         time(asset, 'start')+time(asset, 'duration'))


if __name__ == '__main__':
    unittest.main()
