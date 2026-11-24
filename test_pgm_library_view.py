"""Whole-file pending library grouping without GUI/media/database access."""
import copy
import unittest

from pgm_library_view import prepare_pending_files, prepare_pgm_inventory, inventory_status_rows
from pgm_takes import PgmTakeIndex


def session(identity='session-1', path='/PGM/one.mov', *, duration=True):
    info = dict(kind='pgm', path=path, start_tc='01:00:00:00')
    if duration:
        info.update(fps='25', frames=2500)
    return dict(id=identity, name=identity, config=dict(pgm=path, library_media=[info]))


def speech(identity='speech-1', sid='session-1', path='/PGM/one.mov', start=7.,
           text='这是一句真正的现场台词可以用于核查'):
    return dict(id=identity, session_id=sid, media_path=path, kind='pgm',
                start_seconds=start, end_seconds=start + 2., text=text)


def proposal(identity='candidate-1', source='speech-1', line='line-1', state='candidate', score=.4):
    return dict(id=identity, transcript_id=source, script_line_id=line,
                status=state, score=score)


def pending(identity='gap-1', sid='session-1', path='/PGM/one.mov', start=20., end=30.):
    return dict(id=identity, session_id=sid, media_path=path, kind='pgm',
                start_seconds=start, end_seconds=end, manual_pending=True,
                reason='尚无可用台词识别结果', reason_code='asr_unavailable')


def prepare(*, sessions=None, transcripts=None, candidates=None, intervals=None, reference=None, inventory=False):
    data = dict(sessions=sessions if sessions is not None else [session()],
                scripts=[dict(id='line-1', script_id='script', order=1, text='台本一'),
                         dict(id='line-2', script_id='script', order=2, text='台本二')],
                transcripts=transcripts if transcripts is not None else [speech()],
                candidates=candidates if candidates is not None else [proposal()])
    if reference:
        data['reference_state'] = dict(reference_session_id=reference)
    index = PgmTakeIndex(data, prepared=True)
    rows, flags = (prepare_pgm_inventory if inventory else prepare_pending_files)(data['sessions'], data['transcripts'], data['candidates'],
                                        intervals or [], index)
    return rows, flags, data


