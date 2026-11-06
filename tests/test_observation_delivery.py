import asyncio
import json

import pytest

from mobile_use.service import MobileError
from mobile_use.server import create_server, invoke, pack
from mobile_use.state import inspect_tree, tree_diff
from test_mobile_use import service


def rich_tree(extra=""):
    return ('<hierarchy>' + ''.join(
        f'<node package="example.calculator" class="android.widget.Button" '
        f'resource-id="app:id/b{i}" text="Button {i}" clickable="true" enabled="true" '
        f'bounds="[10,{i * 50}][90,{i * 50 + 40}]"/>' for i in range(3)) + extra + '</hierarchy>')


def test_auto_and_cached_expansion_do_not_mix_snapshots(service):
    service.driver.hierarchy = lambda: rich_tree()
    first = invoke(service.observe)
    assert len(first.content) == 2
    second = invoke(service.observe)
    obs = second.structured_content['observation']
    assert len(second.content) == 1
    assert obs['image']['included'] is False and obs['image']['available']
    assert obs['summary']['base_observation_id'] == first.structured_content['observation']['observation_id']
    assert not obs['summary']['tree_changed']
    assert len(obs['elements']) == 3  # complete snapshot, not an empty diff
    assert 'class_name' not in obs['elements'][0]
    saved = service.observation
    def no_device_read(*args, **kwargs):
        raise AssertionError('Cached expansion must not touch the device')
    service.driver.header = service.driver.hierarchy = service.driver.screenshot = no_device_read
    expanded = invoke(service.get_observation, saved.id, image='always', detail='full')
    full = expanded.structured_content['observation']
    assert len(expanded.content) == 2 and service.observation is saved
    assert full['observation_id'] == obs['observation_id']
    assert full['capture'] == obs['capture']
    assert full['elements'][0]['class_name'] == 'android.widget.Button'
    without_image = pack({}, saved, image='never', detail='full')
    assert len(second.content[0].text.encode('utf-8')) < len(without_image.content[0].text.encode('utf-8'))


@pytest.mark.parametrize('policy,images', [('always', 1), ('never', 0), ('auto', 1)])
def test_image_policy_preserves_capture_checks(service, policy, images):
    original = service.driver.screenshot
    reads = []
    def screenshot():
        reads.append(1)
        return original()
    service.driver.screenshot = screenshot
    result = invoke(service.observe, image=policy)
    assert sum(c.type == 'image' for c in result.content) == images
    assert len(reads) == 1 and service.observation.png


@pytest.mark.parametrize('extra,reason', [
    ('<node class="android.webkit.WebView" bounds="[0,0][100,200]"/>', 'embedded_or_drawn_surface'),
    ('', 'sparse_accessibility_tree'),
])
def test_auto_keeps_images_for_incomplete_accessibility(service, extra, reason):
    if extra:
        service.driver.hierarchy = lambda: rich_tree(extra)
    service.observe()
    result = invoke(service.observe)
    assert len(result.content) == 2
    assert reason in result.structured_content['observation']['image_reasons']


def test_auto_includes_new_foreground_and_layout(service):
    service.driver.hierarchy = lambda: rich_tree()
    service.observe()
    service.driver.package = 'example.newapp'
    assert len(invoke(service.observe).content) == 2
    service.driver.hierarchy = lambda: rich_tree('<node class="android.widget.FrameLayout" bounds="[0,0][100,200]"/>')
    result = invoke(service.observe)
    assert 'layout_or_interaction_changed' in result.structured_content['observation']['image_reasons']


@pytest.mark.parametrize('image,detail', [('invalid','compact'), ('never','invalid')])
def test_bad_output_options_never_send_input(service, image, detail):
    obs = service.observe()
    result = invoke(service.tap, obs.id, 'e1', image=image, detail=detail)
    assert result.is_error and not service.driver.events


def test_cache_expansion_cannot_revive_stale_or_stopped_state(service):
    old = service.observe()
    new = service.observe()
    with pytest.raises(MobileError, match='Stale'):
        service.get_observation(old.id)
    service.stop()
    assert service.get_observation(new.id) is new
    with pytest.raises(MobileError, match='Stopped'):
        service.tap(new.id, 'e1')
    service.observe(resume=True)
    service.observation.created -= 181
    with pytest.raises(MobileError, match='expired'):
        service.get_observation(service.observation.id)


def flapping_screenshot(service):
    original = service.driver.screenshot
    def screenshot():
        service.driver.text += ' changed'
        return original()
    return original, screenshot


def test_partial_snapshot_readable_but_blocks_every_input(service):
    original, changing = flapping_screenshot(service)
    service.driver.screenshot = changing
    result = invoke(service.observe)
    obs = service.observation
    assert not result.is_error and len(result.content) == 2
    assert not obs.actionable and obs.consistency == 'inconsistent'
    assert obs.capture['attempts'] == 3 and obs.issues
    assert service.get_observation(obs.id) is obs
    calls = [lambda: service.tap(obs.id, 'e1'),
             lambda: service.swipe(obs.id, 50, 150, 50, 20),
             lambda: service.press_key(obs.id, 'back'),
             lambda: service.type_text(obs.id, 'text'),
             lambda: service.launch_app(obs.id, 'example.app')]
    for call in calls:
        with pytest.raises(MobileError) as err:
            call()
        assert err.value.details['input_status'] == 'not_sent'
    assert not service.driver.events
    service.driver.screenshot = original
    recovered = service.observe()
    assert recovered.actionable and 'capture_inconsistent_or_recovering' in recovered.image_reasons
    service.tap(recovered.id, 'e1')
    assert len(service.driver.events) == 1


