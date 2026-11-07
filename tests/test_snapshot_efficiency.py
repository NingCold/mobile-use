"""Snapshot-local optimizations must preserve evidence and computation results."""
import io
import random
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw, ImageChops, ImageStat

from mobile_use import state, targets
from mobile_use.vision import PixelComparison, patch_delta, stationary, same_search_pixels
from test_targets import scene, node
from test_vision import visual, tap as visual_tap
from mobile_use.service import Observation


def test_inspect_tree_parses_once_and_preserves_graph(monkeypatch):
    xml = scene()
    expected_graph = targets.build_graph(xml, 100, 200)
    parse = state.fromstring
    calls = []
    def counted(value):
        calls.append(value)
        return parse(value)
    monkeypatch.setattr(state, 'fromstring', counted)
    monkeypatch.setattr(targets, 'fromstring', lambda *_: pytest.fail('XML parsed twice'))
    tree = state.inspect_tree(xml, 100, 200)
    assert calls == [xml] and tree.graph == expected_graph
    assert [tree.graph.nodes[e.node_index].attrs['text'] for e in tree.elements] == [e.text for e in tree.elements]


def test_identical_repeated_nodes_skip_alignment_but_keep_the_complete_diff(monkeypatch):
    tree = state.inspect_tree(scene(), 100, 200)
    tree.nodes = [dict(tree.nodes[-1]) for _ in range(3000)]
    after = replace(tree, nodes=[dict(n) for n in tree.nodes])
    monkeypatch.setattr(state, 'SequenceMatcher', lambda *_args, **_kwargs: pytest.fail('Unnecessary alignment'))
    assert state.tree_diff(tree, after) == {
        'comparison': 'aligned_nodes_diagnostic_only', 'before_nodes': 3000, 'after_nodes': 3000,
        'changed_nodes': 0, 'matched_changed_nodes': 0, 'added_nodes': 0, 'removed_nodes': 0,
        'changes': [], 'changes_truncated': False, 'added': [], 'removed': [],
        'unmatched_may_include_replacements': False}


def test_pixel_pair_reuses_decodes_and_preserves_metrics_and_competition_scope(monkeypatch):
    before = Image.new('RGB', (100, 200), 'white')
    after = before.copy()
    ImageDraw.Draw(after).rectangle((70, 160, 90, 180), fill='red')
    def png(im):
        stream = io.BytesIO(); im.save(stream, format='PNG'); return stream.getvalue()
    a, b = png(before), png(after)
    regions = [(10, 10, 50, 50), (60, 150, 100, 200)]
    expected = []
    for bounds in regions:
        delta = ImageChops.difference(before.crop(bounds), after.crop(bounds))
        r, g, blue = delta.split()
        peak = ImageChops.lighter(ImageChops.lighter(r, g), blue)
        expected.append((max(ImageStat.Stat(delta).mean), sum(peak.histogram()[25:])/(delta.width*delta.height)))
    opens, original = [], Image.open
    def counted(*args, **kwargs):
        opens.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(Image, 'open', counted)
    pair = PixelComparison(a, b)
    assert [pair.patch_delta(r) for r in regions] == expected
    assert pair.stationary(regions[0]) and not pair.stationary(regions[1])
    assert pair.same_search_pixels(regions[0]) and not pair.same_search_pixels(None)
    assert len(opens) == 2
    assert patch_delta(a, b, regions[1]) == expected[1]
    assert stationary(a, b, regions[0]) and not same_search_pixels(a, b, None)
    # A new pair must not inherit cached pixels from the previous confirmation.
    fresh = PixelComparison(a, a)
    assert fresh.stationary(regions[1]) and fresh.same_search_pixels(None)


