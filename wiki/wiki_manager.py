"""
Karpathy LLM Wiki Manager — two-phase ingest with read-before-write.

Three layers:
  1. Raw Sources   — scraped_data/{domain}/*.md  (immutable, crawler output)
  2. The Wiki      — wiki/{domain}/*.md           (LLM-curated, compounding)
  3. The Schema    — wiki/schema.py               (governs all LLM operations)

Ingest pipeline (two phases per batch):
  Phase 1 (plan)  — light DeepSeek call: given doc titles + current index,
                    identify which existing pages to update vs create fresh.
  Phase 2 (write) — main DeepSeek call: with full doc text + existing page
                    content, produce FILE_WRITE blocks that preserve and extend.

Query pipeline (two-step):
  Step 1 — DeepSeek selects the most relevant wiki pages from the index.
  Step 2 — DeepSeek answers from exactly those pages (not raw sources).

Source tracking via wiki/{domain}/.ingested (one filename per line, append-only).
"""

import os
import re
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, AsyncGenerator

from wiki.schema import (
    DEFAULT_SCHEMA,
    PLAN_PROMPT_TEMPLATE,
    INGEST_PROMPT_TEMPLATE,
    WRITE_WITH_CONTEXT_PROMPT_TEMPLATE,
    PAGE_SELECT_PROMPT_TEMPLATE,
    QUERY_PROMPT_TEMPLATE,
    LINT_PROMPT_TEMPLATE,
)
from wiki import deepseek_client

# Path: no newlines, no '>', max 200 chars — prevents the lazy .+? from consuming
# multiple lines when LLM writes garbage on the header line (e.g. >>>]# Title).
# Trailing junk on the header line (after >>>) is swallowed by [^\n]*.
# <<<END accepts all truncated variants: <<<END>>> / <<<END>> / <<<END> / <<<END
FILE_WRITE_RE = re.compile(
    r'<<<FILE:\s*([^\n>]{1,200})>>>[^\n]*\n(.*?)<<<END(?:>>>|>>|>|)',
    re.DOTALL,
)

MAX_CONTEXT_CHARS = 80_000   # safe limit per LLM call
EXISTING_PAGE_CAP = 24_000   # max chars of fetched existing pages fed to write phase

# Source files that are navigation/list/index pages, not real content.
# e.g. News_List_2018..._abcd1234.md  →  skipped from knowledge base.
SKIP_SOURCE_RE = re.compile(
    r'(?i)(^|_)(news_)?list(_|$)|(^|_)index(_|$)|(^|_)column(_|$)|(^|_)node(_|$)'
)


