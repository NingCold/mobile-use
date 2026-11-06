"""Bounded, declarative wait and response options. No executable predicates."""
from dataclasses import asdict, dataclass
from typing import Literal
import math
import re

MAX_VISUAL_ALTERNATIVES = 6


@dataclass
class TargetSelector:
    """Exact fields from an inspected observation; within_text scopes a row/card."""
    text: str | None = None
    description: str | None = None
    resource_id: str | None = None
    within_text: str | None = None
    __pydantic_config__ = {'extra': 'forbid'}


@dataclass
class VisualTarget:
    """Exact inspected text plus a distinct nearby label; no fuzzy/semantic search."""
    text: str
    context_text: str
    search_bounds: list[int] | None = None
    __pydantic_config__ = {'extra': 'forbid'}


@dataclass
class VisualAction:
    """Exact authorized action label and optional identifying context."""
    text: str
    context_text: str | None = None
    __pydantic_config__ = {'extra': 'forbid'}


@dataclass
class NavigationVisualTarget(VisualAction):
    search_bounds: list[int] | None = None
    __pydantic_config__ = {'extra': 'forbid'}


@dataclass
class VisualTargetAnyOf:
    candidates: list[VisualAction]
    search_bounds: list[int] | None = None
    __pydantic_config__ = {'extra': 'forbid'}

    def selectors(self):
        return [NavigationVisualTarget(c.text, c.context_text, self.search_bounds) for c in self.candidates]


def visual_choices_option(value, width, height):
    if isinstance(value, dict):
        value = VisualTargetAnyOf(**value)
    if (not isinstance(value, VisualTargetAnyOf) or not isinstance(value.candidates, list)
            or not 2 <= len(value.candidates) <= MAX_VISUAL_ALTERNATIVES):
        raise ValueError(f'visual_target_any_of requires 2..{MAX_VISUAL_ALTERNATIVES} exact alternatives')
    value.candidates = [VisualAction(**c) if isinstance(c, dict) else c for c in value.candidates]
    if not all(isinstance(c, VisualAction) and not isinstance(c, NavigationVisualTarget) for c in value.candidates):
        raise ValueError('Each alternative takes text and optional context_text; search_bounds is shared')
    seen = set()
    for spec in value.selectors():
        navigation_visual_option(spec, width, height)
        seen.add(''.join(spec.text.split()))
    if len(seen) != len(value.candidates):
        raise ValueError('Use distinct action labels; visual alternatives share one search region')
    return value


def navigation_visual_option(value, width, height):
    if isinstance(value, dict):
        value = NavigationVisualTarget(**value)
    if not isinstance(value, NavigationVisualTarget):
        raise ValueError('Navigation visual_target requires an exact text selector')
    return _check_visual_fields(value, width, height, context_required=False)


def visual_target_option(value, width, height):
    if isinstance(value, dict):
        value = VisualTarget(**value)
    if not isinstance(value, VisualTarget):
        raise ValueError('visual_target requires exact text and context_text')
    return _check_visual_fields(value, width, height, context_required=True)


def _check_visual_fields(value, width, height, *, context_required):
    labels = (value.text, value.context_text) if context_required or value.context_text is not None else (value.text,)
    for label in labels:
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise ValueError('Visual labels must be nonblank strings of at most 200 characters')
    if value.context_text is not None and ''.join(value.text.split()) == ''.join(value.context_text.split()):
        raise ValueError('context_text must identify distinct inspected context')
    if value.search_bounds is not None:
        b = value.search_bounds
        if (not isinstance(b, (list, tuple)) or len(b) != 4 or
                any(type(v) is not int for v in b) or
                not (0 <= b[0] < b[2] <= width and 0 <= b[1] < b[3] <= height)):
            raise ValueError('search_bounds must be an in-display original-pixel rectangle')
    return value


