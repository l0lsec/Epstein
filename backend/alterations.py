"""Judge how a DOJ re-issue changed an EFTA document.

Input is the extracted text of an older and a newer version of the same file.

A line-level unified diff over-counts when a PDF is re-extracted with different
line breaks, says nothing about *what* was lost, and scores explicit redaction
marks added over unchanged text as zero. This module instead compares bags of
normalised words (immune to reflow, case, hyphenation and page furniture), pairs
off OCR spelling variants, counts redaction marks and identifiers (emails,
phones, SSNs), and derives a conservative change kind plus a 0-100 review
priority.

The one rule that shapes every threshold: a wrongly *trivial* verdict hides a
real alteration from review, which is far worse than a wrongly *substantive*
one that costs an admin a click. A document is only called trivial on positive
evidence that nothing substantive left it.

Run `python backend/alterations.py` to re-score the stored review queue
(dry run unless --apply).
"""

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set

# Kinds that carry no evidence of suppressed content. Auto-assigned 'trivial'.
TRIVIAL_KINDS = frozenset({"identical", "cosmetic", "additive", "no_text"})
# Kinds an admin should look at.
SUBSTANTIVE_KINDS = frozenset({"redaction", "removal", "replaced"})
ALL_KINDS = tuple(sorted(TRIVIAL_KINDS | SUBSTANTIVE_KINDS))

_STOPWORDS = frozenset(
    "a an and are as at be but by for from had has have he her his i in is it its "
    "me my not of on or our she so that the their them there they this to was we "
    "were what when which who will with you your".split()
)
# Words that appear on every page footer ("Page 3 of 10"); dropping a page must
# not count as dropped content (pages_removed covers that).
_FURNITURE_WORDS = frozenset({"page", "pages"})
_FURNITURE_TOKEN_RE = re.compile(r"^efta\d{6,}$")  # Bates stamp repeated per page

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

# Explicit redaction marks. Counted separately and masked out before tokenising
# so a "[REDACTED]" stamp is never mistaken for added content.
_MARKER_RE = re.compile(
    r"""
      \[[^\]\n]{0,40}\b(?:redact\w*|withheld|sealed)\b[^\]\n]{0,40}\]   # [REDACTED - PII]
    | \b(?:redacted|redaction|withheld)\b
    | [▀-▟■⬛]{2,}                                  # █████ block runs
    | \b[xX]{4,}\b                                                      # XXXXXX
    | \(b\)\s*\(\d\)(?:\s*\([a-fA-F]\))?                                 # FOIA exemption (b)(6) / (b)(7)(C)
    """,
    re.VERBOSE | re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_CAPITALISED_RE = re.compile(r"\b(?:[A-Z][a-z]{2,}|[A-Z]{3,})\b")
_LOWERCASE_RE = re.compile(r"\b[a-z]{3,}\b")

# OCR variant pairing ("Epsteln" vs "Epstein"): cheap, bounded, alphabetic only.
_OCR_MIN_LEN = 5
_OCR_MIN_RATIO = 0.8
_MAX_FUZZY_COMPARISONS = 200_000

_MAX_REPORTED_TERMS = 30


@dataclass
class Analysis:
    kind: str = "no_text"
    severity: int = 0                # 0 for trivial kinds, else 1-100 review priority
    words_removed: int = 0
    words_added: int = 0
    significant_removed: int = 0     # removed words that are not stopwords/noise/page numbers
    significant_added: int = 0
    lines_removed: int = 0           # old lines that lost >=1 significant word
    lines_added: int = 0             # new lines that gained >=1 significant word
    chars_removed: int = 0
    redactions_added: int = 0        # explicit redaction marks present only in the newer version
    redactions_removed: int = 0
    names_removed: int = 0           # distinct proper-noun-like words that disappeared
    identifiers_removed: int = 0     # emails / phone numbers / SSNs that disappeared
    pages_removed: int = 0
    ocr_variants: int = 0            # removed/added word pairs cancelled as OCR spelling noise
    similarity: float = 1.0
    has_text: bool = False
    removed_terms: List[str] = field(default_factory=list)

    @property
    def is_trivial(self) -> bool:
        return self.kind in TRIVIAL_KINDS

    @property
    def suggested_status(self) -> str:
        return "trivial" if self.is_trivial else "pending"

    def to_dict(self, include_terms: bool = True) -> Dict[str, Any]:
        d = {
            "kind": self.kind,
            "severity": self.severity,
            "words_removed": self.words_removed,
            "words_added": self.words_added,
            "significant_removed": self.significant_removed,
            "significant_added": self.significant_added,
            "lines_removed": self.lines_removed,
            "lines_added": self.lines_added,
            "chars_removed": self.chars_removed,
            "redactions_added": self.redactions_added,
            "redactions_removed": self.redactions_removed,
            "names_removed": self.names_removed,
            "identifiers_removed": self.identifiers_removed,
            "pages_removed": self.pages_removed,
            "ocr_variants": self.ocr_variants,
            "similarity": round(self.similarity, 4),
            "has_text": self.has_text,
            "suggested_status": self.suggested_status,
        }
        if include_terms:
            d["removed_terms"] = list(self.removed_terms)
        return d


def _clean(text: Optional[str]) -> str:
    return unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH)


