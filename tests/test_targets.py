"""Target mode regressions: changed scenery vs changed intent, and bounded reads."""
import io
import pytest
from PIL import Image, ImageDraw
from mobile_use.service import MobileService, MobileError
from mobile_use.server import invoke, create_server
from test_mobile_use import FakeDriver

def node(text='', rid='', bounds='[10,20][90,80]', children='', **attrs):
    attrs = {'package':'example.calculator','class':'android.view.View','text':text,
             'resource-id':rid,'bounds':bounds,'enabled':'true',**attrs}
    from xml.sax.saxutils import quoteattr
    return '<node '+ ' '.join(k+'='+quoteattr(v) for k,v in attrs.items())+'>'+children+'</node>'

def scene(title='task A', button='go', y=20, extra='', outside='timer 1', enabled='true', duplicate=False):
    row = node(rid='row',bounds=f'[0,{y}][100,{y+65}]',children=
               node(title,bounds=f'[0,{y}][40,{y+20}]')+
               node(button,'button',f'[50,{y+25}][95,{y+60}]',clickable='true',enabled=enabled))
    return '<hierarchy>'+node(rid='root',bounds='[0,0][100,200]',children=
        row+(row if duplicate else '')+node(outside,bounds='[0,170][100,195]')+extra)+'</hierarchy>'

@pytest.fixture
def svc(tmp_path):
    s=MobileService(tmp_path,driver_factory=FakeDriver)
    s.connect('test-device')
    s.driver.xml=scene()
    s.driver.hierarchy=lambda:s.driver.xml
    s.driver.key=lambda k:s.driver.events.append(k)
    s.driver.launch=lambda p:(s.driver.events.append(p),setattr(s.driver,'package',p))
    yield s
    s.disconnect()

def observe(s, image='never'):
    r=invoke(s.observe,image=image)
    assert not r.is_error
    return r.structured_content['observation']['observation_id']

def button(s):
    return next(e.id for e in s.observation.elements if e.text=='go')

def test_true_text_only_single_tree(svc):
    r=invoke(svc.observe,image='never')
    assert r.structured_content['reads']=={'header':2,'hierarchy':1}
    assert not r.structured_content['observation']['image']['available']
    assert len(r.content)==1
    assert svc.observation.stable is None

def test_no_cached_new_image_mix(svc):
    oid=observe(svc)
    r=invoke(svc.get_observation,oid,image='always')
    assert r.is_error and not r.structured_content.get('observation')
    assert r.structured_content['reads']=={}
    assert svc.observation.id==oid

def test_unrelated_timer_and_relocated_target(svc):
    oid=observe(svc)
    eid=button(svc)
    svc.driver.xml=scene(y=70,outside='timer 999')
    r=invoke(svc.tap,oid,eid,seed=1,image='never')
    assert not r.is_error, r.structured_content
    data=r.structured_content
    assert data['validation']['status']=='relocated'
    x,y=svc.driver.events[0].points[0]
    assert 50<=x<95 and 95<=y<130
    assert data['reads']['hierarchy']==2
    assert 'screenshot' not in data['reads']

@pytest.mark.parametrize('changes,code',[
    ({'title':'task B'},'target_changed'),
    ({'button':'buy'},'target_changed'),
    ({'duplicate':True},'target_ambiguous'),
    ({'enabled':'false'},'target_disabled'),
    ({'extra':node('dialog','modal','[0,0][100,180]',clickable='true')},'target_obscured'),
])
def test_changed_intent_or_cover_blocks_without_input(svc,changes,code):
    oid=observe(svc)
    eid=button(svc)
    svc.driver.xml=scene(**changes)
    r=invoke(svc.tap,oid,eid,image='never')
    assert r.is_error and r.structured_content['code']==code, r.structured_content
    assert r.structured_content['input_status']=='not_sent'
    assert r.structured_content['recovery']['input_replayed'] is False
    assert not svc.driver.events

def test_selector_requires_observed_unique_scope(svc):
    oid=observe(svc)
    r=invoke(svc.tap,oid,target={'text':'go','within_text':'task A'},image='never')
    assert not r.is_error
    assert len(svc.driver.events)==1

