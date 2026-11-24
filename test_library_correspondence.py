import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from library_correspondence import CorrespondenceError, export_correspondence, filter_active_transcripts


def sample_data():
    pgm = '/nas/Take3/PGM_03.mov'
    sources = [dict(camera='A', path='/nas/Take3/A/A001.mov', start_tc='19:00:00:00', fps='25', frames=50),
               dict(camera='A', path='/nas/Take3/A/A002.mov', start_tc='19:00:02:00', fps='25', frames=250),
               dict(camera='B', path='/nas/Take3/B/B001.mov', start_tc='18:59:59:24', fps='25', frames=301),
               dict(camera='C', path='/nas/Take3/C/C001.mov', start_tc='', fps='25', frames=300),
               dict(camera='D', path='/nas/Take3/D/D001.mov', start_tc='19:00:00:00', fps='25', frames=25),
               dict(camera='D', path='/nas/Take3/D/D002.mov', start_tc='19:00:03:00', fps='25', frames=225)]
    placements = [dict(wav_path='/nas/Take3/Audio/001.wav', pgm_start_seconds=0, pgm_end_seconds=2,
                       wav_start_seconds=10, wav_end_seconds=12, offset_seconds=-10, method='waveform', score=.98,
                       wav_info=dict(sample_rate=48000, samples=576000, start_samples=10 * 3600 * 48000,
                                     has_timecode=True, start_tc='10:00:00:00', sound_roll='SR003', recording_date='2026-10-11',
                                     filename='001.wav', custom_metadata={'do_not_drop': 'original'})),
                  dict(wav_path='/nas/Take3/Audio/002.wav', pgm_start_seconds=2, pgm_end_seconds=12,
                       wav_start_seconds=0, wav_end_seconds=10, offset_seconds=2, method='waveform', score=.99,
                       wav_info=dict(sample_rate=48000, samples=480000, start_samples=0, has_timecode=False, filename='002.wav'))]
    config = dict(fps=25, pgm=pgm, calibration_confirmed=True, camera_offsets={'B': 3}, sources=sources,
                  library_media=[dict(kind='pgm', path=pgm, meta=dict(fps='25', frames=300, start_tc='19:00:00:00'))],
                  library_sync={pgm: dict(status='matched', pgm_info=dict(path=pgm, fps=25, frames=300, start_tc='19:00:00:00'), placements=placements)})
    return dict(sessions=[dict(id='session3', name='第三次拍摄', config=config)],
                scripts=[dict(id='line', order=0, scene='第二幕第三场', speaker='Alice', text='明天见。See you tomorrow.', active=1)],
                transcripts=[dict(id='transcript', session_id='session3', media_path=pgm, start_seconds=1.25, end_seconds=3.5,
                                  text='明天再见。See you tomorrow.', metadata={'subtitle_path': '/nas/Take3/PGM_03.srt'})],
                candidates=[dict(id='candidate', script_line_id='line', transcript_id='transcript', status='confirmed', score=.92,
                                 reason='人工核对对白', note='导演选定', tags=['主选'], script_active=1)])


class CorrespondenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def export(self, data=None, filename='lookup.csv', **kwargs):
        result = export_correspondence(data or sample_data(), self.home / filename, **kwargs)
        document = json.loads(Path(result['json_path']).read_text(encoding='utf-8'))
        return result, document

    def test_video_frames_timecode_and_cross_proxy_files(self):
        result, doc = self.export()
        self.assertEqual(result['count'], 1)
        row = doc['records'][0]
        self.assertEqual((row['pgm']['frame_in'], row['pgm']['frame_out_exclusive']), (31, 88))
        self.assertEqual((row['pgm']['source_tc_in'], row['pgm']['source_tc_out_exclusive']), ('19:00:01:06', '19:00:03:13'))
        self.assertEqual(row['pgm']['start_seconds'], 1.25)
        a = row['proxies']['A']['segments']
        self.assertEqual([(s['filename'], s['frame_in'], s['frame_out_exclusive']) for s in a], [('A001.mov', 31, 50), ('A002.mov', 0, 38)])
        self.assertEqual(row['proxies']['A']['uncovered'], [])
        b = row['proxies']['B']['segments'][0]
        self.assertEqual((b['frame_in'], b['frame_out_exclusive']), (35, 92))
        self.assertEqual(row['proxies']['B']['offset_frames'], 3)
        self.assertEqual(row['proxies']['D']['uncovered'], [dict(pgm_frame_in=31, pgm_frame_out_exclusive=75)])

    def test_audio_real_placements_cross_wav_and_missing_timecode(self):
        _, doc = self.export()
        audio = doc['records'][0]['audio']
        self.assertEqual(len(audio['segments']), 2)
        first, second = audio['segments']
        self.assertEqual((first['sample_in'], first['sample_out_exclusive']), (540000, 576000))
        self.assertEqual((first['start_seconds'], first['end_seconds']), (11.25, 12.))
        self.assertEqual(first['source_tc_in'], '10:00:11:06')
        self.assertEqual(first['source_tc_out_exclusive'], '10:00:12:00')
        self.assertEqual(first['sound_roll'], 'SR003')
        self.assertEqual((second['sample_in'], second['sample_out_exclusive']), (0, 72000))
        self.assertEqual(second['source_tc_in'], '')
        self.assertEqual(second['start_tc'], '')
        self.assertEqual(audio['uncovered'], [])
        self.assertEqual(doc['catalogue_snapshot']['sessions'][0]['config']['library_sync']['/nas/Take3/PGM_03.mov']['placements'][0]['wav_info']['custom_metadata'], {'do_not_drop': 'original'})

    def test_bom_chinese_csv_and_json_bilingual_original(self):
        result, doc = self.export()
        self.assertTrue(Path(result['csv_path']).read_bytes().startswith(b'\xef\xbb\xbf'))
        with Path(result['csv_path']).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]['台本台词'], '明天见。See you tomorrow.')
        self.assertIn('/nas/Take3/A/A001.mov', rows[0]['A机代理'])
        self.assertIn('/nas/Take3/A/A002.mov', rows[0]['A机代理'])
        self.assertIn('不代表不同拍摄场次逐帧同步', rows[0]['位置说明'])
        self.assertEqual(rows[0]['PGM帧OUT(不含)'], '88')
        self.assertEqual(doc['records'][0]['script']['id'], 'line')

    def test_default_confirmed_active_only_and_optional_unverified_candidates(self):
        data = sample_data()
        data['scripts'].append(dict(data['scripts'][0], id='inactive', active=0))
        data['candidates'].extend([dict(data['candidates'][0], id='unverified', status='candidate'),
                                   dict(data['candidates'][0], id='rejected', status='rejected'),
                                   dict(data['candidates'][0], id='inactive', script_line_id='inactive')])
        basic, basic_doc = self.export(data)
        self.assertEqual(basic['count'], 1)
        self.assertEqual([row['status'] for row in basic_doc['catalogue_snapshot']['candidates']], ['confirmed'])
        self.assertEqual([row['id'] for row in basic_doc['catalogue_snapshot']['scripts']], ['line'])
        result, doc = self.export(data, 'optional.csv', include_candidates=True)
        self.assertEqual(result['count'], 2)
        self.assertEqual({row['match']['status'] for row in doc['records']}, {'candidate', 'confirmed'})
        self.assertNotIn('rejected', [row['status'] for row in doc['catalogue_snapshot']['candidates']])
        with Path(result['csv_path']).open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual({row['匹配状态'] for row in csv.DictReader(stream)}, {'未核实候选', '已确认'})

    def test_no_confirmed_is_explicit_and_writes_nothing(self):
        data = sample_data()
        data['candidates'][0]['status'] = 'candidate'
        with self.assertRaisesRegex(CorrespondenceError, '暂无已确认'):
            self.export(data)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_missing_pgm_clock_does_not_turn_local_seconds_into_timecode(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_sync'][config['pgm']]['pgm_info']['start_tc'] = ''
        config['library_media'][0]['meta']['start_tc'] = ''
        _, doc = self.export(data)
        row = doc['records'][0]
        self.assertEqual(row['pgm']['source_tc_in'], '')
        self.assertEqual(row['pgm']['frame_in'], 31)
        self.assertEqual(row['proxies']['A']['segments'], [])
        self.assertTrue(any('首帧源时码' in warning for warning in row['warnings']))
        self.assertEqual(len(row['audio']['segments']), 2)

    def test_missing_fps_preserves_seconds_without_guessing_session_25(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_sync'][config['pgm']]['pgm_info'].pop('fps')
        config['library_media'][0]['meta'].pop('fps')
        _, doc = self.export(data)
        pgm = doc['records'][0]['pgm']
        self.assertEqual(pgm['fps'], '')
        self.assertIsNone(pgm['frame_in'])
        self.assertEqual(pgm['start_seconds'], 1.25)

    def test_missing_proxy_clock_and_unverified_calibration_stay_explicit(self):
        data = sample_data()
        data['sessions'][0]['config']['calibration_confirmed'] = False
        _, doc = self.export(data)
        proxies = doc['records'][0]['proxies']
        self.assertEqual(proxies['C']['segments'], [])
        self.assertEqual(proxies['A']['status'], 'mapped_unverified')
        self.assertFalse(proxies['A']['segments'][0]['calibration_confirmed'])

    def test_overlapping_proxy_and_audio_are_listed_without_choosing(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['sources'].append(dict(config['sources'][0], path='/nas/Take3/A/duplicate.mov'))
        placements = config['library_sync'][config['pgm']]['placements']
        placements.append(dict(placements[0], wav_path='/nas/Take3/Audio/duplicate.wav'))
        _, doc = self.export(data)
        row = doc['records'][0]
        self.assertEqual(row['proxies']['A']['status'], 'ambiguous')
        self.assertEqual(sum(segment['ambiguous'] for segment in row['proxies']['A']['segments']), 2)
        self.assertEqual(sum(segment['ambiguous'] for segment in row['audio']['segments']), 2)

    def test_audio_gap_or_inconsistent_offset_is_never_guessed(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        sync = config['library_sync'][config['pgm']]
        sync['placements'][0]['offset_seconds'] = 100
        _, doc = self.export(data)
        audio = doc['records'][0]['audio']
        self.assertEqual(len(audio['segments']), 1)
        self.assertEqual(audio['uncovered'], [dict(start_seconds=1.25, end_seconds=2.)])

    def test_source_timecode_across_midnight_is_blank_not_wrapped(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_sync'][config['pgm']]['pgm_info']['start_tc'] = '23:59:58:00'
        _, doc = self.export(data)
        pgm = doc['records'][0]['pgm']
        self.assertEqual(pgm['source_tc_in'], '23:59:59:06')
        self.assertEqual(pgm['source_tc_out_exclusive'], '')
        self.assertTrue(any('跨午夜' in warning for warning in doc['records'][0]['warnings']))

    def test_sort_script_then_shoot_then_real_pgm_clock(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        later = '/nas/Take3/AAA_later.mov'
        config['library_sync'][later] = dict(status='matched', pgm_info=dict(fps=25, frames=300, start_tc='20:00:00:00'), placements=[])
        data['transcripts'].append(dict(data['transcripts'][0], id='later', media_path=later, start_seconds=0, end_seconds=1))
        data['candidates'].insert(0, dict(data['candidates'][0], id='later', transcript_id='later'))
        second = copy.deepcopy(data['sessions'][0]); second['id'] = 'session1'; second['name'] = '第一场'
        data['sessions'].insert(0, second)
        data['transcripts'].append(dict(data['transcripts'][0], id='first', session_id='session1'))
        data['candidates'].append(dict(data['candidates'][0], id='first', transcript_id='first'))
        _, doc = self.export(data)
        self.assertEqual([row['transcript']['id'] for row in doc['records']], ['first', 'transcript', 'later'])

    def test_existing_csv_or_json_and_media_paths_are_protected(self):
        existing = self.home / 'lookup.json'; existing.write_text('keep')
        with self.assertRaisesRegex(CorrespondenceError, '已存在'):
            self.export()
        self.assertEqual(existing.read_text(), 'keep')
        self.assertFalse((self.home / 'lookup.csv').exists())
        with self.assertRaisesRegex(CorrespondenceError, '媒体'):
            self.export(filename='PGM.mov')

    def test_cancel_midwrite_and_json_failure_clean_both_new_files(self):
        flag = [False]
        with self.assertRaisesRegex(CorrespondenceError, '取消'):
            self.export(progress=lambda n, m: flag.__setitem__(0, n >= 90), cancelled=lambda: flag[0])
        self.assertEqual(list(self.home.iterdir()), [])
        data = sample_data()
        data['sessions'][0]['unexpected'] = object()
        with self.assertRaisesRegex(CorrespondenceError, '清理'):
            self.export(data)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_invalid_source_range_does_not_write_and_input_remains_unchanged(self):
        data = sample_data()
        before = copy.deepcopy(data)
        self.export(data)
        self.assertEqual(data, before)
        data['transcripts'][0]['end_seconds'] = 0
        with self.assertRaisesRegex(CorrespondenceError, '起止位置'):
            self.export(data, 'invalid.csv')
        self.assertFalse((self.home / 'invalid.csv').exists())

    def test_registered_metadata_info_alias_and_manual_pgm_timecode(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_sync'] = {}
        config['library_media'][0]['metadata'] = config['library_media'][0].pop('meta')
        config['pgm_timecodes'] = {config['pgm']: '18:00:00:00'}
        _, doc = self.export(data)
        self.assertEqual(doc['records'][0]['pgm']['source_tc_in'], '18:00:01:06')
        self.assertEqual(doc['records'][0]['audio']['segments'], [])

    def test_unified_registry_probe_supplies_actual_pgm_and_proxy_metadata(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_sync'] = {}
        registry = config['library_media']
        registry[0]['probe'] = registry[0].pop('meta')
        source = config['sources'][0]
        source.pop('fps')
        registry.append(dict(kind='proxy', path=source['path'], probe=dict(
            fps='25/1', frames=50, start_tc=source['start_tc'])))
        _, doc = self.export(data)
        row = doc['records'][0]
        self.assertEqual((row['pgm']['fps'], row['pgm']['frame_in']), ('25', 31))
        self.assertEqual(row['proxies']['A']['segments'][0]['fps_origin'], 'registered')
        self.assertEqual(row['proxies']['A']['segments'][0]['frame_in'], 31)

    def test_proxy_actual_registry_fps_does_not_fall_back_to_session_fps(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        source = config['sources'][0]
        config['library_media'].append(dict(kind='proxy', path=source['path'], probe=dict(
            fps='24', frames=48, start_tc=source['start_tc'])))
        _, doc = self.export(data)
        a = doc['records'][0]['proxies']['A']
        self.assertEqual([row['filename'] for row in a['segments']], ['A002.mov'])
        self.assertEqual(a['unavailable_sources'][0]['path'], source['path'])

    def test_registry_only_proxies_are_included_in_same_clock_report(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_media'].extend(dict(kind='proxy', id='src-' + str(i),
            camera=source['camera'], path=source['path'],
            probe={key:source[key] for key in ('fps','frames','start_tc')})
            for i, source in enumerate(config.pop('sources')))
        _, doc = self.export(data)
        row = doc['records'][0]
        self.assertEqual([segment['filename'] for segment in row['proxies']['A']['segments']],
                         ['A001.mov', 'A002.mov'])
        self.assertEqual(row['proxies']['B']['segments'][0]['frame_in'], 35)
        self.assertEqual(row['proxies']['A']['segments'][0]['source_id'], 'src-0')

    def test_registry_does_not_duplicate_or_override_calibrated_proxy_clock(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        source = config['sources'][0]
        config['library_media'].append(dict(kind='proxy', camera='A', path=source['path'],
            probe=dict(fps='25',frames=50,start_tc='18:00:00:00')))
        _, doc = self.export(data)
        segments = doc['records'][0]['proxies']['A']['segments']
        self.assertEqual([segment['filename'] for segment in segments], ['A001.mov', 'A002.mov'])
        self.assertEqual(segments[0]['frame_in'], 31)

    def test_superseded_asr_confirmed_is_retained_in_input_but_not_current_report(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        data['transcripts'].append(dict(data['transcripts'][0], id='current', start_seconds=7, end_seconds=8,
                                        text='明天见。See you tomorrow.'))
        data['candidates'].append(dict(data['candidates'][0], id='current_match', transcript_id='current'))
        config['library_asr'] = {config['pgm']: dict(complete=True, active_transcript_ids=['current'])}
        before = copy.deepcopy(data)
        filtered = filter_active_transcripts(data)
        self.assertEqual([row['id'] for row in filtered['transcripts']], ['current'])
        self.assertEqual([row['id'] for row in filtered['candidates']], ['current_match'])
        _, doc = self.export(data)
        self.assertEqual([row['transcript']['id'] for row in doc['records']], ['current'])
        self.assertEqual(doc['records'][0]['pgm']['start_seconds'], 7)
        self.assertEqual(data, before)
        self.assertEqual(data['candidates'][0]['status'], 'confirmed')

    def test_manual_visual_annotations_survive_empty_and_changed_asr_runs(self):
        for active_ids in ([], ['new-asr']):
            data = sample_data()
            config = data['sessions'][0]['config']
            annotation = dict(source_kind='manual_visual', classification_only=True)
            data['transcripts'].append(dict(data['transcripts'][0], id='manual',
                text='[人工画面归类]', data=annotation))
            data['candidates'].append(dict(data['candidates'][0], id='manual-match',
                transcript_id='manual', session_id='session3', media_path=config['pgm'],
                data=annotation))
            config['library_asr'] = {config['pgm']: dict(complete=True,
                active_transcript_ids=active_ids, sync_signature='old')}
            config['library_sync'][config['pgm']]['signature'] = 'new'
            filtered = filter_active_transcripts(data)
            self.assertEqual([row['id'] for row in filtered['transcripts']], ['manual'])
            self.assertEqual([row['id'] for row in filtered['candidates']], ['manual-match'])
            # Candidate joins carry per-segment annotations even in a snapshot
            # which does not include the transcript collection.
            joined = filter_active_transcripts(dict(data, transcripts=[], candidates=[
                dict(row,session_id='session3',media_path=config['pgm'])
                for row in data['candidates']]))
            self.assertEqual([row['id'] for row in joined['candidates']], ['manual-match'])

    def test_active_run_scope_full_path_session_and_legacy_and_empty_run(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_asr'] = {config['pgm']: dict(complete=True, active_transcript_ids=[])}
        second = copy.deepcopy(data['sessions'][0])
        second['id'] = 'other_session'; second['config']['library_asr'] = {}
        data['sessions'].append(second)
        data['transcripts'].extend([
            dict(data['transcripts'][0], id='other_session', session_id='other_session'),
            dict(data['transcripts'][0], id='other_path', media_path='/nas/another/PGM_03.mov')])
        data['candidates'].extend([
            dict(data['candidates'][0], id='other_session', transcript_id='other_session'),
            dict(data['candidates'][0], id='other_path', transcript_id='other_path')])
        filtered = filter_active_transcripts(data)
        self.assertEqual([row['id'] for row in filtered['transcripts']], ['other_session', 'other_path'])
        self.assertEqual([row['id'] for row in filtered['candidates']], ['other_session', 'other_path'])
        # A joined candidate-only snapshot is still scoped safely.
        joined = copy.deepcopy(data)
        joined['transcripts'] = []
        joined['candidates'] = [dict(joined['candidates'][0], session_id='session3', media_path=config['pgm'])]
        self.assertEqual(filter_active_transcripts(joined)['candidates'], [])
        with self.assertRaisesRegex(CorrespondenceError, '暂无已确认'):
            self.export(dict(data, transcripts=data['transcripts'][:1], candidates=data['candidates'][:1]))

    def test_new_sync_then_failed_asr_does_not_present_old_confirmed_as_current(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_sync'][config['pgm']]['signature'] = 'new-waveform-map'
        config['library_asr'] = {config['pgm']: dict(complete=True,
            sync_signature='old-waveform-map', active_transcript_ids=['transcript'])}
        filtered = filter_active_transcripts(data)
        self.assertEqual(filtered['transcripts'], [])
        self.assertEqual(filtered['candidates'], [])
        self.assertEqual(data['candidates'][0]['status'], 'confirmed')
        # Successful completion with matching sync makes current data visible.
        config['library_asr'][config['pgm']]['sync_signature'] = 'new-waveform-map'
        self.assertEqual(len(filter_active_transcripts(data)['candidates']), 1)
        # Hand-corrected subtitles are explicitly in PGM local positions and
        # do not become stale solely because waveform alignment changed.
        config['library_asr'][config['pgm']].update(sync_signature='old-waveform-map', source_kind='manual')
        self.assertEqual(len(filter_active_transcripts(data)['candidates']), 1)

    def test_failed_resync_hides_old_automatic_dialogue_but_preserves_manual_visual(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['library_processing'] = {config['pgm']: dict(status='failed', stage='sync')}
        manual = dict(data['transcripts'][0], id='manual', start_seconds=6., end_seconds=8.,
                      data=dict(source_kind='manual_visual'))
        data['transcripts'].append(manual)
        data['candidates'].append(dict(data['candidates'][0], id='manual-match', transcript_id='manual'))
        # Also covers a legacy successful run with no explicit active-ID list.
        filtered = filter_active_transcripts(data)
        self.assertEqual([row['id'] for row in filtered['transcripts']], ['manual'])
        self.assertEqual([row['id'] for row in filtered['candidates']], ['manual-match'])
        config['library_processing'][config['pgm']]['stage'] = 'asr'
        self.assertEqual(len(filter_active_transcripts(data)['transcripts']), 2)
        config['library_processing'][config['pgm']]['stage'] = 'sync'
        config['library_asr'] = {config['pgm']: dict(source_kind='manual', active_transcript_ids=['transcript'])}
        self.assertEqual(len(filter_active_transcripts(data)['transcripts']), 2)

    def test_later_asr_does_not_duplicate_confirmed_visual_interval_and_undo_restores_it(self):
        data = sample_data()
        visual = dict(data['transcripts'][0], id='visual', start_seconds=1., end_seconds=4.,
                      text='[人工画面归类]', data=dict(source_kind='manual_visual'))
        data['transcripts'].extend([visual, dict(data['transcripts'][0], id='partial',
                                               start_seconds=3.5, end_seconds=5.)])
        decision = dict(data['candidates'][0], id='visual-match', transcript_id='visual')
        data['candidates'].append(decision)
        filtered = filter_active_transcripts(data)
        self.assertEqual([row['id'] for row in filtered['transcripts']], ['visual', 'partial'])
        self.assertEqual([row['id'] for row in filtered['candidates']], ['visual-match'])
        self.assertEqual(len(data['transcripts']), 3)
        decision['status'] = 'candidate'
        self.assertEqual(len(filter_active_transcripts(data)['transcripts']), 3)
        decision['status'] = 'confirmed'; decision['script_active'] = False
        self.assertEqual(len(filter_active_transcripts(data)['transcripts']), 3)
        decision['script_active'] = True
        visual['session_id'] = 'different-performance'
        self.assertEqual(len(filter_active_transcripts(data)['transcripts']), 3)

    def test_manual_visual_export_never_presents_failed_resync_as_original_audio(self):
        data = sample_data()
        config = data['sessions'][0]['config']
        config['sound_unmatched_policy']='error'
        config['library_processing'] = {config['pgm']: dict(status='failed', stage='sync', error='未找到当前录音')}
        data['transcripts'][0]['data'] = dict(source_kind='manual_visual')
        data['candidates'][0].update(session_id='session3', media_path=config['pgm'])
        _, doc = self.export(data)
        audio = doc['records'][0]['audio']
        self.assertEqual(audio['sync_status'], 'failed')
        self.assertEqual(audio['segments'], [])
        self.assertEqual(audio['uncovered'], [dict(start_seconds=1.25, end_seconds=3.5)])
        self.assertEqual(config['library_sync'][config['pgm']]['status'], 'matched')
        from library_workflow import enrich_data
        candidate = enrich_data(data)['candidates'][0]
        self.assertEqual(candidate['sync_status'], 'failed')
        self.assertEqual(candidate['sync_error'], '未找到当前录音')
        self.assertEqual(candidate['synced_audio_paths'], [])

    def test_confirmed_story_position_removes_other_suggestions_from_current_review_only(self):
        data = sample_data()
        data['candidates'].append(dict(data['candidates'][0], id='other-place',
            script_line_id='other-line', status='candidate', score=.6))
        self.assertEqual([row['id'] for row in filter_active_transcripts(data)['candidates']], ['candidate'])
        self.assertEqual(len(data['candidates']), 2)
        data['candidates'][0]['status'] = 'candidate'
        self.assertEqual(len(filter_active_transcripts(data)['candidates']), 2)
        data['candidates'][0]['status'] = 'confirmed'
        data['candidates'][0]['script_active'] = False
        self.assertEqual(len(filter_active_transcripts(data)['candidates']), 2)

    def test_csv_excel_formula_is_text_but_json_preserves_original(self):
        data = sample_data()
        data['scripts'][0]['text'] = '=1+1'
        result, doc = self.export(data)
        with Path(result['csv_path']).open(encoding='utf-8-sig', newline='') as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row['台本台词'], "'=1+1")
        self.assertEqual(doc['records'][0]['script']['text'], '=1+1')


if __name__ == '__main__':
    unittest.main()
