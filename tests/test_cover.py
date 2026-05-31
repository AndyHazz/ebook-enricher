"""Tests for ebook_enricher.cover — pure cover ops (no enrichment policy)."""
import pytest
import respx
import httpx

from ebook_enricher import cover


@pytest.mark.asyncio
async def test_download_cover_returns_bytes_on_200():
    body = b"x" * 100_000  # 100KB, above MIN_COVER_SIZE_BYTES
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=body))
        result = await cover.download_cover(url)
    assert result == body


@pytest.mark.asyncio
async def test_download_cover_returns_none_on_5xx():
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(503))
        result = await cover.download_cover(url)
    assert result is None


@pytest.mark.asyncio
async def test_download_cover_returns_none_on_timeout():
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.TimeoutException("timeout"))
        result = await cover.download_cover(url)
    assert result is None


@pytest.mark.asyncio
async def test_download_cover_rejects_tiny_payload():
    body = b"x" * 1_000  # 1KB, below MIN_COVER_SIZE_BYTES (50KB)
    url = "https://assets.hardcover.app/test.jpg"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=body))
        result = await cover.download_cover(url)
    assert result is None


def test_find_cover_path_finds_standard_meta(epub_with_cover):
    """OPF with <meta name="cover" content="X"/> + manifest item → returns the href."""
    path = cover.find_cover_path_in_opf(epub_with_cover)
    assert path == "OEBPS/images/cover.jpg"


def test_find_cover_path_returns_none_when_no_meta(epub_without_cover):
    """OPF without cover meta → None."""
    assert cover.find_cover_path_in_opf(epub_without_cover) is None


def test_find_cover_path_returns_none_when_manifest_broken(epub_with_broken_cover_ref):
    """OPF cover meta points at a manifest id that doesn't exist → None."""
    assert cover.find_cover_path_in_opf(epub_with_broken_cover_ref) is None


def test_save_sidecar_writes_once(epub_with_cover):
    """First call writes the sidecar; second call is a no-op."""
    sidecar = epub_with_cover.with_suffix("").parent / (
        epub_with_cover.stem + ".original.jpg"
    )
    assert not sidecar.exists()

    ok1 = cover.save_sidecar_if_absent(epub_with_cover)
    assert ok1 is True
    assert sidecar.exists()
    first_bytes = sidecar.read_bytes()

    ok2 = cover.save_sidecar_if_absent(epub_with_cover)
    assert ok2 is True
    # Bytes unchanged — second call did NOT rewrite
    assert sidecar.read_bytes() == first_bytes


def test_save_sidecar_preserves_true_original(epub_with_cover):
    """Sidecar bytes are the original cover bytes, not anything else."""
    cover.save_sidecar_if_absent(epub_with_cover)
    sidecar = epub_with_cover.parent / (epub_with_cover.stem + ".original.jpg")
    # COVER_BYTES_ORIGINAL is defined in conftest.py
    from tests.conftest import COVER_BYTES_ORIGINAL
    assert sidecar.read_bytes() == COVER_BYTES_ORIGINAL


def test_save_sidecar_returns_false_when_no_cover(epub_without_cover):
    """No cover in EPUB → can't save sidecar → returns False."""
    ok = cover.save_sidecar_if_absent(epub_without_cover)
    assert ok is False
    sidecar = epub_without_cover.parent / (epub_without_cover.stem + ".original.jpg")
    assert not sidecar.exists()


def _make_jpeg(width: int, height: int) -> bytes:
    """Generate a real JPEG of the given size for tests."""
    from io import BytesIO
    from PIL import Image
    img = Image.new("RGB", (width, height), (200, 100, 50))
    out = BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def test_resize_skips_when_already_small_enough():
    """Image with longest edge <= 1648 is returned unchanged (no re-encoding)."""
    img_bytes = _make_jpeg(1200, 1648)  # exactly at the limit, no resize
    result = cover.resize_cover_if_needed(img_bytes)
    assert result is img_bytes or result == img_bytes  # unchanged


def test_resize_skips_when_smaller():
    """Image smaller than target is returned unchanged."""
    img_bytes = _make_jpeg(800, 1000)
    result = cover.resize_cover_if_needed(img_bytes)
    assert result == img_bytes


def test_resize_downscales_when_longest_edge_too_big():
    """Image with longest edge > MAX_COVER_LONG_EDGE is downscaled to fit."""
    from io import BytesIO
    from PIL import Image
    img_bytes = _make_jpeg(2000, 3000)  # 3000 > 1648
    result = cover.resize_cover_if_needed(img_bytes)
    # Result is smaller than input
    assert len(result) < len(img_bytes)
    # Decode and check dimensions
    img = Image.open(BytesIO(result))
    assert max(img.size) == cover.MAX_COVER_LONG_EDGE
    # Aspect preserved (2:3 → roughly 1099:1648)
    w, h = img.size
    assert abs((w / h) - (2000 / 3000)) < 0.01


