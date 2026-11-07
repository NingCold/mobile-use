import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import Client

from mobile_use.server import create_server, invoke
from test_targets import svc, observe, button, scene, node


@pytest.fixture
def local_clock(svc, monkeypatch):
    import mobile_use.service as service_module
    import mobile_use.sequences as sequence_module
    clock = SimpleNamespace(now=100., on_wait=lambda: None)
    def wait(delay):
        clock.now += delay
        clock.on_wait()
        return svc.cancel.is_set()
    fake = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(service_module, 'time', fake)
    monkeypatch.setattr(sequence_module, 'time', fake)
    monkeypatch.setattr(svc.cancel, 'wait', wait)
    return clock


def follow(**values):
    return dict(allowed_packages=['example.calculator', 'example.destination'],
                duration_seconds=15, timeout_seconds=30, **values)


def backs(**values):
    return dict(allowed_packages=['example.calculator', 'example.destination'], **values)


def test_click_starts_timer_without_initial_capture_and_returns_twice(svc, local_clock):
    oid = observe(svc)
    key_times = []
    svc.driver.key = lambda key: (key_times.append(local_clock.now), svc.driver.events.append(key))
    data = invoke(svc.tap, oid, button(svc), follow_up=follow(return_back=backs()),
                  image='never', detail='brief').structured_content
    assert data['continuation']['status'] == 'completed'
    assert key_times == [115., 115.4]
    assert [s['kind'] for s in data['continuation']['steps']] == ['tap', 'back', 'back']
    assert data['continuation']['timing']['timer_elapsed_ms'] == 15000
    assert data['observation']['observation_id'] != oid
    events = (svc.run_dir/'actions.jsonl').read_text()
    assert events.count('"event": "observe"') == 2  # initial + final only
    assert len(svc.driver.events) == 3


def test_state_anchor_waits_for_backend_match(svc, local_clock):
    oid = observe(svc)
    local_clock.on_wait = lambda: setattr(svc.driver, 'package', 'example.destination') if local_clock.now >= 103 else None
    r = invoke(svc.tap, oid, button(svc), image='never',
               follow_up=follow(anchor='state_matched', ready={'package': 'example.destination'}))
    assert not r.is_error
    assert local_clock.now == 118
    assert r.structured_content['continuation']['timing']['timer_elapsed_ms'] == 15000


def test_input_anchor_ready_loading_is_included_not_credited_as_dwell(svc, local_clock):
    oid = observe(svc)
    local_clock.on_wait = lambda: setattr(svc.driver, 'package', 'example.destination') if local_clock.now >= 103 else None
    r = invoke(svc.tap, oid, button(svc), image='never',
               follow_up=follow(ready={'package': 'example.destination'}))
    assert not r.is_error and local_clock.now == 115
    assert 'not verified content dwell' in r.structured_content['continuation']['timing']['scope']


def test_completion_ends_early(svc, local_clock):
    oid = observe(svc)
    local_clock.on_wait = lambda: setattr(svc.driver, 'xml', scene(outside='done'))
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(completion={'text': 'done'}), image='never')
    assert not r.is_error and local_clock.now < 102
    assert r.structured_content['continuation']['timing']['status'] == 'completion_matched'


def test_completion_replaces_ready_label_without_interrupting_returns(svc, local_clock):
    oid = observe(svc)
    local_clock.on_wait = lambda: setattr(svc.driver, 'xml', scene(button='done'))
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(
        anchor='state_matched', ready={'package':'example.calculator', 'text':'go'},
        completion={'package':'example.calculator', 'text':'done'}, return_back=backs(count=1)), image='never')
    assert not r.is_error and len(svc.driver.events) == 2
    assert r.structured_content['continuation']['timing']['status'] == 'completion_matched'


@pytest.mark.parametrize('change', ['package', 'off', 'rotation', 'stop', 'dwell_lost'])
def test_interrupted_wait_does_not_send_return_keys(svc, local_clock, change):
    oid = observe(svc)
    header = svc.driver.header
    def update():
        if change == 'package': svc.driver.package = 'example.unexpected'
        elif change == 'stop': svc.cancel.set()
        elif change == 'dwell_lost': svc.driver.xml = scene(button='changed')
        else: svc.driver.header = lambda: {**header(), ('screen_on' if change == 'off' else 'rotation'): (False if change == 'off' else 1)}
    local_clock.on_wait = update
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(
        anchor='state_matched', ready={'package': 'example.calculator', 'text': 'go'}, return_back=backs()), image='never')
    assert r.is_error and r.structured_content['input_status'] == 'sent'
    assert r.structured_content['continuation']['completed_inputs'] == 1
    assert len(svc.driver.events) == 1


