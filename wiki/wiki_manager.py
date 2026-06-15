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
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, AsyncGenerator

from wiki.schema import (
    DEFAULT_SCHEMA,
    DISTILL_ATOM_PROMPT_TEMPLATE,
    ASSEMBLE_PROMPT_TEMPLATE,
    ASSEMBLE_INCREMENTAL_PROMPT_TEMPLATE,
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
RELATIONS_CAP_CHARS = 20_000  # relations.md grows without bound over many ingests;
                               # cap it so it can't crowd out pages_content in query()

# ── Multi-layer distillation budgets (in tokens) ──────────────────────────────
# Quality-optimal window for relationship assembly. We favour MORE small calls
# over fewer giant ones: long-context recall degrades on complex relation
# extraction. HARD cap mirrors the user's 1M/3 reasoning as an absolute ceiling.
ASSEMBLY_BUDGET_TOKENS = 60_000     # per Stage-2 assembly call (input side)
HARD_CONTEXT_CAP_TOKENS = 330_000   # ~1M/3, never exceed
EXISTING_NETWORK_CAP_TOKENS = 30_000  # cap on prior-network context in chunked assembly
STAGE1_DOC_CHARS = 40_000           # max source chars fed to one distillation call
STAGE1_CONCURRENCY = 4              # parallel single-doc distillation calls
ASSEMBLY_TIMEOUT = 600.0            # deepseek-reasoner "thinking" adds latency on large assembly calls


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for mixed CJK/Latin text under DeepSeek's
    tokenizer: CJK chars ~1 token each, other chars ~0.3 token each."""
    if not text:
        return 0
    cjk = sum(1 for ch in text
              if '一' <= ch <= '鿿'
              or '　' <= ch <= '〿'
              or '＀' <= ch <= '￯')
    other = len(text) - cjk
    return int(cjk + other * 0.3) + 1


def slugify(text: str) -> str:
    """CJK-aware slug: lowercase, hyphen-joined, preserves Chinese characters
    (titles/names are now mostly Chinese) instead of stripping them."""
    return re.sub(r'[^a-z0-9一-鿿]+', '-', text.lower()).strip('-')


def is_cjk(ch: str) -> bool:
    """True if `ch` is a CJK Unified Ideograph (the common Chinese text range)."""
    return '一' <= ch <= '鿿'


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

    # ── Stage-1 atom tracking (.stage1_done file) ─────────────────────────────

    def _atom_slug(self, source_name: str) -> str:
        """Deterministic atom filename for a source file (preserves uniqueness)."""
        stem = re.sub(r'\.md$', '', source_name)
        slug = slugify(stem)
        # Scraper-generated filenames often share long common prefixes
        # (e.g. "News_2024-xx_<long-title>_<hash>"); truncating alone can
        # collapse two distinct sources onto the same atom path, silently
        # overwriting one. A short content hash keeps them distinct.
        suffix = hashlib.md5(source_name.encode("utf-8")).hexdigest()[:8]
        return (slug or "atom")[:70] + "-" + suffix + ".md"

    def _get_distilled_sources(self) -> set[str]:
        """Source filenames already distilled into atoms/ (Stage 1 done)."""
        done_path = self.wiki_path / ".stage1_done"
        if not done_path.is_file():
            return set()
        return {n.strip() for n in done_path.read_text(encoding="utf-8").splitlines()
                if n.strip()}

    def _mark_distilled(self, source_name: str):
        with open(self.wiki_path / ".stage1_done", "a", encoding="utf-8") as f:
            f.write(source_name.strip() + "\n")

    def _list_atoms(self) -> list[Path]:
        atoms_dir = self.wiki_path / "atoms"
        if not atoms_dir.is_dir():
            return []
        return sorted(atoms_dir.glob("*.md"))

    # ── Stage-2 assembly tracking (.stage2_done file) ─────────────────────────

    def _get_assembled_atoms(self) -> set[str]:
        """Atom filenames already folded into the relation network (Stage 2 done)."""
        done_path = self.wiki_path / ".stage2_done"
        if not done_path.is_file():
            return set()
        return {n.strip() for n in done_path.read_text(encoding="utf-8").splitlines()
                if n.strip()}

    def _mark_assembled(self, atom_names: list[str]):
        with open(self.wiki_path / ".stage2_done", "a", encoding="utf-8") as f:
            for name in atom_names:
                f.write(name.strip() + "\n")

    # ── Shared helpers ─────────────────────────────────────────────────────────

    def _parse_file_blocks(self, text: str) -> list[tuple[str, str]]:
        return [(path.strip(), content.strip())
                for path, content in FILE_WRITE_RE.findall(text)]

    def _apply_file_blocks(self, blocks: list[tuple[str, str]]) -> tuple[int, int]:
        created = updated = 0
        wiki_root = self.wiki_path.resolve()
        for rel_path, content in blocks:
            target = (self.wiki_path / rel_path).resolve()
            # A plain str.startswith() prefix check would also accept a sibling
            # directory whose name extends this domain's (e.g. "wiki/example_com"
            # vs "wiki/example_com_evil") — require the wiki root as an actual
            # path ancestor.
            if target != wiki_root and wiki_root not in target.parents:
                print(f"[wiki] Blocked path traversal: {rel_path}")
                continue
            existed = target.is_file()
            self.write_page(rel_path, content)
            updated += existed
            created += not existed
        return created, updated

    @staticmethod
    def _score_text(question: str, content: str) -> int:
        """Relevance score supporting both ASCII words and CJK characters.

        Chinese text is NOT space-delimited, so re.findall(r'\\w+', ...) returns
        the whole sentence as one token and never matches individual characters.
        We handle this by scoring:
          - ASCII / alphanumeric words (for English/number terms)
          - Individual CJK characters (coarse but effective for Chinese)
          - CJK bigrams (2-char pairs — better precision than single chars)
        """
        content_lower = content.lower()
        score = 0

        # ASCII words
        ascii_words = set(re.findall(r'[a-zA-Z0-9]+', question))
        score += sum(1 for w in ascii_words if w and w.lower() in content_lower)

        # CJK characters and bigrams from the question
        cjk_chars = [ch for ch in question if is_cjk(ch)]
        # Bigrams (2-char pairs) — weighted double for precision
        bigrams = {''.join(cjk_chars[i:i+2]) for i in range(len(cjk_chars) - 1)}
        score += sum(2 for bg in bigrams if bg in content)
        # Individual chars as backup (weight 1)
        score += sum(1 for ch in set(cjk_chars) if ch in content)

        return score

    def _find_relevant_pages(self, question: str, max_chars: int = 20_000) -> str:
        """Keyword-based fallback page selector (no API call).

        First tries curated pages (concepts/entities/synthesis/summaries).
        Falls back to Stage-1 atoms if no curated pages match.
        Last resort: returns top atoms by filename sort when even scoring gives 0.
        """
        curated, atoms = [], []
        for rel_path in self.list_pages():
            if rel_path in ("log.md",) or rel_path in (".ingested", ".stage1_done"):
                continue
            content = self.read_page(rel_path) or ""
            score = self._score_text(question, content)
            bucket = atoms if rel_path.startswith("atoms/") else curated
            bucket.append((score, rel_path, content))

        def _pack(scored_list, budget, min_score=0):
            scored_list.sort(reverse=True, key=lambda x: x[0])
            parts, total = [], 0
            for sc, rel_path, content in scored_list:
                if sc < min_score:
                    break
                entry = f"=== {rel_path} ===\n{content}"
                if total + len(entry) > budget:
                    break
                parts.append(entry)
                total += len(entry)
            return parts

        # 1. Curated pages with any relevance signal
        curated_parts = _pack(curated, max_chars, min_score=1)
        if curated_parts:
            return "\n\n".join(curated_parts)

        # 2. Atoms with relevance signal
        atom_parts = _pack(atoms, max_chars, min_score=1)

        if atom_parts:
            disclaimer = (
                "\n\n【提示：知识库尚未完成二级关系组装（Stage 2），"
                "以下内容来自第一级知识原子，可能不够完整。"
                "建议点击「重建知识库」以完成完整的关系网组装后再提问。】"
            )
            return "\n\n".join(atom_parts) + disclaimer

        # Last resort: no scored match anywhere. Don't dump arbitrary atom
        # content (it may be entirely unrelated to the question) — instead
        # surface the index plus an atom-count hint so the LLM can say
        # honestly that the knowledge base doesn't yet cover this topic.
        if atoms:
            return (
                f"(未找到与问题直接相关的页面。知识库目前包含 {len(atoms)} 个知识原子，"
                f"但均未匹配到问题中的关键词。目录:\n{self.read_index()})"
            )

        return f"(无匹配页面。目录:\n{self.read_index()})"

    # ── Stage 1: single-doc distillation → knowledge atoms ─────────────────────

    def _read_one_doc(self, path: Path) -> str:
        """Read a single source doc, truncated to the Stage-1 char budget."""
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[wiki] Error reading {path}: {e}")
            return ""
        return text[:STAGE1_DOC_CHARS]

    async def _distill_doc(self, path: Path, sem: asyncio.Semaphore,
                           report) -> Optional[Path]:
        """Distill ONE source document into a compact knowledge atom.
        Returns the written atom Path, or None on failure/empty."""
        async with sem:
            body = await asyncio.to_thread(self._read_one_doc, path)
            if not body.strip():
                report(f"跳过空文档：{path.name}", "warn")
                return None
            try:
                atom = await deepseek_client.chat_completion(
                    [
                        {"role": "system",
                         "content": "你是知识蒸馏器，只输出规定格式的知识原子。"},
                        {"role": "user",
                         "content": DISTILL_ATOM_PROMPT_TEMPLATE.format(
                             filename=path.name, body=body)},
                    ],
                    model=self.model, stream=False,
                    api_key=self.api_key, temperature=0.2,
                )
            except Exception as e:
                report(f"蒸馏失败 {path.name}：{e}", "error")
                print(f"[wiki/{self.domain}] distill error {path.name}: {e}")
                return None

            atom = (atom or "").strip()
            if not atom:
                report(f"蒸馏结果为空：{path.name}", "warn")
                return None

            # Enforce source pointer (anti-error-propagation) regardless of LLM.
            if f"来源: {path.name}" not in atom:
                atom = re.sub(r'(^#[^\n]*\n)',
                              rf'\1- 来源: {path.name}\n', atom, count=1) \
                    if atom.startswith("#") else f"- 来源: {path.name}\n{atom}"

            atom_name = self._atom_slug(path.name)
            await asyncio.to_thread(self.write_page, f"atoms/{atom_name}", atom)
            await asyncio.to_thread(self._mark_distilled, path.name)
            return self.wiki_path / "atoms" / atom_name

    async def _stage1_distill_all(self, sources: list[Path], report) -> int:
        """Distill all undistilled sources into atoms/, in parallel (capped)."""
        done = await asyncio.to_thread(self._get_distilled_sources)
        todo = [p for p in sources if p.name not in done]
        if not todo:
            report("阶段①：所有文档已蒸馏为知识原子（断点续传）", "info")
            return 0

        report(f"阶段① 单篇蒸馏：{len(todo)} 篇文档 → 知识原子"
               f"（并发 {STAGE1_CONCURRENCY}）", "info")
        sem = asyncio.Semaphore(STAGE1_CONCURRENCY)
        made = 0
        tasks = [self._distill_doc(p, sem, report) for p in todo]
        for i, fut in enumerate(asyncio.as_completed(tasks), 1):
            res = await fut
            if res:
                made += 1
            if i % 10 == 0 or i == len(todo):
                report(f"阶段①进度：{i}/{len(todo)} 篇已蒸馏", "log")
        report(f"阶段①完成：新增 {made} 个知识原子", "success")
        return made

    # ── Stage 2: assemble atoms → relation network ─────────────────────────────

    def _read_network_pages(self, cap_tokens: int) -> str:
        """Read existing curated network (relations/index/concepts/entities/
        synthesis) for incremental-assembly context, capped by token budget."""
        priority = ["relations.md", "index.md"]
        parts, total = [], 0
        seen = set()
        ordered = priority + [
            p for p in self.list_pages()
            if p not in priority
            and not p.startswith("atoms/")
            and p not in ("log.md",)
        ]
        for rel in ordered:
            if rel in seen:
                continue
            seen.add(rel)
            content = self.read_page(rel)
            if not content:
                continue
            entry = f"=== {rel} ===\n{content}"
            t = estimate_tokens(entry)
            if total + t > cap_tokens:
                continue
            parts.append(entry)
            total += t
        return "\n\n".join(parts)

    def _chunk_atoms_by_budget(self, atoms: list[Path],
                               budget_tokens: int) -> list[list[Path]]:
        """Group atom files into chunks that each fit within the token budget."""
        chunks, cur, cur_tok = [], [], 0
        for ap in atoms:
            try:
                t = estimate_tokens(ap.read_text(encoding="utf-8"))
            except Exception:
                t = 0
            if cur and cur_tok + t > budget_tokens:
                chunks.append(cur)
                cur, cur_tok = [], 0
            cur.append(ap)
            cur_tok += t
        if cur:
            chunks.append(cur)
        return chunks

    def _join_atoms(self, atoms: list[Path]) -> str:
        parts = []
        for ap in atoms:
            try:
                parts.append(ap.read_text(encoding="utf-8").strip())
            except Exception:
                pass
        return "\n\n---\n\n".join(parts)

    async def _assemble_call(self, prompt: str, label: str,
                             report) -> tuple[int, int]:
        """One Stage-2 assembly LLM call → parse + apply FILE_WRITE blocks."""
        response = await deepseek_client.chat_completion(
            [
                {"role": "system", "content": DEFAULT_SCHEMA},
                {"role": "user",   "content": prompt},
            ],
            model=self.model, stream=False, api_key=self.api_key,
            timeout=ASSEMBLY_TIMEOUT,
        )
        blocks = await asyncio.to_thread(self._parse_file_blocks, response)
        if not blocks:
            report(f"{label}：DeepSeek 未输出有效 FILE_WRITE 块（格式不符）", "warn")
            print(f"[wiki/{self.domain}] {label}: 0 blocks. "
                  f"Response head: {response[:300]!r}")
        created, updated = await asyncio.to_thread(self._apply_file_blocks, blocks)
        return created, updated

    async def _stage2_assemble(self, report) -> tuple[int, int]:
        """Assemble newly-distilled knowledge atoms into the relation network.
        Only atoms not yet folded in (.stage2_done) are sent — atoms already
        represented in relations.md/concepts/etc are skipped, otherwise every
        re-ingest would re-derive the same facts/relations from old atoms and
        accumulate near-duplicate entries on top of what's already there.
        Single call when the new atoms fit the budget; otherwise chunked
        incremental assembly that merges each chunk into the growing network."""
        atoms = await asyncio.to_thread(self._list_atoms)
        if not atoms:
            report("阶段②：没有知识原子可组装", "warn")
            return 0, 0

        done2 = await asyncio.to_thread(self._get_assembled_atoms)
        new_atoms = [a for a in atoms if a.name not in done2]
        if not new_atoms:
            report("阶段②：所有知识原子已组装入关系网（断点续传）", "info")
            return 0, 0

        existing = await asyncio.to_thread(
            self._read_network_pages, EXISTING_NETWORK_CAP_TOKENS)
        chunks = await asyncio.to_thread(
            self._chunk_atoms_by_budget, new_atoms, ASSEMBLY_BUDGET_TOKENS)
        n = len(chunks)

        if n == 1 and not existing:
            report(f"阶段② 关系组装：{len(new_atoms)} 个原子一次性全量组装"
                   "（实体归并 / 关系提取 / 矛盾检测）", "info")
            atoms_text = await asyncio.to_thread(self._join_atoms, new_atoms)
            prompt = ASSEMBLE_PROMPT_TEMPLATE.format(
                existing_network="（空——首次组装）",
                atoms=atoms_text,
            )
            result = await self._assemble_call(prompt, "阶段②", report)
            await asyncio.to_thread(self._mark_assembled, [a.name for a in new_atoms])
            return result

        report(f"阶段② 关系组装：{len(new_atoms)} 个新知识原子，"
               f"分 {n} 批增量融合进关系网", "info")
        tot_c = tot_u = 0
        for idx, chunk in enumerate(chunks, 1):
            report(f"阶段②：组装第 {idx}/{n} 批（{len(chunk)} 个新原子）...", "log")
            existing = await asyncio.to_thread(
                self._read_network_pages, EXISTING_NETWORK_CAP_TOKENS)
            atoms_text = await asyncio.to_thread(self._join_atoms, chunk)
            prompt = ASSEMBLE_INCREMENTAL_PROMPT_TEMPLATE.format(
                chunk_no=idx, n_chunks=n,
                existing_network=existing or "（空——首次组装）",
                atoms=atoms_text,
            )
            try:
                c, u = await self._assemble_call(prompt, f"阶段②批{idx}", report)
                tot_c += c
                tot_u += u
                await asyncio.to_thread(self._mark_assembled, [a.name for a in chunk])
                report(f"阶段②第 {idx}/{n} 批完成：新建 {c} 页 / 更新 {u} 页",
                       "success")
            except Exception as e:
                report(f"阶段②第 {idx} 批失败：{e}", "error")
                print(f"[wiki/{self.domain}] assemble chunk {idx} error: {e}")
        return tot_c, tot_u

    async def ingest(self, source_files: Optional[list[str]] = None,
                     batch_size: int = 5, progress=None) -> dict:
        """
        Two-stage knowledge-distillation pipeline:
          Stage 1 — distill every source doc into a compact knowledge atom.
          Stage 2 — assemble all atoms into a curated relation network.
        Both stages are resumable (.stage1_done / .ingested). `batch_size` is
        retained for API compatibility but no longer drives the pipeline.
        Returns {pages_created, pages_updated, docs_processed, atoms_made}.
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

        existing_atoms = await asyncio.to_thread(self._list_atoms)
        if not sources and not existing_atoms:
            report(
                f"未发现待处理文档（scraped_data/{self.domain}/ 下无新 .md 文件）",
                "warn",
            )
            return {"pages_created": 0, "pages_updated": 0,
                    "docs_processed": 0, "atoms_made": 0, "no_sources": True}

        # ── Stage 1: single-doc distillation ──────────────────────────────────
        atoms_made = 0
        if sources:
            report(f"发现 {len(sources)} 篇新文档，开始两级蒸馏建设知识库", "info")
            atoms_made = await self._stage1_distill_all(sources, report)
            # Mark sources as ingested once distilled (Stage-1 ownership).
            done = await asyncio.to_thread(self._get_distilled_sources)
            newly = [p.name for p in sources if p.name in done]
            if newly:
                await asyncio.to_thread(self._mark_ingested, newly)

        # ── Stage 2: relation-network assembly ────────────────────────────────
        created, updated = await self._stage2_assemble(report)

        total_atoms = len(await asyncio.to_thread(self._list_atoms))
        await asyncio.to_thread(
            self.append_log, "ingest",
            f"stage1_new_atoms={atoms_made} total_atoms={total_atoms} "
            f"assembled_created={created} assembled_updated={updated}",
        )
        report(
            f"知识库建设完成：蒸馏 {total_atoms} 个知识原子 → "
            f"组装关系网（新建 {created} 页 / 更新 {updated} 页）",
            "done",
        )
        return {
            "pages_created": created,
            "pages_updated": updated,
            "docs_processed": atoms_made,
            "atoms_made": atoms_made,
            "total_atoms": total_atoms,
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
                    save_answer: bool = False,
                    history: list = None) -> "AsyncGenerator[str, None] | str":
        """
        Answer a question from the wiki (not raw sources).
        Two steps:
          1. DeepSeek selects the most relevant pages from index.md.
          2. DeepSeek answers from exactly those pages.
        Falls back to keyword-based page selection if Step 1 fails.
        """
        index_content = await asyncio.to_thread(self.read_index)

        # When only Stage-1 atoms exist, tell DeepSeek the wiki is being built
        # so it doesn't refuse due to "empty directory" language in the index.
        if "尚无内容" in index_content:
            atom_count = sum(
                1 for p in await asyncio.to_thread(self.list_pages)
                if p.startswith("atoms/")
            )
            if atom_count > 0:
                index_content = (
                    f"（知识库建设中：已完成第一级蒸馏 {atom_count} 个知识原子，"
                    "二级关系网组装尚未完成。以下知识来自原子层，请据此回答。）"
                )

        # Relation network = reasoning scaffold (always loaded if present).
        relations_content = await asyncio.to_thread(self.read_page, "relations.md")
        relations_content = relations_content or "（暂无显式关系网页面）"
        if len(relations_content) > RELATIONS_CAP_CHARS:
            relations_content = (
                relations_content[:RELATIONS_CAP_CHARS]
                + "\n\n（关系网内容过长，已截断——完整内容请查看 relations.md 页面）"
            )

        # Step 1: LLM page selection
        page_paths = await self._select_relevant_pages(question)

        if page_paths:
            parts = []
            total = 0
            for path in page_paths:
                content = await asyncio.to_thread(self.read_page, path)
                if not content:
                    continue
                entry = f"=== {path} ===\n{content}"
                # LLM-selected pages can be large (synthesis pages run tens of
                # KB); cap the total like _find_relevant_pages does, or a big
                # selection can blow DeepSeek's context window into a 400.
                if total + len(entry) > MAX_CONTEXT_CHARS:
                    break
                parts.append(entry)
                total += len(entry)
            pages_content = (
                "\n\n".join(parts)
                if parts
                else await asyncio.to_thread(self._find_relevant_pages, question)
            )
        else:
            pages_content = await asyncio.to_thread(self._find_relevant_pages, question)

        # Step 2: answer
        query_turn = QUERY_PROMPT_TEMPLATE.format(
            index_content=index_content,
            relations_content=relations_content,
            pages_content=pages_content,
            question=question,
        )
        messages = [{"role": "system", "content": DEFAULT_SCHEMA}]
        # Inject prior conversation turns for multi-turn context
        if history:
            for msg in history:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": query_turn})

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
        try:
            gen = await deepseek_client.chat_completion(
                messages, model=self.model, stream=True, api_key=self.api_key
            )
            async for chunk in gen:
                full_answer.append(chunk)
                yield chunk
        except Exception as e:
            # Surface connect-time errors (missing API key, auth, timeout) and
            # mid-stream errors as visible text instead of silently truncating
            # the answer or crashing the StreamingResponse with no message.
            err = f"\n\n【查询出错：{e}】"
            full_answer.append(err)
            yield err
            return
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
            if rel_path in ("log.md",) or rel_path.startswith("atoms/"):
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
