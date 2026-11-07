"""Target identity and action-scoped guards; XML order is not a universal hit-test."""
from collections import Counter
from dataclasses import dataclass, field
import re
from defusedxml.ElementTree import fromstring

class TargetError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

@dataclass
class Node:
    attrs: dict
    parent: int | None
    bounds: tuple
    children: list = field(default_factory=list)
    visible: bool = True

@dataclass
class Graph:
    nodes: list[Node]
    width: int
    height: int

    def ancestors(self, index):
        result = []
        while (index := self.nodes[index].parent) is not None:
            result.append(index)
        return result

    def descendants(self, index):
        out = []
        for child in self.nodes[index].children:
            out.append(child)
            out.extend(self.descendants(child))
        return out

    def root(self, index):
        ancestors = self.ancestors(index)
        return ancestors[-1] if ancestors else index

def build_graph(xml, width, height):
    return build_graph_from_root(fromstring(xml), width, height)

def build_graph_from_root(root, width, height):
    """Build the graph from an already parsed XML root without parsing it again."""
    nodes = []
    def visit(el, parent=None, visible=True):
        if el.tag != 'node':
            for child in el:
                visit(child, parent, visible)
            return
        a = dict(el.attrib)
        nums = re.findall(r'-?\d+', a.get('bounds', ''))
        rect = tuple(map(int, nums)) if len(nums) == 4 else (0,0,0,0)
        index = len(nodes)
        visible = visible and a.get('visible-to-user') != 'false'
        nodes.append(Node(a, parent, rect, visible=visible))
        if parent is not None:
            nodes[parent].children.append(index)
        for child in el:
            visit(child, index, visible)
    visit(root)
    return Graph(nodes, width, height)

def identity(node, *, container=False):
    keys = ('package','class','resource-id') if container else (
        'package','class','resource-id','text','content-desc','checked','selected')
    return tuple(node.attrs.get(k,'') for k in keys)

def labels(node):
    if node.attrs.get('password') == 'true':
        return ()
    return tuple(dict.fromkeys(node.attrs.get(k,'') for k in ('text','content-desc') if node.attrs.get(k)))

def area(rect):
    l,t,r,b = rect
    return max(0,r-l)*max(0,b-t)

def contains(rect,x,y):
    l,t,r,b = rect
    return l <= x < r and t <= y < b

def intersects(a,b):
    return a[0]<b[2] and b[0]<a[2] and a[1]<b[3] and b[1]<a[3]

def scope(graph,index):
    """Nearest bounded semantic group, e.g. list row/card. No app-specific IDs."""
    own = {index,*graph.descendants(index)}
    root=graph.root(index)
    max_area=area(graph.nodes[root].bounds)*.6
    for parent in graph.ancestors(index):
        p = graph.nodes[parent]
        if parent==root or area(p.bounds) > max_area:
            continue
        others = [n for n in graph.descendants(parent) if n not in own]
        evidence = tuple(sorted(set(s for i in others if graph.nodes[i].visible for s in labels(graph.nodes[i]))))
        if evidence:
            return identity(p,container=True),evidence
    return None

def descriptor(graph,index):
    n = graph.nodes[index]
    ancestry = tuple(identity(graph.nodes[i],container=True) for i in graph.ancestors(index)
                     if graph.nodes[i].attrs.get('resource-id'))
    content=tuple(sorted(set(s for i in graph.descendants(index) if graph.nodes[i].visible for s in labels(graph.nodes[i]))))
    return identity(n),scope(graph,index),ancestry,identity(graph.nodes[graph.root(index)],container=True),content

def aligned_labels(graph,index):
    """Supplement flat layouts with exact, horizontally adjacent peer labels.

    No nearest-neighbour choice or pixel-position identity: require vertical
    overlap of at least half the smaller height. Full structural scope remains
    bound, so this evidence only distinguishes candidates, never weakens guards.
    """
    n=graph.nodes[index]
    if n.parent is None:
        return ()
    l,t,r,b=n.bounds
    if not area(n.bounds):
        return ()
    evidence=set()
    for i in graph.nodes[n.parent].children:
        peer=graph.nodes[i]
        a,c,d,e=peer.bounds
        if (i==index or peer.children or not peer.visible or not area(peer.bounds)
                or not intersects(peer.bounds,(0,0,graph.width,graph.height))
                or peer.attrs.get('clickable')=='true' or peer.attrs.get('editable')=='true'
                or not (d<=l or r<=a)):
            continue
        if min(b,e)-max(t,c) >= min(b-t,e-c)*.5:
            evidence.update(labels(peer))
    return tuple(sorted(evidence))