def test_own_window_resize_does_not_break_semantic_relocation(svc):
    svc.driver.xml=scene().replace('[0,0][100,200]', '[0,0][100,180]')
    oid=observe(svc); eid=button(svc)
    svc.driver.xml=scene(y=50)
    r=invoke(svc.tap,oid,eid,image='never')
    assert not r.is_error and r.structured_content['validation']['status']=='relocated'

def test_disappearing_launch_background_allows_relocation(svc):
    svc.driver.xml=scene().replace('<hierarchy>', '<hierarchy>'+node(rid='launcher',bounds='[0,0][100,200]',package='launcher'))
    oid=observe(svc); eid=button(svc)
    svc.driver.xml=scene(y=50)
    r=invoke(svc.tap,oid,eid,image='never')
    assert not r.is_error

def test_selector_cannot_invent_unobserved_label(svc):
    oid=observe(svc)
    r=invoke(svc.tap,oid,target={'text':'absent'},image='never')
    assert r.is_error and not svc.driver.events

def test_foreground_change_blocks_tap_but_explicit_launch_works(svc):
    oid=observe(svc)
    svc.driver.package='other.app'
    r=invoke(svc.tap,oid,button(svc),image='never')
    assert r.is_error and not svc.driver.events
    oid=observe(svc)
    svc.driver.xml=scene(title='changed')
    r=invoke(svc.launch_app,oid,'example.calculator',image='never')
    assert not r.is_error
    assert r.structured_content['reads']['hierarchy']==1

@pytest.mark.parametrize('kind',['swipe','back'])
def test_navigation_tolerates_dynamic_text(svc,kind):
    oid=observe(svc)
    svc.driver.xml=scene(outside='new feed',title='different feed item')
    r=invoke(svc.swipe,oid,40,160,40,30,image='never') if kind=='swipe' else invoke(svc.press_key,oid,'back',image='never')
    assert not r.is_error, r.structured_content
    assert len(svc.driver.events)==1

def test_new_system_window_blocks_navigation(svc):
    oid=observe(svc)
    svc.driver.xml=scene().replace('</hierarchy>',node('permission','dialog','[0,0][100,200]',package='android')+'</hierarchy>')
    r=invoke(svc.swipe,oid,40,160,40,30,image='never')
    assert r.is_error and not svc.driver.events

def test_visual_needs_original_image(svc):
    oid=observe(svc)
    r=invoke(svc.tap,oid,x=60,y=60,image='never')
    assert r.is_error and r.structured_content['code']=='image_required'

@pytest.mark.parametrize('inside',[True,False])
def test_visual_local_pixels(svc,inside):
    oid=observe(svc,'always')
    def screenshot():
        im=Image.new('RGB',(100,200))
        ImageDraw.Draw(im).rectangle((50,50,70,70) if inside else (0,180,100,190), fill='white')
        b=io.BytesIO(); im.save(b,format='PNG')
        return b.getvalue(),im.size
    svc.driver.screenshot=screenshot
    r=invoke(svc.tap,oid,x=60,y=60,image='auto' if inside else 'never')
    assert r.is_error==inside
    assert len(svc.driver.events)==(0 if inside else 1)
    if inside:
        assert r.structured_content['reads']['screenshot'] == 1
        assert r.structured_content['recovery']['reused_validation_image'] is True
        assert svc.observation.png == screenshot()[0]
        assert r.structured_content['observation']['image']['included'] is True


def test_visual_recovery_does_not_reuse_frame_after_foreground_transition(svc):
    oid = observe(svc, 'always')
    original = svc._state
    calls = 0
    def state():
        nonlocal calls
        calls += 1
        if calls == 2:
            svc.driver.package = 'example.destination'
        return original()
    svc._state = state
    def screenshot():
        im = Image.new('RGB', (100,200), 'white')
        b = io.BytesIO(); im.save(b, format='PNG')
        return b.getvalue(), im.size
    svc.driver.screenshot = screenshot
    r = invoke(svc.tap, oid, x=60, y=60, image='auto')
    assert r.is_error and not svc.driver.events
    assert r.structured_content['reads']['screenshot'] == 2
    assert not r.structured_content['recovery'].get('reused_validation_image')
    assert svc.observation.header['package'] == 'example.destination'

