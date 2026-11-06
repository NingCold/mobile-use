"""Decision evidence, bounded cache delivery and no-extra-observation contracts."""
import asyncio
import base64
from dataclasses import replace
import io
import json

from mcp import Client
from PIL import Image
import pytest

from mobile_use.server import create_server, invoke, pack
from test_mobile_use import service
from test_targets import svc, observe, button, scene
from test_workflows import clock


def cached_list(svc, count=1000):
    observe(svc)
    obs = svc.observation
    seed = obs.elements[0]
    obs.elements = [replace(seed, id=f'e{i+1}', text=f'Record {i:04}',
                            description='', clickable=False, enabled=i % 2 == 0)
                    for i in range(count)]
    obs.tree = None  # Delivery fixture; never used to authorize input.
    return obs


def forbid_reads(svc):
    def forbidden(*args, **kwargs):
        raise AssertionError('Cache delivery must not read or input')
    svc.driver.header = svc.driver.hierarchy = svc.driver.screenshot = forbidden
    svc.driver.execute = forbidden


def test_brief_keeps_current_evidence_and_protocol_compatibility(svc):
    observe(svc)
    obs = svc.observation
    compact = pack({}, obs, image='never').structured_content['observation']
    result = pack({}, obs, image='never', detail='brief')
    brief = result.structured_content['observation']
    assert json.loads(result.content[0].text) == json.loads(json.dumps(result.structured_content))
    for key in ('elements', 'issues', 'observation_id', 'actionable', 'consistency', 'package', 'width', 'height'):
        assert brief[key] == compact[key]
    assert any(not e.get('clickable') and e.get('text') for e in brief['elements'])
    assert brief['capture']['tree_at'] == compact['capture']['tree_at']
    assert not {'summary', 'tree_diagnostics', 'stability_scope'} & brief.keys()


def test_brief_action_has_one_input_and_cached_source_diagnostics(svc):
    oid = observe(svc)
    result = invoke(svc.tap, oid, button(svc), image='never', detail='brief')
    d = result.structured_content
    assert not result.is_error and d['input_status'] == 'sent'
    assert d['observation']['observation_id'] != oid and len(svc.driver.events) == 1
    assert not {'action', 'timings', 'reads'} & d.keys()
    saved = svc.observation
    created = saved.created
    forbid_reads(svc)
    expanded = invoke(svc.get_observation, saved.id, image='never', detail='full').structured_content
    origin = expanded['source_call']
    assert origin['call_id'] == d['call_id'] and origin['action']['kind'] == 'tap'
    assert origin['reads']['hierarchy'] == 2  # preflight + post-input snapshot
    assert expanded['reads'] == {} and svc.observation is saved and saved.created == created
    again = invoke(svc.get_observation, saved.id, image='never', detail='full').structured_content
    assert again['source_call'] == origin and 'source_call' not in origin
    calls = [json.loads(s) for s in (svc.run_dir/'actions.jsonl').read_text().splitlines()]
    logged = next(c for c in calls if c.get('call_id') == d['call_id'] and c['event'] == 'call_finished')
    assert logged['diagnostics']['action'] == json.loads(json.dumps(origin['action']))


def test_brief_rejection_contains_recovery_without_replay(svc):
    oid = observe(svc)
    eid = button(svc)
    svc.driver.xml = scene(title='Replacement')
    result = invoke(svc.tap, oid, eid, image='never', detail='brief')
    d = result.structured_content
    assert result.is_error and d['input_status'] == 'not_sent' and d['error'] and d['code']
    assert d['stage'] == 'preflight' and d['recovery']['status'] == 'observed'
    assert d['observation']['observation_id'] != oid and not svc.driver.events


@pytest.mark.parametrize('failure', ['unknown', 'sent'])
def test_brief_does_not_replay_unknown_or_post_input_failure(svc, failure):
    oid = observe(svc)
    if failure == 'unknown':
        svc.driver.fail = True
    else:
        original = svc.driver.execute
        def execute(gesture):
            original(gesture)
            svc.driver.hierarchy = lambda: (_ for _ in ()).throw(RuntimeError('read failed'))
        svc.driver.execute = execute
    r = invoke(svc.tap, oid, button(svc), image='never', detail='brief')
    assert r.is_error and r.structured_content['input_status'] == failure
    assert r.structured_content['error'] and len(svc.driver.events) == 1


