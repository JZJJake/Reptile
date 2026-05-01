import os
import re
import urllib.parse
from datetime import datetime
import asyncio
import random
import pandas as pd
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import html2text
import aiohttp
from readability import Document
import io

import db_manager

# Global dictionary to hold asyncio.Event objects for each task
# task_id -> {'pause': Event, 'stop': Event}
task_events = {}
task_root_prefixes = {} # task_id -> dynamic root prefix

def init_task_events(task_id: str):
    task_events[task_id] = {
        'pause': asyncio.Event(),
        'stop': asyncio.Event()
    }
    # Initially NOT paused
    task_events[task_id]['pause'].set()
    # Initially NOT stopped
    task_events[task_id]['stop'].clear()

def sanitize_filename(name):
    """Sanitize string to create a safe file/folder name."""
    safe_name = re.sub(r'[\\/*?:"<>|]', "", name)
    safe_name = re.sub(r'\s+', "_", safe_name)
    return safe_name[:50] # Limit length

def setup_base_directory(start_url):
    """Create the root directory for this crawl session."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parsed = urllib.parse.urlparse(start_url)
    domain = parsed.netloc.replace("www.", "")
    safe_domain = re.sub(r'[^a-zA-Z0-9]', '_', domain)

    folder_name = f"{timestamp}_{safe_domain}"
    base_path = os.path.join(os.getcwd(), "scraped_data", folder_name)
    os.makedirs(base_path, exist_ok=True)
    return base_path, domain

def setup_page_directory(base_path, title):
    """Create a sub-directory for a specific page."""
    safe_title = sanitize_filename(title)
    if not safe_title:
        safe_title = "Untitled_" + datetime.now().strftime("%H%M%S")

    page_path = os.path.join(base_path, safe_title)
    # Ensure unique directory, use atomic creation to prevent TOCTOU race condition
    counter = 1
    original_path = page_path
    while True:
        try:
            os.makedirs(page_path, exist_ok=False)
            break
        except FileExistsError:
            page_path = f"{original_path}_{counter}"
            counter += 1

    tables_path = os.path.join(page_path, "tables")
    images_path = os.path.join(page_path, "images")

    os.makedirs(tables_path, exist_ok=True)
    os.makedirs(images_path, exist_ok=True)

    return page_path, tables_path, images_path

async def download_image(session, img_url, save_path):
    """Download an image asynchronously with retries and headers."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': img_url # Some CDNs require referer
    }

    for attempt in range(3):
        try:
            # We use verify_ssl=False in case of self-signed certs
            async with session.get(img_url, headers=headers, timeout=15, ssl=False) as response:
                if response.status == 200:
                    content = await response.read()
                    with open(save_path, 'wb') as f:
                        f.write(content)
                    return True
                elif response.status in [301, 302, 303, 307, 308]:
                    # Follow redirect manually if aiohttp doesn't handle it
                    redirect_url = response.headers.get('Location')
                    if redirect_url:
                        if not redirect_url.startswith('http'):
                            import urllib.parse
                            redirect_url = urllib.parse.urljoin(img_url, redirect_url)
                        img_url = redirect_url
                        continue
                else:
                    print(f"Failed to download {img_url}: HTTP {response.status}")
                    break # Don't retry 404s etc.
        except Exception as e:
            if attempt == 2:
                print(f"Error downloading {img_url} after 3 attempts: {e}")
            import asyncio
            await asyncio.sleep(1)

    return False

def determine_static_boundary(start_url):
    """
    Determines the strict physical generic boundary.
    Extracts the parent directory of the current directory to include siblings and their children.
    """
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

