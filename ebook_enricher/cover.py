"""Pure cover-image operations: parse OPF for cover path, save sidecar,
download from URL. No enrichment policy here — that lives in enrich.py.
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Optional

import httpx
# Use defusedxml's drop-in replacement for ElementTree — a malicious
# EPUB could otherwise feed us a billion-laughs payload or external
# entity reference and OOM/exfiltrate from the enricher container.
import defusedxml.ElementTree as ET

logger = logging.getLogger(__name__)


# Below these thresholds we treat the candidate cover as a placeholder
# or broken asset and reject the swap.
MIN_COVER_SIZE_BYTES = 50_000   # 50KB — smaller is almost certainly a tracking pixel or placeholder
MIN_COVER_WIDTH = 500           # pixels (we trust Hardcover's reported width when checking)
DOWNLOAD_TIMEOUT_S = 10


async def download_cover(url: str, *, timeout_s: int = DOWNLOAD_TIMEOUT_S) -> Optional[bytes]:
    """GET the image at `url`. Returns bytes on a successful 200 with a
    reasonable payload size. Returns None on any failure (network,
    timeout, non-200, suspiciously small body). Never raises.

    Cover replacement is best-effort: any failure here is logged and
    the caller proceeds without replacing the cover.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url)
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.warning("cover download failed (network): %s — %s", url, e)
        return None

    if resp.status_code != 200:
        logger.warning("cover download HTTP %d: %s", resp.status_code, url)
        return None

    data = resp.content
    if len(data) < MIN_COVER_SIZE_BYTES:
        logger.warning(
            "cover download too small (%d bytes < %d): %s",
            len(data), MIN_COVER_SIZE_BYTES, url,
        )
        return None

    return data


_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
}


def _find_opf_path(zf: zipfile.ZipFile) -> Optional[str]:
    """Look up the OPF path from META-INF/container.xml. Returns None
    if missing or unparseable — caller treats that as 'no cover'."""
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError):
        return None
    rootfile = container.find("container:rootfiles/container:rootfile", _NS)
    if rootfile is None:
        return None
    return rootfile.get("full-path")


