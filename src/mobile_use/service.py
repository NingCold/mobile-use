from __future__ import annotations

import hashlib
import json
import re
import secrets
import tempfile
import threading
import time
import uuid
from functools import wraps
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from .driver import AndroidDriver
from .diagnostics import exception_summary
from .gestures import check_point, swipe_plan, tap_plan
from . import __version__
from .options import MAX_VISUAL_ALTERNATIVES, MAX_CACHED_VISUAL_ALTERNATIVES, visual_choices_option, WaitCondition, condition, seconds, focus_option, target_option
from .options import MAX_VISUAL_CONDITIONS, MAX_TARGET_CONTEXTS, visual_batch_option
from .delivery import element_page, brief_observation
from . import targets
from .validation import TargetValidation
from .sequences import LocalSequences
from .navigation import LocalNavigation
from .options import follow_up_option, back_option, visual_target_option, navigation_option, readiness_option
from .state import (Element, TreeState, inspect_tree, tree_diff, unrelated_text_only,
                    localized_motion, localized_text_updates, localized_subtree_updates, localized_passive_updates, overlaps_regions)


class MobileError(Exception):
    def __init__(self, message, *, partial_observation=None, **details):
        super().__init__(message)
        self.details = details
        self.partial_observation = partial_observation


def timed_phase(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            started = time.monotonic()
            try:
                return fn(self, *args, **kwargs)
            finally:
                self._record_time(name, started)
        return wrapped
    return decorate


def audited_input(fn):
    @wraps(fn)
    def wrapped(self, observation_id, *args, **kwargs):
        with self.lock:
            self._input_id = uuid.uuid4().hex[:12]
            self._input_phase = "not_sent"
            started = time.monotonic()
            try:
                return fn(self, observation_id, *args, **kwargs)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, (MobileError, ValueError)) else type(exc).__name__
                details = {"input_status": self._input_phase, "operation": fn.__name__,
                           "request_id": self._input_id, "observation_id": observation_id,
                           "error_type": type(exc).__name__, "reason": reason,
                           **(exc.details if isinstance(exc, MobileError) else {})}
                self._log("input_rejected" if self._input_phase == "not_sent" else "input_failed",
                          **details, elapsed_ms=round((time.monotonic() - started) * 1000))
                raise MobileError(reason, partial_observation=getattr(exc, 'partial_observation', None), **details) from None
            finally:
                self._input_id = None
    return wrapped


@dataclass
class Observation:
    id: str
    created: float
    header: dict
    elements: list[Element]
    digest: str
    png: bytes = field(repr=False)
    stable: bool | None = False
    tree: TreeState | None = field(default=None, repr=False)
    unstable_regions: list = field(default_factory=list)
    actionable: bool = True
    consistency: str = "consistent"
    capture: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    image_reasons: list = field(default_factory=list)
    validation_mode: str = 'legacy'
    source_call: dict = field(default_factory=dict, repr=False)
    visual_queries: dict = field(default_factory=dict, repr=False)
    visual_candidates: dict = field(default_factory=dict, repr=False)
    visual_choice_sets: dict = field(default_factory=dict, repr=False)
    visual_condition_sets: dict = field(default_factory=dict, repr=False)
    visual_result: dict = field(default_factory=dict, repr=False)
    visual_batch_result: dict = field(default_factory=dict, repr=False)

    def metadata(self, detail="full", focus=None, *, limit=None, cursor=None) -> dict:
        focus = focus_option(focus)
        selected, selection, pagination, truncated = element_page(
            self.elements, self.id, focus, limit, cursor)
        elements = [e.as_dict() for e in selected]
        compact = detail in ('compact', 'brief')
        if self.validation_mode == 'target' and self.tree and self.tree.graph:
            for element, record in zip(selected, elements):
                aligned = targets.aligned_labels(self.tree.graph, element.node_index)
                if aligned:
                    record['aligned_context'] = ([v[:120] for v in aligned[:6]] if compact else list(aligned))
                    if compact and (len(aligned)>6 or any(len(v)>120 for v in aligned[:6])):
                        record['aligned_context_truncated'] = True
                context = targets.scope(self.tree.graph, element.node_index)
                if context:
                    values=list(context[1])
                    if compact:
                        record['context']=[v[:120] for v in values[:6]]
                        if len(values)>6 or any(len(v)>120 for v in values[:6]):
                            record['context_truncated']=True
                    else:
                        record['context']=values
        if compact:
            for record in elements:
                for key in ('text', 'description'):
                    value=record.get(key)
                    if value and len(value)>256:
                        record[key]=value[:256]
                        record[key+'_truncated']=True
            # Complete current element list, not a delta. IDs are still scoped
            # to this observation. Full records can be read from the same cache.
            elements = [{k: v for k, v in e.items()
                         if k in ("id", "bounds", "enabled") or
                         (k in ("text", "description", "resource_id", "clickable", "scrollable",
                                "focused", "editable", "password", "context", "context_truncated",
                                "aligned_context", "aligned_context_truncated",
                                "text_truncated", "description_truncated") and v)} for e in elements]
        elif detail != "full":
            raise ValueError("detail must be brief, compact or full")
        metadata = {"observation_id": self.id, **self.header, "stable": self.stable,
                "stability_scope": ('action_specific; global stability not required' if self.validation_mode == 'target'
                                    else 'accessibility_tree_only; inspect screenshot for loading/animation'),
                "validation_mode": self.validation_mode,
                "actionable": self.actionable, "consistency": self.consistency,
                "capture": self.capture, "issues": self.issues, "summary": self.summary,
                "image_reasons": self.image_reasons, "detail": detail,
                "unstable_regions": self.unstable_regions,
                "coordinate_space": "original screenshot pixels (no resize)",
                "elements": elements, "element_count": len(self.elements),
                "returned_element_count": len(elements), "elements_filtered": focus is not None,
                "focus": selection,
                "elements_truncated": truncated,
                "tree_diagnostics": self.tree.diagnostics if self.tree else {}}
        if pagination is not None:
            metadata['pagination'] = pagination
        if self.visual_result:
            metadata['visual_candidates'] = self.visual_result
        if self.visual_batch_result:
            metadata['visual_batch'] = self.visual_batch_result
        return brief_observation(metadata) if detail == 'brief' else metadata


