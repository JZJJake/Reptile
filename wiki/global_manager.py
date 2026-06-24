"""
Global knowledge-base manager — cross-domain retrieval and unified Q&A.

Single-domain `WikiManager` answers from exactly one `wiki/{domain}/` tree. The
GLOBAL mode here answers across EVERY knowledge base at once, without merging or
mutating any of them: each base stays the source of truth for its own pages, and
the global layer is a *derived view* over them. This guarantees "知识库数据完好"
(per-base data stays intact) and "全局数据有据可查" (every global answer is
traceable back to a specific base + page).

Two pieces:

  1. Manifest (the coordinating data structure) — `build_manifest()` scans all
     bases and produces a single structured catalogue: per base, its pages
     (path + real title + type) and counts. It is recomputed from the files on
     every call (never a stale second copy) and materialised to
     `wiki/_global/manifest.json` so it can be inspected/audited.

  2. Unified query — `query()` runs each base's own vector retrieval, merges the
     candidates into one globally-ranked set (each tagged with its base), packs
     the strongest into a char budget, and asks the model ONE question over the
     combined, source-labelled context. Citations carry the base name so the UI
     can resolve them back to the exact base + page.
"""

import os
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, AsyncGenerator

from wiki import deepseek_client
from wiki.schema import DEFAULT_SCHEMA, GLOBAL_QUERY_PROMPT_TEMPLATE
from wiki.wiki_manager import WikiManager, MAX_CONTEXT_CHARS

# Reserve a slice of the answer-context budget for the global directory listing.
GLOBAL_DIRECTORY_CAP_CHARS = 6_000
# Per-base retrieval depth before global re-ranking.
PER_DOMAIN_TOPK = 6
# Pages that are bookkeeping, not knowledge content.
_SKIP_PAGES = {"log.md", ".ingested", ".stage1_done", ".stage2_done"}


def list_global_domains(base_dir: Optional[str] = None) -> list[str]:
    """All knowledge-base domains, excluding internal dirs (``_global`` etc.)
    and hidden dirs. Shared by the API so the global view and the domain list
    agree on what counts as a real knowledge base."""
    wiki_base = base_dir or os.path.join(os.getcwd(), "wiki")
    if not os.path.isdir(wiki_base):
        return []
    return sorted(
        d for d in os.listdir(wiki_base)
        if os.path.isdir(os.path.join(wiki_base, d))
        and not d.startswith("_")
        and not d.startswith(".")
    )


