"""Known visual paths and cached candidates; no application-specific fixtures."""
import asyncio
from types import SimpleNamespace

import pytest
from mcp import Client
from mobile_use.server import invoke, create_server
from test_navigation import clock, plan, tap_step
from test_sequences import local_clock, follow, backs
from test_targets import svc, observe, scene, node
from test_vision import frame, visual


def visual_step(**kwargs):
    return dict({'kind':'tap', 'package':'example.calculator', 'when':{'text':'task A'},
                 'visual_target':{'text':'Open','context_text':'Report A'}}, **kwargs)


def install_visual(svc, frames):
    values = dict(frames)
    stream = iter([png for png, labels in frames])
    last = frames[-1][0]
    svc.driver.screenshot = lambda: (next(stream, last), (100,200))
    svc._vision = SimpleNamespace(read=lambda png, **kw: values[png])


def test_structured_to_visual_to_timer_has_no_intermediate_observation(svc, clock):
    oid = observe(svc)
    install_visual(svc, [frame(90)])
    key_times = []
    svc.driver.key = lambda key: (key_times.append(clock.now), svc.driver.events.append(key))
    r = invoke(svc.navigate, oid, plan(tap_step(), visual_step()),
               follow_up=follow(return_back=backs(count=1)), image='never')
    assert not r.is_error, r.structured_content
    d = r.structured_content
    assert d['navigation']['completed_inputs'] == 2
    assert d['reads']['screenshot'] == 2 and len(svc.driver.events) == 3
    assert key_times == [pytest.approx(115.6)]
    assert d['continuation']['timing']['input_sent_at'] == d['navigation']['steps'][-1]['sent_at']
    assert (svc.run_dir/'actions.jsonl').read_text().count('"event": "observe"') == 2


@pytest.mark.parametrize('mode', ['loading', 'movement'])
def test_visual_transient_is_resolved_locally_without_input_retry(svc, clock, mode):
    oid = observe(svc)
    install_visual(svc, [frame(70, text='' if mode == 'loading' else 'Open'), frame(120), frame(120)])
    r = invoke(svc.navigate, oid, plan(visual_step()), image='never')
    assert not r.is_error, r.structured_content
    assert r.structured_content['reads']['screenshot'] == 3
    assert len(svc.driver.events) == 1 and svc.driver.events[0].points[0][1] >= 120


@pytest.mark.parametrize('mode', ['duplicate','context','dialog','package','page','off','unknown'])
def test_visual_failure_is_bounded_and_preserves_input_status(svc, clock, mode):
    oid = observe(svc)
    install_visual(svc, [frame(90, duplicate=mode=='duplicate', context='Other' if mode=='context' else 'Report A')])
    original = svc._vision.read
    def read(*args, **kwargs):
        if mode == 'dialog': svc.driver.xml = scene(extra=node(**{'class':'android.app.Dialog'}))
        if mode == 'package': svc.driver.package = 'example.unexpected'
        if mode == 'page': svc.driver.xml = scene(title='Different page')
        if mode == 'off':
            old = svc.driver.header
            svc.driver.header = lambda: {**old(), 'screen_on':False}
        return original(*args, **kwargs)
    svc._vision.read = read
    if mode == 'unknown':
        def execute(g):
            svc.driver.events.append(g)
            raise TimeoutError()
        svc.driver.execute = execute
    r = invoke(svc.navigate, oid, plan(visual_step()), image='never')
    assert r.is_error, r.structured_content
    assert r.structured_content['input_status'] == ('unknown' if mode=='unknown' else 'not_sent')
    assert len(svc.driver.events) == int(mode=='unknown')
    assert r.structured_content['reads'].get('screenshot',0) <= 4


@pytest.mark.parametrize('bad', ['selector','page','bounds','follow','after'])
def test_all_late_options_are_checked_before_any_input(svc, clock, bad):
    oid=observe(svc)
    step=visual_step(); timing=follow()
    if bad=='selector': step['target']={'text':'go'}
    if bad=='page': step.pop('when')
    if bad=='bounds': step['visual_target']['search_bounds']=[0,0,101,200]
    if bad=='follow': timing['duration_seconds']=40
    if bad=='after': timing['return_back']=backs(after=plan(visual_step(visual_target=None)))
    r=invoke(svc.navigate,oid,plan(tap_step(),step),follow_up=timing,image='never')
    assert r.is_error and not svc.driver.events


