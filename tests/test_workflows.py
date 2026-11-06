import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from mobile_use.options import Focus, WaitCondition
from mobile_use.server import create_server, invoke
from test_mobile_use import service
from test_observation_delivery import rich_tree


@pytest.fixture
def clock(service, monkeypatch):
    import mobile_use.service as module
    clock = SimpleNamespace(now=100.0, on_wait=lambda: None)
    def wait(delay):
        clock.now += delay
        clock.on_wait()
        return service.cancel.is_set()
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock.now, sleep=wait))
    monkeypatch.setattr(service.cancel, 'wait', wait)
    return clock


def test_action_wait_sends_once_and_captures_final_state_only(service, clock):
    obs = service.observe()
    original = service.driver.execute
    def execute(gesture):
        original(gesture)
        service.driver.text = 'loading'
    service.driver.execute = execute
    clock.on_wait = lambda: setattr(service.driver, 'text', 'ready')
    screenshots = []
    capture = service.driver.screenshot
    service.driver.screenshot = lambda: (screenshots.append(1), capture())[1]
    result = invoke(service.tap, obs.id, 'e1', until={'text': 'ready'}, image='never')
    data = result.structured_content
    assert not result.is_error and data['input_status'] == 'sent'
    assert data['wait']['status'] == 'matched' and data['wait']['polls'] >= 2
    assert data['observation']['elements'][0]['text'] == 'ready'
    assert len(service.driver.events) == len(screenshots) == 1
    assert service._after_input_wait is None


def test_action_wait_timeout_keeps_sent_status_and_latest_snapshot(service, clock):
    obs = service.observe()
    result = invoke(service.tap, obs.id, 'e1', until={'text': 'absent'}, timeout_seconds=0.5)
    data = result.structured_content
    assert result.is_error and data['input_status'] == 'sent'
    assert data['wait']['status'] == 'timeout'
    assert service.observation.id == data['observation']['observation_id'] != obs.id
    assert data['observation']['actionable'] and len(service.driver.events) == 1
    assert 'recovery' not in data and service._after_input_wait is None


def test_unknown_dispatch_does_not_start_wait_or_retry(service, clock):
    obs = service.observe()
    service.driver.fail = True
    result = invoke(service.tap, obs.id, 'e1', until={'text': '7'})
    data = result.structured_content
    assert result.is_error and data['input_status'] == 'unknown'
    assert 'wait' not in data and 'observation' not in data
    assert service.observation is None and len(service.driver.events) == 1


@pytest.mark.parametrize('options', [
    {'until': {}}, {'until': {'text': ''}}, {'until': {'package': 'not a package'}},
    {'until': {'unexpected': 'ready'}}, {'timeout_seconds': float('nan')},
    {'timeout_seconds': 31}, {'timeout_seconds': 0},
    {'focus': {'bounds': [10, 20, 0, 0]}}, {'focus': {'max_elements': 0, 'text': '7'}},
    {'focus': {'text': ' '}}, {'focus': {}},
    {'until': {'text': 'ready'}, 'dry_run': True},
])
def test_invalid_options_never_read_or_send(service, options):
    obs = service.observe()
    def no_read():
        raise AssertionError('Invalid options must fail before device reads')
    service.driver.header = no_read
    result = invoke(service.tap, obs.id, 'e1', **options)
    assert result.is_error and not service.driver.events
    assert service.observation is obs


@pytest.mark.parametrize('change', ['target', 'foreground', 'stale', 'expired'])
def test_preflight_rejection_returns_new_snapshot_without_replay(service, change):
    obs = service.observe()
    oid = obs.id
    if change == 'target':
        service.driver.text = 'changed'
    elif change == 'foreground':
        service.driver.package = 'example.other'
    elif change == 'stale':
        oid = 'old'
    else:
        obs.created -= 181
    result = invoke(service.tap, oid, 'e1', image='never')
    data = result.structured_content
    assert result.is_error and data['input_status'] == 'not_sent'
    assert data['recovery']['status'] == 'observed' and not data['recovery']['input_replayed']
    assert data['observation']['observation_id'] == service.observation.id != obs.id
    assert not service.driver.events
    # Agent can inspect the returned new state and choose an action directly.
    result = invoke(service.tap, service.observation.id, 'e1')
    assert not result.is_error and len(service.driver.events) == 1


def test_rejection_read_failure_preserves_original_failure(service):
    obs = service.observe()
    service.driver.text = 'changed'
    def failure():
        raise OSError('capture unavailable')
    service.driver.screenshot = failure
    result = invoke(service.tap, obs.id, 'e1')
    data = result.structured_content
    assert result.is_error and data['input_status'] == 'not_sent'
    assert data['recovery']['status'] == 'unavailable' and 'observation' not in data
    assert not service.driver.events


