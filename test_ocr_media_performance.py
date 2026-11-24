"""Exact-frame, bounded-resource and 26-camera regressions for OCR workers."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import av
import numpy as np

from core import AnalysisError, probe, read_frame
from media_cache import (FrameReadSession, MetadataCache, current_frame_session,
                         frame_read_session, video_metadata_cache)
from ocr import LabelMatcher, LabelReader, match_label


def make_video(path, count=84, offset=0):
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('libx264', rate=25)
        stream.width, stream.height, stream.pix_fmt = 64, 48, 'yuv420p'
        stream.options = {'preset':'fast', 'g':'48', 'bf':'3', 'sc_threshold':'0'}
        for index in range(count):
            pixels = np.empty((48, 64, 3), np.uint8)
            pixels[:] = ((index*3+offset)%255, (index*7+offset)%255, (index*11+offset)%255)
            frame = av.VideoFrame.from_ndarray(pixels, format='bgr24')
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


class MediaSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.path = cls.directory/'gop.mov'
        make_video(cls.path)
        with av.open(str(cls.path)) as container:
            cls.decoded = [frame.to_ndarray(format='bgr24') for frame in container.decode(video=0)]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_cfr_bframes_random_and_sequential_reads_are_byte_exact(self):
        with frame_read_session() as session:
            for index in (83,0,47,48,50,1,24,70,0,2,5,10,12,15,82):
                np.testing.assert_array_equal(read_frame(self.path,index), self.decoded[index])
            self.assertGreater(session.stats['seeks'], 0)
            self.assertEqual(session.stats['opens'], 1)
            self.assertLessEqual(session.stats['peak_cached_bytes'], session.max_cache_bytes)
        self.assertTrue(session.closed)
        self.assertFalse(session.decoders)
        self.assertFalse(session.frames)
        self.assertIsNone(current_frame_session())

    def test_three_exact_samples_share_decoder_and_gop(self):
        with frame_read_session() as session:
            for start in range(0,80,10):
                for offset in (0,2,5):
                    np.testing.assert_array_equal(read_frame(self.path,start+offset),self.decoded[start+offset])
            self.assertEqual(session.stats['opens'],1)
            self.assertEqual(session.stats['seeks'],0)
            self.assertEqual(session.stats['decoded_frames'],76)

    def test_distant_samples_seek_instead_of_decoding_whole_gap(self):
        with FrameReadSession() as session:
            session.read(self.path,0)
            session.read(self.path,83)
            self.assertEqual(session.stats['seeks'],1)
            self.assertLess(session.stats['decoded_frames'],50)

    def test_cache_arrays_are_detached_and_byte_budget_is_hard(self):
        budget=64*48*3*2
        with FrameReadSession(max_cache_bytes=budget) as session:
            result=session.read(self.path,0);result[:]=255
            np.testing.assert_array_equal(session.read(self.path,0),self.decoded[0])
            for index in range(15):
                session.read(self.path,index)
                self.assertLessEqual(session.cached_bytes,budget)
            self.assertLessEqual(len(session.frames),2)
            self.assertGreater(session.stats['cache_hits'],0)

    def test_all_decoders_close_on_lru_and_exception(self):
        from media_cache import _Decoder
        original=_Decoder.close;closed=[]
        def close(decoder):
            closed.append(decoder);original(decoder)
        with tempfile.TemporaryDirectory() as directory, patch.object(_Decoder,'close',close):
            with self.assertRaisesRegex(RuntimeError,'test error'):
                with FrameReadSession(max_open=2) as session:
                    for number in range(5):
                        path=Path(directory)/f'{number}.mov';shutil.copyfile(self.path,path)
                        session.read(path,number)
                        self.assertLessEqual(len(session.decoders),2)
                    raise RuntimeError('test error')
            self.assertEqual(len(closed),5)
            self.assertEqual(session.stats['max_open'],2)
            self.assertFalse(session.decoders)

    def test_cancellation_stops_between_decoded_frames_and_closes_resources(self):
        checks=[]
        def cancelled():
            checks.append(1)
            return len(checks)>7
        with self.assertRaisesRegex(AnalysisError,'取消'):
            with FrameReadSession(cancelled=cancelled) as session:
                session.read(self.path,40)
        self.assertTrue(session.closed)
        self.assertLess(session.stats['decoded_frames'],8)

    def test_session_is_worker_owned_and_nested_context_restores_owner(self):
        with frame_read_session() as outer:
            with frame_read_session() as inner:
                self.assertIs(current_frame_session(),inner)
            self.assertIs(current_frame_session(),outer)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaisesRegex(RuntimeError,'跨线程'):
                    pool.submit(outer.read,self.path,0).result()

    def test_replaced_file_invalidates_cached_frame_and_open_decoder(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'replace.mov';shutil.copyfile(self.path,path)
            with FrameReadSession() as session:
                first=session.read(path,0)
                make_video(path,count=20,offset=100)
                second=session.read(path,0)
                self.assertFalse(np.array_equal(first,second))
                self.assertEqual(session.stats['opens'],2)
                self.assertEqual(len(session.decoders),1)
                self.assertEqual(len(session.frames),1)

    def test_probe_cache_is_stat_aware_and_returns_deep_copies(self):
        video_metadata_cache.clear()
        original=av.open
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'probe.mov';shutil.copyfile(self.path,path)
            with patch('core.av.open',wraps=original) as opened:
                first=probe(path);first['frames']=-1;first['audio_channels'].append({'source_channel':99})
                second=probe(path)
                self.assertEqual(second['frames'],84);self.assertEqual(second['audio_channels'],[])
                self.assertEqual(opened.call_count,1)
            make_video(path,count=20)
            self.assertEqual(probe(path)['frames'],20)
            path.unlink()
            with self.assertRaisesRegex(AnalysisError,'找不到'):
                probe(path)

    def test_metadata_lru_is_bounded_and_discards_same_path_old_versions(self):
        cache=MetadataCache(limit=3)
        for index in range(20):
            cache.get((str(index),index),lambda:{'values':[1]})
            self.assertLessEqual(len(cache.values),3)
        cache.get(('19',99),lambda:{'values':[2]})
        self.assertNotIn(('19',19),cache.values)

    def test_probe_does_not_cache_a_file_replaced_during_metadata_read(self):
        import core
        video_metadata_cache.clear()
        original=core._probe_uncached
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'changing.mov';shutil.copyfile(self.path,path)
            def changed(value):
                result=original(value)
                make_video(path,count=20,offset=100)
                return result
            with patch('core._probe_uncached',side_effect=changed):
                with self.assertRaisesRegex(AnalysisError,'复制完成'):
                    probe(path)
            self.assertFalse(video_metadata_cache.values)
            self.assertEqual(probe(path)['frames'],20)


class OCRBoundedCacheTests(unittest.TestCase):
    cameras='ABCDEFGHIJKLMNOPQRSTUVWXYZ'

    def config(self, **extra):
        return dict(camera_count=26,label_match_mode='prefix',infer_a_when_blank=False,
                    labels={camera:camera for camera in self.cameras},**extra)

    def test_26_shared_rois_use_one_ocr_even_at_old_refresh_boundaries(self):
        class OCR:
            calls=0
            def recognize(self,rgb):
                self.calls+=1;return [dict(text='Z003C001',confidence=1)]
        backend=OCR();reader=LabelReader(self.config(),backend)
        rois={camera:[0,0,1,1] for camera in self.cameras}
        pixels=np.zeros((48,64,3),np.uint8)
        for index in (0,2,5,25,50,75,100):
            self.assertEqual(reader.inspect(pixels,rois,index)[0],'Z')
        self.assertEqual(backend.calls,1)
        self.assertEqual(reader.hits,6)
        self.assertEqual(len(reader.box_cache),1)

    def test_exact_crop_changes_are_not_similarity_matched(self):
        class OCR:
            def recognize(self,rgb):
                return [dict(text='B003C001' if rgb[0,0,0]==0 else 'Z003C001',confidence=1)]
        reader=LabelReader(self.config(),OCR());rois={camera:[0,0,1,1] for camera in self.cameras}
        pixels=np.zeros((48,64,3),np.uint8)
        self.assertEqual(reader.inspect(pixels,rois,1)[0],'B')
        pixels[0,0,2]=1
        self.assertEqual(reader.inspect(pixels,rois,2)[0],'Z')
        self.assertEqual(reader.calls,2)

    def test_ocr_lru_is_byte_bounded_even_with_large_user_roi(self):
        class OCR:
            def recognize(self,rgb):return []
        reader=LabelReader(self.config(ocr_cache_bytes=64*48*3*2),OCR())
        rois={camera:[0,0,1,1] for camera in self.cameras}
        for index in range(30):
            reader.inspect(np.full((48,64,3),index,np.uint8),rois,index)
            self.assertLessEqual(reader.cache_bytes,reader.cache_limit)
            self.assertLessEqual(len(reader.cache),2)
        # A crop larger than the budget is recognized but never retained.
        reader.inspect(np.zeros((100,200,3),np.uint8),rois,100)
        self.assertLessEqual(reader.cache_bytes,reader.cache_limit)

    def test_compiled_modes_keep_custom_rule_and_ambiguity_semantics(self):
        labels={'A':'A','B':'B','C':'440','Z':'Z'}
        for mode in ('prefix','exact','legacy'):
            matcher=LabelMatcher(labels,False,mode)
            for text in ('B003C001','440_0001','1440','Z003C999','UNKNOWN','A',''):
                self.assertEqual(matcher.match(text),match_label(text,labels,False,mode))
        self.assertIsNone(LabelMatcher({'B':'B','C':'B00'},match_mode='prefix').match('B003C001'))
        matcher=LabelMatcher(labels,match_mode='prefix')
        for index in range(500):matcher.match(f'B{index:04}')
        self.assertLessEqual(len(matcher.cache),256)


class AnalysisCancellationTests(unittest.TestCase):
    def test_cancel_during_final_ocr_never_commits_completed_analysis(self):
        from cut_xml import analyze_cut_xml
        cancelled=[False]
        class OCR:
            def recognize(self,rgb):
                cancelled[0]=True
                return [dict(text='B003C001',confidence=1)]
        block=dict(path='test.mov',start=0,end=1,frames=1,width=64,height=48,start_tc='01:00:00:00')
        cut=dict(fps=25,duration=1,xml_digest='fixture',pgm_info=block,pgm_sources=[block],
                 shots=[dict(start=0,end=1,pgm_path='test.mov',source_in=0,source_out=1)])
        config=dict(camera_ids=['B'],fps=25,label_match_mode='prefix',labels={'B':'B'},
                    label_rois={'B':[0,0,1,1]})
        with tempfile.TemporaryDirectory() as directory, patch('cut_xml.timeline_info',return_value=cut), \
             patch('cut_xml.read_frame',return_value=np.zeros((48,64,3),np.uint8)):
            with self.assertRaisesRegex(AnalysisError,'取消'):
                analyze_cut_xml(config,directory,cancelled=lambda:cancelled[0],backend=OCR())
            self.assertFalse((Path(directory)/'analysis.json').exists())


if __name__=='__main__':unittest.main()