def test_unknown_input_never_replayed(svc):
    oid=observe(svc)
    svc.driver.fail=True
    r=invoke(svc.tap,oid,button(svc),image='never')
    assert r.is_error and r.structured_content['input_status']=='unknown'
    assert len(svc.driver.events)==1 and svc.observation is None

def test_sent_capture_failure_never_replayed(svc):
    oid=observe(svc)
    execute=svc.driver.execute
    def fail_capture(g):
        execute(g)
        svc.driver.hierarchy=lambda:(_ for _ in ()).throw(RuntimeError('capture failed'))
    svc.driver.execute=fail_capture
    r=invoke(svc.tap,oid,button(svc),image='never')
    assert r.is_error and r.structured_content['input_status']=='sent'
    assert len(svc.driver.events)==1

def test_dynamic_capture_does_not_loop(svc):
    count=0
    def moving():
        nonlocal count
        count+=1
        return scene(outside=str(count))
    svc.driver.hierarchy=moving
    observe(svc,'always')
    assert count==1 and svc.observation.actionable

def test_hidden_ancestor_blocks_target(svc):
    oid=observe(svc)
    eid=button(svc)
    svc.driver.xml=scene().replace('resource-id="row"', 'resource-id="row" visible-to-user="false"')
    r=invoke(svc.tap,oid,eid,image='never')
    assert r.is_error and not svc.driver.events

def test_existing_same_window_cover_blocks_target(svc):
    svc.driver.xml=scene(extra=node('dialog','modal','[0,0][100,180]',clickable='true'))
    oid=observe(svc)
    r=invoke(svc.tap,oid,button(svc),image='never')
    assert r.is_error and r.structured_content['code']=='target_obscured' and not svc.driver.events

def test_cover_guard_uses_clipped_actual_hit_area(svc):
    svc.driver.xml=scene().replace('[50,45][95,80]','[80,45][160,80]')
    oid=observe(svc); eid=button(svc)
    svc.driver.xml=scene(extra=node('cover','modal','[80,45][100,80]',clickable='true')).replace('[50,45][95,80]','[80,45][160,80]')
    r=invoke(svc.tap,oid,eid,image='never')
    assert r.is_error and r.structured_content['code']=='target_obscured' and not svc.driver.events

def test_disabled_ancestor_blocks_enabled_label(svc):
    oid=observe(svc); eid=button(svc)
    svc.driver.xml=scene().replace('resource-id="row" bounds="[0,20][100,85]" enabled="true"',
                                 'resource-id="row" bounds="[0,20][100,85]" enabled="false"')
    r=invoke(svc.tap,oid,eid,image='never')
    assert r.is_error and r.structured_content['code']=='target_disabled' and not svc.driver.events

def test_focus_switch_blocks_typing(svc):
    def edit(focus):
        return '<hierarchy>'+node(rid='root',bounds='[0,0][100,200]',children=node(
            'hello','field',focused=focus,**{'class':'android.widget.EditText'}))+'</hierarchy>'
    svc.driver.xml=edit('true'); oid=observe(svc)
    svc.driver.xml=edit('false')
    r=invoke(svc.type_text,oid,'world',image='never')
    assert r.is_error and r.structured_content['code']=='focus_changed'

def test_semantic_price_context_blocks_same_button(svc):
    svc.driver.xml=scene(title='price 10'); oid=observe(svc); eid=button(svc)
    svc.driver.xml=scene(title='price 100')
    r=invoke(svc.tap,oid,eid,image='never')
    assert r.is_error and not svc.driver.events

def test_clickable_parent_binds_its_own_item_content(svc):
    svc.driver.xml=scene().replace('resource-id="row"','resource-id="row" clickable="true"')
    oid=observe(svc)
    eid=next(e.id for e in svc.observation.elements if e.resource_id=='row')
    svc.driver.xml=scene(title='replacement item').replace('resource-id="row"','resource-id="row" clickable="true"')
    r=invoke(svc.tap,oid,eid,image='never')
    assert r.is_error and not svc.driver.events