def test_timeout_does_not_navigate_or_repeat_tap(svc, local_clock):
    oid = observe(svc)
    opts = follow(ready={'text': 'missing'}, return_back=backs())
    opts.update(duration_seconds=1, timeout_seconds=2)
    r = invoke(svc.tap, oid, button(svc), follow_up=opts, image='never')
    assert r.is_error and len(svc.driver.events) == 1
    assert r.structured_content['continuation']['timing']['status'] == 'timeout'


def test_second_back_unknown_keeps_first_sent_and_never_replays(svc, local_clock):
    oid = observe(svc)
    def key(k):
        svc.driver.events.append(k)
        if len(svc.driver.events) == 2: raise TimeoutError('unknown')
    svc.driver.key = key
    r = invoke(svc.press_key, oid, 'back', back_sequence=backs(), image='never')
    data = r.structured_content
    assert r.is_error and data['input_status'] == 'unknown'
    assert [s['input_status'] for s in data['continuation']['steps']] == ['sent', 'unknown']
    assert len(svc.driver.events) == 2
    entries = [json.loads(line) for line in (svc.run_dir/'actions.jsonl').read_text().splitlines()]
    cause = next(e['exception'] for e in entries if e['event'] == 'sequence_input_unknown')
    assert cause == {'type': 'TimeoutError', 'stage': 'input.back', 'message': 'unknown'}


def test_package_escape_between_backs_stops(svc, local_clock):
    oid = observe(svc)
    local_clock.on_wait = lambda: setattr(svc.driver, 'package', 'example.unexpected')
    r = invoke(svc.press_key, oid, 'back', back_sequence=backs(), image='never')
    assert r.is_error and svc.driver.events == ['back']
    assert r.structured_content['input_status'] == 'sent'


def test_stop_condition_can_skip_second_back(svc, local_clock):
    oid = observe(svc)
    svc.driver.key = lambda k: (svc.driver.events.append(k), setattr(svc.driver, 'xml', scene(outside='home')))
    r = invoke(svc.press_key, oid, 'back', back_sequence=backs(stop_when={'text': 'home'}), image='never')
    assert not r.is_error and svc.driver.events == ['back']
    assert r.structured_content['continuation']['back_status'] == 'stop_condition'


def test_dialog_between_backs_stops(svc, local_clock):
    oid = observe(svc)
    local_clock.on_wait = lambda: setattr(svc.driver, 'xml', scene(extra=node(**{'class':'android.app.Dialog'})))
    r = invoke(svc.press_key, oid, 'back', back_sequence=backs(), image='never')
    assert r.is_error and svc.driver.events == ['back']


@pytest.mark.parametrize('option', [
    {'allowed_packages': []}, {'duration_seconds': float('nan')}, {'anchor': 'model_time'},
    {'anchor': 'state_matched'}, {'return_back': backs(count=4)},
    {'return_back': backs(interval_ms=0)}, {'ready': {'package': 'example.unknown'}},
])
def test_invalid_continuation_sends_nothing(svc, option):
    oid = observe(svc)
    opts = follow(); opts.update(option)
    r = invoke(svc.tap, oid, button(svc), follow_up=opts, image='never')
    assert r.is_error and not svc.driver.events


@pytest.mark.parametrize('extras', [{'dry_run': True}, {'until': {'text': 'go'}}, {'settle_ms': 10}])
def test_conflicting_tap_options_rejected_before_input(svc, extras):
    oid = observe(svc)
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(), **extras)
    assert r.is_error and not svc.driver.events


def test_mcp_nested_schema_and_real_dispatch(svc, local_clock):
    async def check():
        async with Client(create_server(svc)) as client:
            ts = {t.name:t for t in (await client.list_tools()).tools}
            assert 'follow_up' in ts['tap'].input_schema['properties']
            assert 'back_sequence' in ts['press_key'].input_schema['properties']
            oid = observe(svc)
            opts = follow(return_back=backs()); opts['duration_seconds'] = 1
            r = await client.call_tool('tap', {'observation_id': oid, 'element_id': button(svc),
                                             'follow_up': opts, 'image': 'never', 'detail':'brief'})
            assert not r.is_error, r.structured_content
            assert r.structured_content['continuation']['status'] == 'completed'
    asyncio.run(check())


def test_final_capture_failure_preserves_all_sent_steps(svc, local_clock):
    oid = observe(svc)
    def broken(*a, **kw): raise OSError('capture unavailable')
    svc._observe = broken
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(return_back=backs()), image='never')
    assert r.is_error and r.structured_content['input_status'] == 'sent'
    assert r.structured_content['continuation']['completed_inputs'] == 3
    assert len(svc.driver.events) == 3 and svc.observation is None


