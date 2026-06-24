"""
Reptile scraper — optimised for knowledge-base construction.

Key design decisions:
 • DeepSeek analyses CSS selectors ONCE per domain, cached in DB
 • Extracted content saved flat: scraped_data/{domain}/{slug}.md
 • 1-year date window (content older than 1 year is skipped)
 • Always headless Playwright (no browser window)
 • Live status pushed to asyncio.Queue per task → SSE to UI
 • Per-domain circuit breaker + rate limiting for stability
"""

import os
import re
import urllib.parse
from datetime import datetime, timezone, timedelta
import asyncio
import random
import hashlib
import time
import io

import pandas as pd
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import html2text
import aiohttp

import db_manager
import content_filter
from site_analyzer import analyze_site_structure

# ── Global state ────────────────────────────────────────────────────────────

task_events: dict = {}          # task_id → {'pause': Event, 'stop': Event}
task_root_prefixes: dict = {}   # task_id → dynamic root prefix
task_log_queues: dict = {}      # task_id → asyncio.Queue  (SSE status stream)

# Circuit breaker: domain → {'fail_count': int, 'open_until': float}
_domain_circuit: dict = {}
CIRCUIT_THRESHOLD = 5
CIRCUIT_COOLDOWN = 300

# Rate limiter: domain → last request monotonic timestamp
_domain_last_request: dict = {}
DOMAIN_MIN_INTERVAL = 1.5      # seconds

# Branch skip throttle — tracks consecutive skips under each parent URL.
# When a parent's children are all being filtered (list pages, too short,
# date-filtered…) the remaining siblings are fast-skipped without loading,
# preventing depth-first spirals that waste workers on dead branches.
_branch_skip_counts: dict = {}   # {f"{task_id}:{parent_url}": int}
BRANCH_SKIP_THRESHOLD = 8        # skip without browser fetch after this many consecutive sibling skips

# ── Initialisation ───────────────────────────────────────────────────────────

def init_task_events(task_id: str):
    task_events[task_id] = {
        'pause': asyncio.Event(),
        'stop': asyncio.Event(),
    }
    task_events[task_id]['pause'].set()
    task_events[task_id]['stop'].clear()

def create_log_queue(task_id: str) -> asyncio.Queue:
    q = asyncio.Queue(maxsize=500)
    task_log_queues[task_id] = q
    return q

# ── Status / logging ─────────────────────────────────────────────────────────

def push_status(task_id: str, msg: str, msg_type: str = "log", **extra):
    """Push a status event to the task's SSE queue (non-blocking)."""
    q = task_log_queues.get(task_id)
    if q:
        event = {"type": msg_type, "ts": datetime.now().strftime("%H:%M:%S"), "msg": msg}
        event.update(extra)
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop oldest would be better but this is rare

# ── Utilities ────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    if not name:
        return "untitled"
    s = re.sub(r'[\\/*?:"<>|]', "", str(name))
    s = re.sub(r'\s+', "_", s)
    return s[:50] or "untitled"

