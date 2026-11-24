"""Conservative identity-based rebuilding of Resolve's native multi-track XML."""
import copy
from fractions import Fraction as F
import unittest
import xml.etree.ElementTree as ET

from fcpxml_conform import normalize_identified_audio_links,normalize_native_audio_links


def sec(value):
    value=F(value)
    return f'{value.numerator}/{value.denominator}s'


def fixture():
    root=ET.Element('fcpxml',version='1.8');resources=ET.SubElement(root,'resources')
    ET.SubElement(resources,'format',id='fmt',frameDuration='1/25s',width='1920',height='1080')
    rows=[];guide=[]
    for session,clock in (('base',1000),('alt',2000)):
        ET.SubElement(resources,'asset',id=session+'v',name='C001.mov',hasVideo='1',
            start=sec(clock),duration='10s',format='fmt',src='file:///'+session+'/daily/C001.mov')
        ET.SubElement(resources,'asset',id=session+'a',name='MIX.wav',hasAudio='1',audioChannels='1',
            start=sec(clock),duration='10s',src='file:///'+session+'/MIX.wav')
        media=ET.SubElement(resources,'media',id=session,name=session)
        mc=ET.SubElement(media,'multicam',format='fmt')
        angle=ET.SubElement(mc,'mc-angle',name='A',angleID=session+'-A')
        ET.SubElement(angle,'asset-clip',ref=session+'v',start=sec(clock),offset='0s',duration='10s')
        rows.append(dict(session_id=session,proxy_path='/'+session+'/daily/C001.mov',original_path='/'+session+'/raw/C001.mov'))
        guide.append(dict(session_id=session,path='/'+session+'/MIX.wav'))
    sequence=ET.SubElement(ET.SubElement(ET.SubElement(ET.SubElement(root,'library'),'event'),'project'),'sequence',
        duration='11s',tcStart='3600s',format='fmt')
    spine=ET.SubElement(sequence,'spine')
    for start,end in ((0,3),(3,6),(6,10)):
        top=ET.SubElement(spine,'mc-clip',name='base',ref='base',offset=sec(3600+start),start=sec(start),duration=sec(end-start))
        ET.SubElement(top,'mc-source',srcEnable='video',angleID='base-A')
        alt=ET.SubElement(top,'mc-clip',name='alt',ref='alt',offset=sec(start+1),start=sec(start),duration=sec(end-start),lane='1')
        ET.SubElement(alt,'mc-source',srcEnable='video',angleID='alt-A')
        for session,clock,lane,offset in (('alt',2000,'2',start+1),('base',1000,'3',start)):
            clip=ET.SubElement(top,'clip',name='MIX.wav',offset=sec(offset),start=sec(clock+start),duration=sec(end-start),lane=lane,enabled='1')
            ET.SubElement(clip,'audio',ref=session+'a',srcCh='1',offset=sec(clock),start=sec(clock),duration='10s')
    ET.SubElement(spine,'gap',name='Gap',offset='3610s',start='3600s',duration='1s')
    return root,rows,guide


def placements(root):
    """Semantic A/V positions, respecting only each audio container's trim."""
    found=[]
    def number(node,key,default='0s'):return F(node.get(key,default)[:-1])
    def walk(node,absolute):
        if node.tag=='mc-clip':
            found.append(('video',node.get('ref'),absolute,number(node,'start'),number(node,'duration')))
        for child in node:
            if child.tag in ('mc-clip','clip','gap'):
                walk(child,absolute+number(child,'offset')-number(node,'start'))
            elif child.tag=='audio':
                position=absolute+number(child,'offset')-number(node,'start')
                first=max(position,absolute)
                end=min(position+number(child,'duration'),absolute+number(node,'duration'))
                found.append(('audio',child.get('ref'),first,number(child,'start')+first-position,end-first))
    for child in root.findall('./library/event/project/sequence/spine/*'):
        walk(child,number(child,'offset'))
    return sorted(found)