def test_sent_input_with_partial_result_is_not_replayed(service):
    obs = service.observe()
    original, changing = flapping_screenshot(service)
    execute = service.driver.execute
    def once(gesture):
        execute(gesture)
        service.driver.screenshot = changing
    service.driver.execute = once
    result = invoke(service.tap, obs.id, 'e1', image='never')
    data = result.structured_content
    assert not result.is_error
    assert data['input_status'] == 'sent' and data['observation_status'] == 'inconsistent'
    assert not data['observation']['actionable'] and len(service.driver.events) == 1
    assert len(result.content) == 1
    assert 'verification_incomplete' in (service.run_dir/'actions.jsonl').read_text()
    service.driver.screenshot = original
    service.observe()
    assert len(service.driver.events) == 1


@pytest.mark.parametrize('change', ['foreground', 'size'])
def test_header_or_image_size_race_never_authorizes_input(service, change):
    original = service.driver.screenshot
    def screenshot():
        png, size = original()
        if change == 'foreground':
            service.driver.package = ('example.other' if service.driver.package != 'example.other' else 'example.calculator')
        else:
            size = (101, 200)
        return png, size
    service.driver.screenshot = screenshot
    obs = service.observe()
    assert not obs.actionable and obs.capture['attempts'] == 3
    with pytest.raises(MobileError, match='inconsistent'):
        service.tap(obs.id, x=30, y=30)
    assert not service.driver.events


@pytest.mark.parametrize('change', ['target_text', 'target_bounds', 'modal', 'foreground'])
def test_suppressed_image_keeps_existing_preflight_guards(service, change):
    result = invoke(service.observe, image='never')
    obs_id = result.structured_content['observation']['observation_id']
    if change == 'target_text':
        service.driver.text = 'Pay'
    elif change == 'target_bounds':
        original = service.driver.hierarchy
        service.driver.hierarchy = lambda: original().replace('[10,20][90,100]', '[10,100][90,190]')
    elif change == 'modal':
        original = service.driver.hierarchy
        service.driver.hierarchy = lambda: original().replace('</hierarchy>', '<node class="android.widget.FrameLayout" bounds="[0,0][100,200]"/></hierarchy>')
    else:
        service.driver.package = 'example.other'
    result = invoke(service.tap, obs_id, 'e1', image='never')
    assert result.is_error and result.structured_content['input_status'] == 'not_sent'
    assert not service.driver.events


def test_diff_insertion_and_deletion_do_not_cascade():
    original = rich_tree()
    inserted = original.replace('<hierarchy>', '<hierarchy><node class="android.widget.TextView" text="private new text" bounds="[0,155][90,170]"/>')
    a,b = inspect_tree(original,100,200),inspect_tree(inserted,100,200)
    forward,backward = tree_diff(a,b),tree_diff(b,a)
    assert forward['added_nodes'] == backward['removed_nodes'] == 1
    assert forward['changed_nodes'] == backward['changed_nodes'] == 1
    assert not forward['changes'] and not backward['changes']
    assert 'private new text' not in json.dumps(forward)


def test_diff_same_count_replacement_counts_add_and_remove():
    old = '<hierarchy><node class="android.widget.TextView" text="private" bounds="[0,0][90,40]"/></hierarchy>'
    new = '<hierarchy><node class="android.widget.Button" text="secret" bounds="[0,50][90,90]"/></hierarchy>'
    diff = tree_diff(inspect_tree(old,100,200),inspect_tree(new,100,200))
    assert diff['added_nodes'] == diff['removed_nodes'] == 1
    assert diff['unmatched_may_include_replacements']
    assert 'private' not in json.dumps(diff) and 'secret' not in json.dumps(diff)


def test_diff_matches_unique_resource_through_text_and_position_change():
    before = rich_tree()
    after = before.replace('Button 1','private replacement').replace('[10,50][90,90]', '[10,55][90,95]')
    diff = tree_diff(inspect_tree(before,100,200),inspect_tree(after,100,200))
    assert diff['changed_nodes'] == 1 and not diff['added_nodes'] and not diff['removed_nodes']
    assert set(diff['changes'][0]['fields']) == {'text','bounds'}
    assert 'private replacement' not in json.dumps(diff)


def test_mcp_options_and_cached_image_roundtrip(service):
    from mcp import Client
    async def check():
        async with Client(create_server(service)) as client:
            tools = (await client.list_tools()).tools
            assert 'get_observation' in [t.name for t in tools]
            first = await client.call_tool('observe', {'image':'never','detail':'compact'})
            data = first.structured_content['observation']
            assert len(first.content) == 1 and data['image']['available']
            full = await client.call_tool('get_observation', {'observation_id':data['observation_id'], 'image':'always'})
            assert any(c.type == 'image' for c in full.content)
            assert full.structured_content['observation']['observation_id'] == data['observation_id']
            assert full.structured_content['observation']['detail'] == 'full'
            failed = await client.call_tool('tap', {'observation_id':data['observation_id'],'element_id':'e1','image':'invalid'})
            assert failed.is_error and not service.driver.events
    asyncio.run(check())
