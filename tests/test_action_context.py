"""Generic flat-layout identity and post-input capture regressions."""
import asyncio
import pytest
from mcp import Client
from mobile_use.server import invoke, create_server
from test_targets import svc, observe, button, node, scene


def flat(first='task A', second='task B', price='10', shift=0):
    children=''
    for title,y in ((first,20+shift),(second,80+shift)):
        children += node(title,bounds=f'[0,{y}][45,{y+20}]')
        children += node(price,bounds=f'[0,{y+22}][45,{y+40}]')
        children += node('go','button',f'[60,{y+5}][95,{y+35}]',clickable='true')
    return '<hierarchy>'+node(rid='root',bounds='[0,0][100,200]',children=node(
        rid='shared-card',bounds=f'[0,{20+shift}][100,{135+shift}]',children=children))+'</hierarchy>'


@pytest.mark.parametrize('selector',[False,True])
@pytest.mark.parametrize('change',['same','move','swap'])
def test_flat_repeated_buttons_follow_exact_row(svc,selector,change):
    svc.driver.xml=flat(); oid=observe(svc); eid=button(svc)
    svc.driver.xml=(flat(shift=20) if change=='move' else
                    flat(first='task B',second='task A') if change=='swap' else flat())
    args={'target':{'text':'go','within_text':'task A'}} if selector else {'element_id':eid}
    result=invoke(svc.tap,oid,**args,image='never',seed=1)
    assert not result.is_error, result.structured_content
    assert len(svc.driver.events)==1
    _,y=svc.driver.events[0].points[0]
    top={'same':25,'move':45,'swap':85}[change]
    assert top<=y<top+30


@pytest.mark.parametrize('new',[
    {'first':'replacement'}, {'first':'task B','second':'task B'}, {'price':'100'},
])
def test_flat_changed_context_does_not_fall_back_to_position(svc,new):
    svc.driver.xml=flat(); oid=observe(svc); eid=button(svc)
    svc.driver.xml=flat(**new)
    result=invoke(svc.tap,oid,eid,image='never')
    assert result.is_error and not svc.driver.events


def test_indistinguishable_flat_rows_stay_ambiguous(svc):
    svc.driver.xml=flat(second='task A'); oid=observe(svc)
    result=invoke(svc.tap,oid,button(svc),image='never')
    assert result.is_error and result.structured_content['code']=='target_ambiguous'
    assert not svc.driver.events


def ungrouped(first='Report.pdf', second='Notes.txt'):
    # All rows live directly under the page root: scope() returns None.
    children=''
    for title,y in ((first,20),(second,100)):
        children+=node(title,bounds=f'[0,{y}][45,{y+20}]')
        children+=node('Open','button',f'[60,{y}][95,{y+30}]',clickable='true')
    return '<hierarchy>'+node(rid='root',bounds='[0,0][100,200]',children=children)+'</hierarchy>'


def test_flat_root_selector_uses_advertised_peer_context(svc):
    svc.driver.xml=ungrouped(); oid=observe(svc)
    from mobile_use import targets
    e=next(e for e in svc.observation.elements if e.text=='Open')
    assert targets.scope(svc.observation.tree.graph,e.node_index) is None
    svc.driver.xml=ungrouped(first='Notes.txt',second='Report.pdf')
    r=invoke(svc.tap,oid,target={'text':'Open','within_text':'Report.pdf'},image='never')
    assert not r.is_error, r.structured_content
    assert len(svc.driver.events)==1
    assert svc.driver.events[0].points[0][1]>=100
    assert r.structured_content['validation']['status']=='relocated'


@pytest.mark.parametrize('first,second',[
    ('Notes.txt','Notes.txt'), ('Report.pdf','Report.pdf'), ('Replacement.pdf','Notes.txt'),
])
def test_flat_root_selector_keeps_identity_and_ambiguity_guards(svc,first,second):
    svc.driver.xml=ungrouped(); oid=observe(svc)
    svc.driver.xml=ungrouped(first,second)
    r=invoke(svc.tap,oid,target={'text':'Open','within_text':'Report.pdf'},image='never')
    assert r.is_error and not svc.driver.events


