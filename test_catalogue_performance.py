"""Large-catalogue optimizations retain source identity and conservative checks."""
import copy
from fractions import Fraction as F
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from core import frames_tc
from library_angles import AngleMapper, _ProxyIntervalIndex, _intersecting_proxy_config, _proxy_headers
from library_auto_review import enrich_review_state
from library_correspondence import PGMInfoIndex, _pgm_info, _proxies
from library_sync import SyncValidationIndex, _identity, _settings, _signature, valid_sync
from library_timeline import build_timeline_data
from sound_fallback import SoundStateIndex, fallback_eligible, sound_entry
from workflow_readiness import _audio_header_index, _registered_pgm_audio, _sound_state


class CataloguePerformanceTests(unittest.TestCase):
    def test_sound_index_preserves_conflicting_aliases_and_fallback_policy(self):
        key = lambda p: str(p).replace('/local/', '/shared/')
        config = dict(library_sync={'/local/p.mov': {'status': 'matched'}, '/shared/p.mov': {'status': 'failed'}},
                      library_processing={'/local/f.mov': {'stage': 'sync', 'status': 'failed'}},
                      library_sound_fallback={'/local/g.mov': {'stage': 'sync', 'status': 'fallback'}})
        before = copy.deepcopy(config); index = SoundStateIndex(config, key)
        for path in ('/shared/p.mov', '/shared/f.mov', '/shared/g.mov', '/missing.mov'):
            for field in index.FIELDS:
                self.assertEqual(index.entry(field, path), sound_entry(config.get(field), path, key))
            self.assertEqual(index.eligible(path), fallback_eligible(config, path, key))
        self.assertEqual(index.entry('library_sync', '/shared/p.mov'), {})
        self.assertEqual(len(index.aliases('library_sync', '/shared/p.mov')), 2)
        self.assertEqual(config, before)
        changed = copy.deepcopy(config); changed['sound_unmatched_policy'] = 'error'
        self.assertFalse(SoundStateIndex(changed, key).eligible('/shared/f.mov'))

    def test_sound_readiness_normalizes_nested_aliases_before_conflict_decision(self):
        key = lambda p: str(p).replace('/local/', '/shared/')
        a = dict(status='matched', placements=[{'wav_path': '/local/a.wav'}], pgm_info={'path': '/local/p.mov'})
        b = dict(status='matched', placements=[{'wav_path': '/shared/a.wav'}], pgm_info={'path': '/shared/p.mov'})
        config = {'library_sync': {'/local/p.mov': a, '/shared/p.mov': b}}
        self.assertEqual(_sound_state(config, '/shared/p.mov', key),
                         _sound_state(config, '/shared/p.mov', key, SoundStateIndex(config, key)))
        self.assertEqual(a['placements'][0]['wav_path'], '/local/a.wav')

    def test_audio_header_index_retains_conflicts_foreign_headers_and_silent_sources(self):
        key = lambda p: str(p).replace('/local/', '/shared/')
        config = dict(pgm_sources=[{'path': '/local/a.mov', 'audio_channels': 0}],
            library_media=[{'path': '/shared/b.mov', 'kind': 'pgm', 'probe': {'audio_channels': [1, 2]}},
                           {'path': '/shared/c.mov', 'kind': 'pgm', 'has_audio': True}],
            library_sync={'/shared/a.mov': {'pgm_info': {'path': '/foreign.mov', 'has_audio': True}}},
            library_processing={'/local/c.mov': {'stage': 'sync', 'has_audio': False}})
        headers = _audio_header_index(config, key)
        for p in ('/shared/a.mov', '/shared/b.mov', '/shared/c.mov', '/missing.mov'):
            self.assertEqual(_registered_pgm_audio(config, p, key), _registered_pgm_audio(config, p, key, headers))
        self.assertIs(_registered_pgm_audio(config, '/shared/a.mov', key, headers), False)
        self.assertIsNone(_registered_pgm_audio(config, '/shared/c.mov', key, headers))

    def test_pgm_info_index_keeps_header_and_transcript_precedence_and_new_snapshot(self):
        path = '/qa/P.mov'
        config = dict(library_media=[{'kind': 'pgm', 'path': path, 'probe': {'fps': '25', 'frames': 250}}],
            pgm_sources=[{'path': path, 'frames': 251}], pgm_timecodes={path: '01:00:00:00'},
            library_sync={path: {'status': 'matched', 'pgm_info': {'frames': 252}}})
        source = {'metadata': {'source_info': {'frames': 253, 'start_tc': '02:00:00:00'}}}
        index = PGMInfoIndex(config)
        self.assertEqual(_pgm_info(config, path, source), _pgm_info(config, path, source, index))
        self.assertEqual(_pgm_info(config, path, source, index)[0]['start_tc'], '01:00:00:00')
        updated = copy.deepcopy(config); updated['pgm_timecodes'][path] = '03:00:00:00'
        self.assertEqual(_pgm_info(updated, path, source, PGMInfoIndex(updated))[0]['start_tc'], '03:00:00:00')

    def test_interval_index_matches_scan_with_long_overlaps_invalid_rates_and_offsets(self):
        randomizer = random.Random(151)
        sources = []
        for i in range(800):
            first = randomizer.randrange(10000)
            sources.append(dict(path=f'/qa/A{i}.mov', camera='A' if i % 2 else 'B', fps='25',
                frames=randomizer.randrange(1, 1000), start_tc=frames_tc(first, 25)))
        sources.extend([dict(path='/long.mov', camera='A', fps='25', frames=20000, start_tc='00:00:00:00'),
            dict(path='/bad.mov', camera='B', fps='25', frames=10, start_tc='invalid'),
            dict(path='/rate.mov', camera='B', fps='24', frames=100, start_tc='00:00:00:00')])
        config = dict(camera_ids=['A','B'], sources=sources, camera_offsets={'A': 3, 'B': -2})
        headers = _proxy_headers(config); indexed = _ProxyIntervalIndex(headers)
        for first in range(0, 10000, 57):
            pgm = {'frame_in': first, 'frame_out_exclusive': first + 100}
            scanned = _intersecting_proxy_config(config, headers, pgm, F(25), 0)
            fast = _intersecting_proxy_config(config, indexed, pgm, F(25), 0)
            self.assertEqual(scanned['sources'], fast['sources'])
        bad = dict(config, camera_offsets={'A': '1.5', 'B': True})
        pgm = {'frame_in': 100, 'frame_out_exclusive': 200}
        self.assertEqual(_intersecting_proxy_config(bad, headers, pgm, F(25), 0)['sources'],
                         _intersecting_proxy_config(bad, indexed, pgm, F(25), 0)['sources'])

    def test_angle_cache_is_bounded_and_eviction_changes_no_results(self):
        path = '/qa/PGM.mov'; config = dict(camera_ids=['A'], calibration_confirmed=True,
            pgm_sources=[dict(path=path, fps='25', frames=25000, start_tc='01:00:00:00')],
            sources=[dict(path='/qa/A.mov', camera='A', fps='25', frames=25000, start_tc='01:00:00:00')])
        mapper = AngleMapper([dict(id='s', config=config)]); mapper.cache_limit = 8
        first = dict(session_id='s', kind='pgm', media_path=path, start_seconds=1, end_seconds=2)
        expected = mapper.fields(first)
        for i in range(100):
            mapper.fields(dict(first, start_seconds=i, end_seconds=i+1))
        self.assertEqual(len(mapper.cache), 8)
        self.assertEqual(expected, mapper.fields(first))

    def test_sweep_overlap_flags_match_all_pairwise_comparisons(self):
        randomizer = random.Random(17); rows=[]
        for i in range(150):
            first = randomizer.randrange(500)
            rows.append(dict(path=f'/qa/A{i}.mov', camera='A', fps='25', frames=randomizer.randrange(1, 40), start_tc=frames_tc(first,25)))
        config = dict(camera_ids=['A'], sources=rows, calibration_confirmed=True)
        report = _proxies(config, {'frame_in': 0, 'frame_out_exclusive': 600}, F(25), 0, [])['A']['segments']
        for row in report:
            expected = any(other is not row and min(row['pgm_frame_out_exclusive'],other['pgm_frame_out_exclusive']) > max(row['pgm_frame_in'],other['pgm_frame_in']) for other in report)
            self.assertEqual(row['ambiguous'], expected)

    def test_review_snapshot_copies_once_preserving_shared_path_lists(self):
        paths=[f'/qa/{i}.mov' for i in range(1000)]
        data=dict(candidates=[dict(id='c'+str(i),status='rejected',proxy_paths=paths,tags=[]) for i in range(20)])
        result=enrich_review_state(data)
        self.assertEqual(len({id(row['proxy_paths']) for row in result['candidates']}),1)
        self.assertIsNot(result['candidates'][0]['proxy_paths'],paths)
        result['candidates'][0]['proxy_paths'].append('/changed.mov')
        self.assertEqual(len(paths),1000)

    def test_story_nodes_preserve_path_inventory_sharing_without_linking_angle_edits(self):
        paths=[f'/qa/{i}.mov' for i in range(1000)]
        data=dict(sessions=[dict(id='s',name='session',config={})],scripts=[dict(id='l',text='台词')],
            transcripts=[dict(id='t'+str(i),session_id='s',media_path=f'/PGM{i}.mov',start_seconds=0,end_seconds=1) for i in range(20)],
            candidates=[dict(id='c'+str(i),transcript_id='t'+str(i),script_line_id='l',status='confirmed',
                proxy_paths=paths,related_angles=[{'camera':'A','media_path':f'/qa/A{i}.mov'}],related_angle_count=1) for i in range(20)])
        clips=build_timeline_data(data,prepared=True)[0]['clips']
        self.assertEqual(len({id(c['proxy_paths']) for c in clips}),1)
        self.assertIsNot(clips[0]['proxy_paths'],paths)
        clips[0]['related_angles'][0]['camera']='Z'
        self.assertEqual(clips[1]['related_angles'][0]['camera'],'A')
        self.assertEqual(data['candidates'][0]['related_angles'][0]['camera'],'A')

    def test_sync_index_signature_equivalence_stat_reuse_and_replacement(self):
        with tempfile.TemporaryDirectory() as folder, patch('library_bridge.read_settings',return_value={}):
            base=Path(folder); wav=base/'MIX.wav';wav.write_bytes(b'original sound')
            pgms=[base/f'{i}.mov' for i in range(12)]
            for p in pgms:p.write_bytes(b'video'+p.name.encode())
            config=dict(sound_sources=[{'path':str(wav)}],pgm_timecodes={str(p):'01:00:00:00' for p in pgms},
                sound_reference_overrides={str(pgms[0]):{'path':str(wav),'start_seconds':.5}})
            states=[]
            for p in pgms:
                deps=[_identity(p),_identity(wav)]
                states.append(dict(version=1,status='matched',placements=[{'wav_path':str(wav.resolve())}],signature=_signature(deps,_settings(config,str(p)))))
            with patch('library_sync._identity',wraps=_identity) as identities:
                index=SyncValidationIndex(config)
                for p,state in zip(pgms,states):
                    self.assertEqual(index.settings(str(p)),_settings(config,str(p)))
                    self.assertTrue(valid_sync(config,str(p),state,index))
                # New PGM-only signatures never stat the retired reference.
                self.assertEqual(identities.call_count,len(pgms)+1)
            wav.write_bytes(b'replaced sound with new size')
            fresh=SyncValidationIndex(config)
            self.assertFalse(valid_sync(config,str(pgms[0]),states[0],fresh))
            self.assertFalse(valid_sync(config,str(pgms[0]),states[0]))

    def test_partial_sync_candidate_change_invalidates_but_full_coverage_does_not(self):
        with tempfile.TemporaryDirectory() as folder, patch('library_bridge.read_settings',return_value={}):
            base=Path(folder);p=base/'p.mov';w=base/'w.wav';extra=base/'new.wav'
            for item in (p,w,extra):item.write_bytes(item.name.encode())
            config={'sound_sources':[{'path':str(w)}]}
            state=dict(version=1,status='matched',placements=[{'wav_path':str(w.resolve())}],
                signature=_signature([_identity(p),_identity(w)],_settings(config,str(p))),candidate_dependencies=[_identity(w)])
            config['sound_sources'].append({'path':str(extra)})
            self.assertTrue(valid_sync(config,str(p),state,SyncValidationIndex(config)))
            state['uncovered']=[{'start_seconds':0,'end_seconds':1}]
            self.assertFalse(valid_sync(config,str(p),state,SyncValidationIndex(config)))

if __name__=='__main__':unittest.main()
