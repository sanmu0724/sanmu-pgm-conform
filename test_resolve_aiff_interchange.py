"""Editor-added PCM AIFF survives the original-audio import bridge unchanged."""
import struct
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import aaf2
from resolve_audio_interchange import _actual_pcm_depth, write_audio_interchange
from test_resolve_audio_interchange import fixture


def aiff(path, depth=16, compression=None):
    comm=struct.pack('>HIH',1,384000,depth)+bytes.fromhex('400ebb80000000000000')
    if compression is not None:comm+=compression+b'\0\0'
    body=(b'AIFC' if compression is not None else b'AIFF')+b'COMM'+struct.pack('>I',len(comm))+comm
    path.write_bytes(b'FORM'+struct.pack('>I',len(body))+body)


class AiffInterchangeTests(unittest.TestCase):
    def test_declared_pcm_header_depth_without_guessing(self):
        with tempfile.TemporaryDirectory() as folder:
            for depth in (16,24,32):
                path=Path(folder)/f'known{depth}.aif';aiff(path,depth)
                self.assertEqual(_actual_pcm_depth(path),depth)
            for code in (b'NONE',b'twos',b'sowt'):
                path=Path(folder)/'uncompressed.aifc';aiff(path,24,code)
                self.assertEqual(_actual_pcm_depth(path),24)

    def test_compressed_or_truncated_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'unsupported.aifc';aiff(path,16,b'ima4')
            with self.assertRaisesRegex(ValueError,'Compressed'): _actual_pcm_depth(path)
            path.write_bytes(b'FORM\0\0\0\x20AIFFCOMM\0\0\0\x12\0')
            with self.assertRaisesRegex(ValueError,'COMM'): _actual_pcm_depth(path)

    def test_bridge_keeps_original_aiff_locator_and_sample_in(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);path,manifest,previous,_=fixture(folder,depth=None)
            original=folder/'editor_music.aif';aiff(original,24);before=original.read_bytes()
            tree=ET.parse(path);asset=tree.find('./resources/asset');asset.set('src',original.as_uri())
            asset.set('name',original.name);tree.write(path)
            for group in manifest['groups']:
                for row in group['audio']:row['path']=str(original)
            result=write_audio_interchange(path,manifest)
            self.assertEqual(original.read_bytes(),before)
            with aaf2.open(result['aaf_path']) as file:
                source=next(file.content.sourcemobs())
                self.assertEqual(source.descriptor['QuantizationBits'].value,24)
                self.assertTrue(source.descriptor['Locator'][0]['URLString'].value.endswith('editor_music.aif'))
                comp=next(file.content.compositionmobs())
                self.assertEqual(comp.slot_at(2).segment.components[1].start,7)