def get_sub_domain_links(html, current_url, base_url, dynamic_root_prefix=None):
    """Extract links that are downward paths from the pre-calculated root prefix."""
    soup = BeautifulSoup(html, 'lxml')
    links = []

    # Use the pre-computed static root prefix if available, otherwise fallback to base_url
    base_prefix = dynamic_root_prefix if dynamic_root_prefix else (base_url if base_url.endswith('/') else base_url + '/')

    # Ignore purely media/executable files, but ALLOW documents we want to download
    ignored_extensions = {'.zip', '.rar', '.exe', '.mp3', '.mp4', '.avi', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg'}

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        full_url = urllib.parse.urljoin(current_url, href)
        # Remove fragment
        full_url = urllib.parse.urldefrag(full_url)[0]

        parsed_url = urllib.parse.urlparse(full_url)
        ext = os.path.splitext(parsed_url.path)[1].lower()

        if parsed_url.scheme in ['http', 'https'] and ext not in ignored_extensions:
            # Check if it is the base_url itself, or a child path of the base_prefix (case-insensitive)
            if full_url.lower() == base_url.lower() or full_url.lower().startswith(base_prefix.lower()):
                links.append(full_url)
    return list(set(links))

async def scroll_page(page):
    """Scroll down the page slowly to load dynamic content."""
    try:
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight / 5)")
            await page.wait_for_timeout(1000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1000)
    except Exception as e:
        print(f"Warning: Failed to scroll page fully: {e}")

def process_tables(soup, tables_path):
    """Extract tables and save as CSV."""
    tables = soup.find_all('table')
    for i, table in enumerate(tables):
        try:
            dfs = pd.read_html(io.StringIO(str(table)))
            if dfs:
                df = dfs[0]
                csv_path = os.path.join(tables_path, f"table_{i+1}.csv")
                df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        except Exception as e:
            print(f"Error processing table {i+1}: {e}")

async def process_images(soup, base_url, images_path, session):
    """Extract image URLs from various sources, download them, and update HTML."""
    download_tasks = []
    image_count = 0

    # Process <img> tags
    for img in soup.find_all('img'):
        src = None
        # Try a variety of common attributes for lazy-loaded images, prioritizing real image over placeholder
        for attr in ['data-src', 'data-original', 'data-url', 'data-echo', 'data-lazy-src', 'src']:
            val = img.get(attr)
            if val and isinstance(val, str) and val.strip():
                if val.strip().startswith('data:image'):
                    continue
                src = val.strip()
                break

        if src:
            img_url = urllib.parse.urljoin(base_url, src)
            if img_url.startswith('data:'):
                continue

            parsed_img_url = urllib.parse.urlparse(img_url)
            ext = os.path.splitext(parsed_img_url.path)[1]
            if not ext or ext.lower() not in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']:
                ext = '.jpg'

            image_count += 1
            filename = f"image_{image_count}{ext}"
            save_path = os.path.join(images_path, filename)

            # Unify all sources to 'src' pointing to local file
            img['src'] = f"./images/{filename}"
            # Remove other data attributes to prevent lazy-loaders from overriding it
            for attr in ['data-src', 'data-original', 'data-url', 'data-echo', 'data-lazy-src']:
                if img.get(attr):
                    del img[attr]

            download_tasks.append(download_image(session, img_url, save_path))

    # Process <video> poster attributes
    for video in soup.find_all('video'):
        poster = video.get('poster')
        if poster and isinstance(poster, str) and poster.strip():
            img_url = urllib.parse.urljoin(base_url, poster.strip())
            if img_url.startswith('data:'):
                continue

            parsed_img_url = urllib.parse.urlparse(img_url)
            ext = os.path.splitext(parsed_img_url.path)[1]
            if not ext or ext.lower() not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                ext = '.jpg'

            image_count += 1
            filename = f"image_{image_count}{ext}"
            save_path = os.path.join(images_path, filename)

            video['poster'] = f"./images/{filename}"
            download_tasks.append(download_image(session, img_url, save_path))

    # Process inline background-image styles and custom elements like <xg-poster>
    import re
    url_pattern = re.compile(r'url\(\s*[\'\"]?(.*?)[\'\"]?\s*\)')

    for tag in soup.find_all(style=True):
        style_content = tag['style']
        if 'background' in style_content:
            matches = url_pattern.findall(style_content)
            new_style = style_content
            for match in matches:
                if match.startswith('data:'):
                    continue
                img_url = urllib.parse.urljoin(base_url, match)
                parsed_img_url = urllib.parse.urlparse(img_url)
                ext = os.path.splitext(parsed_img_url.path)[1]
                if not ext or ext.lower() not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                    ext = '.jpg'

                image_count += 1
                filename = f"image_{image_count}{ext}"
                save_path = os.path.join(images_path, filename)

                # Replace the URL in the style attribute
                new_style = new_style.replace(match, f"./images/{filename}")
                download_tasks.append(download_image(session, img_url, save_path))

                # If it's a specific poster tag, also insert an img tag so markdown captures it
                if tag.name in ['xg-poster', 'div', 'span'] and 'poster' in tag.get('class', []):
                    new_img = soup.new_tag('img', src=f"./images/{filename}")
                    tag.append(new_img)

            tag['style'] = new_style

    if download_tasks:
        await asyncio.gather(*download_tasks)