def test_stopped_input_does_not_recapture_or_clear_stop(service):
    obs = service.observe()
    service.stop()
    def no_read():
        raise AssertionError('Stopped input should not recover')
    service.driver.header = no_read
    result = invoke(service.tap, obs.id, 'e1')
    assert result.is_error and 'recovery' not in result.structured_content
    assert service.cancel.is_set() and not service.driver.events


def test_focus_filters_delivery_without_renumbering_or_weakening_validation(service):
    service.driver.hierarchy = lambda: rich_tree()
    result = invoke(service.observe, focus={'text': 'Button 1'}, image='never')
    obs = result.structured_content['observation']
    assert obs['element_count'] == 3 and obs['returned_element_count'] == 1
    assert obs['elements_filtered'] and obs['elements'][0]['id'] == 'e2'
    assert len(service.observation.elements) == 3
    expanded = invoke(service.get_observation, obs['observation_id'], image='never')
    assert len(expanded.structured_content['observation']['elements']) == 3
    assert expanded.structured_content['observation']['capture'] == obs['capture']
    service.driver.hierarchy = lambda: rich_tree('<node class="android.widget.FrameLayout" bounds="[0,0][100,200]"/>')
    rejected = invoke(service.tap, obs['observation_id'], 'e2', focus={'text': 'Button 1'}, image='never')
    assert rejected.is_error and not service.driver.events


def test_region_focus_limit_and_empty_result_are_explicit(service):
    service.driver.hierarchy = lambda: rich_tree()
    data = invoke(service.observe, focus=Focus(bounds=[0, 0, 100, 200], max_elements=1)).structured_content['observation']
    assert data['focus']['matched_elements'] == 3 and data['focus']['matches_truncated']
    empty = invoke(service.get_observation, data['observation_id'], focus={'text': 'absent'}).structured_content['observation']
    assert empty['elements_filtered'] and not empty['elements'] and empty['element_count'] == 3
    assert not empty['tree_diagnostics']['empty']


def test_wait_rechecks_condition_after_screenshot(service, clock):
    service.driver.text = 'ready'
    original = service.driver.screenshot
    def disappears():
        service.driver.text = 'gone'
        return original()
    service.driver.screenshot = disappears
    result = invoke(service.wait_for_state, None, 'ready', None, 0, 1)
    assert result.is_error and result.structured_content['wait']['status'] == 'timeout'
    assert result.structured_content['observation']['elements'][0]['text'] == 'gone'


def test_wait_requires_text_and_package_to_match(service, clock):
    result = invoke(service.wait_for_state, None, '7', 'example.other', 0, 0.5)
    assert result.is_error and result.structured_content['wait']['status'] == 'timeout'


def test_dwell_is_timed_from_fresh_check_not_snapshot_age(service, clock):
    obs = service.observe()
    clock.now += 100
    started = clock.now
    result = invoke(service.wait_for_state, obs.id, None, obs.header['package'], 15, 20, image='never')
    data = result.structured_content
    assert not result.is_error and data['wait']['status'] == 'matched'
    assert 15 <= clock.now - started < 16
    assert data['wait']['sampled_duration_ms'] >= 15000
    assert data['observation']['observation_id'] != obs.id and not service.driver.events


@pytest.mark.parametrize('change', ['package', 'screen', 'rotation', 'text'])
def test_dwell_interrupts_on_changed_monitored_state(service, clock, change):
    obs = service.observe()
    original = service.driver.header
    def change_state():
        if change == 'package':
            service.driver.package = 'example.other'
        elif change == 'text':
            service.driver.text = 'changed'
        else:
            service.driver.header = lambda: {**original(), ('screen_on' if change == 'screen' else 'rotation'): (False if change == 'screen' else 1)}
    clock.on_wait = change_state
    result = invoke(service.wait_for_state, obs.id, '7', obs.header['package'], 15, 20)
    data = result.structured_content
    assert result.is_error and data['wait']['status'] == 'interrupted'
    assert data['wait']['sampled_duration_ms'] < 15000
    assert not service.driver.events


@pytest.mark.parametrize('args', [(None, None, 'example.calculator', 1, 2),
    ('old', None, 'example.calculator', 1, 2), (None, '7', None, 1, 2),
    (None, None, 'example.calculator', 4, 2), (None, None, None, 0, 2)])
def test_invalid_dwell_cannot_credit_time(service, args):
    result = invoke(service.wait_for_state, *args)
    assert result.is_error and 'wait' not in result.structured_content and not service.driver.events


def test_wait_failure_does_not_log_text_values(service, clock):
    result = invoke(service.wait_for_state, None, 'private sentinel', None, 0, 0.5)
    assert result.is_error
    assert 'private sentinel' not in (service.run_dir / 'actions.jsonl').read_text(encoding='utf-8')


