"""
Karpathy LLM Wiki Manager.

Implements the three-layer architecture:
  Layer 1: Raw Sources — scraped_data/{domain}/ (read-only)
  Layer 2: The Wiki    — wiki/{domain}/ (LLM-owned, compounding)
  Layer 3: Schema      — wiki/schema.py (governs wiki structure)

Operations:
  ingest() — process new crawled docs, build/update wiki pages
  query()  — answer questions from the wiki (not raw docs)
  lint()   — health-check wiki for contradictions & orphans
"""

import os
import re
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, AsyncGenerator

from wiki.schema import (
    DEFAULT_SCHEMA,
    INGEST_PROMPT_TEMPLATE,
    QUERY_PROMPT_TEMPLATE,
    LINT_PROMPT_TEMPLATE,
)
from wiki import deepseek_client

FILE_WRITE_RE = re.compile(
    r'<<<FILE:\s*(.+?)>>>\n(.*?)<<<END>>>',
    re.DOTALL
)

MAX_CONTEXT_CHARS = 80_000   # approximate safe limit per LLM call
CHUNK_CHARS = 4_000          # chars per source doc chunk sent to LLM


class WikiManager:
    def __init__(self, domain: str, api_key: str, model: str = deepseek_client.DEFAULT_MODEL):
        self.domain = domain
        self.api_key = api_key
        self.model = model
        self.wiki_path = Path(os.getcwd()) / "wiki" / domain
        self.raw_path = Path(os.getcwd()) / "scraped_data" / domain
        self.wiki_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # File operations (synchronous; call via asyncio.to_thread if needed)
    # ------------------------------------------------------------------

    def read_page(self, rel_path: str) -> Optional[str]:
        path = self.wiki_path / rel_path
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return None

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
        return content if content else "(empty — no pages ingested yet)"

    def append_log(self, operation: str, detail: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"[{ts}] {operation}: {detail}\n"
        log_path = self.wiki_path / "log.md"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)

    def _get_ingested_sources(self) -> set[str]:
        """Return set of source filenames already recorded in log.md."""
        log_path = self.wiki_path / "log.md"
        if not log_path.is_file():
            return set()
        text = log_path.read_text(encoding="utf-8")
        # Log entries for ingest: [timestamp] ingest: {filename}|...
        return set(re.findall(r'ingest:.*?source=([^\s|]+)', text))

    def _get_unprocessed_sources(self) -> list[Path]:
        """Source .md files not yet ingested (flat scraped_data/{domain}/*.md)."""
        if not self.raw_path.is_dir():
            return []
        ingested = self._get_ingested_sources()
        sources = []
        for md_file in sorted(self.raw_path.glob("*.md")):
            key = md_file.name
            if key not in ingested:
                sources.append(md_file)
        return sources

    def _parse_file_blocks(self, llm_response: str) -> list[tuple[str, str]]:
        """Extract (rel_path, content) pairs from <<<FILE: ...>>>...<<<END>>> blocks."""
        matches = FILE_WRITE_RE.findall(llm_response)
        return [(path.strip(), content.strip()) for path, content in matches]

    def _apply_file_blocks(self, blocks: list[tuple[str, str]]) -> tuple[int, int]:
        """Write extracted blocks to wiki. Returns (created, updated)."""
        created, updated = 0, 0
        for rel_path, content in blocks:
            # Security: prevent path traversal
            target = (self.wiki_path / rel_path).resolve()
            if not str(target).startswith(str(self.wiki_path.resolve())):
                print(f"[wiki] Blocked path traversal attempt: {rel_path}")
                continue
            existed = target.is_file()
            self.write_page(rel_path, content)
            if existed:
                updated += 1
            else:
                created += 1
        return created, updated

    def _read_source_docs(self, source_files: list[Path]) -> str:
        """Read and concatenate source documents with separators."""
        parts = []
        total = 0
        for sf in source_files:
            try:
                text = sf.read_text(encoding="utf-8")
                entry = f"=== SOURCE: {sf.relative_to(self.raw_path)} ===\n{text}"
                if total + len(entry) > MAX_CONTEXT_CHARS:
                    break
                parts.append(entry)
                total += len(entry)
            except Exception as e:
                print(f"[wiki] Error reading {sf}: {e}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    async def ingest(self, source_files: Optional[list[str]] = None,
                     batch_size: int = 5) -> dict:
        """
        Process unprocessed crawled docs in batches.
        Returns stats dict: {pages_created, pages_updated, docs_processed, batches}.
        """
        if source_files:
            sources = [self.raw_path / sf for sf in source_files]
        else:
            sources = await asyncio.to_thread(self._get_unprocessed_sources)

        if not sources:
            return {"pages_created": 0, "pages_updated": 0, "docs_processed": 0, "batches": 0}

        total_created = total_updated = total_docs = batches = 0

        for i in range(0, len(sources), batch_size):
            batch = sources[i: i + batch_size]
            docs_text = await asyncio.to_thread(self._read_source_docs, batch)
            if not docs_text:
                continue

            index_content = await asyncio.to_thread(self.read_index)
            prompt = INGEST_PROMPT_TEMPLATE.format(
                count=len(batch),
                index_content=index_content,
                documents=docs_text,
            )
            messages = [
                {"role": "system", "content": DEFAULT_SCHEMA},
                {"role": "user", "content": prompt},
            ]

            try:
                response = await deepseek_client.chat_completion(
                    messages, model=self.model, stream=False, api_key=self.api_key
                )
                blocks = await asyncio.to_thread(self._parse_file_blocks, response)
                created, updated = await asyncio.to_thread(self._apply_file_blocks, blocks)
                total_created += created
                total_updated += updated
                total_docs += len(batch)
                batches += 1

                # Log processed source filenames (flat: just the filename)
                source_keys = "|".join(
                    f"source={sf.name}" for sf in batch
                )
                await asyncio.to_thread(
                    self.append_log, "ingest",
                    f"batch={batches} docs={len(batch)} pages_created={created} pages_updated={updated} {source_keys}"
                )

                print(f"[wiki/{self.domain}] Batch {batches}: {len(batch)} docs → {created} created, {updated} updated")
            except Exception as e:
                print(f"[wiki/{self.domain}] Ingest batch {i // batch_size + 1} error: {e}")

        return {
            "pages_created": total_created,
            "pages_updated": total_updated,
            "docs_processed": total_docs,
            "batches": batches,
        }

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def query(self, question: str, stream: bool = True,
                    save_answer: bool = False) -> AsyncGenerator[str, None] | str:
        """
        Answer a question from the wiki.
        stream=True: async generator yielding text deltas.
        stream=False: returns full answer string.
        save_answer=True: writes answer as a synthesis page.
        """
        index_content = await asyncio.to_thread(self.read_index)

        # Gather relevant pages (simple keyword heuristic — fast, no embeddings)
        pages_content = await asyncio.to_thread(self._find_relevant_pages, question)

        prompt = QUERY_PROMPT_TEMPLATE.format(
            index_content=index_content,
            pages_content=pages_content,
            question=question,
        )
        messages = [
            {"role": "system", "content": DEFAULT_SCHEMA},
            {"role": "user", "content": prompt},
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
                             save_answer: bool) -> AsyncGenerator[str, None]:
        full_answer = []
        gen = await deepseek_client.chat_completion(
            messages, model=self.model, stream=True, api_key=self.api_key
        )
        async for chunk in gen:
            full_answer.append(chunk)
            yield chunk

        if save_answer and full_answer:
            answer = "".join(full_answer)
            await self._save_answer_as_synthesis(question, answer)

    def _find_relevant_pages(self, question: str, max_chars: int = 20_000) -> str:
        """
        Simple keyword-based page relevance: score pages by question word overlap.
        Returns concatenated relevant page content up to max_chars.
        """
        words = set(re.findall(r'\w+', question.lower()))
        pages = []
        for rel_path in self.list_pages():
            if rel_path in ("log.md",):
                continue
            content = self.read_page(rel_path) or ""
            score = sum(1 for w in words if w in content.lower())
            if score > 0:
                pages.append((score, rel_path, content))

        pages.sort(reverse=True, key=lambda x: x[0])

        parts = []
        total = 0
        for _, rel_path, content in pages:
            entry = f"=== {rel_path} ===\n{content}"
            if total + len(entry) > max_chars:
                break
            parts.append(entry)
            total += len(entry)

        if not parts:
            # Fall back to index only
            return f"(No specific pages found for query. Index:\n{self.read_index()})"

        return "\n\n".join(parts)

    async def _save_answer_as_synthesis(self, question: str, answer: str):
        slug = re.sub(r'[^a-z0-9]+', '-', question.lower())[:50].strip('-')
        rel_path = f"synthesis/{slug}.md"
        content = f"# Q: {question}\n\n{answer}\n\n## Generated\nAuto-filed from query.\n"
        await asyncio.to_thread(self.write_page, rel_path, content)
        await asyncio.to_thread(self.append_log, "query", f"saved_synthesis={rel_path}")

    # ------------------------------------------------------------------
    # Lint
    # ------------------------------------------------------------------

    async def lint(self) -> dict:
        """
        Health-check all wiki pages: fix contradictions, add cross-references,
        update orphan pages in index.md.
        Returns {issues_found, pages_updated}.
        """
        all_pages = await asyncio.to_thread(self.list_pages)
        if not all_pages:
            return {"issues_found": 0, "pages_updated": 0}

        # Build full wiki text (respecting context limit)
        parts = []
        total = 0
        for rel_path in all_pages:
            if rel_path == "log.md":
                continue
            content = await asyncio.to_thread(self.read_page, rel_path) or ""
            entry = f"=== {rel_path} ===\n{content}"
            if total + len(entry) > MAX_CONTEXT_CHARS:
                break
            parts.append(entry)
            total += len(entry)

        pages_content = "\n\n".join(parts)
        prompt = LINT_PROMPT_TEMPLATE.format(pages_content=pages_content)
        messages = [
            {"role": "system", "content": DEFAULT_SCHEMA},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await deepseek_client.chat_completion(
                messages, model=self.model, stream=False, api_key=self.api_key
            )
            blocks = await asyncio.to_thread(self._parse_file_blocks, response)
            _, updated = await asyncio.to_thread(self._apply_file_blocks, blocks)
            await asyncio.to_thread(
                self.append_log, "lint",
                f"pages_reviewed={len(parts)} pages_updated={updated}"
            )
            return {"issues_found": len(blocks), "pages_updated": updated}
        except Exception as e:
            print(f"[wiki/{self.domain}] Lint error: {e}")
            return {"issues_found": 0, "pages_updated": 0, "error": str(e)}
