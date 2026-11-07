"""Bounded local continuations. Application policy is supplied by the caller."""
from datetime import datetime, timezone
import time


class LocalSequences:
    def _sequence_check(self, baseline, packages, *, tree=False, allow_rotation=False):
        from .service import MobileError
        def check(header):
            if self.cancel.is_set():
                raise MobileError('Stopped during continuation')
            if not header['screen_on']:
                raise MobileError('Device screen is off. Unlock manually')
            # Timers based on input elapsed time and back keys carry no pixel target.
            # Accept a coherent quarter-turn of the same display, never an actual resize.
            turn = (header['rotation'] - baseline['rotation']) % 4
            expected = ((baseline['height'], baseline['width']) if turn % 2 else
                        (baseline['width'], baseline['height']))
            same_display = ((header['width'], header['height']) == expected if allow_rotation else
                            all(header[k] == baseline[k] for k in ('width', 'height', 'rotation')))
            if not same_display:
                raise MobileError('Display changed during continuation')
            if header['package'] not in packages:
                raise MobileError('Foreground is outside allowed_packages')

        for attempt in range(3):
            if self.cancel.is_set():
                raise MobileError('Stopped during continuation')
            header, state = self._state() if tree else (self._read('header'), None)
            check(header)
            if state:
                after = self._read('header')
                check(after)  # Unexpected foreground/off/resize never gets a retry exemption.
                if after != header:
                    self._log('sequence_read_transition', attempt=attempt + 1)
                    self.cancel.wait(.1)
                    continue  # Discard the mixed sample; retry reads only, not input.
            if state and state.graph and any(n.visible and 'Dialog' in n.attrs.get('class', '') for n in state.graph.nodes):
                raise MobileError('Dialog detected; inspect before further navigation')
            return header, state
        raise MobileError('Foreground did not settle within three continuation samples')

    def _sequence_back(self, baseline, options, steps):
        from .service import MobileError
        for _ in range(options.count):
            header, tree = self._sequence_check(baseline, options.allowed_packages, tree=True, allow_rotation=True)
            if options.stop_when and self._matches(options.stop_when, header, tree.elements):
                return 'stop_condition'
            # Do not send a navigation key on a different foreground than the tree sample.
            if self._read('header') != header or self.cancel.is_set():
                raise MobileError('Foreground changed or stopped before back')
            step = {'kind': 'back', 'input_status': 'unknown'}
            steps.append(step)
            self.observation = None
            self._input_phase = 'unknown'
            at = time.monotonic()
            self._log('sequence_input_start', index=len(steps), kind='back')
            try:
                self.driver.key('back')
            except Exception as exc:
                self._log('sequence_input_unknown', index=len(steps),
                          exception=self._exception_details(exc, 'input.back'))
                raise
            finally:
                self._record_time('execution_ms', at)
            self._input_phase = 'sent'
            step.update(input_status='sent', sent_at=datetime.now(timezone.utc).isoformat())
            self._log('sequence_input_sent', index=len(steps), **step)
            at = time.monotonic()
            cancelled = self.cancel.wait(options.interval_ms / 1000)
            self._record_time('settle_ms', at)
            if cancelled:
                raise MobileError('Stopped between navigation keys')
        return 'count_reached'

    def _sequence_wait(self, baseline, options, sent_mono, sent_at, result):
        from .service import MobileError
        started = time.monotonic()
        deadline = sent_mono + options.timeout_seconds
        anchor = sent_mono if options.anchor == 'input_sent' else None
        matched_header = None
        ready_seen = options.ready is None
        polls = 0
        use_tree = any(c and c.text is not None for c in (options.ready, options.completion))
        timing = result['timing'] = {
            'anchor': options.anchor, 'input_sent_at': sent_at,
            'timer_started_at': sent_at if anchor is not None else None,
            'duration_seconds': options.duration_seconds,
            'scope': ('elapsed_since_acknowledged_input; not verified content dwell' if anchor is not None
                      else 'sampled_matching_state; not business completion'), 'status': 'running'}
        try:
            while True:
                header, tree = self._sequence_check(baseline, options.allowed_packages, tree=use_tree,
                                                  allow_rotation=options.anchor == 'input_sent')
                polls += 1
                elements = tree.elements if tree else []
                now = time.monotonic()
                matches = options.ready is None or self._matches(options.ready, header, elements)
                completed = options.completion and self._matches(options.completion, header, elements)
                if options.anchor == 'state_matched' and anchor is not None and (
                        not matches or header != matched_header) and not completed:
                    timing['status'] = 'interrupted'
                    raise MobileError('Matched dwell state changed')
                if matches:
                    ready_seen = True
                    if anchor is None:
                        anchor, matched_header = now, header
                        timing['timer_started_at'] = datetime.now(timezone.utc).isoformat()
                timing.update(polls=polls, elapsed_since_input_ms=round((now-sent_mono)*1000),
                              timer_elapsed_ms=round((now-anchor)*1000) if anchor is not None else 0,
                              ready_seen=ready_seen)
                # A known accessible completion condition may finish before fallback duration.
                if completed:
                    timing['status'] = 'completion_matched'
                    return
                if ready_seen and anchor is not None and now-anchor >= options.duration_seconds:
                    timing['status'] = 'duration_reached'
                    return
                if now >= deadline:
                    timing['status'] = 'timeout'
                    raise MobileError('Continuation timed out before timing/ready condition completed')
                remaining = deadline-now
                if anchor is not None and options.duration_seconds > now-anchor:
                    remaining = min(remaining, options.duration_seconds-(now-anchor))
                self.cancel.wait(max(.001, min(.75, remaining)))
        finally:
            if timing['status'] == 'running':
                timing['status'] = 'cancelled' if self.cancel.is_set() else 'interrupted'
            self._record_time('waiting_ms', started)
            self._log('sequence_wait_finished', **timing)

    def _local_sequence(self, old, *, follow_up=None, back=None, sent_mono=None, sent_at=None):
        from .service import MobileError
        steps = ([{'kind': 'tap', 'input_status': 'sent', 'sent_at': sent_at}] if follow_up else [])
        result = {'status': 'running', 'steps': steps}
        planned_back = follow_up.return_back if follow_up else back
        self._log('sequence_start', anchor=follow_up.anchor if follow_up else None,
                  duration_seconds=follow_up.duration_seconds if follow_up else None,
                  timeout_seconds=follow_up.timeout_seconds if follow_up else None,
                  allowed_packages=follow_up.allowed_packages if follow_up else back.allowed_packages,
                  back_count=planned_back.count if planned_back else 0,
                  back_interval_ms=planned_back.interval_ms if planned_back else None,
                  back_allowed_packages=planned_back.allowed_packages if planned_back else None,
                  has_ready=bool(follow_up and follow_up.ready),
                  has_completion=bool(follow_up and follow_up.completion),
                  has_stop_when=bool(planned_back and planned_back.stop_when))
        self.observation = None
        try:
            if follow_up:
                self._sequence_wait(old.header, follow_up, sent_mono, sent_at, result)
                back = follow_up.return_back
            if back:
                result['back_status'] = self._sequence_back(old.header, back, steps)
                if back.after:
                    result['navigation'] = {}
                    navigation_deadline, _ = self._navigation_run(old.header, back.after, result['navigation'])
            if self.cancel.is_set():
                raise MobileError('Stopped before final observation')
            obs = (self._navigation_observe(back.after, result['navigation'], navigation_deadline)
                   if back and back.after else self._observe(wait_ms=0))
            if not obs.actionable:
                raise MobileError('Final observation is inconsistent', partial_observation=obs)
            # Final state still has to be within the explicitly authorized navigation boundary.
            packages = back.allowed_packages if back else follow_up.allowed_packages
            if obs.header['package'] not in packages:
                raise MobileError('Final foreground is outside allowed_packages', partial_observation=obs)
            if back and back.after and obs.header['package'] != back.after.steps[-1].package:
                raise MobileError('Final navigation destination changed', partial_observation=obs)
            if follow_up and not back:
                required = (follow_up.completion if result['timing']['status'] == 'completion_matched'
                            else follow_up.ready if follow_up.anchor == 'state_matched' else None)
                if required and not self._matches(required, obs.header, obs.elements):
                    raise MobileError('Timed condition lost during final observation', partial_observation=obs)
            result['status'] = 'completed'
            self._log('sequence_finished', **result)
            return {'continuation': result, 'input_status': self._input_phase}, obs
        except Exception as exc:
            result['status'] = 'interrupted'
            if result.get('navigation'):
                result['navigation']['status'] = 'interrupted'
            result['completed_inputs'] = (sum(s['input_status'] == 'sent' for s in steps) +
                                          result.get('navigation', {}).get('completed_inputs', 0))
            obs = getattr(exc, 'partial_observation', None)
            if obs is None and not self.cancel.is_set():
                try:
                    obs = self._observe(wait_ms=0)
                except Exception:
                    pass
            self.observation = obs
            self._log('sequence_interrupted', **result,
                      exception=self._exception_details(exc, 'continuation'))
            reason = str(exc) if isinstance(exc, MobileError) else type(exc).__name__
            raise MobileError(f'Continuation stopped: {reason}. Inspect completed steps; do not replay',
                              partial_observation=obs, continuation=result) from None