def resolve(old,current,index,allow_empty=False):
    wanted = descriptor(old,index)
    if not any(wanted[0][2:5]) and not allow_empty:
        raise TargetError('visual_required','Target has no semantic identity; inspect its image and use visual coordinates')
    matches = [i for i,n in enumerate(current.nodes)
               if n.visible and area(n.bounds) and identity(n)==wanted[0] and descriptor(current,i)==wanted]
    # Only require alignment when structural identity is ambiguous in either
    # snapshot. Unrelated peers must not add guards to already unique targets.
    previously_ambiguous = any(i!=index and n.visible and area(n.bounds) and identity(n)==wanted[0]
                               and descriptor(old,i)==wanted for i,n in enumerate(old.nodes))
    if matches and (len(matches)>1 or previously_ambiguous):
        row=aligned_labels(old,index)
        matches=[i for i in matches if aligned_labels(current,i)==row]
    if not matches:
        raise TargetError('target_changed','Target or its context changed or disappeared')
    if len(matches)!=1:
        raise TargetError('target_ambiguous','Target is ambiguous in the current page; choose a scoped target')
    found = matches[0]
    n = current.nodes[found]
    if not any(wanted[0][2:5]) and n.bounds!=old.nodes[index].bounds:
        raise TargetError('target_changed','Anonymous focused field moved; observe again')
    if any(current.nodes[i].attrs.get('enabled','true')!='true' for i in [found,*current.ancestors(found)]):
        raise TargetError('target_disabled','Target is disabled')
    if n.attrs.get('password')=='true':
        raise TargetError('password','Password targets are not supported')
    return found

def windows(graph,region=None):
    return Counter((identity(n,container=True),n.bounds) for n in graph.nodes
                   if n.parent is None and n.visible and area(n.bounds)
                   and (region is None or intersects(n.bounds,region)))

def visual_hit_controls(graph, region):
    """Native touch recipients over an OCR label; unrelated screen changes do not matter."""
    return Counter((identity(n), tuple(identity(graph.nodes[j],container=True) for j in graph.ancestors(i)))
                   for i,n in enumerate(graph.nodes) if n.visible and intersects(n.bounds,region)
                   and any(n.attrs.get(k)=='true' for k in ('clickable','long-clickable','editable')))

def covering(graph,index,region):
    branch=index
    for i in [index,*graph.ancestors(index)]:
        if graph.nodes[i].attrs.get('clickable')=='true':
            branch=i
            break
    excluded={branch,*graph.ancestors(branch),*graph.descendants(branch)}
    root=graph.root(index)
    return Counter((identity(n),n.bounds) for i,n in enumerate(graph.nodes)
                   if n.visible and i>branch and i not in excluded and graph.root(i)==root
                   and intersects(n.bounds,region))

def check_target(old,current,old_index,new_index):
    def visible_bounds(graph,index):
        l,t,r,b=graph.nodes[index].bounds
        clipped=(max(0,l),max(0,t),min(graph.width,r),min(graph.height,b))
        for ancestor in graph.ancestors(index):
            a=graph.nodes[ancestor].bounds
            if area(a):
                clipped=(max(clipped[0],a[0]),max(clipped[1],a[1]),min(clipped[2],a[2]),min(clipped[3],a[3]))
        if not area(clipped):
            raise TargetError('target_offscreen','Target has no visible in-display area')
        return clipped
    old_bounds=visible_bounds(old,old_index)
    new_bounds=visible_bounds(current,new_index)
    def hit_region(bounds):
        l,t,r,b=bounds
        dx,dy=(r-l)*.35,(b-t)*.35
        return (l+dx,t+dy,r-dx,b-dy)
    def other_windows(graph,index,region):
        own=graph.root(index)
        return Counter((identity(n,container=True),n.bounds) for i,n in enumerate(graph.nodes)
                       if i!=own and n.parent is None and n.visible and area(n.bounds) and intersects(n.bounds,region))
    # The target's own window may resize during a launch animation. Its identity
    # is already bound by descriptor(); other windows retain geometric guards.
    if other_windows(current,new_index,hit_region(new_bounds))-other_windows(old,old_index,hit_region(old_bounds)):
        raise TargetError('window_changed','Window covering the target changed; inspect the new page')
    if covering(current,new_index,hit_region(new_bounds)):
        raise TargetError('target_obscured','A visible layer may cover the target; inspect or dismiss the layer')
    return new_bounds

def surfaces(graph):
    """Major window/container topology for navigation, excluding feed text."""
    return Counter((identity(n,container=True),n.bounds) for i,n in enumerate(graph.nodes)
                   if n.visible and area(n.bounds)>=graph.width*graph.height*.2 and len(graph.ancestors(i))<=3)

