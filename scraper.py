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

class ScraperStatus:
    def __init__(self):
        self.visited_urls = set()
        self.queue = []
        self.pages_scraped = 0
        self.current_url = ""
        self.status_message = "Initializing..."
        self.is_running = True
        self.has_error = False
        self.error_message = ""
        self.base_domain = ""

scraper_status_store = {}

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

def get_same_domain_links(html, base_url, base_domain):
    """Extract valid links that belong to the same domain."""
    soup = BeautifulSoup(html, 'lxml')
    links = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        full_url = urllib.parse.urljoin(base_url, href)
        # Remove fragment
        full_url = urllib.parse.urldefrag(full_url)[0]

        # Check if same domain and http/https
        parsed_url = urllib.parse.urlparse(full_url)
        if parsed_url.scheme in ['http', 'https']:
            url_domain = parsed_url.netloc.replace("www.", "")
            if base_domain in url_domain:
                links.append(full_url)
    return list(set(links))

async def scroll_page(page):
    """Scroll down the page slowly to load dynamic content."""
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight / 5)")
        await page.wait_for_timeout(1000)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(1000)

import io

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

async def crawl_worker(task_id, start_url, max_pages, max_depth, headless):
    """Background worker that handles the BFS crawling."""
    status = ScraperStatus()
    scraper_status_store[task_id] = status

    base_path, base_domain = setup_base_directory(start_url)
    status.base_domain = base_domain

    # queue stores tuples of (url, current_depth)
    status.queue.append((start_url, 0))
    status.visited_urls.add(start_url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            while status.queue and status.pages_scraped < max_pages:
                current_url, current_depth = status.queue.pop(0)
                status.current_url = current_url
                status.status_message = f"Scraping [{status.pages_scraped + 1}/{max_pages}]: {current_url}"
                print(status.status_message)

                try:
                    await page.goto(current_url, wait_until="networkidle", timeout=60000)
                    await scroll_page(page)
                    html_content = await page.content()

                    # Add new links to queue if depth allows
                    if current_depth < max_depth:
                        new_links = get_same_domain_links(html_content, current_url, base_domain)
                        for link in new_links:
                            if link not in status.visited_urls:
                                status.visited_urls.add(link)
                                status.queue.append((link, current_depth + 1))

                    # Use readability to extract MAIN article content
                    doc = Document(html_content)
                    title = doc.title()
                    main_html = doc.summary()

                    # Clean up HTML and extract components
                    soup = BeautifulSoup(main_html, 'lxml')

                    # Create directory for this specific page
                    page_path, tables_path, images_path = setup_page_directory(base_path, title)

                    process_tables(soup, tables_path)

                    async with aiohttp.ClientSession() as session:
                        await process_images(soup, current_url, images_path, session)

                    markdown_content = convert_to_markdown(str(soup))

                    # Also append the source URL to the markdown
                    markdown_content = f"# {title}\n\n**Source URL:** {current_url}\n\n---\n\n{markdown_content}"

                    md_path = os.path.join(page_path, "content.md")
                    with open(md_path, 'w', encoding='utf-8') as f:
                        f.write(markdown_content)

                    status.pages_scraped += 1

                except Exception as page_e:
                    print(f"Error scraping {current_url}: {page_e}")
                    # Continue to next url on page error
                    continue

            status.status_message = f"Completed successfully! Scraped {status.pages_scraped} pages."

        except Exception as e:
            status.has_error = True
            status.error_message = str(e)
            status.status_message = f"Crawl failed: {str(e)}"
        finally:
            status.is_running = False
            status.current_url = base_path # Store final path here for the client
            await browser.close()
