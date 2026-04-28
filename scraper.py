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

def sanitize_filename(url):
    """Sanitize URL to create a safe folder name."""
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    safe_domain = re.sub(r'[^a-zA-Z0-9]', '_', domain)
    return safe_domain

def setup_directories(url):
    """Create directory structure for the scraped data."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_domain = sanitize_filename(url)
    folder_name = f"{timestamp}_{safe_domain}"
    base_path = os.path.join(os.getcwd(), "scraped_data", folder_name)

    tables_path = os.path.join(base_path, "tables")
    images_path = os.path.join(base_path, "images")

    os.makedirs(base_path, exist_ok=True)
    os.makedirs(tables_path, exist_ok=True)
    os.makedirs(images_path, exist_ok=True)

    return base_path, tables_path, images_path

async def download_image(img_url, save_path):
    """Download an image asynchronously."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(img_url, timeout=10) as response:
                if response.status == 200:
                    content = await response.read()
                    with open(save_path, 'wb') as f:
                        f.write(content)
                    return True
    except Exception as e:
        print(f"Error downloading {img_url}: {e}")
    return False

async def scrape_url(url: str, headless: bool = True):
    """
    Scrape the given URL for text, tables, and images.
    If headless=False, the browser window will be visible.
    """
    base_path, tables_path, images_path = setup_directories(url)

    async with async_playwright() as p:
        # Launch browser
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # Go to URL and wait until network is mostly idle
            await page.goto(url, wait_until="networkidle", timeout=60000)

            # Scroll down the page slowly to trigger lazy-loaded images
            await scroll_page(page)

            # Get final HTML content
            html_content = await page.content()

            # Process HTML using BeautifulSoup
            soup = BeautifulSoup(html_content, 'lxml')

            # 1. Process and save tables
            process_tables(soup, tables_path)

            # 2. Process and save images
            await process_images(soup, url, images_path)

            # 3. Convert remaining HTML to Markdown
            markdown_content = convert_to_markdown(str(soup), url)

            # Save markdown content
            md_path = os.path.join(base_path, "content.md")
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)

            result_msg = f"Successfully scraped {url}. Data saved to {base_path}"
            return {"status": "success", "message": result_msg, "path": base_path}

        except Exception as e:
            return {"status": "error", "message": f"Error scraping {url}: {str(e)}"}
        finally:
            await browser.close()

async def scroll_page(page):
    """Scroll down the page slowly to load dynamic content."""
    # Scroll down multiple times
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight / 5)")
        await page.wait_for_timeout(1000)
    # Scroll back to top
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(1000)

def process_tables(soup, tables_path):
    """Extract tables and save as CSV."""
    tables = soup.find_all('table')
    for i, table in enumerate(tables):
        try:
            # Convert HTML table to pandas DataFrame
            dfs = pd.read_html(str(table))
            if dfs:
                df = dfs[0]
                csv_path = os.path.join(tables_path, f"table_{i+1}.csv")
                df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        except Exception as e:
            print(f"Error processing table {i+1}: {e}")

async def process_images(soup, base_url, images_path):
    """Extract image URLs, download them, and update src in HTML."""
    images = soup.find_all('img')
    download_tasks = []

    for i, img in enumerate(images):
        src = img.get('src') or img.get('data-src')
        if src:
            # Resolve relative URLs
            img_url = urllib.parse.urljoin(base_url, src)

            # Extract extension or default to .jpg
            parsed_img_url = urllib.parse.urlparse(img_url)
            ext = os.path.splitext(parsed_img_url.path)[1]
            if not ext or ext.lower() not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                ext = '.jpg'

            filename = f"image_{i+1}{ext}"
            save_path = os.path.join(images_path, filename)

            # Update the src in HTML to point to local relative path for markdown
            img['src'] = f"./images/{filename}"

            # Prepare download task
            download_tasks.append(download_image(img_url, save_path))

    # Download images concurrently
    if download_tasks:
        await asyncio.gather(*download_tasks)

def convert_to_markdown(html, base_url):
    """Convert HTML to clean Markdown text."""
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0 # Don't wrap text
    # h.baseurl = base_url # We remove this so it doesn't overwrite our local relative paths

    return h.handle(html)

if __name__ == "__main__":
    # Test script
    test_url = "https://example.com"
    print(f"Running test scrape on {test_url}")
    result = asyncio.run(scrape_url(test_url, headless=True))
    print(result)
