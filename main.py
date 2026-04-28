from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import asyncio
import os
import webbrowser
import threading
import uvicorn
from scraper import scrape_url

app = FastAPI(title="Web Scraper Client")

# Mount static files for HTML/JS
app.mount("/static", StaticFiles(directory="static"), name="static")

class ScrapeRequest(BaseModel):
    url: str
    show_browser: bool = True

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/scrape")
async def start_scraping(request: ScrapeRequest):
    try:
        # Start the scraping process
        result = await scrape_url(request.url, headless=not request.show_browser)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def open_browser():
    """Wait a second for the server to start, then open the browser."""
    import time
    time.sleep(1)
    webbrowser.open('http://127.0.0.1:8000')

if __name__ == "__main__":
    # Ensure directories exist
    os.makedirs("static", exist_ok=True)
    os.makedirs("scraped_data", exist_ok=True)

    # Start a thread to open the browser automatically
    threading.Thread(target=open_browser, daemon=True).start()

    # Start the FastAPI server
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