def test_brief_preserves_dry_run_and_does_not_overwrite_origin(svc):
    oid = observe(svc)
    original = dict(svc.observation.source_call)
    r = invoke(svc.tap, oid, button(svc), dry_run=True, image='never', detail='brief')
    assert r.structured_content['dry_run'] and r.structured_content['action']['points']
    assert not svc.driver.events and svc.observation.source_call == original


def test_brief_preserves_legacy_diagnostic_motion(service):
    obs = service.observe()
    obs.actionable = False
    obs.consistency = 'inconsistent'
    obs.issues = [{'reason': 'changed window'}]
    obs.unstable_regions = [{'bounds': [10, 20, 90, 100]}]
    d = pack({'input_status': 'sent', 'observation_status': 'inconsistent',
              'result': 'Reobserve without replay', 'action': {'kind': 'tap'}},
             obs, detail='brief', image='never').structured_content
    assert d['result'] and d['action']
    assert not d['observation']['actionable'] and d['observation']['issues'] == obs.issues
    assert d['observation']['unstable_regions'] == obs.unstable_regions


def test_brief_wait_has_final_evidence_without_extra_client_observe(service, clock):
    obs = service.observe()
    original = service.driver.execute
    def execute(gesture):
        original(gesture)
        service.driver.text = 'loading'
    service.driver.execute = execute
    clock.on_wait = lambda: setattr(service.driver, 'text', 'ready')
    r = invoke(service.tap, obs.id, 'e1', until={'text': 'ready'}, detail='brief', image='never')
    assert not r.is_error and r.structured_content['wait']['status'] == 'matched'
    assert r.structured_content['observation']['elements'][0]['text'] == 'ready'
    assert len(service.driver.events) == 1


def test_brief_wait_timeout_preserves_sent_and_snapshot(service, clock):
    obs = service.observe()
    r = invoke(service.tap, obs.id, 'e1', until={'text': 'missing'}, timeout_seconds=.5,
               image='never', detail='brief')
    assert r.is_error and r.structured_content['input_status'] == 'sent'
    assert r.structured_content['wait']['status'] == 'timeout'
    assert r.structured_content['observation']['actionable'] and len(service.driver.events) == 1


def test_cache_pages_reach_every_element_without_reads_or_renewal(svc):
    obs = cached_list(svc)
    created, capture = obs.created, dict(obs.capture)
    forbid_reads(svc)
    ids, cursor = [], None
    for page in range(4):
        r = invoke(svc.get_observation, obs.id, image='never', detail='brief', cursor=cursor)
        assert not r.is_error
        o = r.structured_content['observation']
        assert o['observation_id'] == obs.id and o['element_count'] == 1000
        assert o['pagination']['offset'] == 250 * page
        ids += [e['id'] for e in o['elements']]
        cursor = o['pagination']['next_cursor']
    assert cursor is None and ids == [e.id for e in obs.elements]
    assert obs.created == created and obs.capture == capture


def test_focus_searches_past_250_before_truncating_labels(svc):
    obs = cached_list(svc)
    obs.elements[999] = replace(obs.elements[999], text='长' * 300 + 'needle')
    forbid_reads(svc)
    r = invoke(svc.get_observation, obs.id, image='never', detail='brief', focus={'text': 'needle'})
    o = r.structured_content['observation']
    assert o['elements'][0]['id'] == 'e1000' and o['elements'][0]['text_truncated']
    assert o['focus']['matched_elements'] == 1 and not o['elements_truncated']
    full = invoke(svc.get_observation, obs.id, image='never', detail='full', focus={'text': 'needle'})
    assert full.structured_content['observation']['elements'][0]['text'].endswith('needle')


def test_focus_pagination_retains_original_ids(svc):
    obs = cached_list(svc, 340)
    forbid_reads(svc)
    args = dict(image='never', detail='full', focus={'text': 'Record 03', 'max_elements': 15}, limit=20)
    first = invoke(svc.get_observation, obs.id, **args).structured_content['observation']
    assert first['elements'][0]['id'] == 'e301' and len(first['elements']) == 15
    assert first['focus']['matched_elements'] == 40
    next_page = invoke(svc.get_observation, obs.id, cursor=first['pagination']['next_cursor'], **args)
    assert next_page.structured_content['observation']['elements'][0]['id'] == 'e316'


@pytest.mark.parametrize('options', [{'limit': 0}, {'limit': 251}, {'limit': True},
                                     {'cursor': 'bad'}, {'cursor': ''}, {'cursor': 'e'*513}])
