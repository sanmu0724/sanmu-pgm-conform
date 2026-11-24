import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from core import AnalysisError
from cut_xml import read_cut_xml, read_cut_xmls, timeline_info


def make_cut_xml(path, media, frames=100, fps=25, tc='12:00:00:00', cuts=None):
    root = ET.Element('xmeml', version='5')
    sequence = ET.SubElement(root, 'sequence')
    ET.SubElement(sequence, 'duration').text = str(frames)
    def rate(parent):
        node = ET.SubElement(parent, 'rate')
        ET.SubElement(node, 'timebase').text = str(fps)
        ET.SubElement(node, 'ntsc').text = 'FALSE'
    rate(sequence)
    ET.SubElement(ET.SubElement(sequence, 'timecode'), 'string').text = '01:00:00:00'
    track = ET.SubElement(ET.SubElement(ET.SubElement(sequence, 'media'), 'video'), 'track')
    boundaries = [0] + list(cuts or []) + [frames]
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        clip = ET.SubElement(track, 'clipitem', id=f'clip-{index}')
        rate(clip)
        for key, value in dict(start=start, end=end, **{'in': start, 'out': end}).items():
            ET.SubElement(clip, key).text = str(value)
        source = ET.SubElement(clip, 'file', id='source')
        if index == 0:
            ET.SubElement(source, 'pathurl').text = Path(media).as_uri()
            rate(source)
            ET.SubElement(ET.SubElement(source, 'timecode'), 'string').text = tc
    ET.ElementTree(root).write(path)
    return path


class CutXmlBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.media = [self.root / '001.mov', self.root / '002.mov']
        self.infos = {}
        self.xmls = []
        for index, media in enumerate(self.media):
            media.write_bytes(b'media')
            tc = f'{12+index:02d}:00:00:00'
            self.infos[str(media)] = dict(path=str(media), fps='25', frames=100, start_tc=tc, width=1920, height=1080)
            self.xmls.append(make_cut_xml(self.root / f'{index}.xml', media, tc=tc, cuts=[30, 65]))
        self.probe = patch('cut_xml.probe', side_effect=lambda path: copy.deepcopy(self.infos[str(path)]))
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        self.temp.cleanup()

    def test_multiple_timelines_keep_file_order_and_source_in_out(self):
        cut = read_cut_xmls(self.xmls)
        self.assertEqual(cut['duration'], 200)
        self.assertEqual([(row['start'], row['end']) for row in cut['pgm_sources']], [(0, 100), (100, 200)])
        self.assertEqual([(row['start'], row['end'], row['source_in'], row['source_out']) for row in cut['shots']],
                         [(0, 30, 0, 30), (30, 65, 30, 65), (65, 100, 65, 100),
                          (100, 130, 0, 30), (130, 165, 30, 65), (165, 200, 65, 100)])
        self.assertEqual(cut['xml_paths'], list(map(str, self.xmls)))

    def test_single_timeline_keeps_legacy_digest(self):
        self.assertEqual(read_cut_xml(self.xmls[0])['xml_digest'], read_cut_xmls(self.xmls[:1])['xml_digest'])

    def test_digest_tracks_xml_order_and_contents(self):
        original = read_cut_xmls(self.xmls)['xml_digest']
        self.assertNotEqual(original, read_cut_xmls(list(reversed(self.xmls)))['xml_digest'])
        tree = ET.parse(self.xmls[1])
        ET.SubElement(tree.getroot(), 'annotation').text = 'changed'
        tree.write(self.xmls[1])
        self.assertNotEqual(original, read_cut_xmls(self.xmls)['xml_digest'])

    def test_duplicate_xml_and_repeated_pgm_in_different_xml_rejected(self):
        with self.assertRaisesRegex(AnalysisError, '重复导入'):
            read_cut_xmls([self.xmls[0], self.xmls[0]])
        copied = self.root / 'copy.xml'
        copied.write_bytes(self.xmls[0].read_bytes())
        with self.assertRaisesRegex(AnalysisError, 'PGM.*重复'):
            read_cut_xmls([self.xmls[0], copied])

    def test_two_xml_paths_resolving_to_same_pgm_rejected(self):
        elsewhere = self.root / 'moved.mov'
        self.infos[str(elsewhere)] = dict(self.infos[str(self.media[0])], path=str(elsewhere))
        make_cut_xml(self.xmls[1], self.media[1], tc='12:00:00:00')
        with self.assertRaisesRegex(AnalysisError, 'PGM.*重复'):
            read_cut_xmls(self.xmls, pgm_overrides={str(self.media[0]): str(elsewhere), str(self.media[1]): str(elsewhere)})

    def test_mixed_xml_rates_rejected(self):
        self.infos[str(self.media[1])]['fps'] = '24'
        make_cut_xml(self.xmls[1], self.media[1], fps=24, tc='13:00:00:00')
        with self.assertRaisesRegex(AnalysisError, '帧率必须一致'):
            read_cut_xmls(self.xmls)

    def test_timeline_info_uses_list_and_preserves_large_source_clock_gap(self):
        cut = timeline_info({'cut_xml': '/ignored.xml', 'cut_xml_paths': list(map(str, self.xmls)), 'fps': 25})
        self.assertEqual(cut['duration'], 200)
        self.assertEqual(cut['pgm_sources'][1]['start_tc'], '13:00:00:00')
        self.assertEqual(cut['pgm_sources'][1]['start'], 100)

    def test_reversed_recording_clocks_rejected_without_reordering(self):
        with self.assertRaisesRegex(AnalysisError, '倒退'):
            timeline_info({'cut_xml_paths': list(reversed(self.xmls)), 'fps': 25})

    def test_incomplete_source_coverage_cannot_be_hidden_by_next_xml(self):
        tree = ET.parse(self.xmls[0])
        last = tree.findall('./sequence/media/video/track/clipitem')[-1]
        for key in ('end', 'out'):
            last.find(key).text = '99'
        tree.find('./sequence/duration').text = '99'
        tree.write(self.xmls[0])
        with self.assertRaisesRegex(AnalysisError, '没有覆盖完整PGM'):
            read_cut_xmls(self.xmls)

    def test_empty_list_rejected(self):
        with self.assertRaisesRegex(AnalysisError, '导入'):
            read_cut_xmls([])


if __name__ == '__main__':
    unittest.main()
