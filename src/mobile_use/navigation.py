"""Caller-declared navigation with local resolution and a single final observation."""
from dataclasses import asdict
from datetime import datetime, timezone
import time

from . import targets
from .gestures import check_point, swipe_plan, tap_plan
from .options import visual_target_option, navigation_visual_option, visual_choices_option, WaitCondition, TapReadiness
from .vision import LocalVision, unique, PixelComparison, bind_declared, choose_declared, relocate
from .readiness import confirm_target


class LocalNavigation:
    @staticmethod
    def _navigation_window(tree, step, element, region=None):
        graph = tree.graph
        if element is not None:
            l,t,r,b = targets.check_target(graph, graph, element.node_index, element.node_index)
            x,y = round((l+r)/2), round((t+b)/2)
            region = (x,y,x+1,y+1)
            owner = graph.root(element.node_index)
        else:
            if region is None:
                g = step.swipe
                region = (min(g.start_x,g.end_x),min(g.start_y,g.end_y),
                          max(g.start_x,g.end_x)+1,max(g.start_y,g.end_y)+1)
            candidates = [i for i,n in enumerate(graph.nodes) if n.parent is None and n.visible and
                          n.attrs.get('package') == step.package and
                          n.bounds[0] <= region[0] and n.bounds[1] <= region[1] and
                          n.bounds[2] >= region[2] and n.bounds[3] >= region[3]]
            if len(candidates) != 1:
                return False
            owner = candidates[0]
        return not any(i != owner and n.parent is None and n.visible and targets.window_affects(graph,i,[region])
                       for i,n in enumerate(graph.nodes))

    def _navigation_dimensions(self, plan, header):
        """Validate the entire plan before its first input, including late steps."""
        for step in plan.steps:
            if step.swipe:
                g = step.swipe
                for point in ((g.start_x, g.start_y), (g.end_x, g.end_y)):
                    check_point(*point, header['width'], header['height'])
                swipe_plan((g.start_x, g.start_y), (g.end_x, g.end_y), g.kind, 0,
                           header['width'], header['height'], g.duration_ms, 'steady')
            if step.visual_when:
                visual_target_option(step.visual_when, header['width'], header['height'])
            if step.visual_target:
                navigation_visual_option(step.visual_target, header['width'], header['height'])
            if step.visual_target_any_of:
                visual_choices_option(step.visual_target_any_of, header['width'], header['height'])

    def _navigation_visual_target(self, baseline, packages, step, header, tree, deadline, row):
        """Resolve declared intent on a future page; bounded reads, no old pixel box."""
        from .service import MobileError
        if self._vision is None:
            self._vision = LocalVision()
        choices = step.visual_target_any_of
        specs = choices.selectors() if choices else [step.visual_target]
        spec = step.visual_target
        region = specs[0].search_bounds
        wanted = tuple(dict.fromkeys(v for s in specs for v in (s.text, s.context_text) if v is not None))
        started = time.monotonic()
        deadline = min(deadline, started + 8)
        frame, reference, reference_tree = None, None, None
        labels = []
        labels_image_at = None
        captures = ocr_reads = 0

        def recognize(sample):
            nonlocal labels_image_at, ocr_reads
            if 'labels' not in sample:
                ocr_reads += 1
                sample['labels'] = self._vision.read(sample['png'], texts=wanted, region=region)
            labels_image_at = sample['image_at']
            return sample['labels']

        def page_tree():
            current_header, current = self._sequence_check(baseline, packages, tree=True)
            if current_header != header or not self._matches(step.when, header, current.elements):
                raise MobileError('Visual navigation page condition changed')
            return current

        def capture():
            nonlocal captures
            if self.cancel.is_set() or time.monotonic() >= deadline or captures >= (3 if choices else 4):
                raise targets.TargetError('visual_search_timeout', 'Visual resolution exhausted its local budget')
            png, size = self._read('screenshot')
            captures += 1
            if size != (header['width'], header['height']) or self._read('header') != header:
                raise MobileError('Display/foreground changed during visual navigation')
            return {'png': png, 'size': size, 'header': header,
                    'image_at': datetime.now(timezone.utc).isoformat()}

        try:
            frame = capture()
            while True:
                labels = recognize(frame)
                # A future page has no inspected old container to preserve. Bind
                # its FIRST reference after OCR, not to a pre-OCR transition tree.
                # Once bound, subsequent samples must preserve that identity.
                tree = page_tree()
                if choices:
                    frame['page_graph'] = tree.graph
                try:
                    if reference is None:
                        if choices:
                            spec, reference = choose_declared(labels, specs, frame['size'], tree.graph)
                        else:
                            reference = bind_declared(labels, spec, frame['size'], tree.graph)
                        reference_tree = tree
                    target, contexts = relocate(labels, spec, reference, frame['size'], tree.graph)
                except targets.TargetError as exc:
                    if exc.code != 'visual_target_missing':
                        raise
                    if choices and (captures != 1 or reference is not None or not any(
                        n.visible and n.attrs.get('class') == 'android.widget.ProgressBar'
                        and targets.intersects(n.bounds, region or (0, 0, *frame['size']))
                        for n in tree.graph.nodes)):
                        raise  # Unknown content is a decision point, not a timeout per label.
                    self.cancel.wait(min(.2, max(0, deadline-time.monotonic())))
                    tree = page_tree()
                    frame = capture()
                    continue
                checked = capture()
                pixels = PixelComparison(frame['png'], checked['png'])
                stable = all(pixels.stationary(v.bounds) for v in (target, *contexts))
                frame = checked
                if choices:
                    if pixels.same_search_pixels(region):
                        checked['labels'] = labels
                    labels = recognize(checked)
                current = page_tree()
                if choices:
                    frame['page_graph'] = current.graph
                    confirmed, _ = choose_declared(labels, specs, frame['size'], current.graph)
                    if confirmed != spec:
                        raise targets.TargetError('visual_context_changed', 'The matched alternative changed during confirmation')
                row['visual_windows'] = {
                    'bound': [dict(identity=list(targets.identity(n, container=True)), bounds=list(n.bounds))
                              for n in reference_tree.graph.nodes if n.parent is None and n.visible],
                    'current': [dict(identity=list(targets.identity(n, container=True)), bounds=list(n.bounds))
                                for n in current.graph.nodes if n.parent is None and n.visible]}
                prior_bounds = target.bounds
                target, contexts = relocate(labels, spec, reference, frame['size'], current.graph)
                if choices and prior_bounds != target.bounds:
                    stable = False
                regions = [target.bounds, *(c.bounds for c in contexts)]
                targets.check_layout(reference_tree.graph, current.graph, reference.regions, regions)
                if choices and targets.visual_hit_controls(current.graph, target.bounds) != targets.visual_hit_controls(
                        reference_tree.graph, reference.target.bounds):
                    raise targets.TargetError('visual_hit_changed', 'A touch control over the visual target changed')
                tree = current
                if not stable:
                    continue  # Reuse the confirmation frame; never replay input.
                if not self._navigation_window(tree, step, None, target.bounds):
                    raise MobileError('Another window covers visual navigation input')
                if time.monotonic() >= deadline:
                    raise targets.TargetError('visual_search_timeout', 'Visual resolution exhausted its local budget')
                row['visual'] = {'text': spec.text, 'context_text': spec.context_text,
                                 'resolved_bounds': list(target.bounds), 'confidence': round(target.score, 3),
                                 'context_bounds': [list(c.bounds) for c in contexts],
                                 'captures': captures, 'confirmation': 'fresh_local_pixels',
                                 'context_source': 'visual_item' if spec.context_text else 'declared_accessible_page',
                                 'search_ms': round((time.monotonic()-started)*1000)}
                if choices:
                    row['visual'].update(candidate_count=len(specs), ocr_reads=ocr_reads,
                                         confirmation='unique_in_confirmed_scope')
                row.pop('visual_windows', None)  # Full topology is useful only on failure.
                return header, tree, target.bounds
        except Exception as exc:
            row['visual'] = {'text': spec.text if spec else None, 'context_text': spec.context_text if spec else None, 'captures': captures,
                             'code': getattr(exc, 'code', type(exc).__name__),
                             'search_ms': round((time.monotonic()-started)*1000),
                             'labels_image_at': labels_image_at,
                             'sampled_labels': [{'text': v.text[:200], 'score': round(v.score, 3),
                                                 'bounds': list(v.bounds)} for v in labels[:16]],
                             'labels_truncated': len(labels) > 16}
            if choices:
                row['visual'].update(candidate_count=len(specs), ocr_reads=ocr_reads)
            recovery = None
            if frame is not None and not self.cancel.is_set():
                try:
                    current = self._state()
                    if current[0] == header:
                        sample = frame
                        if choices:
                            g = frame.get('page_graph')
                            if (g is None or targets.windows(g) != targets.windows(current[1].graph)
                                    or not self._matches(step.when, header, current[1].elements)
                                    or any(n.visible and targets.is_modal(n)
                                           for n in current[1].graph.nodes)):
                                sample = None  # Do not present the old screen as the new window.
                        recovery = self._remember_observation(self._capture_target(
                            initial_state=current, sampled_image=sample, verify_after_image=sample is None))
                except Exception:
                    pass
            raise MobileError(str(exc), partial_observation=recovery, code=row['visual']['code'],
                              recovery={'status': 'observed' if recovery else 'unavailable',
                                        'input_replayed': False}) from None
        finally:
            self._record_time('visual_local_ms', started)

    def _navigation_visual(self, spec, header):
        from .service import MobileError
        if self._vision is None:
            self._vision = LocalVision()
        png, size = self._read('screenshot')
        if size != (header['width'], header['height']) or self._read('header') != header:
            raise MobileError('Display changed during navigation condition')
        labels = self._vision.read(png, texts=(spec.text, spec.context_text), region=spec.search_bounds)
        region = spec.search_bounds or (0, 0, *size)
        found = [unique(labels, text, region) for text in (spec.text, spec.context_text)]
        checked, checked_size = self._read('screenshot')
        if checked_size != size or self._read('header') != header:
            raise MobileError('Display changed during navigation condition')
        pixels = PixelComparison(png, checked)
        if not all(pixels.stationary(n.bounds) for n in found):
            raise targets.TargetError('visual_unstable', 'Navigation condition moved during confirmation')
        return {'text': spec.text, 'context_text': spec.context_text,
                'bounds': [list(n.bounds) for n in found], 'source': 'local_ocr_confirmed'}

    def _navigation_run(self, baseline, plan, result):
        from .service import MobileError
        started = time.monotonic()
        deadline = started + plan.timeout_seconds
        result.update(status='running', steps=[])
        self._log('navigation_start', plan=asdict(plan))
        try:
            for index, step in enumerate(plan.steps):
                row = {'index': index, 'kind': step.kind, 'package': step.package,
                       'input_status': 'not_sent', 'status': 'resolving'}
                result['steps'].append(row)
                at = time.monotonic()
                step_deadline = min(deadline, at + step.timeout_seconds)
                carried = None
                visual_bounds = None
                while True:
                    header, tree = carried or self._sequence_check(baseline, plan.allowed_packages, tree=True)
                    carried = None
                    if time.monotonic() >= step_deadline:
                        raise MobileError('Navigation step timed out before input')
                    ready = step.kind == 'launch' or header['package'] == step.package
                    if ready and step.when:
                        ready = self._matches(step.when, header, tree.elements)
                    element = None
                    if ready and step.target:
                        try:
                            element = targets.select_observed(tree.elements, tree.graph, step.target)
                        except targets.TargetError as exc:
                            if exc.code != 'target_missing':
                                raise
                            ready = False
                    if ready and element is not None:
                        header, tree, idx, bounds, readiness = confirm_target(
                            header, tree, element.node_index,
                            sample=lambda: self._sequence_check(baseline, plan.allowed_packages, tree=True),
                            cancel=self.cancel, now=time.monotonic, deadline=step_deadline,
                            options=step.readiness or TapReadiness(),
                            context_texts=targets.declared_context(step.target),
                            condition=(lambda h, t: self._matches(step.when, h, t.elements)) if step.when else None)
                        row['readiness'] = readiness
                        if readiness['layout_updates']:
                            row['layout_updates'] = readiness['layout_updates']
                        element = next(e for e in tree.elements if e.node_index == idx)
                    if ready and (step.visual_target or step.visual_target_any_of):
                        header, tree, visual_bounds = self._navigation_visual_target(
                            baseline, plan.allowed_packages, step, header, tree, step_deadline, row)
                    if ready and step.kind != 'launch':
                        ready = self._navigation_window(tree, step, element, visual_bounds)
                    if ready and step.visual_when:
                        try:
                            row['condition'] = self._navigation_visual(step.visual_when, header)
                        except targets.TargetError as exc:
                            if exc.code not in ('visual_target_missing', 'visual_unstable'):
                                raise
                            ready = False
                        if ready:
                            # OCR is slower than tree acquisition; bind input to a new tree too.
                            current_header, current = self._sequence_check(baseline, plan.allowed_packages, tree=True)
                            if current_header != header:
                                raise MobileError('Foreground changed after navigation condition')
                            if step.target:
                                resolved = targets.resolve(tree.graph, current.graph, element.node_index,
                                                           context_texts=targets.declared_context(step.target))
                                confirmed = next(e for e in current.elements if e.node_index == resolved)
                                targets.check_layout(tree.graph,current.graph,[element.bounds],[confirmed.bounds])
                                if confirmed.bounds != element.bounds:
                                    carried = (current_header, current)
                                    row['layout_updates'] = row.get('layout_updates', 0) + 1
                                    ready = False
                                element = confirmed
                            else:
                                g = step.swipe
                                # Navigation uses a steady (straight) gesture.
                                regions = [(min(g.start_x,g.end_x),min(g.start_y,g.end_y),
                                            max(g.start_x,g.end_x)+1,max(g.start_y,g.end_y)+1)]
                                targets.check_layout(tree.graph,current.graph,regions,regions)
                            tree = current
                            if step.when and not self._matches(step.when, header, tree.elements):
                                raise MobileError('Navigation condition changed before input')
                    if ready:
                        break
                    self.cancel.wait(min(.25, max(0, step_deadline-time.monotonic())))
                gesture = None
                if step.kind != 'launch' and not self._navigation_window(tree, step, element, visual_bounds):
                    raise MobileError('Another window covers navigation input; inspect first')
                if element is not None:
                    idx = targets.resolve(tree.graph, tree.graph, element.node_index,
                                          context_texts=targets.declared_context(step.target))
                    bounds = targets.check_target(tree.graph, tree.graph, idx, idx)
                    gesture = tap_plan(bounds, self._seed(None), 'steady')
                    row['target'] = asdict(step.target)
                    row['resolved_bounds'] = list(bounds)
                elif visual_bounds is not None:
                    gesture = tap_plan(visual_bounds, self._seed(None), 'steady')
                    row['resolved_bounds'] = list(visual_bounds)
                elif step.swipe:
                    g = step.swipe
                    gesture = swipe_plan((g.start_x,g.start_y),(g.end_x,g.end_y),g.kind,
                                         self._seed(None),header['width'],header['height'],g.duration_ms,'steady')
                    region = (min(x for x,y in gesture.points),min(y for x,y in gesture.points),
                              max(x for x,y in gesture.points)+1,max(y for x,y in gesture.points)+1)
                    targets.check_scroll(tree.graph, tree.graph, region)
                    # Reject a clickable layer across the path, except its containing surface.
                    if any(n.visible and n.attrs.get('clickable') == 'true' and targets.intersects(n.bounds, region)
                           and not (n.bounds[0] <= region[0] and n.bounds[1] <= region[1]
                                    and n.bounds[2] >= region[2] and n.bounds[3] >= region[3])
                           for n in tree.graph.nodes):
                        raise MobileError('Interactive layer crosses navigation swipe; inspect first')
                if self._read('header') != header or self.cancel.is_set():
                    raise MobileError('Foreground changed or stopped before navigation input')
                if time.monotonic() >= step_deadline:
                    raise MobileError('Navigation step timed out before input')
                row['resolve_ms'] = round((time.monotonic()-at)*1000)
                row['action'] = gesture.as_dict() if gesture else {'kind':'launch','package':step.package}
                self.observation = None
                row['input_status'] = self._input_phase = 'unknown'
                self._log('navigation_input_start', **row)
                at = time.monotonic()
                try:
                    if gesture:
                        self.driver.execute(gesture)
                    else:
                        self.driver.launch(step.package)
                except Exception as exc:
                    self._log('navigation_input_unknown', index=index,
                              exception=self._exception_details(exc, f'input.{step.kind}'))
                    raise
                finally:
                    self._record_time('execution_ms', at)
                sent_mono = time.monotonic()
                row.update(input_status='sent', status='completed', sent_at=datetime.now(timezone.utc).isoformat(),
                           execution_ms=round((time.monotonic()-at)*1000))
                self._input_phase = 'sent'
                self._log('navigation_input_sent', **row)
                if step.kind == 'launch':
                    # Launch acknowledgement and foreground transition are different outcomes.
                    while True:
                        h, _ = self._sequence_check(baseline, plan.allowed_packages)
                        if time.monotonic() >= step_deadline:
                            raise MobileError('Launched app did not become foreground before deadline')
                        if h['package'] == step.package:
                            break
                        self.cancel.wait(.15)
            if plan.settle_ms:
                at = time.monotonic()
                if self.cancel.wait(min(plan.settle_ms/1000, max(0, deadline-at))):
                    raise MobileError('Stopped during final navigation delay')
                self._record_time('settle_ms', at)
            if time.monotonic() >= deadline:
                raise MobileError('Navigation budget exhausted')
            result['status'] = 'completed'
            return deadline, sent_mono
        except Exception:
            result['status'] = 'interrupted'
            if result['steps']:
                result['steps'][-1]['status'] = 'interrupted'
            raise
        finally:
            result['completed_inputs'] = sum(s['input_status'] == 'sent' for s in result['steps'])
            result['elapsed_ms'] = round((time.monotonic()-started)*1000)
            self._log('navigation_finished', **result)

    def _navigation_observe(self, plan, result, deadline):
        from .service import MobileError
        if self.cancel.is_set():
            raise MobileError('Stopped before final navigation observation')
        if plan.until is None:
            return self._observe(wait_ms=0)
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise MobileError('Navigation budget exhausted before final condition')
        required = WaitCondition(text=plan.until.text, package=plan.steps[-1].package)
        try:
            data, obs = self._wait_state(required, remaining)
        except MobileError as exc:
            if 'wait' in exc.details:
                result['wait'] = exc.details['wait']
            raise
        result['wait'] = data['wait']
        if data['wait']['status'] != 'matched':
            raise MobileError('Final navigation condition did not match', partial_observation=obs)
        return obs

    def _navigate(self, old, plan, follow_up=None):
        from .service import MobileError
        result = {}
        try:
            deadline, sent_mono = self._navigation_run(old.header, plan, result)
            if follow_up is not None:
                # No post-input tree/image before the input-anchored timer.
                sequence, obs = self._local_sequence(old, follow_up=follow_up,
                    sent_mono=sent_mono, sent_at=result['steps'][-1]['sent_at'])
                return {'navigation': result, **sequence}, obs
            obs = self._navigation_observe(plan, result, deadline)
            if not obs.actionable or obs.header['package'] != plan.steps[-1].package:
                raise MobileError('Final navigation state is inconsistent or outside the destination', partial_observation=obs)
            return {'navigation': result, 'input_status': self._input_phase}, obs
        except Exception as exc:
            self._log('navigation_interrupted', exception=self._exception_details(exc, 'navigation'))
            if not (follow_up and result.get('status') == 'completed'):
                result['status'] = 'interrupted'
            obs = getattr(exc, 'partial_observation', None)
            details = getattr(exc, 'details', {})
            if (obs is None and not self.cancel.is_set() and
                    'continuation' not in details and 'recovery' not in details):
                try:
                    obs = self._observe(wait_ms=0)
                except Exception:
                    pass
            self.observation = obs
            reason = str(exc) if isinstance(exc, (MobileError, ValueError)) else type(exc).__name__
            raise MobileError(f'Navigation stopped: {reason}. Inspect steps; do not replay',
                              partial_observation=obs, **{**details, 'navigation': result}) from None