def test_new_schema_works_through_mcp(svc):
    import asyncio
    from mcp import Client
    async def run():
        async with Client(create_server(svc)) as client:
            tools={t.name:t for t in (await client.list_tools()).tools}
            assert 'target' in tools['tap'].input_schema['properties']
            assert 'container_id' in tools['swipe'].input_schema['properties']
            first=await client.call_tool('observe',{'image':'never'})
            oid=first.structured_content['observation']['observation_id']
            result=await client.call_tool('tap',{'observation_id':oid,'target':{'text':'go','within_text':'task A'},'image':'never','until':{'text':'go'}})
            assert not result.is_error and result.structured_content['reads']['hierarchy']==2
            assert len(svc.driver.events)==1
    asyncio.run(run())

def test_wait_matches_without_global_stability(svc):
    count=0
    def moving():
        nonlocal count
        count+=1
        return scene(outside=str(count))
    svc.driver.hierarchy=moving
    r=invoke(svc.wait_for_state,text='go',timeout_seconds=1,image='never')
    assert not r.is_error and r.structured_content['wait']['status']=='matched'

def test_header_only_dwell_reads_one_final_tree(svc):
    oid=observe(svc)
    r=invoke(svc.wait_for_state,oid,None,'example.calculator',.01,.5,image='never')
    assert not r.is_error
    assert r.structured_content['reads']['hierarchy']==1
    assert 'screenshot' not in r.structured_content['reads']

def test_text_disappearing_during_wait_image_does_not_match(svc):
    capture=svc.driver.screenshot
    def change():
        svc.driver.xml=scene(button='absent')
        return capture()
    svc.driver.screenshot=change
    r=invoke(svc.wait_for_state,None,'go',None,0,.05,image='always')
    assert r.is_error and r.structured_content['wait']['status']=='timeout'
    assert not any(e['text']=='go' for e in r.structured_content['observation']['elements'] if 'text' in e)

def test_unique_anonymous_focused_field_can_be_filled(svc):
    svc.driver.xml='<hierarchy>'+node(rid='root',bounds='[0,0][100,200]',children=node(
        focused='true',**{'class':'android.widget.EditText'}))+'</hierarchy>'
    oid=observe(svc)
    r=invoke(svc.type_text,oid,'中文',image='never')
    assert not r.is_error and svc.driver.text=='中文'

def test_moving_scroll_container_blocks_old_path(svc):
    svc.driver.xml=scene().replace('resource-id="row"','resource-id="row" scrollable="true"')
    oid=observe(svc)
    eid=next(e.id for e in svc.observation.elements if e.scrollable)
    svc.driver.xml=scene(y=60).replace('resource-id="row"','resource-id="row" scrollable="true"')
    r=invoke(svc.swipe,oid,20,75,20,25,container_id=eid,image='never')
    assert r.is_error and not svc.driver.events

def test_drag_requires_visual_evidence(svc):
    oid=observe(svc)
    r=invoke(svc.swipe,oid,20,75,20,25,kind='drag',image='never')
    assert r.is_error and r.structured_content['code']=='image_required'
    assert not svc.driver.events

def test_compact_context_truncation_does_not_weaken_validation(svc):
    extra=''.join(node('fact'+str(i),'fact'+str(i),bounds='[0,20][40,40]') for i in range(9))
    # Use one row with many sibling facts beside the go button.
    svc.driver.xml='<hierarchy>'+node(rid='root',bounds='[0,0][100,200]',children=node(
        rid='row',bounds='[0,20][100,90]',children=extra+node('go','button','[50,40][90,70]',clickable='true')))+'</hierarchy>'
    r=invoke(svc.observe,image='never')
    e=next(e for e in r.structured_content['observation']['elements'] if e.get('text')=='go')
    assert e['context_truncated'] and len(e['context'])==6
    svc.driver.xml=svc.driver.xml.replace('text="fact8"','text="changed fact8"')
    result=invoke(svc.tap,svc.observation.id,e['id'],image='never')
    assert result.is_error and not svc.driver.events

@pytest.mark.parametrize('change',['stop','expire','off','rotate'])
def test_session_and_display_guards_remain(svc,change):
    oid=observe(svc); eid=button(svc)
    if change=='stop':
        svc.stop()
    elif change=='expire':
        svc.observation.created-=200
    else:
        original=svc.driver.header
        svc.driver.header=lambda:{**original(),('screen_on' if change=='off' else 'rotation'):(False if change=='off' else 1)}
    r=invoke(svc.tap,oid,eid,image='never')
    assert r.is_error and r.structured_content['input_status']=='not_sent' and not svc.driver.events