def test_unknown_tap_does_not_start_timer_or_back(svc, local_clock):
    oid = observe(svc)
    svc.driver.fail = True
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(return_back=backs()), image='never')
    assert r.is_error and r.structured_content['input_status'] == 'unknown'
    assert 'continuation' not in r.structured_content and len(svc.driver.events) == 1


def test_completion_disappears_during_final_capture(svc, local_clock):
    oid = observe(svc)
    original = svc._observe
    local_clock.on_wait = lambda: setattr(svc.driver, 'xml', scene(outside='done'))
    def changed(*a, **kw):
        svc.driver.xml = scene(outside='gone')
        return original(*a, **kw)
    svc._observe = changed
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(completion={'text': 'done'}), image='never')
    assert r.is_error and r.structured_content['input_status'] == 'sent'
    assert r.structured_content['continuation']['status'] == 'interrupted'


def test_stop_between_backs_sends_only_first(svc, local_clock):
    oid = observe(svc)
    local_clock.on_wait = svc.cancel.set
    r = invoke(svc.press_key, oid, 'back', back_sequence=backs(), image='never')
    assert r.is_error and svc.driver.events == ['back']
    assert r.structured_content['continuation']['completed_inputs'] == 1


def test_elapsed_timing_and_back_accept_coherent_rotation(svc, local_clock):
    oid = observe(svc)
    original = svc.driver.header
    local_clock.on_wait = lambda: setattr(svc.driver, 'header', lambda: {
        **original(), 'width': 200, 'height': 100, 'rotation': 1})
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(return_back=backs()), image='never')
    assert not r.is_error and len(svc.driver.events) == 3
    assert r.structured_content['continuation']['timing']['timer_elapsed_ms'] == 15000


def test_back_accepts_rotation_between_keys(svc, local_clock):
    oid = observe(svc)
    original = svc.driver.header
    local_clock.on_wait = lambda: setattr(svc.driver, 'header', lambda: {
        **original(), 'width': 200, 'height': 100, 'rotation': 1})
    r = invoke(svc.press_key, oid, 'back', back_sequence=backs(), image='never')
    assert not r.is_error and svc.driver.events == ['back', 'back']


def test_elapsed_timer_rejects_resize_even_with_rotation(svc, local_clock):
    oid = observe(svc)
    original = svc.driver.header
    local_clock.on_wait = lambda: setattr(svc.driver, 'header', lambda: {
        **original(), 'width': 220, 'height': 100, 'rotation': 1})
    r = invoke(svc.tap, oid, button(svc), follow_up=follow(return_back=backs()), image='never')
    assert r.is_error and len(svc.driver.events) == 1


def test_expected_transition_during_tree_read_is_retried_locally(svc, local_clock):
    oid = observe(svc)
    original = svc.driver.hierarchy
    active = [False]
    execute = svc.driver.execute
    def sent(g):
        execute(g); active[0] = True
    def hierarchy():
        if active[0]:
            svc.driver.package = 'example.destination'; active[0] = False
        return original()
    svc.driver.execute, svc.driver.hierarchy = sent, hierarchy
    r = invoke(svc.tap, oid, button(svc), image='never', follow_up=follow(
        completion={'text':'done'}, return_back=backs()))
    assert not r.is_error and len(svc.driver.events) == 3
    assert r.structured_content['continuation']['timing']['timer_elapsed_ms'] >= 15000
    assert 'sequence_read_transition' in (svc.run_dir/'actions.jsonl').read_text()


def test_unexpected_transition_during_tree_read_stops_before_back(svc, local_clock):
    oid = observe(svc)
    original = svc.driver.hierarchy
    execute = svc.driver.execute
    active = [False]
    def sent(g): execute(g); active[0] = True
    def hierarchy():
        if active[0]: svc.driver.package = 'example.unexpected'
        return original()
    svc.driver.execute, svc.driver.hierarchy = sent, hierarchy
    r = invoke(svc.tap, oid, button(svc), image='never', follow_up=follow(
        completion={'text':'done'}, return_back=backs()))
    assert r.is_error and len(svc.driver.events) == 1


def test_continuous_allowed_transition_has_bounded_reads(svc, local_clock):
    oid = observe(svc)
    header = svc.observation.header
    count = [0]
    def changing():
        count[0] += 1
        return {**header, 'package': 'example.destination' if count[0] % 2 else 'example.calculator'}
    svc.driver.header = changing
    from mobile_use.service import MobileError
    with pytest.raises(MobileError, match='three continuation samples'):
        svc._sequence_check(header, backs()['allowed_packages'], tree=True, allow_rotation=True)
    assert count[0] == 6