import re

def extract_publish_info(soup):
    """Extract publish date, source, and author information."""
    info_text = []

    # Common text patterns in government websites
    patterns = [
        re.compile(r'(?:发布|创建)时间\s*[:：]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?[\s\d:]*)'),
        re.compile(r'来源\s*[:：]\s*([^\s]+)'),
        re.compile(r'作者\s*[:：]\s*([^\s]+)')
    ]

    # Check meta tags first
    meta_date = soup.find('meta', attrs={'name': re.compile(r'pubdate', re.I)})
    if meta_date and meta_date.get('content'):
        info_text.append(f"**发布时间:** {meta_date['content']}")

    meta_source = soup.find('meta', attrs={'name': re.compile(r'source', re.I)})
    if meta_source and meta_source.get('content'):
        info_text.append(f"**来源:** {meta_source['content']}")

    # Search in text nodes if we don't have them
    full_text = soup.get_text(separator=' ', strip=True)
    if not any("发布时间" in t for t in info_text):
        match = patterns[0].search(full_text)
        if match:
            info_text.append(f"**发布时间:** {match.group(1)}")

    if not any("来源" in t for t in info_text):
        match = patterns[1].search(full_text)
        if match:
            info_text.append(f"**来源:** {match.group(1)}")

    match = patterns[2].search(full_text)
    if match:
        info_text.append(f"**作者:** {match.group(1)}")

    return "\n".join(info_text)

def clean_html_structure(soup):
    """Remove boilerplate tags (nav, footer, sidebar) to extract clean core content."""
    # Tags to remove entirely
    tags_to_decompose = [
        'nav', 'footer', 'header', 'aside', 'script', 'style', 'noscript', 'iframe'
    ]
    for tag in tags_to_decompose:
        for match in soup.find_all(tag):
            match.decompose()

    # Classes/IDs commonly used for non-content wrappers
    bad_classes_ids = re.compile(r'menu|nav|footer|sidebar|header|banner|ad|advert|breadcrumb|share|comment', re.I)

    for tag in soup.find_all(['div', 'ul', 'ol', 'section']):
        # Some tags might not have attributes dict if they are malformed or a NavigableString (though find_all filters)
        if not hasattr(tag, 'attrs'):
            continue

        class_list = tag.attrs.get('class') if tag.attrs else None
        class_str = " ".join(class_list) if isinstance(class_list, list) else (class_list if isinstance(class_list, str) else "")
        id_str = tag.attrs.get('id', '') if tag.attrs else ''

        if bad_classes_ids.search(class_str) or bad_classes_ids.search(id_str):
            tag.decompose()

    return soup

async def download_file(url, save_path, user_agent):
    """Download arbitrary files (PDF, Word, Excel, etc.) via aiohttp."""
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        headers = {'User-Agent': user_agent}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            async with session.get(url, timeout=300) as response:
                if response.status == 200:
                    with open(save_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                    return True
    except Exception as e:
        print(f"Error downloading file {url}: {e}")
    return False

def convert_to_markdown(html):
    """Convert HTML to clean Markdown text."""
    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.body_width = 0
    return h.handle(html)

async def check_pause_stop(task_id: str) -> bool:
    """Returns True if the task should stop, False otherwise. Handles pausing."""
    events = task_events.get(task_id)
    if not events:
        return False

    if events['stop'].is_set():
        await asyncio.to_thread(db_manager.update_task_status, task_id, 'stopped')
        return True

    if not events['pause'].is_set():
        await asyncio.to_thread(db_manager.update_task_status, task_id, 'paused')
        await events['pause'].wait() # Wait until unpaused

        # Check if stopped while paused
        if events['stop'].is_set():
            await asyncio.to_thread(db_manager.update_task_status, task_id, 'stopped')
            return True

        await asyncio.to_thread(db_manager.update_task_status, task_id, 'running')

    return False

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
]