def check_layout(old, current, previous_regions=None, current_regions=None):
    """Separate a window's identity/order from its own layout geometry.

    Root layout animation is allowed for the same unambiguous largest window. Other
    windows keep exact geometry. Inner containers still bind geometry unless
    both samples contain the entire action evidence in the same passive container.
    """
    def roots(graph):
        return [n for n in graph.nodes if n.parent is None and n.visible and area(n.bounds)]
    before, after = roots(old), roots(current)
    if [identity(n, container=True) for n in before] != [identity(n, container=True) for n in after]:
        raise TargetError('window_changed', 'Window identity or stacking order changed')
    def encloses(bounds, regions):
        return all(bounds[0] <= r[0] and bounds[1] <= r[1] and bounds[2] >= r[2] and bounds[3] >= r[3]
                   for r in regions)
    largest_before = [i for i,n in enumerate(before) if area(n.bounds) == max((area(v.bounds) for v in before), default=0)]
    largest_after = [i for i,n in enumerate(after) if area(n.bounds) == max((area(v.bounds) for v in after), default=0)]
    for i, (a, b) in enumerate(zip(before, after)):
        if a.bounds != b.bounds and (largest_before != [i] or largest_after != [i] or
                (previous_regions and not encloses(a.bounds, previous_regions)) or
                (current_regions and not encloses(b.bounds, current_regions))):
            raise TargetError('window_changed', 'Window geometry no longer contains the action evidence')
    def inner(graph):
        result = {}
        for i, n in enumerate(graph.nodes):
            if n.parent is None or not n.visible or area(n.bounds) < graph.width*graph.height*.2 or len(graph.ancestors(i)) > 3:
                continue
            key = (identity(n, container=True), tuple(identity(graph.nodes[a], container=True) for a in graph.ancestors(i)),
                   n.attrs.get('clickable'), n.attrs.get('scrollable'))
            result.setdefault(key, []).append(n)
        return result
    a, b = inner(old), inner(current)
    if a.keys() != b.keys():
        raise TargetError('surface_changed', 'Main container identity changed')
    for key, nodes in a.items():
        others = b[key]
        if [n.bounds for n in nodes] == [n.bounds for n in others]:
            continue
        if (len(nodes) != 1 or len(others) != 1 or not previous_regions or not current_regions or
                nodes[0].attrs.get('clickable') == 'true' or nodes[0].attrs.get('scrollable') == 'true' or
                not encloses(nodes[0].bounds, previous_regions) or not encloses(others[0].bounds, current_regions)):
            raise TargetError('surface_changed', 'Main container geometry changed around the action')

def check_scroll(old,current,region,container_index=None):
    """A coordinate gesture must stay in the same scroll container and geometry."""
    candidates=[i for i,n in enumerate(old.nodes) if n.visible and n.attrs.get('scrollable')=='true'
                and contains(n.bounds,region[0],region[1]) and contains(n.bounds,region[2]-1,region[3]-1)]
    if container_index is not None:
        if container_index not in candidates:
            raise TargetError('scroll_container','Chosen container must contain the gesture path and be scrollable')
        candidates=[container_index]
    if not candidates:
        return 'window_and_navigation_surface'
    index=min(candidates,key=lambda i:area(old.nodes[i].bounds))
    def key(graph,i):
        return (identity(graph.nodes[i],container=True),graph.nodes[i].bounds,
                tuple(identity(graph.nodes[a],container=True) for a in graph.ancestors(i)))
    matches=[i for i,n in enumerate(current.nodes) if n.visible and n.attrs.get('scrollable')=='true'
             and key(current,i)==key(old,index)]
    if len(matches)!=1:
        raise TargetError('scroll_container_changed','Scroll container moved, disappeared or became ambiguous')
    if covering(old,index,region)!=covering(current,matches[0],region):
        raise TargetError('scroll_obscured','A layer covering the scroll container changed')
    return 'scroll_container_geometry_and_windows'

def element_by_id(elements, element_id):
    """Resolve within the complete snapshot; delivery pagination is not a filter."""
    return next((e for e in elements if e.id == element_id), None)

def select_observed(elements,graph,selector):
    found=[]
    for e in elements:
        if any(getattr(selector,k) is not None and getattr(e,k)!=getattr(selector,k)
               for k in ('text','description','resource_id')):
            continue
        if selector.within_text is not None:
            s=scope(graph,e.node_index)
            # A flat screen may have no bounded semantic group at all. The
            # same aligned-peer evidence advertised in observations must be
            # usable for selection, not only after a structural scope match.
            if ((not s or selector.within_text not in s[1]) and
                    selector.within_text not in aligned_labels(graph,e.node_index)):
                continue
        found.append(e)
    if len(found)>1 and selector.within_text is not None:
        aligned=[e for e in found if selector.within_text in aligned_labels(graph,e.node_index)]
        if aligned:
            found=aligned
    if len(found)!=1:
        raise TargetError('target_ambiguous' if found else 'target_missing',
                          'Selector must match exactly one target in the inspected observation')
    return found[0]
