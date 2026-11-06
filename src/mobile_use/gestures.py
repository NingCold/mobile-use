"""Reproducible gesture plans. Profiles are engineering defaults, not human measurements."""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from typing import Literal

Profile = Literal["steady", "natural"]
SwipeKind = Literal["browse", "fling", "drag"]


@dataclass
class Gesture:
    kind: str
    seed: int
    points: list[tuple[int, int]]
    segment_ms: int = 20
    hold_ms: int = 0

    def as_dict(self) -> dict:
        result = asdict(self)
        result["planned_duration_ms"] = self.hold_ms if self.kind == "tap" else (len(self.points) - 1) * self.segment_ms
        return result


def check_point(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    if not all(math.isfinite(v) for v in (x, y)) or not (0 <= x < width and 0 <= y < height):
        raise ValueError(f"Point must be inside the observed {width}x{height} screenshot")
    return int(x), int(y)


def tap_plan(bounds: tuple[int, int, int, int], seed: int, profile: Profile = "natural") -> Gesture:
    left, top, right, bottom = bounds
    if left >= right or top >= bottom:
        raise ValueError("Target has no visible area")
    rng = random.Random(seed)
    x, y = (left + right - 1) / 2, (top + bottom - 1) / 2
    if profile == "natural":
        # Stay well inside the target. Variability never expands its clickable region.
        x += rng.uniform(-0.10, 0.10) * (right - left)
        y += rng.uniform(-0.10, 0.10) * (bottom - top)
    x = max(left, min(right - 1, round(x)))
    y = max(top, min(bottom - 1, round(y)))
    return Gesture("tap", seed, [(x, y)], hold_ms=rng.randint(85, 140) if profile == "natural" else 100)


def swipe_plan(start: tuple[int, int], end: tuple[int, int], kind: SwipeKind,
               seed: int, width: int, height: int, duration_ms: int | None = None,
               profile: Profile = "natural") -> Gesture:
    check_point(*start, width, height)
    check_point(*end, width, height)
    if start == end:
        raise ValueError("Swipe start and end must differ")
    if kind not in ("browse", "fling", "drag"):
        raise ValueError("Unknown swipe kind")
    rng = random.Random(seed)
    duration = duration_ms if duration_ms is not None else {"browse": 600, "fling": 280, "drag": 800}[kind]
    if not 120 <= duration <= 2000:
        raise ValueError("Movement duration must be 120..2000 ms")
    segments = max(6, round(duration / 20))
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    bend = rng.uniform(-0.018, 0.018) * distance if profile == "natural" else 0
    points: list[tuple[int, int]] = []
    for i in range(segments + 1):
        t = i / segments
        # A fling retains release velocity; browse/drag ease into the endpoint.
        progress = t ** 1.5 if kind == "fling" else t * t * (3 - 2 * t)
        offset = math.sin(math.pi * progress) * bend
        x = start[0] + dx * progress - dy / distance * offset
        y = start[1] + dy * progress + dx / distance * offset
        points.append((round(max(0, min(width - 1, x))), round(max(0, min(height - 1, y)))))
    if kind == "drag":
        # Repeated start coordinates hold contact in the SAME device-side gesture.
        points = [start] * 30 + points
    return Gesture(kind, seed, points)
