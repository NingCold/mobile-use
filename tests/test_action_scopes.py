"""Spatial relevance and caller-declared context, independent of app vocabulary."""
import asyncio
from types import SimpleNamespace

import pytest
from mcp import Client
from mobile_use import targets
from mobile_use.server import create_server, invoke
from test_targets import svc, observe, button, scene, node
from test_navigation import clock
from test_sequences import local_clock
from test_vision import visual, tap as visual_tap


def edge(**attrs):
    return node(rid='edge-window', bounds='[0,0][100,8]', **attrs)


def with_window(xml, window):
    return xml.replace('</hierarchy>', window+'</hierarchy>')


@pytest.mark.parametrize('kind', ['structured', 'coordinate', 'swipe', 'drag'])
def test_disjoint_edge_change_does_not_block_spatial_input(svc, kind):
    oid = observe(svc, 'always')
    svc.driver.xml = with_window(scene(), edge())
    if kind == 'structured':
        r = invoke(svc.tap, oid, button(svc), image='never')
    elif kind == 'coordinate':
        r = invoke(svc.tap, oid, x=65, y=130, visual_bounds=[55,120,75,140], image='never')
    else:
        r = invoke(svc.swipe, oid, 50,160,50,100, kind='drag' if kind=='drag' else 'browse', image='never')
    assert not r.is_error, r.structured_content
    assert len(svc.driver.events)==1 and r.structured_content['reads']['hierarchy']==2


def test_visual_relocation_also_ignores_disjoint_edge_change(visual):
    s, oid, change = visual
    change()
    original = s.driver.hierarchy
    s.driver.hierarchy = lambda: with_window(original(), edge())
    r = visual_tap(s, oid)
    assert not r.is_error and len(s.driver.events)==1, r.structured_content
    assert r.structured_content['reads']['screenshot']==2


@pytest.mark.parametrize('change', ['crossing', 'focused', 'modal', 'nested_modal'])
def test_spatial_scope_retains_crossing_focus_and_modal_guards(svc, change):
    oid = observe(svc)
    if change == 'crossing':
        svc.driver.xml = with_window(scene(), node(rid='overlay',bounds='[40,110][70,140]'))
    elif change == 'focused':
        svc.driver.xml = with_window(scene(), edge(focused='true'))
    elif change == 'modal':
        svc.driver.xml = with_window(scene(), edge(**{'class':'android.widget.PopupWindow'}))
    else:
        svc.driver.xml = scene(extra=edge(**{'class':'android.app.Dialog'}))
    r = invoke(svc.swipe,oid,50,160,50,100,image='never')
    assert r.is_error and r.structured_content['input_status']=='not_sent'
    assert r.structured_content['code']=='window_changed' and not svc.driver.events


def test_drag_checks_destination_path_beyond_its_source_pixel_patch(svc):
    oid = observe(svc,'always')
    svc.driver.xml = with_window(scene(),node(rid='path-cover',bounds='[45,40][60,70]'))
    r = invoke(svc.swipe,oid,50,160,50,30,kind='drag',image='never')
    assert r.is_error and r.structured_content['code']=='window_changed'
    assert not svc.driver.events


def test_scoped_topology_uses_both_frame_regions_and_ignores_distant_surfaces():
    a = targets.build_graph(scene(),100,200)
    moved = [(60,120,80,140)]
    targets.check_layout(a,a,[(60,60,80,80)],moved)
    distant = targets.build_graph(scene(extra=node(rid='sidebar',bounds='[0,0][30,200]')),100,200)
    targets.check_layout(a,distant,moved,moved)
    targets.check_layout(distant,a,moved,moved)  # Removal is irrelevant too.
    edge_tree = targets.build_graph(with_window(scene(),edge()),100,200)
    targets.check_layout(edge_tree,a,moved,moved)


def test_global_key_does_not_inherit_a_spatial_exemption(svc):
    oid = observe(svc)
    svc.driver.xml = with_window(scene(),edge())
    r = invoke(svc.press_key,oid,'back',image='never')
    assert r.is_error and not svc.driver.events


def card(timer='10 seconds', title='Item A', price='Price 10', label='Open', enabled='true',
         duplicate=False, row_id='card', cover=False):
    row = node(rid=row_id,bounds='[0,20][100,110]',children=
        node(title,'title','[0,20][45,35]')+node(price,'price','[0,40][45,55]')+
        node(timer,'status','[0,60][45,75]')+
        node(label,'open','[60,40][95,75]',clickable='true',enabled=enabled))
    extra = node('Overlay','cover','[55,35][100,80]',clickable='true') if cover else ''
    return '<hierarchy>'+node(rid='root',bounds='[0,0][100,200]',children=row+(row if duplicate else '')+extra)+'</hierarchy>'


