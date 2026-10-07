"""
In-PDF search helpers (PyMuPDF).

Finds which pages of a PDF contain a set of search terms and can produce a copy
of the PDF with those terms highlighted, so the viewer can jump to the exact
page and show the words in place.

This only works on pages that have a text layer. Pages that are pure images
(no embedded/OCR text layer) cannot be matched here; callers fall back to the
extracted-text view for those.
"""

import re
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

MAX_TERMS = 8
MAX_TERM_LENGTH = 100
MIN_TERM_LENGTH = 2
MAX_PAGES = 1500                  # don't scan absurdly long PDFs
MAX_HIGHLIGHTS_PER_PAGE = 300     # keep annotation count (and output size) bounded

Term = Tuple[str, bool]  # (text, is_prefix)


def parse_terms(raw_terms: List[str]) -> List[Term]:
    """Normalise raw `t=` query values into (text, is_prefix) terms.

    A trailing `*` marks a prefix term (matches the rest of the word).
    Over-long/short terms are dropped and duplicates removed.
    """
    seen = set()
    terms: List[Term] = []
    for raw in raw_terms[:MAX_TERMS * 2]:
        raw = (raw or "").strip()[:MAX_TERM_LENGTH + 1]
        prefix = raw.endswith("*")
        text = re.sub(r"\s+", " ", raw.strip("*").strip())
        if len(text) < MIN_TERM_LENGTH or len(text) > MAX_TERM_LENGTH:
            continue
        key = (text.lower(), prefix)
        if key in seen:
            continue
        seen.add(key)
        terms.append((text, prefix))
        if len(terms) >= MAX_TERMS:
            break
    return terms


def _build_regex(terms: List[Term]) -> Optional["re.Pattern"]:
    parts = []
    for text, prefix in sorted(terms, key=lambda t: -len(t[0])):
        src = re.escape(text).replace(r"\ ", r"\s+")
        if prefix:
            src += r"\w*"
        parts.append(src)
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def find_pages(pdf_path: str, terms: List[Term]) -> Dict:
    """Return which pages contain the terms.

    {
      "page_count": int,          # real number of pages in the PDF
      "has_text_layer": bool,     # at least one page has searchable text
      "pages": [{"page": 4, "hits": 2}, ...]   # 1-based, ascending
    }
    """
    regex = _build_regex(terms)
    result = {"page_count": 0, "has_text_layer": False, "pages": []}
    if regex is None:
        return result

    with fitz.open(pdf_path) as doc:
        result["page_count"] = doc.page_count
        for index in range(min(doc.page_count, MAX_PAGES)):
            text = doc[index].get_text("text")
            if not text.strip():
                continue
            result["has_text_layer"] = True
            hits = len(regex.findall(re.sub(r"\s+", " ", text)))
            if hits:
                result["pages"].append({"page": index + 1, "hits": hits})
    return result


def highlight_pdf(pdf_path: str, terms: List[Term]) -> Optional[bytes]:
    """Return the PDF with matching words highlighted, or None if nothing matched."""
    if not terms:
        return None

    highlighted = False
    with fitz.open(pdf_path) as doc:
        for index in range(min(doc.page_count, MAX_PAGES)):
            page = doc[index]
            if not page.get_text("text").strip():
                continue
            count = 0
            for text, prefix in terms:
                # search_for is case-insensitive and spans line breaks for phrases
                rects = page.search_for(text, quads=False)
                if prefix:
                    rects = _extend_prefix_rects(page, text, rects)
                for rect in rects:
                    if count >= MAX_HIGHLIGHTS_PER_PAGE:
                        break
                    annot = page.add_highlight_annot(rect)
                    annot.set_colors(stroke=(1, 0.85, 0))
                    annot.update()
                    count += 1
                    highlighted = True
        if not highlighted:
            return None
        return doc.tobytes(garbage=1, deflate=True)


def _extend_prefix_rects(page, text: str, rects):
    """Widen prefix-term rects (`trav*`) to cover the whole word, not just the prefix."""
    if not rects:
        return rects
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    prefix = text.lower()
    covered = []
    for rect in rects:
        union = fitz.Rect(rect)
        for x0, y0, x1, y1, word, *_ in words:
            w = fitz.Rect(x0, y0, x1, y1)
            if w.intersects(rect) and word.lower().startswith(prefix.split(" ")[0]):
                union |= w
        covered.append(union)
    return covered
