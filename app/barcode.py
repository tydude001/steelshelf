"""Local barcode decode: the UPC off a photo, with no model in the loop.

zxing-cpp reads the EAN/UPC on the back (or a sticker in the "other" shot) and
the GS1 check digit confirms it, so a code found here is taken over what Claude
read off the digits under the bars. Most steelbook cases carry no barcode at
all — it is on the slip or a removable sticker — so a miss is usual and means
nothing; the model still reads whatever the photos show.

Runs from the identify and save POSTs, never on a page render.
"""

import io
import logging
from dataclasses import replace

import zxingcpp
from PIL import Image, ImageOps

from app.identify import Identification, upc_valid

log = logging.getLogger("steelshelf.barcode")

FORMATS = [
    zxingcpp.BarcodeFormat.EAN13,
    zxingcpp.BarcodeFormat.UPCA,
    zxingcpp.BarcodeFormat.UPCE,
    zxingcpp.BarcodeFormat.EAN8,
]
# The photos most likely to show the code, first.
ORDER = ("back", "other", "spine", "front")


def decode(data: bytes) -> str | None:
    """The first valid UPC/EAN in a photo, as the digits printed under it, or None."""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
    except Exception:
        return None
    for found in zxingcpp.read_barcodes(img, formats=FORMATS):
        code = found.text
        # zxing reports a UPC-A as EAN-13 with a leading 0; the case prints 12 digits.
        if len(code) == 13 and code.startswith("0"):
            code = code[1:]
        if upc_valid(code):
            return code
    return None


def decode_any(shots: list[tuple[str, bytes]]) -> str | None:
    """The first code found across the photos, back first."""
    by_kind = dict(shots)
    for kind in ORDER:
        if kind in by_kind:
            code = decode(by_kind[kind])
            if code:
                log.info("barcode %s read from the %s photo", code, kind)
                return code
    return None


def apply(found: Identification, code: str | None) -> Identification:
    """The decoded code over the model's reading; a disagreement is noted as a doubt."""
    if not code:
        return found
    read = found.values.get("upc")
    doubts = list(found.doubts)
    if read and read != code:
        doubts.insert(0, f"upc: the barcode decodes to {code}; Claude read {read} — kept the decode")
    return replace(found, values={**found.values, "upc": code}, doubts=doubts)
