"""Known-path execution: bounded reads, one dispatch per step, useful partial results."""
import asyncio
from types import SimpleNamespace

import pytest
from mcp import Client
from mobile_use.server import invoke, create_server
from mobile_use import targets
from test_targets import svc, observe, button, scene, node
from test_sequences import local_clock, follow, backs
from test_vision import visual, tap, frame


@pytest.fixture
def clock(local_clock, monkeypatch):
    import mobile_use.navigation as nav
    monkeypatch.setattr(nav, 'time', SimpleNamespace(monotonic=lambda: local_clock.now))
    return local_clock


def plan(*steps, **kwargs):
    return dict(allowed_packages=['example.calculator', 'example.destination'], steps=list(steps), **kwargs)


def tap_step(text='go', **kwargs):
    return dict(kind='tap', package='example.calculator', target={'text': text}, **kwargs)


def swipe_step(**kwargs):
    return dict(kind='swipe', package='example.calculator',
                swipe={'start_x': 20, 'start_y': 150, 'end_x': 25, 'end_y': 110}, **kwargs)


def test_known_path_has_one_final_capture_and_fresh_selectors(svc, clock):
    oid = observe(svc)
    def execute(g):
        svc.driver.events.append(g)
        svc.driver.xml = scene(button='next', y=65)
    svc.driver.execute = execute
    r = invoke(svc.navigate, oid, plan(tap_step(), tap_step('next')), image='never')
    assert not r.is_error, r.structured_content
    data = r.structured_content
    assert data['reads']['hierarchy'] == 7  # zero-cost fake reads need a timed third sample per target, plus final
    assert data['reads'].get('screenshot', 0) == 0
    assert len(svc.driver.events) == 2
    assert svc.driver.events[1].points[0][1] >= 90
    assert data['navigation']['completed_inputs'] == 2
    assert (svc.run_dir/'actions.jsonl').read_text().count('"event": "observe"') == 2


@pytest.mark.parametrize('change', ['duplicate', 'dialog', 'package', 'off', 'stop', 'missing'])
def test_second_step_halts_without_replaying_first(svc, clock, change):
    oid = observe(svc)
    def execute(g):
        svc.driver.events.append(g)
        if change == 'duplicate': svc.driver.xml = scene(duplicate=True)
        elif change == 'dialog': svc.driver.xml = scene(extra=node(**{'class':'android.app.Dialog'}))
        elif change == 'package': svc.driver.package = 'example.unexpected'
        elif change == 'stop': svc.cancel.set()
        elif change == 'off':
            h = svc.driver.header
            svc.driver.header = lambda: {**h(), 'screen_on':False}
        else: svc.driver.xml = scene(button='gone')
    svc.driver.execute = execute
    r = invoke(svc.navigate, oid, plan(tap_step(), tap_step()), image='never')
    assert r.is_error and len(svc.driver.events) == 1
    assert r.structured_content['input_status'] == 'sent'
    assert [s['input_status'] for s in r.structured_content['navigation']['steps']] == ['sent', 'not_sent']
    assert clock.now <= 105.601


def test_unknown_second_input_is_journaled_once(svc, clock):
    oid = observe(svc)
    def execute(g):
        svc.driver.events.append(g)
        if len(svc.driver.events) == 2: raise TimeoutError()
    svc.driver.execute = execute
    r = invoke(svc.navigate, oid, plan(tap_step(), tap_step()), image='never')
    assert r.is_error and len(svc.driver.events) == 2
    assert r.structured_content['input_status'] == 'unknown'
    assert [s['input_status'] for s in r.structured_content['navigation']['steps']] == ['sent', 'unknown']


def test_late_invalid_step_rejected_before_launch(svc, clock):
    oid = observe(svc)
    invalid = swipe_step(when={'text':'task A'})
    invalid['swipe']['end_x'] = 200
    r = invoke(svc.navigate, oid, plan({'kind':'launch','package':'example.destination'}, invalid), image='never')
    assert r.is_error and not svc.driver.events


