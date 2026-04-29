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
    # Ensure unique directory
    counter = 1
    original_path = page_path
    while os.path.exists(page_path):
        page_path = f"{original_path}_{counter}"
        counter += 1

    tables_path = os.path.join(page_path, "tables")
    images_path = os.path.join(page_path, "images")

    os.makedirs(page_path, exist_ok=True)
    os.makedirs(tables_path, exist_ok=True)
    os.makedirs(images_path, exist_ok=True)

    return page_path, tables_path, images_path

async def download_image(session, img_url, save_path):
    """Download an image asynchronously."""
    try:
        async with session.get(img_url, timeout=10) as response:
            if response.status == 200:
                content = await response.read()
                with open(save_path, 'wb') as f:
                    f.write(content)
                return True
    except Exception as e:
        print(f"Error downloading {img_url}: {e}")
    return False

def get_sub_domain_links(html, current_url, base_url):
    """Extract links that share the same domain as the base_url."""
    soup = BeautifulSoup(html, 'lxml')
    links = []

    base_parsed = urllib.parse.urlparse(base_url)
    base_domain = base_parsed.netloc

    # Common static file extensions to ignore
    ignored_extensions = {'.pdf', '.zip', '.rar', '.exe', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.mp3', '.mp4', '.avi', '.jpg', '.jpeg', '.png', '.gif'}

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        full_url = urllib.parse.urljoin(current_url, href)
        # Remove fragment
        full_url = urllib.parse.urldefrag(full_url)[0]

        parsed_url = urllib.parse.urlparse(full_url)
        ext = os.path.splitext(parsed_url.path)[1].lower()

        if parsed_url.scheme in ['http', 'https'] and ext not in ignored_extensions:
            # Check if it shares the same domain (netloc)
            if parsed_url.netloc == base_domain:
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
    """Extract image URLs, download them, and update src in HTML."""
    images = soup.find_all('img')
    download_tasks = []

    for i, img in enumerate(images):
        src = img.get('src') or img.get('data-src')
        if src:
            img_url = urllib.parse.urljoin(base_url, src)

            parsed_img_url = urllib.parse.urlparse(img_url)
            ext = os.path.splitext(parsed_img_url.path)[1]
            if not ext or ext.lower() not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                ext = '.jpg'

            filename = f"image_{i+1}{ext}"
            save_path = os.path.join(images_path, filename)

            img['src'] = f"./images/{filename}"
            download_tasks.append(download_image(session, img_url, save_path))

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

async def download_file(url, save_path):
    """Download arbitrary files (PDF, Word, Excel, etc.) via aiohttp."""
    try:
        async with aiohttp.ClientSession() as session:
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
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0
    return h.handle(html)

async def check_pause_stop(task_id: str) -> bool:
    """Returns True if the task should stop, False otherwise. Handles pausing."""
    events = task_events.get(task_id)
    if not events:
        return False

    if events['stop'].is_set():
        db_manager.update_task_status(task_id, 'stopped')
        return True

    if not events['pause'].is_set():
        db_manager.update_task_status(task_id, 'paused')
        await events['pause'].wait() # Wait until unpaused

        # Check if stopped while paused
        if events['stop'].is_set():
            db_manager.update_task_status(task_id, 'stopped')
            return True

        db_manager.update_task_status(task_id, 'running')

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
        page_path = os.path.join(base_path, safe_title)
        os.makedirs(page_path, exist_ok=True)
        save_path = os.path.join(page_path, filename)

        success = await download_file(current_url, save_path)
        if success:
            db_manager.mark_url_scraped(task_id, current_url, filename, page_path, 'article')
        else:
            db_manager.mark_url_failed(task_id, current_url, "File download failed")
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

        # Use commit to prevent timeouts on sites with persistent tracking scripts or broken resources
        try:
            await page.goto(current_url, wait_until="commit", timeout=60000)
            await page.wait_for_selector('body', state='attached', timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as goto_e:
            print(f"Warning: page.goto timeout or error for {current_url}: {goto_e}. Attempting to proceed with loaded content.")

        await scroll_page(page)
        html_content = await page.content()

        # Extract new links under the start root
        new_links = get_sub_domain_links(html_content, current_url, start_url)
        if new_links:
            db_manager.add_discovered_urls(task_id, current_url, new_links)

        # Extract robust content
        full_soup = BeautifulSoup(html_content, 'lxml')

        # Extract publish info BEFORE structural cleaning
        publish_info = extract_publish_info(full_soup)

        # 1. Try to get title from document or fallback to <title> tag
        title = full_soup.title.string if full_soup.title else "Untitled Page"
        try:
            cleaned_soup = clean_html_structure(BeautifulSoup(html_content, 'lxml'))
            cleaned_html = str(cleaned_soup)

            doc = Document(cleaned_html)
            title = doc.title() or title
            main_html = doc.summary()
            main_soup = BeautifulSoup(main_html, 'lxml')

            if len(main_soup.get_text(strip=True)) < 50:
                main_soup = cleaned_soup.find('body') or cleaned_soup
        except Exception:
            cleaned_soup = clean_html_structure(BeautifulSoup(html_content, 'lxml'))
            main_soup = cleaned_soup.find('body') or cleaned_soup

        page_path, tables_path, images_path = setup_page_directory(base_path, title)

        # 2. Extract tables from the MAIN content area
        process_tables(main_soup, tables_path)

        # 3. Extract images from the ENTIRE page
        body_soup = full_soup.find('body') or full_soup
        async with aiohttp.ClientSession() as session:
            await process_images(body_soup, current_url, images_path, session)

        # 4. Generate Markdown
        markdown_content = convert_to_markdown(str(main_soup))

        images_in_body = body_soup.find_all('img')
        if images_in_body:
            md_images = "\n\n### Page Images\n"
            for img in images_in_body:
                src = img.get('src')
                if src and src.startswith('./images/'):
                    md_images += f"![Image]({src})\n"
            markdown_content += md_images

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

        db_manager.mark_url_scraped(task_id, current_url, title, page_path, content_type)

    except Exception as page_e:
        import traceback
        traceback.print_exc()
        print(f"Error scraping {current_url}: {page_e}")
        db_manager.mark_url_failed(task_id, current_url, str(page_e))
    finally:
        if page:
            await page.close()
        if context:
            await context.close()

async def worker_task(task_id: str, start_url: str, base_path: str, browser, semaphore: asyncio.Semaphore):
    """A worker task that runs under a semaphore to limit concurrency."""
    async with semaphore:
        should_stop = await check_pause_stop(task_id)
        if should_stop:
            return False # Indicate stopping

        current_url = db_manager.get_and_lock_pending_url(task_id)
        if not current_url:
            return True # Queue empty for now

        # Add random delay to simulate human behavior and avoid WAF rate limiting
        delay = random.uniform(1.0, 3.0)
        await asyncio.sleep(delay)

        await process_single_url(task_id, current_url, start_url, base_path, browser)
        return True

async def crawl_worker(task_id: str, start_url: str, headless: bool = True):
    """Background manager that schedules concurrent workers."""

    init_task_events(task_id)
    db_manager.reset_processing_urls(task_id)

    base_path, base_domain = setup_base_directory(start_url)
    db_manager.create_task(task_id, start_url, start_url)

    # Strictly limit concurrency to 5 to prevent memory overload and aggressive IP blocks
    semaphore = asyncio.Semaphore(5)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"]
            )

            while True:
                # Check global stop/pause
                should_stop = await check_pause_stop(task_id)
                if should_stop:
                    break

                # If active count (pending + processing) is 0, we are completely done
                active_count = db_manager.get_active_count(task_id)
                if active_count == 0:
                    db_manager.update_task_status(task_id, 'completed')
                    break

                # Fetch up to 5 URLs to process concurrently
                tasks = []
                for _ in range(5):
                    tasks.append(asyncio.create_task(worker_task(task_id, start_url, base_path, browser, semaphore)))

                # Wait for this batch to complete
                results = await asyncio.gather(*tasks)

                if False in results: # A worker signaled to stop
                    break

    except Exception as e:
        print(f"Crawl fatally failed: {str(e)}")
        db_manager.update_task_status(task_id, 'failed')
    finally:
        db_manager.reset_processing_urls(task_id)
        if task_id in task_events:
            del task_events[task_id]
