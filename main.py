from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.routing import APIRouter
from pydantic import BaseModel
import uuid
import os
import webbrowser
import uvicorn
import asyncio
import sys
from typing import Optional

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
    show_browser: bool = True
    update_data: bool = False
    text_only: bool = False
    date_filter: bool = False
    update_mode: bool = False

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

@app.get("/wiki", response_class=HTMLResponse)
async def get_wiki():
    wiki_path = os.path.join(static_dir, "wiki.html")
    with open(wiki_path, "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/scrape/start")
async def start_scraping(request: ScrapeRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, request.url))

    if request.update_data:
        await asyncio.to_thread(db_manager.clear_task_data, task_id)

    task = await asyncio.to_thread(db_manager.get_task, task_id)
    if not task:
        await asyncio.to_thread(db_manager.create_task, task_id, request.url, request.url)

    # If the task is already running in memory, don't start a new one
    if task_id in task_events and not task_events[task_id]['stop'].is_set():
         return {"task_id": task_id, "status": "already running or paused"}

    # Make sure status is set to running
    await asyncio.to_thread(db_manager.update_task_status, task_id, "running")

    background_tasks.add_task(
        crawl_worker,
        task_id,
        request.url,
        not request.show_browser,
        request.text_only,
        request.date_filter,
        request.update_mode,
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


# ---------------------------------------------------------------------------
# Wiki Router
# ---------------------------------------------------------------------------

wiki_router = APIRouter(prefix="/api/wiki", tags=["wiki"])

class WikiQueryRequest(BaseModel):
    question: str
    domain: Optional[str] = None
    stream: bool = True
    save_answer: bool = False

class WikiIndexRequest(BaseModel):
    domain: str
    batch_size: int = 5
    rebuild: bool = False

@wiki_router.get("/domains")
async def wiki_domains():
    """List all domains that have an active wiki."""
    wiki_base = os.path.join(os.getcwd(), "wiki")
    if not os.path.isdir(wiki_base):
        return {"domains": []}
    domains = [
        d for d in os.listdir(wiki_base)
        if os.path.isdir(os.path.join(wiki_base, d))
    ]
    return {"domains": sorted(domains)}

@wiki_router.get("/pages/{domain}")
async def wiki_pages(domain: str):
    """List all wiki pages for a domain."""
    from pathlib import Path
    wiki_path = Path(os.getcwd()) / "wiki" / domain
    if not wiki_path.is_dir():
        raise HTTPException(status_code=404, detail="Domain wiki not found")
    pages = [
        str(p.relative_to(wiki_path))
        for p in wiki_path.rglob("*.md")
    ]
    return {"domain": domain, "pages": sorted(pages)}

@wiki_router.get("/page/{domain}/{path:path}")
async def wiki_page(domain: str, path: str):
    """Read a specific wiki page."""
    wiki_path = os.path.join(os.getcwd(), "wiki", domain, path)
    if not os.path.isfile(wiki_path):
        raise HTTPException(status_code=404, detail="Page not found")
    with open(wiki_path, encoding="utf-8") as f:
        content = f.read()
    return {"domain": domain, "path": path, "content": content}

@wiki_router.get("/status/{domain}")
async def wiki_status(domain: str):
    """Return wiki stats for a domain."""
    from pathlib import Path
    wiki_path = Path(os.getcwd()) / "wiki" / domain
    if not wiki_path.is_dir():
        return {"domain": domain, "exists": False, "page_count": 0}
    pages = list(wiki_path.rglob("*.md"))
    log_path = wiki_path / "log.md"
    last_op = None
    if log_path.is_file():
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        if lines:
            last_op = lines[-1]
    return {
        "domain": domain,
        "exists": True,
        "page_count": len(pages),
        "last_operation": last_op,
    }

@wiki_router.post("/ingest")
async def wiki_ingest(request: WikiIndexRequest, background_tasks: BackgroundTasks):
    """Trigger background ingestion of crawled docs into the wiki."""
    from wiki.wiki_manager import WikiManager
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_API_KEY not set")

    manager = WikiManager(request.domain, api_key)

    async def run_ingest():
        result = await manager.ingest(batch_size=request.batch_size)
        db_manager.log_wiki_operation(request.domain, "ingest", result)

    background_tasks.add_task(run_ingest)
    return {"status": "ingest started", "domain": request.domain}

@wiki_router.post("/query")
async def wiki_query(request: WikiQueryRequest):
    """Ask a question answered from the wiki. Supports SSE streaming."""
    from wiki.wiki_manager import WikiManager
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_API_KEY not set")

    domains: list[str] = []
    if request.domain:
        domains = [request.domain]
    else:
        wiki_base = os.path.join(os.getcwd(), "wiki")
        if os.path.isdir(wiki_base):
            domains = [d for d in os.listdir(wiki_base) if os.path.isdir(os.path.join(wiki_base, d))]

    if not domains:
        raise HTTPException(status_code=404, detail="No wiki domains found. Run ingest first.")

    managers = [WikiManager(d, api_key) for d in domains]

    if request.stream:
        async def event_stream():
            for manager in managers:
                async for chunk in manager.query(request.question, stream=True,
                                                  save_answer=request.save_answer):
                    yield f"data: {chunk}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")
    else:
        # Non-streaming: collect full answer
        parts = []
        for manager in managers:
            answer = await manager.query(request.question, stream=False,
                                          save_answer=request.save_answer)
            parts.append(answer)
        return {"answer": "\n\n".join(parts)}

@wiki_router.post("/lint")
async def wiki_lint(domain: str, background_tasks: BackgroundTasks):
    """Trigger background lint/health-check of a wiki domain."""
    from wiki.wiki_manager import WikiManager
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="DEEPSEEK_API_KEY not set")

    manager = WikiManager(domain, api_key)

    async def run_lint():
        result = await manager.lint()
        db_manager.log_wiki_operation(domain, "lint", result)

    background_tasks.add_task(run_lint)
    return {"status": "lint started", "domain": domain}

app.include_router(wiki_router)


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