def test_return_condition_swipe_has_no_intermediate_observation(svc, clock):
    oid = observe(svc)
    after = plan(swipe_step(when={'text':'task A'}))
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(return_back=backs(count=1, after=after)), image='never')
    assert not r.is_error, r.structured_content
    assert len(svc.driver.events) == 3
    assert r.structured_content['continuation']['navigation']['steps'][0]['input_status'] == 'sent'
    assert (svc.run_dir/'actions.jsonl').read_text().count('"event": "observe"') == 2


def test_mcp_navigation_schema_and_dispatch(svc, clock):
    async def check():
        async with Client(create_server(svc)) as client:
            ts = {t.name:t for t in (await client.list_tools()).tools}
            assert ts['navigate'].annotations.read_only_hint is False
            r = await client.call_tool('navigate', {'observation_id':observe(svc), 'plan':plan(tap_step()), 'image':'never'})
            assert not r.is_error, r.structured_content
    asyncio.run(check())


@pytest.mark.parametrize('mode', ['match', 'missing', 'dialog'])
def test_visual_page_guard_before_continuation_swipe(svc, clock, mode):
    oid = observe(svc)
    png, labels = frame(70)
    svc.driver.screenshot = lambda: (png, (100,200))
    def read(*args, **kwargs):
        if mode == 'dialog':
            svc.driver.xml = scene(extra=node(**{'class':'android.app.Dialog'}))
        return labels if mode != 'missing' else []
    svc._vision = SimpleNamespace(read=read)
    r = invoke(svc.navigate, oid, plan(swipe_step(
        visual_when={'text':'Open','context_text':'Report A'}, timeout_seconds=1)), image='never')
    assert bool(r.is_error) == (mode != 'match'), r.structured_content
    assert len(svc.driver.events) == (1 if mode == 'match' else 0)


def test_read_that_exceeds_deadline_cannot_send_input(svc, clock):
    oid = observe(svc)
    original = svc.driver.hierarchy
    def read():
        clock.now += 2
        return original()
    svc.driver.hierarchy = read
    r = invoke(svc.navigate, oid, plan(tap_step(timeout_seconds=1)), image='never')
    assert r.is_error and not svc.driver.events


def test_launch_window_transition_waits_for_single_window_without_input_retry(svc, clock):
    svc.driver.xml = scene().replace('</hierarchy>', node(rid='old-window')+'</hierarchy>')
    oid = observe(svc)
    clock.on_wait = lambda: setattr(svc.driver, 'xml', scene())
    r = invoke(svc.navigate, oid, plan(tap_step()), image='never')
    assert not r.is_error and len(svc.driver.events) == 1
    assert clock.now == pytest.approx(100.6)


def test_edge_windows_do_not_block_interior_target_or_root_layout(svc, clock):
    edges = node(rid='status',bounds='[0,0][100,10]',package='system.ui')
    edges += node(rid='nav',bounds='[0,190][100,200]',package='system.nav')
    svc.driver.xml = scene().replace('</hierarchy>',edges+'</hierarchy>')
    oid = observe(svc)
    r = invoke(svc.navigate, oid, plan(tap_step()), image='never')
    assert not r.is_error and len(svc.driver.events) == 1
    a = targets.build_graph(svc.driver.xml,100,200)
    b = targets.build_graph(svc.driver.xml.replace('[0,0][100,200]','[1,2][99,198]'),100,200)
    targets.check_layout(a,b,[(40,40,90,70)],[(40,40,90,70)])


def test_layout_reflow_waits_for_target_geometry_and_uses_last_sample(svc, clock):
    oid = observe(svc)
    count = 0
    def hierarchy():
        nonlocal count
        count += 1
        return scene(y=20 if count == 1 else 70)
    svc.driver.hierarchy = hierarchy
    r = invoke(svc.navigate, oid, plan(tap_step()), image='never')
    assert not r.is_error, r.structured_content
    assert len(svc.driver.events) == 1 and svc.driver.events[0].points[0][1] >= 95
    assert r.structured_content['navigation']['steps'][0]['layout_updates'] == 1
    assert r.structured_content['reads']['hierarchy'] == 4  # moved sample reused, not discarded