def _mask_markers(text: str):
    """Return (text with redaction marks blanked, number of marks)."""
    return _MARKER_RE.subn(" ", text)


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text.casefold()) if not _FURNITURE_TOKEN_RE.match(t)]


def _looks_like_noise(tok: str) -> bool:
    # Vowel-less runs ("xjfq") are overwhelmingly OCR garbage, not words.
    return len(tok) >= 4 and tok.isalpha() and not any(c in "aeiouy" for c in tok)


def _is_significant(tok: str) -> bool:
    if tok in _STOPWORDS or tok in _FURNITURE_WORDS:
        return False
    if tok.isdigit():
        return len(tok) >= 3  # years, phone/ID fragments matter; "3" of "page 3" does not
    return len(tok) >= 3 and not _looks_like_noise(tok)


def _proper_nouns(text: str) -> Set[str]:
    """Words only ever seen capitalised in `text` — a cheap stand-in for names."""
    capitalised = {m.casefold() for m in _CAPITALISED_RE.findall(text)}
    return capitalised - set(_LOWERCASE_RE.findall(text)) - _STOPWORDS


def _identifiers(text: str) -> Set[str]:
    ids = {m.casefold() for m in _EMAIL_RE.findall(text)}
    ids |= {"".join(m) for m in _PHONE_RE.findall(text)}
    ids |= {m.replace("-", "") for m in _SSN_RE.findall(text)}
    return ids


def _pairable(tok: str) -> bool:
    return tok.isalpha() and len(tok) >= _OCR_MIN_LEN


def _pair_ocr_variants(removed: Counter, added: Counter) -> int:
    """Cancel removed/added words that are near-identical spellings (re-OCR noise).
    Numbers never pair: 1234 -> 1235 is a real change. Mutates both counters and
    returns the number of pairs cancelled. Bounded so pathological inputs stay fast."""
    by_len: Dict[int, List[str]] = defaultdict(list)
    for a in added:
        if added[a] > 0 and _pairable(a):
            by_len[len(a)].append(a)
    if not by_len:
        return 0
    budget = _MAX_FUZZY_COMPARISONS
    paired = 0
    for r in sorted(t for t in removed if removed[t] > 0 and _pairable(t)):
        best, best_ratio = None, _OCR_MIN_RATIO
        for length in (len(r) - 1, len(r), len(r) + 1):
            for a in by_len.get(length, ()):
                if added[a] <= 0 or a == r:
                    continue
                budget -= 1
                sm = SequenceMatcher(None, r, a)
                if sm.real_quick_ratio() < best_ratio or sm.quick_ratio() < best_ratio:
                    continue
                ratio = sm.ratio()
                if ratio >= best_ratio:
                    best, best_ratio = a, ratio
        if best is not None:
            n = min(removed[r], added[best])
            removed[r] -= n
            added[best] -= n
            paired += n
        if budget <= 0:
            break
    return paired


def _lines_touched(text: str, leftover: Counter) -> int:
    """Count lines that contain >=1 significant word from `leftover` (removed or
    added words). Words are consumed in order so each leftover occurrence is
    credited to one line; counting words rather than lines keeps this immune to
    reflow."""
    budget = Counter({t: n for t, n in leftover.items() if n > 0 and _is_significant(t)})
    if not budget:
        return 0
    touched = 0
    for line in text.splitlines():
        hit = False
        for tok in _tokens(line):
            if budget.get(tok, 0) > 0:
                budget[tok] -= 1
                hit = True
        if hit:
            touched += 1
    return touched


def _severity(a: Analysis) -> int:
    if a.is_trivial:
        return 0
    score = min(55.0, 14 * math.log2(1 + a.significant_removed))
    score += min(20, 5 * a.names_removed)
    score += min(15, 5 * a.identifiers_removed)
    if a.redactions_added:
        score += 25 + min(10, 2 * a.redactions_added)
    if a.pages_removed:
        score += min(15, 5 * a.pages_removed)
    # A surgical edit to an otherwise intact document is the signature of a
    # redaction; a wholesale rewrite is usually a re-scan or a format swap.
    if a.similarity >= 0.9:
        score *= 1.1
    elif a.similarity < 0.5:
        score *= 0.7
    return int(max(1, min(100, round(score))))


