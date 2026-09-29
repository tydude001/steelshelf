"""Find the steelbook in a spine photo, so the shelf can show the real spine.

A spine photo is a case held upright in a hand over carpet or a table, shot from
above. `detect` straightens the small tilt a hand gives it, finds the case, and
returns the box as fractions of the straightened frame; `cut` takes that box out
of the full photo for the shelf.

The case is found by being unlike what it lies on. Its face is smooth running top
to bottom where carpet and wood grain are rough, and where the art is busy it is
still not the ground's colour: columns that are one or the other, joined across a
line of print, give the long edges; rows the same between them give the ends.
Each is then snapped to the sharpest edge nearby. Tuned on 72 real spine
photos (2026-09-25): a gold case on brown carpet is not found, and a
hand under a case's foot can come along in the crop; the crop editor fixes both.
"""

import io
import json
from dataclasses import asdict, dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

WORK_H = 640  # detection runs on the photo scaled to this height
ANGLES = np.arange(-8.0, 8.01, 0.5)  # tilt searched, degrees
# A case's spine face is 1:14 and a slipcover's about 1:5; outside these it is not one.
MIN_RATIO, MAX_RATIO = 0.04, 0.26
SLIP_RATIO = 0.16  # wider than this: shot in its slipcover
SMOOTH = 0.45  # a case column is at most this rough, against the ground's roughness
SMOOTH_ROWS = 0.6  # rows are judged on their smoothest fifth, which carpet shares more of
GROUND = 0.35  # a case column or row has less of the ground's colour than this
STRIP_H = 600  # the derived strip's height


# Where the crop editor starts when nothing was found: a tall strip down the middle.
DEFAULT = (0.4, 0.05, 0.6, 0.95)


@dataclass
class Box:
    """The spine face in the upright photo, turned `turns` quarter turns clockwise and
    then straightened by `angle` degrees; x0..y1 are fractions of that frame."""

    x0: float
    y0: float
    x1: float
    y1: float
    angle: float = 0.0
    turns: int = 0

    def ratio(self, size: tuple[int, int]) -> float:
        """Width over height of the box, in pixels of a frame of this (w, h)."""
        w, h = size
        return (self.x1 - self.x0) * w / max(1e-6, (self.y1 - self.y0) * h)

    def dumps(self) -> str:
        return json.dumps({k: round(v, 4) if isinstance(v, float) else v
                           for k, v in asdict(self).items()})

    @classmethod
    def loads(cls, text: str | None) -> "Box | None":
        if not text:
            return None
        try:
            d = json.loads(text)
            return cls(**{k: d[k] for k in ("x0", "y0", "x1", "y1", "angle", "turns")})
        except (ValueError, KeyError, TypeError):
            return None


def open_upright(data: bytes, height: int | None = None) -> Image.Image | None:
    """Decode a photo upright (EXIF applied), optionally scaled to `height`."""
    try:
        img = Image.open(io.BytesIO(data))
        if height:
            img.draft("RGB", (height * 2, height * 2))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        return None
    if height and img.height > height:
        img = img.resize((round(img.width * height / img.height), height), Image.BILINEAR)
    return img


def frame(img: Image.Image, turns: int, angle: float) -> Image.Image:
    """The photo turned and straightened the way a Box's fractions are measured."""
    if turns % 4:
        img = img.transpose([None, Image.ROTATE_270, Image.ROTATE_180,
                             Image.ROTATE_90][turns % 4])
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=(0, 0, 0))
    return img


def _grad(gray: np.ndarray, axis: int) -> np.ndarray:
    g = np.abs(np.diff(gray, axis=axis))
    return np.pad(g, ((0, 1), (0, 0)) if axis == 0 else ((0, 0), (0, 1)))


def _peaks(profile: np.ndarray, spread: int) -> np.ndarray:
    """The profile less its local mean: straight edges stand out, texture sinks."""
    k = np.ones(spread) / spread
    return profile - np.convolve(profile, k, mode="same")