def find_cover_path_in_opf(epub_path: Path) -> Optional[str]:
    """Open the EPUB, locate <meta name="cover" content="<id>"/> in OPF,
    resolve <id> to the manifest item's href (joined to the OPF dir).
    Returns the path-within-zip (e.g. 'OEBPS/images/cover.jpg') or None
    if no cover meta is declared OR the declared manifest item isn't
    present in the zip.
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_path = _find_opf_path(zf)
            if not opf_path:
                return None
            try:
                opf_root = ET.fromstring(zf.read(opf_path))
            except (KeyError, ET.ParseError):
                return None

            # Find <meta name="cover" content="<id>"/> — EPUB 2 style.
            metadata = opf_root.find("opf:metadata", _NS)
            if metadata is None:
                return None
            cover_id = None
            for meta_el in metadata.findall("opf:meta", _NS):
                if meta_el.get("name") == "cover":
                    cover_id = meta_el.get("content")
                    break
            if not cover_id:
                return None

            # Resolve the manifest item by id.
            manifest = opf_root.find("opf:manifest", _NS)
            if manifest is None:
                return None
            for item in manifest.findall("opf:item", _NS):
                if item.get("id") == cover_id:
                    href = item.get("href")
                    if not href:
                        return None
                    # Resolve relative to the OPF dir
                    opf_dir = str(Path(opf_path).parent)
                    if opf_dir and opf_dir != ".":
                        full = f"{opf_dir}/{href}"
                    else:
                        full = href
                    # Must actually exist in the zip
                    if full in zf.namelist():
                        return full
                    return None

            return None
    except zipfile.BadZipFile:
        return None


def _sidecar_path(epub_path: Path) -> Path:
    """Recovery-sidecar location: same directory, base name with
    .original.jpg suffix. e.g. /a/b/Foo.epub → /a/b/Foo.original.jpg."""
    return epub_path.parent / (epub_path.stem + ".original.jpg")


def save_sidecar_if_absent(epub_path: Path) -> bool:
    """If `<epub>.original.jpg` does not exist next to the EPUB, extract
    the current cover bytes and write them as the sidecar. Idempotent:
    returns True if a usable sidecar exists at end of call (either pre-
    existing or just-written). Returns False if we couldn't save
    (no cover in EPUB, OS error) — caller should skip cover swap in
    that case to avoid losing the only original.
    """
    sidecar = _sidecar_path(epub_path)
    if sidecar.exists():
        return True

    cover_zip_path = find_cover_path_in_opf(epub_path)
    if not cover_zip_path:
        return False

    try:
        with zipfile.ZipFile(epub_path) as zf:
            data = zf.read(cover_zip_path)
    except (zipfile.BadZipFile, KeyError) as e:
        logger.warning("could not read cover from EPUB %s: %s", epub_path, e)
        return False

    try:
        sidecar.write_bytes(data)
    except OSError as e:
        logger.warning("could not write sidecar %s: %s", sidecar, e)
        return False

    return True


MAX_COVER_LONG_EDGE = 1648  # PW5's larger display dimension at native 300dpi
JPEG_QUALITY = 85


DEFAULT_COVER_HREF = "images/cover.jpg"          # path INSIDE OPF dir
DEFAULT_COVER_MANIFEST_ID = "cover-image"
DEFAULT_COVER_MEDIA_TYPE = "image/jpeg"


def add_cover_to_opf(opf_root, opf_path: str) -> tuple[str, str]:
    """Mutate an OPF tree IN PLACE to register a new cover image, returning
    `(cover_zip_path, cover_href)`.

    Appends:
      * `<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg"/>`
        to the existing `<manifest>`.
      * `<meta name="cover" content="cover-image"/>` to the existing
        `<metadata>`.

    The caller (write_meta) writes the cover bytes to `cover_zip_path`
    during the same atomic zip rewrite.

    Raises:
      ValueError if `<metadata>` or `<manifest>` is absent.
      ValueError if a manifest item with id="cover-image" already exists
      (the caller should have taken the REPLACE path).
    """
    metadata = opf_root.find(f"{{{_NS['opf']}}}metadata")
    if metadata is None:
        raise ValueError("OPF has no <metadata> element; cannot register cover")
    manifest = opf_root.find(f"{{{_NS['opf']}}}manifest")
    if manifest is None:
        raise ValueError("OPF has no <manifest> element; cannot register cover")

    # Collision guard: if id=cover-image already exists, the EPUB already
    # has *some* cover registration and we should have taken REPLACE.
    for item in manifest.findall(f"{{{_NS['opf']}}}item"):
        if item.get("id") == DEFAULT_COVER_MANIFEST_ID:
            raise ValueError(
                f"manifest item with id='{DEFAULT_COVER_MANIFEST_ID}' already exists; "
                "caller should use cover_override (REPLACE) instead of cover_add"
            )

    # Append manifest item
    import xml.etree.ElementTree as _ET   # std lib; defusedxml only secures parsing
    item = _ET.SubElement(manifest, f"{{{_NS['opf']}}}item")
    item.set("id", DEFAULT_COVER_MANIFEST_ID)
    item.set("href", DEFAULT_COVER_HREF)
    item.set("media-type", DEFAULT_COVER_MEDIA_TYPE)

    # Append meta tag inside metadata
    meta = _ET.SubElement(metadata, f"{{{_NS['opf']}}}meta")
    meta.set("name", "cover")
    meta.set("content", DEFAULT_COVER_MANIFEST_ID)

    # Resolve the zip path: OPF's dir + the href
    opf_dir = str(Path(opf_path).parent)
    if opf_dir and opf_dir != ".":
        zip_path = f"{opf_dir}/{DEFAULT_COVER_HREF}"
    else:
        zip_path = DEFAULT_COVER_HREF
    return zip_path, DEFAULT_COVER_HREF


def resize_cover_if_needed(image_bytes: bytes) -> bytes:
    """If the image's longest edge exceeds MAX_COVER_LONG_EDGE, downscale
    (preserving aspect ratio) and re-encode as JPEG quality 85. Otherwise
    return the input bytes unchanged.

    Never raises: on any decode/encode failure, returns the input bytes
    untouched and logs a warning. Cover replacement is best-effort.
    """
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        if max(img.size) <= MAX_COVER_LONG_EDGE:
            return image_bytes  # already small enough; no re-encode
        img.thumbnail(
            (MAX_COVER_LONG_EDGE, MAX_COVER_LONG_EDGE),
            Image.Resampling.LANCZOS,
        )
        # Ensure RGB for JPEG (book covers usually are, but defensive)
        if img.mode != "RGB":
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img.convert("RGB"))
            img = background
        out = BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()
    except Exception as e:
        logger.warning("cover resize failed, using original: %s", e)
        return image_bytes
