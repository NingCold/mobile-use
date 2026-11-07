"""Action-specific validation and independently timestamped observations."""
import time
import uuid
from datetime import datetime, timezone

from . import targets
from .images import image_evidence
from .vision import LocalVision, bind_reference, bind_declared, relocate, PixelComparison, patch_delta, normalized, inside, choose_declared
from .options import VisualTarget, VisualTargetAnyOf
from .readiness import confirm_target


class TargetValidation:
    def _query_visual_batch(self, obs, batch):
        """Evaluate independent caller-defined facts on one immutable frame."""
        if self._vision is None:
            self._vision = LocalVision()
        wanted = tuple(dict.fromkeys(v for s in [*batch.conditions, *batch.candidates]
                                     for v in (s.text, s.context_text) if v is not None))
        labels = self._vision.read(obs.png, texts=wanted, region=batch.search_bounds)
        size, graph = (obs.header['width'], obs.header['height']), obs.tree.graph
        records = []
        for c in batch.conditions:
            record = {'name': c.name, 'text': c.text}
            try:
                binding = bind_declared(labels, c.selector(batch.search_bounds), size, graph)
                record.update(status='matched', bounds=list(binding.target.bounds),
                              confidence=round(binding.target.score, 3), context_text=c.context_text,
                              context_bounds=[list(v.bounds) for v in binding.contexts])
            except targets.TargetError as exc:
                record.update(status={'visual_target_missing': 'missing', 'visual_target_ambiguous': 'ambiguous',
                                      'visual_low_confidence': 'low_confidence'}.get(exc.code, 'unresolved'), code=exc.code)
            records.append(record)
        unmet = [r['name'] for r in records if r['name'] in batch.require and r['status'] != 'matched']
        result = {'conditions': records, 'require': list(batch.require), 'unmet': unmet,
                  'source': 'cached_image_ocr', 'search_bounds': batch.search_bounds}
        if batch.candidates:
            required = [c for c in batch.conditions if c.name in batch.require]
            result['target'] = self._query_visual_choices(obs,
                VisualTargetAnyOf(batch.candidates, batch.search_bounds), labels=labels,
                required=required, eligible=not unmet)
        obs.visual_result = {}
        obs.visual_batch_result = result

    def _query_visual_choices(self, obs, choices, *, labels=None, required=(), eligible=True):
        from dataclasses import asdict
        specs = [VisualTarget(s.text, s.context_text, s.search_bounds) for s in choices.selectors()]
        if self._vision is None:
            self._vision = LocalVision()
        wanted = tuple(dict.fromkeys(v for s in specs for v in (s.text, s.context_text)))
        if labels is None:
            labels = self._vision.read(obs.png, texts=wanted, region=choices.search_bounds)
        for spec in specs:
            obs.visual_queries[(spec.text, spec.context_text, tuple(spec.search_bounds or ()))] = labels
        result = {'query_any_of': asdict(choices), 'candidates': [], 'truncated': False,
                  'source': 'cached_image_ocr', 'scope': 'all declared alternatives; fresh confirmation required'}
        try:
            spec, binding = choose_declared(labels, specs, (obs.header['width'], obs.header['height']), obs.tree.graph)
            value = (spec, binding.target.bounds)
            candidate = {'text': spec.text,
                'bounds': list(binding.target.bounds), 'confidence': round(binding.target.score, 3),
                'context_text': spec.context_text, 'context_bounds': [list(c.bounds) for c in binding.contexts]}
            if eligible:
                cid = next((k for k, v in obs.visual_candidates.items()
                            if v == value and obs.visual_choice_sets.get(k) == specs
                            and obs.visual_condition_sets.get(k, []) == list(required)), None)
                if cid is None:
                    cid = 'v' + uuid.uuid4().hex[:12]
                    obs.visual_candidates[cid] = value
                    obs.visual_choice_sets[cid] = specs
                    if required:
                        obs.visual_condition_sets[cid] = list(required)
                candidate['id'] = cid
                result.update(status='matched', candidates=[candidate])
            else:
                result.update(status='blocked', code='visual_conditions_unmet', candidates=[candidate])
        except targets.TargetError as exc:
            matches = [v for v in labels if any(normalized(v.text) == normalized(s.text) for s in specs)]
            result.update(status='unresolved', code=exc.code,
                candidates=[{'text': v.text, 'bounds': list(v.bounds), 'confidence': round(v.score, 3),
                             'rejection': exc.code} for v in matches[:16]], truncated=len(matches)>16)
        obs.visual_result = result
        obs.visual_batch_result = {}
        return result

    def _cached_visual_labels(self, obs, spec):
        key = (spec.text, spec.context_text, tuple(spec.search_bounds or ()))
        if key not in obs.visual_queries:
            if self._vision is None:
                self._vision = LocalVision()
            obs.visual_queries[key] = self._vision.read(obs.png, texts=(spec.text, spec.context_text), region=spec.search_bounds)
        return obs.visual_queries[key]

    def _query_visual_candidates(self, obs, spec):
        """Interpret only cached pixels. Candidate IDs never outlive their observation."""
        from dataclasses import asdict
        labels = self._cached_visual_labels(obs, spec)
        scope = spec.search_bounds or (0, 0, obs.header['width'], obs.header['height'])
        matches = [v for v in labels if normalized(v.text) == normalized(spec.text) and inside(v.bounds, scope)]
        records = []
        for label in matches[:16]:
            record = {'text': label.text, 'bounds': list(label.bounds), 'confidence': round(label.score, 3)}
            try:
                binding = bind_reference(labels, spec, label.bounds, (obs.header['width'], obs.header['height']), obs.tree.graph)
                value = (spec, label.bounds)
                candidate_id = next((key for key, item in obs.visual_candidates.items()
                                     if item == value and key not in obs.visual_choice_sets), None)
                if candidate_id is None:
                    candidate_id = 'v' + uuid.uuid4().hex[:12]
                    obs.visual_candidates[candidate_id] = value
                record.update(id=candidate_id, context_text=spec.context_text,
                              context_bounds=[list(c.bounds) for c in binding.contexts])
            except targets.TargetError as exc:
                record['rejection'] = exc.code
            records.append(record)
        obs.visual_result = {'query': asdict(spec), 'candidates': records, 'truncated': len(matches) > 16,
                             'source': 'cached_image_ocr', 'scope': 'label evidence; inspect image before choosing'}
        obs.visual_batch_result = {}

    def _capture_target(self, initial_state=None, verify_after_image=False, sampled_image=None):
        from .service import Observation, MobileError
        started = time.monotonic()
        for attempt in range(1, 3):
            header, tree = initial_state if initial_state is not None else self._state()
            initial_state = None
            capture = {'tree_at': datetime.now(timezone.utc).isoformat(),
                       'image_at': None, 'image_size': None, 'attempts': attempt}
            png, size = b'', None
            hint = self._image_hint
            if self._input_id and (self._validation_result or {}).get('kind') in ('visual', 'drag'):
                hint = 'visual_action'
            reasons, visual_regions = image_evidence(header, tree, self._last_observation,
                                                    recovering=attempt > 1, hint=hint)
            if sampled_image is not None:
                reasons.append('visual_recovery')
            capture_image = self._image_policy == 'always' or (self._image_policy == 'auto' and bool(reasons))
            capture.update(image_policy=self._image_policy, image_reasons=reasons,
                           visual_regions=visual_regions)
            if sampled_image is not None and sampled_image['header'] == header:
                png, size = sampled_image['png'], sampled_image['size']
                capture.update(image_at=sampled_image['image_at'], image_size=list(size),
                               image_source='visual_validation', screenshot_ms=0)
                capture_image = True
            elif capture_image:
                at = time.monotonic()
                png, size = self._read('screenshot')
                capture.update(image_at=datetime.now(timezone.utc).isoformat(),
                               image_size=list(size), screenshot_ms=round((time.monotonic()-at)*1000))
            sampled_image = None  # Never reuse across a failed consistency check.
            if capture_image and verify_after_image:
                after,tree=self._state()
                capture['tree_at']=datetime.now(timezone.utc).isoformat()
            else:
                after = self._read('header')
            coherent = header == after and (size is None or size == (header['width'],header['height']))
            capture.update(checked_at=datetime.now(timezone.utc).isoformat(),
                           total_ms=round((time.monotonic()-started)*1000))
            obs = Observation(uuid.uuid4().hex[:16], time.monotonic(), header, tree.elements,
                              tree.digest, png, None, tree, actionable=coherent,
                              consistency='sampled' if coherent else 'inconsistent',
                              capture=capture, validation_mode='target',
                              image_reasons=reasons,
                              issues=[] if coherent else [{'reason':'Foreground/display changed during capture'}])
            if coherent:
                return obs
            if self.cancel.is_set():
                raise MobileError('Stopped during capture')
        return obs

    def _visual_guard(self, old, header, bounds):
        from .service import MobileError
        if not old.png:
            raise MobileError('Visual coordinates require an inspected image. Call observe(image="always")',
                              stage='preflight', code='image_required')
        png, size = self._read('screenshot')
        image_at = datetime.now(timezone.utc).isoformat()
        if size != (header['width'],header['height']) or self._read('header') != header:
            raise MobileError('Display changed during visual validation', stage='preflight', code='display_changed')
        mean, hot = patch_delta(old.png, png, bounds)
        if mean > 4 or hot > .02:
            # The validation frame already shows why this input is rejected. Reuse
            # it with a fresh tree, only while its foreground/display still agrees.
            recovery = None
            current = self._state()
            if current[0] == header:
                recovery = self._remember_observation(self._capture_target(
                    initial_state=current, sampled_image={
                        'png': png, 'size': size, 'image_at': image_at, 'header': header}))
            raise MobileError('Visual target region changed. Inspect a fresh image before choosing coordinates',
                              partial_observation=recovery,
                              stage='preflight', code='visual_changed',
                              visual={'region':list(bounds), 'mean_channel_delta':round(mean,2),
                                      'changed_fraction':round(hot,4)})
        return {'region':list(bounds), 'mean_channel_delta':round(mean,2), 'changed_fraction':round(hot,4)}

    def _relocate_visual(self, old, header, region, spec, context_ids, tree, choices=None, conditions=None):
        """Resolve once locally; at most two read-only attempts, never retry input."""
        from .service import MobileError
        engine = self._vision
        if engine is None:
            self._vision = engine = LocalVision()
        if not old.png:
            raise MobileError('Visual relocation requires an inspected image',
                              stage='preflight', code='image_required')
        started = time.monotonic()
        frame = None
        try:
            conditions = conditions or []
            guard_specs = [c.selector(spec.search_bounds) for c in conditions]
            wanted = tuple(dict.fromkeys(v for s in [*(choices or [spec]), *guard_specs]
                                         for v in (s.text, s.context_text) if v is not None))
            reference_labels = (engine.read(old.png, texts=wanted, region=spec.search_bounds) if conditions
                                else self._cached_visual_labels(old, spec))
            reference = bind_reference(reference_labels, spec, region,
                                       (header['width'], header['height']), old.tree.graph)
            guard_refs = [bind_declared(reference_labels, s, (header['width'], header['height']), old.tree.graph)
                          for s in guard_specs]

            def check_conditions(labels, size, graph):
                regions = []
                for c, s, ref in zip(conditions, guard_specs, guard_refs):
                    try:
                        bind_declared(labels, s, size, graph)  # Still unique in the declared condition region.
                        label, contexts = relocate(labels, s, ref, size, graph)
                        regions.extend((label, *contexts))
                    except targets.TargetError as exc:
                        raise targets.TargetError('visual_condition_changed',
                            f'Required condition {c.name!r} no longer matches ({exc.code})') from None
                return regions
            self._record_time('visual_reference_ms', started)
            search_started = time.monotonic()

            def capture():
                self._current_observation(old.id)  # age, cancellation and connection
                png, size = self._read('screenshot')
                at = datetime.now(timezone.utc).isoformat()
                if size != (header['width'], header['height']) or self._read('header') != header:
                    raise targets.TargetError('foreground_changed', 'Display/foreground changed during visual search')
                return {'png': png, 'size': size, 'header': header, 'image_at': at}

            frame = capture()
            for attempt in range(1, 3):
                labels = engine.read(frame['png'], texts=wanted, region=spec.search_bounds)
                guards = check_conditions(labels, frame['size'], tree.graph)
                try:
                    if choices:
                        winner, _ = choose_declared(labels, choices, frame['size'], tree.graph)
                        if winner != spec:
                            raise targets.TargetError('visual_context_changed', 'The matched alternative changed')
                    target, contexts = relocate(labels, spec, reference, frame['size'], tree.graph)
                except targets.TargetError as exc:
                    if exc.code != 'visual_target_missing' or attempt == 2:
                        raise
                    frame = capture()
                    current_header, tree = self._state()
                    if current_header != header:
                        raise targets.TargetError('foreground_changed', 'Foreground changed during visual search')
                    continue  # A transient absent label can settle locally; never retry input.
                checked = capture()
                pixels = PixelComparison(frame['png'], checked['png'])
                consistent = all(pixels.stationary(item.bounds)
                                 for item in (target, *contexts, *guards))
                if choices and not pixels.same_search_pixels(spec.search_bounds):
                    labels = engine.read(checked['png'], texts=wanted, region=spec.search_bounds)
                frame = checked
                if time.monotonic()-search_started > 8:
                    raise targets.TargetError('visual_search_timeout', 'Local visual search exceeded eight seconds')
                if not consistent:
                    continue
                current_header, tree = self._state()
                if current_header != header or self._read('header') != header:
                    raise targets.TargetError('foreground_changed', 'Foreground changed after local visual search')
                previous, current = old.tree.graph, tree.graph
                guards = check_conditions(labels, frame['size'], current)
                if choices:
                    winner, _ = choose_declared(labels, choices, frame['size'], current)
                    if winner != spec:
                        raise targets.TargetError('visual_context_changed', 'The matched alternative changed during confirmation')
                target, contexts = relocate(labels, spec, reference, frame['size'], current)
                targets.check_layout(previous, current,
                                     [*reference.regions, *(b for ref in guard_refs for b in ref.regions)],
                                     [target.bounds, *(c.bounds for c in contexts), *(g.bounds for g in guards)])
                old_dialogs = {(targets.identity(n), n.bounds) for n in previous.nodes if 'Dialog' in n.attrs.get('class', '')}
                if any('Dialog' in n.attrs.get('class', '') and (targets.identity(n), n.bounds) not in old_dialogs
                       for n in current.nodes if n.visible):
                    raise targets.TargetError('window_changed', 'A dialog appeared during local visual search')
                for eid in context_ids or []:
                    element = targets.element_by_id(old.elements, eid)
                    targets.resolve(previous, current, element.node_index)
                self._current_observation(old.id)
                evidence = {'method': 'local_ocr_context', 'attempts': attempt,
                            'candidate_count': len(choices) if choices else 1,
                            'required_conditions': [c.name for c in conditions],
                            'reference_age_ms': round((time.monotonic()-old.created)*1000),
                            'text': spec.text, 'context_text': spec.context_text,
                            'previous_bounds': list(reference.target.bounds), 'resolved_bounds': list(target.bounds),
                            'context_bounds': [list(c.bounds) for c in contexts], 'confidence': round(target.score, 3),
                            'binding': 'container_and_labels' if reference.container else 'search_region_and_labels',
                            'confirmation': 'fresh_local_pixels',
                            'search_ms': round((time.monotonic()-search_started)*1000)}
                return target.bounds, evidence
            raise targets.TargetError('visual_unstable', 'Visual target moved during both local confirmation attempts')
        except targets.TargetError as exc:
            recovery = None
            if frame is not None and not self.cancel.is_set():
                current = self._state()
                if current[0] == header:
                    recovery = self._remember_observation(self._capture_target(initial_state=current, sampled_image=frame))
            diagnostic = {}
            if frame is None:
                diagnostic = {'reference_bounds': list(region), 'reference_observation_id': old.id,
                              'reference_labels': [{'text': v.text[:200], 'score': round(v.score, 3),
                                                    'bounds': list(v.bounds)} for v in locals().get('reference_labels', [])[:16]]}
                if self.save_screenshots:
                    path = self._artifact_path(f'{old.id}-visual-reference.png')
                    path.write_bytes(old.png)
                    diagnostic['reference_image'] = str(path)
            raise MobileError(str(exc), stage='preflight', code=exc.code, partial_observation=recovery,
                              visual_diagnostic=diagnostic) from None
        finally:
            self._record_time('visual_local_ms', started)

    def _validate_target(self, observation_id, kind, element=None, region=None, context_ids=None, container_index=None,
                         visual_target=None, readiness=None, visual_choices=None, visual_conditions=None):
        from .service import MobileError
        started = time.monotonic()
        self._validation_result = None
        try:
            old = self._current_observation(observation_id)
            bounds = None
            evidence = {'kind':kind, 'status':'ready', 'scope':'device'}
            if kind in ('launch','home'):
                if not self._read('header')['screen_on']:
                    raise MobileError('Device screen is off. Unlock manually', stage='preflight')
                self._validation_result = evidence
                return old, None
            header, tree = self._state()
            if header != old.header:
                raise MobileError('Display or foreground app changed', stage='preflight', code='foreground_changed')
            previous, current = old.tree.graph, tree.graph
            for eid in context_ids or []:
                e = targets.element_by_id(old.elements, eid)
                if e is None:
                    raise targets.TargetError('context_missing','Context ID is absent from the inspected observation')
                targets.resolve(previous,current,e.node_index)
            if element is not None:
                index = targets.resolve(previous,current,element.node_index,allow_empty=kind=='type')
                bounds = targets.check_target(previous,current,element.node_index,index)
                if readiness is not None:
                    header, tree, index, bounds, ready_evidence = confirm_target(
                        header, tree, index, sample=lambda: self._sequence_check(header, [header['package']], tree=True),
                        cancel=self.cancel, now=time.monotonic,
                        deadline=time.monotonic()+readiness.timeout_seconds, options=readiness)
                    current = tree.graph
                    # Additional facts remain bound through the readiness wait.
                    for eid in context_ids or []:
                        e = targets.element_by_id(old.elements, eid)
                        targets.resolve(previous, current, e.node_index)
                    evidence['readiness'] = ready_evidence
                if kind == 'type':
                    n = current.nodes[index]
                    if n.attrs.get('focused') != 'true' or not (
                            n.attrs.get('editable') == 'true' or 'EditText' in n.attrs.get('class','')):
                        raise targets.TargetError('focus_changed','The observed field is no longer focused and editable')
                evidence.update(scope='target_identity_context_visibility_windows',
                                status='relocated' if bounds != element.bounds else 'ready',
                                previous_bounds=list(element.bounds), resolved_bounds=list(bounds))
            else:
                regions = [region] if region is not None else None
                if visual_target is not None:
                    bounds, visual = self._relocate_visual(old, header, region, visual_target, context_ids, tree,
                                                          visual_choices, visual_conditions)
                    evidence.update(scope='window_and_visual_text_context', visual=visual,
                                    status='relocated' if visual['previous_bounds'] != list(bounds) else 'ready',
                                    previous_bounds=visual['previous_bounds'], resolved_bounds=list(bounds))
                elif kind in ('visual','drag'):
                    targets.check_layout(previous, current, regions, regions)
                    evidence.update(scope='window_and_local_pixels', visual=self._visual_guard(old,header,region))
                else:
                    targets.check_layout(previous, current, regions, regions)
                    evidence['scope'] = 'window_and_navigation_surface'
                if kind == 'swipe':
                    evidence['scope']=targets.check_scroll(previous,current,region,container_index)
            # A header read is cheap relative to XML; avoid input after a foreground race.
            if self._read('header') != header:
                raise targets.TargetError('foreground_changed','Foreground/display changed during validation')
            self._validation_result = evidence
            self._log('preflight_target', observation_id=old.id, **evidence)
            return old,bounds
        except targets.TargetError as exc:
            raise MobileError(str(exc), stage='preflight', code=exc.code) from None
        finally:
            self._record_time('validation_ms', started)