def url_to_slug(url: str) -> str:
    """Convert a URL to a safe flat filename slug (without extension)."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip("/")
    # Keep alphanumeric, hyphens, underscores — replace rest with _
    slug = re.sub(r'[^a-zA-Z0-9\-]', '_', path)
    slug = re.sub(r'_+', '_', slug).strip('_')
    slug = slug[:60] if slug else "index"
    hash_suffix = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{slug}_{hash_suffix}"

_LIST_URL_RE = re.compile(r'/(list|index|column|node)([/_]|$)', re.IGNORECASE)

def is_list_page(url: str) -> bool:
    """True if the URL looks like a list/index/navigation page (e.g. /News/List/...).
    These are useful for link discovery but are not knowledge content."""
    path = urllib.parse.urlparse(url).path
    return bool(_LIST_URL_RE.search(path))

def compute_content_hash(content: str) -> str:
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    return random.uniform(0, min(cap, base * (2 ** attempt)))

def _get_domain(url: str) -> str:
    return urllib.parse.urlparse(url).netloc

# ── Circuit breaker ──────────────────────────────────────────────────────────

def check_circuit(domain: str) -> bool:
    entry = _domain_circuit.get(domain)
    if not entry:
        return False
    if entry['fail_count'] >= CIRCUIT_THRESHOLD:
        if time.monotonic() < entry['open_until']:
            return True
        _domain_circuit[domain] = {'fail_count': 0, 'open_until': 0}
    return False

def record_domain_failure(domain: str):
    entry = _domain_circuit.setdefault(domain, {'fail_count': 0, 'open_until': 0})
    entry['fail_count'] += 1
    if entry['fail_count'] >= CIRCUIT_THRESHOLD:
        entry['open_until'] = time.monotonic() + CIRCUIT_COOLDOWN

def record_domain_success(domain: str):
    if domain in _domain_circuit:
        _domain_circuit[domain]['fail_count'] = 0

# ── Rate limiter ─────────────────────────────────────────────────────────────

async def acquire_rate_limit(domain: str):
    last = _domain_last_request.get(domain, 0)
    wait = DOMAIN_MIN_INTERVAL - (time.monotonic() - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _domain_last_request[domain] = time.monotonic()

# ── Directory setup ──────────────────────────────────────────────────────────

def setup_base_directory(start_url: str) -> tuple:
    """Stable output dir: scraped_data/{safe_domain}/"""
    parsed = urllib.parse.urlparse(start_url)
    domain = parsed.netloc.replace("www.", "")
    safe_domain = re.sub(r'[^a-zA-Z0-9_\-]', '_', domain)[:50]
    base_path = os.path.join(os.getcwd(), "scraped_data", safe_domain)
    os.makedirs(base_path, exist_ok=True)
    return base_path, domain

# ── Date filtering ───────────────────────────────────────────────────────────

_DATE_PATTERNS = [
    re.compile(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})'),   # YYYY-MM-DD / YYYY.MM.DD
    re.compile(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?'), # Chinese
]

def parse_publish_date(date_str: str):
    if not date_str:
        return None
    for pat in _DATE_PATTERNS:
        m = pat.search(date_str)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return datetime(y, mo, d, tzinfo=timezone.utc)
            except (ValueError, IndexError):
                continue
    return None

def is_within_date_window(date_str, cutoff_date=None) -> bool:
    """True if content is within the date window, or if date is unparseable (safe default).
    cutoff_date=None means no filter (keep all content regardless of date)."""
    if cutoff_date is None:
        return True
    if not date_str:
        return True      # missing date → assume recent
    dt = parse_publish_date(date_str)
    if dt is None:
        return True      # unparseable → assume recent
    return dt >= cutoff_date

# ── Content extraction ───────────────────────────────────────────────────────

_PUBLISH_PATTERNS = [
    # Primary: explicit publish/create/update time label with date value
    re.compile(r'(?:发布|创建|更新|日期)\s*[:：]\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}[日]?[\s\d:]*)'),
    # Source / author — stored separately, NOT used as publish date
    re.compile(r'来源\s*[:：]\s*([^\s<>]+)'),
    re.compile(r'作者\s*[:：]\s*([^\s<>]+)'),
]

def extract_publish_info_from_soup(soup) -> dict:
    result = {}
    meta_date = soup.find('meta', attrs={'name': re.compile(r'pubdate|publish', re.I)})
    if meta_date and meta_date.get('content'):
        result['publish_date'] = meta_date['content'].strip()

    # Check <time> tags
    time_tag = soup.find('time')
    if time_tag and not result.get('publish_date'):
        dt_attr = time_tag.get('datetime') or time_tag.get_text(strip=True)
        if dt_attr:
            result['publish_date'] = dt_attr

    full_text = soup.get_text(separator=' ', strip=True)
    if 'publish_date' not in result:
        m = _PUBLISH_PATTERNS[0].search(full_text)
        if m:
            result['publish_date'] = m.group(1).strip()

    m = _PUBLISH_PATTERNS[1].search(full_text)
    if m:
        result['source'] = m.group(1).strip()
    m = _PUBLISH_PATTERNS[2].search(full_text)
    if m:
        result['author'] = m.group(1).strip()

    return result


def extract_with_selectors(html: str, soup: BeautifulSoup, selectors: dict) -> dict:
    """
    Extract content using DeepSeek-identified CSS selectors.
    Falls back to trafilatura → body text if selectors fail.
    Returns {'title', 'date_str', 'content_md'}
    """
    result = {'title': None, 'date_str': None, 'content_md': None}

    # ── Title ──
    if selectors.get('title_selector'):
        try:
            els = soup.select(selectors['title_selector'])
            if els:
                result['title'] = els[0].get_text(strip=True)
        except Exception:
            pass
    if not result['title']:
        t = soup.find('title')
        result['title'] = t.string.strip() if t and t.string else 'Untitled'

    # ── Date ──
    if selectors.get('date_selector'):
        try:
            els = soup.select(selectors['date_selector'])
            if els:
                result['date_str'] = (
                    els[0].get('datetime') or
                    els[0].get('content') or
                    els[0].get_text(strip=True)
                )
        except Exception:
            pass
    if not result['date_str']:
        info = extract_publish_info_from_soup(soup)
        result['date_str'] = info.get('publish_date')

    # ── Content ──
    content_html = None
    if selectors.get('content_selector'):
        try:
            els = soup.select(selectors['content_selector'])
            if els:
                content_html = str(els[0])
        except Exception:
            pass

    if not content_html:
        # trafilatura fallback
        try:
            import trafilatura
            text = trafilatura.extract(html, include_tables=True, include_links=False,
                                       output_format='html')
            if text and len(text) > 50:
                content_html = text
        except ImportError:
            pass
        except Exception:
            pass

    if not content_html:
        # readability fallback
        try:
            from readability import Document
            doc = Document(html)
            content_html = doc.summary()
            if doc.title():
                result['title'] = result['title'] or doc.title()
        except Exception:
            pass

    if content_html:
        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        h.body_width = 0
        result['content_md'] = h.handle(content_html).strip()
    else:
        result['content_md'] = soup.get_text(separator='\n', strip=True)

    return result


def build_markdown_file(title: str, url: str, date_str: str, content_md: str) -> str:
    """Build a markdown file with YAML frontmatter."""
    header = "---\n"
    header += f'title: "{title}"\n'
    header += f'source_url: "{url}"\n'
    if date_str:
        header += f'publish_date: "{date_str}"\n'
    header += "---\n\n"
    header += f"# {title}\n\n"
    return header + content_md

# ── Link discovery ───────────────────────────────────────────────────────────

def determine_static_boundary(start_url: str) -> str:
    parsed = urllib.parse.urlparse(start_url)
    ext = os.path.splitext(parsed.path)[1].lower()
    is_file = ext in ['.html', '.htm', '.php', '.jsp', '.asp', '.aspx']
    path = parsed.path
    if is_file:
        path = path.rsplit('/', 1)[0]
    path = path.rstrip('/')
    if not path:
        base_path = '/'
    else:
        parts = path.rsplit('/', 1)
        base_path = parts[0] if parts[0] else '/'
        if not base_path.endswith('/'):
            base_path += '/'
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, base_path, '', '', ''))

def compute_link_density(soup) -> float:
    """Fraction of the page's text that lives inside <a> tags. High density →
    navigation / link-list hub rather than an article. Returns 0.0 on error."""
    try:
        total = soup.get_text(strip=True)
        if not total:
            return 1.0
        link_text = ''.join(a.get_text(strip=True) for a in soup.find_all('a'))
        return min(1.0, len(link_text) / max(1, len(total)))
    except Exception:
        return 0.0


def get_sub_domain_links(html: str, current_url: str, base_url: str,
                          dynamic_root_prefix=None) -> list:
    soup = BeautifulSoup(html, 'lxml')
    base_prefix = dynamic_root_prefix or (base_url if base_url.endswith('/') else base_url + '/')
    ignored = {'.zip', '.rar', '.exe', '.mp3', '.mp4', '.avi', '.jpg', '.jpeg',
               '.png', '.gif', '.webp', '.svg', '.pdf', '.doc', '.docx'}
    links = []
    for a in soup.find_all('a', href=True):
        full_url = urllib.parse.urljoin(current_url, a['href'])
        full_url = urllib.parse.urldefrag(full_url)[0]
        parsed = urllib.parse.urlparse(full_url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if parsed.scheme in ['http', 'https'] and ext not in ignored:
            # Drop structurally non-content URLs (login/search/share/feeds…) up
            # front so they never enter the queue or cost a fetch.
            if content_filter.should_skip_url(full_url):
                continue
            if (full_url.lower() == base_url.lower() or
                    full_url.lower().startswith(base_prefix.lower())):
                links.append(full_url)
    return list(set(links))

# ── Browser helpers ──────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

async def intercept_route(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()

async def scroll_page(page):
    try:
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight / 3)")
            await page.wait_for_timeout(800)
    except Exception:
        pass

# ── Pause / stop ─────────────────────────────────────────────────────────────

async def check_pause_stop(task_id: str) -> bool:
    events = task_events.get(task_id)
    if not events:
        return False
    if events['stop'].is_set():
        await asyncio.to_thread(db_manager.update_task_status, task_id, 'stopped')
        return True
    if not events['pause'].is_set():
        await asyncio.to_thread(db_manager.update_task_status, task_id, 'paused')
        await events['pause'].wait()
        if events['stop'].is_set():
            await asyncio.to_thread(db_manager.update_task_status, task_id, 'stopped')
            return True
        await asyncio.to_thread(db_manager.update_task_status, task_id, 'running')
    return False

# ── Core URL processor ───────────────────────────────────────────────────────

async def process_single_url(task_id: str, current_url: str, start_url: str,
                              base_path: str, browser, selectors: dict,
                              update_mode: bool = False,
                              single_page: bool = False,
                              date_cutoff=None,
                              parent_url: str = None) -> None:
    """Process one URL: load, extract with selectors, save flat markdown."""

    if await check_pause_stop(task_id):
        return

    domain = _get_domain(current_url)

    # Branch throttle: if this parent's siblings have been skipped too many
    # times in a row, fast-skip without loading the page at all.
    branch_key = f"{task_id}:{parent_url or ''}"
    if parent_url and _branch_skip_counts.get(branch_key, 0) >= BRANCH_SKIP_THRESHOLD:
        push_status(task_id, f"↩ 快速跳过 (父级分支连续跳过 {_branch_skip_counts[branch_key]} 次): {current_url}", "info")
        await asyncio.to_thread(db_manager.mark_url_filtered, task_id, current_url, "branch_throttled")
        return

    if check_circuit(domain):
        push_status(task_id, f"⚡ 跳过 {current_url} (线路熔断)", "warn")
        await asyncio.to_thread(db_manager.mark_url_filtered, task_id, current_url, "circuit_open")
        return

    await acquire_rate_limit(domain)

    context = None
    page = None
    try:
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)

        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = await browser.new_context(
                user_agent=ua, viewport=vp, ignore_https_errors=True
            )

        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        await page.route("**/*", intercept_route)

        for attempt in range(3):
            try:
                await page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_selector('body', state='attached', timeout=20000)
                if await check_pause_stop(task_id):
                    return
                await page.wait_for_timeout(2000)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == 2:
                    record_domain_failure(domain)
                    push_status(task_id, f"⚠ 页面加载失败: {current_url}", "warn")
                    await asyncio.to_thread(db_manager.mark_url_failed, task_id, current_url, str(e))
                    return
                await asyncio.sleep(backoff_delay(attempt))

        await scroll_page(page)
        html_content = await page.content()

        # ── Crawl boundary ──
        dynamic_root = task_root_prefixes.get(task_id)
        if not dynamic_root:
            task_data = await asyncio.to_thread(db_manager.get_task, task_id)
            if task_data and task_data.get('dynamic_root'):
                dynamic_root = task_data['dynamic_root']
                task_root_prefixes[task_id] = dynamic_root
        if current_url == start_url and not dynamic_root:
            dynamic_root = determine_static_boundary(start_url)
            task_root_prefixes[task_id] = dynamic_root
            await asyncio.to_thread(db_manager.update_task_dynamic_root, task_id, dynamic_root)

        # Discover new links (skipped in single_page mode)
        if not single_page:
            new_links = get_sub_domain_links(html_content, current_url, start_url, dynamic_root)
            if new_links:
                await asyncio.to_thread(db_manager.add_discovered_urls, task_id, current_url, new_links)

        # ── Extract content ──
        full_soup = BeautifulSoup(html_content, 'lxml')
        extracted = extract_with_selectors(html_content, full_soup, selectors)

        title = extracted['title'] or 'Untitled'
        date_str = extracted['date_str']
        content_md = extracted['content_md'] or ''

        def _skip(reason_label: str, db_reason: str):
            """Record a skip and increment the branch-skip counter."""
            push_status(task_id, f"{reason_label}: {current_url}", "info")
            asyncio.ensure_future(asyncio.to_thread(
                db_manager.mark_url_filtered, task_id, current_url, db_reason
            ))
            if parent_url:
                _branch_skip_counts[branch_key] = _branch_skip_counts.get(branch_key, 0) + 1

        # ── Date filter (only active when caller passes a cutoff) ──
        if not is_within_date_window(date_str, cutoff_date=date_cutoff):
            _skip(f"📅 跳过 (时间超出范围 {date_str})", f"date_out_of_window:{date_str}")
            return

        # Skip list/navigation pages — keep them for link discovery (already
        # done above) but don't save them as knowledge content.
        if is_list_page(current_url):
            _skip("↩ 跳过 (列表/导航页)", "list_page")
            return

        # Quality gate: only keep formal, paragraphed, substantial prose. Drops
        # nav hubs (high link density), menu/list pages, number/symbol dumps and
        # near-empty stubs — the knowledge base only wants real article content.
        link_density = compute_link_density(full_soup)
        low_quality, reason = content_filter.is_low_quality_content(
            content_md, link_density)
        if low_quality:
            _skip(f"↩ 跳过 (低质量内容: {reason})", f"low_quality:{reason}")
            return

        # Page has real content — reset the branch-skip counter
        if parent_url:
            _branch_skip_counts.pop(branch_key, None)

        # ── Build markdown ──
        markdown = build_markdown_file(title, current_url, date_str, content_md)
        new_hash = compute_content_hash(markdown)

        # ── Iterative update: skip if unchanged ──
        if update_mode:
            old_hash = await asyncio.to_thread(db_manager.get_url_content_hash, task_id, current_url)
            if old_hash and old_hash == new_hash:
                push_status(task_id, f"✓ 未变化: {title}", "info")
                await asyncio.to_thread(
                    db_manager.mark_url_scraped, task_id, current_url, title, None, new_hash,
                    bump_count=False
                )
                record_domain_success(domain)
                return

        # ── Cross-URL duplicate content: same article reached via another URL ──
        # (mirror pages, print/lang variants, trailing-slash dups). Skip the
        # redundant download instead of saving a second identical file.
        if await asyncio.to_thread(
                db_manager.content_hash_seen, task_id, new_hash, current_url):
            push_status(task_id, f"↩ 跳过 (内容与已保存页面重复): {title}", "info")
            await asyncio.to_thread(
                db_manager.mark_url_filtered, task_id, current_url, "duplicate_content")
            record_domain_success(domain)
            return

        # ── Save flat file ──
        slug = url_to_slug(current_url)
        file_path = os.path.join(base_path, f"{slug}.md")
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(markdown)

        record_domain_success(domain)
        push_status(task_id, f"✅ 已保存: {title}", "success", url=current_url)
        await asyncio.to_thread(
            db_manager.mark_url_scraped, task_id, current_url, title, file_path, new_hash
        )

    except asyncio.CancelledError:
        raise
    except Exception as page_e:
        import traceback
        traceback.print_exc()
        push_status(task_id, f"✖ 错误: {current_url} — {page_e}", "error")
        record_domain_failure(domain)
        await asyncio.to_thread(db_manager.mark_url_failed, task_id, current_url, str(page_e))
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass

# ── Worker task ──────────────────────────────────────────────────────────────

async def worker_task(task_id: str, start_url: str, base_path: str, browser,
                      semaphore: asyncio.Semaphore, selectors: dict,
                      update_mode: bool = False,
                      single_page: bool = False,
                      date_cutoff=None):
    async with semaphore:
        try:
            if await check_pause_stop(task_id):
                return False

            result = await asyncio.to_thread(db_manager.get_and_lock_pending_url, task_id)
            if not result:
                return True
            current_url, parent_url = result

            delay = random.uniform(1.0, 2.5)
            await asyncio.sleep(delay)

            await process_single_url(
                task_id, current_url, start_url, base_path,
                browser, selectors,
                update_mode=update_mode,
                single_page=single_page,
                date_cutoff=date_cutoff,
                parent_url=parent_url,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            import traceback
            traceback.print_exc()
            push_status(task_id, f"✖ Worker 异常: {e}", "error")
            return True

# ── Main crawl worker ────────────────────────────────────────────────────────

async def crawl_worker(task_id: str, start_url: str, api_key: str,
                       update_mode: bool = False,
                       single_page: bool = False,
                       date_from: str = ""):
    """
    Background manager:
    1. Analyses site structure with DeepSeek (once per domain, cached)
    2. Schedules 5 concurrent workers using the identified selectors
    """
    init_task_events(task_id)
    await asyncio.to_thread(db_manager.reset_processing_urls, task_id)

    base_path, base_domain = setup_base_directory(start_url)
    await asyncio.to_thread(db_manager.create_task, task_id, start_url, start_url)
    await asyncio.to_thread(db_manager.update_task_base_path, task_id, base_path)

    # Parse cutoff date from "YYYY-MM" string
    date_cutoff = None
    if date_from:
        try:
            y, m = int(date_from[:4]), int(date_from[5:7])
            date_cutoff = datetime(y, m, 1, tzinfo=timezone.utc)
        except (ValueError, IndexError):
            pass

    semaphore = asyncio.Semaphore(5)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

    push_status(task_id, f"🚀 启动任务: {start_url}", "log")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-gpu"]
            )

            # ── Session warm-up ──
            push_status(task_id, "🌐 正在建立浏览器会话...", "log")
            shared_context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport=random.choice(VIEWPORTS),
                ignore_https_errors=True
            )
            warmup_html = ""
            try:
                warmup_page = await shared_context.new_page()
                await Stealth().apply_stealth_async(warmup_page)
                await warmup_page.route("**/*", intercept_route)
                await warmup_page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
                await scroll_page(warmup_page)
                await warmup_page.wait_for_timeout(2000)
                warmup_html = await warmup_page.content()
                push_status(task_id, "✅ 浏览器会话建立成功", "log")
                await warmup_page.close()
            except Exception as e:
                push_status(task_id, f"⚠ 会话预热遇到问题 (继续): {e}", "warn")

            # ── Site analysis ──
            selectors = await asyncio.to_thread(db_manager.get_site_analysis, base_domain)
            if selectors:
                push_status(task_id, "📋 使用缓存的页面结构分析", "log")
            else:
                push_status(task_id, "🤖 DeepSeek 正在分析页面结构，请稍候...", "log")
                selectors = await analyze_site_structure(warmup_html, api_key)
                await asyncio.to_thread(db_manager.save_site_analysis, base_domain, selectors)
                summary = ", ".join(
                    f"{k}: {v}" for k, v in selectors.items() if v
                ) or "（未能识别到明确选择器，使用通用提取）"
                push_status(task_id, f"✅ 结构分析完成 — {summary}", "log")

            push_status(task_id, "🕷 开始爬取...", "log")

            # ── Main crawl loop ──
            active_tasks = set()
            while True:
                if await check_pause_stop(task_id):
                    break

                done = {t for t in active_tasks if t.done()}
                active_tasks.difference_update(done)

                for t in done:
                    if not t.cancelled() and t.exception() is not None:
                        push_status(task_id, f"✖ Task 异常: {t.exception()}", "error")
                    elif not t.cancelled() and t.result() is False:
                        break

                active_count = await asyncio.to_thread(db_manager.get_active_count, task_id)
                task_data = await asyncio.to_thread(db_manager.get_task, task_id)
                push_status(task_id, "", "progress",
                            pages_done=task_data['total_scraped'] if task_data else 0,
                            pages_queue=active_count)

                if active_count == 0 and len(active_tasks) == 0:
                    await asyncio.to_thread(db_manager.update_task_status, task_id, 'completed')
                    task_data = await asyncio.to_thread(db_manager.get_task, task_id)
                    total = task_data['total_scraped'] if task_data else 0
                    push_status(task_id,
                                f"🎉 数据下载完成！共保存 {total} 篇内容到 {base_path}",
                                "done", pages_done=total, path=base_path)
                    break

                while len(active_tasks) < 5 and active_count > 0:
                    t = asyncio.create_task(
                        worker_task(task_id, start_url, base_path, browser,
                                    semaphore, selectors,
                                    update_mode=update_mode,
                                    single_page=single_page,
                                    date_cutoff=date_cutoff)
                    )
                    active_tasks.add(t)
                    active_count -= 1

                if active_tasks:
                    stop_evt = asyncio.create_task(task_events[task_id]['stop'].wait())
                    done_set, _ = await asyncio.wait(
                        list(active_tasks) + [stop_evt],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop_evt in done_set:
                        await asyncio.to_thread(db_manager.update_task_status, task_id, 'stopped')
                        push_status(task_id, "⏹ 任务已停止", "done")
                        break
                    else:
                        stop_evt.cancel()
                else:
                    await asyncio.sleep(1)

    except Exception as e:
        push_status(task_id, f"💥 致命错误: {e}", "error")
        await asyncio.to_thread(db_manager.update_task_status, task_id, 'failed')
    finally:
        if 'active_tasks' in locals() and active_tasks:
            for t in active_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.wait(active_tasks, timeout=2.0)

        await asyncio.to_thread(db_manager.reset_processing_urls, task_id)

        # Signal SSE consumers to close
        q = task_log_queues.get(task_id)
        if q:
            try:
                q.put_nowait(None)  # sentinel
            except asyncio.QueueFull:
                pass

        if task_id in task_events:
            del task_events[task_id]
        if task_id in task_root_prefixes:
            del task_root_prefixes[task_id]
