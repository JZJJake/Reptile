import asyncio
import json
import os
import sys
import uuid
import uvicorn

from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Request, UploadFile, File, Form
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
    single_page: bool = False   # scrape only the start URL, no link discovery
    date_from: str = ""         # "YYYY-MM" cutoff; empty = no date filter

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
        req.single_page,
        req.date_from,
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

class WikiRebuildRequest(BaseModel):
    domain: str
    api_key: str
    level: str = "stage2"   # "stage2" (keep atoms) | "full" (re-distill from raw files)

class WikiQueryRequest(BaseModel):
    question: str
    domain: Optional[str] = None
    api_key: str
    stream: bool = True
    history: list = []   # prior turns: [{"role":"user","content":"..."},...]
    deep: bool = False   # escalate the answer step to v4-pro for query-time
                         # synthesis (connecting clues into new logic chains)

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

@wiki_router.post("/rebuild")
async def wiki_rebuild(req: WikiRebuildRequest, background_tasks: BackgroundTasks):
    """Rebuild a knowledge base WITHOUT re-crawling — for architecture/version
    upgrades. level=stage2 keeps Stage-1 atoms and re-assembles the relation
    network; level=full deletes everything and re-distills from the raw scraped
    source files. Progress streams via /api/wiki/events/{domain}."""
    active = await asyncio.to_thread(db_manager.get_active_tasks)
    if active:
        raise HTTPException(status_code=409,
                            detail="爬虫正在运行，请等待爬取完成后再重建知识库")
    if req.domain in _wiki_building:
        raise HTTPException(status_code=409,
                            detail="该知识库正在建设中，请勿重复点击")
    if req.level not in ("stage2", "full"):
        raise HTTPException(status_code=400, detail="level 必须为 stage2 或 full")

    from scraper import push_status, create_log_queue
    from wiki.wiki_manager import WikiManager

    qid = _wiki_queue_id(req.domain)
    q = create_log_queue(qid)
    _wiki_building.add(req.domain)

    async def run_rebuild():
        def progress(msg, mtype="log"):
            push_status(qid, msg, mtype)
        try:
            label = "Stage-2 重建（保留原子）" if req.level == "stage2" else "全量重建（重新蒸馏）"
            push_status(qid, f"开始{label}：{req.domain}", "info")
            mgr = WikiManager(req.domain, req.api_key)
            if req.level == "stage2":
                result = await mgr.force_rebuild_stage2(progress=progress)
            else:
                result = await mgr.force_rebuild_full(progress=progress)
            db_manager.log_wiki_operation(req.domain, f"rebuild_{req.level}", result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            push_status(qid, f"知识库重建失败：{e}", "error")
            db_manager.log_wiki_operation(req.domain, "rebuild_error", {"error": str(e)})
        finally:
            _wiki_building.discard(req.domain)
            try:
                q.put_nowait(None)
            except Exception:
                pass

    background_tasks.add_task(run_rebuild)
    return {"status": "rebuilding", "domain": req.domain, "level": req.level}

@wiki_router.post("/import")
async def wiki_import(
    background_tasks: BackgroundTasks,
    domain: str = Form(...),
    api_key: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """Build a knowledge base directly from uploaded raw .md files, independent
    of the crawler. Files are saved into scraped_data/{domain}/ then the full
    pipeline runs. Lets users (re)build a KB from previously-downloaded sources
    without re-crawling. Progress streams via /api/wiki/events/{domain}."""
    domain = domain.strip()
    if not domain:
        raise HTTPException(status_code=400, detail="缺少域名")
    if domain in _wiki_building:
        raise HTTPException(status_code=409, detail="该知识库正在建设中，请勿重复提交")

    from pathlib import Path as _Path
    md_files = [f for f in files if (f.filename or "").lower().endswith(".md")]
    if not md_files:
        raise HTTPException(status_code=400, detail="请上传至少一个 .md 文件")

    # Persist uploads into scraped_data/{domain}/ before returning so the build
    # task reads them off disk (UploadFile streams aren't safe to use later).
    raw_dir = _Path(os.getcwd()) / "scraped_data" / domain
    raw_dir.mkdir(parents=True, exist_ok=True)
    saved_names: list[str] = []
    for f in md_files:
        safe_name = os.path.basename(f.filename)            # strip any path segments
        if not safe_name.endswith(".md"):
            continue
        data = await f.read()
        (raw_dir / safe_name).write_bytes(data)
        saved_names.append(safe_name)

    if not saved_names:
        raise HTTPException(status_code=400, detail="没有有效的 .md 文件被保存")

    from scraper import push_status, create_log_queue
    from wiki.wiki_manager import WikiManager

    qid = _wiki_queue_id(domain)
    q = create_log_queue(qid)
    _wiki_building.add(domain)

    async def run_import_build():
        def progress(msg, mtype="log"):
            push_status(qid, msg, mtype)
        try:
            push_status(qid, f"已接收 {len(saved_names)} 个文件，开始导入建库：{domain}", "info")
            mgr = WikiManager(domain, api_key)
            paths = [str(raw_dir / n) for n in saved_names]
            result = await mgr.ingest_from_files(paths, progress=progress)
            db_manager.log_wiki_operation(domain, "import_build", result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            push_status(qid, f"导入建库失败：{e}", "error")
            db_manager.log_wiki_operation(domain, "import_error", {"error": str(e)})
        finally:
            _wiki_building.discard(domain)
            try:
                q.put_nowait(None)
            except Exception:
                pass

    background_tasks.add_task(run_import_build)
    return {"status": "importing", "domain": domain, "files": saved_names}

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
                try:
                    gen = await mgr.query(req.question, stream=True,
                                          history=req.history, deep=req.deep)
                    async for chunk in gen:
                        yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
                except Exception as e:
                    # Isolate per-domain failures in multi-domain queries — one
                    # bad manager (bad API key, I/O error) shouldn't silently
                    # truncate the whole stream with no [DONE]/error frame.
                    err = f"\n\n【{mgr.domain} 查询出错：{e}】"
                    yield f"data: {json.dumps({'delta': err}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        parts = []
        for mgr in managers:
            answer = await mgr.query(req.question, stream=False,
                                     history=req.history, deep=req.deep)
            parts.append(answer)
        return {"answer": "\n\n".join(parts)}

@wiki_router.get("/graph/{domain}")
async def wiki_graph(domain: str):
    """Return nodes + links for the knowledge graph visualisation.

    Node names come from the first '# Title' line of each page (real Chinese
    titles), not the filename slug.

    Edges come from three sources (in order of quality):
      1. Explicit [[citations]] inside curated pages
      2. Explicit relation edges parsed from relations.md
      3. Shared-tag edges between Stage-1 atoms (connects even before Stage 2)
    """
    import re as _re
    from pathlib import Path as _Path
    from wiki.wiki_manager import WikiManager, slugify, is_cjk
    from collections import defaultdict

    mgr = WikiManager(domain, "")
    skip = {"log.md", ".ingested", ".stage1_done"}
    all_pages = [p for p in mgr.list_pages() if p not in skip]

    def _page_title(content: str, fallback: str) -> str:
        """Return the first '# Heading' line, stripped, or fallback."""
        for line in (content or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()[:50] or fallback
        return fallback

    # ── nodes: read each page once, extract real title ──
    page_contents: dict[str, str] = {}
    node_map: dict[str, dict] = {}
    for p in all_pages:
        content = mgr.read_page(p) or ""
        page_contents[p] = content
        seg   = p.split("/")
        # "atoms/" dir → singular "atom" type (matches GCOLOR/JS comparisons);
        # any other subdir keeps its name; root-level pages → "root"
        dtype = "atom" if seg[0] == "atoms" else (seg[0] if len(seg) > 1 else "root")
        slug_name = _Path(p).stem.replace("-", " ")
        node_map[p] = {
            "id":     p,
            "name":   _page_title(content, slug_name),
            "type":   dtype,
            "degree": 0,
        }

    # ── shared name resolution: [[citations]]/relations reference pages by
    # their (often Chinese) display TITLE, but filenames are LLM-generated
    # English/pinyin slugs per schema convention — neither alone reliably
    # matches the other, so check both slug forms for every node. ──
    node_slugs: dict[str, tuple[str, str]] = {
        tp: (_Path(tp).stem.lower(), slugify(node_map[tp]["name"]))
        for tp in node_map
    }

    def _match_node(name: str) -> "str | None":
        slug = slugify(name)
        if not slug:
            return None
        for tp, (stem_slug, title_slug) in node_slugs.items():
            if slug == stem_slug or slug == title_slug:
                return tp
        # CJK substrings carry more meaning per character than ASCII ones
        # (e.g. "api"/"gpt" are too short to substring-match safely)
        min_len = 2 if any(is_cjk(ch) for ch in slug) else 4
        if len(slug) < min_len:
            return None
        for tp, (stem_slug, title_slug) in node_slugs.items():
            if (slug in stem_slug or stem_slug in slug
                    or slug in title_slug or title_slug in slug):
                return tp
        return None

    # ── links from [[citations]] in curated (non-atom) pages ──
    link_set: set[tuple] = set()
    links: list[dict]    = []
    cite_re = _re.compile(r'\[\[([^\]]+)\]\]')

    for p, content in page_contents.items():
        if p.startswith("atoms/"):
            continue  # raw atoms don't carry curated citations
        for cite in cite_re.findall(content):
            target = _match_node(cite)
            if target and target != p and (p, target) not in link_set:
                link_set.add((p, target))
                links.append({"source": p, "target": target, "type": "citation"})
                node_map[p]["degree"]      += 1
                node_map[target]["degree"] += 1

    # ── explicit relation edges from relations.md ──
    rel_content = page_contents.get("relations.md", "") or ""
    rel_re = _re.compile(
        r'\[\[([^\]]+)\]\]\s*[—\-]+\(([^)]+)\)\s*[—→\-]+\s*\[\[([^\]]+)\]\]'
    )
    for m in rel_re.finditer(rel_content):
        src_name, rel_type, tgt_name = m.group(1), m.group(2), m.group(3)
        sp  = _match_node(src_name)
        tp_ = _match_node(tgt_name)
        if sp and tp_ and sp != tp_ and (sp, tp_) not in link_set:
            link_set.add((sp, tp_))
            links.append({"source": sp, "target": tp_, "type": rel_type[:20]})
            node_map[sp]["degree"]  += 1
            node_map[tp_]["degree"] += 1

    # ── Stage-1 atom concept-tag edges (active even before Stage 2) ──
    # Parse "概念标签: tag1, tag2" from each atom and connect atoms that share tags.
    # Primary field name per schema.py; fallbacks tolerate minor LLM format
    # drift (synonyms the model may substitute for "概念标签").
    tag_re = _re.compile(r'(?:概念标签|标签|关键词|主题词)[：:]\s*([^\n]+)')
    tag_map: "defaultdict[str, list[str]]" = defaultdict(list)
    for p, content in page_contents.items():
        if not p.startswith("atoms/"):
            continue
        m = tag_re.search(content)
        if not m:
            continue
        raw = m.group(1)
        for tag in _re.split(r'[，,、；;]+', raw):
            tag = tag.strip()
            if tag and tag != '无' and len(tag) >= 2:
                tag_map[tag].append(p)

    # For each tag shared by 2–8 atoms, add "concept" edges (avoid mega-hubs)
    for tag, members in tag_map.items():
        if len(members) < 2 or len(members) > 8:
            continue
        for i, src in enumerate(members):
            for tgt in members[i + 1:]:
                if (src, tgt) not in link_set:
                    link_set.add((src, tgt))
                    links.append({"source": src, "target": tgt,
                                  "type": f"共同概念: {tag[:12]}"})
                    node_map[src]["degree"] += 1
                    node_map[tgt]["degree"] += 1

    nodes = list(node_map.values())
    atom_count    = sum(1 for n in nodes if n["type"] == "atom")
    curated_count = len(nodes) - atom_count
    return {
        "nodes": nodes, "links": links,
        "stats": {
            "total_nodes":   len(nodes),
            "total_links":   len(links),
            "atom_nodes":    atom_count,
            "curated_nodes": curated_count,
        },
    }


class SaveSynthesisRequest(BaseModel):
    domain: str
    question: str
    answer: str
    api_key: str

@wiki_router.post("/save-synthesis")
async def wiki_save_synthesis(req: SaveSynthesisRequest):
    """Archive a Q&A answer as a synthesis wiki page."""
    from wiki.wiki_manager import WikiManager
    mgr = WikiManager(req.domain, req.api_key)
    await mgr._save_answer_as_synthesis(req.question, req.answer)
    return {"status": "saved", "domain": req.domain}


@wiki_router.get("/find/{domain}")
async def wiki_find_page(domain: str, name: str = ""):
    """Find a wiki page by name — searches all subdirectories for a matching stem."""
    from pathlib import Path
    from wiki.wiki_manager import WikiManager, slugify, is_cjk
    mgr = WikiManager(domain, "")
    # Exact-path fast path: graph clicks pass the real page path (e.g.
    # "concepts/digital-currency-policy.md") since titles are now Chinese
    # and won't fuzzy-match LLM-generated English/pinyin filenames below.
    if name.endswith(".md") and "/" in name:
        content = mgr.read_page(name)
        if content:
            return {"path": name, "name": name, "content": content}

    search = slugify(name)
    has_cjk = any(is_cjk(ch) for ch in search)
    min_len = 2 if has_cjk else 4   # CJK substrings carry more meaning per char
    pages = mgr.list_pages()

    # Pass 1: filename-stem search
    for page_path in pages:
        stem = Path(page_path).stem.lower()
        if stem == search or (len(search) >= min_len and (search in stem or stem in search)):
            content = mgr.read_page(page_path)
            if content:
                return {"path": page_path, "name": name, "content": content}

    # Pass 2: page-title search — handles the common case where DeepSeek cites
    # "数字货币" but the file is named "digital-currency.md" with "# 数字货币"
    # as its first heading. Read the first # line of each page and compare.
    for page_path in pages:
        content = mgr.read_page(page_path)
        if not content:
            continue
        title = ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
        if not title:
            continue
        title_slug = slugify(title)
        if title_slug == search or (len(search) >= min_len and
                                    (search in title_slug or title_slug in search)):
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
