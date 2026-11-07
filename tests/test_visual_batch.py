"""Same-frame facts, guarded IDs and unchanged legacy query contracts."""
import asyncio
import copy
import io

import pytest
from PIL import Image, ImageDraw, ImageFont
from mcp import Client

from mobile_use.server import invoke, create_server
from mobile_use.vision import Label, LocalVision
from test_vision import visual


BATCH = {
    'conditions': [{'name': 'ready', 'text': 'Ready', 'search_bounds': [0, 0, 100, 40]},
                   {'name': 'done', 'text': 'Finished'}],
    'candidates': [{'text': 'Open', 'context_text': 'Report A'},
                   {'text': 'Read', 'context_text': 'Report A'}],
    'require': ['ready'],
}


@pytest.fixture
def scene(visual):
    s, _, change = visual

    def sample(y=70, state='Ready', score=.99, duplicate=False, competitor=False):
        png = change(y)
        labels = list(s._vision.values[png])
        im = Image.open(io.BytesIO(png)); draw = ImageDraw.Draw(im)
        draw.text((4, 4), state, fill='black')
        labels.append(Label(state, score, (4, 4, 75, 18)))
        if duplicate:
            draw.text((4, 24), state, fill='black')
            labels.append(Label(state, .99, (4, 24, 75, 38)))
        if competitor:
            draw.text((30, 160), 'Read', fill='black')
            labels.append(Label('Read', .99, (30, 160, 70, 176)))
        b = io.BytesIO(); im.save(b, format='PNG'); result = b.getvalue()
        s._vision.values[result] = labels
        s.driver.screenshot = lambda: (result, (100, 200))
        return result

    sample()
    oid = invoke(s.observe, image='always').structured_content['observation']['observation_id']
    return s, oid, sample


def query(s, oid, batch=BATCH, **kwargs):
    return invoke(s.get_observation, oid, visual_query_batch=copy.deepcopy(batch),
                  image='never', detail='compact', **kwargs)


def candidate(result):
    return result.structured_content['observation']['visual_batch']['target']['candidates'][0]['id']


def test_one_read_returns_independent_facts_and_guarded_target_without_device_reads(scene):
    s, oid, sample = scene
    status = s.get_status()
    assert status['capabilities']['same_frame_conditions'] and status['limits']['visual_conditions'] == 8
    calls = []; read = s._vision.read
    s._vision.read = lambda png, texts=(), region=None: (calls.append((texts, region)) or read(png, texts, region))
    age = s.observation.created
    r = query(s, oid)
    assert not r.is_error, r.structured_content
    data = r.structured_content['observation']['visual_batch']
    assert [(c['name'], c['status']) for c in data['conditions']] == [('ready', 'matched'), ('done', 'missing')]
    assert len(calls) == 1 and set(calls[0][0]) == {'Ready', 'Finished', 'Open', 'Read', 'Report A'}
    assert r.structured_content['reads'] == {} and s.observation.created == age
    assert not s.driver.events and 'visual_candidates' not in r.structured_content['observation']
    cid = candidate(r)
    sample(120)
    tapped = invoke(s.tap, oid, visual_candidate_id=cid, image='never')
    assert not tapped.is_error, tapped.structured_content
    assert len(s.driver.events) == 1 and tapped.structured_content['validation']['visual']['required_conditions'] == ['ready']
    assert tapped.structured_content['reads']['screenshot'] == 2
    stale = invoke(s.tap, s.observation.id, visual_candidate_id=cid, image='never')
    assert stale.is_error and len(s.driver.events) == 1


@pytest.mark.parametrize('state,score,duplicate,status', [
    ('Finished', .99, False, 'missing'), ('Ready', .5, False, 'low_confidence'),
    ('Ready', .99, True, 'ambiguous')])
def test_unmet_condition_withholds_action_id_but_keeps_other_results(scene, state, score, duplicate, status):
    s, _, sample = scene
    sample(state=state, score=score, duplicate=duplicate)
    oid = invoke(s.observe, image='always').structured_content['observation']['observation_id']
    r = query(s, oid); batch = r.structured_content['observation']['visual_batch']
    assert batch['conditions'][0]['status'] == status and batch['unmet'] == ['ready']
    assert batch['target']['status'] == 'blocked' and 'id' not in batch['target']['candidates'][0]
    assert not s.driver.events


@pytest.mark.parametrize('change,code', [
    ({'state': 'Finished'}, 'visual_condition_changed'),
    ({'duplicate': True}, 'visual_condition_changed'),
    ({'competitor': True}, 'visual_target_ambiguous')])
