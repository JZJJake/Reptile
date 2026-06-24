"""
Dependency-free content/URL quality heuristics for the crawler.

Kept separate from scraper.py (which pulls in Playwright/bs4/aiohttp) so these
pure functions can be unit-tested standalone and reused by db_manager without a
heavy import. Stdlib only (re, urllib).

Three jobs:
  1. should_skip_url  — never even queue obvious non-content URLs (login, search,
     share, feeds, action endpoints, non-http schemes). Saves fetches.
  2. url_priority     — crawl article/detail pages BEFORE list/index pages, so the
     crawler produces useful content early and dead branches don't starve workers.
  3. is_low_quality_content — only keep formal, paragraphed, substantial prose;
     drop navigation hubs, link lists, calendars, near-empty stubs.
"""

import re
import urllib.parse

# ── URL filtering ─────────────────────────────────────────────────────────────

_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "ftp:", "#")

# Path segments that are never knowledge content: auth, search, user actions,
# sharing, machine endpoints. Anchored to a path-segment boundary.
_SKIP_PATH_RE = re.compile(
    r'(?:^|/)('
    r'login|signin|sign-in|register|signup|sign-up|logout|signout|'
    r'search|query|sso|account|member|members|user/login|passport|'
    r'verifycode|captcha|share|comment|comments|reply|vote|like|favorite|'
    r'print|download|attachment|feed|rss|atom|sitemap|robots|api|ajax|'
    r'wp-login|wp-admin|xmlrpc'
    r')(?:[/?._-]|$)',
    re.IGNORECASE,
)

# Query strings that signal an action/duplicate view rather than a document.
_SKIP_QUERY_KEYS = (
    "action=", "replytocom=", "share=", "do=login", "logout",
    "from=singlemessage", "redirect=", "redirect_uri=", "callback=",
)


def should_skip_url(url: str) -> bool:
    """True if the URL is structurally non-content and should not be queued."""
    if not url:
        return True
    low = url.strip().lower()
    if low.startswith(_SKIP_SCHEMES):
        return True
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return True
    if _SKIP_PATH_RE.search(parsed.path or ""):
        return True
    q = (parsed.query or "").lower()
    if any(k in q for k in _SKIP_QUERY_KEYS):
        return True
    return False


# ── URL priority (higher = crawl sooner) ──────────────────────────────────────

# Article/detail hints in the path.
_ARTICLE_HINT_RE = re.compile(
    r'/(article|content|detail|details|art|post|posts|news|show|view|info|'
    r'notice|gonggao|tongzhi|xinwen|c|a|p|t|item|story|read)(?:[/_-]|\d)',
    re.IGNORECASE,
)
# A date in the path (…/2024/05/… or …/20240518/…) → almost always a document.
_DATE_IN_PATH_RE = re.compile(r'/(?:19|20)\d{2}[-/_]?\d{1,2}')
# A long numeric id segment, optionally with an extension (…/123456.html).
_NUMERIC_ID_RE = re.compile(r'/\d{4,}(?:\.[a-z0-9]+)?/?$', re.IGNORECASE)
# List/index/navigation pages — useful for link DISCOVERY but low content value.
_LIST_URL_RE = re.compile(
    r'/(list|index|column|node|category|categories|tag|tags|channel|'
    r'catalog|directory|archive|page)(?:[/_-]|$)',
    re.IGNORECASE,
)

PRIORITY_ARTICLE = 20
PRIORITY_HOME = 15
PRIORITY_DEFAULT = 12
PRIORITY_LIST = 8


def url_priority(url: str) -> int:
    """Crawl-order hint. Article/detail pages first (produce content early),
    list/index pages later (still crawled, for discovery)."""
    try:
        path = (urllib.parse.urlparse(url).path or "").lower()
    except ValueError:
        return PRIORITY_DEFAULT
    # Strong, unambiguous document signals win first.
    if _DATE_IN_PATH_RE.search(path) or _NUMERIC_ID_RE.search(path):
        return PRIORITY_ARTICLE
    if path in ("", "/"):
        return PRIORITY_HOME
    # A list/index keyword (…/news/list/) outranks a weak article hint (…/news/),
    # so a section landing page isn't mistaken for an article.
    if _LIST_URL_RE.search(path):
        return PRIORITY_LIST
    if _ARTICLE_HINT_RE.search(path):
        return PRIORITY_ARTICLE
    return PRIORITY_DEFAULT


# ── Content quality ───────────────────────────────────────────────────────────

_SENT_END_RE = re.compile(r'[。！？!?；;…]')
_MD_LINK_RE = re.compile(r'!?\[([^\]]*)\]\([^)]*\)')   # [label](url) → label
_MD_MARKUP_RE = re.compile(r'[#>*`_~|]')
_MEANINGFUL_CHAR_RE = re.compile(r'[a-zA-Z一-鿿]')

MIN_CONTENT_CHARS = 180        # was 100 — formal articles are longer
LINK_DENSITY_MAX = 0.55        # above this the page is a nav/link hub
MIN_TEXT_RATIO = 0.5           # letters+CJK / non-space chars


def is_low_quality_content(text: str, link_density: float = None) -> tuple:
    """Return (is_low_quality: bool, reason: str).

    Keeps only formal, paragraphed, substantial prose. Rejects: too-short stubs,
    link/nav hubs (by link_density when available, else by shape), menu/list
    pages (many short lines, little sentence punctuation), and number/symbol
    dumps (calendars, tables of figures)."""
    if not text or not text.strip():
        return True, "empty"

    plain = _MD_LINK_RE.sub(r'\1', text)
    plain = _MD_MARKUP_RE.sub(' ', plain)
    nospace = re.sub(r'\s+', '', plain)
    n = len(nospace)
    if n < MIN_CONTENT_CHARS:
        return True, f"too_short:{n}"

    if link_density is not None and link_density > LINK_DENSITY_MAX:
        return True, f"link_heavy:{link_density:.2f}"

    sentences = len(_SENT_END_RE.findall(plain))
    paragraphs = [p for p in re.split(r'\n\s*\n', text)
                  if len(re.sub(r'\s+', '', p)) > 40]
    # Formal prose has sentence structure OR multiple real paragraphs.
    if sentences < 3 and len(paragraphs) < 2:
        return True, f"no_prose:sent={sentences},para={len(paragraphs)}"

    # Menu/list shape: many short lines and little sentence punctuation.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 8:
        short = sum(1 for ln in lines if len(re.sub(r'\s+', '', ln)) < 12)
        if short / len(lines) > 0.7 and sentences < 5:
            return True, "list_like"

    # Symbol/number dump: too few actual letters/ideographs.
    meaningful = len(_MEANINGFUL_CHAR_RE.findall(nospace))
    if meaningful / n < MIN_TEXT_RATIO:
        return True, f"low_text_ratio:{meaningful / n:.2f}"

    return False, "ok"