class WikiManager:
    def __init__(self, domain: str, api_key: str,
                 model: str = deepseek_client.DEFAULT_MODEL):
        self.domain    = domain
        self.api_key   = api_key
        self.model     = model
        self.wiki_path = Path(os.getcwd()) / "wiki" / domain
        self.raw_path  = Path(os.getcwd()) / "scraped_data" / domain
        self.wiki_path.mkdir(parents=True, exist_ok=True)

    # ── File I/O ───────────────────────────────────────────────────────────────

    def read_page(self, rel_path: str) -> Optional[str]:
        path = self.wiki_path / rel_path
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def write_page(self, rel_path: str, content: str):
        target = self.wiki_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def list_pages(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.wiki_path))
            for p in self.wiki_path.rglob("*.md")
        )

    def read_index(self) -> str:
        content = self.read_page("index.md")
        return content if content else "(空目录——知识库尚无内容)"

    def append_log(self, operation: str, detail: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log_path = self.wiki_path / "log.md"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {operation}: {detail}\n")

    # ── Source tracking (.ingested file) ──────────────────────────────────────

    def _get_ingested_sources(self) -> set[str]:
        """Return set of already-ingested filenames from .ingested file.
        Migrates from the old log.md regex approach on first call."""
        ingested_path = self.wiki_path / ".ingested"
        if not ingested_path.is_file():
            # One-time migration: extract source names from old log.md entries
            log_path = self.wiki_path / "log.md"
            if log_path.is_file():
                text = log_path.read_text(encoding="utf-8")
                names = {
                    n.strip()
                    for n in re.findall(r'source=([^\s|,\]]+)', text)
                    if n.strip()
                }
                if names:
                    ingested_path.write_text(
                        "\n".join(sorted(names)) + "\n", encoding="utf-8"
                    )
                    return names
            return set()
        return {
            n.strip()
            for n in ingested_path.read_text(encoding="utf-8").splitlines()
            if n.strip()
        }

    def _mark_ingested(self, filenames: list[str]):
        """Append successfully-ingested filenames to .ingested."""
        ingested_path = self.wiki_path / ".ingested"
        with open(ingested_path, "a", encoding="utf-8") as f:
            for name in filenames:
                f.write(name.strip() + "\n")

    def _get_unprocessed_sources(self) -> list[Path]:
        """Flat *.md files under scraped_data/{domain}/ not yet ingested.
        Navigation/list/index pages (e.g. News_List_*) are skipped — they are
        not knowledge content."""
        if not self.raw_path.is_dir():
            return []
        ingested = self._get_ingested_sources()
        result = []
        for p in sorted(self.raw_path.glob("*.md")):
            if p.name in ingested:
                continue
            if SKIP_SOURCE_RE.search(p.stem):
                continue   # navigation / list page — not real content
            result.append(p)
        return result

    # ── Shared helpers ─────────────────────────────────────────────────────────

    def _parse_file_blocks(self, text: str) -> list[tuple[str, str]]:
        return [(path.strip(), content.strip())
                for path, content in FILE_WRITE_RE.findall(text)]

    def _apply_file_blocks(self, blocks: list[tuple[str, str]]) -> tuple[int, int]:
        created = updated = 0
        for rel_path, content in blocks:
            target = (self.wiki_path / rel_path).resolve()
            if not str(target).startswith(str(self.wiki_path.resolve())):
                print(f"[wiki] Blocked path traversal: {rel_path}")
                continue
            existed = target.is_file()
            self.write_page(rel_path, content)
            updated += existed
            created += not existed
        return created, updated

    def _read_source_docs(self, source_files: list[Path]) -> str:
        """Read and concatenate source docs up to MAX_CONTEXT_CHARS."""
        parts = []
        total = 0
        for sf in source_files:
            try:
                text = sf.read_text(encoding="utf-8")
                entry = f"=== SOURCE: {sf.name} ===\n{text}"
                if total + len(entry) > MAX_CONTEXT_CHARS:
                    break
                parts.append(entry)
                total += len(entry)
            except Exception as e:
                print(f"[wiki] Error reading {sf}: {e}")
        return "\n\n".join(parts)

    def _doc_titles_for_plan(self, source_files: list[Path]) -> str:
        """Lightweight title-only list for the plan phase — avoids token waste."""
        lines = []
        for sf in source_files:
            try:
                text = sf.read_text(encoding="utf-8")
                title = next(
                    (l.lstrip("#").strip() for l in text.splitlines() if l.startswith("#")),
                    sf.stem,
                )
                lines.append(f"- {sf.name}: {title[:120]}")
            except Exception:
                lines.append(f"- {sf.name}")
        return "\n".join(lines)

    def _find_relevant_pages(self, question: str, max_chars: int = 20_000) -> str:
        """Keyword-based fallback page selector (no API call)."""
        words = set(re.findall(r'\w+', question.lower()))
        scored = []
        for rel_path in self.list_pages():
            if rel_path in ("log.md",) or rel_path == ".ingested":
                continue
            content = self.read_page(rel_path) or ""
            score = sum(1 for w in words if w in content.lower())
            if score > 0:
                scored.append((score, rel_path, content))
        scored.sort(reverse=True, key=lambda x: x[0])
        parts = []
        total = 0
        for _, rel_path, content in scored:
            entry = f"=== {rel_path} ===\n{content}"
            if total + len(entry) > max_chars:
                break
            parts.append(entry)
            total += len(entry)
        return "\n\n".join(parts) if parts else f"(无匹配页面。目录:\n{self.read_index()})"

    # ── Two-phase ingest ───────────────────────────────────────────────────────

    async def _ingest_batch(self, batch: list[Path], batch_no: int,
                             n_batches: int, progress) -> tuple[int, int]:
        """
        Phase 1 (plan): cheap DeepSeek call with doc titles + index.
                        Returns JSON mapping pages to create vs update.
        Phase 2 (write): main DeepSeek call with full doc text + fetched
                         existing page content → FILE_WRITE blocks.
        Falls back to single-call (Phase 2 only) if Phase 1 fails.
        """
        def report(msg, mtype="log"):
            if progress:
                try:
                    progress(msg, mtype)
                except Exception:
                    pass

        docs_text = await asyncio.to_thread(self._read_source_docs, batch)
        if not docs_text:
            report(f"批次 {batch_no}：文档内容为空，跳过", "warn")
            return 0, 0

        index_content = await asyncio.to_thread(self.read_index)
        existing_pages_text = None   # None means "plan failed, use simple ingest"

        # ── Phase 1: plan ──────────────────────────────────────────────────────
        try:
            report(f"批次 {batch_no}/{n_batches}：分析文档，规划更新方案...", "log")
            doc_titles = await asyncio.to_thread(self._doc_titles_for_plan, batch)
            plan_raw = await deepseek_client.chat_completion(
                [
                    {"role": "system", "content": "仅输出 JSON，不要其他文字。"},
                    {"role": "user",   "content": PLAN_PROMPT_TEMPLATE.format(
                        count=len(batch),
                        index_content=index_content,
                        doc_titles=doc_titles,
                    )},
                ],
                model=self.model, stream=False,
                api_key=self.api_key, temperature=0.0,
            )

            match = re.search(r'\{.*\}', plan_raw, re.DOTALL)
            if match:
                plan = json.loads(match.group())
                to_update = [
                    p for p in plan.get("update", [])
                    if isinstance(p, str)
                    and p.endswith(".md")
                    and p not in ("index.md", "log.md")
                ]

                # Fetch current content of pages marked for update (capped)
                if to_update:
                    fetched: dict[str, str] = {}
                    total_chars = 0
                    for page_path in to_update:
                        content = await asyncio.to_thread(self.read_page, page_path)
                        if content and total_chars + len(content) < EXISTING_PAGE_CAP:
                            fetched[page_path] = content
                            total_chars += len(content)

                    if fetched:
                        report(
                            f"批次 {batch_no}/{n_batches}：读取 {len(fetched)} 个已有页面以便融合更新",
                            "log",
                        )
                        existing_pages_text = "\n\n".join(
                            f"=== {path} ===\n{content}"
                            for path, content in fetched.items()
                        )

        except Exception as e:
            report(
                f"批次 {batch_no} 规划阶段遇到问题，直接整合（不含已有页面上下文）: {e}",
                "warn",
            )
            # existing_pages_text stays None → falls through to simple ingest

        # ── Phase 2: write ─────────────────────────────────────────────────────
        report(f"批次 {batch_no}/{n_batches}：调用 DeepSeek 整合知识，写入知识库...", "info")

        if existing_pages_text is not None:
            prompt = WRITE_WITH_CONTEXT_PROMPT_TEMPLATE.format(
                count=len(batch),
                index_content=index_content,
                existing_pages=existing_pages_text,
                documents=docs_text,
            )
        else:
            prompt = INGEST_PROMPT_TEMPLATE.format(
                count=len(batch),
                index_content=index_content,
                documents=docs_text,
            )

        response = await deepseek_client.chat_completion(
            [
                {"role": "system", "content": DEFAULT_SCHEMA},
                {"role": "user",   "content": prompt},
            ],
            model=self.model, stream=False, api_key=self.api_key,
        )
        blocks = await asyncio.to_thread(self._parse_file_blocks, response)
        if not blocks:
            report(
                f"批次 {batch_no}：DeepSeek 未输出有效 FILE_WRITE 块——"
                "可能是格式不符合 <<<FILE: path>>>/<<<END>>> 协议",
                "warn",
            )
            print(f"[wiki/{self.domain}] batch {batch_no}: 0 blocks parsed. "
                  f"Response head: {response[:300]!r}")
        created, updated = await asyncio.to_thread(self._apply_file_blocks, blocks)
        return created, updated

    async def ingest(self, source_files: Optional[list[str]] = None,
                     batch_size: int = 5, progress=None) -> dict:
        """
        Process unprocessed crawled docs in batches.
        `progress(msg, type)` receives live status events.
        Returns {pages_created, pages_updated, docs_processed, batches}.
        """
        def report(msg, mtype="log"):
            if progress:
                try:
                    progress(msg, mtype)
                except Exception:
                    pass

        if source_files:
            sources = [self.raw_path / sf for sf in source_files]
        else:
            sources = await asyncio.to_thread(self._get_unprocessed_sources)

        if not sources:
            report(
                f"未发现待处理文档（scraped_data/{self.domain}/ 下无新 .md 文件）",
                "warn",
            )
            return {"pages_created": 0, "pages_updated": 0,
                    "docs_processed": 0, "batches": 0, "no_sources": True}

        total = len(sources)
        n_batches = (total + batch_size - 1) // batch_size
        report(
            f"发现 {total} 篇新文档，分 {n_batches} 批次（每批最多 {batch_size} 篇）处理",
            "info",
        )

        total_created = total_updated = total_docs = batches = 0

        for i in range(0, len(sources), batch_size):
            batch = sources[i: i + batch_size]
            batch_no = i // batch_size + 1

            try:
                created, updated = await self._ingest_batch(
                    batch, batch_no, n_batches, progress
                )
                total_created += created
                total_updated += updated
                total_docs    += len(batch)
                batches       += 1

                filenames = [sf.name for sf in batch]
                await asyncio.to_thread(self._mark_ingested, filenames)
                await asyncio.to_thread(
                    self.append_log, "ingest",
                    f"batch={batches} docs={len(batch)} created={created} "
                    f"updated={updated} sources=[{','.join(filenames)}]",
                )
                report(
                    f"批次 {batch_no}/{n_batches} 完成：新建 {created} 页 / 更新 {updated} 页",
                    "success",
                )
            except Exception as e:
                report(f"批次 {batch_no} 失败：{e}", "error")
                print(f"[wiki/{self.domain}] batch {batch_no} error: {e}")

        report(
            f"知识库建设完成：处理 {total_docs} 篇文档 → 新建 {total_created} 页 / 更新 {total_updated} 页",
            "done",
        )
        return {
            "pages_created": total_created,
            "pages_updated": total_updated,
            "docs_processed": total_docs,
            "batches": batches,
        }

    # ── Two-step query ─────────────────────────────────────────────────────────

    async def _select_relevant_pages(self, question: str) -> list[str]:
        """
        Step 1 of query: ask DeepSeek to pick the most relevant pages from
        the index (cheap call, temperature=0). Falls back to [] on error.
        """
        index = await asyncio.to_thread(self.read_index)
        if not index or "尚无内容" in index or "empty" in index.lower():
            return []
        try:
            response = await deepseek_client.chat_completion(
                [
                    {"role": "system",
                     "content": "精确输出页面路径，每行一个，不要其他内容。"},
                    {"role": "user",
                     "content": PAGE_SELECT_PROMPT_TEMPLATE.format(
                         question=question,
                         index_content=index,
                     )},
                ],
                model=self.model, stream=False,
                api_key=self.api_key, temperature=0.0,
            )
            paths = []
            for line in response.strip().splitlines():
                line = line.strip().lstrip('-').strip()
                if line.endswith('.md') and line not in ("log.md",):
                    paths.append(line)
            return paths[:6]
        except Exception as e:
            print(f"[wiki] page selection failed: {e}")
            return []

    async def query(self, question: str, stream: bool = True,
                    save_answer: bool = False) -> "AsyncGenerator[str, None] | str":
        """
        Answer a question from the wiki (not raw sources).
        Two steps:
          1. DeepSeek selects the most relevant pages from index.md.
          2. DeepSeek answers from exactly those pages.
        Falls back to keyword-based page selection if Step 1 fails.
        """
        index_content = await asyncio.to_thread(self.read_index)

        # Step 1: LLM page selection
        page_paths = await self._select_relevant_pages(question)

        if page_paths:
            parts = []
            for path in page_paths:
                content = await asyncio.to_thread(self.read_page, path)
                if content:
                    parts.append(f"=== {path} ===\n{content}")
            pages_content = (
                "\n\n".join(parts)
                if parts
                else await asyncio.to_thread(self._find_relevant_pages, question)
            )
        else:
            pages_content = await asyncio.to_thread(self._find_relevant_pages, question)

        # Step 2: answer
        messages = [
            {"role": "system", "content": DEFAULT_SCHEMA},
            {"role": "user",   "content": QUERY_PROMPT_TEMPLATE.format(
                index_content=index_content,
                pages_content=pages_content,
                question=question,
            )},
        ]

        if stream:
            return self._stream_query(messages, question, save_answer)
        else:
            response = await deepseek_client.chat_completion(
                messages, model=self.model, stream=False, api_key=self.api_key
            )
            if save_answer and response:
                await self._save_answer_as_synthesis(question, response)
            return response

    async def _stream_query(self, messages: list[dict], question: str,
                             save_answer: bool) -> "AsyncGenerator[str, None]":
        full_answer: list[str] = []
        gen = await deepseek_client.chat_completion(
            messages, model=self.model, stream=True, api_key=self.api_key
        )
        async for chunk in gen:
            full_answer.append(chunk)
            yield chunk
        if save_answer and full_answer:
            await self._save_answer_as_synthesis(question, "".join(full_answer))

    async def _save_answer_as_synthesis(self, question: str, answer: str):
        slug = re.sub(r'[^a-z0-9]+', '-', question.lower())[:50].strip('-')
        rel_path = f"synthesis/{slug}.md"
        content = f"# Q: {question}\n\n{answer}\n\n## 生成时间\n自动存档自查询。\n"
        await asyncio.to_thread(self.write_page, rel_path, content)
        await asyncio.to_thread(self.append_log, "query", f"saved_synthesis={rel_path}")

    # ── Lint ───────────────────────────────────────────────────────────────────

    async def lint(self) -> dict:
        """Health-check all wiki pages: fix contradictions, add cross-links."""
        all_pages = await asyncio.to_thread(self.list_pages)
        if not all_pages:
            return {"issues_found": 0, "pages_updated": 0}

        parts = []
        total = 0
        for rel_path in all_pages:
            if rel_path in ("log.md",):
                continue
            content = await asyncio.to_thread(self.read_page, rel_path) or ""
            entry = f"=== {rel_path} ===\n{content}"
            if total + len(entry) > MAX_CONTEXT_CHARS:
                break
            parts.append(entry)
            total += len(entry)

        pages_content = "\n\n".join(parts)
        messages = [
            {"role": "system", "content": DEFAULT_SCHEMA},
            {"role": "user",   "content": LINT_PROMPT_TEMPLATE.format(
                pages_content=pages_content,
            )},
        ]
        try:
            response = await deepseek_client.chat_completion(
                messages, model=self.model, stream=False, api_key=self.api_key
            )
            blocks = await asyncio.to_thread(self._parse_file_blocks, response)
            _, updated = await asyncio.to_thread(self._apply_file_blocks, blocks)
            await asyncio.to_thread(
                self.append_log, "lint",
                f"pages_reviewed={len(parts)} pages_updated={updated}",
            )
            return {"issues_found": len(blocks), "pages_updated": updated}
        except Exception as e:
            print(f"[wiki/{self.domain}] Lint error: {e}")
            return {"issues_found": 0, "pages_updated": 0, "error": str(e)}
