"""Compact, observation-scoped accessibility elements and layout fingerprints."""
from __future__ import annotations

import hashlib
from collections import defaultdict
from difflib import SequenceMatcher
import json
import re
from dataclasses import asdict, dataclass
from .targets import build_graph

from defusedxml.ElementTree import fromstring


@dataclass
class Element:
    id: str
    text: str
    description: str
    resource_id: str
    class_name: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    scrollable: bool
    enabled: bool
    focused: bool
    editable: bool
    password: bool
    node_index: int = -1

    def as_dict(self) -> dict:
        data = asdict(self)
        data.pop('node_index', None)
        return data


@dataclass
class TreeState:
    elements: list[Element]
    digest: str
    nodes: list[dict]
    diagnostics: dict
    graph: object | None = None


def inspect_tree(xml: str, width: int, height: int) -> TreeState:
    root = fromstring(xml)
    graph = build_graph(xml, width, height)
    elements: list[Element] = []
    fingerprint = []
    nodes = []
    total = hidden = system = 0
    keys = ("package", "class", "resource-id", "text", "content-desc", "bounds", "clickable",
            "scrollable", "enabled", "focused", "selected", "checked", "password", "editable")
    for node_index, node in enumerate(root.iter("node")):
        total += 1
        a = node.attrib
        if not graph.nodes[node_index].visible:
            hidden += 1
            continue
        # Clock and battery in the status bar change independently of the app layout.
        if a.get("package") in ("com.android.systemui", "miui.systemui.plugin"):
            system += 1
            continue
        values = [a.get(k, "") for k in keys]
        # Android exposes a live range value as text on media progress widgets.
        # Playback changes this every frame without moving or replacing controls.
        # Keep its latest value in the observation, but exclude only a numeric
        # value from the layout fingerprint; bounds, identity, descriptions and
        # all interaction flags must still match.
        if (a.get("class") in ("android.widget.ProgressBar", "android.widget.SeekBar",
                               "android.widget.AbsSeekBar")
                and re.fullmatch(r"-?\d+(?:\.\d+)?", a.get("text", ""))):
            values[keys.index("text")] = "[live range value]"
        fingerprint.append(values)
        record = dict(zip(keys, values))
        # Keep semantic fingerprints in memory, but never log field contents.
        nodes.append(record)
        numbers = re.findall(r"-?\d+", a.get("bounds", ""))
        if len(numbers) != 4:
            continue
        l, t, r, b = map(int, numbers)
        bounds = max(0, l), max(0, t), min(width, r), min(height, b)
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            continue
        interesting = any(a.get(k) for k in ("text", "content-desc")) or any(
            a.get(k) == "true" for k in ("clickable", "scrollable", "focused"))
        if not interesting:
            continue
        password = a.get("password") == "true"
        elements.append(Element(
            id=f"e{len(elements) + 1}", text="[redacted]" if password else a.get("text", ""),
            description=a.get("content-desc", ""), resource_id=a.get("resource-id", ""),
            class_name=a.get("class", ""), bounds=bounds,
            clickable=a.get("clickable") == "true", scrollable=a.get("scrollable") == "true",
            enabled=a.get("enabled", "true") == "true", focused=a.get("focused") == "true",
            editable="EditText" in a.get("class", "") or a.get("editable") == "true", password=password,
            node_index=node_index))
    digest = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False).encode()).hexdigest()
    return TreeState(elements, digest, nodes, {"node_count": total, "hidden_nodes": hidden,
        "system_nodes": system, "element_count": len(elements),
        "empty": not elements}, graph)


def parse_tree(xml: str, width: int, height: int) -> tuple[list[Element], str]:
    tree = inspect_tree(xml, width, height)
    return tree.elements, tree.digest


