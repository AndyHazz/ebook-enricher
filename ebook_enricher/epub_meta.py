"""EPUB metadata reader/writer.

Uses zipfile + ElementTree directly rather than ebooklib because:
- We only touch the OPF file, which is plain XML inside a zip.
- ebooklib has a heavy dependency on lxml and sometimes mangles complex
  EPUBs during a round-trip write. Manipulating the OPF directly is
  more surgical and preserves everything else in the archive.
- Keeping deps minimal suits the Pi deployment.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# Register namespaces for output
ET.register_namespace("", NS["opf"])
ET.register_namespace("dc", NS["dc"])


@dataclass
class EpubMeta:
    title: str
    author: str
    series: Optional[str] = None
    series_index: Optional[str] = None
    description: Optional[str] = None
    subjects: list[str] = field(default_factory=list)


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    """Parse META-INF/container.xml to locate the OPF path."""
    data = zf.read("META-INF/container.xml")
    root = ET.fromstring(data)
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = root.find("c:rootfiles/c:rootfile", ns)
    if rootfile is None:
        raise ValueError("EPUB container.xml has no rootfile")
    return rootfile.attrib["full-path"]


def _parse_opf(opf_bytes: bytes) -> ET.Element:
    return ET.fromstring(opf_bytes)


def _text(el: Optional[ET.Element]) -> Optional[str]:
    return el.text.strip() if el is not None and el.text else None


def read_meta(path: Path) -> EpubMeta:
    with zipfile.ZipFile(path) as zf:
        opf_path = _find_opf_path(zf)
        root = _parse_opf(zf.read(opf_path))
    metadata = root.find("opf:metadata", NS)
    if metadata is None:
        raise ValueError(f"No metadata block in {path}")

    title = _text(metadata.find("dc:title", NS)) or ""
    author = _text(metadata.find("dc:creator", NS)) or ""
    description = _text(metadata.find("dc:description", NS))
    subjects = [
        s.text.strip() for s in metadata.findall("dc:subject", NS)
        if s.text and s.text.strip()
    ]

    series = None
    series_index = None
    for meta in metadata.findall("opf:meta", NS):
        name = meta.attrib.get("name")
        prop = meta.attrib.get("property")
        # EPUB 2 style: <meta name="calibre:series" content="X"/>
        # EPUB 3 style: <meta property="calibre:series">X</meta>
        content = meta.attrib.get("content") or (meta.text.strip() if meta.text else None)
        if (name == "calibre:series" or prop == "calibre:series") and content:
            series = content
        elif (name == "calibre:series_index" or prop == "calibre:series_index") and content:
            series_index = content

    return EpubMeta(
        title=title,
        author=author,
        series=series,
        series_index=series_index,
        description=description,
        subjects=subjects,
    )


def _set_or_add_meta(metadata: ET.Element, name: str, content: str) -> None:
    """Replace any existing calibre-style meta element, or add a new one.

    Handles both EPUB 2 (<meta name="X" content="Y"/>) and EPUB 3
    (<meta property="X">Y</meta>) forms. An existing element of either
    form is updated in-place; only when neither is present do we add
    a new EPUB 2-style element.
    """
    for meta in metadata.findall("opf:meta", NS):
        if meta.attrib.get("name") == name:
            meta.attrib["content"] = content
            return
        if meta.attrib.get("property") == name:
            meta.text = content
            # Clean up any stale content attribute from mixed-style edits
            meta.attrib.pop("content", None)
            return
    meta = ET.SubElement(metadata, f"{{{NS['opf']}}}meta")
    meta.attrib["name"] = name
    meta.attrib["content"] = content


def _set_or_add_dc(metadata: ET.Element, tag: str, text: str) -> None:
    """Replace existing dc:X element's text, or add a new one."""
    existing = metadata.find(f"dc:{tag}", NS)
    if existing is not None:
        existing.text = text
        return
    el = ET.SubElement(metadata, f"{{{NS['dc']}}}{tag}")
    el.text = text


def write_meta(
    path: Path,
    meta: EpubMeta,
    cover_override: Optional[tuple[str, bytes]] = None,
) -> None:
    """Write series, series_index, description, and subjects into the EPUB.

    Title and author are NEVER overwritten — the values on `meta` for
    those fields are ignored. Only the enrichment-owned fields are
    updated.

    If `cover_override=(zip_path, bytes)` is provided, the zip member at
    that path is also replaced with the given bytes during the same
    single-pass rewrite — keeping the operation atomic.
    """
    with zipfile.ZipFile(path) as zf:
        opf_path = _find_opf_path(zf)
        root = _parse_opf(zf.read(opf_path))

    metadata = root.find("opf:metadata", NS)
    if metadata is None:
        raise ValueError(f"No metadata block in {path}")

    if meta.series:
        _set_or_add_meta(metadata, "calibre:series", meta.series)
    if meta.series_index:
        _set_or_add_meta(metadata, "calibre:series_index", meta.series_index)
    if meta.description:
        _set_or_add_dc(metadata, "description", meta.description)
    if meta.subjects:
        # Remove existing subjects, then add new ones
        for s in metadata.findall("dc:subject", NS):
            metadata.remove(s)
        for subject in meta.subjects:
            el = ET.SubElement(metadata, f"{{{NS['dc']}}}subject")
            el.text = subject

    new_opf_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    # Rewrite the zip with the modified OPF and the rest copied verbatim.
    # Capture original ownership+mode before writing so we can restore them
    # after the rename — tempfile.mkstemp defaults to 0600/root:root, which
    # would lock out Syncthing and other consumers from reading the file.
    orig_stat = path.stat()
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".epub", dir=path.parent)
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(path) as src, \
             zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                if item.filename == opf_path:
                    dst.writestr(item, new_opf_bytes)
                elif cover_override is not None and item.filename == cover_override[0]:
                    dst.writestr(item, cover_override[1])
                elif item.filename == "mimetype":
                    # mimetype must be stored uncompressed
                    dst.writestr(item, src.read(item.filename),
                                 compress_type=zipfile.ZIP_STORED)
                else:
                    dst.writestr(item, src.read(item.filename))
        # Restore permissions and ownership before the atomic rename.
        os.chmod(tmp_path, orig_stat.st_mode & 0o7777)
        try:
            os.chown(tmp_path, orig_stat.st_uid, orig_stat.st_gid)
        except PermissionError:
            # Non-root caller can't chown; mode alone is usually enough
            # because the file is created by the same user that owns path.
            pass
        shutil.move(tmp_path, path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