def target_option(value):
    if isinstance(value, dict):
        value = TargetSelector(**value)
    if not isinstance(value, TargetSelector) or not any((value.text, value.description, value.resource_id)):
        raise ValueError('target requires text, description or resource_id')
    if any(v is not None and (not isinstance(v, str) or not v.strip() or len(v) > 500)
           for v in asdict(value).values()):
        raise ValueError('Target fields must be nonblank strings of at most 500 characters')
    return value


@dataclass
class WaitCondition:
    """All supplied conditions must match; text is an accessible substring."""
    text: str | None = None
    package: str | None = None
    __pydantic_config__ = {'extra': 'forbid'}


@dataclass
class TapReadiness:
    """Local geometry quiet window, optionally gated by an exposed busy control."""
    quiet_ms: int = 600
    timeout_seconds: float = 5
    busy: TargetSelector | None = None
    __pydantic_config__ = {'extra': 'forbid'}


def readiness_option(value):
    if isinstance(value, dict):
        value = TapReadiness(**value)
    if not isinstance(value, TapReadiness):
        raise ValueError('readiness requires a bounded quiet window')
    if type(value.quiet_ms) is not int or not 200 <= value.quiet_ms <= 2000:
        raise ValueError('readiness.quiet_ms must be 200..2000')
    value.timeout_seconds = seconds(value.timeout_seconds, 'readiness.timeout_seconds')
    if value.timeout_seconds * 1000 < value.quiet_ms:
        raise ValueError('Readiness budget must contain its quiet window')
    if value.busy is not None:
        value.busy = target_option(value.busy)
    return value


@dataclass
class NavigationSwipe:
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    kind: Literal['browse', 'fling'] = 'browse'
    duration_ms: int = 400
    __pydantic_config__ = {'extra': 'forbid'}


@dataclass
class NavigationStep:
    """Known destination/selector, freshly resolved locally; no stored element IDs."""
    kind: Literal['launch', 'tap', 'swipe']
    package: str
    target: TargetSelector | None = None
    swipe: NavigationSwipe | None = None
    when: WaitCondition | None = None
    visual_when: VisualTarget | None = None
    timeout_seconds: float = 5
    readiness: TapReadiness | None = None
    visual_target: NavigationVisualTarget | None = None
    visual_target_any_of: VisualTargetAnyOf | None = None
    __pydantic_config__ = {'extra': 'forbid'}


@dataclass
class NavigationPlan:
    allowed_packages: list[str]
    steps: list[NavigationStep]
    timeout_seconds: float = 20
    settle_ms: int = 0
    until: WaitCondition | None = None
    __pydantic_config__ = {'extra': 'forbid'}


