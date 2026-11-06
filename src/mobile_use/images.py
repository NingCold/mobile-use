"""Conservative screenshot selection from structure; never authorizes input.

An accessibility tree cannot prove that it describes every visible pixel. These
rules choose evidence to deliver, not whether an action is safe or a task ready.
"""
from . import targets


def image_evidence(header, tree, previous=None, *, recovering=False, hint=None):
    reasons = []
    if previous is None:
        reasons.append('first_observation')
    elif previous.header != header:
        reasons.append('foreground_or_display_changed')
    if recovering or (previous and not previous.actionable):
        reasons.append('capture_inconsistent_or_recovering')
    if hint:
        reasons.append(hint)
    if len(tree.elements) <= 2:
        reasons.append('sparse_accessibility_tree')
    if len(tree.elements) > 250:
        reasons.append('truncated_elements')

    graph = tree.graph
    if graph is None:
        return reasons + ['unknown_structure'], []
    if previous and previous.tree and previous.tree.graph:
        before = previous.tree.graph
        if (targets.windows(before) != targets.windows(graph) or
                targets.surfaces(before) != targets.surfaces(graph)):
            reasons.append('window_or_surface_changed')

    # Inspect visible subtrees once. A labelled wrapper does not hide a drawn
    # descendant. Offscreen and explicitly hidden surfaces need no screenshot.
    viewport = (0, 0, graph.width, graph.height)
    packages = {n['package'] for n in tree.nodes}  # same system-node filtering as delivery
    visible, semantic = set(), set()
    for i, n in enumerate(graph.nodes):
        if (n.visible and n.attrs.get('package', '') in packages and
                targets.area(n.bounds) and targets.intersects(n.bounds, viewport)):
            visible.add(i)
            if (targets.labels(n) or n.attrs.get('password') == 'true' or
                    n.attrs.get('editable') == 'true' or 'EditText' in n.attrs.get('class', '')):
                semantic.add(i)
    subtree_semantic = set(semantic)
    for i in reversed(range(len(graph.nodes))):
        if i in subtree_semantic and graph.nodes[i].parent is not None:
            subtree_semantic.add(graph.nodes[i].parent)
    local = []
    for i in sorted(visible):
        n = graph.nodes[i]
        cls = n.attrs.get('class', '')
        children = [j for j in n.children if j in visible]
        if 'SurfaceView' in cls or 'TextureView' in cls:
            reasons.append('drawn_surface')
        elif 'WebView' in cls:
            if not any(j in subtree_semantic for j in children):
                reasons.append('unrepresented_web_content')
        elif i not in subtree_semantic:
            l, t, r, b = n.bounds
            clipped = (max(0, l), max(0, t), min(graph.width, r), min(graph.height, b))
            fraction = targets.area(clipped) / max(1, targets.area(viewport))
            # Small native icons are a local visual question, not proof that
            # all the readable content needs another full-screen image. Custom
            # views and large blank/drawn content remain conservative fallbacks.
            native = cls.startswith(('android.', 'androidx.'))
            if native and fraction <= .02 and (n.attrs.get('clickable') == 'true' or
                                               ('Image' in cls and not children)):
                local.append({'bounds': list(clipped), 'class': cls,
                              'resource_id': n.attrs.get('resource-id', '')})
                continue
            if n.attrs.get('clickable') == 'true':
                reasons.append('unlabelled_control')
            elif not children:
                # Resource IDs identify nodes, not their visual content. Large
                # blank leaves, images and custom views can hide loading/canvas
                # content even when other parts of the tree are richly labelled.
                if ('Image' in cls or not cls.startswith(('android.', 'androidx.')) or
                        targets.area(clipped) >= targets.area(viewport) * .1):
                    reasons.append('unrepresented_content')
    # Bound both the omitted visual area and the metadata. Summed area is an
    # upper bound (overlap is intentionally counted); this never underestimates.
    if local:
        if (len(semantic) < 3 or len(local) > 8 or
                sum(targets.area(r['bounds']) for r in local) > targets.area(viewport) * .08):
            reasons.append('extensive_unlabelled_regions')
        elif previous is None or local != previous.capture.get('visual_regions', []):
            reasons.append('new_visual_regions')
    return list(dict.fromkeys(reasons)), local


def image_reasons(header, tree, previous=None, *, recovering=False, hint=None):
    return image_evidence(header, tree, previous, recovering=recovering, hint=hint)[0]
