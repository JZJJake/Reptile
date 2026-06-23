# Wiki Pipeline Self-Check

A recurring audit checklist for logic errors across the three Karpathy-wiki
stages. Run this whenever `wiki/schema.py` or `wiki/wiki_manager.py` change.

## Stage 1 — 数据的蒸馏 (doc → knowledge atom)

- [ ] `DISTILL_ATOM_PROMPT_TEMPLATE` output format still matches what
      `_distill_doc` post-processes (source-pointer enforcement, `# 标题` check).
- [ ] `.stage1_done` / `.ingested` only mark a source once an atom was
      actually written — a failed/empty distillation must not be marked done.
- [ ] `_atom_slug` stays collision-free (hash suffix) as filenames evolve.
- [ ] `STAGE1_DOC_CHARS` truncation doesn't regularly cut off the part of the
      document that contains the actual claims (check against real scraped
      docs, not just synthetic short ones).

## Stage 2 — 知识库构建 (atoms → relations/concepts/entities)

- [ ] **Idempotent re-ingest**: only atoms not yet in `.stage2_done` are sent
      to assembly. Re-running `ingest()` with no new sources must be a no-op
      (verify log shows "断点续传" and `pages_created/updated == 0`).
- [ ] `relations.md` does not re-accumulate the same edge text on repeated
      ingests — spot-check for near-duplicate `[[A]] —(关系)→ [[B]]（来源: x.md）`
      lines after 2+ ingest cycles on the same domain.
- [ ] `_read_network_pages` token cap (`EXISTING_NETWORK_CAP_TOKENS`) doesn't
      silently drop `relations.md` once it grows large — if it does, chunked
      assembly loses the existing relation network entirely for that batch.
- [ ] `_apply_file_blocks` path check rejects sibling-domain writes
      (`wiki/<domain>` vs `wiki/<domain>-evil`), not just `..` traversal —
      this matters because FILE_WRITE paths come from LLM output, which can
      be influenced by adversarial content in crawled source documents.
- [ ] FILE_WRITE block parser (`FILE_WRITE_RE`) still matches all `<<<END>>>`
      variants DeepSeek actually emits; check `print` warnings for "0 blocks".

## Stage 3 — 关系网分片 (relations.md → relations/ shards)

- [ ] `_stage3_consolidate_relations_if_needed` only fires once `relations.md`
      exceeds `RELATIONS_SHARD_THRESHOLD`; below it, no shard dir is created.
- [ ] The consolidator never overwrites `relations.md` itself (the
      `p.strip() != "relations.md"` filter in `_apply_file_blocks` feed) — shards
      are a *derived* view; Stage-2 remains the writer of `relations.md`.
- [ ] `_read_network_pages` excludes `relations/` shards (they would double-count
      edges already present in `relations.md` in Stage-2 assembly context).
- [ ] `_load_relations_for_query` prefers `relations/_index.md` + keyword-matched
      shard(s) when shards exist, and falls back to truncated `relations.md`
      otherwise — both paths stay under `RELATIONS_CAP_CHARS`.
- [ ] **`_read_network_pages` truncates** oversized entries to the remaining
      budget instead of `continue`-dropping them — `relations.md` must never
      vanish from assembly context once it grows past the cap.

## Rebuild / import (no re-crawl)

- [ ] `force_rebuild_stage2` deletes curated pages + `.stage2_done` but KEEPS
      `atoms/`, `.stage1_done`, `.ingested` (verify atoms survive, Stage-1 is
      not re-run, relations network is re-assembled from existing atoms).
- [ ] `force_rebuild_full` deletes the whole `wiki/{domain}/` and re-distills
      from `scraped_data/{domain}/` — it does NOT re-crawl.
- [ ] `ingest_from_files` copies external `.md` into `scraped_data/{domain}/`
      (basename only, no path traversal) so atom source-pointers + citations
      resolve, then runs the normal resumable pipeline.
- [ ] `/api/wiki/import` strips path segments from uploaded filenames
      (`os.path.basename`) and rejects non-`.md` uploads.

## Model tier (hybrid strategy)

