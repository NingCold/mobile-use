"""Snapshot-local optimizations must preserve evidence and computation results."""
import io
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw, ImageChops, ImageStat

from mobile_use import state, targets
from mobile_use.vision import PixelComparison, patch_delta, stationary, same_search_pixels
from test_targets import scene


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