class MobileService(TargetValidation, LocalSequences, LocalNavigation):
    def __init__(self, artifact_dir: Path, driver_factory=AndroidDriver, save_screenshots=False,
                 max_age_seconds=180, validation_mode='target'):
        if validation_mode not in ('target', 'legacy'):
            raise ValueError('Unknown validation mode')
        self.validation_mode = validation_mode
        self._image_policy = 'always'  # direct Python callers retain explicit image evidence
        self._image_hint = None
        self._reads = None
        self._validation_result = None
        self._vision = None
        self.driver_factory = driver_factory
        self.driver = None
        self.observation: Observation | None = None
        self._last_observation: Observation | None = None  # comparison only, never authorizes input
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.lease = None
        self.save_screenshots = save_screenshots
        self.max_age_seconds = max_age_seconds
        self._input_id = None
        self._metrics = None
        self._last_call_finished = None
        self._after_input_wait = None
        self._after_input_settle_ms = 0
        self.run_dir = artifact_dir / datetime.now().strftime("%Y%m%d-%H%M%S") / uuid.uuid4().hex[:8]
        self._run_dir_ready = False

    def _artifact_path(self, name):
        if not self._run_dir_ready:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._run_dir_ready = True
        return self.run_dir / name

    def _exception_details(self, exc, stage, sensitive_values=()):
        values = list(sensitive_values)
        obs = self._last_observation
        if obs and obs.tree and obs.tree.graph:
            for node in obs.tree.graph.nodes:
                a = node.attrs
                if (a.get('password') == 'true' or a.get('editable') == 'true' or
                        'EditText' in a.get('class', '')):
                    values.extend(a.get(k, '') for k in ('text', 'content-desc'))
        return exception_summary(exc, stage, values)

    def _log(self, event: str, **values):
        data = {"time": datetime.now(timezone.utc).isoformat(), "event": event,
                "device": self.driver.serial if self.driver else None,
                "request_id": self._input_id, **values}
        with self._artifact_path("actions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def list_devices(self):
        return {"devices": self.driver_factory.list_devices()}

    def _record_time(self, name, started):
        if self._metrics is not None:
            self._metrics[name] = self._metrics.get(name, 0) + (time.monotonic() - started) * 1000

    def get_status(self):
        """Process capabilities only: no device read, pairing, or lease acquisition."""
        return {"server_version": __version__, "connected": self.driver is not None,
                "serial": self.driver.serial if self.driver else None, "stopped": self.cancel.is_set(),
                "capabilities": {"action_until": True, "wait_for_state": True,
                                 "tap_follow_up": True, "back_sequence": True,
                                 "local_navigation": self.validation_mode == 'target',
                                 "visual_navigation": self.validation_mode == 'target',
                                 "visual_target_any_of": self.validation_mode == 'target',
                                 "navigation_follow_up": self.validation_mode == 'target',
                                 "visual_candidates": self.validation_mode == 'target',
                                 "cached_visual_alternatives": self.validation_mode == 'target',
                                 "same_frame_conditions": self.validation_mode == 'target',
                                 "declared_target_context": self.validation_mode == 'target',
                                 "action_region_windows": self.validation_mode == 'target',
                                 "shared_ocr_frames": True,
                                 "rejection_observation": True, "focus": True, "call_timings": True,
                                 "post_input_settle": True,
                                 "compact_image_preview": True, "compact_label_limits": True,
                                 "brief_responses": True, "cached_pagination": True,
                                 "tree_first_images": self.validation_mode == 'target',
                                 "local_visual_regions": self.validation_mode == 'target',
                                 "cache_auto_text_only": True,
                                 "aligned_peer_context": self.validation_mode == 'target',
                                 "validation_mode": self.validation_mode,
                                 "target_relocation": self.validation_mode == 'target',
                                 "local_visual_relocation": self.validation_mode == 'target',
                                 "visual_container_binding": self.validation_mode == 'target',
                                 "tap_readiness": self.validation_mode == 'target',
                                 "true_text_only": self.validation_mode == 'target'},
                "limits": {"wait_seconds": 30, "settle_ms": 2000, "observation_age_seconds": self.max_age_seconds,
                           "back_count": 3, "back_interval_ms": [200, 2000],
                           "navigation_steps": 8, "navigation_seconds": 30,
                           "visual_target_candidates": MAX_VISUAL_ALTERNATIVES if self.validation_mode == 'target' else 0,
                           "cached_visual_candidates": MAX_CACHED_VISUAL_ALTERNATIVES if self.validation_mode == 'target' else 0,
                           "visual_conditions": MAX_VISUAL_CONDITIONS if self.validation_mode == 'target' else 0,
                           "target_context_texts": MAX_TARGET_CONTEXTS if self.validation_mode == 'target' else 0,
                           "compact_image_max_edge": 1280, "compact_label_characters": 256,
                           "cached_page_elements": 250},
                "trace_dir": str(self.run_dir)}

    def connect(self, serial: str):
        with self.lock:
            if self.driver:
                if self.driver.serial == serial:
                    return self.get_status()
                raise MobileError("Disconnect before selecting a different device")
            if serial not in [d["serial"] for d in self.driver_factory.list_devices()]:
                raise MobileError("Select an online serial returned by list_devices; no implicit reconnect or pairing")
            lock_dir = Path(tempfile.gettempdir()) / "codex-mobile-use-locks"
            lock_dir.mkdir(exist_ok=True)
            self.lease = FileLock(str(lock_dir / (hashlib.sha256(serial.encode()).hexdigest() + ".lock")), thread_local=False)
            try:
                self.lease.acquire(timeout=0)
            except Timeout:
                self.lease = None
                raise MobileError("Another Mobile Use process owns this device; disconnect it first") from None
            try:
                self.driver = self.driver_factory(serial)
            except Exception:
                self.lease.release()
                self.lease = None
                raise
            self.cancel.clear()
            self._log("connect")
            return self.get_status()

    def disconnect(self):
        self.cancel.set()
        with self.lock:
            if self.driver is not None or self._run_dir_ready:
                self._log("disconnect")
            self.observation = None
            self._last_observation = None
            self.driver = None
            if self._vision is not None and hasattr(self._vision, 'clear_cache'):
                self._vision.clear_cache()
            if self.lease:
                self.lease.release()
                self.lease = None
            return {"disconnected": True}

    def stop(self):
        self.cancel.set()
        return {"stopped": True, "detail": "Further inputs blocked. An in-flight Android RPC may finish. Observe before resuming."}

    def _require_device(self):
        if self.driver is None:
            raise MobileError("Call connect with an explicit device serial first")

    def _read(self, method):
        if self._reads is not None:
            self._reads[method] = self._reads.get(method, 0) + 1
        try:
            return getattr(self.driver, method)()
        except Exception as exc:
            self._log('device_read_failed', exception=self._exception_details(exc, f'read.{method}'))
            raise

    def _state(self):
        self._require_device()
        header = self._read('header')
        if not header["screen_on"]:
            raise MobileError("Device screen is off. Turn it on and unlock manually")
        xml = self._read('hierarchy')
        tree = inspect_tree(xml, header["width"], header["height"])
        tree.diagnostics.update(getattr(self.driver, "hierarchy_diagnostics", {}))
        return header, tree

    @timed_phase("capture_ms")
    def _observe(self, wait_ms=1200, initial_state=None, verify_after_image=False):
        if self.validation_mode == 'target':
            return self._remember_observation(self._capture_target(initial_state,verify_after_image))
        # A transition can race the screenshot. Retry only reads, never input.
        started = time.monotonic()
        for attempt in range(1, 4):
            try:
                obs = self._capture_observation(wait_ms)
                break
            except MobileError as exc:
                if exc.details.get("stage") != "capture_consistency":
                    raise
                if attempt == 3:
                    if exc.partial_observation is None:
                        raise
                    obs = exc.partial_observation
                    self._log("observation_inconsistent", reason=str(exc), **exc.details)
                    break
                self._log("observation_retry", attempt=attempt, reason=str(exc), **exc.details)
                if self.cancel.wait(0.12):
                    raise MobileError("Stopped during observation recovery") from None
        obs.capture.update(attempts=attempt, total_ms=round((time.monotonic() - started) * 1000))
        return self._remember_observation(obs)

    def _remember_observation(self, obs):
        previous = self._last_observation
        difference = tree_diff(previous.tree, obs.tree) if previous and previous.tree else None
        header_changed = [k for k in obs.header if previous and previous.header.get(k) != obs.header[k]]
        obs.summary = {"base_observation_id": previous.id if previous else None,
                       "header_changed": header_changed,
                       "tree_changed": previous.digest != obs.digest if previous else None}
        if difference:
            obs.summary["changes"] = {k: difference[k] for k in
                ("changed_nodes", "matched_changed_nodes", "added_nodes", "removed_nodes")}
            obs.summary["changed_fields"] = sorted({f for c in difference["changes"] for f in c["fields"]})
            obs.summary["changed_fields_truncated"] = difference["changes_truncated"]
        reasons = []
        if previous is None:
            reasons.append("first_observation")
        if header_changed:
            reasons.append("foreground_or_display_changed")
        if not obs.actionable or (previous and not previous.actionable):
            reasons.append("capture_inconsistent_or_recovering")
        if obs.stable is False or obs.unstable_regions:
            reasons.append("dynamic_state")
        if len(obs.elements) <= 2:
            reasons.append("sparse_accessibility_tree")
        if len(obs.elements) > 250:
            reasons.append("truncated_elements")
        if obs.tree and any(any(kind in n["class"] for kind in ("WebView", "SurfaceView", "TextureView"))
                            for n in obs.tree.nodes):
            reasons.append("embedded_or_drawn_surface")
        if difference and (difference["added_nodes"] or difference["removed_nodes"] or
                           difference["changes_truncated"] or
                           set(obs.summary["changed_fields"]) - {"text", "content-desc"}):
            reasons.append("layout_or_interaction_changed")
        if self.validation_mode == 'target':
            # The target sampler chose these before acquisition. Layout/text
            # differences are diagnostics, not a mandate to capture every frame.
            reasons = obs.image_reasons
            if not obs.actionable and 'capture_inconsistent_or_recovering' not in reasons:
                reasons.append('capture_inconsistent_or_recovering')
        obs.image_reasons = reasons
        self.observation = self._last_observation = obs
        if self.save_screenshots and obs.png:
            self._artifact_path(f"{obs.id}.png").write_bytes(obs.png)
        self._log("observe", observation_id=obs.id, package=obs.header["package"], stable=obs.stable,
                  actionable=obs.actionable, consistency=obs.consistency, summary=obs.summary,
                  capture=obs.capture, image_reasons=reasons,
                  width=obs.header["width"], height=obs.header["height"], tree_diagnostics=obs.tree.diagnostics)
        return obs

    def _capture_observation(self, wait_ms):
        deadline = time.monotonic() + wait_ms / 1000
        previous = None
        stable = False
        while True:
            header, tree = self._state()
            state_key = (header, tree.digest)
            if previous == state_key:
                stable = True
                break
            if time.monotonic() >= deadline:
                break
            previous = state_key
            time.sleep(0.12)
        tree_at = datetime.now(timezone.utc).isoformat()
        image_start = time.monotonic()
        png, size = self._read('screenshot')
        capture = {"tree_at": tree_at, "image_at": datetime.now(timezone.utc).isoformat(),
                   "image_size": list(size), "screenshot_ms": round((time.monotonic() - image_start) * 1000)}
        after = self._read('header')
        capture["checked_header"] = after

        def inconsistent(message, **details):
            capture["checked_at"] = datetime.now(timezone.utc).isoformat()
            partial = Observation(uuid.uuid4().hex[:16], time.monotonic(), header, tree.elements,
                                  tree.digest, png, False, tree, actionable=False,
                                  consistency="inconsistent", capture=capture,
                                  issues=[{"reason": message, **details}])
            self.observation = None
            raise MobileError(message, partial_observation=partial, stage="capture_consistency", **details)

        if after != header or size != (header["width"], header["height"]):
            inconsistent("Display changed while capturing. Observe again before acting",
                         header_changed=[k for k in header if header[k] != after.get(k)], screenshot_size=list(size))
        # Check the tree again after the screenshot so it cannot silently lag behind it.
        check_tree = inspect_tree(self._read('hierarchy'), *size)
        motion = []
        if check_tree.digest != tree.digest:
            motion = (localized_motion(tree, check_tree) or localized_text_updates(tree, check_tree)
                      or localized_subtree_updates(tree, check_tree) or localized_passive_updates(tree, check_tree))
            if not motion:
                inconsistent("UI changed while capturing screenshot. Observe again",
                             difference=tree_diff(tree, check_tree))
            stable = False
            self._log("observation_local_motion", unstable_regions=motion)
        capture["checked_at"] = datetime.now(timezone.utc).isoformat()
        return Observation(uuid.uuid4().hex[:16], time.monotonic(), header, tree.elements,
                           tree.digest, png, stable, tree, motion,
                           consistency="localized_changes" if motion else "consistent", capture=capture)

    def observe(self, resume=False):
        with self.lock:
            self.observation = None
            if resume:
                self.cancel.clear()
            return self._observe()

    def get_observation(self, observation_id, visual_query=None, visual_query_any_of=None, visual_query_batch=None):
        """Read the latest cached observation without recapturing or clearing stop."""
        with self.lock:
            self._require_device()
            obs = self.observation
            if obs is None or obs.id != observation_id:
                raise MobileError("Stale observation ID. Call observe for a fresh snapshot")
            if time.monotonic() - obs.created > self.max_age_seconds:
                self.observation = None
                raise MobileError("Observation expired. Call observe again")
            queries = (visual_query, visual_query_any_of, visual_query_batch)
            if sum(q is not None for q in queries) > 1:
                raise ValueError('Use one of visual_query, visual_query_any_of or visual_query_batch')
            if any(q is not None for q in queries):
                if self.validation_mode != 'target' or not obs.png:
                    raise ValueError('visual_query requires a cached image in target mode; observe(image="always") if absent')
                try:
                    if visual_query_batch is not None:
                        batch = visual_batch_option(visual_query_batch, obs.header['width'], obs.header['height'])
                        self._query_visual_batch(obs, batch)
                    elif visual_query_any_of is not None:
                        choices = visual_choices_option(visual_query_any_of, obs.header['width'], obs.header['height'],
                            limit=MAX_CACHED_VISUAL_ALTERNATIVES, context_required=True)
                        self._query_visual_choices(obs, choices)
                    else:
                        spec = visual_target_option(visual_query, obs.header['width'], obs.header['height'])
                        self._query_visual_candidates(obs, spec)
                except targets.TargetError as exc:
                    raise MobileError(str(exc), code=exc.code) from None
            return obs

    def _current_observation(self, observation_id: str):
        self._require_device()
        if self.cancel.is_set():
            raise MobileError("Stopped. Call observe(resume=True) to resume explicitly")
        old = self.observation
        if old is None or old.id != observation_id:
            raise MobileError("Stale observation ID. Call observe; do not reuse old element IDs or coordinates", stage="preflight")
        if time.monotonic() - old.created > self.max_age_seconds:
            self.observation = None
            raise MobileError("Observation expired. Call observe again", stage="preflight")
        if not old.actionable:
            raise MobileError("Observation is inconsistent and cannot authorize input. Call observe again",
                              stage="preflight", consistency=old.consistency)
        return old

    @timed_phase("validation_ms")
    def _validate(self, observation_id: str, target_bounds=None):
        old = self._current_observation(observation_id)
        header, tree = self._state()
        if header != old.header:
            self.observation = None
            raise MobileError("Display or foreground app changed. Call observe before any input",
                              stage="preflight", header_changed=[k for k in header if header[k] != old.header.get(k)])
        if old.unstable_regions and (target_bounds is None or overlaps_regions(target_bounds, old.unstable_regions)):
            self.observation = None
            raise MobileError("Input intersects an unstable observation region. Observe again or choose a stable target",
                              stage="preflight", unstable_regions=old.unstable_regions)
        if tree.digest != old.digest:
            difference = tree_diff(old.tree, tree) if old.tree else {}
            motion = (localized_motion(old.tree, tree) or localized_text_updates(old.tree, tree)
                      or localized_subtree_updates(old.tree, tree) or localized_passive_updates(old.tree, tree)) if old.tree else None
            if target_bounds is not None and old.tree and unrelated_text_only(old.tree, tree, target_bounds):
                self._log("preflight_text_tolerated", observation_id=old.id, difference=difference,
                          target_bounds=list(target_bounds))
            elif target_bounds is not None and motion and not overlaps_regions(target_bounds, motion):
                self._log("preflight_local_motion_tolerated", observation_id=old.id,
                          unstable_regions=motion, target_bounds=list(target_bounds))
            else:
                self.observation = None
                raise MobileError("UI changed. Call observe before any input", stage="preflight", difference=difference)
        return old

    def _act(self, old: Observation, action: dict, fn, follow_up=None, *, sensitive_values=()):
        # Invalidate before sending; never automatically replay possibly successful input.
        self.observation = None
        if self.cancel.is_set():
            raise MobileError("Stopped before input")
        self._log("action_start", observation_id=old.id, action=action)
        start = time.monotonic()
        self._input_phase = "unknown"
        try:
            fn()
        except Exception as exc:
            self._log("action_unknown", error_type=type(exc).__name__,
                      exception=self._exception_details(exc, f"input.{action['kind']}", sensitive_values))
            raise MobileError(f"Action outcome unknown ({type(exc).__name__}). Reobserve; do not automatically repeat") from None
        finally:
            self._record_time("execution_ms", start)
        elapsed_ms = round((time.monotonic() - start) * 1000)
        self._input_phase = "sent"
        sent_mono = time.monotonic()
        sent_at = datetime.now(timezone.utc).isoformat()
        self._log("action_sent", elapsed_ms=elapsed_ms, sent_at=sent_at)
        if follow_up is not None:
            result, obs = self._local_sequence(old, follow_up=follow_up, sent_mono=sent_mono, sent_at=sent_at)
            result['validation'] = self._validation_result
            return result, obs
        try:
            wait_data = {}
            settle_data = {}
            if self._after_input_settle_ms:
                at = time.monotonic()
                cancelled = self.cancel.wait(self._after_input_settle_ms / 1000)
                self._record_time('settle_ms', at)
                if cancelled:
                    raise MobileError('Stopped during post-input delay', stage='settle')
                settle_data = {'settle': {'requested_ms': self._after_input_settle_ms,
                                         'elapsed_ms': round((time.monotonic()-at)*1000),
                                         'scope': 'delay_only; not readiness or dwell proof'}}
            if self._after_input_wait is None:
                obs = self._observe()
            else:
                until, timeout = self._after_input_wait
                wait_data, obs = self._wait_state(until, timeout)
        except Exception as exc:
            self.observation = None if self.cancel.is_set() else getattr(exc, 'partial_observation', None)
            reason = str(exc) if isinstance(exc, MobileError) else type(exc).__name__
            details = exc.details if isinstance(exc, MobileError) else {}
            self._log("verification_failed", error_type=type(exc).__name__, reason=reason, **details)
            raise MobileError(f"Input was sent but observation failed: {reason}. Reobserve; do not repeat the input",
                              partial_observation=getattr(exc, 'partial_observation', None), **details) from None
        self._log("action_observed", observation_id=obs.id, actionable=obs.actionable)
        if not obs.actionable:
            self._log("verification_incomplete", observation_id=obs.id, input_status="sent")
        return {"action": action, "execution_wall_ms": elapsed_ms, **wait_data, **settle_data,
                "validation": self._validation_result,
                "input_status": "sent", "observation_status": obs.consistency,
                "result": ("Input sent; inspect observation to verify task outcome" if obs.actionable else
                           "Input sent; observation is inconsistent. Reobserve, do not replay input")}, obs

    @staticmethod
    def _seed(seed):
        return secrets.randbits(32) if seed is None else seed

    @audited_input
    def tap(self, observation_id, element_id=None, x=None, y=None, profile="natural", seed=None, dry_run=False,
            target=None, context_element_ids=None, visual_bounds=None, follow_up=None, visual_target=None, readiness=None,
            visual_candidate_id=None):
        with self.lock:
            if readiness is not None:
                readiness = readiness_option(readiness)
                if self.validation_mode != 'target' or (element_id is None and target is None):
                    raise ValueError('readiness requires a structured target in target mode')
            if follow_up is not None:
                follow_up = follow_up_option(follow_up)
                if dry_run or self._after_input_wait or self._after_input_settle_ms:
                    raise ValueError('follow_up cannot be combined with dry_run, until or settle_ms')
            obs = self._current_observation(observation_id)
            if visual_candidate_id is not None:
                if self.validation_mode != 'target' or any(v is not None for v in
                        (element_id, x, y, target, visual_bounds, visual_target, readiness)):
                    raise ValueError('visual_candidate_id is exclusive with other target/coordinate selectors')
                candidate = obs.visual_candidates.get(visual_candidate_id)
                if candidate is None:
                    raise ValueError('Visual candidate must belong to the current inspected observation')
                visual_target, visual_bounds = candidate
                x, y = (visual_bounds[0]+visual_bounds[2])//2, (visual_bounds[1]+visual_bounds[3])//2
            if visual_target is not None:
                if (element_id is not None or target is not None or visual_bounds is None or
                        self.validation_mode != 'target'):
                    raise ValueError('visual_target requires coordinates and visual_bounds in target mode')
                visual_target = visual_target_option(visual_target, obs.header['width'], obs.header['height'])
            if follow_up and obs.header['package'] not in follow_up.allowed_packages:
                raise ValueError('allowed_packages must include the inspected source package')
            if follow_up and follow_up.return_back and follow_up.return_back.after:
                if self.validation_mode != 'target':
                    raise ValueError('navigation requires target mode')
                self._navigation_dimensions(follow_up.return_back.after, obs.header)
            if profile not in ("natural", "steady"):
                raise MobileError("Unknown gesture profile")
            element = None
            context_texts = None
            if target is not None:
                if element_id is not None or x is not None or y is not None:
                    raise MobileError('Choose target OR element_id OR coordinates')
                try:
                    target = target_option(target)
                    context_texts = targets.declared_context(target)
                    if context_texts is not None and self.validation_mode != 'target':
                        raise ValueError('Declared target context requires target mode')
                    element = targets.select_observed(obs.elements, obs.tree.graph, target)
                except targets.TargetError as exc:
                    raise MobileError(str(exc), code=exc.code) from None
                element_id = element.id
            if element_id is not None:
                if x is not None or y is not None:
                    raise MobileError("Choose element_id OR screenshot coordinates")
                element = targets.element_by_id(obs.elements, element_id)
                if not element or not element.enabled:
                    raise MobileError("Element is absent or disabled")
                bounds = element.bounds
            else:
                if x is None or y is None:
                    raise MobileError("Supply element_id or both x and y")
                px, py = check_point(x, y, obs.header["width"], obs.header["height"])
                bounds = (px, py, px + 1, py + 1)
            if self.validation_mode == 'legacy':
                self._validate(observation_id, target_bounds=bounds)
            elif element is not None:
                _, bounds = self._validate_target(observation_id, 'tap', element, context_ids=context_element_ids,
                                                  readiness=readiness, context_texts=context_texts)
            else:
                w, h = obs.header['width'], obs.header['height']
                region = (max(0, px-48), max(0, py-48), min(w, px+49), min(h, py+49))
                if visual_bounds is not None:
                    if (not isinstance(visual_bounds, (list, tuple)) or len(visual_bounds) != 4 or
                            any(isinstance(v, bool) or not isinstance(v, int) for v in visual_bounds) or
                            not (0 <= visual_bounds[0] <= px < visual_bounds[2] <= w and
                                 0 <= visual_bounds[1] <= py < visual_bounds[3] <= h)):
                        raise ValueError('visual_bounds must be an in-display rectangle containing the point')
                    region = tuple(visual_bounds)
                _, relocated = self._validate_target(observation_id, 'visual', region=region,
                                                     context_ids=context_element_ids, visual_target=visual_target,
                                                     visual_choices=obs.visual_choice_sets.get(visual_candidate_id),
                                                     visual_conditions=obs.visual_condition_sets.get(visual_candidate_id))
                if relocated is not None:
                    bounds = relocated
            gesture = tap_plan(bounds, self._seed(seed), profile)
            action = gesture.as_dict()
            if dry_run:
                return {"dry_run": True, "action": action, "validation": self._validation_result}, obs
            return self._act(obs, action, lambda: self.driver.execute(gesture), follow_up)

    @audited_input
    def swipe(self, observation_id, start_x, start_y, end_x, end_y,
              kind="browse", duration_ms=None, profile="natural", seed=None, dry_run=False, container_id=None):
        with self.lock:
            obs = self._current_observation(observation_id)
            if profile not in ("natural", "steady"):
                raise MobileError("Unknown gesture profile")
            width, height = obs.header["width"], obs.header["height"]
            start = check_point(start_x, start_y, width, height)
            end = check_point(end_x, end_y, width, height)
            gesture = swipe_plan(start, end, kind, self._seed(seed), width, height, duration_ms, profile)
            if self.validation_mode == 'legacy':
                self._validate(observation_id)
            else:
                container=None
                if container_id is not None:
                    container=targets.element_by_id(obs.elements, container_id)
                    if container is None or not container.scrollable:
                        raise ValueError('container_id must identify an observed scrollable element')
                region=(min(p[0] for p in gesture.points),min(p[1] for p in gesture.points),
                        max(p[0] for p in gesture.points)+1,max(p[1] for p in gesture.points)+1)
                path_region=region
                if kind=='drag':
                    region=(max(0,start[0]-48),max(0,start[1]-48),min(width,start[0]+49),min(height,start[1]+49))
                self._validate_target(observation_id, 'drag' if kind=='drag' else 'swipe', region=region,
                                      container_index=container.node_index if container else None,
                                      action_region=path_region if kind=='drag' else None)
            action = gesture.as_dict()
            if dry_run:
                return {"dry_run": True, "action": action}, obs
            return self._act(obs, action, lambda: self.driver.execute(gesture))

    @audited_input
    def type_text(self, observation_id, text, mode="replace"):
        with self.lock:
            obs = self._current_observation(observation_id)
            if mode not in ("replace", "append") or len(text) > 4096:
                raise MobileError("Use replace/append and at most 4096 characters")
            focused = [e for e in obs.elements if e.focused and e.editable and e.enabled]
            if len(focused) != 1 or focused[0].password:
                raise MobileError("Observe exactly one focused, non-password editable field before text input")
            if self.validation_mode == 'legacy':
                self._validate(observation_id)
            else:
                self._validate_target(observation_id, 'type', focused[0])
            value = focused[0].text + text if mode == "append" else text
            action = {"kind": "type_text", "mode": mode, "characters": len(text),
                      "mechanism": "accessibility setText; not physical keyboard typing"}
            return self._act(obs, action, lambda: self.driver.set_text(value), sensitive_values=(text, value))

    @audited_input
    def press_key(self, observation_id, key, back_sequence=None):
        with self.lock:
            if back_sequence is not None:
                back_sequence = back_option(back_sequence)
                if key != 'back' or self._after_input_wait or self._after_input_settle_ms:
                    raise ValueError('back_sequence requires back and cannot combine with until/settle_ms')
            obs = self._current_observation(observation_id)
            if back_sequence and obs.header['package'] not in back_sequence.allowed_packages:
                raise ValueError('allowed_packages must include the inspected source package')
            if back_sequence and back_sequence.after:
                if self.validation_mode != 'target':
                    raise ValueError('navigation requires target mode')
                self._navigation_dimensions(back_sequence.after, obs.header)
            if key not in ("back", "home", "enter", "delete", "recent"):
                raise MobileError("Unsupported key")
            if self.validation_mode == 'legacy':
                self._validate(observation_id)
            elif key in ('enter', 'delete'):
                focused = [e for e in obs.elements if e.focused and e.editable and e.enabled and not e.password]
                if len(focused) != 1:
                    raise MobileError('Observe exactly one focused non-password editable field first')
                self._validate_target(observation_id, 'type', focused[0])
            else:
                self._validate_target(observation_id, key)
            if back_sequence:
                return self._local_sequence(obs, back=back_sequence)
            return self._act(obs, {"kind": "key", "key": key}, lambda: self.driver.key(key))

    @audited_input
    def launch_app(self, observation_id, package):
        with self.lock:
            obs = self._current_observation(observation_id)
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
                raise MobileError("Invalid Android package name")
            if self.validation_mode == 'legacy':
                self._validate(observation_id)
            else:
                self._validate_target(observation_id, 'launch')
            def launch_and_wait():
                self.driver.launch(package)
                deadline = time.monotonic() + 8
                while self._read('header')["package"] != package:
                    if self.cancel.is_set() or time.monotonic() >= deadline:
                        raise TimeoutError("Requested app did not become foreground")
                    self.cancel.wait(0.15)
            return self._act(obs, {"kind": "launch", "package": package}, launch_and_wait)

    @audited_input
    def navigate(self, observation_id, plan, follow_up=None):
        plan = navigation_option(plan)
        if self.validation_mode != 'target':
            raise ValueError('navigate requires target mode')
        old = self._current_observation(observation_id)
        if follow_up is not None:
            follow_up = follow_up_option(follow_up)
            if plan.steps[-1].kind != 'tap' or plan.until is not None or plan.settle_ms:
                raise ValueError('Navigation follow_up requires a final tap, without plan.until/settle_ms')
            if plan.steps[-1].package not in follow_up.allowed_packages:
                raise ValueError('follow_up must include the final tap package')
            if follow_up.return_back and follow_up.return_back.after:
                self._navigation_dimensions(follow_up.return_back.after, old.header)
        self._navigation_dimensions(plan, old.header)
        if old.header['package'] not in plan.allowed_packages:
            raise ValueError('allowed_packages must include the inspected source package')
        if self._read('header') != old.header:
            raise MobileError('Foreground changed before navigation', stage='preflight')
        return self._navigate(old, plan, follow_up)

    @staticmethod
    def _matches(until, header, elements):
        return (header['screen_on'] and
                (until.package is None or header['package'] == until.package) and
                (until.text is None or any(not e.password and
                    (until.text in e.text or until.text in e.description) for e in elements)))

    def _wait_state(self, until, timeout, duration=0, initial=None):
        """Read-only polling; final conditions are checked on a captured snapshot.

        The deadline bounds polling, not an in-flight Android RPC or final capture.
        A dwell checks package/display and optional text at sample points only.
        """
        started = time.monotonic()
        capture_before = (self._metrics or {}).get('capture_ms', 0)
        deadline = started + timeout
        held_since = None
        polls = 0
        header_only = self.validation_mode == 'target' and until.text is None
        baseline = initial.header if initial else None
        self.observation = None

        def info(status):
            now = time.monotonic()
            return {'status': status, 'elapsed_ms': round((now - started) * 1000),
                    'duration_seconds': duration, 'polls': polls,
                    'sampled_duration_ms': round((now - held_since) * 1000) if held_since is not None else 0,
                    'scope': 'sampled_package_display_and_accessible_text; not business completion'}

        def finish(status, obs=None, reason=None):
            result = info(status)
            self._log('wait_finished', **result)
            if status == 'matched':
                return {'wait': result}, obs
            raise MobileError(reason or 'Expected state did not appear before timeout',
                              partial_observation=obs, stage='wait', wait=result)

        def final_snapshot(state=None):
            if self.cancel.is_set():
                return finish('cancelled', reason='Stopped while waiting')
            # Reuse the just-read matching tree even with an image. Text waits
            # still read and verify a NEW tree after the image below.
            return self._observe(wait_ms=0, initial_state=state if self.validation_mode == 'target' else None,
                                 verify_after_image=until.text is not None)

        def matches_snapshot(obs):
            return (obs.actionable and (self.validation_mode == 'target' or obs.consistency == 'consistent') and
                    self._matches(until, obs.header, obs.elements) and
                    (not duration or obs.header == baseline))

        try:
            while True:
                if self.cancel.is_set():
                    return finish('cancelled', reason='Stopped while waiting')
                try:
                    if header_only:
                        header,tree=self._read('header'),None
                        if not header['screen_on']:
                            raise MobileError('Device screen is off. Unlock manually')
                    else:
                        header, tree = self._state()
                except Exception as exc:
                    status = 'interrupted' if duration else 'unavailable'
                    reason = str(exc) if isinstance(exc, MobileError) else f'Device read failed ({type(exc).__name__})'
                    return finish(status, reason=reason)
                polls += 1
                matches = self._matches(until, header, tree.elements if tree else [])
                if duration:
                    if header != baseline or not matches:
                        return finish('interrupted', final_snapshot(), 'Confirmed dwell state changed; time is not credited')
                    if held_since is None:
                        held_since = time.monotonic()  # never credit old snapshot age or loading time
                        deadline = held_since + timeout  # initial device read is not dwell time
                elif until.package is None:
                    if baseline is None:
                        baseline = header
                    elif header != baseline:
                        return finish('interrupted', final_snapshot(), 'Foreground/display changed while waiting for text')

                now = time.monotonic()
                ready = matches and (not duration or now - held_since >= duration)
                if ready or now >= deadline:
                    obs = final_snapshot((header,tree) if tree else None)
                    if self.cancel.is_set():
                        self.observation = None
                        return finish('cancelled', reason='Stopped while waiting')
                    verified = matches_snapshot(obs)
                    if ready and verified:
                        return finish('matched', obs)
                    if duration and not verified:
                        return finish('interrupted', obs, 'Dwell state changed during final verification')
                    if time.monotonic() >= deadline:
                        return finish('timeout', obs)
                remaining = deadline - time.monotonic()
                if duration and held_since is not None:
                    remaining = min(remaining, duration - (time.monotonic() - held_since))
                self.cancel.wait(max(0.001, min(0.75 if header_only else 0.25, remaining)))
        except MobileError as exc:
            if 'wait' not in exc.details:
                exc.details.update(stage='wait', wait=info('unavailable'))
                self._log('wait_finished', **exc.details['wait'])
            raise
        finally:
            if self._metrics is not None:
                elapsed = (time.monotonic() - started) * 1000
                captured = self._metrics.get('capture_ms', 0) - capture_before
                self._metrics['waiting_ms'] = self._metrics.get('waiting_ms', 0) + max(0, elapsed - captured)

    def wait_for_state(self, observation_id=None, text=None, package=None, duration_seconds=0, timeout_seconds=30):
        until = condition(WaitCondition(text, package))
        duration = seconds(duration_seconds, 'duration_seconds', allow_zero=True)
        timeout = seconds(timeout_seconds, 'timeout_seconds')
        if duration > timeout:
            raise ValueError('duration_seconds must not exceed timeout_seconds')
        with self.lock:
            self._require_device()
            initial = self._current_observation(observation_id) if observation_id is not None else None
            if duration and (initial is None or package is None or
                             not self._matches(until, initial.header, initial.elements)):
                raise MobileError('Dwell requires a latest inspected observation_id matching package and optional text')
            return self._wait_state(until, timeout, duration, initial)

    def wait_for(self, text, timeout_seconds=10):
        data, obs = self.wait_for_state(text=text, timeout_seconds=timeout_seconds)
        return data, obs
