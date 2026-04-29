import re

with open('scraper.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace the part inside the while loop where we extract content
old_extract_logic = """                    # Extract content
                    doc = Document(html_content)
                    title = doc.title()
                    main_html = doc.summary()
                    soup = BeautifulSoup(main_html, 'lxml')

                    page_path, tables_path, images_path = setup_page_directory(base_path, title)

                    process_tables(soup, tables_path)

                    async with aiohttp.ClientSession() as session:
                        await process_images(soup, current_url, images_path, session)

                    markdown_content = convert_to_markdown(str(soup))
                    markdown_content = f"# {title}\\n\\n**Source URL:** {current_url}\\n\\n---\\n\\n{markdown_content}"

                    md_path = os.path.join(page_path, "content.md")
                    with open(md_path, 'w', encoding='utf-8') as f:
                        f.write(markdown_content)"""

new_extract_logic = """                    # Extract robust content
                    full_soup = BeautifulSoup(html_content, 'lxml')

                    # 1. Try to get title from document or fallback to <title> tag
                    try:
                        doc = Document(html_content)
                        title = doc.title()
                        main_html = doc.summary()
                        main_soup = BeautifulSoup(main_html, 'lxml')
                    except Exception:
                        title = full_soup.title.string if full_soup.title else "Untitled Page"
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
                        md_images = "\\n\\n### Page Images\\n"
                        for img in images_in_body:
                            src = img.get('src')
                            if src and src.startswith('./images/'):
                                md_images += f"![Image]({src})\\n"
                        markdown_content += md_images

                    # Final markdown formatting
                    markdown_content = f"# {title}\\n\\n**Source URL:** {current_url}\\n\\n---\\n\\n{markdown_content}"

                    md_path = os.path.join(page_path, "content.md")
                    with open(md_path, 'w', encoding='utf-8') as f:
                        f.write(markdown_content)"""

if old_extract_logic in code:
    code = code.replace(old_extract_logic, new_extract_logic)
    with open('scraper.py', 'w', encoding='utf-8') as f:
        f.write(code)
    print("Scraper successfully patched for robust image and text extraction.")
else:
    print("Could not find the target code block to patch.")
