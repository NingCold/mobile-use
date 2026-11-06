"""Local OCR relocation of explicitly bound visual targets. Never sends input."""
from dataclasses import dataclass
from pathlib import Path
import io
import unicodedata

from PIL import Image, ImageChops, ImageStat

from .targets import TargetError, area, identity


def normalized(text):
    # OCR inserts spaces into some labels. Do not discard punctuation, case or
    # digits: those can distinguish different actions/items.
    return ''.join(text.split())


@dataclass(frozen=True)
class Label:
    text: str
    score: float
    bounds: tuple[int, int, int, int]


def inside(inner, outer):
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def center(bounds):
    return ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)


def patch_delta(before, after, bounds):
    with Image.open(io.BytesIO(before)) as a, Image.open(io.BytesIO(after)) as b:
        delta = ImageChops.difference(a.convert('RGB').crop(bounds), b.convert('RGB').crop(bounds))
        mean = max(ImageStat.Stat(delta).mean)
        r, g, b = delta.split()
        peak = ImageChops.lighter(ImageChops.lighter(r, g), b)
        fraction = sum(peak.histogram()[25:]) / (delta.width * delta.height)
    return mean, fraction


def stationary(before, after, bounds):
    mean, fraction = patch_delta(before, after, bounds)
    return mean <= 4 and fraction <= .02


def same_search_pixels(before, after, region):
    """Exact equality across the competition area, not a fuzzy check of just the winner."""
    if before == after:
        return True
    with Image.open(io.BytesIO(before)) as a, Image.open(io.BytesIO(after)) as b:
        left, right = a.convert('RGB'), b.convert('RGB')
        if region:
            left, right = left.crop(region), right.crop(region)
        return ImageChops.difference(left, right).getbbox() is None


class LocalVision:
    """One lazy CPU engine per service. Models are wheel-local; no network fallback."""
    def __init__(self):
        self.engine = None

    def read(self, png, texts=(), region=None):
        import numpy as np
        if self.engine is None:
            import rapidocr
            from rapidocr import RapidOCR
            models = Path(rapidocr.__file__).parent / 'models'
            paths = {'Det': models / 'PP-OCRv6_det_small.onnx',
                     'Rec': models / 'PP-OCRv6_rec_small.onnx'}
            if not all(p.is_file() for p in paths.values()):
                raise TargetError('visual_backend_unavailable', 'Bundled local OCR models are missing')
            self.engine = RapidOCR(params={
                'Global.log_level': 'error', 'Global.use_cls': False,
                'Global.max_side_len': 960, 'Det.limit_type': 'max', 'Det.limit_side_len': 960,
                'EngineConfig.onnxruntime.intra_op_num_threads': 4,
                'EngineConfig.onnxruntime.inter_op_num_threads': 1,
                **{f'{k}.model_path': str(v) for k, v in paths.items()}})
        with Image.open(io.BytesIO(png)) as image:
            offset = (region[0], region[1]) if region is not None else (0, 0)
            if region is not None:
                image = image.crop(region)
            w, h = image.size
            # ndarray input uses BGR; PIL input conversion varies across OCR releases.
            pixels = np.asarray(image.convert('RGB'))[:, :, ::-1].copy()
            result = self.engine(pixels, use_det=True, use_cls=False, use_rec=not texts)
        labels = []
        decoded = 0
        if result.boxes is None:
            return labels
        lengths = {sum(1 if unicodedata.east_asian_width(c) in ('W', 'F') else .55
                       for c in normalized(text)) for text in texts}
        for index, box in enumerate(result.boxes):
            l, t = np.floor(box.min(axis=0)).astype(int)
            r, b = np.ceil(box.max(axis=0)).astype(int)
            bounds = (max(0, int(l)), max(0, int(t)), min(w, int(r)), min(h, int(b)))
            # This initial implementation deliberately supports horizontal text only.
            if not area(bounds) or abs(float(box[1][1]-box[0][1])) > max(3, (r-l)*.08):
                continue
            if texts:
                # Detection still covers the entire search image. Do not decode
                # unrelated paragraphs: long lines dominate recognition cost.
                # Wide bounds accommodate proportional fonts and OCR padding.
                ratio = (r-l)/(b-t)
                if not any(max(.15, length*.3) <= ratio <= max(2, length*1.5) for length in lengths):
                    continue
                if decoded >= 64:
                    raise TargetError('visual_search_limit', 'Too many visual text candidates; narrow search_bounds')
                decoded += 1
                crop = pixels[bounds[1]:bounds[3], bounds[0]:bounds[2]].copy()
                rec = self.engine(crop, use_det=False, use_cls=False, use_rec=True)
                if not rec.txts:
                    continue
                text, score = rec.txts[0], rec.scores[0]
            else:
                text, score = result.txts[index], result.scores[index]
            labels.append(Label(str(text), float(score),
                                tuple(v + offset[i % 2] for i, v in enumerate(bounds))))
        return labels