class IdentifiedMultitrackLinkTests(unittest.TestCase):
    def test_six_pairs_keep_exact_positions_sources_and_extended_alternative_tail(self):
        root,rows,guide=fixture();before=placements(root)
        resources=ET.tostring(root.find('resources'))
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],6)
        self.assertEqual(report['normalized_audio_components'],6)
        self.assertEqual(report['skipped'],[])
        self.assertEqual(before,placements(root))
        self.assertEqual(resources,ET.tostring(root.find('resources')))
        linked=[node for node in root.findall('.//library//clip') if node.find('mc-clip') is not None and node.find('audio') is not None]
        self.assertEqual(len(linked),6)
        for wrapper in linked:
            self.assertEqual(wrapper.find('mc-clip').get('ref')+'a',wrapper.find('audio').get('ref'))
            self.assertIsNone(wrapper.find('audio').get('lane'))
        alt=[row for row in placements(root) if row[:2]==('video','alt')]
        self.assertEqual(alt[-1][2]+alt[-1][4],3611)

    def test_old_json_without_identity_remains_conservative(self):
        root,_,_=fixture();before=ET.tostring(root)
        report=normalize_native_audio_links(root)
        self.assertEqual(report['normalized_multicam_clips'],0)
        self.assertEqual(len(report['skipped']),3)
        self.assertEqual(before,ET.tostring(root))

    def test_unknown_same_basename_wav_never_gets_inferred_from_lane_or_overlap(self):
        root,rows,guide=fixture();guide[0]['path']='/unrelated/MIX.wav';before=placements(root)
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],3)
        self.assertEqual(before,placements(root))
        for wrapper in root.findall('.//library//clip'):
            if wrapper.find('mc-clip') is not None and wrapper.find('audio') is not None:
                self.assertEqual(wrapper.find('mc-clip').get('ref'),'alt')

    def test_shared_wav_session_ambiguity_does_not_link(self):
        root,rows,guide=fixture();guide.append(dict(session_id='other',path='/base/MIX.wav'))
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],3)
        self.assertEqual(len(report['skipped']),3)

    def test_explicit_pgm_fallback_keeps_embedded_original_sound_and_links(self):
        root,rows,guide=fixture()
        asset=root.find('./resources/asset[@id="basea"]')
        asset.set('hasVideo','1');asset.set('src','file:///base/PGM.mov')
        guide[0].update(path='/base/PGM.mov',source_kind='pgm')
        before=placements(root);resources=ET.tostring(root.find('resources'))
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],6)
        self.assertEqual(before,placements(root))
        self.assertEqual(resources,ET.tostring(root.find('resources')))

    def test_video_asset_without_explicit_pgm_fallback_declaration_is_preserved(self):
        root,rows,guide=fixture()
        asset=root.find('./resources/asset[@id="basea"]');asset.set('hasVideo','1')
        before=ET.tostring(root)
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],0)
        self.assertEqual(len(report['skipped']),3)
        self.assertEqual(before,ET.tostring(root))

    def test_picture_only_trim_keeps_its_audio_edit_unlinked_and_unchanged(self):
        root,rows,guide=fixture()
        root.find('./library/event/project/sequence/spine/mc-clip').set('duration','74/25s')
        before=placements(root)
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],5)
        self.assertEqual(before,placements(root))
        self.assertEqual(len(report['skipped']),1)

    def test_resolve_video_only_trim_with_sound_connected_to_leading_gap(self):
        # Actual Resolve 21.1 export after trimming just the first picture:
        # audio remains [0,75), picture becomes [11,69), all in frames.
        root,rows,guide=fixture();spine=root.find('./library/event/project/sequence/spine')
        for top in spine.findall('mc-clip'):
            for child in list(top):
                if child.tag=='mc-clip' or (child.tag=='clip' and child.find('audio').get('ref')=='alta'):
                    top.remove(child)
        first=spine.find('mc-clip');audio=first.find('clip');first.remove(audio)
        leading=ET.Element('gap',name='Gap',offset='3600s',start='3600s',duration='11/25s')
        audio.set('offset','3600s');leading.append(audio);spine.insert(0,leading)
        first.set('offset','90011/25s');first.set('start','11/25s');first.set('duration','58/25s')
        spine.insert(2,ET.Element('gap',name='Gap',offset='90069/25s',start='3600s',duration='6/25s'))
        before=placements(root);unlinked=ET.tostring(first);leading_before=ET.tostring(leading)
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],2)
        self.assertEqual(before,placements(root))
        self.assertEqual(ET.tostring(spine.find('mc-clip')),unlinked)
        self.assertEqual(ET.tostring(spine.find('gap')),leading_before)

    def test_duplicate_same_session_picture_range_does_not_guess_audio_owner(self):
        root,rows,guide=fixture();top=root.find('./library/event/project/sequence/spine/mc-clip')
        duplicate=copy.deepcopy(top.find('mc-clip'));duplicate.set('lane','4');top.append(duplicate)
        before=placements(root);report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],5)
        self.assertEqual(before,placements(root))
        self.assertEqual(len(report['skipped']),2)

    def test_same_absolute_alternate_range_across_hosts_is_globally_ambiguous(self):
        root,rows,guide=fixture();first=root.find('./library/event/project/sequence/spine/mc-clip')
        alternate=first.find('mc-clip');alternate.set('offset','4s');alternate.set('lane','4')
        audio=next(clip for clip in first.findall('clip') if clip.find('audio').get('ref')=='alta')
        audio.set('offset','4s');audio.set('start','2003s');audio.set('lane','5')
        before=placements(root)
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],4)
        self.assertEqual(len(report['skipped']),2)
        self.assertEqual(before,placements(root))

    def test_existing_linked_composite_in_another_host_prevents_duplicate_pairing(self):
        root,rows,guide=fixture();spine=root.find('./library/event/project/sequence/spine')
        top=spine.find('mc-clip');existing=ET.Element('clip',name='Existing linked take',
            offset='3601s',start='0s',duration='3s',lane='4')
        picture=copy.deepcopy(top.find('mc-clip'));picture.attrib.pop('lane');picture.set('offset','0s')
        existing.append(picture)
        ET.SubElement(existing,'audio',ref='alta',offset='0s',start='2000s',duration='3s')
        spine.append(existing);before=placements(root);existing_before=ET.tostring(existing)
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],5)
        self.assertEqual(len(report['skipped']),1)
        self.assertEqual(before,placements(root))
        self.assertEqual(existing_before,ET.tostring(spine[-1]))

    def test_coincident_audio_asset_in_other_gap_prevents_local_pairing(self):
        root,rows,guide=fixture();spine=root.find('./library/event/project/sequence/spine')
        host=ET.SubElement(spine,'gap',offset='3601s',start='0s',duration='3s')
        ET.SubElement(host,'asset-clip',ref='alta',offset='0s',start='2000s',duration='3s',lane='4')
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],5)
        self.assertEqual(len(report['skipped']),2)  # Ambiguous pair plus unsupported standalone host.

    def test_effects_and_retiming_are_left_intact(self):
        for tag in ('timeMap','filter-video','transition'):
            root,rows,guide=fixture();top=root.find('./library/event/project/sequence/spine/mc-clip')
            ET.SubElement(top,tag);before=ET.tostring(top)
            report=normalize_identified_audio_links(root,rows,guide)
            self.assertEqual(report['normalized_multicam_clips'],4)
            self.assertEqual(before,ET.tostring(root.find('./library/event/project/sequence/spine/mc-clip')))

    def test_static_volume_and_crop_transform_are_preserved(self):
        root,rows,guide=fixture();top=root.find('./library/event/project/sequence/spine/mc-clip')
        ET.SubElement(top.find('mc-source'),'adjust-transform',scale='1.2 1.2',position='0 0')
        ET.SubElement(top.find('clip'),'adjust-volume',amount='-3dB')
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],6)
        self.assertEqual(root.find('.//audio/adjust-volume').get('amount'),'-3dB')
        self.assertEqual(root.find('.//library//mc-source/adjust-transform').get('scale'),'1.2 1.2')

    def test_idempotence_and_source_session_mismatch(self):
        root,rows,guide=fixture();normalize_identified_audio_links(root,rows,guide);before=ET.tostring(root)
        self.assertEqual(normalize_identified_audio_links(root,rows,guide)['normalized_multicam_clips'],0)
        self.assertEqual(before,ET.tostring(root))
        root,rows,guide=fixture();rows[0]['session_id']='unrelated'
        report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],3)


if __name__=='__main__':unittest.main()
