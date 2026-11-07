"""Cached alternatives share recognition, but never skip fresh input guards."""
import io
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from mobile_use.server import invoke
from mobile_use.server import create_server
from mcp import Client
from mobile_use.vision import LocalVision, Label
from test_vision import visual, frame


CHOICES = {'candidates': [{'text': 'Open', 'context_text': 'Report A'},
                          {'text': 'Read', 'context_text': 'Report A'}]}


def query(s, oid, choices=CHOICES):
    return invoke(s.get_observation, oid, visual_query_any_of=choices, image='never')


def test_shared_detection_incrementally_decodes_lines_and_fresh_frames_do_not_reuse():
    v = LocalVision()
    boxes = np.array([[[0,0],[20,0],[20,10],[0,10]], [[0,20],[200,20],[200,30],[0,30]]])
    def engine(pixels, *, use_det, **kwargs):
        return SimpleNamespace(boxes=boxes, txts=['short' if pixels.shape[1] == 20 else 'long'], scores=[.99])
    v.engine = engine
    b = io.BytesIO(); Image.new('RGB', (220,100), 'white').save(b, format='PNG'); png = b.getvalue()
    assert [x.text for x in v.read(png, texts=['ab'])] == ['short']
    assert [x.text for x in v.read(png, texts=['ab','a'*30])] == ['short','long']
    assert v.stats == dict(detections=1, recognitions=2, frame_hits=1, line_hits=1)
    v.read(png, texts=['ab'])
    assert v.stats['recognitions'] == 2
    v.read(bytes(bytearray(png)), texts=['ab'])
    assert v.stats['detections'] == 2  # new sample, even with identical bytes
    v.read(png, texts=['ab'], region=[0,0,220,50])
    assert v.stats['detections'] == 3
    v.clear_cache(); assert not v._frames


def test_cached_set_queries_once_without_device_reads_then_relocates_and_expires(visual):
    s, oid, change = visual
    calls=[]; original=s._vision.read
    def read(png, texts=(), region=None):
        calls.append(texts); return original(png, texts, region)
    s._vision.read=read
    r=query(s,oid); d=r.structured_content
    assert not r.is_error and d['reads']=={} and len(calls)==1
    q=d['observation']['visual_candidates']; assert q['status']=='matched'
    cid=q['candidates'][0]['id']
    change()
    r=invoke(s.tap,oid,visual_candidate_id=cid,image='never')
    assert not r.is_error, r.structured_content
    assert len(s.driver.events)==1 and 120 <= s.driver.events[0].points[0][1] < 136
    assert all('Read' in x for x in calls)
    stale=invoke(s.tap,r.structured_content['observation']['observation_id'],visual_candidate_id=cid,image='never')
    assert stale.is_error and len(s.driver.events)==1


@pytest.mark.parametrize('mode', ['missing','competing','low-confidence'])
def test_unresolved_set_returns_relevant_evidence_without_action_ids(visual,mode):
    s,oid,change=visual; png=s.observation.png
    if mode=='missing': s._vision.values[png]=[Label('Poster',1,(0,0,20,20))]
    if mode=='competing': s._vision.values[png].append(Label('Read',1,(30,150,70,170)))
    if mode=='low-confidence': s._vision.values[png][0]=Label('Open',.5,(30,70,71,86))
    r=query(s,oid); q=r.structured_content['observation']['visual_candidates']
    assert q['status']=='unresolved' and all('id' not in c for c in q['candidates'])
    assert not s.driver.events


def test_new_competitor_in_confirmation_frame_blocks_even_when_winner_pixels_unchanged(visual):
    s,oid,change=visual
    cid=query(s,oid).structured_content['observation']['visual_candidates']['candidates'][0]['id']
    first=change()
    im=Image.open(io.BytesIO(first)); ImageDraw.Draw(im).rectangle((30,170,70,190),fill='red')
    b=io.BytesIO();im.save(b,format='PNG');second=b.getvalue()
    s._vision.values[second]=s._vision.values[first]+[Label('Read',.99,(30,170,70,190))]
    frames=iter([first,second]);s.driver.screenshot=lambda:(next(frames),(100,200))
    r=invoke(s.tap,oid,visual_candidate_id=cid,image='never')
    assert r.is_error and r.structured_content['input_status']=='not_sent'
    assert r.structured_content['code']=='visual_target_ambiguous' and not s.driver.events


def test_set_budget_context_and_mutual_exclusion_preserve_single_query(visual):
    s,oid,_=visual
    assert s.get_status()['limits']['cached_visual_candidates']==12
    for choices in [{'candidates':[{'text':f'A{i}','context_text':'Report A'} for i in range(13)]},
                    {'candidates':[{'text':'Open'},{'text':'Read'}]},
                    {'candidates':[{'text':'Open','context_text':'Report A'}]*2}]:
        assert query(s,oid,choices).is_error
    r=invoke(s.get_observation,oid,visual_query=CHOICES['candidates'][0],visual_query_any_of=CHOICES,image='never')
    assert r.is_error
    group_id=query(s,oid).structured_content['observation']['visual_candidates']['candidates'][0]['id']
    r=invoke(s.get_observation,oid,visual_query=CHOICES['candidates'][0],image='never')
    assert not r.is_error and r.structured_content['observation']['visual_candidates']['candidates'][0]['id']
    single_id=r.structured_content['observation']['visual_candidates']['candidates'][0]['id']
    assert query(s,oid).structured_content['observation']['visual_candidates']['candidates'][0]['id']==group_id
    again=invoke(s.get_observation,oid,visual_query=CHOICES['candidates'][0],image='never')
    assert single_id!=group_id and again.structured_content['observation']['visual_candidates']['candidates'][0]['id']==single_id


def test_mcp_schema_dispatches_cached_choices(visual):
    s,oid,_=visual
    async def run():
        async with Client(create_server(s)) as client:
            ts={t.name:t for t in (await client.list_tools()).tools}
            assert 'visual_query_any_of' in ts['get_observation'].input_schema['properties']
            r=await client.call_tool('get_observation',{'observation_id':oid,'visual_query_any_of':CHOICES,
                                                      'image':'never','detail':'brief'})
            assert not r.is_error and r.structured_content['observation']['visual_candidates']['status']=='matched'
    asyncio.run(run())