@pytest.mark.parametrize('arrives', [True, False])
def test_final_condition_uses_remaining_budget_and_captures_once(svc, clock, arrives):
    oid = observe(svc)
    if arrives:
        clock.on_wait = lambda: setattr(svc.driver, 'xml', scene(outside='loaded'))
    r = invoke(svc.navigate, oid, plan(tap_step(), until={'text':'loaded'}, timeout_seconds=2), image='never')
    assert bool(r.is_error) == (not arrives), r.structured_content
    assert len(svc.driver.events) == 1
    assert r.structured_content['navigation']['wait']['status'] == ('matched' if arrives else 'timeout')
    assert (svc.run_dir/'actions.jsonl').read_text().count('"event": "observe"') == 2


def test_own_root_layout_animation_allows_local_visual_relocation(visual):
    s, oid, change = visual
    change()
    old = s.driver.hierarchy
    s.driver.hierarchy = lambda: old().replace('[0,0][100,200]', '[1,2][99,197]')
    r = tap(s, oid)
    assert not r.is_error, r.structured_content
    assert len(s.driver.events) == 1


@pytest.mark.parametrize('layout', ['window', 'clipped', 'surface'])
def test_layout_changes_that_affect_evidence_still_stop(layout):
    a = targets.build_graph(scene(), 100, 200)
    xml = scene()
    if layout == 'window': xml = xml.replace('</hierarchy>', node(rid='popup')+'</hierarchy>')
    elif layout == 'clipped': xml = xml.replace('[0,0][100,200]', '[0,0][30,200]')
    else: xml = scene(extra=node(rid='new-surface', bounds='[0,0][100,150]'))
    with pytest.raises(targets.TargetError):
        targets.check_layout(a, targets.build_graph(xml,100,200), [(40,40,90,70)], [(40,40,90,70)])


def test_two_equal_samples_before_refresh_rebound_do_not_authorize_old_position(svc, clock):
    oid = observe(svc)
    count = 0
    def hierarchy():
        nonlocal count
        count += 1
        return scene(y=20 if count <= 2 else 70, outside=f'timer {count}')
    svc.driver.hierarchy = hierarchy
    r = invoke(svc.navigate, oid, plan(tap_step()), image='never')
    assert not r.is_error, r.structured_content
    assert svc.driver.events[0].points[0][1] >= 95
    readiness = r.structured_content['navigation']['steps'][0]['readiness']
    assert readiness['layout_updates'] == 1 and readiness['elapsed_ms'] >= 1200


@pytest.mark.parametrize('clears',[True,False])
def test_declared_refresh_control_gates_click_and_times_out_without_input(svc, clock, clears):
    svc.driver.xml = scene(extra=node('Updating','refresh',bounds='[0,0][20,10]'))
    oid = observe(svc)
    if clears:
        clock.on_wait = lambda: setattr(svc.driver,'xml',scene()) if clock.now >= 100.4 else None
    r = invoke(svc.navigate,oid,plan(tap_step(readiness={'busy':{'resource_id':'refresh'},'timeout_seconds':2})),image='never')
    assert bool(r.is_error) == (not clears),r.structured_content
    assert len(svc.driver.events) == int(clears)
    if clears:
        evidence = r.structured_content['navigation']['steps'][0]['readiness']
        assert evidence['busy_samples'] >= 2 and clock.now >= 101
    else:
        assert r.structured_content['navigation']['steps'][0]['input_status'] == 'not_sent'


def test_ordinary_tap_can_use_same_readiness_guard(svc, clock, monkeypatch):
    import mobile_use.validation as validation
    monkeypatch.setattr(validation,'time',SimpleNamespace(monotonic=lambda:clock.now))
    oid = observe(svc)
    clock.on_wait = lambda: setattr(svc.driver,'xml',scene(y=70))
    r = invoke(svc.tap,oid,button(svc),readiness={'quiet_ms':700},image='never')
    assert not r.is_error,r.structured_content
    assert len(svc.driver.events)==1 and svc.driver.events[0].points[0][1]>=95
    assert r.structured_content['validation']['readiness']['layout_updates']==1


def test_invalid_readiness_in_later_step_blocks_entire_plan(svc,clock):
    oid=observe(svc)
    r=invoke(svc.navigate,oid,plan(tap_step(),tap_step(readiness={'quiet_ms':50})),image='never')
    assert r.is_error and not svc.driver.events