class PendingFileViewTests(unittest.TestCase):
    def test_same_name_in_different_sessions_and_paths_is_never_merged(self):
        sources = [speech('a', 'day1', '/day1/PIX_001.mov'),
                   speech('b', 'day2', '/day2/PIX_001.mov'),
                   speech('c', 'day2', '/day2/pickup/PIX_001.mov')]
        sessions = [session('day1', '/day1/PIX_001.mov'), session('day2', '/day2/PIX_001.mov')]
        rows, flags, _ = prepare(sessions=sessions, transcripts=sources, candidates=[])
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(flags), {('day1', '/day1/PIX_001.mov'), ('day2', '/day2/PIX_001.mov'),
                                      ('day2', '/day2/pickup/PIX_001.mov')})

    def test_baseline_is_deduplicated_by_file_and_uses_true_first_speech(self):
        later = speech('later', start=20.)
        first = speech('first', start=7.)
        rows, flags, _ = prepare(transcripts=[later, first], candidates=[],
                                 intervals=[pending(), pending('gap-2', start=40., end=50.)],
                                 reference='session-1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'first')
        self.assertEqual((rows[0]['start_seconds'], rows[0]['end_seconds']), (0., 100.))
        self.assertEqual(rows[0]['dialogue_start_seconds'], 7.)
        self.assertEqual(len(flags), 1)

    def test_placed_incomplete_file_is_only_on_track_and_not_in_either_pool(self):
        first = speech('first', text='首句', start=3.)
        later = speech('later', start=10.)
        rows, flags, _ = prepare(transcripts=[later, first],
                                 candidates=[proposal(source='first', state='confirmed')], inventory=True)
        self.assertEqual(rows[0]['id'], 'first')
        self.assertEqual(flags[('session-1', '/PGM/one.mov')]['pool'], 'timeline')
        self.assertFalse(flags[('session-1', '/PGM/one.mov')]['reviewed'])
        self.assertEqual(rows[0]['dialogue_start_seconds'], 3.)
        self.assertTrue(flags[('session-1', '/PGM/one.mov')]['confirmed'])
        self.assertTrue(flags[('session-1', '/PGM/one.mov')]['active'])
        self.assertFalse(flags[('session-1', '/PGM/one.mov')]['proposed'])

    def test_matched_and_aligned_file_is_removed_including_baseline(self):
        for reference in (None, 'session-1'):
            with self.subTest(reference=reference):
                rows, flags, _ = prepare(candidates=[proposal(score=.9)], intervals=[pending()],
                                         reference=reference)
                self.assertEqual((rows, flags), ([], {}))

    def test_explicit_manual_whole_range_removes_old_pending_gaps(self):
        visual = speech('visual', start=18., text='[人工画面归类]')
        visual['data'] = dict(source_kind='manual_visual', manual_range_explicit=True,
                              manual_script_line_ids=['line-1', 'line-2'], source_anchor_seconds=18.)
        rows, flags, _ = prepare(transcripts=[speech(), visual],
                                 candidates=[proposal(source='visual', state='confirmed')],
                                 intervals=[pending()])
        self.assertEqual((rows, flags), ([], {}))

    def test_asrless_file_uses_earliest_actual_pending_interval(self):
        rows, flags, _ = prepare(transcripts=[], candidates=[],
                                 intervals=[pending(), pending('gap-early', start=5., end=9.)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'gap-early')
        self.assertTrue(rows[0]['manual_pending'])
        self.assertEqual(rows[0]['dialogue_start_seconds'], 5.)
        self.assertEqual((rows[0]['start_seconds'], rows[0]['end_seconds']), (0., 100.))
        self.assertFalse(flags[('session-1', '/PGM/one.mov')]['active'])

    def test_missing_duration_retains_real_interval_and_warning(self):
        interval = pending(start=5., end=9.)
        interval['metadata'] = {'proxy_headers': [{'very_large': list(range(1000))}]}
        interval['source_anchor_seconds'] = 6.25
        rows, flags, _ = prepare(sessions=[session(duration=False)], transcripts=[], candidates=[],
                                 intervals=[interval])
        self.assertEqual((rows[0]['start_seconds'], rows[0]['end_seconds']), (5., 9.))
        self.assertFalse(rows[0]['whole_pgm'])
        self.assertEqual(rows[0]['source_anchor_seconds'], 6.25)
        self.assertIs(rows[0]['metadata'], interval['metadata'])
        self.assertTrue(flags[('session-1', '/PGM/one.mov')]['duration_missing'])
        self.assertIn('帧率', flags[('session-1', '/PGM/one.mov')]['brief_reason'])

    def test_manual_picture_annotation_alone_never_adds_an_extra_queue(self):
        visual = speech(text='[人工画面归类]')
        visual['data'] = {'source_kind': 'manual_visual'}
        rows, flags, _ = prepare(transcripts=[visual], candidates=[])
        self.assertEqual(len(rows), 1)  # registered real file is still unmatched
        self.assertEqual(flags[('session-1', '/PGM/one.mov')]['pool'], 'unmatched')

    def test_removed_and_readded_match_refreshes_flags_without_history_resurrection(self):
        old = proposal('old', state='confirmed')
        old['active'] = False
        rows, flags, _ = prepare(candidates=[old])
        self.assertEqual(len(rows), 1)
        self.assertFalse(flags[('session-1', '/PGM/one.mov')]['active'])
        current = proposal('new', score=.4)
        rows, flags, _ = prepare(candidates=[old, current], inventory=True)
        self.assertTrue(flags[('session-1', '/PGM/one.mov')]['proposed'])
        self.assertEqual([r['id'] for r in flags[('session-1', '/PGM/one.mov')]['proposals']], ['new'])
        rows, flags, _ = prepare(candidates=[old, dict(current, score=.9)])
        self.assertEqual((rows, flags), ([], {}))

    def test_inactive_speech_and_script_candidates_do_not_affect_search_or_modes(self):
        old_speech = speech('retired', text='不能搜索到的历史台词', start=1.)
        old_speech['active'] = False
        inactive_script = proposal('old-script', score=.9)
        inactive_script['script_active'] = False
        rows, flags, _ = prepare(transcripts=[old_speech, speech()],
                                 candidates=[proposal('retired-candidate', source='retired'), inactive_script])
        state = flags[('session-1', '/PGM/one.mov')]
        self.assertEqual(rows[0]['id'], 'speech-1')
        self.assertFalse(state['active'])
        self.assertFalse(state['proposed'])
        self.assertNotIn('历史', state['searchable'])

    def test_mode_flags_are_file_wide_and_search_includes_later_confirmed_speech(self):
        sources = [speech('first', text='第一句话有许多完全没有确认过的完整台词应当等待人工复核并且不应自动通过', start=3.),
                   speech('later', text='Later special 中文关键词', start=20.)]
        row_proposal = proposal(source='first', score=.4)
        row_proposal['review_reason'] = '多个相似位置'
        rows, flags, _ = prepare(transcripts=sources,
                                 candidates=[row_proposal, proposal('confirmed-later', source='later', state='confirmed')], inventory=True)
        state = flags[('session-1', '/PGM/one.mov')]
        self.assertTrue(state['active'])
        self.assertTrue(state['proposed'])
        self.assertTrue(state['confirmed'])
        self.assertIn('later special 中文关键词', state['searchable'])
        self.assertIs(state['proposals'][0], row_proposal)
        self.assertEqual(rows[0]['id'], 'first')
        self.assertIn('多个相似位置', state['brief_reason'])

    def test_display_zero_never_changes_source_anchor_or_nested_metadata(self):
        first = speech(start=7.5)
        first.update(source_anchor_seconds=8.25,
                     metadata={'fps': '25', 'proxy_sources': [{'nested': [1, 2, 3]}]},
                     data={'source_anchor_seconds': 8.25})
        before = copy.deepcopy(first)
        rows, flags, _ = prepare(transcripts=[first], candidates=[])
        self.assertEqual(first, before)
        self.assertEqual(rows[0]['start_seconds'], 0.)
        self.assertEqual(rows[0]['dialogue_start_seconds'], 7.5)
        self.assertEqual(rows[0]['source_anchor_seconds'], 8.25)
        self.assertIs(rows[0]['metadata'], first['metadata'])
        self.assertIs(rows[0]['data'], first['data'])

    def test_registered_before_asr_is_visible_and_has_stable_whole_file_identity(self):
        first, flags, _ = prepare(transcripts=[], candidates=[])
        second, _, _ = prepare(transcripts=[], candidates=[])
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['id'], second[0]['id'])
        self.assertTrue(first[0]['id'].startswith('registered-pgm-'))
        self.assertEqual((first[0]['start_seconds'], first[0]['end_seconds']), (0.,100.))
        self.assertEqual(flags[('session-1','/PGM/one.mov')]['pool'], 'unmatched')

    def test_inventory_partitions_every_file_exactly_once_and_counts_automatic_as_blue(self):
        from pgm_editor_state import coverage_fingerprint
        sessions = [session('day1','/day1/a.mov'), session('day2','/day2/b.mov'),
                    session('day3','/day3/c.mov'), session('day4','/day4/d.mov')]
        sources = [speech('a','day1','/day1/a.mov'), speech('b','day2','/day2/b.mov'),
                   speech('d','day4','/day4/d.mov')]
        choices = [proposal('a-choice','a',score=.2), proposal('b-choice','b',state='confirmed',score=.9),
                   proposal('d-choice','d',score=.9)]
        rows, flags, data = prepare(sessions=sessions, transcripts=sources, candidates=choices, inventory=True)
        index = PgmTakeIndex(data, prepared=True)
        summary = index.file_summary(sources[1])
        fingerprint = coverage_fingerprint(summary,index.source(summary['first_candidate']))
        sessions[1]['config']['library_pgm_editor'] = {'/day2/b.mov': dict(pool='timeline',reviewed=True,coverage_fingerprint=fingerprint)}
        sessions[3]['config']['library_pgm_editor'] = {'/day4/d.mov': dict(pool='removed',reviewed=False)}
        rows, flags, data = prepare(sessions=sessions, transcripts=sources, candidates=choices, inventory=True)
        states = inventory_status_rows(rows, flags, PgmTakeIndex(data,prepared=True))
        self.assertEqual(len(states),4)
        self.assertEqual({row['status'] for row in states},{'unreviewed','reviewed','unmatched','removed'})
        pools, pf, _ = prepare(sessions=sessions, transcripts=sources, candidates=choices)
        self.assertEqual({row['media_path'] for row in pools},{'/day3/c.mov','/day4/d.mov'})
        self.assertEqual(sum(value['pool']=='timeline' for value in flags.values())+len(pools), len(rows))
        self.assertEqual(flags[('day1','/day1/a.mov')]['pool'],'timeline')
        self.assertFalse(flags[('day1','/day1/a.mov')]['reviewed'])
        self.assertTrue(flags[('day2','/day2/b.mov')]['reviewed'])
        self.assertEqual(flags[('day4','/day4/d.mov')]['pool'],'removed')  # rematching must not resurrect
        choices[1]['script_line_id']='line-2'
        _, changed, _ = prepare(sessions=sessions, transcripts=sources, candidates=choices,inventory=True)
        self.assertFalse(changed[('day2','/day2/b.mov')]['reviewed'])

    def test_rejected_only_file_is_unmatched_even_if_first_and_later_high_score(self):
        sources=[speech('a',start=5),speech('b',start=20)]
        candidates=[proposal('a','a',state='rejected',score=.99),proposal('b','b',state='rejected',score=.99)]
        rows,flags,_=prepare(transcripts=sources,candidates=candidates,inventory=True)
        self.assertEqual(len(rows),1)
        self.assertEqual(flags[('session-1','/PGM/one.mov')]['pool'],'unmatched')


class ReviewOnlySnapshotGuardTests(unittest.TestCase):
    def setUp(self):
        self.before = dict(sessions=[session()],scripts=[{'id':'l1'}],transcripts=[{'id':'t1'}],
            candidates=[{'id':'c1','score':.3}],notes=[],reference_state={'revision':1})
        self.after = copy.deepcopy(self.before)
        self.after['sessions'][0].update(revision=2,updated_by='QA')
        self.after['sessions'][0]['config']['library_pgm_editor']={'/PGM/one.mov':dict(pool='timeline',reviewed=True)}

    def accepted(self):
        from pgm_library_view import review_snapshot_compatible
        return review_snapshot_compatible(self.before,self.after,'session-1','/PGM/one.mov')

    def test_only_target_file_review_and_revision_change_can_reuse_snapshot(self):
        self.assertTrue(self.accepted())
        self.assertNotIn('library_pgm_editor',self.before['sessions'][0]['config'])

    def test_every_concurrent_source_candidate_script_note_reference_edit_forces_full_refresh(self):
        for field in ('scripts','transcripts','candidates','notes','reference_state'):
            with self.subTest(field=field):
                original = copy.deepcopy(self.after)
                if isinstance(self.after[field],list):self.after[field].append({'id':'concurrent'})
                else:self.after[field]['revision']=2
                self.assertFalse(self.accepted())
                self.after=original

    def test_concurrent_other_pgm_review_in_same_session_forces_full_refresh(self):
        self.after['sessions'][0]['config']['library_pgm_editor']['/PGM/another.mov']={'reviewed':True}
        self.assertFalse(self.accepted())

    def test_camera_sound_path_or_session_name_change_forces_full_refresh(self):
        for name in ('sources','library_sync','pgm'):
            with self.subTest(name=name):
                original=copy.deepcopy(self.after)
                self.after['sessions'][0]['config'][name]='concurrent'
                self.assertFalse(self.accepted())
                self.after=original
        self.after['sessions'][0]['name']='changed name'
        self.assertFalse(self.accepted())

    def test_new_deleted_reordered_or_other_updated_sessions_force_full_refresh(self):
        self.after['sessions'].append(session('new','/PGM/two.mov'))
        self.assertFalse(self.accepted())
        self.before['sessions'].append(session('new','/PGM/two.mov'))
        self.assertTrue(self.accepted())
        self.after['sessions'][1]['revision']=20
        self.assertFalse(self.accepted())


if __name__ == '__main__':
    unittest.main()