def test_followup_failure_keeps_completed_navigation_and_does_not_replay(svc, clock):
    oid=observe(svc)
    install_visual(svc,[frame(90)])
    clock.on_wait=lambda:setattr(svc.driver,'package','example.unexpected')
    r=invoke(svc.navigate,oid,plan(visual_step()),follow_up=follow(),image='never')
    assert r.is_error and len(svc.driver.events)==1
    assert r.structured_content['navigation']['status']=='completed'
    assert r.structured_content['continuation']['status']=='interrupted'
    assert r.structured_content['input_status']=='sent'


def test_cached_candidates_need_no_device_reads_and_relocate_by_id(visual):
    s,oid,change=visual
    r=invoke(s.get_observation,oid,visual_query={'text':'Open','context_text':'Report A'},image='never')
    d=r.structured_content
    assert not r.is_error and d['reads']=={}
    candidate=d['observation']['visual_candidates']['candidates'][0]['id']
    change()
    r=invoke(s.tap,oid,visual_candidate_id=candidate,image='never')
    assert not r.is_error, r.structured_content
    assert len(s.driver.events)==1 and s.driver.events[0].points[0][1]>=120
    stale=invoke(s.tap,r.structured_content['observation']['observation_id'],visual_candidate_id=candidate,image='never')
    assert stale.is_error and len(s.driver.events)==1


def test_ambiguous_cached_candidates_are_diagnostics_not_action_ids(visual):
    s,oid,change=visual
    change(70,duplicate=True)
    oid=invoke(s.observe,image='always').structured_content['observation']['observation_id']
    r=invoke(s.get_observation,oid,visual_query={'text':'Open','context_text':'Report A'},image='never')
    candidates=r.structured_content['observation']['visual_candidates']['candidates']
    assert len(candidates)==2 and all('id' not in c for c in candidates)
    assert not s.driver.events


def test_new_schema_dispatches_visual_navigation_and_followup(svc, clock):
    async def run():
        async with Client(create_server(svc)) as client:
            ts={t.name:t for t in (await client.list_tools()).tools}
            assert 'follow_up' in ts['navigate'].input_schema['properties']
            assert 'visual_query' in ts['get_observation'].input_schema['properties']
            install_visual(svc,[frame(90)])
            result=await client.call_tool('navigate',{'observation_id':observe(svc),'plan':plan(visual_step()),
                'follow_up':follow(),'image':'never'})
            assert not result.is_error, result.structured_content
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['before_binding', 'after_binding'])
def test_first_visual_binding_uses_post_ocr_tree_but_later_window_changes_stop(svc, clock, phase):
    oid=observe(svc)
    install_visual(svc,[frame(90)])
    read=svc._vision.read
    def recognize(*args, **kwargs):
        if phase=='before_binding':
            svc.driver.xml=scene().replace('resource-id="root"','resource-id="arrived"')
        return read(*args, **kwargs)
    svc._vision.read=recognize
    screenshot=svc.driver.screenshot
    count=0
    def capture():
        nonlocal count
        count+=1
        if phase=='after_binding' and count==2:
            svc.driver.xml=scene().replace('resource-id="root"','resource-id="changed"')
        return screenshot()
    svc.driver.screenshot=capture
    r=invoke(svc.navigate,oid,plan(visual_step()),image='never')
    assert bool(r.is_error)==(phase=='after_binding'),r.structured_content
    assert len(svc.driver.events)==int(phase=='before_binding')


@pytest.mark.parametrize('duplicate', [False, True])
def test_known_page_can_supply_context_without_binding_an_irrelevant_item_label(svc, clock, duplicate):
    oid=observe(svc)
    install_visual(svc,[frame(90,context='Variable display title',duplicate=duplicate)])
    r=invoke(svc.navigate,oid,plan(visual_step(visual_target={'text':'Open'})),image='never')
    assert bool(r.is_error)==duplicate,r.structured_content
    assert len(svc.driver.events)==int(not duplicate)
    if not duplicate:
        assert r.structured_content['navigation']['steps'][0]['visual']['context_source']=='declared_accessible_page'


def test_outer_navigation_does_not_repeat_failed_continuation_recovery(svc, clock):
    oid=observe(svc)
    install_visual(svc,[frame(90)])
    clock.on_wait=lambda:setattr(svc.driver,'package','example.unexpected')
    reads=[]
    def fail_observe(**kwargs):
        reads.append(1)
        raise TimeoutError()
    svc._observe=fail_observe
    r=invoke(svc.navigate,oid,plan(visual_step()),follow_up=follow(),image='never')
    assert r.is_error and len(svc.driver.events)==1
    assert len(reads)==1
    assert r.structured_content['input_status']=='sent'