def test_stop_interrupts_inflight_wait_without_waiting_for_service_lock(service):
    active = threading.Event()
    original = service.driver.header
    def header():
        active.set()
        return original()
    service.driver.header = header
    results = []
    thread = threading.Thread(target=lambda: results.append(invoke(service.wait_for_state, None, 'missing', None, 0, 30)))
    thread.start()
    assert active.wait(2)
    stopped = invoke(service.stop)
    thread.join(timeout=2)
    assert not thread.is_alive() and stopped.structured_content['stopped']
    assert results[0].structured_content['wait']['status'] == 'cancelled'
    assert service.cancel.is_set() and not service.driver.events


def test_status_no_reads_and_metrics_are_separate_from_payload(service):
    def no_read():
        raise AssertionError('Status should not read device')
    original = service.driver.header
    service.driver.header = no_read
    state = invoke(service.get_status).structured_content
    assert state['capabilities']['action_until'] and state['connected']
    service.driver.header = original
    observed = invoke(service.observe, image='never').structured_content
    assert observed['timings']['capture_ms'] <= observed['timings']['service_ms'] + 1
    assert observed['timings']['previous_call_gap_ms'] is not None
    assert observed['call_id'] != state['call_id']
    events = [json.loads(s) for s in (service.run_dir/'actions.jsonl').read_text().splitlines()]
    assert any(e['event'] == 'call_finished' and e['tool'] == 'observe' for e in events)


def test_mcp_new_schemas_and_single_call_workflow(service):
    from mcp import Client
    async def check():
        async with Client(create_server(service)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {'wait_for_state', 'get_status'} <= tools.keys()
            assert 'until' in tools['tap'].input_schema['properties']
            first = await client.call_tool('observe', {'image': 'never', 'focus': {'text': '7'}})
            oid = first.structured_content['observation']['observation_id']
            result = await client.call_tool('tap', {'observation_id': oid, 'element_id': 'e1',
                'until': {'text': '7', 'package': 'example.calculator'}, 'focus': {'text': '7'}, 'image': 'never'})
            assert not result.is_error and result.structured_content['wait']['status'] == 'matched'
            assert len(service.driver.events) == 1
            invalid = await client.call_tool('tap', {'observation_id': result.structured_content['observation']['observation_id'],
                'element_id': 'e1', 'until': {'text': '7', 'unknown': True}})
            assert invalid.is_error and len(service.driver.events) == 1
    asyncio.run(check())


@pytest.mark.parametrize('tool', ['swipe', 'press_key', 'type_text', 'launch_app'])
def test_each_input_supports_until_once(service, clock, tool):
    service.driver.focused = True
    service.driver.key = lambda key: service.driver.events.append(key)
    service.driver.launch = lambda package: (service.driver.events.append(package), setattr(service.driver, 'package', package))
    set_text = service.driver.set_text
    service.driver.set_text = lambda value: (service.driver.events.append('text'), set_text(value))
    obs = service.observe()
    calls = {'swipe': (20, 150, 20, 120), 'press_key': ('back',),
             'type_text': ('7',), 'launch_app': ('example.calculator',)}
    result = invoke(getattr(service, tool), obs.id, *calls[tool], until={'text': '7'})
    assert not result.is_error and result.structured_content['wait']['status'] == 'matched'
    assert len(service.driver.events) == 1


def test_mcp_stop_can_cancel_wait_in_parallel(service):
    from mcp import Client
    async def check():
        active = threading.Event()
        original = service.driver.header
        service.driver.header = lambda: (active.set(), original())[1]
        async with Client(create_server(service)) as client:
            waiting = asyncio.create_task(client.call_tool('wait_for_state', {'text': 'missing', 'timeout_seconds': 30}))
            assert await asyncio.to_thread(active.wait, 2)
            stopped = await asyncio.wait_for(client.call_tool('stop', {}), 2)
            result = await asyncio.wait_for(waiting, 2)
            assert stopped.structured_content['stopped']
            assert result.is_error and result.structured_content['wait']['status'] == 'cancelled'
    asyncio.run(check())


def test_dwell_final_capture_change_is_not_success(service, clock):
    obs = service.observe()
    original = service.driver.screenshot
    service.driver.screenshot = lambda: (setattr(service.driver, 'package', 'example.other'), original())[1]
    result = invoke(service.wait_for_state, obs.id, None, obs.header['package'], 1, 2)
    assert result.is_error and result.structured_content['wait']['status'] == 'interrupted'
    assert not service.driver.events


def test_inconsistent_wait_snapshot_cannot_be_a_match(service, clock):
    original = service.driver.screenshot
    def changing():
        service.driver.text += ' changed'
        return original()
    service.driver.screenshot = changing
    result = invoke(service.wait_for_state, None, '7', None, 0, 0.5)
    assert result.is_error and result.structured_content['wait']['status'] == 'timeout'
    assert not result.structured_content['observation']['actionable']
