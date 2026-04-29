import os
import re
import urllib.parse
from datetime import datetime
import asyncio
import pandas as pd
from playwright.async_api import async_playwright
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
    """Extract links that are sub-paths of the base_url to ensure we only crawl under the root dir."""
    soup = BeautifulSoup(html, 'lxml')
    links = []

    # Ensure base_url ends with '/' for proper prefix matching
    base_prefix = base_url if base_url.endswith('/') else base_url + '/'

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
            # Check if it's a sub-directory of the base root
            if full_url.startswith(base_prefix) or full_url == base_url:
                links.append(full_url)
    return list(set(links))

async def scroll_page(page):
    """Scroll down the page slowly to load dynamic content."""
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight / 5)")
        await page.wait_for_timeout(1000)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(1000)

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

async def crawl_worker(task_id: str, start_url: str, headless: bool = True):
    """Background worker that handles the smart crawling."""

    init_task_events(task_id)

    base_path, base_domain = setup_base_directory(start_url)

    # Save the base folder path so we know where to save
    db_manager.create_task(task_id, start_url, start_url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True
        )
        page = await context.new_page()

        try:
            while True:
                # Check for pause/stop signals before fetching the next URL
                should_stop = await check_pause_stop(task_id)
                if should_stop:
                    break

                current_url = db_manager.get_pending_url(task_id)
                if not current_url:
                    # No more URLs to crawl
                    db_manager.update_task_status(task_id, 'completed')
                    break

                try:
                    try:
                        # Use commit to prevent timeouts on sites with persistent tracking scripts or broken resources
                        await page.goto(current_url, wait_until="commit", timeout=60000)

                        # Wait for body to ensure the DOM is at least partially ready
                        await page.wait_for_selector('body', state='attached', timeout=30000)

                        # Wait a short moment for dynamic scripts to render content
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

                    # 1. Try to get title from document or fallback to <title> tag
                    title = full_soup.title.string if full_soup.title else "Untitled Page"
                    try:
                        doc = Document(html_content)
                        title = doc.title() or title
                        main_html = doc.summary()
                        main_soup = BeautifulSoup(main_html, 'lxml')

                        # Fallback if readability library stripped too much (e.g., node pages)
                        if len(main_soup.get_text(strip=True)) < 50:
                            main_soup = full_soup.find('body') or full_soup
                    except Exception:
                        main_soup = full_soup.find('body') or full_soup

                    page_path, tables_path, images_path = setup_page_directory(base_path, title)

                    # 2. Extract tables from the MAIN content area
                    process_tables(main_soup, tables_path)

                    # 3. Extract images from the ENTIRE page (or a broad wrapper like body/main)
                    # to ensure we don't miss image-only posts that readability might filter out.
                    body_soup = full_soup.find('body') or full_soup
                    async with aiohttp.ClientSession() as session:
                        await process_images(body_soup, current_url, images_path, session)

                    # 4. Generate Markdown. Combine robust readability text with ALL images to ensure no loss.
                    markdown_content = convert_to_markdown(str(main_soup))

                    # If readability missed images, append the ones we found in body
                    images_in_body = body_soup.find_all('img')
                    if images_in_body:
                        md_images = "\n\n### Page Images\n"
                        for img in images_in_body:
                            src = img.get('src')
                            if src and src.startswith('./images/'):
                                md_images += f"![Image]({src})\n"
                        markdown_content += md_images

                    # Final markdown formatting
                    markdown_content = f"# {title}\n\n**Source URL:** {current_url}\n\n---\n\n{markdown_content}"

                    # Content heuristic: differentiate between 'node' (navigational) and 'article' (substantive)
                    # We can use text length of the extracted readable content as a primary heuristic.
                    # Alternatively, if there are many links relative to the text length, it's likely a node.
                    # We'll use a simple threshold on the raw markdown length (excluding the boilerplate we added).
                    clean_md_len = len(markdown_content) - len(f"# {title}\n\n**Source URL:** {current_url}\n\n---\n\n")

                    content_type = 'article'
                    if clean_md_len < 300: # Adjust threshold as needed
                        content_type = 'node'

                    # Save markdown even for nodes, but we might want to optionally skip it in future.
                    md_path = os.path.join(page_path, "content.md")
                    with open(md_path, 'w', encoding='utf-8') as f:
                        f.write(markdown_content)

                    # Mark success with content type
                    db_manager.mark_url_scraped(task_id, current_url, title, page_path, content_type)

                except Exception as page_e:
                    print(f"Error scraping {current_url}: {page_e}")
                    db_manager.mark_url_failed(task_id, current_url, str(page_e))

        except Exception as e:
            print(f"Crawl fatally failed: {str(e)}")
            db_manager.update_task_status(task_id, 'failed')
        finally:
            await browser.close()
            # Cleanup events
            if task_id in task_events:
                del task_events[task_id]