def test_snapshot_indices_match_full_scans_and_do_not_escape_the_frame():
    rng = random.Random(13)
    children = []
    for i in range(80):
        l,t = rng.randrange(-20,100),rng.randrange(-20,200)
        r,b = l+rng.randrange(0,70),t+rng.randrange(0,70)
        flags = {flag:'true' for flag in ('clickable','editable','password') if rng.random()<.15}
        if i%11==0:
            flags['visible-to-user']='false'
        children.append(node(f'Peer {i}',f'p{i%9}',f'[{l},{t}][{r},{b}]',**flags))
    xml='<hierarchy>'+node(rid='root',bounds='[0,0][100,200]',children=''.join(children))+'</hierarchy>'
    graph=targets.build_graph(xml,100,200)
    expected_keys, groups = [], {}
    for i,n in enumerate(graph.nodes):
        ancestors=[];p=n.parent
        while p is not None:
            ancestors.append(p);p=graph.nodes[p].parent
        expected_keys.append((targets.identity(n,container=True),
            tuple(targets.identity(graph.nodes[a],container=True) for a in ancestors)))
        if n.visible:groups.setdefault(expected_keys[-1],[]).append(i)
        assert graph.ancestors(i)==ancestors
        copy=graph.ancestors(i);copy.append(-1)
        assert graph.ancestors(i)==ancestors
        expected=set()
        if n.parent is not None and targets.area(n.bounds):
            l,t,r,b=n.bounds
            for j in graph.nodes[n.parent].children:
                peer=graph.nodes[j];a,c,d,e=peer.bounds
                if (j!=i and not peer.children and peer.visible and targets.area(peer.bounds)
                    and targets.intersects(peer.bounds,(0,0,100,200))
                    and peer.attrs.get('clickable')!='true' and peer.attrs.get('editable')!='true'
                    and (d<=l or r<=a) and min(b,e)-max(t,c)>=min(b-t,e-c)*.5):
                    expected.update(targets.labels(peer))
        assert targets.aligned_labels(graph,i)==tuple(sorted(expected))
    assert graph.container_keys==tuple(expected_keys)
    assert graph.container_index=={k:tuple(v) for k,v in groups.items()}
    # The next frame has new ownership, even for identical structure/IDs.
    fresh=targets.build_graph(xml.replace('Peer 1"','Changed"'),100,200)
    assert not fresh._ancestry and not fresh._peer_edges
    assert 'container_index' not in fresh.__dict__
    assert fresh.container_index is not graph.container_index


def test_delivery_serializes_only_selected_elements_and_keeps_results_independent(monkeypatch):
    seed=state.inspect_tree(scene(),100,200).elements[0]
    elements=[replace(seed,id=f'e{i}',text=f'Row {i}',description='') for i in range(300)]
    obs=Observation('page',0,{},elements,'',b'',validation_mode='target')
    converted=[];original=state.Element.as_dict
    def counted(element):
        converted.append(element.id)
        return original(element)
    monkeypatch.setattr(state.Element,'as_dict',counted)
    first=obs.metadata('full',limit=2)
    assert converted==['e0','e1'] and first['pagination']['matched_elements']==300
    converted.clear()
    second=obs.metadata('full',limit=2,cursor=first['pagination']['next_cursor'])
    assert converted==['e2','e3'] and second['elements'][0]['text']=='Row 2'
    converted.clear()
    focused=obs.metadata('full',focus={'text':'Row 299'})
    assert converted==['e299'] and focused['focus']['matched_elements']==1
    focused['elements'][0]['text']='modified by consumer'
    assert elements[-1].text=='Row 299'


def test_visual_preflight_merges_one_header_not_confirmation_evidence(visual):
    service,oid,change=visual;change()
    result=visual_tap(service,oid,dry_run=True)
    assert not result.is_error,result.structured_content
    reads=result.structured_content['reads']
    assert reads['header']<=5 and reads['hierarchy']==2 and reads['screenshot']==2
    assert not service.driver.events


@pytest.mark.parametrize('stage',['tree','local_checks'])
def test_final_header_still_blocks_changes_after_visual_tree_sampling(visual,monkeypatch,stage):
    service,oid,change=visual;change()
    if stage=='tree':
        original=service.driver.hierarchy;reads=[]
        def hierarchy():
            xml=original();reads.append(1)
            if len(reads)==2:service.driver.package='example.other'
            return xml
        service.driver.hierarchy=hierarchy
    else:
        original=targets.check_layout
        def checked(*args,**kwargs):
            original(*args,**kwargs)
            service.driver.package='example.other'
        monkeypatch.setattr(targets,'check_layout',checked)
    result=visual_tap(service,oid)
    assert result.is_error and result.structured_content['code']=='foreground_changed'
    assert result.structured_content['input_status']=='not_sent' and not service.driver.events