def navigation_option(value):
    if isinstance(value, dict):
        value = NavigationPlan(**value)
    if not isinstance(value, NavigationPlan):
        raise ValueError('navigation requires a bounded plan')
    packages_option(value.allowed_packages)
    value.timeout_seconds = seconds(value.timeout_seconds, 'navigation.timeout_seconds')
    settle_option(value.settle_ms)
    if not isinstance(value.steps, list) or not 1 <= len(value.steps) <= 8:
        raise ValueError('navigation requires 1..8 steps')
    converted = []
    for step in value.steps:
        if isinstance(step, dict):
            step = NavigationStep(**step)
        if not isinstance(step, NavigationStep) or step.kind not in ('launch', 'tap', 'swipe'):
            raise ValueError('Unsupported navigation step')
        if step.package not in value.allowed_packages:
            raise ValueError('Step package must be in allowed_packages')
        step.timeout_seconds = seconds(step.timeout_seconds, 'step.timeout_seconds')
        if step.when is not None:
            step.when = condition(step.when)
            if step.when.package and step.when.package != step.package:
                raise ValueError('when.package must match the step package')
        if step.kind == 'tap':
            if sum(v is not None for v in (step.target, step.visual_target, step.visual_target_any_of)) != 1:
                raise ValueError('Navigation tap requires exactly one of target, visual_target or visual_target_any_of')
            if step.target is not None:
                step.target = target_option(step.target)
            else:
                if step.visual_target_any_of is not None:
                    step.visual_target_any_of = visual_choices_option(step.visual_target_any_of, 2**31-1, 2**31-1)
                else:
                    step.visual_target = navigation_visual_option(step.visual_target, 2**31-1, 2**31-1)
                if not step.when or not step.when.text:
                    raise ValueError('Visual navigation tap requires a known accessible page condition')
                if step.visual_when is not None:
                    raise ValueError('Use visual_target context, not visual_when, on visual tap')
        elif any(v is not None for v in (step.target, step.visual_target, step.visual_target_any_of)):
            raise ValueError('target/visual_target is only supported on navigation tap')
        if step.readiness is not None:
            if step.kind != 'tap' or step.target is None:
                raise ValueError('readiness is only supported on structured tap')
            step.readiness = readiness_option(step.readiness)
        if step.kind == 'swipe':
            if isinstance(step.swipe, dict):
                step.swipe = NavigationSwipe(**step.swipe)
            if not isinstance(step.swipe, NavigationSwipe):
                raise ValueError('swipe requires an explicit gesture')
            if (any(type(getattr(step.swipe, k)) is not int for k in ('start_x','start_y','end_x','end_y','duration_ms'))
                    or step.swipe.kind not in ('browse', 'fling') or not 120 <= step.swipe.duration_ms <= 2000):
                raise ValueError('Invalid navigation gesture')
            if not (step.when and step.when.text) and step.visual_when is None:
                raise ValueError('swipe requires a known text or visual page condition')
        elif step.swipe is not None:
            raise ValueError('swipe is only supported on navigation swipe')
        if step.kind == 'launch' and (step.when is not None or step.visual_when is not None):
            raise ValueError('launch accepts only its destination package')
        if step.visual_when is not None:
            # Display bounds are checked before any input by the service.
            step.visual_when = visual_target_option(step.visual_when, 2**31-1, 2**31-1)
        converted.append(step)
    value.steps = converted
    if value.until is not None:
        value.until = condition(value.until)
        if value.until.package and value.until.package != converted[-1].package:
            raise ValueError('navigation.until.package must match the final step package')
    return value


@dataclass
class BackSequence:
    """Caller-authorized bounded navigation; packages include all intermediaries."""
    allowed_packages: list[str]
    count: int = 2
    interval_ms: int = 400
    stop_when: WaitCondition | None = None
    after: NavigationPlan | None = None
    __pydantic_config__ = {'extra': 'forbid'}


@dataclass
class TapFollowUp:
    """Local timing and optional returns. No intermediate observations are delivered."""
    allowed_packages: list[str]
    duration_seconds: float
    anchor: Literal['input_sent', 'state_matched'] = 'input_sent'
    ready: WaitCondition | None = None
    completion: WaitCondition | None = None
    timeout_seconds: float = 30
    return_back: BackSequence | None = None
    __pydantic_config__ = {'extra': 'forbid'}


