import asyncio
import json
import os
import sys
import uuid
import uvicorn

from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.routing import APIRouter
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager

import db_manager
from scraper import crawl_worker, task_events, task_log_queues, create_log_queue
from site_analyzer import validate_api_key

# ── PyInstaller path resolution ───────────────────────────────────────────────

if getattr(sys, 'frozen', False):
    application_path = sys._MEIPASS
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

static_dir = os.path.join(application_path, "static")
os.makedirs(static_dir, exist_ok=True)

# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    install_playwright_browsers()
    yield

app = FastAPI(title="Reptile Knowledge Crawler", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ── Helper: read static HTML ─────────────────────────────────────────────────

def _read_html(filename: str) -> str:
    path = os.path.join(static_dir, filename)
    with open(path, encoding="utf-8") as f:
        return f.read()

# ── Page routes ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def get_login():
    return _read_html("login.html")

@app.get("/app", response_class=HTMLResponse)
async def get_app():
    return _read_html("index.html")

@app.get("/wiki", response_class=HTMLResponse)
async def get_wiki():
    return _read_html("wiki.html")

# ── Auth ─────────────────────────────────────────────────────────────────────

class AuthRequest(BaseModel):
    api_key: str

@app.post("/api/auth/validate")
async def auth_validate(req: AuthRequest):
    """Validate a DeepSeek API key. Returns {valid: bool}."""
    valid = await validate_api_key(req.api_key)
    return {"valid": valid}

# ── Scrape API ───────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str
    api_key: str
    update_data: bool = False   # clear DB and re-crawl from scratch
    update_mode: bool = False   # iterative: skip unchanged pages

@app.post("/api/scrape/start")
async def start_scraping(req: ScrapeRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, req.url))

    if req.update_data:
        await asyncio.to_thread(db_manager.clear_task_data, task_id)

    task = await asyncio.to_thread(db_manager.get_task, task_id)
    if not task:
        await asyncio.to_thread(db_manager.create_task, task_id, req.url, req.url)

    if task_id in task_events and not task_events[task_id]['stop'].is_set():
        return {"task_id": task_id, "status": "already_running"}

    await asyncio.to_thread(db_manager.update_task_status, task_id, "running")

    # Create SSE queue BEFORE starting background task
    create_log_queue(task_id)

    background_tasks.add_task(
        crawl_worker,
        task_id,
        req.url,
        req.api_key,
        req.update_mode,
    )

    return {"task_id": task_id, "status": "started"}

