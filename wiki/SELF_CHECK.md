# Wiki Pipeline Self-Check

A recurring audit checklist for logic errors across the map-reduce Karpathy-wiki
stages (distil → cluster-assemble → shard → cross-synthesis). Run this whenever
`wiki/schema.py` or `wiki/wiki_manager.py` change. The deterministic items are
covered by `python -m wiki.test_pipeline_offline` (no API key needed).

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
- [ ] **Cross-batch entity identity** (`.entity_registry.json`): after 2+ ingest
      cycles on the same domain, an entity introduced in an early batch is merged
      into — not duplicated by — a later batch that mentions it, even when its
      page falls outside `EXISTING_NETWORK_CAP_TOKENS`. The registry is the
      mechanism; `_build_entity_hint` must surface it to every chunk. Spot-check
      for duplicate `entities/*.md` pages describing the same object.
- [ ] **Entity-match precision**: `_match_existing_entities` must not false-merge
      distinct entities that merely share a generic suffix (e.g. "中心"); the
      `ENTITY_MIN_MATCH_LEN` containment guard governs this. A false merge
      corrupts knowledge worse than a duplicate — keep the guard conservative.
- [ ] **Anti-loss backup**: a substantial page rewritten to under
      `PAGE_SHRINK_BACKUP_RATIO` of its size is snapshotted to `.backups/` (as
      `*.md.bak`, invisible to `list_pages()`) before being overwritten.
- [ ] `_read_network_pages` sub-caps `relations.md` to half the budget and orders
      the rest by relevance to the current chunk (`focus_text`), so entity/concept
      pages are never fully crowded out by a large relation file.
- [ ] These four items are covered by `python -m wiki.test_pipeline_offline`
      (no API key needed) — run it whenever this file or `wiki_manager.py` changes.

## Stage 2 "map" — topic-affinity clustering (atoms → coherent batches)

- [ ] When new atoms exceed one assembly budget (or a network already exists),
      `_cluster_atoms_by_affinity` groups them by shared concept-tags/entities,
      NOT by file order — verify atoms on the same topic land in the same batch.
- [ ] First/small build (all new atoms fit one budget AND no existing network)
      still goes through the SINGLE full-corpus call (best global view) — clustering
      only kicks in once scale forces multiple batches.
- [ ] `_cluster_atoms_by_affinity` respects the token budget (splits same-topic
      atoms when needed) and never drops an atom; falls back to budget chunking
      when no atom exposes any features.

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

## Stage 4 "reduce" — cross-topic synthesis (connect clues → logic chains)

- [ ] `_stage4_cross_synthesis` runs after Stage 3 in `ingest` /
      `force_rebuild_stage2`, but ONLY when `created or updated` (a no-op
      re-ingest must not spend tokens re-synthesising).
- [ ] It writes ONLY `synthesis/*.md` + `index.md` — blocks targeting atoms,
      `relations.md`, concepts or entities are filtered out before apply (it is a
      read-only consumer of the network, not a writer of it).
- [ ] No-fabrication: the prompt requires inferred (not network-stated) links to
      carry a `[推断]` tag; spot-check synthesis pages don't assert invented facts
      as sourced. `CROSS_SYNTHESIS_PROMPT_TEMPLATE` must keep that rule.
- [ ] Incremental: existing `synthesis/` pages are fed back (capped) and merged,
      not overwritten — prior insights survive across ingests.

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
- [ ] **Direction is intentional, do NOT reverse**: construction = pro because
      its errors COMPOUND (read back as "existing network" forever); answers =
      flash because they are ephemeral and grounded by already-curated pages.
- [ ] **Deep answer escalation**: `query(deep=True)` routes ONLY the answer step
      to `REASON_MODEL` (v4-pro) for clue-connecting/synthesis; page-select stays
      on the cheap tier. `WikiQueryRequest.deep` plumbs it through `/api/wiki/query`.

## Query — 查询回答质量 (wiki → DeepSeek answer)

- [ ] `_select_relevant_pages` returns real `index.md` page paths, not atom
      paths or hallucinated filenames — `query()` falls back to
      `_find_relevant_pages` correctly when it returns `[]`.
- [ ] `_select_relevant_pages` caps `index.md` to `INDEX_SELECT_CAP_CHARS` before
      the page-select call — at scale an uncapped index blows that call's context.
- [ ] **Hybrid retrieval**: `query()` unions LLM page-select with
      `_vector_retrieve_paths` (TF-IDF/cosine, `wiki/retrieval.py`). `_find_relevant_pages`
      ranks by `VectorIndex`, not single-keyword overlap. Covered by
      `test_vector_index_ranks_relevant_first` / `test_find_relevant_pages_uses_vector_ranking`.
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