def packages_option(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ValueError('allowed_packages requires 1..8 explicit packages')
    for value in values:
        if not isinstance(value, str):
            raise ValueError('Invalid allowed package')
        condition(WaitCondition(package=value))
    return values


def back_option(value):
    if isinstance(value, dict):
        value = BackSequence(**value)
    if not isinstance(value, BackSequence):
        raise ValueError('back_sequence requires bounded navigation options')
    packages_option(value.allowed_packages)
    if type(value.count) is not int or not 1 <= value.count <= 3:
        raise ValueError('back count must be 1..3')
    if type(value.interval_ms) is not int or not 200 <= value.interval_ms <= 2000:
        raise ValueError('back interval_ms must be 200..2000')
    if value.stop_when is not None:
        value.stop_when = condition(value.stop_when)
        if value.stop_when.package and value.stop_when.package not in value.allowed_packages:
            raise ValueError('stop_when.package must be in allowed_packages')
    if value.after is not None:
        value.after = navigation_option(value.after)
        if not set(value.after.allowed_packages) <= set(value.allowed_packages):
            raise ValueError('after packages must be a subset of back allowed_packages')
    return value


def follow_up_option(value):
    if isinstance(value, dict):
        value = TapFollowUp(**value)
    if not isinstance(value, TapFollowUp):
        raise ValueError('follow_up requires bounded timing options')
    packages_option(value.allowed_packages)
    value.duration_seconds = seconds(value.duration_seconds, 'duration_seconds')
    value.timeout_seconds = seconds(value.timeout_seconds, 'timeout_seconds')
    if value.duration_seconds > value.timeout_seconds:
        raise ValueError('duration_seconds must not exceed timeout_seconds')
    if value.anchor not in ('input_sent', 'state_matched'):
        raise ValueError('anchor must be input_sent or state_matched')
    for key in ('ready', 'completion'):
        if getattr(value, key) is not None:
            setattr(value, key, condition(getattr(value, key)))
    if value.anchor == 'state_matched' and (value.ready is None or value.ready.package is None):
        raise ValueError('state_matched requires ready.package and optional text')
    for c in (value.ready, value.completion):
        if c and c.package and c.package not in value.allowed_packages:
            raise ValueError('Condition package must be in allowed_packages')
    if value.return_back is not None:
        value.return_back = back_option(value.return_back)
    return value


@dataclass
class Focus:
    """Filter delivered elements only. Text and original-pixel bounds are ANDed."""
    text: str | None = None
    bounds: list[int] | None = None
    max_elements: int = 40
    __pydantic_config__ = {'extra': 'forbid'}


def seconds(value, name, *, allow_zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or not (0 <= value <= 30) or
            (not allow_zero and value == 0)):
        raise ValueError(f"{name} must be {'0..30' if allow_zero else '>0..30'} finite seconds")
    return float(value)


def settle_option(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2000:
        raise ValueError('settle_ms must be an integer in 0..2000')
    return value


def condition(value):
    if isinstance(value, dict):
        value = WaitCondition(**value)
    if not isinstance(value, WaitCondition):
        raise ValueError("until must contain text and/or package")
    if value.text is not None and (not isinstance(value.text, str) or not value.text.strip() or len(value.text) > 200):
        raise ValueError("Wait text must be nonblank and at most 200 characters")
    if value.package is not None and (not isinstance(value.package, str) or
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", value.package)):
        raise ValueError("Invalid wait package name")
    if value.text is None and value.package is None:
        raise ValueError("Supply text and/or package to wait for")
    return value


def focus_option(value):
    if value is None:
        return None
    if isinstance(value, dict):
        value = Focus(**value)
    if not isinstance(value, Focus):
        raise ValueError("focus must contain text and/or bounds")
    if value.text is not None and (not isinstance(value.text, str) or not value.text.strip() or len(value.text) > 200):
        raise ValueError("Focus text must be nonblank and at most 200 characters")
    if (isinstance(value.max_elements, bool) or not isinstance(value.max_elements, int) or
            not 1 <= value.max_elements <= 250):
        raise ValueError("focus.max_elements must be 1..250")
    if value.bounds is not None:
        b = value.bounds
        if (not isinstance(b, (list, tuple)) or len(b) != 4 or
                any(isinstance(v, bool) or not isinstance(v, int) for v in b) or
                not (0 <= b[0] < b[2] and 0 <= b[1] < b[3])):
            raise ValueError("focus.bounds must be [left, top, right, bottom] original pixels")
    if value.text is None and value.bounds is None:
        raise ValueError("Supply focus.text and/or focus.bounds")
    return value


def focused_elements(elements, focus):
    matches = []
    for e in elements:
        if focus.text is not None and not any(focus.text in e.get(k, '') for k in ('text', 'description')):
            continue
        if focus.bounds is not None:
            l, t, r, b = e['bounds']
            x1, y1, x2, y2 = focus.bounds
            if not (l < x2 and x1 < r and t < y2 and y1 < b):
                continue
        matches.append(e)
    return matches[:focus.max_elements], {**asdict(focus), 'matched_elements': len(matches),
                                         'matches_truncated': len(matches) > focus.max_elements}
