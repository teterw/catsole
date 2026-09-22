"""Turn cover art into something a 1-bit panel can show.

Windows hands out a thumbnail per media session, usually a small PNG. At
48x48 with one bit per pixel there is very little to work with, so the
conversion leans on two things: a contrast stretch, because covers are
often mid-tone and threshold badly, and error-diffusion dithering, which
holds shape far better than a flat threshold at this size.

Output is XBM-packed -- rows padded to whole bytes, bits least significant
first -- because that is what U8g2's drawXBM expects, so the device can
blit it with no unpacking.
"""

from __future__ import annotations

import base64
import io
import logging

log = logging.getLogger(__name__)

try:
    from PIL import Image, ImageOps

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without Pillow
    Image = None
    ImageOps = None
    PIL_AVAILABLE = False

ART_SIZE = 48  # 48x48 packs to exactly 6 bytes per row


def pack_xbm(image, size: int = ART_SIZE) -> bytes:
    """Pack a 1-bit PIL image into XBM byte order.

    Each row starts on a byte boundary and bits run least significant
    first, matching drawXBM. A set bit lights a pixel.
    """
    stride = (size + 7) // 8
    out = bytearray(stride * size)
    pixels = image.load()

    for y in range(size):
        for x in range(size):
            # PIL mode "1": 0 is black, 255 is white. The panel lights the
            # bits we set, and covers read better as light-on-dark here.
            if pixels[x, y]:
                out[y * stride + (x >> 3)] |= 1 << (x & 7)
    return bytes(out)


def render_1bit(raw: bytes, size: int = ART_SIZE) -> bytes | None:
    """Convert encoded image bytes into an XBM-packed 1-bit square."""
    if not PIL_AVAILABLE or not raw:
        return None
    try:
        image = Image.open(io.BytesIO(raw))
        image = image.convert("L")

        # Square off by cropping to the centre rather than squashing, so
        # round covers and off-square thumbnails keep their proportions.
        width, height = image.size
        if width != height:
            edge = min(width, height)
            left = (width - edge) // 2
            top = (height - edge) // 2
            image = image.crop((left, top, left + edge, top + edge))

        image = image.resize((size, size), Image.LANCZOS)

        # Covers are often mid-tone and low contrast, which threshold and
        # dither into mush. Stretching first gives the dither something to
        # work with.
        image = ImageOps.autocontrast(image, cutoff=2)

        # Error diffusion, not a flat threshold: at this size it is the
        # difference between a recognisable shape and a blob.
        image = image.convert("1", dither=Image.FLOYDSTEINBERG)

        return pack_xbm(image, size)
    except Exception as exc:
        log.debug("artwork conversion failed: %s", exc)
        return None


def encode_art(packed: bytes) -> str:
    """Base64 for the wire, so the bitmap survives a text protocol."""
    return base64.b64encode(packed).decode("ascii")


def art_signature(raw: bytes) -> str:
    """Cheap identity for a thumbnail, to avoid resending an unchanged one."""
    if not raw:
        return ""
    # Length plus a sample beats hashing the whole buffer and is plenty to
    # tell one cover from another.
    return f"{len(raw)}:{raw[:16].hex()}"
