"""
DeepSeek-powered site structure analyzer.
Identifies CSS selectors for content, date, and title — ONCE per domain, then cached.
"""

import re
import json
import httpx

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

ANALYSIS_PROMPT = """You are analyzing a webpage HTML to identify CSS selectors for key content elements.

Analyze the HTML below and return a JSON object with:
- "content_selector": CSS selector for the MAIN article/post body text (primary readable content)
- "date_selector": CSS selector for the publication/post date element (or datetime attribute holder)
- "title_selector": CSS selector for the article/page title (not the site brand/logo title)

Rules:
- Use specific, stable class-based or attribute selectors likely to work across all pages of this site
- Return null for any field you cannot confidently determine
- Do NOT select navigation, headers, footers, sidebars, pagination, or ads
- Prefer the most content-rich element available

Return ONLY valid JSON, nothing else. Example:
{"content_selector": "div.article-content", "date_selector": "time.publish-date", "title_selector": "h1.entry-title"}

HTML (site sample):
"""


async def validate_api_key(api_key: str) -> bool:
    """Validate a DeepSeek API key via the /models endpoint (no token cost)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{DEEPSEEK_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"}
            )
            return r.status_code == 200
    except Exception:
        return False


def _clean_html_for_analysis(html: str) -> str:
    """Strip scripts/styles and truncate for sending to DeepSeek."""
    # Remove script and style blocks
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    # Collapse whitespace
    html = re.sub(r'\s+', ' ', html)
    # Truncate to ~10 000 chars — enough for structural analysis
    return html[:10000]


async def analyze_site_structure(html: str, api_key: str) -> dict:
    """
    Send a cleaned HTML sample to DeepSeek and extract CSS selectors.
    Returns {"content_selector", "date_selector", "title_selector"} — each may be None.
    """
    cleaned = _clean_html_for_analysis(html)
    prompt = ANALYSIS_PROMPT + cleaned

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "You are an HTML structure analyst. Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": 0.0,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()

        # Extract JSON object from response (handle markdown code fences)
        json_match = re.search(r'\{.*?\}', content, re.DOTALL)
        if json_match:
            raw = json.loads(json_match.group())
            return {
                "content_selector": raw.get("content_selector") or None,
                "date_selector": raw.get("date_selector") or None,
                "title_selector": raw.get("title_selector") or None,
            }
    except Exception as e:
        print(f"[analyzer] DeepSeek analysis failed: {e}")

    return {"content_selector": None, "date_selector": None, "title_selector": None}
