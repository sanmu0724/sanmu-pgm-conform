"""Offline payload integrity, no-index installation and rollback semantics."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_offline_distribution as builder
import install_macos as installer
import offline_assets as assets
import dialogue_engine as engine

SOURCE = Path(__file__).resolve().parent


class OfflineInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        (self.assets / 'python-3.12.10-macos11.pkg').write_bytes(b'unit-test-only-pkg')
        for model in ('small', 'medium'):
            folder = self.assets / 'models' / model
            folder.mkdir(parents=True)
            for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'):
                (folder / name).write_bytes((model+'-'+name).encode())
        wheels = self.assets / 'wheels'
        wheels.mkdir()
        with zipfile.ZipFile(wheels / 'dummy-1.0-py3-none-any.whl', 'w') as archive:
            archive.writestr('dummy-1.0.dist-info/METADATA', 'Name: dummy\nVersion: 1.0\n')
        self.source = self.root / 'release'
        with patch.object(builder, 'sign_app'):
            builder.prepare(SOURCE, self.assets, self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_hash_lock_and_archive_round_trip(self):
        manifest = assets.verify_payload(self.source)
        self.assertEqual(manifest['models'], ['small', 'medium'])
        self.assertIn('dummy==1.0 --hash=sha256:',
                      (self.source / 'offline_payload/requirements-offline.txt').read_text())
        result = builder.archive_folder(self.source, self.root / 'test.zip')
        with zipfile.ZipFile(result['path']) as archive:
            self.assertTrue(any(n.endswith('/offline_manifest.json') for n in archive.namelist()))
            self.assertTrue(any(n.endswith('/models/medium/model.bin') for n in archive.namelist()))
        self.assertEqual(result['sha256'], assets.sha256_file(self.root/'test.zip'))

    def test_corruption_missing_model_and_invalid_paths_fail_before_install(self):
        for path in ('offline_payload/models/small/model.bin', 'offline_payload/models/medium/tokenizer.json',
                     'offline_payload/python-3.12.10-macos11.pkg'):
            file = self.source / path
            original = file.read_bytes()
            file.write_bytes(b'corruption')
            with self.assertRaisesRegex(ValueError, '校验失败'):
                assets.verify_payload(self.source)
            file.write_bytes(original)
        manifest_path = self.source / 'offline_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'][0]['path'] = '../outside'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, '清单无效'):
            assets.verify_payload(self.source)

    def test_pip_is_isolated_offline_binary_only_and_hash_locked(self):
        command = assets.pip_command('/fresh/python', self.source)
        for flag in ('--isolated', '--no-index', '--require-hashes', '--only-binary=:all:', '--no-cache-dir', '--disable-pip-version-check'):
            self.assertIn(flag, command)
        self.assertFalse(any('http:' in arg or 'https:' in arg for arg in command))

    def test_intel_manifest_is_supported_and_wrong_python_arch_fails_before_writes(self):
        manifest_path = self.source/'offline_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['architecture'] = 'x86_64'
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(assets.verify_payload(self.source)['architecture'], 'x86_64')
        with patch.object(installer.platform, 'machine', return_value='arm64'), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, '当前Python运行架构'):
                installer.install(self.source, self.root/'runtime', self.root/'apps', sys.executable)
        self.assertFalse((self.root/'runtime').exists())

    def test_bundled_models_resolve_without_cache_and_missing_model_fails_closed(self):
        program = self.root / 'program'
        program.mkdir()
        assets.install_models(self.source, program)
        with patch.object(engine, '__file__', str(program/'dialogue_engine.py')):
            self.assertEqual(engine.resolve_model_path('small', self.root/'empty-cache'),
                             str(program/'bundled_models/small'))
            (program/'bundled_models/medium/tokenizer.json').unlink()
            with self.assertRaisesRegex(engine.DialogueError, '不会联网下载'):
                engine.resolve_model_path('medium', self.root/'empty-cache')
            user = self.root/'user-cache/small'
            user.mkdir(parents=True)
            for name in ('model.bin', 'config.json', 'tokenizer.json'):
                (user/name).write_text('preserved user model')
            self.assertEqual(engine.resolve_model_path('small', user.parent), str(user.resolve()))

    def install(self, fail=False):
        self.commands = []
        def run(command, **kwargs):
            self.commands.append(command)
            if command[1:3] == ['-m', 'venv']:
                python = Path(command[3])/'bin/python'
                python.parent.mkdir(parents=True)
                python.write_text('test interpreter')
            elif '--no-index' in command and fail:
                raise RuntimeError('simulated offline pip failure')
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, 'home', return_value=self.root/'home'))
            stack.enter_context(patch.object(installer, 'local_target', side_effect=lambda path: Path(path).resolve()))
            stack.enter_context(patch.object(installer, 'find_python', return_value=sys.executable))
            stack.enter_context(patch.object(installer.platform, 'machine', return_value='arm64'))
            stack.enter_context(patch.object(installer, 'validate_architecture'))
            stack.enter_context(patch.object(installer, 'python_info', return_value={'version':[3,12,10]}))
            stack.enter_context(patch.object(installer, 'sign_app'))
            stack.enter_context(patch.object(installer.subprocess, 'run', side_effect=run))
            stack.enter_context(redirect_stdout(io.StringIO()))
            return installer.install(self.source, self.root/'runtime', self.root/'apps', sys.executable)

    def test_auto_detect_offline_installs_models_in_runtime_and_keeps_user_cache(self):
        cache = self.root/'home/Library/Application Support/PGM机位识别/models/small/model.bin'
        cache.parent.mkdir(parents=True);cache.write_bytes(b'user model')
        result = self.install()
        self.assertTrue(result['offline'])
        self.assertFalse(result['download_dependencies'])
        self.assertTrue((self.root/'runtime/program/bundled_models/medium/model.bin').is_file())
        self.assertEqual(cache.read_bytes(), b'user model')
        self.assertTrue(any('--no-index' in command for command in self.commands))
        record = json.loads((self.root/'runtime/installation.json').read_text())
        self.assertTrue(record['offline'])
        self.assertEqual(set(record['bundled_models']), {'small','medium'})

    def test_offline_failure_restores_existing_runtime_and_app(self):
        runtime = self.root/'runtime';runtime.mkdir()
        (runtime/'installation.json').write_text(json.dumps({'managed_by':installer.MANAGER}))
        (runtime/'old').write_text('keep this')
        with self.assertRaisesRegex(RuntimeError, 'simulated'):
            self.install(fail=True)
        self.assertEqual((runtime/'old').read_text(), 'keep this')
        self.assertFalse((runtime/'program').exists())

    def test_incomplete_package_never_creates_runtime(self):
        (self.source/'offline_payload/models/small/model.bin').unlink()
        with self.assertRaises(ValueError), redirect_stdout(io.StringIO()):
            installer.install(self.source, self.root/'runtime', self.root/'apps', sys.executable)
        self.assertFalse((self.root/'runtime').exists())


if __name__ == '__main__':
    unittest.main()