def tree_diff(before: TreeState, after: TreeState) -> dict:
    """Diagnostic alignment only; never used to decide whether input is safe.

    Align unchanged runs, then unique identities among unmatched nodes. An
    ambiguous replacement stays added/removed instead of inventing a pairing.
    Field values (including text and descriptions) never enter the result.
    """
    old_nodes, new_nodes = before.nodes, after.nodes
    matcher = SequenceMatcher(None, [tuple(n.items()) for n in old_nodes],
                              [tuple(n.items()) for n in new_nodes], autojunk=False)
    pairs = []
    for a, b, size in matcher.get_matching_blocks():
        pairs.extend((a + i, b + i) for i in range(size))
    old_left = set(range(len(old_nodes))) - {a for a, _ in pairs}
    new_left = set(range(len(new_nodes))) - {b for _, b in pairs}

    def match_unique(key):
        left, right = defaultdict(list), defaultdict(list)
        for i in sorted(old_left):
            if (value := key(old_nodes[i])) is not None:
                left[value].append(i)
        for i in sorted(new_left):
            if (value := key(new_nodes[i])) is not None:
                right[value].append(i)
        for value in left.keys() & right.keys():
            if len(left[value]) == len(right[value]) == 1:
                a, b = left[value][0], right[value][0]
                pairs.append((a, b))
                old_left.remove(a)
                new_left.remove(b)

    match_unique(lambda n: (n['package'], n['class'], n['resource-id']) if n['resource-id'] else None)
    match_unique(lambda n: (n['package'], n['class'], n['bounds']) if n['bounds'] else None)
    match_unique(lambda n: (n['package'], n['class'], n['text'], n['content-desc'])
                 if (n['text'] or n['content-desc']) and n['password'] != 'true' else None)

    def identity(node, index):
        return {"node": index, "resource_id": node["resource-id"],
                "class": node["class"], "bounds": node["bounds"]}

    changes = []
    for a, b in sorted(pairs):
        old, new = old_nodes[a], new_nodes[b]
        fields = [k for k in old if old[k] != new[k]]
        if fields:
            changes.append({**identity(old, a), "after_node": b,
                            "after_bounds": new["bounds"], "fields": fields})
    return {"comparison": "aligned_nodes_diagnostic_only",
            "before_nodes": len(old_nodes), "after_nodes": len(new_nodes),
            "changed_nodes": len(changes) + len(old_left) + len(new_left),
            "matched_changed_nodes": len(changes),
            "added_nodes": len(new_left), "removed_nodes": len(old_left),
            "changes": changes[:12], "changes_truncated": len(changes) > 12,
            "added": [identity(new_nodes[i], i) for i in sorted(new_left)[:12]],
            "removed": [identity(old_nodes[i], i) for i in sorted(old_left)[:12]],
            "unmatched_may_include_replacements": bool(old_left and new_left)}


def unrelated_text_only(before: TreeState, after: TreeState, target_bounds) -> bool:
    """Only tolerate passive text outside a tap target; never layout or target changes."""
    if not before.nodes or len(before.nodes) != len(after.nodes):
        return False
    l, t, r, b = target_bounds
    changed = False
    for old, new in zip(before.nodes, after.nodes):
        fields = {k for k in old if old[k] != new[k]}
        if not fields:
            continue
        changed = True
        if not fields <= {"text", "content-desc"}:
            return False
        if any(old.get(k) == "true" for k in ("clickable", "focused", "password", "editable")):
            return False
        if "EditText" in old["class"]:
            return False
        coords = re.findall(r"-?\d+", old["bounds"])
        if len(coords) != 4:
            return False
        nl, nt, nr, nb = map(int, coords)
        # Include surrounding labels and containers, not just a one-pixel tap.
        if nl < r + 48 and nr > l - 48 and nt < b + 48 and nb > t - 48:
            return False
    return changed


def localized_motion(before: TreeState, after: TreeState) -> list[tuple] | None:
    """Locate small anonymous noninteractive animations; never hide them globally."""
    if not before.nodes or len(before.nodes) != len(after.nodes):
        return None
    regions = []
    for old, new in zip(before.nodes, after.nodes):
        fields = {k for k in old if old[k] != new[k]}
        if not fields:
            continue
        if fields != {"bounds"} or any(old[k] for k in ("text", "content-desc", "resource-id")):
            return None
        if old["class"] not in ("android.view.View", "android.view.ViewGroup", "android.widget.ImageView"):
            return None
        if any(old[k] == "true" for k in ("clickable", "scrollable", "focused", "selected", "checked", "password", "editable")):
            return None
        coords = [re.findall(r"-?\d+", n["bounds"]) for n in (old, new)]
        if any(len(c) != 4 for c in coords):
            return None
        a, b = [tuple(map(int, c)) for c in coords]
        if any(not (0 < c[2]-c[0] <= 320 and 0 < c[3]-c[1] <= 320) for c in (a,b)):
            return None
        if max(abs(x-y) for x,y in zip(a,b)) > 96:
            return None
        regions.append((min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3])))
        if len(regions) > 8:
            return None
    return regions or None