def _classify(a: Analysis, old_has_tokens: bool, new_has_tokens: bool) -> str:
    if not (old_has_tokens or new_has_tokens) and not (a.redactions_added or a.redactions_removed):
        return "no_text"
    if a.redactions_added > 0:
        return "redaction"
    if a.significant_removed == 0 and a.pages_removed == 0:
        if a.words_removed == 0 and a.words_added == 0 and a.ocr_variants == 0:
            return "identical"
        if a.significant_added > 0 and a.identifiers_removed == 0:
            return "additive"
        if a.identifiers_removed == 0:
            return "cosmetic"
    # Something substantive left the document (or an identifier did).
    if a.significant_added > 0.5 * max(a.significant_removed, 1):
        return "replaced"
    return "removal"


def analyze_versions(old_text: Optional[str], new_text: Optional[str],
                     old_pages: Optional[int] = None,
                     new_pages: Optional[int] = None) -> Analysis:
    """Compare an older and a newer extraction of the same document."""
    old_raw, new_raw = _clean(old_text), _clean(new_text)
    old_masked, old_marks = _mask_markers(old_raw)
    new_masked, new_marks = _mask_markers(new_raw)

    old_tokens, new_tokens = _tokens(old_masked), _tokens(new_masked)
    old_counts, new_counts = Counter(old_tokens), Counter(new_tokens)
    removed, added = old_counts - new_counts, new_counts - old_counts

    a = Analysis(has_text=bool(old_tokens or new_tokens or old_marks or new_marks))
    a.ocr_variants = _pair_ocr_variants(removed, added)
    removed, added = +removed, +added  # drop zeroed entries

    a.words_removed = sum(removed.values())
    a.words_added = sum(added.values())
    a.significant_removed = sum(n for t, n in removed.items() if _is_significant(t))
    a.significant_added = sum(n for t, n in added.items() if _is_significant(t))
    a.chars_removed = sum(len(t) * n for t, n in removed.items())
    a.lines_removed = _lines_touched(old_masked, removed)
    a.lines_added = _lines_touched(new_masked, added)
    a.redactions_added = max(0, new_marks - old_marks)
    a.redactions_removed = max(0, old_marks - new_marks)
    if old_pages is not None and new_pages is not None:
        a.pages_removed = max(0, old_pages - new_pages)

    # Emails are lowercase by convention; left in, they'd disqualify "Smith" from being a name.
    proper = _proper_nouns(_EMAIL_RE.sub(" ", old_masked))
    a.names_removed = sum(1 for t in removed if t in proper and _is_significant(t))
    a.identifiers_removed = len(_identifiers(old_raw) - _identifiers(new_raw))

    total = len(old_tokens) + len(new_tokens)
    a.similarity = 1.0 if total == 0 else max(0.0, 1.0 - (a.words_removed + a.words_added) / total)

    # Proper nouns first, then by how often they vanished — the likeliest names.
    ranked = sorted((t for t in removed if _is_significant(t)),
                    key=lambda t: (t not in proper, -removed[t], t))
    a.removed_terms = ranked[:_MAX_REPORTED_TERMS]

    a.kind = _classify(a, bool(old_tokens), bool(new_tokens))
    a.severity = _severity(a)
    return a


# --------------------------------------------------------------------------- #
# Re-scoring the stored review queue
# --------------------------------------------------------------------------- #

def _main() -> None:
    import argparse
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from database import Database

    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description="Re-score document_alterations with the reflow-tolerant analysis. "
                    "Dry run unless --apply.")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(here), "epstein.db"))
    parser.add_argument("--apply", action="store_true", help="write results (default: report only)")
    parser.add_argument("--limit", type=int, default=None, help="only analyse the first N rows")
    args = parser.parse_args()

    def progress(done: int, total: int) -> None:
        print(f"\r  analysed {done:,}/{total:,}", end="", flush=True)

    db = Database(args.db)
    report = db.analyze_alterations(apply=args.apply, limit=args.limit, progress=progress)
    print()
    print(f"{'APPLIED' if args.apply else 'DRY RUN'}: {report['analyzed']:,} analysed, "
          f"{report['skipped']:,} skipped (missing version/text)")
    print("by kind:")
    for kind in ALL_KINDS:
        print(f"  {kind:<10} {report['by_kind'].get(kind, 0):>8,}")
    print(f"status moves: {report['to_trivial']:,} pending→trivial, "
          f"{report['to_pending']:,} trivial→pending "
          f"({report['left_alone']:,} admin-reviewed rows left as-is)")
    if not args.apply:
        print("re-run with --apply to write.")


if __name__ == "__main__":
    _main()
