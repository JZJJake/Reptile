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

import db_manager
from scraper import crawl_worker, task_events

app = FastAPI(title="Web Scraper Client")

app.mount("/static", StaticFiles(directory="static"), name="static")

class ScrapeRequest(BaseModel):
    url: str
    show_browser: bool = True
    update_data: bool = False

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/console", response_class=HTMLResponse)
async def get_console():
    with open("static/console.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/scrape/start")
async def start_scraping(request: ScrapeRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, request.url))

    if request.update_data:
        db_manager.clear_task_data(task_id)

    task = db_manager.get_task(task_id)
    if not task:
        db_manager.create_task(task_id, request.url, request.url)

    # If the task is already running in memory, don't start a new one
    if task_id in task_events and not task_events[task_id]['stop'].is_set():
         return {"task_id": task_id, "status": "already running or paused"}

    # Make sure status is set to running
    db_manager.update_task_status(task_id, "running")

    background_tasks.add_task(
        crawl_worker,
        task_id,
        request.url,
        not request.show_browser
    )

    return {"task_id": task_id, "status": "started"}

@app.get("/api/scrape/status/{task_id}")
async def get_scraping_status(task_id: str):
    task = db_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # In concurrent mode, "current" isn't just one pending, it's multiple processing
    # Let's just return a count of active links or a general label
    active_count = db_manager.get_active_count(task_id)

    return {
        "status": task['status'],
        "pages_scraped": task['total_scraped'],
        "current_url": f"{active_count} 个页面正在队列中...",
        "is_running": task['status'] == 'running'
    }

@app.get("/api/scrape/tree/{task_id}")
async def get_scrape_tree(task_id: str):
    tree_data = db_manager.get_url_tree(task_id)
    return {"tree": tree_data}

@app.post("/api/scrape/pause/{task_id}")
async def pause_scraping(task_id: str):
    if task_id in task_events:
        task_events[task_id]['pause'].clear()
        db_manager.update_task_status(task_id, "paused")
    return {"status": "paused"}

@app.post("/api/scrape/resume/{task_id}")
async def resume_scraping(task_id: str):
    if task_id in task_events:
        task_events[task_id]['pause'].set()
        db_manager.update_task_status(task_id, "running")
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
    db_manager.update_task_status(task_id, "stopped")
    return {"status": "stopped"}


def open_browser():
    """Wait a second for the server to start, then open the browser."""
    import time
    time.sleep(1)
    webbrowser.open('http://127.0.0.1:8000')

if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    os.makedirs("scraped_data", exist_ok=True)

    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