class GlobalWikiManager:
    GLOBAL_KEY = "__global__"   # sentinel domain value selecting global mode

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model
        self.wiki_base = Path(os.getcwd()) / "wiki"
        self.domain = "全局知识库"   # used for error labelling parity with WikiManager

    # ── Domains ────────────────────────────────────────────────────────────────
    def list_domains(self) -> list[str]:
        return list_global_domains(str(self.wiki_base))

    def _manager(self, domain: str) -> WikiManager:
        return WikiManager(domain, self.api_key, model=self.model)

    # ── Manifest (coordinating data structure) ─────────────────────────────────
    @staticmethod
    def _page_type(rel_path: str) -> str:
        seg = rel_path.split("/")
        if seg[0] == "atoms":
            return "atom"
        return seg[0] if len(seg) > 1 else "root"

    @staticmethod
    def _page_title(content: str, fallback: str) -> str:
        for line in (content or "").splitlines():
            s = line.strip()
            if s.startswith("# "):
                return s[2:].strip()[:60] or fallback
        return fallback

    def build_manifest(self, persist: bool = True) -> dict:
        """Scan every base and return a structured catalogue. Always derived
        from the live files, so it can never silently drift from reality."""
        domains = self.list_domains()
        manifest = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "domains": {},
            "totals": {"domains": 0, "curated_pages": 0, "atoms": 0},
        }
        for d in domains:
            mgr = self._manager(d)
            pages, atoms = [], 0
            for rel in mgr.list_pages():
                if rel in _SKIP_PAGES:
                    continue
                if rel.startswith("atoms/"):
                    atoms += 1
                    continue
                content = mgr.read_page(rel) or ""
                pages.append({
                    "path": rel,
                    "title": self._page_title(content, Path(rel).stem),
                    "type": self._page_type(rel),
                })
            # last operation, for auditability
            last_op = None
            log = mgr.read_page("log.md")
            if log:
                lines = [l for l in log.splitlines() if l.strip()]
                if lines:
                    last_op = lines[-1]
            manifest["domains"][d] = {
                "curated_pages": len(pages),
                "atoms": atoms,
                "pages": pages,
                "last_operation": last_op,
            }
            manifest["totals"]["domains"] += 1
            manifest["totals"]["curated_pages"] += len(pages)
            manifest["totals"]["atoms"] += atoms

        if persist:
            try:
                out_dir = self.wiki_base / "_global"
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError as e:
                print(f"[global] manifest persist failed: {e}")
        return manifest

    def _directory_text(self, manifest: dict) -> str:
        """Compact, model-facing rendering of the manifest (capped)."""
        parts, total = [], 0
        for d, info in manifest["domains"].items():
            head = (f"## 知识库：{d}（{info['curated_pages']} 个知识页 / "
                    f"{info['atoms']} 个原子）")
            # list up to ~12 curated page titles per base to keep it compact
            titles = [f"  - [[{d}::{p['title']}]]" for p in info["pages"][:12]]
            if len(info["pages"]) > 12:
                titles.append(f"  - …（另有 {len(info['pages']) - 12} 页）")
            block = "\n".join([head] + titles)
            if total + len(block) > GLOBAL_DIRECTORY_CAP_CHARS:
                parts.append("…（知识库目录过长，已截断）")
                break
            parts.append(block)
            total += len(block)
        return "\n".join(parts) if parts else "（暂无任何知识库，请先建设知识库）"

    # ── Cross-domain retrieval ─────────────────────────────────────────────────
    def _gather_pages(self, question: str) -> str:
        """Run each base's own vector retrieval, merge + globally re-rank, and
        pack the strongest pages into the answer budget — each labelled with its
        source base so the answer can attribute every claim."""
        hits = []   # (score, domain, path, content)
        for d in self.list_domains():
            mgr = self._manager(d)
            curated, atoms = mgr._collect_page_docs()
            docs = curated or atoms
            if not docs:
                continue
            try:
                ranked = mgr._rank(docs, question, top_k=PER_DOMAIN_TOPK)
            except Exception as e:
                print(f"[global] retrieval failed for {d}: {e}")
                continue
            for path, score in ranked:
                hits.append((score, d, path, docs.get(path, "")))

        if not hits:
            return "（各知识库均未检索到与该问题直接相关的页面。）"

        hits.sort(key=lambda x: x[0], reverse=True)
        parts, total = [], 0
        for _score, d, path, content in hits:
            if not content:
                continue
            entry = f"=== 【知识库: {d}】 {path} ===\n{content}"
            if total + len(entry) > MAX_CONTEXT_CHARS:
                break
            parts.append(entry)
            total += len(entry)
        return "\n\n".join(parts) if parts else "（各知识库均未检索到相关页面。）"

    # ── Unified global query ───────────────────────────────────────────────────
    async def query(self, question: str, stream: bool = True,
                    history: Optional[list] = None,
                    deep: bool = False) -> "AsyncGenerator[str, None] | str":
        answer_model = deepseek_client.REASON_MODEL if deep \
            else (self.model or deepseek_client.QUERY_MODEL)

        manifest = await asyncio.to_thread(self.build_manifest)
        directory = self._directory_text(manifest)
        pages_content = await asyncio.to_thread(self._gather_pages, question)

        query_turn = GLOBAL_QUERY_PROMPT_TEMPLATE.format(
            directory=directory,
            pages_content=pages_content,
            question=question,
        )
        messages = [{"role": "system", "content": DEFAULT_SCHEMA}]
        if history:
            for msg in history:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": query_turn})

        if stream:
            return self._stream(messages, answer_model)
        return await deepseek_client.chat_completion(
            messages, model=answer_model, stream=False, api_key=self.api_key)

    async def _stream(self, messages, answer_model) -> "AsyncGenerator[str, None]":
        try:
            gen = await deepseek_client.chat_completion(
                messages, model=answer_model, stream=True, api_key=self.api_key)
            async for chunk in gen:
                yield chunk
        except Exception as e:
            yield f"\n\n【全局知识库查询出错：{e}】"
