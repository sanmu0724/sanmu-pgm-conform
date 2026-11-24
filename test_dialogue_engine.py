import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
import zipfile

import av  # Load compiled dependencies before temporary sys.modules mocks.
import numpy
import dialogue_engine as engine
import speech_runtime


class DialogueEngineTests(unittest.TestCase):
    def test_exact_source_range_reuses_asr_and_file_or_language_changes_invalidate(self):
        calls = []
        class Model:
            def __init__(self, *args, **kwargs):
                pass
            def transcribe(self, samples, **kwargs):
                calls.append(kwargs)
                return iter([SimpleNamespace(start=.1, end=.8, text='Hello，世界')]), None
        path = self.make_wav()
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}), \
             patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.home)}):
            first = engine.transcribe_media(path, start_seconds=1, duration=1)
            first[0]['text'] = 'edited'
            with patch.object(engine, '_extract_audio', side_effect=AssertionError('duplicate decode')):
                self.assertEqual(engine.transcribe_media(path, start_seconds=1, duration=1)[0]['text'], 'Hello，世界')
            self.assertEqual(len(calls), 1)
            engine.transcribe_media(path, start_seconds=2, duration=1)
            engine.transcribe_media(path, start_seconds=1, duration=1, language='en')
            self.assertEqual(len(calls), 3)
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
            engine.transcribe_media(path, start_seconds=1, duration=1)
            self.assertEqual(len(calls), 4)

    def setUp(self):
        speech_runtime.clear_recognizer_cache()
        self.addCleanup(speech_runtime.clear_recognizer_cache)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def write(self, name, text, encoding='utf-8'):
        path = self.home / name
        path.write_text(text, encoding=encoding)
        return path

    def test_text_encodings_and_original_words(self):
        text = '第一幕 第一场\n（灯光渐暗）\n甲：你明天还来吗？\n乙：（低声）二十点，我等你。\n舞台提示：甲离开\n'
        for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
            with self.subTest(encoding=encoding):
                rows = engine.import_script(self.write('script.txt', text, encoding))
                self.assertEqual(len(rows), 4)
                self.assertEqual(rows[0]['speaker'], '')
                self.assertEqual(rows[0]['text'], '（灯光渐暗）')
                self.assertEqual(rows[1], dict(order=1, scene='第一幕 第一场', speaker='甲', text='你明天还来吗？'))
                self.assertEqual(rows[2]['text'], '（低声）二十点，我等你。')
                self.assertEqual(rows[3]['speaker'], '')
                self.assertEqual(rows[3]['text'], '舞台提示：甲离开')

    def test_scene_and_english_code_switch(self):
        text = 'ACT I\nSCENE 2 — Garden\nAlice: “Wait—我们一起走。”\nJohn: I will wait here.\n灯光渐暗。\n[Exit John]\n第三场\n甲：你在哪里？'
        rows = engine.parse_script(text)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['scene'], 'SCENE 2 — Garden')
        self.assertEqual(rows[0]['speaker'], 'Alice')
        self.assertEqual(rows[0]['text'], '“Wait—我们一起走。”')
        self.assertEqual(rows[1]['text'], 'I will wait here.')
        self.assertEqual(engine._spoken_text(rows[1]['text']), 'I will wait here.')
        self.assertEqual(rows[2]['speaker'], '')
        self.assertEqual(rows[2]['text'], '灯光渐暗。\n[Exit John]')
        self.assertEqual(rows[3]['scene'], '第三场')

    def test_multiline_and_stage_only_role(self):
        rows = engine.parse_script('第一场\n甲：我们今天\n一起去看看。\n乙：（沉默）\n\n甲：好吗？')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['text'], '我们今天\n一起去看看。')
        self.assertEqual(rows[1]['speaker'], '乙')
        self.assertEqual(rows[1]['text'], '（沉默）')

    def test_named_turn_keeps_paragraphs_and_actions_until_next_speaker(self):
        rows = engine.parse_script('第一幕\n甲：先等我一下。\n\n（甲拿起外套）\n我还没有说完，\nPlease wait.\n\n动作：甲走到门口\n乙：好。')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['speaker'], '甲')
        self.assertEqual(rows[0]['text'], '先等我一下。\n\n（甲拿起外套）\n我还没有说完，\nPlease wait.\n\n动作：甲走到门口')
        self.assertEqual(rows[1]['speaker'], '乙')
        self.assertEqual(rows[1]['text'], '好。')
        self.assertNotIn('甲走到门口', engine._spoken_text(rows[0]['text']))
        self.assertNotIn('甲拿起外套', engine._spoken_text(rows[0]['text']))

    def test_role_only_cue_keeps_following_action_and_wrapped_dialogue(self):
        rows = engine.parse_script('SCENE 1\nAlice:\n(Opening the door)\nHello,\n世界。\n\nI am back.\nJohn: Welcome.')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['speaker'], 'Alice')
        self.assertEqual(rows[0]['text'], '(Opening the door)\nHello,\n世界。\n\nI am back.')
        self.assertEqual(rows[1]['text'], 'Welcome.')

    def test_unlabelled_text_uses_natural_paragraphs(self):
        rows = engine.parse_script('第一场\n没有角色标注的第一句，\n换行之后仍是同一自然段。\n\n这是第二段。\n（他停顿）\nThen he continues.\n\n第三段。')
        self.assertEqual([row['text'] for row in rows], [
            '没有角色标注的第一句，\n换行之后仍是同一自然段。',
            '这是第二段。\n（他停顿）\nThen he continues.', '第三段。'])
        self.assertTrue(all(row['speaker'] == '' for row in rows))
        self.assertEqual([row['order'] for row in rows], [0, 1, 2])

    def test_scene_heading_ends_turn_and_new_explicit_cue_starts_another(self):
        rows = engine.parse_script('甲：第一段。\n\n继续第一段。\n甲：明确的新发言。\n第二场\n没有角色的段落。\n\n乙：新场对白。')
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['text'], '第一段。\n\n继续第一段。')
        self.assertEqual(rows[1]['text'], '明确的新发言。')
        self.assertEqual(rows[2]['scene'], '第二场')
        self.assertEqual(rows[2]['speaker'], '')
        self.assertEqual(rows[3]['speaker'], '乙')

    def test_action_only_role_does_not_leak_into_previous_turn(self):
        rows = engine.parse_script('甲：我要走了。\n乙：（沉默）\n[Looks away]\n甲：再见。')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['text'], '我要走了。')
        self.assertEqual(rows[1]['speaker'], '乙')
        self.assertEqual(rows[1]['text'], '（沉默）\n[Looks away]')
        self.assertEqual(rows[2]['text'], '再见。')

    def test_standalone_directions_retained_without_invented_owner(self):
        rows = engine.parse_script('第一场\n（音乐渐弱）\n\n甲：你来了。\n（乙转身）\n乙：我来了。\n灯光渐暗。')
        self.assertEqual(len(rows), 5)
        self.assertEqual([(row['speaker'], row['text']) for row in rows], [
            ('', '（音乐渐弱）'), ('甲', '你来了。'), ('', '（乙转身）'), ('乙', '我来了。'), ('', '灯光渐暗。')])

    def test_action_only_nodes_are_not_automatically_matched_to_audio(self):
        rows = engine.parse_script('第一场\n甲：（沉默）\n舞台提示：甲起身\n灯光渐暗。')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['speaker'], '甲')
        self.assertEqual(rows[0]['text'], '（沉默）\n舞台提示：甲起身')
        self.assertTrue(all(engine.normalize_dialogue(row['text']) == '' for row in rows))
        transcript = dict(id='audio1', session_id='s1', media_path='/a.mov',
                          start_seconds=0, end_seconds=3, text='甲起身，灯光渐暗。')
        self.assertEqual(engine.match_dialogue(rows, [transcript]), [])

    def test_docx_paragraph_wraps_do_not_split_named_turn(self):
        path = self.home / 'paragraphs.docx'
        paragraphs = ['第一场', 'Alice: Hello.', '', '(She waves.)', '我们又见面了。', 'John: Welcome.']
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        xml += ''.join('<w:p><w:r><w:t>' + text + '</w:t></w:r></w:p>' for text in paragraphs)
        xml += '</w:body></w:document>'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        rows = engine.import_script(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['text'], 'Hello.\n\n(She waves.)\n我们又见面了。')

    def test_pdf_page_break_keeps_named_turn(self):
        pages = [SimpleNamespace(extract_text=lambda: 'SCENE 1\nAlice: Hello.\n'),
                 SimpleNamespace(extract_text=lambda: '\n世界。\nJohn: Goodbye.')]
        fake_reader = lambda path: SimpleNamespace(is_encrypted=False, pages=pages)
        with patch.dict(sys.modules, {'pypdf': SimpleNamespace(PdfReader=fake_reader)}):
            rows = engine.import_script(self.home / 'script.pdf')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['text'], 'Hello.\n\n世界。')

    def test_symbol_character_cues_keep_wrapped_mixed_dialogue_and_ignore_pages(self):
        rows = engine.parse_script('《剧名》第一幕\n◆ Algernon\n（坐下）Hello，\n'
                                   '世界。\n- 2 -\n\n这是同一段话。\n'
                                   '■ Lane\n好的。\n—— Lane 退场\n◆ Algernon\n再见。')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['scene'], '《剧名》第一幕')
        self.assertEqual(rows[0]['speaker'], 'Algernon')
        self.assertEqual(rows[0]['text'], '（坐下）Hello，\n世界。\n\n这是同一段话。')
        self.assertEqual(rows[1]['text'], '好的。\n—— Lane 退场')
        self.assertEqual(engine._spoken_text(rows[1]['text']), '好的。')
        self.assertEqual(rows[2]['text'], '再见。')

    def test_symbol_edition_cover_and_appendix_are_not_dialogue(self):
        rows = engine.parse_script('剧名\n作 者：某人\n人物记号对照表\n'
                                   '◆ Algernon 阿尔杰农 1F4E79\n'
                                   '《剧名》第一幕\n◆ Algernon\n你好。\n'
                                   '● Worthing 先生\nGoodbye.\n▪ 全剧终\n'
                                   '排演备注\n· 字体大小说明。\n')
        self.assertEqual([(r['speaker'], r['text']) for r in rows],
                         [('Algernon', '你好。'), ('Worthing 先生', 'Goodbye.'), ('', '▪ 全剧终')])
        self.assertEqual(engine.normalize_dialogue(rows[-1]['text']), '')

    def test_symbol_aliases_and_joint_character_cues_remain_explicit(self):
        rows = engine.parse_script('第二幕\n● Jack\n我是 Jack。\n● Worthing 先生\n'
                                   '我是 Worthing。\n▲ Gwendolen ◇ Cecily\n我们同意。\n')
        self.assertEqual([r['speaker'] for r in rows], ['Jack', 'Worthing 先生', 'Gwendolen ◇ Cecily'])
        self.assertEqual([r['text'] for r in rows], ['我是 Jack。', '我是 Worthing。', '我们同意。'])

    def test_symbol_edition_internal_colon_and_estate_number_do_not_split_speech(self):
        rows = engine.parse_script('人物记号对照表\n◆ Jack\n《剧名》第一幕\n'
                                   '◆ Jack\n阁下似乎不明白：我就是那座农\n'
                                   '场 149 号产业的主人。\n（原文：我的意思。）\n'
                                   '■ Lane\n明白了。\n《剧名》第二幕\n◆ Jack\n又见面了。')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['speaker'], 'Jack')
        self.assertEqual(rows[0]['scene'], '《剧名》第一幕')
        self.assertIn('阁下似乎不明白：', rows[0]['text'])
        self.assertIn('场 149 号产业', rows[0]['text'])
        self.assertEqual(rows[2]['scene'], '《剧名》第二幕')

    def test_unrelated_dash_direction_is_separate_context(self):
        rows = engine.parse_script('第一幕\n◆ Algernon\n等一下。\n'
                                   '—— Lane 退场，\n灯熄灭。\n■ Lane\n好的。')
        self.assertEqual([r['speaker'] for r in rows], ['Algernon', '', 'Lane'])
        self.assertEqual(rows[1]['text'], '—— Lane 退场，\n灯熄灭。')
        self.assertEqual(engine.normalize_dialogue(rows[1]['text']), '')

    def test_docx_runs_tables_and_breaks(self):
        path = self.home / 'script.docx'
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>第一场</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Alice：</w:t></w:r><w:r><w:t>Hello</w:t><w:br/><w:t>世界。</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        rows = engine.import_script(path)
        self.assertEqual(rows[0]['text'], 'Hello\n世界。')

    def test_empty_script_reports_error(self):
        with self.assertRaisesRegex(engine.DialogueError, '没有可读取的对白'):
            engine.parse_script('第一幕\n\n')

    def test_stage_only_script_can_be_manually_classified(self):
        rows = engine.parse_script('第一幕\n（灯光渐暗）\n舞台提示：结束')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], '（灯光渐暗）\n舞台提示：结束')
        self.assertEqual(rows[0]['speaker'], '')

    def test_pdf_without_text_and_missing_dependency(self):
        with patch.dict(sys.modules, {'pypdf': None}):
            with self.assertRaisesRegex(engine.DialogueError, 'pypdf'):
                engine.import_script(self.home / 'script.pdf')
        fake_reader = lambda path: SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: '')])
        with patch.dict(sys.modules, {'pypdf': SimpleNamespace(PdfReader=fake_reader)}):
            with self.assertRaisesRegex(engine.DialogueError, '扫描件'):
                engine.import_script(self.home / 'script.pdf')

    def test_real_text_pdf(self):
        try:
            from pypdf import PdfWriter
            from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        except ImportError:
            self.skipTest('optional pypdf is not installed')
        writer = PdfWriter()
        page = writer.add_blank_page(width=400, height=400)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b'BT /F1 12 Tf 30 350 Td (SCENE 1) Tj 0 -30 Td (Alice: Hello world.) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(content)
        path = self.home / 'text.pdf'
        with path.open('wb') as handle:
            writer.write(handle)
        rows = engine.import_script(path)
        self.assertEqual(rows[0]['speaker'], 'Alice')
        self.assertEqual(rows[0]['text'], 'Hello world.')
        notes = engine.import_notes(path)
        self.assertIn('Alice: Hello world.', notes[0]['rawtext'])
        self.assertNotIn('dialogue', notes[0])

    def test_srt_times_not_frame_rounded(self):
        rows = engine.import_transcript(self.write('captions.srt', '1\n00:00:00,041 --> 00:00:01,999\n你好。\n\n2\n01:02:03,125 --> 01:02:04,500\nHello\nworld!'))
        self.assertEqual(rows[0]['start_seconds'], .041)
        self.assertEqual(rows[0]['end_seconds'], 1.999)
        self.assertEqual(rows[1]['start_seconds'], 3723.125)
        self.assertEqual(rows[1]['text'], 'Hello\nworld!')

    def test_webvtt_cues_and_speaker(self):
        rows = engine.import_transcript(self.write('captions.vtt', 'WEBVTT\n\nNOTE ignore this\n\ncue-one\n00:01.040 --> 00:03.500 align:start\n<v Alice>Hello &amp; 世界</v>\n\n00:04.000 --> 00:05.000\n<b>Bye</b>'))
        self.assertEqual(rows[0]['speaker'], 'Alice')
        self.assertEqual(rows[0]['text'], 'Hello & 世界')
        self.assertEqual(rows[0]['start_seconds'], 1.04)
        self.assertEqual(len(rows), 2)

    def test_transcript_json_common_fields_preserved(self):
        path = self.write('transcript.json', json.dumps({'segments': [dict(id='t1', start=1.125, end=2.125, text='你好', speaker='甲')]}))
        rows = engine.import_transcript(path)
        self.assertEqual(rows[0]['id'], 't1')
        self.assertEqual(rows[0]['speaker'], '甲')
        self.assertEqual(rows[0]['end_seconds'], 2.125)

    def test_invalid_or_unsorted_transcript_rejected(self):
        cases = [dict(start=-1, end=2), dict(start=0, end=0), dict(start=0, end=float('nan')),
                 dict(start='bad', end=3)]
        for row in cases:
            with self.subTest(row=row), self.assertRaises(engine.DialogueError):
                engine.validate_transcript([dict(row, text='测试')])
        with self.assertRaisesRegex(engine.DialogueError, '顺序'):
            engine.validate_transcript([dict(start=2, end=3, text='甲'), dict(start=1, end=2, text='乙')])

    def test_invalid_subtitle_time_rejected(self):
        with self.assertRaisesRegex(engine.DialogueError, '超出范围'):
            engine.import_transcript(self.write('bad.srt', '1\n00:65:00,000 --> 00:65:10,000\n你好'))

    def test_export_srt_preserves_bilingual_source_seconds_and_half_up(self):
        path = self.home / 'captions.srt'
        rows = [dict(start_seconds=1.0005, end_seconds=1.2345, text='“Wait—我们一起走。”\nI will wait here.'),
                dict(start_seconds=59.9995, end_seconds=61.0005, text='明天见。See you tomorrow!'),
                dict(start_seconds=3599.9995, end_seconds=3601, text='结束。')]
        result = engine.export_subtitles(rows, path)
        self.assertEqual(result, str(path))
        text = path.read_text(encoding='utf-8')
        self.assertIn('00:00:01,001 --> 00:00:01,235', text)
        self.assertIn('00:01:00,000 --> 00:01:01,001', text)
        self.assertIn('01:00:00,000 --> 01:00:01,000', text)
        imported = engine.import_transcript(path)
        self.assertEqual(imported[0]['text'], rows[0]['text'])
        self.assertEqual(imported[0]['start_seconds'], 1.001)

    def test_export_srt_refuses_existing_file_media_or_source_path(self):
        rows = [dict(start_seconds=0, end_seconds=1, text='你好')]
        existing = self.write('existing.srt', 'keep this unchanged')
        with self.assertRaisesRegex(engine.DialogueError, '已存在'):
            engine.export_subtitles(rows, existing)
        self.assertEqual(existing.read_text(), 'keep this unchanged')
        media = self.home / 'PGM.mov'
        media.write_bytes(b'original media bytes')
        with self.assertRaisesRegex(engine.DialogueError, '媒体'):
            engine.export_subtitles(rows, media)
        self.assertEqual(media.read_bytes(), b'original media bytes')
        nonexistent_source = self.home / 'input.srt'
        with self.assertRaisesRegex(engine.DialogueError, '输入素材'):
            engine.export_subtitles([dict(rows[0], media_path=str(nonexistent_source))], nonexistent_source)
        self.assertFalse(nonexistent_source.exists())

    def test_export_srt_rejects_invalid_or_collapsed_ranges_before_writing(self):
        for begin, end in ((-1, 1), (2, 1), (1, 1), (float('nan'), 2), (1.0001, 1.0002)):
            path = self.home / 'bad.srt'
            with self.subTest(begin=begin, end=end), self.assertRaises(engine.DialogueError):
                engine.export_subtitles([dict(start_seconds=begin, end_seconds=end, text='你好')], path)
            self.assertFalse(path.exists())
        with self.assertRaisesRegex(engine.DialogueError, '没有可导出'):
            engine.export_subtitles([], self.home / 'empty.srt')

    def test_notes_csv_encodings_delimiters_and_actual_fields(self):
        for encoding, delimiter in (('utf-8-sig', ','), ('gb18030', ';'), ('utf-8', '\t')):
            text = delimiter.join(['拍摄场次', '场号', '台词', '备注', '文件名', '开始秒', '结束秒', '日期', '机位', '条号']) + '\n'
            text += delimiter.join(['第一场拍摄', '第二幕', '明天见。See you tomorrow!', '这一条较好', '/nas/Day01/PGM001.mov', '12.125', '18.500', '2026-10-09', 'B', 'C003']) + '\n'
            with self.subTest(encoding=encoding, delimiter=delimiter):
                path = self.write('notes.csv', text, encoding)
                rows = engine.import_notes(path)
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row['session_name'], '第一场拍摄')
                self.assertEqual(row['scene'], '第二幕')
                self.assertEqual(row['dialogue'], '明天见。See you tomorrow!')
                self.assertEqual(row['media_path'], '/nas/Day01/PGM001.mov')
                self.assertEqual((row['start_seconds'], row['end_seconds']), (12.125, 18.5))
                self.assertEqual(row['time_basis'], 'source_local')
                self.assertEqual(row['date'], '2026-10-09')
                self.assertEqual(row['camera'], 'B')
                self.assertEqual(row['take'], 'C003')
                self.assertEqual(row['raw_fields']['备注'], '这一条较好')
                self.assertEqual(row['source_path'], str(path))
                json.dumps(rows, ensure_ascii=False)

    def test_notes_csv_quotes_newlines_frame_tc_and_unknown_columns(self):
        path = self.write('notes.csv', 'sep=;\nFile Name;Scene;IN;OUT;Remarks;Unknown\n"PGM_001.mov";"SCENE II";"21:43:22:13";"21:43:30:03";"导演说\nKeep this one";"x;y"\n')
        row = engine.import_notes(path)[0]
        self.assertEqual(row['media_path'], 'PGM_001.mov')
        self.assertEqual(row['notes'], '导演说\nKeep this one')
        self.assertEqual(row['start_timecode'], '21:43:22:13')
        self.assertEqual(row['end_timecode'], '21:43:30:03')
        self.assertEqual(row['time_basis'], 'unspecified')
        self.assertNotIn('start_seconds', row)
        self.assertEqual(row['raw_fields']['Unknown'], 'x;y')
        self.assertIn('Keep this one', row['rawtext'])

    def test_notes_csv_generic_in_out_need_explicit_time_basis(self):
        row = engine.import_notes(self.write('notes.csv', 'Scene,IN,OUT\nScene 1,00:00:12.125,00:00:20.5\n'))[0]
        self.assertEqual(row['start_seconds'], 12.125)
        self.assertEqual(row['time_basis'], 'unspecified')
        row = engine.import_notes(self.write('notes.csv', 'Scene,IN,OUT,time_basis\nScene 1,00:00:12.125,00:00:20.5,source_local\n'))[0]
        self.assertEqual(row['time_basis'], 'source_local')

    def test_notes_csv_invalid_ranges_and_duplicate_columns_rejected(self):
        for content in ('IN,OUT\n10,9\n', 'IN,OUT\n-1,10\n', 'Scene,Scene\none,two\n'):
            with self.subTest(content=content), self.assertRaises(engine.DialogueError):
                engine.import_notes(self.write('bad.csv', content))

    def test_notes_text_preserves_freeform_without_inventing_dialogue(self):
        text = '拍摄场次：第二次录制\n场景：第二幕\n文件名：/nas/Day02/PGM.mov\n开始秒：13.5\n结束秒：20\n台词：Wait，我们明天再见。\n备注：导演偏好这一条\n\n演员临场换词，此处需人工核对。'
        rows = engine.import_notes(self.write('notes.txt', text))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['scene'], '第二幕')
        self.assertEqual(rows[0]['dialogue'], 'Wait，我们明天再见。')
        self.assertEqual(rows[0]['time_basis'], 'source_local')
        self.assertEqual(rows[1]['rawtext'], '演员临场换词，此处需人工核对。')
        self.assertNotIn('dialogue', rows[1])
        self.assertNotIn('scene', rows[1])
        self.assertNotIn('session_name', rows[1])

    def test_notes_docx_uses_document_text_and_preserves_notes(self):
        path = self.home / 'notes.docx'
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>场号：第一场</w:t></w:r></w:p><w:p><w:r><w:t>备注：Keep this one.</w:t></w:r></w:p></w:body></w:document>'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        rows = engine.import_notes(path)
        self.assertEqual(rows[0]['scene'], '第一场')
        self.assertEqual(rows[0]['notes'], 'Keep this one.')

    def test_structured_notes_scope_only_real_media_and_never_create_transcripts(self):
        script = [dict(id='line', scene='SCENE II', text='I will wait for you at the station')]
        parts = [dict(id='correct', session_id='s1', session_name='First show', media_path='/nas/Day1/PGM.mov', start_seconds=10, end_seconds=14, text='I will wait for you near the station'),
                 dict(id='other', session_id='s2', session_name='Second show', media_path='/nas/Day2/PGM.mov', start_seconds=10, end_seconds=14, text='I will wait for you near the station')]
        note = dict(media_path='/nas/Day1/PGM.mov', scene='SCENE II', dialogue=script[0]['text'])
        values = engine.match_dialogue(script, parts, notes=[note])
        correct = next(row for row in values if row['transcript_id'] == 'correct')
        other = next(row for row in values if row['transcript_id'] == 'other')
        self.assertGreater(correct['score'], other['score'])
        self.assertIn('场记台词线索', correct['reason'])
        self.assertNotIn('confirmed', correct)
        self.assertEqual(engine.match_dialogue(script, [], notes=[note]), [])
        unrelated = [dict(parts[0], text='The children are playing in the garden')]
        self.assertEqual(engine.match_dialogue(script, unrelated, notes=[note]), [])

    def test_structured_notes_basename_ambiguous_times_and_session_ranges(self):
        item = dict(session_id='s1', session_name='First show', media_path='/nas/Day1/PGM.mov', start_seconds=10, end_seconds=14)
        self.assertFalse(engine._note_scope(dict(media_path='PGM.mov'), item))
        self.assertFalse(engine._note_scope(dict(session_id='s1'), item))
        self.assertFalse(engine._note_scope(dict(media_path=item['media_path'], start_seconds=10, end_seconds=14, time_basis='unspecified'), item))
        self.assertTrue(engine._note_scope(dict(session_id='s1', start_seconds=9, end_seconds=12, time_basis='source_local'), item))
        self.assertFalse(engine._note_scope(dict(session_id='s2', start_seconds=9, end_seconds=12, time_basis='source_local'), item))
        self.assertFalse(engine._note_scope(dict(session_name='First show', start_seconds=15, end_seconds=19, time_basis='source_local'), item))
        self.assertTrue(engine._note_scope(dict(media_path=item['media_path']), item))

    def test_normalize_digits_punctuation_and_english(self):
        self.assertEqual(engine.normalize_dialogue('（低声）二十点，我们见！'), engine.normalize_dialogue('20点我们见'))
        self.assertEqual(engine.normalize_dialogue('“WAIT—我们走。”'), engine.normalize_dialogue('wait 我们走'))
        self.assertNotEqual(engine.normalize_dialogue('he is'), engine.normalize_dialogue('heis'))
        self.assertEqual(engine.normalize_dialogue('二〇二六年'), engine.normalize_dialogue('2026年'))

    def test_traditional_simplified_match_preserves_original_subtitles(self):
        original = '你在偷聽我彈琴嗎？說到 tomorrow, I will wait here.'
        simplified = '你在偷听我弹琴吗？说到 tomorrow, I will wait here.'
        self.assertEqual(engine.normalize_dialogue(original), engine.normalize_dialogue(simplified))
        values = engine.match_dialogue([dict(id='line', text=simplified)], [dict(id='actual', session_id='s', text=original)])
        self.assertEqual(values[0]['transcript_id'], 'actual')
        self.assertEqual(values[0]['score'], 1.)
        path = self.home / 'traditional.srt'
        engine.export_subtitles([dict(start_seconds=1, end_seconds=3, text=original)], path)
        self.assertIn(original, path.read_text(encoding='utf-8'))
        self.assertEqual(engine.import_transcript(path)[0]['text'], original)

    def test_traditional_converter_missing_dependency_falls_back_without_translation(self):
        engine._traditional_converter.cache_clear()
        try:
            with patch.dict(sys.modules, {'opencc': None}):
                self.assertEqual(engine.normalize_dialogue('彈琴 tomorrow'), '彈 琴 tomorrow')
        finally:
            engine._traditional_converter.cache_clear()

    def test_edited_lines_and_unrelated_rejection(self):
        lines = [dict(id='l1', text='你明天早上还会来这里吗？')]
        parts = [dict(id='t1', session_id='s1', text='你明天早上还来这里吗'), dict(id='t2', session_id='s1', text='窗外下雨了，我去拿伞')]
        values = engine.match_dialogue(lines, parts)
        self.assertEqual([v['transcript_id'] for v in values], ['t1'])
        self.assertIn('改词', values[0]['reason'])

    def test_english_edits_keep_words_and_mixed_language(self):
        lines = [dict(id='e', text='I will wait for you here.'), dict(id='m', text='Wait，我们明天再见。')]
        parts = [dict(id='te', session_id='s1', text='I will wait for you right here'),
                 dict(id='tm', session_id='s2', text='Wait, 我们明天再见'),
                 dict(id='no', session_id='s1', text='ill water offers youth hearing')]
        values = engine.match_dialogue(lines, parts)
        self.assertIn(('e', 'te'), [(v['script_line_id'], v['transcript_id']) for v in values])
        self.assertIn(('m', 'tm'), [(v['script_line_id'], v['transcript_id']) for v in values])
        self.assertNotIn('no', [v['transcript_id'] for v in values])

    def test_english_containment_never_accepts_partial_word_prefix(self):
        script = engine.normalize_dialogue('I saw a rat')
        wrong = engine.normalize_dialogue('I saw a rate')
        self.assertLess(engine._similarity(script, wrong), .85)
        self.assertEqual(engine.match_dialogue([dict(id='line', text='I saw a rat')], [dict(id='wrong', session_id='s', text='I saw a rate')], threshold=.85), [])
        exact_phrase = engine.normalize_dialogue('Yesterday I saw a rat outside')
        self.assertGreater(engine._similarity(script, exact_phrase), .8)
        self.assertFalse(engine._contains_tokens(['act', 'ii'], ['act', 'iii']))

    def test_threshold_applies_after_all_ambiguity_penalties(self):
        line = dict(id='line', text='我明天一定会回来')
        repeated = [dict(id='one', session_id='s', text=line['text']), dict(id='two', session_id='s', text=line['text'])]
        self.assertEqual(engine.match_dialogue([line], repeated, threshold=.9), [])
        values = engine.match_dialogue([line], repeated, threshold=.8)
        self.assertEqual(len(values), 2)
        self.assertTrue(all(.8 <= row['score'] <= .84 and '人工确认' in row['reason'] for row in values))
        duplicate_script = [line, dict(line, id='another')]
        self.assertEqual(engine.match_dialogue(duplicate_script, repeated[:1], threshold=.9), [])
        separate_sessions = [dict(repeated[0], session_id='first'), dict(repeated[1], session_id='second')]
        values = engine.match_dialogue([line], separate_sessions, threshold=.9)
        self.assertEqual(len(values), 2)
        self.assertTrue(all(row['score'] >= .9 and 'confirmed' not in row for row in values))

    def test_top_k_per_session_not_global(self):
        lines = [dict(id='l', text='我明天一定会回来。')]
        parts = [dict(id=f'{session}-{n}', session_id=session, text='我明天一定会回来') for session in ('first', 'second', 'third') for n in range(6)]
        values = engine.match_dialogue(lines, parts, top_k=2)
        self.assertEqual(len(values), 6)
        self.assertTrue(all(v['score'] <= .84 and '相近候选' in v['reason'] for v in values))

    def test_repeated_script_is_ambiguous_not_auto_confirmed(self):
        lines = [dict(id='l1', text='你好吗'), dict(id='l2', text='你好吗')]
        values = engine.match_dialogue(lines, [dict(id='t1', session_id='s1', text='你好吗')])
        self.assertEqual(len(values), 2)
        self.assertTrue(all(v['score'] <= .84 and '重复台词' in v['reason'] for v in values))
        self.assertTrue(all('confirmed' not in v for v in values))

    def test_context_speaker_and_scene_assist(self):
        lines = [dict(id='l0', text='你去了哪里？'), dict(id='l1', scene='第二场', speaker='甲', text='我去找他了。')]
        parts = [dict(id='t0', session_id='s', media_path='x', start_seconds=0, text='你去了哪里'),
                 dict(id='t1', session_id='s', media_path='x', start_seconds=2, scene_note='第二场', speaker='甲', text='我去找他'),
                 dict(id='t2', session_id='s2', media_path='x', start_seconds=0, speaker='乙', text='我去找他')]
        values = engine.match_dialogue(lines, parts)
        first = next(v for v in values if v['script_line_id'] == 'l1' and v['transcript_id'] == 't1')
        second = next(v for v in values if v['script_line_id'] == 'l1' and v['transcript_id'] == 't2')
        self.assertGreater(first['score'], second['score'])
        self.assertIn('前后台词', first['reason'])
        self.assertIn('场记', first['reason'])

    def test_matching_can_cancel(self):
        with self.assertRaisesRegex(engine.DialogueError, '取消'):
            engine.match_dialogue([dict(text='你好')], [], cancelled=lambda: True)

    def test_long_edited_script_matches_split_asr_without_fake_ids(self):
        lines = [dict(id='line', text='你明天早上还会到这里来吗？我会在车站等你，我们一起回家。')]
        parts = [dict(id='a', media_path='x', session_id='one', start_seconds=10.125, end_seconds=14, text='你明天早上还到这儿来吗'),
                 dict(id='b', media_path='x', session_id='one', start_seconds=14.1, end_seconds=17, text='我会在车站等你'),
                 dict(id='c', media_path='x', session_id='one', start_seconds=17.1, end_seconds=20, text='我们一起回家')]
        before = json.dumps(parts, ensure_ascii=False)
        values = engine.match_dialogue(lines, parts, threshold=.8)
        self.assertEqual({v['transcript_id'] for v in values}, {'a', 'b', 'c'})
        self.assertTrue(all('联合匹配' in v['reason'] for v in values))
        self.assertEqual(json.dumps(parts, ensure_ascii=False), before)

    def test_long_script_split_english_and_chinese(self):
        lines = [dict(id='line', text='I will wait for you at the station. 我们明天一起回家，不要再迟到了。')]
        parts = [dict(id='a', media_path='x', session_id='one', start_seconds=0, end_seconds=3, text='I will wait for you at the train station'),
                 dict(id='b', media_path='x', session_id='one', start_seconds=3.1, end_seconds=6, text='我们明天一起回家'),
                 dict(id='c', media_path='x', session_id='one', start_seconds=6.1, end_seconds=8, text='不要再迟到了')]
        values = engine.match_dialogue(lines, parts, threshold=.8)
        self.assertEqual({v['transcript_id'] for v in values}, {'a', 'b', 'c'})

    def test_joint_windows_never_cross_media_session_or_long_gap(self):
        lines = [dict(id='line', text='你明天早上还会到这里来吗我会在车站等你我们一起回家')]
        pieces = ['你明天早上还到这儿来吗', '我会在车站等你', '我们一起回家']
        for boundary in ('media_path', 'session_id', 'gap', 'speaker'):
            parts = [dict(id=str(i), media_path='x', session_id='one', speaker='甲', start_seconds=i * 3, end_seconds=i * 3 + 2, text=text) for i, text in enumerate(pieces)]
            for i, part in enumerate(parts):
                if boundary in ('media_path', 'session_id', 'speaker'):
                    part[boundary] = str(i)
                else:
                    part['start_seconds'] = i * 100
                    part['end_seconds'] = i * 100 + 2
            with self.subTest(boundary=boundary):
                self.assertEqual(engine.match_dialogue(lines, parts, threshold=.9), [])

    def test_joint_matching_preserves_repeated_performances_and_unrelated_neighbour(self):
        lines = [dict(id='line', text='你明天早上还会到这里来吗我会在车站等你我们一起回家')]
        pieces = ['你明天早上还到这儿来吗', '我会在车站等你', '我们一起回家']
        parts = []
        for take in range(2):
            for i, text in enumerate(pieces):
                parts.append(dict(id=f'{take}-{i}', media_path='x', session_id='one', start_seconds=take * 100 + i * 3, end_seconds=take * 100 + i * 3 + 2, text=text))
        parts.append(dict(id='unrelated', media_path='x', session_id='one', start_seconds=108.1, end_seconds=110, text='窗外的飞机刚刚起飞'))
        values = engine.match_dialogue(lines, parts, threshold=.8, top_k=10)
        self.assertEqual({v['transcript_id'] for v in values}, {f'{take}-{i}' for take in range(2) for i in range(3)})

    def test_model_prefers_complete_direct_then_snapshot_cache(self):
        cache = self.home / 'models'
        def model_dir(path):
            path.mkdir(parents=True)
            for name in ('model.bin', 'config.json', 'tokenizer.json'):
                (path / name).write_bytes(b'x')
            return path
        snapshot = model_dir(cache / 'models--Systran--faster-whisper-small' / 'snapshots' / 'abcdef')
        self.assertEqual(engine.resolve_model_path('small', cache), str(snapshot.resolve()))
        direct = model_dir(cache / 'small')
        self.assertEqual(engine.resolve_model_path('small', cache), str(direct.resolve()))
        explicit = model_dir(self.home / 'explicit')
        self.assertEqual(engine.resolve_model_path(explicit, cache), str(explicit.resolve()))
        self.assertEqual(engine.resolve_model_path('tiny', cache), 'tiny')
        (explicit / 'tokenizer.json').unlink()
        with self.assertRaisesRegex(engine.DialogueError, 'tokenizer.json'):
            engine.resolve_model_path(explicit, cache)

    def test_hf_defaults_are_inside_application_root(self):
        with patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.home)}, clear=True):
            cache = engine.model_cache_dir()
            values = engine.configure_hf_cache(cache)
        self.assertEqual(cache, self.home / 'models')
        self.assertEqual(values['HF_HOME'], str(self.home / 'huggingface'))
        self.assertEqual(values['HF_HUB_CACHE'], str(self.home / 'models'))
        self.assertEqual(values['HF_XET_CACHE'], str(self.home / 'huggingface' / 'xet'))

    def test_hf_explicit_user_environment_is_never_overridden(self):
        preferences = {'HF_HOME': '/user/chosen/hf', 'HF_HUB_CACHE': '/user/chosen/hub', 'HF_XET_CACHE': '/user/chosen/xet'}
        with patch.dict(os.environ, preferences, clear=True):
            self.assertEqual(engine.configure_hf_cache(self.home / 'models'), preferences)
        with patch.dict(os.environ, {'HF_HOME': '/user/chosen/hf'}, clear=True):
            values = engine.configure_hf_cache(self.home / 'models')
            self.assertEqual(values['HF_HOME'], '/user/chosen/hf')
            self.assertEqual(values['HF_HUB_CACHE'], str(self.home / 'models'))

    def test_hf_cache_is_configured_before_faster_whisper_import(self):
        import builtins
        original = builtins.__import__
        observed = []
        def intercept(name, *args, **kwargs):
            if name == 'faster_whisper':
                observed.append({key: os.environ.get(key) for key in ('HF_HOME', 'HF_HUB_CACHE', 'HF_XET_CACHE')})
                raise ImportError('optional dependency intentionally absent')
            return original(name, *args, **kwargs)
        path = self.make_wav()
        with patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.home)}, clear=True), patch.object(builtins, '__import__', intercept):
            with self.assertRaisesRegex(engine.DialogueError, 'faster-whisper'):
                engine.transcribe_media(path)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['HF_HUB_CACHE'], str(self.home / 'models'))
        self.assertTrue(all(str(self.home) in value for value in observed[0].values()))

    def make_wav(self, seconds=4):
        path = self.home / 'reference.wav'
        with wave.open(str(path), 'wb') as target:
            target.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            chunk = struct.pack('<h', 1200) * 48000
            for _ in range(seconds):
                target.writeframesraw(chunk)
        return path

    def test_real_pyav_extract_trim_length_and_no_extra_gap(self):
        path = self.make_wav()
        out = self.home / 'trim.wav'
        length = engine._extract_audio(path, out, 1.125, 1.25, lambda *a: None, lambda: False)
        with wave.open(str(out), 'rb') as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnframes(), 20000)
            self.assertGreater(struct.unpack('<h', audio.readframes(1))[0], 1000)
        self.assertEqual(length, 1.25)

    def test_audio_extract_progress_only_emits_integer_changes(self):
        updates = []
        engine._extract_audio(self.make_wav(), self.home / 'out.wav', 0, None, lambda n, message: updates.append(n), lambda: False)
        self.assertLessEqual(len(updates), 16)
        self.assertEqual(updates, sorted(set(updates)))
        self.assertEqual(updates[-1], 15)

    def test_asr_uses_optional_model_cache_auto_and_source_seconds(self):
        calls = []
        class Model:
            def __init__(self, *args, **kwargs):
                calls.append(('model', args, kwargs))
            def transcribe(self, path, **kwargs):
                calls.append(('transcribe', kwargs, path))
                return iter([SimpleNamespace(start=.041, end=.8, text=' Hello，世界 ')]), None
        path = self.make_wav()
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}), patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.home)}):
            original_open = wave.open
            def direct_pcm_only(name, mode='rb'):
                self.assertNotEqual(Path(name).name, 'chunk.wav')
                return original_open(name, mode)
            with patch.object(engine.wave, 'open', side_effect=direct_pcm_only):
                rows = engine.transcribe_media(path, start_seconds=1.125, duration=1.25)
        self.assertAlmostEqual(rows[0]['start_seconds'], 1.166)
        self.assertAlmostEqual(rows[0]['end_seconds'], 1.925)
        self.assertEqual(rows[0]['text'], 'Hello，世界')
        self.assertIsNone(calls[1][1]['language'])
        self.assertTrue(calls[1][1]['multilingual'])
        self.assertEqual(calls[1][1]['task'], 'transcribe')
        self.assertEqual(calls[0][2]['download_root'], str(self.home / 'models'))
        samples = calls[1][2]
        self.assertIsInstance(samples, numpy.ndarray)
        self.assertEqual(samples.dtype, numpy.float32)
        self.assertEqual(samples.ndim, 1)
        self.assertEqual(len(samples), 20000)
        self.assertTrue(numpy.allclose(samples[100:-100], 1200 / 32768, atol=1 / 32768))
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}), patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.home)}):
            engine.transcribe_media(path, language='en')
        self.assertFalse(calls[-1][1]['multilingual'])
        self.assertEqual(calls[-1][1]['language'], 'en')
        self.assertEqual(sum(call[0] == 'model' for call in calls), 1,
                         'Multiple clips/language choices should reuse the loaded model')

    def test_asr_download_failure_not_empty_success(self):
        def fail(*args, **kwargs):
            raise RuntimeError('offline')
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=fail)}), patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.home)}):
            with self.assertRaisesRegex(engine.DialogueError, '下载失败'):
                engine.transcribe_media(self.make_wav())

    def test_asr_cancellation_and_missing_dependency(self):
        with self.assertRaisesRegex(engine.DialogueError, '取消'):
            engine.transcribe_media(self.home / 'absent.wav', cancelled=lambda: True)
        with patch.dict(sys.modules, {'faster_whisper': None}):
            with self.assertRaisesRegex(engine.DialogueError, 'faster-whisper'):
                engine.transcribe_media(self.make_wav())

    def test_asr_cancels_between_segments(self):
        flag = [False]
        class Model:
            def __init__(self, *a, **kw):
                pass
            def transcribe(self, path, **kw):
                def segments():
                    yield SimpleNamespace(start=0, end=.5, text='hello')
                    flag[0] = True
                    yield SimpleNamespace(start=.5, end=1, text='world')
                return segments(), None
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}), patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.home)}):
            with self.assertRaisesRegex(engine.DialogueError, '取消'):
                engine.transcribe_media(self.make_wav(), cancelled=lambda: flag[0])

    def test_long_auto_asr_is_bounded_and_overlap_deduplicated(self):
        lengths = []
        class Model:
            def __init__(self, *a, **kw):
                pass
            def transcribe(self, path, **kw):
                self_array = path
                if not isinstance(self_array, numpy.ndarray) or self_array.dtype != numpy.float32 or self_array.ndim != 1:
                    raise AssertionError('ASR must receive decoded mono float32 samples')
                lengths.append(len(self_array) / 16000)
                n = len(lengths)
                if n == 1:
                    rows = [SimpleNamespace(start=58, end=59.5, text='我们在这里等你')]
                elif n == 2:
                    rows = [SimpleNamespace(start=0, end=1.5, text='我们在这里等你'),
                            SimpleNamespace(start=3, end=4, text='Wait here')]
                else:
                    rows = [SimpleNamespace(start=2, end=3, text='谢谢')]
                return iter(rows), None
        def extract(path, target, *args):
            with wave.open(str(target), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                for _ in range(121):
                    audio.writeframesraw(b'\0\0' * 16000)
            return 121.
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}), patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.home)}), patch.object(engine, '_extract_audio', extract):
            rows = engine.transcribe_media(self.make_wav(), start_seconds=100)
        self.assertEqual(lengths, [60., 62., 3.])
        self.assertEqual([v['text'] for v in rows], ['我们在这里等你', 'Wait here', '谢谢'])
        self.assertEqual(rows[1]['start_seconds'], 161)
        self.assertEqual(rows[-1]['end_seconds'], 221)


if __name__ == '__main__':
    unittest.main()
