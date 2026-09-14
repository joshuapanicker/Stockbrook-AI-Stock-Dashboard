"""
SEC EDGAR filing fetcher — pulls Risk Factors (Item 1A) and MD&A
(Item 7 for 10-K / Item 2 for 10-Q) sections from a company's most recent
10-K and 10-Q, for RAG grounding in stock analysis.

Free, no API key. SEC does require a descriptive User-Agent identifying
the requester (SEC_EDGAR_CONTACT env var) per its fair-access policy —
set this to "YourApp yourreal@email.com", or requests get rate-limited
or blocked.

Section extraction is best-effort regex over the filing's plain text.
Filers format 10-Ks/10-Qs inconsistently, so a section that can't be
confidently isolated is skipped rather than indexed as garbage.
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import dataclass

import httpx
from lxml import html as lxml_html

from core.cache import fetch_through

_CONTACT = os.getenv("SEC_EDGAR_CONTACT", "Stockbrook research@example.com")
_HEADERS = {"User-Agent": _CONTACT}

_TICKER_MAP_TTL = 7 * 86_400   # SEC's ticker list barely changes
_SUBMISSIONS_TTL = 86_400      # recheck for new filings daily

_throttle_lock = threading.Lock()
_last_request = 0.0
_MIN_INTERVAL = 0.15  # stay well under SEC's ~10 req/s fair-use limit


def _throttled_get(url: str) -> httpx.Response:
    global _last_request
    with _throttle_lock:
        wait = _MIN_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
    resp = httpx.get(url, headers=_HEADERS, timeout=10.0)
    resp.raise_for_status()
    return resp


def _fetch_ticker_map() -> dict[str, str]:
    """symbol -> zero-padded 10-digit CIK"""
    resp = _throttled_get("https://www.sec.gov/files/company_tickers.json")
    data = resp.json()
    return {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in data.values()}


def get_cik(symbol: str) -> str | None:
    mapping = fetch_through("sec:ticker_map", _TICKER_MAP_TTL, _fetch_ticker_map)
    return mapping.get(symbol.strip().upper())


@dataclass
class Filing:
    form: str          # "10-K" or "10-Q"
    filing_date: str
    accession: str
    primary_doc: str
    cik: str

    @property
    def url(self) -> str:
        acc_no_dash = self.accession.replace("-", "")
        cik_int = str(int(self.cik))
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{self.primary_doc}"


def _fetch_submissions(cik: str) -> dict:
    resp = _throttled_get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    return resp.json()


def get_recent_filings(symbol: str, forms: tuple[str, ...] = ("10-K", "10-Q"),
                       limit: int = 1, as_of: str | None = None) -> list[Filing]:
    """Most recent filing of each requested form type for a ticker.

    `as_of` (an ISO date string) restricts this to the filing that was
    actually the most recent one on that date — for backtesting, so a
    historical analysis is never grounded in a filing that, on the date
    being simulated, hadn't been published yet. None (the default) keeps
    today's live behavior: the true most recent filing regardless of date.

    Only searches SEC's `recent` submissions window (last ~1000 filings) —
    plenty for a public company's quarterly cadence, but an `as_of` older
    than that window may find nothing rather than reaching into paginated
    filing history, which isn't fetched here.
    """
    cik = get_cik(symbol)
    if not cik:
        return []
    data = fetch_through(f"sec:submissions:{cik}", _SUBMISSIONS_TTL,
                         lambda: _fetch_submissions(cik))
    recent = data.get("filings", {}).get("recent", {})
    forms_list = recent.get("form", [])
    seen: set[str] = set()
    out: list[Filing] = []
    for i, form in enumerate(forms_list):
        if form not in forms or form in seen:
            continue
        filing_date = recent["filingDate"][i]
        if as_of is not None and filing_date > as_of:
            continue
        out.append(Filing(
            form=form,
            filing_date=filing_date,
            accession=recent["accessionNumber"][i],
            primary_doc=recent["primaryDocument"][i],
            cik=cik,
        ))
        seen.add(form)
        if len(seen) >= len(forms):
            break
    return out


def _strip_html(raw: str) -> str:
    try:
        tree = lxml_html.fromstring(raw)
        text = tree.text_content()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
    # SEC's iXBRL-tagged filings can leave numeric entities (&#160;, &#8217;)
    # un-decoded as literal text after tag-stripping, which breaks "Item 1A"
    # / "Risk Factors" adjacency in the section regexes below. Unescape
    # twice to also handle the occasional double-escaped entity.
    text = html.unescape(html.unescape(text))
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str) -> tuple[str, list[int]]:
    """Lowercased, whitespace-free copy of `text`, plus a map from each of
    its positions back to the corresponding index in the original.

    Item headers are matched against this rather than the raw text. Filers'
    HTML routinely leaves stray spaces *inside* words once tags are
    stripped — Microsoft's 10-K heading comes through as "ITEM 1A. RIS K
    FACTORS" (and "EXECUTIV E OFFICERS" elsewhere in the same document),
    because each run of letters is its own styled span. No amount of `\\s+`
    between tokens catches that; dropping whitespace entirely normalises
    every such variation at once, including non-breaking spaces and the
    line breaks that split headings across pages.
    """
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            chars.append(ch)
            idx.append(i)
    return "".join(chars).lower(), idx


# Written against the compacted text above, so no whitespace appears in any
# pattern. `[.:]?` because filers punctuate item numbers both ways
# ("Item 7." vs Shopify's "Item 7A:"), and `.{0,3}` absorbs whichever
# apostrophe variant survived entity decoding.
_MDA_TITLE = r"management.{0,3}sdiscussionandanalysis"

# End boundaries name the *whole* next heading, title included, rather than
# just its item number. The number alone matches the many places a filing
# merely refers to the next item — Microsoft's 10-Q MD&A opens by pointing
# at "Item 3 of this Form 10-Q", which under a bare `item3` pattern cut its
# entire MD&A down to one chunk.
# Filers vary the wording: AMD writes "Disclosure" singular, ServiceNow
# reverses it to "Qualitative and Quantitative" in the body while its own
# table of contents uses the standard order.
_MARKET_RISK = (r"(?:quantitativeandqualitative|qualitativeandquantitative)"
                r"disclosures?aboutmarketrisk")

# What separates an item number from its title. Albertsons uses a hyphen
# ("Item 1B - Unresolved Staff Comments"), others a period, a colon, an
# en/em dash, or nothing at all.
_SEP = r"[.:\-–—]?"

_SECTION_BOUNDARIES = {
    ("10-K", "risk_factors"): (r"item1a" + _SEP + r"riskfactors",
                               r"item1b" + _SEP + r"unresolvedstaffcomments"),
    ("10-K", "mda"):          (r"item7" + _SEP + _MDA_TITLE,
                               r"item7a" + _SEP + _MARKET_RISK),
    ("10-Q", "risk_factors"): (r"item1a" + _SEP + r"riskfactors",
                               r"item2" + _SEP + r"unregisteredsalesofequitysecurities"),
    ("10-Q", "mda"):          (r"item2" + _SEP + _MDA_TITLE,
                               r"item3" + _SEP + _MARKET_RISK),
}

# Last resort when the filer titles the next section in a way the patterns
# above don't anticipate. Matching the bare item number is imprecise — it is
# what the pre-2026-08 extractor did, and what a cross-reference fools — but
# it beats the alternative of giving up and truncating at _MAX_SECTION,
# which silently cut Albertsons' 148k-character MD&A to 23k. Guarded by the
# same table-of-contents check used to pick a section's start.
_END_FALLBACK = {
    ("10-K", "risk_factors"): r"item1b" + _SEP,
    ("10-K", "mda"):          r"item7a" + _SEP,
    ("10-Q", "risk_factors"): r"item2" + _SEP,
    ("10-Q", "mda"):          r"item3" + _SEP,
}

# Some filers head the section with its title alone, no "Item N" prefix
# (Shopify's 10-K). Only used when the prefixed form matches nothing, and
# only for MD&A: its full title is long and distinctive, whereas a bare
# "Risk Factors" occurs throughout ordinary prose and would match anywhere.
_SECTION_FALLBACK = {
    "mda": _MDA_TITLE + r"offinancialconditionandresultsofoperations",
}

_GENERIC_ITEM_PATTERN = re.compile(r"item\d+[a-c]?")
# Measured over compacted text, so this window is tight on purpose: in a
# contents listing the next heading follows within a few dozen characters
# (only a page number between them), while a real section body opens with
# prose. Widening it starts counting the ordinary cross-references that a
# genuine section makes in its first paragraph — at 600 it rejected
# Alphabet's real MD&A, whose opening sentence cites four other items.
_TOC_WINDOW = 200
_TOC_DENSITY_THRESHOLD = 3  # 3+ other "Item N" mentions nearby = a TOC listing
_MIN_SECTION = 2_000   # below this a boundary near a page number is a listing
_MAX_SECTION = 20_000  # cap when the next heading is never found


def _density(ctext: str, pos: int) -> int:
    """How many other item headings crowd the span after `pos`.

    A table-of-contents listing crams several into a small span; a real
    section body has at most an occasional cross-reference. Used to tell a
    heading in the TOC apart from the same heading over the real section.
    """
    return len(_GENERIC_ITEM_PATTERN.findall(ctext[pos:pos + _TOC_WINDOW]))


def _find_end(ctext: str, start: int, end_pat: str) -> int | None:
    """First end-boundary match that is a real section border.

    `end_pat` already requires the next section's full heading, so the only
    false border left is that heading appearing in a contents listing —
    Goldman's MD&A opens with a block listing "Item 7A Quantitative and
    Qualitative Disclosures About Market Risk 135", which taken as the
    boundary truncated the bank's entire MD&A to a single chunk.

    Two signals have to agree before a candidate is skipped, because either
    alone throws away real boundaries. A page number right after the
    heading is not enough: Datadog's genuine Item 2 heading is followed by
    "65" from the page furniture. Nor is a short section: a 10-Q whose risk
    factors say only "no material changes since our Form 10-K" is
    legitimately a few hundred characters, and skipping its real boundary
    would run the section on through the rest of the filing. Together they
    are specific — a contents listing appears at the *top* of the section
    it belongs to, so it both carries a page number and leaves nothing
    behind it.
    """
    for m in re.finditer(end_pat, ctext[start:]):
        if m.start() == 0:
            continue  # the section's own heading
        page_number = ctext[start + m.end():start + m.end() + 1].isdigit()
        if page_number and m.start() < _MIN_SECTION:
            continue
        return start + m.start()
    return None


def _find_end_loose(ctext: str, start: int, bare_pat: str) -> int | None:
    """Bare item-number boundary, for filers whose heading wording the
    titled patterns don't anticipate.

    The number alone also matches every passing mention of the next item,
    so candidates sitting in a contents listing are skipped by the same
    density test used to choose a section's start. Imprecise, but the
    alternative when nothing matches is truncating at _MAX_SECTION, which
    is worse: it cut Albertsons' 148k-character MD&A down to 23k.
    """
    for m in re.finditer(bare_pat, ctext[start:]):
        if m.start() == 0:
            continue
        if _density(ctext, start + m.end()) < _TOC_DENSITY_THRESHOLD:
            return start + m.start()
    return None


def extract_section(text: str, form: str, section: str) -> str | None:
    key = (form, section)
    if key not in _SECTION_BOUNDARIES:
        return None
    start_pat, end_pat = _SECTION_BOUNDARIES[key]
    ctext, cmap = _compact(text)

    starts = list(re.finditer(start_pat, ctext))
    if not starts:
        fallback = _SECTION_FALLBACK.get(section)
        if fallback:
            starts = list(re.finditer(fallback, ctext))
    if not starts:
        return None

    # Try candidates least-TOC-like first, falling through to the next one
    # if this heading yields nothing usable.
    #
    # Known imperfection: where a filer punctuates cross-references exactly
    # as it punctuates headings ("consider Item 1A—Risk Factors in the Form
    # 10-K", American Water), a reference in prose can outrank the real
    # heading, since prose mentions few other items. Rejecting candidates
    # that follow a lower-case letter looks like the fix and is not — real
    # headings sit directly after the "Table of Contents" running header,
    # so that test throws away more than it catches. The result is a
    # superset of the section rather than the wrong section, so it is left
    # alone until there is a discriminator that survives measurement.
    for m in sorted(starts, key=lambda m: _density(ctext, m.end())):
        if _density(ctext, m.end()) >= _TOC_DENSITY_THRESHOLD:
            continue  # a contents listing, not the section itself
        start = m.start()
        end = _find_end(ctext, start, end_pat)
        if end is None:
            end = _find_end_loose(ctext, start, _END_FALLBACK[key])
        if end is None:
            end = min(start + _MAX_SECTION, len(ctext))
        if end - start < 200:
            continue
        section_text = text[cmap[start]:cmap[end - 1] + 1].strip()
        if len(section_text) > 200:
            return section_text
    return None


def fetch_filing_sections(symbol: str) -> list[dict]:
    """
    Returns a list of {form, date, section, text} — one entry per
    successfully-extracted section from the ticker's most recent 10-K/10-Q.
    Best-effort: skips whatever it can't cleanly parse rather than failing.
    """
    results: list[dict] = []
    for filing in get_recent_filings(symbol):
        try:
            resp = _throttled_get(filing.url)
        except Exception:
            continue
        text = _strip_html(resp.text)
        for section in ("risk_factors", "mda"):
            extracted = extract_section(text, filing.form, section)
            if extracted:
                results.append({
                    "form": filing.form,
                    "date": filing.filing_date,
                    "section": section,
                    "text": extracted,
                })
    return results
