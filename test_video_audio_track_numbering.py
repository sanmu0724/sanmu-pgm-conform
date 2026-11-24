"""One performance's single-WAV sound uses the same numbered audio track."""
import copy
from fractions import Fraction
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
import tempfile

from reference_timeline import ReferenceTimelineError, _route_manual_audio


def asset(resources, identity, channels, paired=None):
    node = ET.SubElement(resources, 'asset', id=identity, hasAudio='1',
        audioChannels=str(channels), audioRate='48000', start='1001/48000s',
        duration='20s', src='file:///qa/' + identity + '.wav')
    metadata = ET.SubElement(node, 'metadata')
    ET.SubElement(metadata, 'md', key='com.apple.proapps.studio.reel', value='ORIGINAL_ROLL')
    if paired:
        ET.SubElement(metadata, 'md', key='com.pgm.audioPairID', value='pair-original')
        ET.SubElement(metadata, 'md', key='com.pgm.audioPairChannel', value=paired)
    return node


def sound(host, identity, *, offset='0s', start='3001/48000s', duration='2s', channels='1', leaf='audio'):
    values = dict(ref=identity, offset=offset, start=start, duration=duration, srcCh=channels)
    if leaf == 'asset-clip':
        values.update(srcEnable='audio', audioRole='dialogue')
    else:
        values.update(outCh='L,R' if ',' in channels else 'C', role='dialogue')
    return ET.SubElement(host, leaf, **values)