def _gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L").filter(ImageFilter.BoxBlur(1)), dtype=np.float32)


def _tilt(img: Image.Image) -> float:
    """The tilt that makes the frame's long vertical edges straightest: the column sums
    of the horizontal gradient peak hardest where a straight edge runs the height."""
    best = (-1.0, 0.0)
    h = img.height
    rows = slice(int(h * 0.1), int(h * 0.9))
    for a in ANGLES:
        g = _grad(_gray(img.rotate(float(a), resample=Image.BILINEAR)), 1)[rows]
        prof = _peaks(g.sum(axis=0), 25)
        score = float(np.sort(prof)[-2:].sum())  # the two strongest edges
        if score > best[0]:
            best = (score, float(a))
    return best[1]


def _texture(img: Image.Image) -> np.ndarray:
    """Local roughness: the vertical gradient of the unblurred frame, which carpet and
    table grain are full of and a case's face, running top to bottom, is not."""
    return _grad(np.asarray(img.convert("L"), dtype=np.float32), 0)


def _ground(img: Image.Image) -> np.ndarray:
    """Per pixel, whether it looks like what the case lies on: within two spreads of the
    colour down both sides of the frame, above where a hand holds it."""
    a = np.asarray(img, dtype=np.float32)
    h, w = a.shape[:2]
    m = max(4, w // 10)
    sides = np.concatenate([a[int(h * 0.05): int(h * 0.6), :m],
                            a[int(h * 0.05): int(h * 0.6), -m:]], axis=1).reshape(-1, 3)
    mean, spread = sides.mean(axis=0), sides.std(axis=0) + 4.0
    return (np.abs(a - mean) < 2 * spread).all(axis=2)


def _runs(smooth: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """Runs of True, joining runs split by `gap` or fewer False (a line of print)."""
    runs: list[list[int]] = []
    for x, v in enumerate(smooth):
        if not v:
            continue
        if runs and x - runs[-1][1] <= gap + 1:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    return [(a, b + 1) for a, b in runs]


def _snap(edge: np.ndarray, x: int, reach: int) -> int:
    """The strongest edge within `reach` of x."""
    lo, hi = max(0, x - reach), min(len(edge), x + reach + 1)
    return lo + int(np.argmax(edge[lo:hi]))


def _edges(img: Image.Image) -> tuple[int, int] | None:
    """The case's left and right edge columns in a straightened frame: the widest
    smooth run of columns that is case-shaped, snapped to the edges beside it."""
    w, h = img.size
    rows = slice(int(h * 0.08), int(h * 0.55))
    prof = np.median(_texture(img)[rows], axis=0)
    margin = max(4, w // 10)
    rough = float(np.median(np.concatenate([prof[:margin], prof[-margin:]])))
    case = (prof < rough * SMOOTH) | (_ground(img)[rows].mean(axis=0) < GROUND)
    runs = [r for r in _runs(case, max(1, w // 24))
            if MIN_RATIO * h * 0.7 <= r[1] - r[0] <= MAX_RATIO * h * 1.1]
    if not runs:
        return None
    run = max(runs, key=lambda r: r[1] - r[0])
    edge = _grad(_gray(img), 1)[rows].sum(axis=0)
    reach = max(2, (run[1] - run[0]) // 8)
    return _snap(edge, run[0], reach), _snap(edge, run[1] - 1, reach)


def _best_segment(smooth: np.ndarray) -> tuple[int, int]:
    """The stretch with the most smooth rows net of rough ones (a rough row costs two):
    the case, with a line of print inside it, rather than a lucky patch of carpet."""
    score = np.where(smooth, 1.0, -2.0)
    best, cur, start, span = 0.0, 0.0, 0, (0, len(smooth))
    for y, v in enumerate(score):
        if cur <= 0:
            cur, start = 0.0, y
        cur += v
        if cur > best:
            best, span = cur, (start, y + 1)
    return span


def _ends(img: Image.Image, lo: int, hi: int) -> tuple[int, int]:
    """Top and bottom rows of the case between its edge columns, snapped to the
    strongest end edge nearby. Rows are judged on their smoothest fifth, so a line
    of print across the middle of the face still counts as case."""
    w, h = img.size
    pad = max(2, (hi - lo) // 6)
    cols = slice(lo + pad, max(lo + pad + 1, hi - pad))
    tex = _texture(img)
    prof = np.percentile(tex[:, cols], 20, axis=1)
    outside = np.concatenate([tex[:, : max(1, lo - 10)], tex[:, min(w - 1, hi + 10):]], axis=1)
    rough = float(np.median(np.percentile(outside, 20, axis=1)))
    smooth = np.convolve(prof, np.ones(5) / 5, mode="same") < max(1.0, rough * SMOOTH_ROWS)
    unlike = _ground(img)[:, cols].mean(axis=1) < GROUND
    top, bot = _best_segment(smooth | unlike)
    edge = _grad(_gray(img), 0)[:, cols].sum(axis=1)
    reach = max(3, h // 50)
    return _snap(edge, top, reach), _snap(edge, bot - 1, reach)


def detect(data: bytes, turns: int = 0) -> Box | None:
    """The spine face in a spine photo turned `turns` quarter turns, or None when no
    case-shaped box is found."""
    img = open_upright(data, WORK_H)
    if img is None:
        return None
    img = frame(img, turns, 0)
    angle = _tilt(img)
    straight = img.rotate(angle, resample=Image.BILINEAR)
    w, h = straight.size
    pair = _edges(straight)
    if pair is None:
        return None
    lo, hi = pair
    top, bot = _ends(straight, lo, hi)
    box = Box(lo / w, top / h, (hi + 1) / w, (bot + 1) / h, round(angle, 2), turns % 4)
    if not MIN_RATIO <= box.ratio((w, h)) <= MAX_RATIO:
        return None
    return box


def cut(data: bytes, box: Box) -> Image.Image | None:
    """The box cut from the full-size photo, STRIP_H tall at its true proportions."""
    img = open_upright(data, STRIP_H * 3)
    if img is None:
        return None
    img = frame(img, box.turns, box.angle)
    w, h = img.size
    crop = img.crop((round(box.x0 * w), round(box.y0 * h), round(box.x1 * w),
                     round(box.y1 * h)))
    if crop.width < 2 or crop.height < 2:
        return None
    return crop.resize((max(1, round(crop.width * STRIP_H / crop.height)), STRIP_H),
                       Image.LANCZOS)


# A spine reads when its print stands off the metal at shelf size: enough of the
# face, scaled to a shelf spine's height, is crossed by sharp edges. Grey lettering
# on dark steel under a sleeve's glare is not, and the shelf shows the front edge
# instead. 6% splits the faint spines in those 72 photos from
# the rest; the few borderline ones land on /review, where a tap overrides it.
READS_EDGES = 0.06
SHELF_H = 172


def reads(strip: Image.Image) -> bool:
    """Whether the spine's print is legible enough at shelf size to stand in for a title."""
    small = strip.resize((max(3, round(strip.width * SHELF_H / strip.height)), SHELF_H))
    g = np.asarray(small.convert("L"), dtype=np.float32)
    side = max(1, g.shape[1] // 6)
    g = g[4:-4, side:-side]
    if g.size == 0:
        return False
    edges = np.concatenate([np.abs(np.diff(g, axis=1)).ravel(),
                            np.abs(np.diff(g, axis=0)).ravel()])
    return float((edges > 30).mean()) >= READS_EDGES


def strip_jpeg(strip: Image.Image) -> bytes:
    out = io.BytesIO()
    strip.save(out, "JPEG", quality=86, optimize=True)
    return out.getvalue()
