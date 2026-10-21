"""Unit tests for atlas.ingest.pdf on a tiny PDF written with PyMuPDF (no fixture corpus needed)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import fitz
import pytest

from atlas.core.schemas import DocMeta, PageText
from atlas.ingest import pdf as pdfmod

TITLE = "A Large Title Line For Testing"
LINES_P1 = [
    (TITLE, 16.0),
    ("Alice Author, Bob Author and Carol Author", 10.0),
    ("DOI: 10.1234/jeb.2019.0012.", 9.0),
    ("Primary stability of a cementless stem depends on micro-", 10.0),
    ("motion at the interface. The threshold was 150 um.", 10.0),
    ("Fig. 3 shows the distribution along the stem. It was large.", 10.0),
]
LINES_P2 = [("Second page text about stems.", 10.0), ("Nothing else here.", 10.0)]


@pytest.fixture(scope="module")
def small_pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "small.pdf"
    doc = fitz.open()
    for lines in (LINES_P1, LINES_P2):
        page = doc.new_page(width=595, height=842)
        y = 80.0
        for text, size in lines:
            page.insert_text((50, y), text, fontsize=size, fontname="helv")
            y += 18.0
    doc.set_metadata({"title": "Info Dict Title", "author": "Info Author", "keywords": "k1, k2",
                      "subject": "subj", "creationDate": "D:20190503120000"})
    doc.save(str(path))
    doc.close()
    return path


def test_extractor_tag():
    tag = pdfmod.extractor_tag()
    assert tag.startswith("pymupdf:") and tag == f"pymupdf:{fitz.VersionBind}"


def test_sha256_file(small_pdf: Path):
    assert pdfmod.sha256_file(small_pdf) == hashlib.sha256(small_pdf.read_bytes()).hexdigest()


def test_extract_pages(small_pdf: Path):
    pages = pdfmod.extract_pages(small_pdf)
    assert [p.page_no for p in pages] == [1, 2]
    assert all(isinstance(p, PageText) for p in pages)
    assert pages[0].width == pytest.approx(595.0) and pages[0].height == pytest.approx(842.0)
    t = pages[0].text
    assert TITLE + "\n" in t and "micro-\nmotion at the interface." in t
    assert "\r" not in t and "\x00" not in t
    assert "Second page text about stems." in pages[1].text
    assert pages[0].text == fitz.open(str(small_pdf))[0].get_text("text").replace("\r", "")


def test_text_ratio(small_pdf: Path):
    pages = pdfmod.extract_pages(small_pdf)
    r = pdfmod.text_ratio(pages)
    assert 0.5 < r < 1.0
    assert pdfmod.text_ratio([]) == 0.0
    assert pdfmod.text_ratio([PageText(1, "")]) == 0.0
    assert pdfmod.text_ratio([PageText(1, "ab12")]) == pytest.approx(0.5)


def test_pdf_metadata(small_pdf: Path):
    meta = pdfmod.pdf_metadata(small_pdf)
    assert isinstance(meta, DocMeta)
    assert meta.title == "Info Dict Title"
    assert meta.authors == "Info Author"
    assert meta.keywords == "k1, k2"
    assert meta.extra["title_guess"] == TITLE
    assert meta.extra["doi_guess"] == "10.1234/jeb.2019.0012"      # trailing period stripped
    assert meta.extra["pages"] == 2
    assert meta.extra["subject"] == "subj"
    assert "error" not in meta.extra


def test_pdf_metadata_broken_file(tmp_path: Path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    meta = pdfmod.pdf_metadata(bad)
    assert meta.title is None and meta.doi is None
    assert "error" in meta.extra and meta.extra["error"]
    meta2 = pdfmod.pdf_metadata(tmp_path / "missing.pdf")
    assert "error" in meta2.extra


def _is_png(b: bytes) -> bool:
    return b[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_page_plain(small_pdf: Path):
    png, info = pdfmod.render_page(small_pdf, 1, zoom=1.0)
    assert _is_png(png)
    assert info == {"page": 1, "pages": 2, "hits": 0, "hit_y": None}
    big, _ = pdfmod.render_page(small_pdf, 1, zoom=2.0)
    assert len(big) > len(png)
    # page number is clamped into range
    _, info = pdfmod.render_page(small_pdf, 99)
    assert info["page"] == 2 and info["pages"] == 2
    _, info = pdfmod.render_page(small_pdf, 0)
    assert info["page"] == 1


def test_render_page_highlight_phrase(small_pdf: Path):
    _, info = pdfmod.render_page(small_pdf, 1, highlight="Fig. 3 shows the distribution")
    assert info["hits"] >= 1 and 0.0 < info["hit_y"] < 1.0
    # hyphen at a line end: the phrase as it is stored (with the newline) is found via the whitespace-collapsed
    # 'micro- motion' variant, so the two line fragments are highlighted
    _, info = pdfmod.render_page(small_pdf, 1, highlight="depends on micro-\nmotion at the interface")
    assert info["hits"] >= 2
    # fallback to the longest word (>= 3 chars) when the phrase itself is not on the page
    _, info = pdfmod.render_page(small_pdf, 1, highlight="qqq threshold zzz")
    assert info["hits"] >= 1
    _, info = pdfmod.render_page(small_pdf, 1, highlight="nothing of this is there")
    assert info["hits"] == 0 and info["hit_y"] is None
    _, info = pdfmod.render_page(small_pdf, 1, highlight="   ")
    assert info["hits"] == 0


def test_render_page_spans(small_pdf: Path):
    pages = pdfmod.extract_pages(small_pdf)
    text = pages[0].text
    a = text.index("The threshold was 150 um.")
    b = a + len("The threshold was 150 um.")
    _, info = pdfmod.render_page(small_pdf, 1, spans=[(a, b)])
    assert info["hits"] == 1 and 0.0 < info["hit_y"] < 1.0
    # a span across a line break yields one rect per line
    a2 = text.index("depends on micro-")
    b2 = text.index("interface.") + len("interface.")
    _, info = pdfmod.render_page(small_pdf, 1, spans=[(a2, b2)])
    assert info["hits"] == 2
    # two spans → two hits; the first hit's relative y is the smaller one
    _, info = pdfmod.render_page(small_pdf, 1, spans=[(a, b), (0, len(TITLE))])
    assert info["hits"] == 2
    _, info_title = pdfmod.render_page(small_pdf, 1, spans=[(0, len(TITLE))])
    assert info["hit_y"] == info_title["hit_y"]
    # degenerate / out-of-range spans are ignored
    _, info = pdfmod.render_page(small_pdf, 1, spans=[(5, 5), (10 ** 6, 10 ** 6 + 5)])
    assert info["hits"] == 0
    # spans take precedence over the phrase; the phrase is only a fallback when spans yield nothing
    _, info = pdfmod.render_page(small_pdf, 1, spans=[(5, 5)], highlight="Second page")
    assert info["hits"] == 0
    _, info = pdfmod.render_page(small_pdf, 2, spans=[(5, 5)], highlight="Second page")
    assert info["hits"] == 1


def test_char_map_reproduces_text(small_pdf: Path):
    with fitz.open(str(small_pdf)) as doc:
        page = doc[0]
        text = page.get_text("text").replace("\r", "")
        cmap = pdfmod._char_map(page, text)
        assert cmap is not None
        assert "".join(c for c, _ in cmap) == text
        assert pdfmod._char_map(page, text + "x") is None
