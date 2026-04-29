from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uuid
import os
import webbrowser
import threading
import uvicorn
from scraper import crawl_worker, scraper_status_store

app = FastAPI(title="Web Scraper Client")

# Mount static files for HTML/JS
app.mount("/static", StaticFiles(directory="static"), name="static")

class ScrapeRequest(BaseModel):
    url: str
    show_browser: bool = True
    max_pages: int = 10
    max_depth: int = 1

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/scrape/start")
async def start_scraping(request: ScrapeRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())

    # Start the worker in the background
    background_tasks.add_task(
        crawl_worker,
        task_id,
        request.url,
        request.max_pages,
        request.max_depth,
        not request.show_browser
    )

    return {"task_id": task_id, "status": "started"}

@app.get("/api/scrape/status/{task_id}")
async def get_scraping_status(task_id: str):
    if task_id not in scraper_status_store:
        raise HTTPException(status_code=404, detail="Task not found")

    status = scraper_status_store[task_id]

    return {
        "is_running": status.is_running,
        "pages_scraped": status.pages_scraped,
        "current_url": status.current_url,
        "status_message": status.status_message,
        "has_error": status.has_error,
        "error_message": status.error_message
    }

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
