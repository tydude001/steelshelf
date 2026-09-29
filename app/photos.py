"""Photo files on disk under PHOTO_DIR, one directory per item.

The `photos.path` column holds the path *relative to* PHOTO_DIR
(`12/front-3f9a1c2e.jpg`), so the same row resolves on the host and inside the
container, whose mount points differ.

Derived images live beside them under `PHOTO_DIR/_derived/`, not in the
database: a thumbnail per photo (`thumb-<photo id>.jpg`) for the covers grid and
the item page, an art strip per item (`spine-<item id>.jpg`) cut from the front
photo's left edge, and the real spine cut from the spine photo
(`spine-photo-<item id>.jpg`); the shelf paints one or the other. All are made
at save and backfilled at startup; a request for one that is missing makes it
then, from the original on disk.
"""

import colorsys
import io
import re
import shutil
import time
import uuid
from pathlib import Path

import pillow_heif
from PIL import Image, ImageEnhance, ImageOps

pillow_heif.register_heif_opener()

# "other" is a detail shot: a limited-edition number, the bottom of a box set, a sticker.
KINDS = ("front", "spine", "back", "other")
MAX_BYTES = 25 * 1024 * 1024  # a phone photo is 3-8 MB; this is a bound, not a target

# Content types a phone camera or gallery hands a file input.
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


class PhotoError(ValueError):
    """An upload that is not a photo this app will keep."""


def check(kind: str, content_type: str | None, data: bytes) -> str:
    """The file extension for an acceptable upload, or PhotoError saying why not."""
    ext = EXTENSIONS.get((content_type or "").lower())
    if ext is None:
        raise PhotoError(f"{kind}: {content_type or 'unknown type'} is not a supported image")
    if not data:
        raise PhotoError(f"{kind}: the file is empty")
    if len(data) > MAX_BYTES:
        raise PhotoError(f"{kind}: {len(data) // (1024 * 1024)} MB is over the 25 MB limit")
    return ext


def save(photo_dir: str, item_id: int, kind: str, ext: str, data: bytes) -> str:
    """Write one photo and return its path relative to photo_dir."""
    rel = Path(str(item_id)) / f"{kind}-{uuid.uuid4().hex[:8]}{ext}"
    dest = Path(photo_dir) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return rel.as_posix()


def remove_item_dir(photo_dir: str, item_id: int) -> None:
    shutil.rmtree(Path(photo_dir) / str(item_id), ignore_errors=True)
    spine_path(photo_dir, item_id).unlink(missing_ok=True)
    spine_photo_path(photo_dir, item_id).unlink(missing_ok=True)


def resolve(photo_dir: str, rel: str) -> Path | None:
    """Absolute path for a stored relative path, or None if it escapes photo_dir."""
    root = Path(photo_dir).resolve()
    p = (root / rel).resolve()
    return p if p.is_relative_to(root) and p.is_file() else None


# --- spine colour ---------------------------------------------------------------
# The shelf draws each steelbook's spine in a colour from its own front photo.
# Computed once at save (and backfilled at startup), never on a page render.

SPINE_LIGHTNESS = (0.24, 0.62)  # dark enough to read as metal, light enough to show
SPINE_MAX_SATURATION = 0.72  # a photo's neon becomes a painted case, not a highlighter
LIGHT_INK, DARK_INK = "#F3F1EC", "#23262B"