@app.get("/api/scrape/events/{task_id}")
async def scrape_events(task_id: str):
    """SSE stream of crawl log messages for the given task."""
    q = task_log_queues.get(task_id)
    if q is None:
        # Create a placeholder queue in case client connects before worker starts
        q = create_log_queue(task_id)

    async def generator():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=25.0)
                    if item is None:
                        # Sentinel: task finished
                        yield "data: null\n\n"
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {\"type\":\"ping\"}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )

@app.get("/api/scrape/status/{task_id}")
async def get_scraping_status(task_id: str):
    task = await asyncio.to_thread(db_manager.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    active_count = await asyncio.to_thread(db_manager.get_active_count, task_id)
    return {
        "status": task['status'],
        "pages_scraped": task['total_scraped'],
        "pages_queued": active_count,
        "is_running": task['status'] == 'running',
    }

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
    return {"status": "resumed"}

@app.post("/api/scrape/stop/{task_id}")
async def stop_scraping(task_id: str):
    if task_id in task_events:
        task_events[task_id]['stop'].set()
        task_events[task_id]['pause'].set()
    await asyncio.to_thread(db_manager.update_task_status, task_id, "stopped")
    return {"status": "stopped"}

@app.get("/api/scrape/active")
async def get_active_tasks():
    """Return currently running tasks (used to disable the wiki build button)."""
    tasks = await asyncio.to_thread(db_manager.get_active_tasks)
    return {"active": tasks, "count": len(tasks)}

@app.get("/api/scrape/domains")
async def get_scraped_domains():
    """List domains that have scraped data."""
    data_dir = os.path.join(os.getcwd(), "scraped_data")
    if not os.path.isdir(data_dir):
        return {"domains": []}
    domains = [
        d for d in os.listdir(data_dir)
        if os.path.isdir(os.path.join(data_dir, d))
    ]
    result = []
    for d in sorted(domains):
        domain_path = os.path.join(data_dir, d)
        files = [f for f in os.listdir(domain_path) if f.endswith('.md')]
        result.append({"name": d, "file_count": len(files)})
    return {"domains": result}

# ── Wiki API ─────────────────────────────────────────────────────────────────

wiki_router = APIRouter(prefix="/api/wiki", tags=["wiki"])

class WikiBuildRequest(BaseModel):
    domain: str
    api_key: str
    batch_size: int = 5

class WikiQueryRequest(BaseModel):
    question: str
    domain: Optional[str] = None
    api_key: str
    stream: bool = True

def _wiki_queue_id(domain: str) -> str:
    return f"wiki::{domain}"

# Domains currently being built — prevents duplicate/overlapping builds.
_wiki_building: set = set()

@wiki_router.get("/building")
async def wiki_building_status():
    """Return the set of domains currently being built (for UI button gating)."""
    return {"building": sorted(_wiki_building)}

@wiki_router.post("/build")
async def wiki_build(req: WikiBuildRequest, background_tasks: BackgroundTasks):
    """Build or update the wiki for a domain. Streams progress via /api/wiki/events."""
    # Check no crawl is running
    active = await asyncio.to_thread(db_manager.get_active_tasks)
    if active:
        raise HTTPException(status_code=409,
                            detail="爬虫正在运行，请等待爬取完成后再建设知识库")

    # Guard against duplicate builds of the same domain
    if req.domain in _wiki_building:
        raise HTTPException(status_code=409,
                            detail="该知识库正在建设中，请勿重复点击")

    from scraper import push_status, create_log_queue
    from wiki.wiki_manager import WikiManager

    qid = _wiki_queue_id(req.domain)
    q = create_log_queue(qid)   # create BEFORE returning so SSE can attach
    _wiki_building.add(req.domain)

    async def run_build():
        def progress(msg, mtype="log"):
            push_status(qid, msg, mtype)
        try:
            push_status(qid, f"开始建设知识库：{req.domain}", "info")
            mgr = WikiManager(req.domain, req.api_key)
            result = await mgr.ingest(batch_size=req.batch_size, progress=progress)
            db_manager.log_wiki_operation(req.domain, "build", result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            push_status(qid, f"知识库建设失败：{e}", "error")
            db_manager.log_wiki_operation(req.domain, "build_error", {"error": str(e)})
        finally:
            _wiki_building.discard(req.domain)
            # sentinel: tell SSE stream to close
            try:
                q.put_nowait(None)
            except Exception:
                pass

    background_tasks.add_task(run_build)
    return {"status": "building", "domain": req.domain}

@wiki_router.get("/events/{domain}")
async def wiki_events(domain: str):
    """SSE stream of wiki-build progress for a domain."""
    from scraper import task_log_queues, create_log_queue
    qid = _wiki_queue_id(domain)
    q = task_log_queues.get(qid) or create_log_queue(qid)

    async def generator():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=25.0)
                    if item is None:
                        yield "data: null\n\n"
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {\"type\":\"ping\"}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@wiki_router.get("/status/{domain}")
async def wiki_status(domain: str):
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
    return {"domain": domain, "exists": True, "page_count": len(pages), "last_operation": last_op}

@wiki_router.get("/domains")
async def wiki_domains():
    wiki_base = os.path.join(os.getcwd(), "wiki")
    if not os.path.isdir(wiki_base):
        return {"domains": []}
    domains = [d for d in os.listdir(wiki_base) if os.path.isdir(os.path.join(wiki_base, d))]
    return {"domains": sorted(domains)}

@wiki_router.post("/query")
async def wiki_query(req: WikiQueryRequest):
    from wiki.wiki_manager import WikiManager
    domains = [req.domain] if req.domain else []
    if not domains:
        wiki_base = os.path.join(os.getcwd(), "wiki")
        if os.path.isdir(wiki_base):
            domains = [d for d in os.listdir(wiki_base)
                       if os.path.isdir(os.path.join(wiki_base, d))]
    if not domains:
        raise HTTPException(status_code=404, detail="没有找到知识库，请先建设知识库")

    managers = [WikiManager(d, req.api_key) for d in domains]

    if req.stream:
        async def event_stream():
            for mgr in managers:
                gen = await mgr.query(req.question, stream=True)
                async for chunk in gen:
                    yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        parts = []
        for mgr in managers:
            answer = await mgr.query(req.question, stream=False)
            parts.append(answer)
        return {"answer": "\n\n".join(parts)}

@wiki_router.get("/find/{domain}")
async def wiki_find_page(domain: str, name: str = ""):
    """Find a wiki page by name — searches all subdirectories for a matching stem."""
    import re as _re
    from pathlib import Path
    from wiki.wiki_manager import WikiManager
    mgr = WikiManager(domain, "")
    search = _re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    for page_path in mgr.list_pages():
        stem = Path(page_path).stem.lower()
        if stem == search or (len(search) > 3 and (search in stem or stem in search)):
            content = mgr.read_page(page_path)
            if content:
                return {"path": page_path, "name": name, "content": content}
    raise HTTPException(status_code=404, detail=f"未找到页面: {name}")

@wiki_router.post("/lint")
async def wiki_lint(domain: str, api_key: str, background_tasks: BackgroundTasks):
    from wiki.wiki_manager import WikiManager
    async def run():
        result = await WikiManager(domain, api_key).lint()
        db_manager.log_wiki_operation(domain, "lint", result)
    background_tasks.add_task(run)
    return {"status": "lint started", "domain": domain}

app.include_router(wiki_router)

# ── Raw source file access ────────────────────────────────────────────────────

@app.get("/api/source/{domain}/{filename:path}")
async def get_source_file(domain: str, filename: str):
    """Return the raw scraped markdown file for a domain."""
    from pathlib import Path
    base = (Path(os.getcwd()) / "scraped_data" / domain).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=403, detail="禁止访问")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="源文件未找到")
    content = target.read_text(encoding="utf-8")
    return {"domain": domain, "filename": filename, "content": content}

# ── Direct chat (no wiki required) ───────────────────────────────────────────

class ChatRequest(BaseModel):
    messages: list[dict]
    api_key: str
    stream: bool = True

@app.post("/api/chat")
async def direct_chat(req: ChatRequest):
    """Direct DeepSeek chat — works without any pre-built wiki."""
    from wiki.deepseek_client import chat_completion
    from wiki.schema import GENERAL_CHAT_SYSTEM

    # Ensure a project-scoped system prompt so replies stay focused.
    messages = req.messages or []
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": GENERAL_CHAT_SYSTEM}] + messages
    req.messages = messages

    if req.stream:
        async def event_stream():
            gen = await chat_completion(req.messages, stream=True, api_key=req.api_key)
            async for chunk in gen:
                yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        response = await chat_completion(req.messages, stream=False, api_key=req.api_key)
        return {"answer": response}

# ── Playwright install ────────────────────────────────────────────────────────

def install_playwright_browsers():
    print("Checking Playwright browsers...")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    if getattr(sys, 'frozen', False):
        return
    try:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        print("Playwright browsers ready.")
    except Exception as e:
        print(f"Warning: {e}")

if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    os.makedirs("scraped_data", exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