def overlaps_regions(bounds, regions, margin=48):
    l,t,r,b = bounds
    return any(nl < r+margin and nr > l-margin and nt < b+margin and nb > t-margin
               for nl,nt,nr,nb in regions)


def localized_text_updates(before: TreeState, after: TreeState) -> list[tuple] | None:
    """Bound changing passive labels; taps in or near these labels remain blocked."""
    if not before.nodes or len(before.nodes) != len(after.nodes):
        return None
    regions = []
    for old, new in zip(before.nodes, after.nodes):
        fields = {k for k in old if old[k] != new[k]}
        if not fields:
            continue
        if not fields <= {"text", "content-desc", "bounds"} or not fields & {"text", "content-desc"}:
            return None
        if old["class"] not in ("android.widget.TextView", "android.view.View"):
            return None
        if any(old[k] == "true" for k in ("clickable", "scrollable", "focused", "selected", "checked", "password", "editable")):
            return None
        coords = [re.findall(r"-?\d+", n["bounds"]) for n in (old,new)]
        if any(len(c) != 4 for c in coords):
            return None
        a,b = [tuple(map(int,c)) for c in coords]
        if any(not (0 < c[2]-c[0] and 0 < c[3]-c[1] <= 320) for c in (a,b)):
            return None
        if max(abs(x-y) for x,y in zip(a,b)) > 192:
            return None
        regions.append((min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3])))
        if len(regions) > 8:
            return None
    return regions or None


def localized_subtree_updates(before: TreeState, after: TreeState) -> list[tuple] | None:
    """Bound a small changing card without treating shifted node indices as changes.

    This is tap-only tolerance. Unchanged ancestors and distant controls must
    remain identical; large overlays and changes spanning the screen fail closed.
    """
    if not before.nodes or not after.nodes:
        return None
    key = lambda n: tuple(n.items())
    matcher = SequenceMatcher(None, [key(n) for n in before.nodes],
                              [key(n) for n in after.nodes], autojunk=False)
    changed = []
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag != "equal":
            changed.extend(before.nodes[a:b])
            changed.extend(after.nodes[c:d])
    if not changed or len(changed) > 64:
        return None
    boxes = []
    for n in changed:
        if any(n.get(k) == "true" for k in ("focused", "password", "editable")) or "EditText" in n["class"]:
            return None
        coords = re.findall(r"-?\d+", n["bounds"])
        if len(coords) != 4:
            return None
        box = tuple(map(int, coords))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        boxes.append(box)
    region = (min(b[0] for b in boxes), min(b[1] for b in boxes),
              max(b[2] for b in boxes), max(b[3] for b in boxes))
    if region[3] - region[1] > 320:
        return None
    return [region]


def localized_passive_updates(before: TreeState, after: TreeState) -> list[tuple] | None:
    """Combine independently validated passive changes; never tolerate interactive changes."""
    if not before.nodes or len(before.nodes) != len(after.nodes):
        return None
    regions = []
    for old, new in zip(before.nodes, after.nodes):
        if old == new:
            continue
        a = TreeState([], "", [old], {})
        b = TreeState([], "", [new], {})
        part = localized_motion(a, b) or localized_text_updates(a, b)
        if not part and {k for k in old if old[k] != new[k]} == {"bounds"}:
            # Resource-named passive indicators obey the same size/movement limits.
            x, y = dict(old), dict(new)
            if x["class"] in ("android.widget.RelativeLayout", "android.widget.ImageView"):
                x["resource-id"] = y["resource-id"] = ""
                x["class"] = y["class"] = "android.view.ViewGroup"
                part = localized_motion(TreeState([], "", [x], {}), TreeState([], "", [y], {}))
        if not part:
            return None
        regions.extend(part)
        if len(regions) > 8:
            return None
    return regions or None
