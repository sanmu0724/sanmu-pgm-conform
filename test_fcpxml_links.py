"""Native Resolve FCPXML link rebuilding without reading or rewriting media."""
import copy
import json
import tempfile
import unittest
from fractions import Fraction as F
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fcpxml_conform import conform_fcpxml, normalize_native_audio_links


def seconds(value):
    return F(value.removesuffix('s'))


def ts(value):
    value = F(value)
    return f'{value.numerator}/{value.denominator}s'


def fixture():
    root = ET.fromstring('''<fcpxml version="1.8">
      <resources>
        <format id="fmt" frameDuration="1/25s" width="1920" height="1080"/>
        <asset id="proxy" src="file:///tmp/proxy.mov" name="proxy.mov" start="100s" duration="20s" format="fmt" hasVideo="1"/>
        <asset id="wav-1" src="file:///tmp/original-main.wav" name="original-main.wav" start="600s" duration="20s" hasAudio="1" audioChannels="1"/>
        <asset id="wav-2" src="file:///tmp/next-main.wav" name="next-main.wav" start="700s" duration="20s" hasAudio="1" audioChannels="1"/>
        <media id="mc"><multicam format="fmt" tcStart="0s" duration="200s"><mc-angle angleID="A">
          <clip offset="100s" start="100s" duration="20s"><video ref="proxy" offset="100s" start="100s" duration="20s"/></clip>
        </mc-angle></multicam></media>
      </resources>
      <library><event><project><sequence format="fmt" tcStart="3600s" duration="20s"><spine>
        <gap name="REVIEW_UNKNOWN" offset="3600s" start="3600s" duration="5s"><marker start="3600s" duration="5s" value="REVIEW_UNKNOWN"/></gap>
        <mc-clip ref="mc" name="ABCD" offset="3605s" start="100s" duration="4s">
          <mc-source angleID="A" srcEnable="video"><adjust-transform position="0 0" scale="1.2 1.2"/></mc-source>
          <marker start="101s" duration="1/25s" value="KEEP"/>
        </mc-clip>
      </spine></sequence></project></event></library>
    </fcpxml>''')
    return root, root.find('./library/event/project/sequence/spine'), root.find('.//project/sequence/spine/mc-clip')


def add_sound(mc, ref='wav-1', record=100, source=600, duration=4, asset_start=600, asset_duration=20, lane='1'):
    # Native Resolve represents a trimmed clip around the whole WAV component.
    child = ET.Element('clip', name=ref + '.wav', lane=lane, start=ts(source), offset=ts(record), duration=ts(duration), enabled='1')
    audio = ET.SubElement(child, 'audio', ref=ref, srcCh='1', start=ts(asset_start), offset=ts(asset_start), duration=ts(asset_duration))
    mc.insert(list(mc).index(mc.find('marker')), child)
    return child, audio


