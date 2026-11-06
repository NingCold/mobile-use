"""Capture/round-trip regressions with actual tool envelopes and counted reads."""
import asyncio
import io

import pytest
from PIL import Image, ImageDraw
from mcp import Client

from mobile_use.server import create_server, invoke
from test_targets import svc, scene, node, button


def observe(s, **kwargs):
    r = invoke(s.observe, **kwargs)
    assert not r.is_error, r.structured_content
    return r


def test_auto_reads_tree_first_and_stops_capturing_rich_native_page(svc):
    first = observe(svc)
    assert first.structured_content['reads'] == {'header': 2, 'hierarchy': 1, 'screenshot': 1}
    second = observe(svc)
    data = second.structured_content
    assert data['reads'] == {'header': 2, 'hierarchy': 1}
    assert len(second.content) == 1 and not data['observation']['image']['available']
    assert data['observation']['capture']['image_at'] is None
    assert not svc.observation.png  # never carry the first image into the new ID
    assert len(data['observation']['elements']) == 3


def test_native_action_and_until_return_usable_state_without_extra_observe(svc):
    observe(svc)
    r = invoke(svc.tap, svc.observation.id, button(svc), until={'text':'go'})
    assert not r.is_error and r.structured_content['input_status'] == 'sent'
    assert r.structured_content['wait']['status'] == 'matched'
    assert r.structured_content['reads'] == {'header': 4, 'hierarchy': 2}
    assert r.structured_content['observation']['actionable']
    assert len(svc.driver.events) == 1 and len(r.content) == 1


@pytest.mark.parametrize('extra,reason', [
    (node(bounds='[0,0][100,200]', **{'class':'android.webkit.WebView'}), 'unrepresented_web_content'),
    (node('label', bounds='[0,0][100,200]', **{'class':'android.view.SurfaceView'}), 'drawn_surface'),
    (node(bounds='[0,0][100,200]', **{'class':'android.view.TextureView'}), 'drawn_surface'),
    (node(bounds='[0,0][100,100]', **{'class':'android.view.View'}), 'unrepresented_content'),
    (node(bounds='[2,2][8,8]', **{'class':'org.example.Canvas'}), 'unrepresented_content'),
    (node(bounds='[2,2][80,80]', **{'class':'android.widget.ImageView'}), 'unrepresented_content'),
    (node(rid='icon', bounds='[2,2][80,80]', clickable='true'), 'unlabelled_control'),
])
def test_unknown_content_is_captured_in_same_call_even_when_tree_is_rich(svc, extra, reason):
    svc.driver.xml = scene(extra=extra)
    observe(svc)
    r = observe(svc)
    assert r.structured_content['reads']['screenshot'] == 1
    assert len(r.content) == 2 and reason in svc.observation.image_reasons


@pytest.mark.parametrize('attrs', [{'class':'android.widget.ImageView'}, {'clickable':'true'}])
def test_small_native_unknown_region_is_disclosed_without_repeated_capture(svc, attrs):
    svc.driver.xml = scene(extra=node(rid='icon', bounds='[2,2][8,8]', **attrs))
    first = observe(svc, detail='brief')
    assert len(first.content) == 2
    second = observe(svc, detail='brief')
    assert svc.observation.source_call['reads'].get('screenshot', 0) == 0
    info = second.structured_content['observation']['image']
    assert info['visual_regions'] == [[2,2,8,8]]
    assert info['visual_region_count'] == 1 and not info['available']
    # A missing current image still prevents coordinate input; old pixels are
    # never borrowed into the new observation or used to authorize the icon.
    r = invoke(svc.tap, svc.observation.id, x=5, y=5)
    assert r.is_error and r.structured_content['code'] == 'image_required'
    assert len(r.content) == 2 and not svc.driver.events


def test_new_or_moved_small_unknown_region_gets_image(svc):
    observe(svc)
    svc.driver.xml = scene(extra=node(bounds='[2,2][8,8]', clickable='true'))
    assert len(observe(svc).content) == 2
    assert len(observe(svc).content) == 1
    svc.driver.xml = svc.driver.xml.replace('[2,2][8,8]', '[12,2][18,8]')
    assert len(observe(svc).content) == 2