def selector():
    return {'resource_id':'open','context_texts':['Item A','Price 10']}


@pytest.mark.parametrize('declared',[False,True])
def test_unlisted_card_status_changes_only_with_explicit_context(svc,declared):
    svc.driver.xml=card(); oid=observe(svc)
    svc.driver.xml=card(timer='Updated status')  # No timer-specific regex or resource ID rule.
    r=invoke(svc.tap,oid,target=selector() if declared else {'resource_id':'open'},image='never',detail='brief')
    assert r.is_error != declared and len(svc.driver.events)==int(declared),r.structured_content
    if declared:
        assert r.structured_content['validation']['context_binding']=='declared'


@pytest.mark.parametrize('change', [dict(title='Item B'),dict(price='Price 100'),dict(label='Pay'),
    dict(enabled='false'),dict(duplicate=True),dict(row_id='replacement'),dict(cover=True)])
def test_declared_context_keeps_identity_price_ambiguity_and_cover_guards(svc,change):
    svc.driver.xml=card(); oid=observe(svc)
    svc.driver.xml=card(timer='9 seconds',**change)
    r=invoke(svc.tap,oid,target=selector(),image='never')
    assert r.is_error and r.structured_content['input_status']=='not_sent'
    assert not svc.driver.events


def test_within_text_remains_bound_even_when_not_repeated_in_context_texts(svc):
    svc.driver.xml=card(); oid=observe(svc)
    svc.driver.xml=card(title='Item B')
    r=invoke(svc.tap,oid,target={'text':'Open','within_text':'Item A','context_texts':['Price 10']},image='never')
    assert r.is_error and not svc.driver.events


def test_declared_surrounding_context_does_not_unbind_target_own_content(svc):
    def clickable_card(**kwargs):
        return card(**kwargs).replace('resource-id="card"','resource-id="card" clickable="true"')
    svc.driver.xml=clickable_card(); oid=observe(svc)
    svc.driver.xml=clickable_card(label='Pay')
    r=invoke(svc.tap,oid,target={'resource_id':'card','context_texts':['Item A','Price 10']},image='never')
    assert r.is_error and not svc.driver.events


@pytest.mark.parametrize('route',['tap','navigate'])
def test_readiness_and_navigation_keep_declared_binding_across_timer_ticks(svc,clock,monkeypatch,route):
    import mobile_use.validation as validation
    monkeypatch.setattr(validation,'time',SimpleNamespace(monotonic=lambda:clock.now))
    svc.driver.xml=card(); oid=observe(svc)
    clock.on_wait=lambda:setattr(svc.driver,'xml',card(timer=str(clock.now)))
    if route=='tap':
        r=invoke(svc.tap,oid,target=selector(),readiness={'quiet_ms':600},image='never')
    else:
        r=invoke(svc.navigate,oid,{'allowed_packages':['example.calculator'],'steps':[
            {'kind':'tap','package':'example.calculator','target':selector()}]},image='never')
    assert not r.is_error and len(svc.driver.events)==1,r.structured_content
    assert clock.now==pytest.approx(100.6)
    assert 'screenshot' not in r.structured_content['reads']


def test_invalid_late_context_is_rejected_before_any_navigation_input(svc):
    oid=observe(svc)
    r=invoke(svc.navigate,oid,{'allowed_packages':['example.calculator'],'steps':[
        {'kind':'launch','package':'example.calculator'},
        {'kind':'tap','package':'example.calculator','target':{'text':'Open','context_texts':[]}}]},image='never')
    assert r.is_error and not svc.driver.events and r.structured_content['reads']=={}


def test_declared_context_roundtrips_through_mcp_and_reports_capability(svc):
    svc.driver.xml=card()
    async def run():
        async with Client(create_server(svc)) as client:
            status=(await client.call_tool('get_status',{})).structured_content
            assert status['capabilities']['declared_target_context'] and status['capabilities']['action_region_windows']
            assert status['limits']['target_context_texts']==8
            oid=(await client.call_tool('observe',{'image':'never'})).structured_content['observation']['observation_id']
            svc.driver.xml=card(timer='9 seconds')
            r=await client.call_tool('tap',{'observation_id':oid,'target':selector(),'image':'never'})
            assert not r.is_error and len(svc.driver.events)==1,r.structured_content
    asyncio.run(run())