def test_bad_pagination_is_a_tool_error_without_reads(svc, options):
    obs = cached_list(svc, 10)
    forbid_reads(svc)
    r = invoke(svc.get_observation, obs.id, image='never', detail='brief', **options)
    assert r.is_error and 'error' in r.structured_content


def test_cursor_cannot_cross_query_or_observation(svc):
    obs = cached_list(svc, 300)
    first = invoke(svc.get_observation, obs.id, image='never').structured_content['observation']
    cursor = first['pagination']['next_cursor']
    different = invoke(svc.get_observation, obs.id, image='never', cursor=cursor, focus={'text': 'Record'})
    assert different.is_error
    newer = observe(svc)
    r = invoke(svc.get_observation, newer, image='never', cursor=cursor)
    assert r.is_error and r.structured_content['reads'] == {}


def test_out_of_range_cursor_returns_structured_error_without_reads(svc):
    obs = cached_list(svc, 300)
    page = invoke(svc.get_observation, obs.id, image='never').structured_content['observation']
    token = json.loads(base64.urlsafe_b64decode(page['pagination']['next_cursor']))
    token[2] = 1000
    cursor = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
    forbid_reads(svc)
    r = invoke(svc.get_observation, obs.id, image='never', detail='brief', cursor=cursor)
    assert r.is_error and 'offset' in r.structured_content['error']
    assert 'observation' not in r.structured_content and svc.observation is obs
    r = invoke(svc.get_observation, obs.id, image='never', cursor=cursor)
    assert r.is_error and r.structured_content['reads'] == {}


def test_pagination_never_changes_input_or_expiration_checks(svc):
    oid = observe(svc)
    r = invoke(svc.tap, oid, button(svc), limit=1, image='never', detail='brief')
    assert r.is_error and r.structured_content['input_status'] == 'not_sent' and not svc.driver.events
    svc.observation.created -= 181
    assert invoke(svc.get_observation, oid, limit=1, image='never').is_error


def test_input_invalidates_cached_pages_and_focus_cannot_hide_target_change(svc):
    oid = observe(svc)
    page = invoke(svc.get_observation, oid, image='never', detail='brief', limit=1).structured_content['observation']
    cursor = page['pagination']['next_cursor']
    r = invoke(svc.tap, oid, button(svc), image='never', detail='brief', focus={'text':'go'})
    assert not r.is_error and len(svc.driver.events) == 1
    assert invoke(svc.get_observation, oid, image='never', cursor=cursor).is_error
    fresh_id = r.structured_content['observation']['observation_id']
    svc.driver.xml = scene(title='Changed context')
    rejected = invoke(svc.tap, fresh_id, button(svc), image='never', detail='brief', focus={'text':'go'})
    assert rejected.is_error and rejected.structured_content['input_status'] == 'not_sent'
    assert len(svc.driver.events) == 1


def test_brief_cache_does_not_clear_stop_or_lose_disabled_labels(svc):
    obs = cached_list(svc, 10)
    svc.stop()
    r = invoke(svc.get_observation, obs.id, image='never', detail='brief')
    assert not r.is_error and svc.cancel.is_set()
    elements = r.structured_content['observation']['elements']
    assert len(elements) == 10 and any(e['enabled'] is False for e in elements)
    assert all(e['text'] and not e.get('clickable') for e in elements)
    assert invoke(svc.tap, obs.id, 'e1', image='never', detail='brief').is_error
    assert not svc.driver.events


def test_brief_preview_preserves_original_evidence(svc):
    oid = observe(svc)
    obs = svc.observation
    b = io.BytesIO()
    Image.new('RGB', (1080, 2400)).save(b, format='PNG')
    obs.png = b.getvalue()
    obs.header.update(width=1080, height=2400)
    r = pack({}, obs, detail='brief', image='always')
    assert r.structured_content['observation']['image']['delivered_size'] == [576, 1280]
    assert r.structured_content['observation']['image']['input_coordinates'] == 'original_pixels'
    assert r.structured_content['observation']['width'] == 1080
    assert obs.png == b.getvalue() and obs.id == oid


def test_brief_and_pagination_protocol_schema(svc):
    cached_list(svc, 300)
    async def run():
        async with Client(create_server(svc)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert 'brief' in tools['tap'].input_schema['properties']['detail']['enum']
            assert {'cursor', 'limit'} <= tools['get_observation'].input_schema['properties'].keys()
            r = await client.call_tool('get_observation', {'observation_id':svc.observation.id,
                                       'detail':'brief','image':'never','limit':10})
            assert not r.is_error and len(r.structured_content['observation']['elements']) == 10
    asyncio.run(run())