def test_compact_preview_preserves_original_cache_and_coordinates(svc):
    import base64, io
    from PIL import Image
    from mobile_use.server import pack
    oid=observe(svc)
    original=io.BytesIO()
    Image.new('RGB',(1080,2400),'blue').save(original,format='PNG')
    obs=svc.observation
    obs.png=original.getvalue()
    obs.header.update(width=1080,height=2400)
    before=obs.elements[0].bounds
    compact=pack({},obs,image='always',detail='compact')
    info=compact.structured_content['observation']['image']
    assert info['original_size']==[1080,2400] and info['delivered_size']==[576,1280]
    assert info['preview'] and info['input_coordinates']=='original_pixels'
    assert 288/info['scale_x']==540 and 640/info['scale_y']==1200
    assert Image.open(io.BytesIO(base64.b64decode(compact.content[1].data))).size==(576,1280)
    assert obs.png==original.getvalue() and obs.elements[0].bounds==before
    svc.driver.screenshot=lambda:(_ for _ in ()).throw(AssertionError('No new read'))
    full=invoke(svc.get_observation,oid,image='always',detail='full')
    assert not full.is_error and full.structured_content['reads']=={}
    assert base64.b64decode(full.content[1].data)==original.getvalue()
    assert full.structured_content['observation']['image']['preview'] is False


def test_long_compact_label_keeps_full_selector_evidence(svc):
    value='Report '+('x'*600)
    svc.driver.xml=scene(button=value)
    oid=observe(svc)
    compact=invoke(svc.get_observation,oid,image='never',detail='compact').structured_content['observation']
    e=next(e for e in compact['elements'] if e.get('text','').startswith('Report'))
    assert len(e['text'])==256 and e['text_truncated']
    full=invoke(svc.get_observation,oid,image='never',detail='full').structured_content['observation']
    assert next(x for x in full['elements'] if x['id']==e['id'])['text']==value
    assert not invoke(svc.tap,oid,e['id'],image='never').is_error


@pytest.mark.parametrize('package,action,first,second,scale,rtl,container_class',[
    ('org.example.files','Open','Report.pdf','Notes.txt',1,False,'android.view.ViewGroup'),
    ('org.example.library','打开','画册甲','画册乙',2,False,'android.webkit.WebView'),
    ('org.example.catalog','فتح','العنصر الأول','العنصر الثاني',3,True,'android.view.ViewGroup'),
])
@pytest.mark.parametrize('label_field',['text','content-desc'])
def test_row_identity_across_languages_scales_and_directions(
        svc,package,action,first,second,scale,rtl,container_class,label_field):
    """The same inspected item must survive row reordering in unrelated UIs."""
    import re
    from xml.etree import ElementTree as ET
    width,height=100*scale,200*scale
    def layout(swapped=False):
        root=ET.fromstring(flat(first=second if swapped else first,second=first if swapped else second))
        buttons=[]
        for n in root.iter('node'):
            n.set('package',package)
            n.set('class',container_class if list(n) else 'android.widget.TextView')
            l,t,r,b=[int(x)*scale for x in re.findall(r'\d+',n.get('bounds'))]
            if rtl:
                l,r=width-r,width-l
            n.set('bounds',f'[{l},{t}][{r},{b}]')
            if n.get('resource-id')=='button':
                n.set('class','android.widget.Button')
                n.set('text','')
                n.set(label_field,action)
                buttons.append((l,t,r,b))
        return ET.tostring(root,encoding='unicode'),buttons
    svc.driver.package=package
    header=svc.driver.header
    svc.driver.header=lambda:{**header(),'width':width,'height':height}
    svc.driver.xml,_=layout()
    oid=observe(svc)
    svc.driver.xml,bounds=layout(swapped=True)
    key='text' if label_field=='text' else 'description'
    r=invoke(svc.tap,oid,target={key:action,'within_text':first},image='never')
    assert not r.is_error, r.structured_content
    assert len(svc.driver.events)==1
    x,y=svc.driver.events[0].points[0]
    l,t,right,b=bounds[1]
    assert l<=x<right and t<=y<b
    assert r.structured_content['validation']['status']=='relocated'