def test_resize_handles_wider_than_tall():
    """A landscape image (rare for book covers) is also clamped to 1648 max."""
    from io import BytesIO
    from PIL import Image
    img_bytes = _make_jpeg(2400, 1200)
    result = cover.resize_cover_if_needed(img_bytes)
    img = Image.open(BytesIO(result))
    assert max(img.size) == cover.MAX_COVER_LONG_EDGE


def test_resize_returns_original_on_decode_failure():
    """Garbage bytes → return them unchanged (don't crash)."""
    garbage = b"this is not a jpeg at all"
    result = cover.resize_cover_if_needed(garbage)
    assert result == garbage


# ---------- add_cover_to_opf ----------
# Pure OPF tree mutation: appends manifest item + meta cover tag for the
# "EPUB has no cover" case. Used by enrich.py when find_cover_path_in_opf
# returns None.

import defusedxml.ElementTree as ET

_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS  = "http://purl.org/dc/elements/1.1/"


def _make_opf_root(have_metadata: bool = True, have_manifest: bool = True, extra_items: str = "") -> ET.Element:
    """Build an OPF tree fragment. Knobs let tests omit metadata or manifest
    to verify error paths."""
    meta_block = (
        f'<metadata xmlns:dc="{_DC_NS}">'
        '<dc:title>X</dc:title>'
        '</metadata>'
    ) if have_metadata else ""
    manifest_block = (
        '<manifest>'
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"/>'
        f'{extra_items}'
        '</manifest>'
    ) if have_manifest else ""
    src = (
        f'<package xmlns="{_OPF_NS}" version="3.0" unique-identifier="uid">'
        f'{meta_block}{manifest_block}'
        '</package>'
    )
    return ET.fromstring(src)


def test_add_cover_to_opf_inserts_manifest_item_and_meta_tag():
    """Happy path: a fresh OPF gets <item id=cover-image .../> in the manifest
    and <meta name=cover content=cover-image/> in metadata."""
    root = _make_opf_root()
    zip_path, href = cover.add_cover_to_opf(root, "OEBPS/Content.opf")
    assert zip_path == "OEBPS/images/cover.jpg"
    assert href == "images/cover.jpg"
    # Manifest item present
    NS = {"opf": _OPF_NS}
    items = root.find("opf:manifest", NS).findall("opf:item", NS)
    cover_items = [i for i in items if i.get("id") == "cover-image"]
    assert len(cover_items) == 1
    assert cover_items[0].get("href") == "images/cover.jpg"
    assert cover_items[0].get("media-type") == "image/jpeg"
    # Meta tag present
    metas = root.find("opf:metadata", NS).findall("opf:meta", NS)
    cover_metas = [m for m in metas if m.get("name") == "cover"]
    assert len(cover_metas) == 1
    assert cover_metas[0].get("content") == "cover-image"


def test_add_cover_to_opf_uses_root_layout_when_opf_at_zip_root():
    """OPF at root (no subdir) → zip path is plain 'images/cover.jpg'."""
    root = _make_opf_root()
    zip_path, href = cover.add_cover_to_opf(root, "content.opf")
    assert zip_path == "images/cover.jpg"
    assert href == "images/cover.jpg"


def test_add_cover_to_opf_raises_when_metadata_missing():
    """Defensive: malformed OPF without <metadata> can't be safely mutated."""
    root = _make_opf_root(have_metadata=False)
    with pytest.raises(ValueError, match="metadata"):
        cover.add_cover_to_opf(root, "OEBPS/Content.opf")


def test_add_cover_to_opf_raises_when_manifest_missing():
    """Defensive: malformed OPF without <manifest> can't register the cover."""
    root = _make_opf_root(have_manifest=False)
    with pytest.raises(ValueError, match="manifest"):
        cover.add_cover_to_opf(root, "OEBPS/Content.opf")


def test_add_cover_to_opf_raises_when_cover_id_collides():
    """Defensive: if id=cover-image already exists, caller should have taken
    the REPLACE path instead. Loud failure is better than silent corruption
    (two items with same id)."""
    root = _make_opf_root(extra_items='<item id="cover-image" href="old.jpg" media-type="image/jpeg"/>')
    with pytest.raises(ValueError, match="cover-image"):
        cover.add_cover_to_opf(root, "OEBPS/Content.opf")