def test_confirmation_rechecks_conditions_and_competitors_outside_unchanged_button(scene, change, code):
    s, oid, sample = scene
    cid = candidate(query(s, oid))
    first, second = sample(120), sample(120, **change)
    # No target movement. Only an independent condition or competitor changes.
    frames = iter([first, second])
    s.driver.screenshot = lambda: (next(frames), (100, 200))
    r = invoke(s.tap, oid, visual_candidate_id=cid, image='never')
    assert r.is_error and r.structured_content['code'] == code, r.structured_content
    assert r.structured_content['input_status'] == 'not_sent' and not s.driver.events


def test_read_only_and_legacy_ids_do_not_inherit_or_erase_guard_contract(scene):
    s, oid, sample = scene
    guarded = candidate(query(s, oid))
    plain = invoke(s.get_observation, oid, visual_query_any_of={'candidates': BATCH['candidates']}, image='never')
    plain_id = plain.structured_content['observation']['visual_candidates']['candidates'][0]['id']
    single = invoke(s.get_observation, oid, visual_query=BATCH['candidates'][0], image='never')
    single_id = single.structured_content['observation']['visual_candidates']['candidates'][0]['id']
    assert len({guarded, plain_id, single_id}) == 3
    assert candidate(query(s, oid)) == guarded
    # A batch may have one candidate; the existing alternatives API still requires two.
    assert candidate(query(s, oid, {'conditions': BATCH['conditions'], 'candidates': BATCH['candidates'][:1]}))
    assert invoke(s.get_observation, oid, visual_query_any_of={'candidates': BATCH['candidates'][:1]}, image='never').is_error
    r = query(s, oid, {'conditions': BATCH['conditions']})
    assert 'target' not in r.structured_content['observation']['visual_batch']
    assert 'visual_candidates' not in r.structured_content['observation']
    sample(120, state='Finished')
    # The later unguarded query did not replace the earlier guarded ID's premise.
    bad = invoke(s.tap, oid, visual_candidate_id=guarded, image='never')
    assert bad.is_error and bad.structured_content['code'] == 'visual_condition_changed'
    assert not s.driver.events


def test_batch_limits_scopes_and_exclusivity_fail_before_ocr(scene):
    s, oid, _ = scene
    invalid = [
        {'conditions': []}, {'conditions': [{'name': str(i), 'text': 'Ready'} for i in range(9)]},
        {'conditions': [BATCH['conditions'][0]] * 2},
        {**BATCH, 'require': ['unknown']}, {**BATCH, 'require': ['ready', 'ready']},
        {**BATCH, 'candidates': []}, {**BATCH, 'candidates': BATCH['candidates'] * 7},
        {**BATCH, 'candidates': [{'text': 'Open'}]},
        {'conditions': [{'name': 'ready', 'text': 'Ready', 'search_bounds': []}]},
        {**BATCH, 'search_bounds': [0, 40, 100, 200]},
    ]
    def unexpected_read(*args, **kwargs):
        pytest.fail('Invalid batch must fail before OCR')
    s._vision.read = unexpected_read
    for batch in invalid:
        r = query(s, oid, batch)
        assert r.is_error and r.structured_content['reads'] == {}
    assert query(s, oid, visual_query=BATCH['candidates'][0]).is_error
    assert query(s, oid, visual_query_any_of={'candidates': BATCH['candidates']}).is_error
    assert not s.driver.events


def test_real_ocr_single_detection_and_mcp_dispatch(scene):
    s, _, _ = scene
    im = Image.new('RGB', (600, 900), 'white'); draw = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=40)
    for y, text in [(100, 'Ready'), (300, 'Report A'), (500, 'Open')]:
        draw.text((80, y), text, fill='black', font=font)
    b = io.BytesIO(); im.save(b, format='PNG'); png = b.getvalue()
    header = s.driver.header()
    s.driver.header = lambda: {**header, 'width': 600, 'height': 900}
    s.driver.hierarchy = lambda: '<hierarchy><node class="android.view.View" bounds="[0,0][600,900]" /></hierarchy>'
    s.driver.screenshot = lambda: (png, (600, 900))
    oid = invoke(s.observe, image='always').structured_content['observation']['observation_id']
    s._vision = LocalVision()
    batch = copy.deepcopy(BATCH); batch['conditions'][0]['search_bounds'] = [0, 0, 600, 240]

    async def run():
        async with Client(create_server(s)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert 'visual_query_batch' in tools['get_observation'].input_schema['properties']
            r = await client.call_tool('get_observation', {'observation_id': oid, 'visual_query_batch': batch,
                                                         'image': 'never', 'detail': 'compact'})
            assert not r.is_error, r.structured_content
            result = r.structured_content['observation']['visual_batch']
            assert result['conditions'][0]['status'] == 'matched' and result['target']['candidates'][0]['id']
            assert r.structured_content['vision']['detections'] == 1 and r.structured_content['reads'] == {}
    asyncio.run(run())
