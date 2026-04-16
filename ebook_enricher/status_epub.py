"""Writes a minimal EPUB into the ebooks folder to surface service-level
problems to the Kindle reader.

The filename is prefixed with an underscore so it sorts to the top of
KOReader's library listing. Syncthing picks up the file and syncs it to
the Kindle; the reader sees it as a book titled like the problem.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

STATUS_FILENAME = "_ebook-enricher-status.epub"

MIMETYPE = "application/epub+zip"

CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _opf(title: str) -> str:
    from html import escape
    safe_title = escape(title)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">ebook-enricher-status</dc:identifier>
    <dc:title>{safe_title}</dc:title>
    <dc:creator>ebook-enricher</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chap" href="status.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chap"/>
  </spine>
</package>
"""


NAV_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Nav</title></head>
<body>
  <nav epub:type="toc"><ol><li><a href="status.xhtml">Status</a></li></ol></nav>
</body>
</html>
"""


def _chapter(title: str, body: str) -> str:
    from html import escape
    safe_title = escape(title)
    # Body is allowed to contain newlines; we turn them into paragraphs.
    paragraphs = "\n".join(
        f"  <p>{escape(line, quote=False)}</p>" for line in body.split("\n") if line.strip()
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{safe_title}</title></head>
<body>
<h1>{safe_title}</h1>
{paragraphs}
</body>
</html>
"""


def write_status_epub(ebooks_path: Path, title: str, body: str) -> Path:
    """Write (or overwrite) the status EPUB with the given title and body.

    Returns the path written. Overwriting an existing file is intentional:
    Syncthing treats the new content as an update to the same book.
    """
    target = ebooks_path / STATUS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            zipfile.ZipInfo("mimetype"),
            MIMETYPE,
            compress_type=zipfile.ZIP_STORED,
        )
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("OEBPS/content.opf", _opf(title))
        z.writestr("OEBPS/nav.xhtml", NAV_XHTML)
        z.writestr("OEBPS/status.xhtml", _chapter(title, body))
    return target


def clear_status_epub(ebooks_path: Path) -> bool:
    """Remove the status EPUB if it exists. Returns True if a file was removed."""
    target = ebooks_path / STATUS_FILENAME
    if target.exists():
        target.unlink()
        return True
    return False