def unique(labels, text, region, *, reference=False):
    matches = [v for v in labels if normalized(v.text) == normalized(text) and
               inside(v.bounds, region)]
    if len(matches) != 1:
        code = 'visual_target_ambiguous' if len(matches) > 1 else 'visual_target_missing'
        raise TargetError(code, ('Inspected image' if reference else 'Current image') +
                          ' must contain one exact visual label in its bound region')
    if matches[0].score < .90:
        raise TargetError('visual_low_confidence', 'Visual label confidence is insufficient')
    return matches[0]


def container_key(graph, index):
    return (identity(graph.nodes[index], container=True),
            tuple(identity(graph.nodes[i], container=True) for i in graph.ancestors(index)))


@dataclass(frozen=True)
class VisualBinding:
    target: Label
    contexts: tuple[Label, ...]
    container: tuple | None

    @property
    def regions(self):
        return [self.target.bounds, *(c.bounds for c in self.contexts)]


def context_matches(labels, text, region, target):
    matches = tuple(v for v in labels if normalized(v.text) == normalized(text)
                    and inside(v.bounds, region) and v.bounds != target.bounds)
    if not matches:
        raise TargetError('visual_target_missing', 'Bound visual context is missing from the target region')
    if any(v.score < .90 for v in matches):
        raise TargetError('visual_low_confidence', 'Visual context confidence is insufficient')
    return matches


def intersection(a, b):
    return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])


def bind_reference(labels, spec, bounds, size, graph=None):
    target = unique(labels, spec.text, bounds, reference=True)
    region = spec.search_bounds or (0, 0, *size)
    if not inside(target.bounds, region):
        raise TargetError('visual_scope', 'Search region must include the inspected target')
    contexts = context_matches(labels, spec.context_text, region, target)
    container = None
    if graph is not None:
        # Prefer an actual, uniquely identifiable container over old relative
        # label positions. Repeated anonymous list rows cannot identify a card.
        candidates = []
        for i, n in enumerate(graph.nodes):
            if not n.visible or not (n.children or n.parent is None):
                continue
            local = intersection(region, n.bounds)
            if inside(target.bounds, local) and any(inside(c.bounds, local) for c in contexts):
                key = container_key(graph, i)
                if sum(v.visible and container_key(graph, j) == key for j, v in enumerate(graph.nodes)) == 1:
                    candidates.append((area(local), i, local, key))
        if candidates:
            _, _, region, container = min(candidates)
            contexts = context_matches(labels, spec.context_text, region, target)
    # The inspected button must also be unique within its semantic scope. A
    # repeated brand is supporting evidence, not another actionable candidate.
    unique(labels, spec.text, region, reference=True)
    return VisualBinding(target, contexts, container)


def relocate(labels, spec, reference, size, graph=None):
    region = spec.search_bounds or (0, 0, *size)
    if reference.container is not None:
        matches = [n for i, n in enumerate(graph.nodes) if n.visible and
                   container_key(graph, i) == reference.container] if graph is not None else []
        if len(matches) != 1:
            raise TargetError('visual_context_changed', 'Bound visual container changed or became ambiguous')
        region = intersection(region, matches[0].bounds)
    target = unique(labels, spec.text, region)
    contexts = context_matches(labels, spec.context_text, region, target) if spec.context_text is not None else ()
    return target, contexts


def bind_declared(labels, spec, size, graph):
    region = spec.search_bounds or (0, 0, *size)
    if spec.context_text is not None:
        return bind_reference(labels, spec, region, size, graph)
    # The caller's accessible page guard supplies context. This does not select
    # among items: the exact action must be unique throughout the declared scope.
    target = unique(labels, spec.text, region)
    candidates = []
    for i, n in enumerate(graph.nodes):
        if n.visible and (n.children or n.parent is None) and inside(target.bounds, n.bounds):
            key = container_key(graph, i)
            if sum(v.visible and container_key(graph, j) == key for j, v in enumerate(graph.nodes)) == 1:
                candidates.append((area(n.bounds), key))
    key = min(candidates, key=lambda v:v[0])[1] if candidates else None
    return VisualBinding(target, (), key)


def choose_declared(labels, specs, size, graph):
    """All alternatives see the same labels. Only an absent candidate is skippable."""
    found = []
    for spec in specs:
        try:
            found.append((spec, bind_declared(labels, spec, size, graph)))
        except TargetError as exc:
            if exc.code != 'visual_target_missing':
                raise
    if len(found) > 1:
        raise TargetError('visual_target_ambiguous', 'Multiple authorized alternatives are visible; inspect the current page')
    if not found:
        raise TargetError('visual_target_missing', 'No exact alternative with its required context; inspect the current page')
    return found[0]