- [ ] `deepseek_client.BUILD_MODEL` (v4-pro) drives Stage 1 distil, Stage 2
      assembly, Stage 3 shard, and `lint`. `QUERY_MODEL` (v4-flash) drives
      page-select and the streamed answer.  `site_analyzer.DEFAULT_MODEL`
      tracks the build tier.  Grep for `model=self.model` should return nothing
      (all call sites use `build_model`/`query_model` explicitly).

## Query — 查询回答质量 (wiki → DeepSeek answer)

- [ ] `_select_relevant_pages` returns real `index.md` page paths, not atom
      paths or hallucinated filenames — `query()` falls back to
      `_find_relevant_pages` correctly when it returns `[]`.
- [ ] `relations_content` + `pages_content` + `index_content` together stay
      under `MAX_CONTEXT_CHARS` (now enforced via `RELATIONS_CAP_CHARS` on
      `relations.md`) — a 400 from DeepSeek on `query()` usually means one of
      these grew unbounded.
- [ ] Citations in the answer are clickable end-to-end:
      - `[[页面名]]` → `/api/wiki/find/{domain}` (filename-stem match, then
        title-based match via each page's first `# Heading`).
      - bare `*.md` source filenames (e.g. `（来源: News_2024_xxx_abcd1234.md）`)
        → `/api/source/{domain}/{filename}`. The regex must accept filenames
        that start with an uppercase letter (crawler slugs mirror URL path
        segments verbatim).
      - Chat bubbles must use `renderWikiContent`, not plain `renderMarkdown`,
        or the second bullet never triggers.
- [ ] Multi-turn `history` is appended before the current `query_turn` and
      never produces two consecutive same-role messages.

## Fixes applied in this pass

1. Chat bubbles now render with `renderWikiContent` (was `renderMarkdown`),
   and the bare-`.md` regex now accepts an uppercase first character —
   together these make `（来源: News_xxx.md）`-style citations clickable in
   chat replies, not just in the wiki viewer.
2. `DEFAULT_MODEL` switched from `deepseek-chat` to `deepseek-reasoner`
   (DeepSeek's top-tier "thinking" model) in `wiki/deepseek_client.py` and
   `site_analyzer.py`; timeouts bumped accordingly.
3. Stage 2 assembly is now incremental (`.stage2_done`): re-ingesting no
   longer re-derives relations/concepts from atoms already folded into the
   network, preventing duplicate entries from accumulating in `relations.md`
   over repeated crawls.
4. `_apply_file_blocks` path-containment check fixed: a naive
   `str.startswith()` on the resolved path allowed writes into a sibling
   domain directory whose name extends this one's (`wiki/example_com` vs
   `wiki/example_com_evil`); now requires true path ancestry.
5. `relations.md` is capped (`RELATIONS_CAP_CHARS`) before being placed in
   the query prompt, so it can't crowd out `pages_content`/`index_content`
   as it grows across many ingests.
6. Removed dead single-stage-pipeline prompts/helpers
   (`PLAN_PROMPT_TEMPLATE`, `INGEST_PROMPT_TEMPLATE`,
   `WRITE_WITH_CONTEXT_PROMPT_TEMPLATE`, `_doc_titles_for_plan`,
   `_read_source_docs`) that no longer match the two-stage
   distill→assemble pipeline and could mislead future audits.
7. **Stage 3 relation-network sharding**: `relations.md` is auto-clustered into
   `relations/<topic>.md` + `relations/_index.md` once it exceeds
   `RELATIONS_SHARD_THRESHOLD`; queries load only the matching shard, breaking
   the single-file scalability ceiling.
8. **`_read_network_pages` no longer silently drops** oversized pages — it
   truncates to the remaining budget, so `relations.md` always stays in Stage-2
   assembly context.
9. **Hybrid model strategy**: `BUILD_MODEL=deepseek-v4-pro` for Stage 1/2/3 +
   lint + site analysis; `QUERY_MODEL=deepseek-v4-flash` for page-select + Q&A.
10. **Rebuild / import without re-crawl**: `force_rebuild_stage2` (keep atoms),
    `force_rebuild_full` (re-distill from raw files), and `ingest_from_files` /
    `POST /api/wiki/import` (build directly from uploaded `.md`) — so a knowledge
    base survives architecture-version upgrades without re-crawling sources.
