from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uuid
import os
import webbrowser
import threading
import uvicorn
import asyncio
import sys

import db_manager
from scraper import crawl_worker, task_events

# Support for PyInstaller paths
if getattr(sys, 'frozen', False):
    # If the application is run as a bundle, the PyInstaller bootloader
    # extends the sys module by a flag frozen=True and sets the app
    # path into variable _MEIPASS'.
    application_path = sys._MEIPASS
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

static_dir = os.path.join(application_path, "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # This runs when the app starts up
    # Pre-install browsers before launching the UI so it doesn't fail when hitting start
    install_playwright_browsers()

    # Spawn the browser asynchronously after startup
    import asyncio
    async def open_browser_async():
        await asyncio.sleep(0.5) # Slight delay to let uvicorn print its startup message
        webbrowser.open('http://127.0.0.1:8000')

    asyncio.create_task(open_browser_async())

    yield
    # This runs when the app shuts down
    pass

app = FastAPI(title="Web Scraper Client", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

class ScrapeRequest(BaseModel):
    url: str
    crawl_scope: str = "subpages"
    show_browser: bool = True
    update_data: bool = False
    crawl_scope: str = "subpages" # "subpages" or "all_site"

@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = os.path.join(static_dir, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/console", response_class=HTMLResponse)
async def get_console():
    console_path = os.path.join(static_dir, "console.html")
    with open(console_path, "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/scrape/start")
async def start_scraping(request: ScrapeRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, request.url))

    if request.update_data:
        await asyncio.to_thread(db_manager.clear_task_data, task_id)

    task = await asyncio.to_thread(db_manager.get_task, task_id)
    if not task:
        await asyncio.to_thread(db_manager.create_task, task_id, request.url, request.url, request.crawl_scope)
    else:
        # Update the crawl_scope if it's an existing task being resumed/updated
        await asyncio.to_thread(db_manager.update_task_crawl_scope, task_id, request.crawl_scope)

    # If the task is already running in memory, don't start a new one
    if task_id in task_events and not task_events[task_id]['stop'].is_set():
         return {"task_id": task_id, "status": "already running or paused"}

    # Make sure status is set to running
    await asyncio.to_thread(db_manager.update_task_status, task_id, "running")

    background_tasks.add_task(
        crawl_worker,
        task_id,
        request.url,
        not request.show_browser
    )

    return {"task_id": task_id, "status": "started"}

@app.get("/api/scrape/status/{task_id}")
async def get_scraping_status(task_id: str):
    task = await asyncio.to_thread(db_manager.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # In concurrent mode, "current" isn't just one pending, it's multiple processing
    # Let's just return a count of active links or a general label
    active_count = await asyncio.to_thread(db_manager.get_active_count, task_id)

    return {
        "status": task['status'],
        "pages_scraped": task['total_scraped'],
        "current_url": f"{active_count} 个页面正在队列中...",
        "is_running": task['status'] == 'running'
    }

@app.get("/api/scrape/tree/{task_id}")
async def get_scrape_tree(task_id: str):
    tree_data = await asyncio.to_thread(db_manager.get_url_tree, task_id)
    return {"tree": tree_data}

@app.post("/api/scrape/pause/{task_id}")
async def pause_scraping(task_id: str):
    if task_id in task_events:
        task_events[task_id]['pause'].clear()
        await asyncio.to_thread(db_manager.update_task_status, task_id, "paused")
    return {"status": "paused"}

@app.post("/api/scrape/resume/{task_id}")
async def resume_scraping(task_id: str):
    if task_id in task_events:
        task_events[task_id]['pause'].set()
        await asyncio.to_thread(db_manager.update_task_status, task_id, "running")
    else:
        # Task may have fully stopped, so we can't just resume, we must restart the worker loop via /start
        pass
    return {"status": "resumed"}

@app.post("/api/scrape/stop/{task_id}")
async def stop_scraping(task_id: str):
    if task_id in task_events:
        task_events[task_id]['stop'].set()
        # Unpause in case it's paused so it can process the stop signal
        task_events[task_id]['pause'].set()
    await asyncio.to_thread(db_manager.update_task_status, task_id, "stopped")
    return {"status": "stopped"}


def install_playwright_browsers():
    """Ensure playwright browsers are installed before starting."""
    print("Checking/installing Playwright browsers...")
    # This environment variable forces Playwright to install and look for browsers
    # in the local folder structure, rather than a global appdata folder which might
    # fail or be hidden when running as a PyInstaller executable.
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

    # Check if we're running as a frozen executable
    if getattr(sys, 'frozen', False):
        print("Running as a frozen executable. Skipping automatic playwright install since sys.executable points to this executable.")
        return

    try:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        print("Playwright browsers ready.")
    except Exception as e:
        print(f"Warning: Failed to install playwright browsers automatically. Error: {e}")

if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    os.makedirs("scraped_data", exist_ok=True)

    # We pass the app object directly rather than a string "main:app"
    # because string references often fail when packaged by PyInstaller.
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