def test_settle_reads_final_page_once_and_does_not_replay(svc,monkeypatch):
    oid=observe(svc); eid=button(svc)
    original=svc.driver.execute
    def execute(g):
        original(g)
        svc.driver.xml=scene(button='loading')
    svc.driver.execute=execute
    delays=[]
    def delay(seconds):
        delays.append(seconds)
        svc.driver.xml=scene(button='ready')
        return False
    monkeypatch.setattr(svc.cancel,'wait',delay)
    result=invoke(svc.tap,oid,eid,image='always',settle_ms=600,until={'text':'ready'})
    data=result.structured_content
    assert not result.is_error and data['input_status']=='sent'
    assert delays==[.6] and len(svc.driver.events)==1
    assert data['reads']['screenshot']==1 and data['reads']['hierarchy']==3
    assert data['wait']['polls']==1 and data['settle']['requested_ms']==600
    assert any(e.get('text')=='ready' for e in data['observation']['elements'])
    assert svc._after_input_settle_ms==0


def test_stop_during_settle_keeps_sent_and_never_replays(svc,monkeypatch):
    oid=observe(svc); eid=button(svc)
    def cancel(seconds):
        svc.cancel.set()
        return True
    monkeypatch.setattr(svc.cancel,'wait',cancel)
    result=invoke(svc.tap,oid,eid,image='never',settle_ms=600)
    assert result.is_error and result.structured_content['input_status']=='sent'
    assert result.structured_content['stage']=='settle'
    assert len(svc.driver.events)==1 and svc.observation is None
    assert svc._after_input_settle_ms==0


def test_settle_without_condition_has_one_final_tree(svc,monkeypatch):
    oid=observe(svc); eid=button(svc)
    def delay(seconds):
        svc.driver.xml=scene(button='final')
        return False
    monkeypatch.setattr(svc.cancel,'wait',delay)
    r=invoke(svc.tap,oid,eid,settle_ms=600,image='always')
    assert not r.is_error and len(svc.driver.events)==1
    assert r.structured_content['reads']['hierarchy']==2  # preflight + final
    assert r.structured_content['reads']['screenshot']==1
    assert any(e.get('text')=='final' for e in r.structured_content['observation']['elements'])


def test_unknown_input_never_enters_settle(svc,monkeypatch):
    oid=observe(svc); svc.driver.fail=True
    def unexpected(seconds):
        raise AssertionError('Must not delay or capture after unknown dispatch')
    monkeypatch.setattr(svc.cancel,'wait',unexpected)
    r=invoke(svc.tap,oid,button(svc),settle_ms=600,image='never')
    assert r.is_error and r.structured_content['input_status']=='unknown'
    assert len(svc.driver.events)==1 and svc._after_input_settle_ms==0


def test_removed_duplicate_does_not_select_wrong_remaining_row(svc):
    svc.driver.xml=flat(); oid=observe(svc); eid=button(svc)
    svc.driver.xml=svc.driver.xml.replace(node('go','button','[60,25][95,55]',clickable='true'),'')
    r=invoke(svc.tap,oid,eid,image='never')
    assert r.is_error and not svc.driver.events


@pytest.mark.parametrize('value',[-1,2001,True,1.5,float('nan'),'600'])
def test_invalid_settle_never_reads_or_sends(svc,value):
    oid=observe(svc)
    result=invoke(svc.tap,oid,button(svc),image='never',settle_ms=value)
    assert result.is_error and result.structured_content['reads']=={}
    assert not svc.driver.events and svc.observation.id==oid


def test_dry_run_cannot_wait_after_unsent_action(svc):
    oid=observe(svc)
    r=invoke(svc.tap,oid,button(svc),dry_run=True,settle_ms=1)
    assert r.is_error and not svc.driver.events and r.structured_content['reads']=={}


def test_settle_exposed_by_all_input_tools(svc):
    async def run():
        async with Client(create_server(svc)) as client:
            schemas={t.name:t.input_schema for t in (await client.list_tools()).tools}
            for name in ('tap','swipe','type_text','press_key','launch_app'):
                assert 'settle_ms' in schemas[name]['properties']
            first=await client.call_tool('observe',{'image':'never'})
            result=await client.call_tool('tap',{
                'observation_id':first.structured_content['observation']['observation_id'],
                'target':{'text':'go'},'settle_ms':1,'image':'never'})
            assert not result.is_error and result.structured_content['settle']['requested_ms']==1
            assert len(svc.driver.events)==1
    asyncio.run(run())
