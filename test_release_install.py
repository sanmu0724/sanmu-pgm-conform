"""Release branding and reversible upgrades never move the user's catalogue."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_distribution as distribution
import install_macos as installer
import library_bridge


SOURCE = Path(__file__).resolve().parent


class ReleaseInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'home'
        self.runtime = self.home / 'Library/Application Support/PGM机位识别/runtime'
        self.app_dir = self.home / 'Desktop'
        self.app_path = self.app_dir / (distribution.APP_NAME + '.app')

    def tearDown(self):
        self.temporary.cleanup()

    def bundle(self, name, marker, identity=installer.BUNDLE_ID):
        app = self.app_dir / name
        (app / 'Contents').mkdir(parents=True)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identity}))
        (app / 'marker.txt').write_text(marker, encoding='utf-8')
        return app

    def old_runtime(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / 'installation.json').write_text(json.dumps({'managed_by': installer.MANAGER}), encoding='utf-8')
        (self.runtime / 'old-runtime.txt').write_text('previous runtime', encoding='utf-8')

    def user_data(self):
        folder = self.runtime.parent
        paths = {'server/library.sqlite3': b'original database', 'server/service.token': b'private-token',
                 'workstation.json': b'{"selected_production":"same-uuid"}', 'models/small/model.bin': b'cached-model'}
        for relative, content in paths.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return {folder / relative: content for relative, content in paths.items()}

    def fake_command(self, command, **kwargs):
        self.assertEqual(command[1:3], ['-m', 'venv'])
        python = Path(command[3]) / 'bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('isolated test interpreter', encoding='utf-8')

    def installation(self, **kwargs):
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, 'home', return_value=self.home))
            stack.enter_context(patch.object(installer, 'local_target', side_effect=lambda path: Path(path).resolve()))
            stack.enter_context(patch.object(installer, 'find_python', return_value=sys.executable))
            stack.enter_context(patch.object(installer, 'validate_architecture'))
            stack.enter_context(patch.object(installer, 'python_info', return_value={'version': [3, 12, 0]}))
            stack.enter_context(patch.object(installer, 'sign_app'))
            stack.enter_context(patch.object(installer.subprocess, 'run', side_effect=self.fake_command))
            stack.enter_context(redirect_stdout(io.StringIO()))
            return installer.install(SOURCE, self.runtime, self.app_dir, sys.executable, no_deps=True, **kwargs)

    def visible_apps(self):
        return sorted(path.name for path in self.app_dir.glob('*.app') if not path.name.startswith('.'))

    def test_template_brand_and_version_keep_bundle_identity(self):
        value = plistlib.loads((SOURCE / 'PGMTimeline.app/Contents/Info.plist').read_bytes())
        self.assertEqual(value['CFBundleDisplayName'], '三木的PGM回套软件')
        self.assertEqual(value['CFBundleName'], distribution.APP_NAME)
        self.assertEqual(value['CFBundleIdentifier'], installer.BUNDLE_ID)
        self.assertEqual(value['CFBundleExecutable'], 'PGMTimeline')
        self.assertEqual((value['CFBundleShortVersionString'], value['CFBundleVersion']),
                         (distribution.APP_VERSION, distribution.APP_BUILD))

    def test_source_copy_contains_only_new_commands_and_required_modules(self):
        target = distribution.copy_program(SOURCE, self.root / 'program')
        self.assertTrue((target / '安装三木的PGM回套软件.command').is_file())
        self.assertTrue((target / '启动三木的PGM回套软件.command').is_file())
        self.assertFalse((target / '安装PGM机位识别.command').exists())
        self.assertFalse((target / '启动PGM识别.command').exists())
        for name in ('project_ui.py', 'project_state.py', 'workflow_readiness.py', 'recording_schema.py', 'registered_sound.py',
                     'reference_timeline.py', 'reference_workflow.py', 'ui_theme.py'):
            self.assertTrue((target / name).is_file(), name)
        self.assertFalse(list(target.glob('test*.py')))
        self.assertTrue((target/'local_project.py').is_file())
        for name in ('library_service.py','shared_library_launcher.py','启动机房素材库.command'):
            self.assertFalse((target/name).exists(),name)

    def test_isolated_distribution_has_brand_version_and_clean_launcher(self):
        # This is a throwaway test archive, never the final published ZIP.
        with patch.object(distribution, 'sign_app'):
            result = distribution.build(SOURCE, self.root / 'test-release.zip')
        with zipfile.ZipFile(result['path']) as archive:
            prefix = distribution.APP_NAME + '/'
            self.assertTrue(all(name.startswith(prefix) for name in archive.namelist()))
            manifest = json.loads(archive.read(prefix + 'distribution_manifest.json'))
            self.assertEqual((manifest['application_version'], manifest['application_build']),
                             (distribution.APP_VERSION, distribution.APP_BUILD))
            self.assertEqual(manifest['application'], distribution.APP_NAME)
            launcher = json.loads(archive.read(prefix + 'PGMTimeline.app/Contents/Resources/Launcher.json'))
            self.assertEqual(launcher, {'python': '', 'entry': ''})
            for row in manifest['files']:
                content = archive.read(prefix + row['path'])
                self.assertEqual(hashlib.sha256(content).hexdigest(), row['sha256'])
            self.assertFalse(any('sqlite3' in name or 'workstation.json' in name or '/test_' in name
                                 for name in archive.namelist()))

    def test_fresh_install_has_one_new_visible_app_and_local_launcher(self):
        result = self.installation()
        self.assertEqual(self.visible_apps(), [distribution.APP_NAME + '.app'])
        self.assertEqual(result['migrate_apps'], [])
        self.assertEqual((result['version'], result['build']), (distribution.APP_VERSION, distribution.APP_BUILD))
        launcher = json.loads((self.app_path / 'Contents/Resources/Launcher.json').read_text())
        self.assertEqual(launcher['entry'], str(self.runtime / 'program/pgm_app.py'))
        self.assertEqual(launcher['python'], str(self.runtime / '.venv/bin/python'))
        self.assertTrue((self.runtime / 'program/启动三木的PGM回套软件.command').is_file())
        self.assertTrue((self.runtime / 'program/.venv').is_symlink())

    def test_legacy_app_upgrade_preserves_database_settings_and_models(self):
        self.old_runtime()
        data = self.user_data()
        legacy = self.bundle('PGM机位识别.app', 'legacy app')
        result = self.installation()
        self.assertEqual(self.visible_apps(), [distribution.APP_NAME + '.app'])
        self.assertEqual(result['migrate_apps'], [str(legacy)])
        self.assertEqual((Path(result['previous_app']) / 'marker.txt').read_text(), 'legacy app')
        self.assertEqual((Path(result['previous_runtime']) / 'old-runtime.txt').read_text(), 'previous runtime')
        self.assertTrue(Path(result['previous_app']).name.startswith('.'))
        for path, expected in data.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_both_old_and_new_installed_entries_become_hidden_backups(self):
        self.bundle(distribution.APP_NAME + '.app', 'previous new name')
        self.bundle('PGM机位识别.app', 'legacy name')
        result = self.installation()
        self.assertEqual(self.visible_apps(), [distribution.APP_NAME + '.app'])
        self.assertEqual(len(result['previous_apps']), 2)
        self.assertEqual({(Path(row['backup']) / 'marker.txt').read_text() for row in result['previous_apps']},
                         {'previous new name', 'legacy name'})

    def test_failure_after_legacy_move_restores_both_apps_and_runtime(self):
        self.old_runtime()
        data = self.user_data()
        self.bundle(distribution.APP_NAME + '.app', 'previous new name')
        self.bundle('PGM机位识别.app', 'legacy name')
        original_rename = Path.rename
        def fail_install(path, target):
            if path.name.startswith('.' + distribution.APP_NAME + '.installing_') and Path(target) == self.app_path:
                raise OSError('simulated final app installation failure')
            return original_rename(path, target)
        with patch.object(Path, 'rename', fail_install):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.installation()
        self.assertEqual(self.visible_apps(), sorted([distribution.APP_NAME + '.app', 'PGM机位识别.app']))
        self.assertEqual((self.app_path / 'marker.txt').read_text(), 'previous new name')
        self.assertEqual((self.app_dir / 'PGM机位识别.app/marker.txt').read_text(), 'legacy name')
        self.assertEqual((self.runtime / 'old-runtime.txt').read_text(), 'previous runtime')
        self.assertFalse(list(self.app_dir.glob('.*.app')))
        self.assertFalse(list(self.runtime.parent.glob('runtime.*')))
        for path, expected in data.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_same_name_unrelated_application_is_never_moved(self):
        legacy = self.bundle('PGM机位识别.app', 'another application', 'example.unrelated')
        with self.assertRaisesRegex(ValueError, '不属于本软件'):
            self.installation()
        self.assertEqual((legacy / 'marker.txt').read_text(), 'another application')
        self.assertFalse(self.runtime.exists())

    def test_legacy_desktop_identity_can_upgrade_old_named_app(self):
        legacy = self.bundle('PGM机位识别.app', 'legacy desktop', installer.LEGACY_DESKTOP_BUNDLE_ID)
        data = self.user_data()
        result = self.installation()
        self.assertEqual(self.visible_apps(), [distribution.APP_NAME + '.app'])
        self.assertEqual(result['migrate_apps'], [str(legacy)])
        self.assertEqual((Path(result['previous_app']) / 'marker.txt').read_text(), 'legacy desktop')
        for path, expected in data.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_legacy_desktop_identity_can_upgrade_new_named_app(self):
        self.bundle(distribution.APP_NAME + '.app', 'renamed legacy desktop', installer.LEGACY_DESKTOP_BUNDLE_ID)
        result = self.installation()
        self.assertEqual(self.visible_apps(), [distribution.APP_NAME + '.app'])
        self.assertEqual(result['migrate_apps'], [])
        self.assertEqual((Path(result['previous_app']) / 'marker.txt').read_text(), 'renamed legacy desktop')

    def test_unknown_identity_in_new_named_app_is_never_moved(self):
        app = self.bundle(distribution.APP_NAME + '.app', 'unrelated renamed app', 'example.unknown')
        with self.assertRaisesRegex(ValueError, '不属于本软件'):
            self.installation()
        self.assertEqual((app / 'marker.txt').read_text(), 'unrelated renamed app')
        self.assertFalse(self.runtime.exists())

    def test_symlink_application_is_not_followed_or_replaced(self):
        external = self.root / 'external-app'
        external.mkdir()
        self.app_dir.mkdir(parents=True)
        legacy = self.app_dir / 'PGM机位识别.app'
        legacy.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '普通应用目录'):
            self.installation()
        self.assertTrue(legacy.is_symlink())
        self.assertTrue(external.exists())

    def test_dry_run_reports_legacy_migration_without_writes(self):
        legacy = self.bundle('PGM机位识别.app', 'legacy')
        result = self.installation(dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['migrate_apps'], [str(legacy)])
        self.assertEqual(self.visible_apps(), ['PGM机位识别.app'])
        self.assertFalse(self.runtime.exists())

    def test_default_data_directory_remains_legacy_and_environment_override_works(self):
        with patch.object(Path, 'home', return_value=self.home), patch.object(library_bridge.sys, 'platform', 'darwin'):
            with patch.dict(library_bridge.os.environ, {'PGM_LIBRARY_HOME': ''}):
                self.assertEqual(library_bridge.data_dir(), self.runtime.parent)
            with patch.dict(library_bridge.os.environ, {'PGM_LIBRARY_HOME': str(self.root / 'chosen-library')}):
                self.assertEqual(library_bridge.data_dir(), self.root / 'chosen-library')

    def test_resolve_menu_is_local_and_recorded_without_changing_media(self):
        from resolve_import_helper import MENU_FILENAME, MANAGED_MARKER
        directory = self.home / 'Resolve Scripts/Edit'
        data = self.user_data()
        result = self.installation(resolve_scripts_dir=directory)
        target = directory / MENU_FILENAME
        self.assertIn(MANAGED_MARKER, target.read_text())
        self.assertEqual(result['resolve_menu'], str(target))
        manifest = json.loads((self.runtime / 'installation.json').read_text())
        self.assertEqual(manifest['resolve_menu'], str(target))
        for path, expected in data.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_resolve_menu_dry_run_has_no_writes(self):
        directory = self.root / 'Resolve Scripts/Edit'
        result = self.installation(resolve_scripts_dir=directory, dry_run=True)
        self.assertTrue(result['resolve_menu'])
        self.assertFalse(directory.exists())

    def test_existing_unmanaged_resolve_menu_is_not_overwritten(self):
        from resolve_import_helper import MENU_FILENAME
        directory = self.root / 'Resolve Scripts/Edit'
        directory.mkdir(parents=True)
        target = directory / MENU_FILENAME
        target.write_text('-- user script')
        with self.assertRaisesRegex(ValueError, '非本软件'):
            self.installation(resolve_scripts_dir=directory)
        self.assertEqual(target.read_text(), '-- user script')
        self.assertFalse(self.runtime.exists())

    def test_resolve_menu_failure_restores_previous_app_and_runtime(self):
        from resolve_import_helper import MENU_FILENAME, MANAGED_MARKER
        self.old_runtime()
        self.bundle(distribution.APP_NAME + '.app', 'old app')
        directory = self.root / 'Resolve Scripts/Edit'
        directory.mkdir(parents=True)
        target = directory / MENU_FILENAME
        target.write_text(MANAGED_MARKER + '\n-- previous')
        before = target.read_bytes()
        with patch('resolve_import_helper.write_resolve_menu_script', side_effect=OSError('test menu write failure')):
            with self.assertRaisesRegex(OSError, 'menu write failure'):
                self.installation(resolve_scripts_dir=directory)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual((self.runtime/'old-runtime.txt').read_text(), 'previous runtime')
        self.assertEqual((self.app_path/'marker.txt').read_text(), 'old app')


if __name__ == '__main__':
    unittest.main()
