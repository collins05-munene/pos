"""
Product images: validate, shrink and re-encode an upload, then store it.

Why this exists: a phone photo is 3-8 MB. Stored as-is, a shop with 500 products would eat
gigabytes and make the POS grid crawl on mobile data. Every upload is therefore:

  * checked to be a real JPEG / PNG / WebP (never trusting the extension or Content-Type),
  * rotated per its EXIF tag, flattened onto white (no transparency surprises on receipts/tiles),
  * re-encoded as WebP twice:  FULL (<= 800 px, product page)  and  THUMB (<= 320 px, POS tiles),
    which typically lands at ~20-50 KB and ~6-15 KB.

Only the file *paths* live in the database (see Product.image / Product.thumb); the bytes live in
Django's default storage, so moving to S3 / Cloudinary later is a settings change, not a rewrite.
"""
import hashlib
import io
import uuid
from dataclasses import dataclass

from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024          # what we accept over the wire
MAX_SOURCE_PIXELS = 40_000_000               # refuse decompression bombs *before* decoding pixels
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}

FULL_BOX, FULL_QUALITY = 800, 78
THUMB_BOX, THUMB_QUALITY = 320, 72


class ImageRejected(ValueError):
    """Raised with a message that is safe to show the user."""


@dataclass
class ProcessedImage:
    full: bytes
    thumb: bytes
    token: str        # short content hash; used as the cache-busting ?v= and in the file names


def _flatten(img):
    """Return an RGB image; transparent areas become white."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def _encode(img, box, quality):
    out = img.copy()
    out.thumbnail((box, box), Image.LANCZOS)          # keeps aspect ratio, never upscales
    buf = io.BytesIO()
    out.save(buf, format="WEBP", quality=quality, method=4)
    return buf.getvalue()


def compress(uploaded) -> ProcessedImage:
    size = getattr(uploaded, "size", None)
    if size is not None and size > MAX_UPLOAD_BYTES:
        raise ImageRejected(f"That image is too large ({size / 1024 / 1024:.1f} MB). The limit is "
                            f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
    try:
        img = Image.open(uploaded)                    # reads the header only
        if img.format not in ALLOWED_FORMATS:
            raise ImageRejected("Please upload a JPG, PNG or WebP image.")
        if img.width * img.height > MAX_SOURCE_PIXELS:
            raise ImageRejected("That image has too many pixels. Please use a smaller photo.")
        if img.format == "JPEG":
            img.draft("RGB", (FULL_BOX * 2, FULL_BOX * 2))   # lets libjpeg decode large photos far faster
        img = ImageOps.exif_transpose(img)
        img = _flatten(img)
        full = _encode(img, FULL_BOX, FULL_QUALITY)
        thumb = _encode(img, THUMB_BOX, THUMB_QUALITY)
    except ImageRejected:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        raise ImageRejected("That file could not be read as an image. Try another photo.")
    return ProcessedImage(full=full, thumb=thumb, token=hashlib.sha1(full).hexdigest()[:12])


# ---------------------------------------------------------------------------
# Attach / detach on a Product or a ProductVariant
# ---------------------------------------------------------------------------
def _delete_files(names):
    from django.core.files.storage import default_storage
    for name in names:
        try:
            default_storage.delete(name)
        except Exception:        # a missing/locked old file must never break a save
            pass


def _update_fields(obj):
    """The image columns, plus updated_at when the model has one (Product does, ProductVariant does not)."""
    fields = ["image", "thumb", "image_token"]
    if any(f.name == "updated_at" for f in obj._meta.fields):
        fields.append("updated_at")
    return fields


def set_image(obj, processed: ProcessedImage):
    """
    Store the new files, point `obj` (a Product or a ProductVariant) at them, and remove the old
    ones once the save commits.
    """
    old = [n for n in (obj.image.name, obj.thumb.name) if n]
    stem = f"{uuid.uuid4().hex[:10]}-{processed.token}"
    obj.image.save(f"{stem}.webp", ContentFile(processed.full), save=False)
    obj.thumb.save(f"{stem}-t.webp", ContentFile(processed.thumb), save=False)
    obj.image_token = processed.token
    obj.save(update_fields=_update_fields(obj))
    if old:
        transaction.on_commit(lambda: _delete_files(old))


def clear_image(obj):
    old = [n for n in (obj.image.name, obj.thumb.name) if n]
    if not old:
        return
    obj.image = ""
    obj.thumb = ""
    obj.image_token = ""
    obj.save(update_fields=_update_fields(obj))
    transaction.on_commit(lambda: _delete_files(old))


# Original names, still used by the product form and the tests.
set_product_image = set_image
clear_product_image = clear_image