@pytest.mark.parametrize('count,size', [(9,6), (5,20)])
def test_many_or_large_total_unknown_regions_remain_visual(svc, count, size):
    svc.driver.xml = scene(extra=''.join(node(rid=str(i), bounds=f'[0,0][{size},{size}]',
                                               clickable='true') for i in range(count)))
    observe(svc)
    r = observe(svc, detail='brief')
    assert len(r.content) == 2
    assert 'extensive_unlabelled_regions' in svc.observation.image_reasons
    assert r.structured_content['observation']['image']['visual_region_count'] == count
    assert len(r.structured_content['observation']['image']['visual_regions']) == min(8,count)


def test_small_icons_do_not_make_an_unreadable_page_text_only(svc):
    svc.driver.xml = '<hierarchy>'+''.join(node(bounds=f'[{i*10},0][{i*10+6},6]',
            clickable='true') for i in range(4))+'</hierarchy>'
    observe(svc)
    assert len(observe(svc).content) == 2


def test_readable_action_with_local_unknown_region_needs_no_extra_observe(svc):
    svc.driver.xml = scene(extra=node(rid='icon', bounds='[2,2][8,8]', clickable='true'))
    observe(svc)
    r = invoke(svc.tap, svc.observation.id, button(svc), until={'text':'go'})
    assert not r.is_error and r.structured_content['input_status'] == 'sent'
    assert r.structured_content['reads'].get('screenshot',0) == 0
    assert r.structured_content['observation']['actionable']
    assert len(svc.driver.events) == 1


@pytest.mark.parametrize('xml', ['<hierarchy/>', '<hierarchy>'+node('loading')+'</hierarchy>'])
def test_sparse_and_empty_trees_always_get_new_image(svc, xml):
    svc.driver.xml = xml
    observe(svc)
    r = observe(svc)
    assert len(r.content) == 2 and r.structured_content['reads']['screenshot'] == 1


def test_semantic_webview_does_not_force_images_but_drawn_descendant_does(svc):
    def web(extra=''):
        return '<hierarchy>'+node(bounds='[0,0][100,200]', **{'class':'android.webkit.WebView'},
            children=node('Documents', bounds='[0,0][100,30]')+
                     node('Open', bounds='[0,40][80,70]', clickable='true')+
                     node('Recent files', bounds='[0,80][100,100]')+extra)+'</hierarchy>'
    svc.driver.xml = web()
    observe(svc)
    assert observe(svc).structured_content['reads'].get('screenshot', 0) == 0
    svc.driver.xml = web(node(bounds='[0,110][100,190]', **{'class':'android.view.SurfaceView'}))
    assert len(observe(svc).content) == 2


@pytest.mark.parametrize('attrs', [
    {'bounds':'[0,220][100,300]'}, {'bounds':'[0,0][100,200]', 'visible-to-user':'false'},
    {'bounds':'[0,0][100,10]', 'package':'com.android.systemui'},
])
def test_hidden_offscreen_and_filtered_system_content_does_not_force_capture(svc, attrs):
    svc.driver.xml = scene(extra=node(**{'class':'android.view.TextureView', **attrs}))
    observe(svc)
    assert observe(svc).structured_content['reads'].get('screenshot', 0) == 0


@pytest.mark.parametrize('change', ['foreground', 'rotation', 'modal', 'surface'])
def test_new_context_still_gets_image(svc, change):
    observe(svc)
    if change == 'foreground':
        svc.driver.package = 'org.example.other'
    elif change == 'rotation':
        header = svc.driver.header
        svc.driver.header = lambda: {**header(), 'rotation':1}
    elif change == 'modal':
        svc.driver.xml = svc.driver.xml.replace('</hierarchy>', node('Permission', bounds='[0,0][100,200]', package='android')+'</hierarchy>')
    else:
        svc.driver.xml = scene().replace('[0,0][100,200]', '[0,0][100,180]')
    r = observe(svc)
    assert len(r.content) == 2 and r.structured_content['reads']['screenshot'] == 1


def test_dynamic_text_and_small_control_relocation_do_not_force_image(svc):
    observe(svc)
    svc.driver.xml = scene(outside='timer 999').replace('[50,45][95,80]', '[48,45][93,80]')
    assert observe(svc).structured_content['reads'].get('screenshot', 0) == 0


