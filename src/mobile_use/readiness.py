"""Bounded read-only click readiness, independent of application vocabulary."""
from . import targets


def busy_present(graph, selector):
    if selector is None:
        return False
    for i, n in enumerate(graph.nodes):
        if not n.visible or not targets.area(n.bounds):
            continue
        if any(getattr(selector, field) is not None and n.attrs.get(attr, '') != getattr(selector, field)
               for field, attr in [('text', 'text'), ('description', 'content-desc'), ('resource_id', 'resource-id')]):
            continue
        if not targets.matches_context(graph,i,selector):
            continue
        return True
    return False


def geometry(graph, index, bounds):
    # Ancestor transforms can move a click even while its leaf bounds briefly
    # repeat. Unrelated labels, timers and animation elsewhere are not readiness.
    return bounds, tuple(graph.nodes[i].bounds for i in graph.ancestors(index))


def confirm_target(header, tree, index, *, sample, cancel, now, deadline, options, condition=None, context_texts=None):
    """Return the last checked target, never send input or create an observation."""
    anchor, anchor_index = tree.graph, index
    started = now()
    deadline = min(deadline, started + options.timeout_seconds)
    quiet_since, signature = None, None
    reads, movements, busy_samples, missing_samples = 0, 0, 0, 0
    while True:
        if cancel.is_set():
            raise targets.TargetError('stopped', 'Stopped while waiting for click readiness')
        if now() >= deadline:
            raise targets.TargetError('target_not_ready', 'Target did not become ready within the click budget')
        try:
            current = targets.resolve(anchor, tree.graph, anchor_index,context_texts=context_texts)
        except targets.TargetError as exc:
            # Missing is transient; a different identity/context or ambiguity is
            # not. Keep the original binding across every read-only attempt.
            identical = any(n.visible and targets.identity(n) == targets.identity(anchor.nodes[anchor_index])
                            for n in tree.graph.nodes)
            if exc.code != 'target_changed' or identical:
                raise
            current = None
        if current is None:
            missing_samples += 1
            quiet_since, signature = None, None
        else:
            bounds = targets.check_target(anchor, tree.graph, anchor_index, current)
            current_signature = geometry(tree.graph, current, bounds)
            busy = busy_present(tree.graph, options.busy) or (condition is not None and not condition(header, tree))
            if busy:
                busy_samples += 1
                quiet_since, signature = None, None
            elif current_signature != signature:
                movements += int(signature is not None)
                signature, quiet_since = current_signature, now()
            elif reads and (now() - quiet_since) * 1000 >= options.quiet_ms - .001:
                return header, tree, current, bounds, {
                    'quiet_ms': options.quiet_ms, 'elapsed_ms': round((now() - started) * 1000),
                    'layout_updates': movements, 'busy_samples': busy_samples, 'missing_samples': missing_samples,
                    'confirmation_reads': reads, 'scope': 'sampled_target_geometry_and_declared_busy'}
        # First confirmation can spend its quiet window inside the tree RPC.
        # Only wait for the still-missing interval after that, not a fixed delay
        # on top of every device read.
        delay = 0 if reads == 0 else min(.2, max(0, deadline - now()))
        if quiet_since is not None and reads:
            delay = min(max(0, options.quiet_ms/1000 - (now() - quiet_since)), max(0, deadline-now()))
        if delay:
            cancel.wait(delay)
        checked_header, tree = sample()
        if checked_header != header:
            raise targets.TargetError('foreground_changed', 'Foreground changed while waiting for click readiness')
        reads += 1
