"""Bounded raster attachments. No remote fetches, paths, SVG, or metadata."""
from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import io
import warnings

MAX_IMAGE_BYTES = 512_000
MAX_SOURCE_BYTES = 8_000_000
MAX_SOURCE_PIXELS = 16_000_000
MAX_EDGE = 1536
MAX_IMAGES = 4
MAX_CONTEXT_IMAGES = 16
MAX_CONTEXT_IMAGE_BYTES = 2_000_000
RASTER_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


@dataclass(frozen=True)
class ImageAttachment:
    mime_type: str
    data: bytes = field(repr=False)
    width: int
    height: int

    def __post_init__(self):
        if (self.mime_type not in {"image/png", "image/jpeg"} or type(self.data) is not bytes
                or not 1 <= len(self.data) <= MAX_IMAGE_BYTES
                or type(self.width) is not int or type(self.height) is not int
                or not 1 <= self.width <= MAX_EDGE or not 1 <= self.height <= MAX_EDGE):
            raise ValueError("Invalid bounded image attachment")

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()

    def record(self):
        return {"mime_type": self.mime_type, "data": base64.b64encode(self.data).decode("ascii"),
                "width": self.width, "height": self.height, "sha256": self.sha256}

    @classmethod
    def from_record(cls, record):
        if not isinstance(record, Mapping):
            raise ValueError("Invalid image record")
        encoded = record.get("data")
        if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
            raise ValueError("Image record exceeds byte limit")
        try:
            image = cls(record.get("mime_type"), base64.b64decode(encoded, validate=True),
                        record.get("width"), record.get("height"))
            from PIL import Image
            with Image.open(io.BytesIO(image.data)) as decoded:
                if decoded.size != (image.width, image.height) or decoded.format != (
                        "PNG" if image.mime_type == "image/png" else "JPEG"):
                    raise ValueError("Invalid image record encoding")
                decoded.verify()
        except (OSError, TypeError, ValueError):
            raise ValueError("Invalid image record") from None
        if image.sha256 != record.get("sha256"):
            raise ValueError("Image record does not match its bytes")
        return image

    def data_url(self):
        return "data:" + self.mime_type + ";base64," + base64.b64encode(self.data).decode("ascii")


def normalize(mime_type, value):
    if mime_type not in RASTER_MIMES:
        raise ValueError("Unsupported raster MIME type")
    if isinstance(value, str):
        if len(value) > (MAX_SOURCE_BYTES * 4 // 3 + 4):
            raise ValueError("Image input exceeds byte limit")
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise ValueError("Invalid image base64") from None
    elif isinstance(value, (bytes, bytearray, memoryview)):
        if len(value) > MAX_SOURCE_BYTES:
            raise ValueError("Image input exceeds byte limit")
        raw = bytes(value)
    else:
        raise ValueError("Unsupported raster payload")
    if not 1 <= len(raw) <= MAX_SOURCE_BYTES:
        raise ValueError("Image input exceeds byte limit")
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise ValueError("Unsupported image encoding")
                if source.width * source.height > MAX_SOURCE_PIXELS:
                    raise ValueError("Image input exceeds pixel limit")
                source.seek(0)  # Animated images: explicitly use the first frame.
                source.load()
                # Bound decoded pixels before applying EXIF orientation.
                oriented = ImageOps.exif_transpose(source)
                if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    image = oriented.convert("RGB")
                image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        # Re-encode even small files to strip metadata and normalize formats.
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        if len(buffer.getvalue()) <= MAX_IMAGE_BYTES:
            return ImageAttachment("image/png", buffer.getvalue(), *image.size)
        for quality in (85, 70, 50):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            if len(buffer.getvalue()) <= MAX_IMAGE_BYTES:
                return ImageAttachment("image/jpeg", buffer.getvalue(), *image.size)
        while image.width > 64 and image.height > 64:
            image.thumbnail((image.width // 2, image.height // 2))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=70)
            if len(buffer.getvalue()) <= MAX_IMAGE_BYTES:
                return ImageAttachment("image/jpeg", buffer.getvalue(), *image.size)
        raise ValueError("Image cannot fit attachment limit")
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError("Invalid or unsafe image") from None


def observation_images(outputs):
    """Take one raster per display bundle. Never stringify image data."""
    images, notices = [], []
    display_ids = []
    for output in outputs:
        if output.kind == "clear":
            images.clear()
            notices.clear()
            display_ids.clear()
            continue
        if output.kind not in {"display", "execute_result", "update"}:
            continue
        if output.kind == "update" and output.display_id in display_ids:
            index = display_ids.index(output.display_id)
            images.pop(index)
            display_ids.pop(index)
        for mime_type in RASTER_MIMES:
            if mime_type not in output.data:
                continue
            if len(images) >= MAX_IMAGES:
                notices.append("Additional image omitted: per-cell image limit is 4.")
                break
            try:
                image = normalize(mime_type, output.data[mime_type])
                images.append(image.record())
                display_ids.append(output.display_id)
                notices.append(f"[Image {len(images)}: {image.width}x{image.height}, "
                               f"{image.mime_type}; attached for visual inspection. "
                               "Animated inputs use their first frame.]")
            except (ValueError, TypeError):
                notices.append("[Image rejected: invalid raster or size/pixel limit exceeded.]")
            break
    return tuple(images), tuple(notices)


def require_vision(model, messages):
    """Fail explicitly for known text-only or unrecognized model families."""
    if not any(isinstance(m.get("content"), list) for m in messages):
        return
    name = model.lower().rsplit("/", 1)[-1]
    supported = (name.startswith(("gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-5", "o1", "o3", "o4",
                                  "claude-3", "claude-sonnet-4", "claude-opus-4", "claude-haiku-4",
                                  "gemini-"))
                 or any(marker in name for marker in ("vision", "-vl", "/vl", "pixtral")))
    if not supported:
        from .provider import ProviderError
        raise ProviderError("Image input is unavailable for this model. Select a vision-capable "
                            "model with /model; image attachments were not silently discarded.",
                            kind="configuration")


def content_with_images(text, images):
    return ([{"type": "text", "text": text}] +
            [{"type": "image_url", "image_url": {"url": image.data_url()}} for image in images])