def test_cache_auto_and_dry_run_do_not_redeliver_image(svc):
    observe(svc)
    saved = svc.observation
    for policy, images in [('auto',0), ('never',0), ('always',1)]:
        r = invoke(svc.get_observation, saved.id, image=policy)
        assert not r.is_error and r.structured_content['reads'] == {}
        assert len(r.content) == 1+images and svc.observation is saved
    r = invoke(svc.tap, saved.id, button(svc), dry_run=True)
    assert not r.is_error and len(r.content) == 1 and not svc.driver.events


def test_uncaptured_image_requires_new_id_not_cache_mutation(svc):
    observe(svc)
    observe(svc)
    saved = svc.observation
    r = invoke(svc.get_observation, saved.id, image='always')
    assert r.is_error and r.structured_content['reads'] == {} and svc.observation is saved
    fresh = observe(svc, image='always')
    assert svc.observation.id != saved.id and len(fresh.content) == 2


def test_visual_input_preflight_and_result_keep_fresh_images(svc):
    observe(svc, image='always')
    r = invoke(svc.tap, svc.observation.id, x=60, y=60)
    assert not r.is_error and r.structured_content['reads']['screenshot'] == 2
    assert len(r.content) == 2 and 'visual_action' in svc.observation.image_reasons
    # It is a per-action decision, not a sticky mode for future structured reads.
    assert observe(svc).structured_content['reads'].get('screenshot',0) == 0


def test_visual_failure_auto_returns_recovery_image_without_replay(svc):
    observe(svc, image='never')
    r = invoke(svc.tap, svc.observation.id, x=60, y=60)
    assert r.is_error and r.structured_content['code'] == 'image_required'
    assert r.structured_content['input_status'] == 'not_sent'
    assert len(r.content) == 2 and r.structured_content['reads']['screenshot'] == 1
    assert 'visual_recovery' in svc.observation.image_reasons and not svc.driver.events


def test_never_skips_post_image_but_not_visual_preflight(svc):
    observe(svc, image='always')
    r = invoke(svc.tap, svc.observation.id, x=60, y=60, image='never')
    assert not r.is_error and r.structured_content['reads']['screenshot'] == 1
    assert len(r.content) == 1 and not svc.observation.png


def test_changed_local_pixels_reject_with_fresh_recovery_image(svc):
    observe(svc, image='always')
    def screenshot():
        im = Image.new('RGB', (100,200))
        ImageDraw.Draw(im).rectangle((50,50,70,70), fill='white')
        b = io.BytesIO(); im.save(b, format='PNG')
        return b.getvalue(), im.size
    svc.driver.screenshot = screenshot
    r = invoke(svc.tap, svc.observation.id, x=60, y=60)
    assert r.is_error and r.structured_content['code'] == 'visual_changed'
    assert len(r.content) == 2 and not svc.driver.events


def test_header_race_after_skipped_image_retries_reads_and_captures(svc):
    observe(svc)
    calls = 0
    header = svc.driver.header
    def change():
        nonlocal calls
        calls += 1
        if calls == 2:
            svc.driver.package = 'org.example.other'
        return header()
    svc.driver.header = change
    r = observe(svc)
    assert svc.observation.actionable and svc.observation.capture['attempts'] == 2
    assert r.structured_content['reads']['screenshot'] == 1 and len(r.content) == 2


def test_text_wait_still_checks_condition_after_captured_image(svc):
    observe(svc, image='never')
    original = svc.driver.screenshot
    def change():
        svc.driver.xml = scene(button='removed')
        return original()
    svc.driver.screenshot = change
    # Match the public wrapper's positional forwarding; invoke's timeout option
    # configures action-until, whereas this function owns its wait timeout.
    r = invoke(svc.wait_for_state, None, 'go', None, 0, .05, image='always')
    assert r.is_error and r.structured_content['wait']['status'] == 'timeout'
    assert not svc.driver.events


def test_mcp_cache_default_is_text_and_capabilities_are_explicit(svc):
    observe(svc)
    async def run():
        async with Client(create_server(svc)) as client:
            tools = {t.name:t for t in (await client.list_tools()).tools}
            assert tools['get_observation'].input_schema['properties']['image']['default'] == 'auto'
            r = await client.call_tool('get_observation', {'observation_id':svc.observation.id})
            assert not r.is_error and len(r.content) == 1 and r.structured_content['reads'] == {}
            r = await client.call_tool('get_status', {})
            assert r.structured_content['capabilities']['tree_first_images']
            assert r.structured_content['capabilities']['cache_auto_text_only']
    asyncio.run(run())
