"""Retiring a UI setting must not invalidate unchanged successful edits."""
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from library_bridge import translate_config
from library_export import dialogue_ranges, sync_signature
from reference_workflow import session_fingerprint
from sound_fallback import retire_sound_references
from workflow_readiness import preparation_signature


class ReferenceRetirementCompatibilityTests(unittest.TestCase):
    def config(self):
        return dict(fps=25, sound_sources=[], pgm_sources=[],
            sound_reference_overrides={'/qa/pgm.mov': dict(path='/qa/old.mov', start_seconds=3)},
            library_context=dict(session_id='session'),
            dialogue_markers=[dict(status='confirmed', session_id='session',
                media_path='/qa/old.mov', start_seconds=4, end_seconds=5)])

    def test_fingerprints_and_export_provenance_survive_read_only_migration(self):
        config = self.config()
        cut = dict(xml_digest='same-cut', pgm_sources=[dict(path='/qa/pgm.mov', frames=250, start=0)])
        session = dict(id='session', name='day01_通条', config=config, segments=[])
        migrated = dict(session, config=retire_sound_references(config))
        self.assertEqual(preparation_signature(session), preparation_signature(migrated))
        self.assertEqual(sync_signature(config, cut), sync_signature(migrated['config'], cut))
        data = dict(sessions=[session], transcripts=[], candidates=[], script_lines=[])
        with patch('library_bridge.read_settings', return_value={}):
            first = session_fingerprint(data, 'session')
            self.assertEqual(first, session_fingerprint(dict(data, sessions=[migrated]), 'session'))

    def test_historical_dialogue_positions_keep_original_pgm_local_time(self):
        config = self.config()
        cut = dict(pgm_sources=[dict(path='/qa/pgm.mov', start=250, frames=250)])
        original = dialogue_ranges(config, cut)
        migrated = dialogue_ranges(retire_sound_references(config), cut)
        self.assertEqual(original, migrated)
        self.assertEqual([(row['timeline_start_frame'], row['timeline_end_frame']) for row in migrated],
                         [(275, 300)])

    def test_history_paths_translate_both_directions_without_reactivating_setting(self):
        config = retire_sound_references(self.config())
        before = copy.deepcopy(config)
        mappings = [dict(shared='/qa', local='/Volumes/NEXIS')]
        translated = translate_config(config, mappings)
        self.assertNotIn('sound_reference_overrides', translated)
        self.assertEqual(translated['library_legacy_sound_references'],
            {'/Volumes/NEXIS/pgm.mov': dict(path='/Volumes/NEXIS/old.mov', start_seconds=3)})
        self.assertEqual(translate_config(translated, mappings, reverse=True),
                         translate_config(config, [], reverse=True))
        self.assertEqual(config, before)

    def test_retired_missing_media_not_required_for_new_recognition(self):
        from library_ui import media_paths
        config = self.config()
        config['pgm'] = '/qa/pgm.mov'
        self.assertNotIn('/qa/old.mov', media_paths(config))
        self.assertNotIn('/qa/old.mov', media_paths(retire_sound_references(config)))

    def test_translation_never_creates_a_removed_setting_on_new_projects(self):
        config = dict(fps=25, sound_sources=[])
        for reverse in (True, False):
            self.assertNotIn('sound_reference_overrides', translate_config(config, [], reverse=reverse))
            self.assertNotIn('library_legacy_sound_references', translate_config(config, [], reverse=reverse))


if __name__ == '__main__':
    unittest.main()
