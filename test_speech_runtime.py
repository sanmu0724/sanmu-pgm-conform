from concurrent.futures import ThreadPoolExecutor
import builtins
import importlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import weakref

import speech_runtime
from speech_runtime import clear_recognizer_cache, get_recognizer


class _Recognizer:
    pass


class RecognizerCacheTests(unittest.TestCase):
    def test_transcript_reuse_is_exact_bounded_and_does_not_retain_old_model(self):
        model = get_recognizer(self.factory, 'small', self.cache)
        rows = [{'text': 'Hello，世界', 'start_seconds': 1, 'end_seconds': 2}]
        speech_runtime.remember_transcription(model, ('source', 1, 2, 'auto'), rows)
        rows[0]['text'] = 'edited'
        reused = speech_runtime.cached_transcription(model, ('source', 1, 2, 'auto'))
        self.assertEqual(reused[0]['text'], 'Hello，世界')
        reused[0]['text'] = 'edited again'
        self.assertEqual(speech_runtime.cached_transcription(model, ('source', 1, 2, 'auto'))[0]['text'], 'Hello，世界')
        self.assertIsNone(speech_runtime.cached_transcription(model, ('source', 2, 3, 'auto')))
        for number in range(150):
            speech_runtime.remember_transcription(model, ('source', number), reused)
        self.assertLessEqual(len(speech_runtime._transcripts), 128)
        self.assertIsNone(speech_runtime.cached_transcription(model, ('source', 1, 2, 'auto')))
        speech_runtime.remember_transcription(model, ('same',), reused)
        other = get_recognizer(self.factory, 'medium', self.cache)
        self.assertIsNone(speech_runtime.cached_transcription(other, ('same',)))
        self.assertIsNone(speech_runtime.cached_transcription(model, ('same',)))

    def setUp(self):
        clear_recognizer_cache()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(clear_recognizer_cache)
        self.root = Path(self.directory.name)
        self.model = self.root / 'small'
        self.model.mkdir()
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (self.model / name).write_bytes(b'model fixture')
        self.cache = self.root / 'cache'
        self.factory = mock.Mock(side_effect=lambda *args, **kwargs: _Recognizer())

    def test_same_model_reuses_single_initialization(self):
        first = get_recognizer(self.factory, str(self.model), self.cache)
        second = get_recognizer(self.factory, str(self.model), self.cache)
        self.assertIs(first, second)
        self.factory.assert_called_once_with(str(self.model), device='cpu', compute_type='int8',
                                             download_root=str(self.cache))

    def test_canonical_local_path_reuses_model(self):
        first = get_recognizer(self.factory, str(self.model), self.cache)
        second = get_recognizer(self.factory, str(self.model / '..' / 'small'), self.cache)
        self.assertIs(first, second)

    def test_model_switch_retains_only_one_cached_model(self):
        one = get_recognizer(self.factory, str(self.model), self.cache)
        two = get_recognizer(self.factory, 'medium', self.cache)
        three = get_recognizer(self.factory, str(self.model), self.cache)
        self.assertIsNot(one, two)
        self.assertIsNot(one, three)
        self.assertEqual(self.factory.call_count, 3)

    def test_each_required_model_file_change_invalidates(self):
        previous = get_recognizer(self.factory, str(self.model), self.cache)
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            with self.subTest(name=name):
                with (self.model / name).open('ab') as file:
                    file.write(b'updated')
                current = get_recognizer(self.factory, str(self.model), self.cache)
                self.assertIsNot(previous, current)
                previous = current
        self.assertEqual(self.factory.call_count, 4)

    def test_same_size_stat_change_invalidates(self):
        previous = get_recognizer(self.factory, str(self.model), self.cache)
        file = self.model / 'model.bin'
        stat = file.stat()
        os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
        self.assertIsNot(previous, get_recognizer(self.factory, str(self.model), self.cache))

    def test_missing_model_file_invalidates_old_cache(self):
        previous = get_recognizer(self.factory, str(self.model), self.cache)
        (self.model / 'tokenizer.json').unlink()
        # Completeness validation belongs to the existing model resolver/backend.
        current = get_recognizer(self.factory, str(self.model), self.cache)
        self.assertIsNot(previous, current)

    def test_concurrent_callers_construct_only_once(self):
        gate = threading.Barrier(8)

        def construct(*args, **kwargs):
            time.sleep(.025)
            return _Recognizer()

        self.factory.side_effect = construct

        def load(_):
            gate.wait(timeout=5)
            return get_recognizer(self.factory, str(self.model), self.cache)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(load, range(8)))
        self.assertEqual(self.factory.call_count, 1)
        self.assertTrue(all(result is results[0] for result in results))

    def test_failed_construction_is_retried(self):
        valid = _Recognizer()
        self.factory.side_effect = [RuntimeError('load failed'), valid]
        with self.assertRaisesRegex(RuntimeError, 'load failed'):
            get_recognizer(self.factory, str(self.model), self.cache)
        self.assertIs(valid, get_recognizer(self.factory, str(self.model), self.cache))
        self.assertEqual(self.factory.call_count, 2)

    def test_failure_releases_previous_cached_model(self):
        first = get_recognizer(self.factory, str(self.model), self.cache)
        self.factory.side_effect = RuntimeError('replacement failed')
        with self.assertRaises(RuntimeError):
            get_recognizer(self.factory, 'medium', self.cache)
        self.factory.side_effect = lambda *args, **kwargs: _Recognizer()
        self.assertIsNot(first, get_recognizer(self.factory, str(self.model), self.cache))
        self.assertEqual(self.factory.call_count, 3)

    def test_different_factory_identity_does_not_share(self):
        first = get_recognizer(self.factory, str(self.model), self.cache)
        other = mock.Mock(side_effect=lambda *args, **kwargs: _Recognizer())
        self.assertIsNot(first, get_recognizer(other, str(self.model), self.cache))
        other.assert_called_once()

    def test_remote_name_uses_cache_root_as_identity(self):
        first = get_recognizer(self.factory, 'small', self.cache)
        self.assertIs(first, get_recognizer(self.factory, 'small', self.cache / '..' / 'cache'))
        self.assertIsNot(first, get_recognizer(self.factory, 'small', self.root / 'other_cache'))
        self.assertEqual(self.factory.call_count, 2)

    def test_clear_drops_cache_reference_without_closing_active_user(self):
        model = get_recognizer(self.factory, str(self.model), self.cache)
        reference = weakref.ref(model)
        clear_recognizer_cache()
        self.assertIs(reference(), model)
        del model
        self.assertIsNone(reference())
        self.assertEqual(self.factory.call_count, 1)

    def test_import_and_lookup_do_not_import_backend_or_create_downloads(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.startswith(('faster_whisper', 'huggingface_hub')):
                raise AssertionError('speech backend imported implicitly')
            return original_import(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=guarded_import):
            importlib.reload(speech_runtime)
            model = speech_runtime.get_recognizer(self.factory, 'small', self.cache)
        self.assertIsInstance(model, _Recognizer)
        self.assertFalse(self.cache.exists())
        self.factory.assert_called_once_with('small', device='cpu', compute_type='int8',
                                             download_root=str(self.cache))


if __name__ == '__main__':
    unittest.main()