async def intercept_route(route):
    """Intercept and block irrelevant resources to speed up loading and save bandwidth."""
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()

async def process_single_url(task_id: str, current_url: str, start_url: str, base_path: str, browser) -> None:
    """Processes a single URL within a worker context."""

    # Check for direct file downloads
    parsed_url = urllib.parse.urlparse(current_url)
    ext = os.path.splitext(parsed_url.path)[1].lower()
    if ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx']:
        filename = os.path.basename(parsed_url.path) or f"download_{random.randint(1000,9999)}{ext}"
        safe_title = re.sub(r'[\\/*?:"<>|]', "", filename)

        # Directory creation is I/O blocking, offload to thread, atomic generation to prevent override
        page_path, _, _ = await asyncio.to_thread(setup_page_directory, base_path, safe_title)
        save_path = os.path.join(page_path, filename)

        ua = random.choice(USER_AGENTS)
        success = await download_file(current_url, save_path, ua)
        if success:
            await asyncio.to_thread(db_manager.mark_url_scraped, task_id, current_url, filename, page_path, 'article')
        else:
            await asyncio.to_thread(db_manager.mark_url_failed, task_id, current_url, "File download failed")
        return

    # Normal HTML processing
    context = None
    page = None
    try:
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)

        context = await browser.new_context(
            user_agent=ua,
            viewport=vp,
            ignore_https_errors=True
        )
        page = await context.new_page()

        # Apply stealth
        await Stealth().apply_stealth_async(page)

        # Intercept and block unnecessary resources
        await page.route("**/*", intercept_route)

        # Use domcontentloaded to ensure SPA and dynamic frameworks generate the DOM
        try:
            await page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector('body', state='attached', timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as goto_e:
            print(f"Warning: page.goto timeout or error for {current_url}: {goto_e}. Attempting to proceed with loaded content.")

        await scroll_page(page)
        html_content = await page.content()

        # Ensure we have the root boundary prefix. If it's the start URL, calculate and save it.
        # If it's a child worker picking up a task after a restart, try to load it from the DB.
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

        # Extract new links under the determined generic root boundary
        new_links = get_sub_domain_links(html_content, current_url, start_url, dynamic_root)
        if new_links:
            await asyncio.to_thread(db_manager.add_discovered_urls, task_id, current_url, new_links)

        # Extract robust content
        full_soup = BeautifulSoup(html_content, 'lxml')

        # 1. Try to get title from document or fallback to <title> tag
        title = full_soup.title.string if full_soup.title else "Untitled Page"
        # Directory creation is I/O blocking, offload to thread
        page_path, tables_path, images_path = await asyncio.to_thread(setup_page_directory, base_path, title)

        # Process images on the ENTIRE page FIRST to mutate src tags to local relative paths
        body_soup = full_soup.find('body') or full_soup
        connector = aiohttp.TCPConnector(ssl=False)
        headers = {'User-Agent': ua}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            await process_images(body_soup, current_url, images_path, session)

        # Extract publish info BEFORE structural cleaning
        publish_info = extract_publish_info(full_soup)

        # Now create a cleaned copy from the mutated full_soup (so local image src is preserved)
        try:
            cleaned_soup = clean_html_structure(BeautifulSoup(str(full_soup), 'lxml'))
            cleaned_html = str(cleaned_soup)

            doc = Document(cleaned_html)
            title = doc.title() or title
            main_html = doc.summary()
            main_soup = BeautifulSoup(main_html, 'lxml')

            if len(main_soup.get_text(strip=True)) < 50:
                main_soup = cleaned_soup.find('body') or cleaned_soup
        except Exception:
            cleaned_soup = clean_html_structure(BeautifulSoup(str(full_soup), 'lxml'))
            main_soup = cleaned_soup.find('body') or cleaned_soup

        # 2. Extract tables from the MAIN content area
        process_tables(main_soup, tables_path)

        # 3. Generate Markdown from the main_soup (which now contains local image src attributes)
        markdown_content = convert_to_markdown(str(main_soup))

        markdown_header = f"# {title}\n\n**Source URL:** {current_url}\n"
        if publish_info:
            markdown_header += f"{publish_info}\n"
        markdown_header += "\n---\n\n"

        markdown_content = markdown_header + markdown_content

        # Content heuristic
        content_type = 'node'
        text_blocks = full_soup.find_all(['p', 'div', 'span', 'article', 'section'])
        for block in text_blocks:
            if not hasattr(block, 'attrs'):
                continue
            temp_block = BeautifulSoup(str(block), 'lxml')
            for a in temp_block.find_all('a'):
                a.decompose()
            text_content = temp_block.get_text(strip=True)
            if len(text_content) > 30:
                content_type = 'article'
                break

        md_path = os.path.join(page_path, "content.md")
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(markdown_content)

        await asyncio.to_thread(db_manager.mark_url_scraped, task_id, current_url, title, page_path, content_type)

    except Exception as page_e:
        import traceback
        traceback.print_exc()
        print(f"Error scraping {current_url}: {page_e}")
        await asyncio.to_thread(db_manager.mark_url_failed, task_id, current_url, str(page_e))
    finally:
        if page:
            await page.close()
        if context:
            await context.close()

async def worker_task(task_id: str, start_url: str, base_path: str, browser, semaphore: asyncio.Semaphore):
    """A worker task that runs under a semaphore to limit concurrency."""
    async with semaphore:
        try:
            should_stop = await check_pause_stop(task_id)
            if should_stop:
                return False # Indicate stopping

            current_url = await asyncio.to_thread(db_manager.get_and_lock_pending_url, task_id)
            if not current_url:
                return True # Queue empty for now

            # Add random delay to simulate human behavior and avoid WAF rate limiting
            delay = random.uniform(1.0, 3.0)
            await asyncio.sleep(delay)

            await process_single_url(task_id, current_url, start_url, base_path, browser)
            return True
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Worker task crashed unexpectedly: {e}")
            return True # Don't stop the whole crawl, just this worker cycle

async def crawl_worker(task_id: str, start_url: str, headless: bool = True):
    """Background manager that schedules concurrent workers."""

    init_task_events(task_id)
    await asyncio.to_thread(db_manager.reset_processing_urls, task_id)

    base_path, base_domain = setup_base_directory(start_url)
    await asyncio.to_thread(db_manager.create_task, task_id, start_url, start_url)

    # Strictly limit concurrency to 5 to prevent memory overload and aggressive IP blocks
    semaphore = asyncio.Semaphore(5)

    # Force PLAYWRIGHT_BROWSERS_PATH to 0 here as well so the worker picks it up
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"]
            )

            active_tasks = set()
            while True:
                # Check global stop/pause
                should_stop = await check_pause_stop(task_id)
                if should_stop:
                    break

                # Clean up completed tasks
                done = {t for t in active_tasks if t.done()}
                active_tasks.difference_update(done)

                # Check if any completed task signaled to stop or failed
                stop_signal = False
                for t in done:
                    if t.cancelled():
                        continue
                    if t.exception() is not None:
                        print(f"Task exception caught in manager: {t.exception()}")
                        continue
                    if t.result() is False:
                        stop_signal = True
                        break

                if stop_signal:
                    break

                # If active count (pending + processing) is 0 and no tasks are running, we are done
                active_count = await asyncio.to_thread(db_manager.get_active_count, task_id)
                if active_count == 0 and len(active_tasks) == 0:
                    await asyncio.to_thread(db_manager.update_task_status, task_id, 'completed')
                    break

                # Replenish workers up to the concurrency limit (5)
                while len(active_tasks) < 5 and active_count > 0:
                    task = asyncio.create_task(worker_task(task_id, start_url, base_path, browser, semaphore))
                    active_tasks.add(task)
                    active_count -= 1 # Optimistically decrement to avoid over-spawning if DB hasn't caught up

                if active_tasks:
                    # Wait for at least one task to complete before continuing the loop
                    await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)
                else:
                    # Brief pause if nothing to do but DB says there should be (rare race condition)
                    await asyncio.sleep(1)

    except Exception as e:
        print(f"Crawl fatally failed: {str(e)}")
        await asyncio.to_thread(db_manager.update_task_status, task_id, 'failed')
    finally:
        await asyncio.to_thread(db_manager.reset_processing_urls, task_id)
        if task_id in task_events:
            del task_events[task_id]
        if task_id in task_root_prefixes:
            del task_root_prefixes[task_id]