class VideoAudioTrackNumberingTests(unittest.TestCase):
    def setUp(self):
        self.resources = ET.Element('resources')
        asset(self.resources, 'mono', 1)
        asset(self.resources, 'stereo', 2)

    def test_video_track_number_controls_audio_not_source_format_order(self):
        # V1 mono used to reserve a different slot from V2 stereo/V3 mono.
        hosts = [ET.Element('clip') for _ in range(3)]
        nodes = [sound(hosts[0], 'mono'), sound(hosts[1], 'stereo', channels='1, 2'), sound(hosts[2], 'mono')]
        routes = _route_manual_audio(self.resources, list(enumerate(hosts, 1)))
        self.assertEqual([(row['index'], row['video_track'], row['subtype']) for row in routes],
            [(1, 1, 'stereo'), (2, 2, 'stereo'), (3, 3, 'stereo')])
        self.assertEqual([node.get('role') for node in nodes], ['dialogue.pgm-track-1', 'dialogue.pgm-track-2', 'dialogue.pgm-track-3'])

    def test_mono_and_stereo_same_video_track_keep_source_channels_and_samples(self):
        first, second = ET.Element('clip'), ET.Element('clip')
        mono = sound(first, 'mono', duration='1s')
        stereo = sound(second, 'stereo', offset='1s', channels='1, 2')
        assets_before = ET.tostring(self.resources)
        nodes_before = [copy.deepcopy(node.attrib) for node in (mono, stereo)]
        routes = _route_manual_audio(self.resources, [(2, first), (2, second)])
        self.assertEqual(routes[1]['source_formats'], ['mono', 'stereo'])
        self.assertEqual(routes[1]['subtype'], 'stereo')
        self.assertEqual(ET.tostring(self.resources), assets_before)
        for node, before in zip((mono, stereo), nodes_before):
            self.assertEqual({key: value for key, value in node.attrib.items() if key != 'role'},
                             {key: value for key, value in before.items() if key != 'role'})
            self.assertEqual(Fraction(node.get('start')[:-1]) * 48000, 3001)
            self.assertEqual(node.get('role'), 'dialogue.pgm-track-2')

    def test_gaps_in_video_numbers_and_empty_tail_keep_empty_audio_slots(self):
        host = ET.Element('clip')
        node = sound(host, 'stereo', channels='1, 2', leaf='asset-clip')
        routes = _route_manual_audio(self.resources, [(4, host)], video_track_count=6)
        self.assertEqual([row['index'] for row in routes], [1, 2, 3, 4, 5, 6])
        self.assertEqual([row['empty'] for row in routes], [True, True, True, False, True, True])
        self.assertEqual(node.get('audioRole'), 'dialogue.pgm-track-4')

    def test_no_original_wav_keeps_slots_without_inventing_sound(self):
        host = ET.Element('mc-clip', ref='silent-pgm-fallback', duration='2s')
        before = ET.tostring(host)
        routes = _route_manual_audio(self.resources, [(1, host), (3, ET.Element('gap'))])
        self.assertEqual([row['index'] for row in routes], [1, 2, 3])
        self.assertTrue(all(row['empty'] for row in routes))
        self.assertEqual(ET.tostring(host), before)

    def test_unprepared_split_pair_blocks_instead_of_discarding_or_summing(self):
        asset(self.resources, 'left', 1, 'L')
        asset(self.resources, 'right', 1, 'R')
        host = ET.Element('clip')
        left, right = sound(host, 'left'), sound(host, 'right')
        before = ET.tostring(host)
        with self.assertRaisesRegex(ReferenceTimelineError, '合并为立体声'):
            _route_manual_audio(self.resources, [(1, host)])
        self.assertEqual(ET.tostring(host), before)
        self.assertEqual((left.get('ref'), right.get('ref')), ('left', 'right'))

    def test_connected_other_performance_is_not_claimed_by_baseline(self):
        base = ET.Element('clip')
        own = sound(base, 'mono')
        alternative = ET.SubElement(base, 'clip', lane='2')
        other = sound(alternative, 'stereo', channels='1, 2')
        routes = _route_manual_audio(self.resources, [(1, base), (3, alternative)])
        self.assertEqual(own.get('role'), 'dialogue.pgm-track-1')
        self.assertEqual(other.get('role'), 'dialogue.pgm-track-3')
        self.assertTrue(routes[1]['empty'])

    def test_native_reconform_derives_v3_a3_from_picture_ownership_not_stale_roles(self):
        from fcpxml_conform import standardize_connected_native_interchange
        from resolve_import_helper import build_resolve_manifest
        import test_fcpxml_multitrack_links as native
        root,records,guide=native.fixture()
        resources=root.find('resources')
        stereo=root.find('./resources/asset[@id="alta"]')
        stereo.set('audioChannels','2')
        mono=copy.deepcopy(stereo)
        mono.set('id','alt-mono');mono.set('audioChannels','1');mono.set('src','file:///alt/MIX_MONO.wav')
        resources.append(mono)
        guide.append(dict(session_id='alt',path='/alt/MIX_MONO.wav'))
        for index,top in enumerate(root.findall('./library/event/project/sequence/spine/mc-clip')):
            top.find('mc-clip').set('lane','2')  # V3, while V2 is empty.
            for leaf in top.findall('clip/audio'):
                leaf.set('role','dialogue.pgm-track-99')  # A stale private/import role is not ownership.
                if leaf.get('ref')=='alta':
                    leaf.set('srcCh','1, 2')
                    if index==0:leaf.set('ref','alt-mono');leaf.set('srcCh','1')
        assets_before=ET.tostring(resources)
        expected=native.placements(root)
        report=standardize_connected_native_interchange(root,records,guide)
        self.assertTrue(report['changed'])
        self.assertEqual(report['routing_policy'],'video_audio_ordinal')
        self.assertEqual([(row['index'],row['subtype']) for row in report['audio_tracks']],[(1,'stereo'),(2,'stereo'),(3,'stereo')])
        self.assertTrue(report['audio_tracks'][1]['empty'])
        self.assertEqual(report['audio_tracks'][2]['source_formats'],['mono','stereo'])
        self.assertEqual(ET.tostring(resources),assets_before)
        self.assertEqual(sorted((kind,ref,start,duration) for kind,ref,_position,start,duration in expected),
                         sorted(('video' if node.tag=='mc-clip' else 'audio',node.get('ref'),Fraction(node.get('start')[:-1]),Fraction(node.get('duration')[:-1]))
                                for node in root.findall('./library/event/project/sequence/spine/*') if node.tag in ('mc-clip','asset-clip')))
        alt_audio=[node for node in root.findall('./library/event/project/sequence/spine/asset-clip') if node.get('ref') in ('alta','alt-mono')]
        self.assertEqual({node.get('audioRole') for node in alt_audio},{'dialogue.pgm-track-3'})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'reconform.fcpxml';ET.ElementTree(root).write(path,encoding='utf-8')
            manifest=build_resolve_manifest(path,guide,report['audio_tracks'])
        self.assertEqual({audio['track_index'] for group in manifest['groups'] if group.get('video') and group['video']['track_index']==3 for audio in group['audio']},{3})

    def test_historical_native_split_pair_keeps_both_original_files_and_routes(self):
        from fcpxml_conform import standardize_connected_native_interchange
        from test_native_manual_reconform import pair_fixture
        root,records,guide=pair_fixture()
        report=standardize_connected_native_interchange(root,records,guide)
        self.assertTrue(report['changed'])
        self.assertEqual(report['routing_policy'],'legacy_split_mono')
        sounds=[node for node in root.findall('./library/event/project/sequence/spine/asset-clip') if node.get('ref') in ('alta','alt-right')]
        self.assertEqual(len(sounds),6)
        self.assertEqual({node.get('ref') for node in sounds},{'alta','alt-right'})
        self.assertEqual({node.get('audioRole') for node in sounds},{'dialogue.pgm-track-2','dialogue.pgm-track-3'})


if __name__ == '__main__':
    unittest.main()