def spine_color(data: bytes) -> str | None:
    """The front photo's dominant colour as '#rrggbb', or None if it will not decode.

    The centre of the frame is the case (the edges are table and hand); of its
    few main colours, the one covering most area wins, weighted towards colour
    over grey so a black-and-silver cover with one red title still reads red.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (160, 160))  # JPEG decodes at reduced size: fast on 8 MB photos
        img = img.convert("RGB")
    except Exception:
        return None
    img.thumbnail((96, 96))
    w, h = img.size
    img = img.crop((w // 8, h // 8, w - w // 8, h - h // 8))
    counts = img.quantize(colors=6).convert("RGB").getcolors(96 * 96) or []
    best, best_score = None, -1.0
    for n, rgb in counts:
        hue, light, sat = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
        score = n * (0.35 + sat) * (0.4 if light < 0.08 or light > 0.92 else 1.0)
        if score > best_score:
            best, best_score = (hue, light, sat), score
    if best is None:
        return None
    hue, light, sat = best
    light = min(max(light, SPINE_LIGHTNESS[0]), SPINE_LIGHTNESS[1])
    r, g, b = colorsys.hls_to_rgb(hue, light, min(sat, SPINE_MAX_SATURATION))
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def spine_ink(color: str) -> str:
    """Title colour for a spine of this colour: dark on light cases, light on dark."""
    r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    luminance = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    # 0.2 is where the two inks tie on contrast ratio (about 3.7:1 each way).
    return DARK_INK if luminance > 0.2 else LIGHT_INK


# --- derived images -----------------------------------------------------------
# A phone photo is 3-4 MB; the covers grid showed 71 of them at full size. The
# thumbnail is what every page shows, the original is a tap away.

DERIVED_DIR = "_derived"
THUMB_EDGE = 480
# The shelf spine is ~1:5. A steelbook's front art wraps round onto the spine, so a
# strip from the left edge of the front cover at true proportions is what the real
# spine shows; the strip is cut from the middle of the frame, where the case sits in
# a hand-held photo (the edges are table and hand).
SPINE_STRIP = (84, 400)
CASE_BOX = (0.27, 0.19, 0.73, 0.81)  # fraction of the frame the case fills


def _open_small(data: bytes, edge: int) -> Image.Image | None:
    """Decode a photo upright at roughly `edge` px, or None if it will not decode."""
    try:
        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (edge * 2, edge * 2))  # JPEG decodes at reduced size: fast
        return ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        return None


def thumbnail(data: bytes) -> bytes | None:
    """A JPEG no larger than THUMB_EDGE on the long side, or None if undecodable."""
    img = _open_small(data, THUMB_EDGE)
    if img is None:
        return None
    img.thumbnail((THUMB_EDGE, THUMB_EDGE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


def spine_strip(data: bytes) -> bytes | None:
    """The front cover's left edge as a SPINE_STRIP JPEG, or None if undecodable."""
    img = _open_small(data, SPINE_STRIP[1] * 2)
    if img is None:
        return None
    w, h = img.size
    case = img.crop((int(w * CASE_BOX[0]), int(h * CASE_BOX[1]),
                     int(w * CASE_BOX[2]), int(h * CASE_BOX[3])))
    cw, ch = case.size
    sw = max(1, int(ch * SPINE_STRIP[0] / SPINE_STRIP[1]))  # no squeeze: aspect kept
    strip = case.crop((0, 0, min(sw, cw), ch)).resize(SPINE_STRIP, Image.LANCZOS)
    strip = ImageEnhance.Brightness(strip).enhance(0.9)
    out = io.BytesIO()
    strip.save(out, "JPEG", quality=86, optimize=True)
    return out.getvalue()


def derived_path(photo_dir: str, name: str) -> Path:
    return Path(photo_dir) / DERIVED_DIR / name


def thumb_path(photo_dir: str, photo_id: int) -> Path:
    return derived_path(photo_dir, f"thumb-{photo_id}.jpg")


def spine_path(photo_dir: str, item_id: int) -> Path:
    return derived_path(photo_dir, f"spine-{item_id}.jpg")


def spine_photo_path(photo_dir: str, item_id: int) -> Path:
    """The case's spine, cut from its spine photo (`spine.cut`)."""
    return derived_path(photo_dir, f"spine-photo-{item_id}.jpg")


def write_derived(dest: Path, data: bytes | None) -> bool:
    """Write a derived image; False (and nothing written) when there is none to write."""
    if data is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def ensure_thumb(photo_dir: str, photo_id: int, original: Path) -> Path | None:
    """The thumbnail's path, made from the original if it is not there yet."""
    dest = thumb_path(photo_dir, photo_id)
    if dest.is_file() or write_derived(dest, thumbnail(original.read_bytes())):
        return dest
    return None


def ensure_spine(photo_dir: str, item_id: int, front: Path) -> Path | None:
    dest = spine_path(photo_dir, item_id)
    if dest.is_file() or write_derived(dest, spine_strip(front.read_bytes())):
        return dest
    return None


# --- drafts -------------------------------------------------------------------
# Photos sent to /add/identify are held here until the item is saved, so the
# review step does not make the phone take them again. They are not in the
# database: a draft that is never saved is pruned after DRAFT_MAX_AGE.

DRAFT_DIR = "_drafts"
DRAFT_MAX_AGE = 24 * 3600
_TOKEN = re.compile(r"[0-9a-f]{32}")


def save_draft(photo_dir: str, uploads: list[tuple[str, str, bytes]]) -> str:
    """Hold (kind, ext, data) uploads under a new draft token and return it."""
    prune_drafts(photo_dir)
    token = uuid.uuid4().hex
    d = Path(photo_dir) / DRAFT_DIR / token
    d.mkdir(parents=True)
    for kind, ext, data in uploads:
        (d / f"{kind}{ext}").write_bytes(data)
    return token


def load_draft(photo_dir: str, token: str) -> list[tuple[str, str, bytes]] | None:
    """The (kind, ext, data) photos held under a token, or None if it is unknown."""
    if not _TOKEN.fullmatch(token):
        return None
    d = Path(photo_dir) / DRAFT_DIR / token
    if not d.is_dir():
        return None
    held = {p.stem: p for p in d.iterdir() if p.stem in KINDS}
    return [(k, held[k].suffix, held[k].read_bytes()) for k in KINDS if k in held]


def discard_draft(photo_dir: str, token: str) -> None:
    if _TOKEN.fullmatch(token):
        shutil.rmtree(Path(photo_dir) / DRAFT_DIR / token, ignore_errors=True)


def prune_drafts(photo_dir: str, max_age: float = DRAFT_MAX_AGE) -> None:
    root = Path(photo_dir) / DRAFT_DIR
    if not root.is_dir():
        return
    cutoff = time.time() - max_age
    for d in root.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