class NativeAudioLinkTests(unittest.TestCase):
    def test_source_in_and_record_position_use_nested_audio_coordinates(self):
        root, spine, mc = fixture()
        child, audio = add_sound(mc, record=F(401, 4), source=F(1201, 2), duration=2)
        # audio offset and source IN deliberately differ.
        audio.set('start', '610s')
        original_mc_source = ET.tostring(mc.find('mc-source'))
        original_marker = ET.tostring(mc.find('marker'))
        original_assets = ET.tostring(root.find('resources'))
        report = normalize_native_audio_links(root)
        self.assertEqual((report['normalized_multicam_clips'], report['normalized_audio_components']), (1, 1))
        wrapper = spine.find('clip'); result = wrapper.find('audio'); inner = wrapper.find('mc-clip')
        self.assertEqual((seconds(wrapper.get('offset')), seconds(wrapper.get('start')), seconds(wrapper.get('duration'))), (3605, 100, 4))
        self.assertEqual((seconds(inner.get('offset')), seconds(inner.get('start'))), (100, 100))
        self.assertEqual((seconds(result.get('offset')), seconds(result.get('start')), seconds(result.get('duration'))), (F(401, 4), F(1221, 2), 2))
        self.assertEqual(seconds(wrapper.get('offset')) + seconds(result.get('offset')) - seconds(wrapper.get('start')), F(14421, 4))
        self.assertIsNone(result.get('lane'))
        self.assertEqual(ET.tostring(inner.find('mc-source')), original_mc_source)
        self.assertEqual(ET.tostring(inner.find('marker')), original_marker)
        self.assertEqual(ET.tostring(root.find('resources')), original_assets)
        self.assertEqual(report['skipped'], [])

    def test_two_original_wavs_remain_serial_components_of_one_multicam(self):
        root, spine, mc = fixture()
        add_sound(mc, duration=2)
        add_sound(mc, ref='wav-2', record=102, source=700, duration=2, asset_start=700)
        report = normalize_native_audio_links(root)
        wrapper = spine.find('clip')
        self.assertEqual(len(wrapper.findall('mc-clip')), 1)
        self.assertEqual([(a.get('ref'), seconds(a.get('offset')), seconds(a.get('start')), seconds(a.get('duration'))) for a in wrapper.findall('audio')],
                         [('wav-1', 100, 600, 2), ('wav-2', 102, 700, 2)])
        self.assertEqual(report['normalized_audio_components'], 2)

    def test_late_audio_and_internal_gap_do_not_gain_fabricated_coverage(self):
        root, spine, mc = fixture()
        add_sound(mc, record=F(401, 4), source=600, duration=F(3, 4))
        add_sound(mc, ref='wav-2', record=102, source=700, duration=2, asset_start=700)
        gap_before = ET.tostring(spine.find('gap'))
        normalize_native_audio_links(root)
        wrapper = spine.find('clip')
        self.assertEqual([(seconds(a.get('offset')), seconds(a.get('duration'))) for a in wrapper.findall('audio')], [(F(401, 4), F(3, 4)), (102, 2)])
        self.assertEqual(ET.tostring(spine.find('gap')), gap_before)
        self.assertEqual(seconds(wrapper.get('duration')), 4)

    def test_audio_range_is_intersected_with_trimmed_multicam(self):
        root, spine, mc = fixture()
        add_sound(mc, record=99, source=600, duration=8)
        normalize_native_audio_links(root)
        audio = spine.find('clip/audio')
        self.assertEqual((seconds(audio.get('offset')), seconds(audio.get('start')), seconds(audio.get('duration'))), (100, 601, 4))

    def test_only_main_project_spine_is_normalized_not_multicam_angles(self):
        root, spine, mc = fixture(); add_sound(mc)
        angle = root.find('./resources/media/multicam/mc-angle')
        angle_mc = copy.deepcopy(mc); angle.append(angle_mc)
        before = ET.tostring(angle)
        normalize_native_audio_links(root)
        self.assertEqual(ET.tostring(angle), before)
        self.assertEqual(len(spine.findall('clip')), 1)

    def test_disabled_markers_effects_and_retime_are_preserved_and_reported(self):
        def disabled_clip(mc, child, audio): child.set('enabled', '0')
        def disabled_audio(mc, child, audio): audio.set('enabled', '0')
        def disabled_mc(mc, child, audio): mc.set('enabled', '0')
        def clip_marker(mc, child, audio): ET.SubElement(child, 'marker', start='600s', duration='1/25s', value='KEEP_SOUND')
        def audio_marker(mc, child, audio): ET.SubElement(audio, 'marker', start='600s', duration='1/25s', value='KEEP_SOUND')
        def clip_pan(mc, child, audio): child.insert(0, ET.Element('adjust-panner', mode='stereo', amount='0.3'))
        def audio_effect(mc, child, audio): ET.SubElement(audio, 'filter-audio', ref='fx')
        def clip_retime(mc, child, audio): child.insert(0, ET.Element('timeMap'))
        def parent_retime(mc, child, audio): mc.insert(0, ET.Element('timeMap'))
        def animated_volume(mc, child, audio): ET.SubElement(ET.SubElement(audio, 'adjust-volume'), 'param', name='volume', value='-3dB')
        for edit in (disabled_clip, disabled_audio, disabled_mc, clip_marker, audio_marker, clip_pan, audio_effect, clip_retime, parent_retime, animated_volume):
            with self.subTest(edit=edit.__name__):
                root, spine, mc = fixture(); child, audio = add_sound(mc)
                edit(mc, child, audio); before = ET.tostring(root)
                report = normalize_native_audio_links(root)
                self.assertEqual(ET.tostring(root), before)
                self.assertEqual(report['normalized_multicam_clips'], 0)
                self.assertEqual(len(report['skipped']), 1)

    def test_one_complex_component_prevents_partial_group_rewrite(self):
        root, spine, mc = fixture(); add_sound(mc, duration=2)
        child, audio = add_sound(mc, ref='wav-2', record=102, source=700, duration=2, asset_start=700)
        ET.SubElement(child, 'metadata')
        before = ET.tostring(root)
        report = normalize_native_audio_links(root)
        self.assertEqual(ET.tostring(root), before)
        self.assertEqual(len(report['skipped']), 1)

    def test_overlap_and_multiple_audio_lanes_are_preserved(self):
        for lane, position in (('1', 101), ('2', 102)):
            with self.subTest(lane=lane):
                root, spine, mc = fixture(); add_sound(mc, duration=2)
                add_sound(mc, ref='wav-2', record=position, source=700, duration=2, asset_start=700, lane=lane)
                before = ET.tostring(root)
                report = normalize_native_audio_links(root)
                self.assertEqual(ET.tostring(root), before)
                self.assertEqual(len(report['skipped']), 1)

    def test_static_volume_moves_without_changing_amount(self):
        for on_clip in (False, True):
            with self.subTest(on_clip=on_clip):
                root, spine, mc = fixture(); child, audio = add_sound(mc)
                (child if on_clip else audio).insert(0, ET.Element('adjust-volume', amount='-3dB'))
                normalize_native_audio_links(root)
                self.assertEqual(spine.find('clip/audio/adjust-volume').get('amount'), '-3dB')

    def test_nonzero_source_channels_and_stereo_expression_are_preserved(self):
        root, spine, mc = fixture(); child, audio = add_sound(mc)
        asset = root.find('./resources/asset[@id="wav-1"]'); asset.set('audioChannels', '2')
        audio.set('srcCh', '0, 2'); audio.set('outCh', 'L,R')
        normalize_native_audio_links(root)
        result = spine.find('clip/audio')
        self.assertEqual((result.get('srcCh'), result.get('outCh')), ('0, 2', 'L,R'))

    def test_normalization_is_idempotent(self):
        root, spine, mc = fixture(); add_sound(mc)
        normalize_native_audio_links(root); before = ET.tostring(root)
        second = normalize_native_audio_links(root)
        self.assertEqual(ET.tostring(root), before)
        self.assertEqual(second['normalized_multicam_clips'], 0)

    def test_conform_normalizes_after_replacing_video_and_keeps_wav_identity(self):
        root, spine, mc = fixture(); add_sound(mc)
        wav_before = ET.tostring(root.find('./resources/asset[@id="wav-1"]'))
        index = {str(Path('/tmp/proxy.mov').resolve()): dict(original_path='/tmp/original.mov', original_start=2500, proxy_start=2500, frames=500)}
        info = dict(path='/tmp/original.mov', fps=25, frames=500, start_tc='00:01:40:00', width=3840, height=2160, audio_channels=[])
        with tempfile.TemporaryDirectory() as temp, patch('core.probe', return_value=info):
            target = Path(temp) / 'original.fcpxml'
            report = conform_fcpxml(ET.ElementTree(root), index, 25, Path(temp) / 'edit.fcpxml', target)
            saved = ET.parse(target).getroot()
            self.assertEqual(report['audio_link_normalization']['normalized_multicam_clips'], 1)
            self.assertEqual(report['replaced_file_definitions'], 1)
            self.assertEqual(json.loads(target.with_suffix('.review.json').read_text())['audio_link_normalization'], report['audio_link_normalization'])
            self.assertEqual(saved.find('./library/event/project/sequence/spine/clip/audio').get('ref'), 'wav-1')
            self.assertIn('original', saved.find('./resources/media/multicam/mc-angle/clip/video').get('ref'))
        self.assertEqual(ET.tostring(root.find('./resources/asset[@id="wav-1"]')).strip(), wav_before.strip())


if __name__ == '__main__':
    unittest.main()
