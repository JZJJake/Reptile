import Foundation

/// Karpathy LLM-Wiki manager (three-stage pipeline).
///
/// Stage 1 — source doc → knowledge atom         (buildModel / v4-pro)
/// Stage 2 — atoms → relation network             (buildModel / v4-pro)
/// Stage 3 — shard relations.md when oversized    (buildModel / v4-pro)
/// Query   — LLM page-select + stream answer      (queryModel / v4-flash)
///
/// Architectural fixes vs. original port:
/// - readNetworkPages: truncates large files instead of silently dropping them,
///   so relations.md is never invisible to Stage-2 context.
/// - Stage 3 sharding: relations.md is split into topic shards when it exceeds
///   relationsShardThreshold chars; queries load only the matching shard.
/// - LLM page selection: two-step query (select pages → answer from pages),
///   matching the Python wiki_manager behaviour.  Falls back to keyword scoring.
/// - forceRebuildStage2 / forceRebuildFull: independent rebuild without re-crawl.
/// - ingestFromFiles: import arbitrary .md files and build wiki without crawling.
@MainActor
final class WikiManager {
    var logs: [LogLine] = []
    var onLog: ((LogLine) -> Void)?

    let domain: String
    private let client: DeepSeekClient
    private let store = Store.shared

    // Context budgets (chars — coarse, mirrors Python char-based caps).
    private let maxContextChars        = 80_000
    private let relationsCapChars      = 20_000   // cap fed to query prompt
    private let relationsShardThreshold = 30_000  // trigger Stage-3 sharding
    private let stage1DocChars         = 40_000
    private let assemblyBudgetChars    = 120_000  // ~60k tokens, CJK-heavy
    private let existingNetworkCapChars = 60_000
    private let indexSelectCapChars    = 8_000    // index fed to LLM page-select

    init(domain: String, apiKey: String) {
        self.domain = domain
        self.client = DeepSeekClient(apiKey: apiKey)
    }

    private func log(_ m: String, _ l: LogLine.Level = .log) {
        let line = LogLine(level: l, message: m)
        logs.append(line)
        onLog?(line)
    }

    // MARK: - Public entry points

    /// Standard two-(now three-)stage pipeline: distil → assemble → shard.
    @discardableResult
    func ingest() async -> IngestResult {
        let sources = unprocessedSources()
        let existingAtoms = listAtoms()
        if sources.isEmpty && existingAtoms.isEmpty {
            log("未发现待处理文档（scraped_data/\(domain)/ 下无新 .md 文件）", .warn)
            return IngestResult(noSources: true)
        }

        var atomsMade = 0
        if !sources.isEmpty {
            log("发现 \(sources.count) 篇新文档，开始三级蒸馏建设知识库", .info)
            atomsMade = await stage1DistillAll(sources)
        }

        let (created, updated) = await stage2Assemble()

        // Stage 3: shard relations.md when it has grown large.
        await stage3ConsolidateRelationsIfNeeded()

        let total = listAtoms().count
        store.appendLog(domain: domain, operation: "ingest",
                        detail: "stage1_new_atoms=\(atomsMade) total_atoms=\(total) created=\(created) updated=\(updated)")
        log("知识库建设完成：蒸馏 \(total) 个知识原子 → 组装关系网（新建 \(created) 页 / 更新 \(updated) 页）", .done)
        return IngestResult(pagesCreated: created, pagesUpdated: updated,
                            atomsMade: atomsMade, totalAtoms: total)
    }

    /// Import external .md files (from Files app / document picker) into
    /// scraped_data/{domain}/ and then run the full pipeline.
    /// Security-scoped resource access is handled per file.
    @discardableResult
    func ingestFromFiles(_ urls: [URL]) async -> IngestResult {
        var copied = 0
        for src in urls {
            let accessing = src.startAccessingSecurityScopedResource()
            defer { if accessing { src.stopAccessingSecurityScopedResource() } }
            guard src.pathExtension.lowercased() == "md" else {
                log("跳过非 .md 文件：\(src.lastPathComponent)", .warn); continue
            }
            if store.importFile(from: src, domain: domain) {
                copied += 1
                log("已导入：\(src.lastPathComponent)", .log)
            } else {
                log("导入失败：\(src.lastPathComponent)", .error)
            }
        }
        log("文件导入完成：\(copied) 个文件 → scraped_data/\(domain)/，开始建库…", .info)
        return await ingest()
    }

    /// Clear Stage-2 curated pages and re-assemble from existing atoms.
    /// Use when Stage-2 prompt/architecture changes; avoids re-crawling and
    /// re-distilling (Stage-1 atoms are reused).
    @discardableResult
    func forceRebuildStage2() async -> IngestResult {
        let atomCount = listAtoms().count
        log("Stage-2 重建：保留 \(atomCount) 个知识原子，清除已有策展页面…", .info)
        store.deleteCuratedPages(domain)          // removes index, relations, concepts, etc.
        store.deleteRelationsShards(domain)        // removes relations/ shard dir
        // .stage2_done is inside wiki dir and was deleted by deleteCuratedPages.
        // .stage1_done and atoms/ are preserved.
        log("策展页面已清除，开始重新组装关系网…", .info)
        let (created, updated) = await stage2Assemble()
        await stage3ConsolidateRelationsIfNeeded()
        let total = listAtoms().count
        store.appendLog(domain: domain, operation: "force_rebuild_stage2",
                        detail: "atoms_reused=\(total) created=\(created) updated=\(updated)")
        log("Stage-2 重建完成：新建 \(created) 页 / 更新 \(updated) 页", .done)
        return IngestResult(pagesCreated: created, pagesUpdated: updated,
                            atomsMade: 0, totalAtoms: total)
    }

    /// Delete the entire wiki (atoms included) and rebuild from raw source files.
    /// Use when Stage-1 prompt or data schema changes significantly.
    @discardableResult
    func forceRebuildFull() async -> IngestResult {
        let srcCount = store.scrapedFileCount(domain)
        log("全量重建：清除整个知识库（含知识原子），将从 \(srcCount) 个原始文件重新蒸馏…", .warn)
        store.deleteWiki(domain)
        log("知识库已清除，开始全量重新蒸馏…", .info)
        return await ingest()
    }

    // MARK: - Stage 1: single-doc distillation → knowledge atoms

    private func stage1DistillAll(_ sources: [URL]) async -> Int {
        let done = store.sentinelSet(domain: domain, name: ".stage1_done")
        let todo = sources.filter { !done.contains($0.lastPathComponent) }
        if todo.isEmpty { log("阶段①：所有文档已蒸馏（断点续传）", .info); return 0 }

        log("阶段① 单篇蒸馏：\(todo.count) 篇文档 → 知识原子（模型：\(DeepSeekClient.buildModel)）", .info)
        var made = 0
        for (i, src) in todo.enumerated() {
            if let atom = await distillOne(src) {
                let name = atomSlug(src.lastPathComponent)
                store.writePage(domain: domain, rel: "atoms/\(name)", content: atom)
                store.sentinelAppend(domain: domain, name: ".stage1_done", lines: [src.lastPathComponent])
                store.sentinelAppend(domain: domain, name: ".ingested",   lines: [src.lastPathComponent])
                made += 1
            }
            if (i + 1) % 5 == 0 || i + 1 == todo.count {
                log("阶段①进度：\(i + 1)/\(todo.count)", .log)
            }
        }
        log("阶段①完成：新增 \(made) 个知识原子", .success)
        return made
    }

    private func distillOne(_ src: URL) async -> String? {
        guard var body = try? String(contentsOf: src, encoding: .utf8),
              !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            log("跳过空文档：\(src.lastPathComponent)", .warn); return nil
        }
        if body.count > stage1DocChars { body = String(body.prefix(stage1DocChars)) }
        let name = src.lastPathComponent
        do {
            var atom = try await client.complete(
                messages: [
                    ["role": "system", "content": "你是知识蒸馏器，只输出规定格式的知识原子。"],
                    ["role": "user",   "content": WikiSchema.distillAtom(filename: name, body: body)],
                ],
                model: DeepSeekClient.buildModel,
                temperature: 0.2)
            atom = atom.trimmingCharacters(in: .whitespacesAndNewlines)
            if atom.isEmpty { log("蒸馏结果为空：\(name)", .warn); return nil }
            if !atom.contains("来源: \(name)") {
                if atom.hasPrefix("#"), let nl = atom.firstIndex(of: "\n") {
                    let head = atom[...nl]
                    let rest = atom[atom.index(after: nl)...]
                    atom = head + "- 来源: \(name)\n" + rest
                } else {
                    atom = "- 来源: \(name)\n" + atom
                }
            }
            return atom
        } catch {
            log("蒸馏失败 \(name)：\(error.localizedDescription)", .error)
            return nil
        }
    }

    // MARK: - Stage 2: atoms → relation network

    private func stage2Assemble() async -> (Int, Int) {
        let atoms = listAtoms()
        if atoms.isEmpty { log("阶段②：没有知识原子可组装", .warn); return (0, 0) }

        let done2 = store.sentinelSet(domain: domain, name: ".stage2_done")
        let newAtoms = atoms.filter { !done2.contains($0.lastPathComponent) }
        if newAtoms.isEmpty { log("阶段②：所有知识原子已组装入关系网（断点续传）", .info); return (0, 0) }

        let chunks = chunkByBudget(newAtoms, budget: assemblyBudgetChars)
        let existing0 = readNetworkPages(cap: existingNetworkCapChars)
        var totalC = 0, totalU = 0

        if chunks.count == 1 && existing0.isEmpty {
            log("阶段② 关系组装：\(newAtoms.count) 个原子一次性全量组装（模型：\(DeepSeekClient.buildModel)）", .info)
            let prompt = WikiSchema.assemble(existingNetwork: "（空——首次组装）",
                                             atoms: joinAtoms(newAtoms))
            let (c, u) = await assembleCall(prompt, label: "阶段②")
            store.sentinelAppend(domain: domain, name: ".stage2_done",
                                 lines: newAtoms.map { $0.lastPathComponent })
            return (c, u)
        }

        log("阶段② 关系组装：\(newAtoms.count) 个新原子，分 \(chunks.count) 批增量融合", .info)
        for (idx, chunk) in chunks.enumerated() {
            log("阶段②：组装第 \(idx + 1)/\(chunks.count) 批（\(chunk.count) 个新原子）...", .log)
            let existing = readNetworkPages(cap: existingNetworkCapChars)
            let prompt = WikiSchema.assembleIncremental(
                chunkNo: idx + 1, nChunks: chunks.count,
                existingNetwork: existing.isEmpty ? "（空——首次组装）" : existing,
                atoms: joinAtoms(chunk))
            let (c, u) = await assembleCall(prompt, label: "阶段②批\(idx + 1)")
            totalC += c; totalU += u
            store.sentinelAppend(domain: domain, name: ".stage2_done",
                                 lines: chunk.map { $0.lastPathComponent })
            log("阶段②第 \(idx + 1)/\(chunks.count) 批完成：新建 \(c) 页 / 更新 \(u) 页", .success)
        }
        return (totalC, totalU)
    }

    private func assembleCall(_ prompt: String, label: String) async -> (Int, Int) {
        do {
            let resp = try await client.complete(
                messages: [
                    ["role": "system", "content": WikiSchema.defaultSchema],
                    ["role": "user",   "content": prompt],
                ],
                model: DeepSeekClient.buildModel,
                timeout: DeepSeekClient.assemblyTimeout)
            let blocks = parseFileBlocks(resp)
            if blocks.isEmpty { log("\(label)：未输出有效 FILE_WRITE 块（格式不符）", .warn) }
            return applyFileBlocks(blocks)
        } catch {
            log("\(label) 失败：\(error.localizedDescription)", .error)
            return (0, 0)
        }
    }

    // MARK: - Stage 3: relation-network sharding

    /// If relations.md has grown beyond the shard threshold, cluster it into
    /// topic shards (relations/<topic>.md) + a lightweight index
    /// (relations/_index.md).  Non-fatal: query falls back to relations.md on
    /// failure.  Stage-3 is purely a derived/read-optimised view; Stage-2
    /// continues writing to the monolithic relations.md.
    private func stage3ConsolidateRelationsIfNeeded() async {
        guard let relContent = store.readPage(domain: domain, rel: "relations.md"),
              relContent.count > relationsShardThreshold else { return }

        log("阶段③ 关系网分片：relations.md (\(relContent.count) 字符 > 阈值 \(relationsShardThreshold))，开始主题分片…（模型：\(DeepSeekClient.buildModel)）", .info)
        let prompt = WikiSchema.relationsConsolidate(relationsContent: relContent)
        do {
            let resp = try await client.complete(
                messages: [
                    ["role": "system", "content": WikiSchema.defaultSchema],
                    ["role": "user",   "content": prompt],
                ],
                model: DeepSeekClient.buildModel,
                timeout: DeepSeekClient.assemblyTimeout)
            let blocks = parseFileBlocks(resp)
            let (c, u) = applyFileBlocks(blocks)
            log("阶段③完成：生成 \(c) 个关系分片 / 更新 \(u) 个分片页面", .success)
        } catch {
            log("阶段③分片失败（非致命，查询将降级为 relations.md）：\(error.localizedDescription)", .warn)
        }
    }

    // MARK: - Query (two-step: LLM page-select → stream answer)

    /// Stream an answer from the wiki.
    /// Step 1 (async, v4-flash): LLM selects most relevant pages from index.
    /// Step 2 (stream, v4-flash): LLM answers from selected pages.
    /// Falls back to keyword-scored page selection if Step 1 fails.
    func query(question: String, history: [[String: String]]) async -> AsyncThrowingStream<String, Error> {
        // Load index (truncated for LLM page-select call)
        var indexContent = store.readPage(domain: domain, rel: "index.md") ?? "(空目录——知识库尚无内容)"
        if indexContent.contains("尚无内容") || indexContent.contains("空目录") {
            let atomCount = store.listPages(domain).filter { $0.relativePath.hasPrefix("atoms/") }.count
            if atomCount > 0 {
                indexContent = "（知识库建设中：已完成第一级蒸馏 \(atomCount) 个知识原子，二级关系网组装尚未完成。以下知识来自原子层，请据此回答。）"
            }
        }

        // Load relations (Stage-3-aware: prefer shard index + matching shard).
        let relationsContent = loadRelationsForQuery(question: question)

        // Step 1: LLM page selection (v4-flash cheap call, 30 s timeout).
        let llmPaths = await selectPagesWithLLM(question: question, indexContent: indexContent)

        let pagesContent: String
        if !llmPaths.isEmpty {
            var parts: [String] = []; var total = 0
            for path in llmPaths {
                guard let content = store.readPage(domain: domain, rel: path) else { continue }
                let entry = "=== \(path) ===\n\(content)"
                if total + entry.count > maxContextChars { break }
                parts.append(entry); total += entry.count
            }
            pagesContent = parts.isEmpty ? keywordPages(question: question) : parts.joined(separator: "\n\n")
        } else {
            pagesContent = keywordPages(question: question)
        }

        // Step 2: build messages and start streaming (v4-flash).
        let prompt = WikiSchema.query(indexContent: indexContent,
                                      relationsContent: relationsContent,
                                      pagesContent: pagesContent,
                                      question: question)
        var messages: [[String: String]] = [["role": "system", "content": WikiSchema.defaultSchema]]
        for m in history where (m["role"] == "user" || m["role"] == "assistant") && !(m["content"]?.isEmpty ?? true) {
            messages.append(m)
        }
        messages.append(["role": "user", "content": prompt])
        return client.stream(messages: messages, model: DeepSeekClient.queryModel)
    }

    /// Step 1 of query: ask v4-flash (cheap) to pick pages from the index.
    /// Returns [] on failure → caller falls back to keyword scoring.
    private func selectPagesWithLLM(question: String, indexContent: String) async -> [String] {
        guard !indexContent.contains("尚无内容"), !indexContent.contains("空目录"),
              !indexContent.contains("知识库建设中") else { return [] }
        let truncatedIndex = indexContent.count > indexSelectCapChars
            ? String(indexContent.prefix(indexSelectCapChars)) + "…（目录已截断）"
            : indexContent
        do {
            let resp = try await client.complete(
                messages: [
                    ["role": "system", "content": "精确输出页面路径，每行一个，不要其他内容。"],
                    ["role": "user",   "content": WikiSchema.pageSelect(question: question,
                                                                        indexContent: truncatedIndex)],
                ],
                model: DeepSeekClient.queryModel,
                temperature: 0.0,
                timeout: 30)
            return resp.split(separator: "\n")
                .map {
                    $0.trimmingCharacters(in: .whitespaces)
                      .trimmingCharacters(in: CharacterSet(charactersIn: "-• "))
                }
                .filter { $0.hasSuffix(".md") && $0 != "log.md" }
                .prefix(6)
                .map { String($0) }
        } catch {
            return []
        }
    }

    /// Stage-3-aware relations loading for query context.
    /// If shards exist (relations/_index.md), load the index + keyword-matched shard(s).
    /// Otherwise fall back to the monolithic relations.md (truncated to cap).
    private func loadRelationsForQuery(question: String) -> String {
        if let shardIndex = store.readPage(domain: domain, rel: "relations/_index.md") {
            var parts = ["=== relations/_index.md ===\n\(shardIndex)"]
            var total = parts[0].count

            // Find and load the most relevant shard(s).
            let shards = store.listPages(domain)
                .filter { $0.relativePath.hasPrefix("relations/") && $0.relativePath != "relations/_index.md" }
            let scored: [(Int, String, String)] = shards.compactMap { ref in
                guard let content = store.readPage(domain: domain, rel: ref.relativePath) else { return nil }
                return (WikiManager.scoreText(question: question, content: content), ref.relativePath, content)
            }.sorted { $0.0 > $1.0 }

            for (_, path, content) in scored.prefix(2) {
                let entry = "=== \(path) ===\n\(content)"
                if total + entry.count > relationsCapChars { break }
                parts.append(entry); total += entry.count
            }
            return parts.joined(separator: "\n\n")
        }

        // Fall back to monolithic relations.md.
        var rel = store.readPage(domain: domain, rel: "relations.md") ?? "（暂无显式关系网页面）"
        if rel.count > relationsCapChars {
            rel = String(rel.prefix(relationsCapChars)) + "\n\n（关系网内容过长，已截断）"
        }
        return rel
    }

    /// Keyword-scored page selection (no API call) — fallback for Step 1.
    private func keywordPages(question: String) -> String {
        var curated: [(Int, String, String)] = []
        var atoms:   [(Int, String, String)] = []
        for ref in store.listPages(domain) {
            if ref.relativePath == "log.md" { continue }
            let content = store.readPage(domain: domain, rel: ref.relativePath) ?? ""
            let score = Self.scoreText(question: question, content: content)
            let tuple = (score, ref.relativePath, content)
            if ref.relativePath.hasPrefix("atoms/") { atoms.append(tuple) } else { curated.append(tuple) }
        }
        func pack(_ list: [(Int, String, String)], minScore: Int) -> String {
            let sorted = list.sorted { $0.0 > $1.0 }
            var parts: [String] = []; var total = 0
            for (sc, path, content) in sorted {
                if sc < minScore { break }
                let entry = "=== \(path) ===\n\(content)"
                if total + entry.count > maxContextChars { break }
                parts.append(entry); total += entry.count
            }
            return parts.joined(separator: "\n\n")
        }
        let c = pack(curated, minScore: 1)
        if !c.isEmpty { return c }
        let a = pack(atoms, minScore: 1)
        if !a.isEmpty {
            return a + "\n\n【提示：二级关系组装尚未完成，以下来自第一级知识原子，可能不够完整。】"
        }
        return "(无匹配页面。目录:\n\(store.readPage(domain: domain, rel: "index.md") ?? ""))"
    }

    static func scoreText(question: String, content: String) -> Int {
        let lower = content.lowercased()
        var score = 0
        for w in question.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }) where w.count > 1 {
            if lower.contains(w) { score += 1 }
        }
        let cjk = question.filter { $0 >= "\u{4E00}" && $0 <= "\u{9FFF}" }
        let chars = Array(cjk)
        if chars.count >= 2 {
            for i in 0..<(chars.count - 1) {
                let bg = String(chars[i...i+1])
                if content.contains(bg) { score += 2 }
            }
        }
        for ch in Set(chars) where content.contains(ch) { score += 1 }
        return score
    }

    // MARK: - FILE_WRITE parsing / applying

    func parseFileBlocks(_ text: String) -> [(String, String)] {
        let pattern = #"<<<FILE:\s*([^\n>]{1,200})>>>[^\n]*\n([\s\S]*?)<<<END(?:>>>|>>|>|)"#
        guard let re = try? NSRegularExpression(pattern: pattern) else { return [] }
        let ns = text as NSString
        var out: [(String, String)] = []
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let path    = ns.substring(with: m.range(at: 1)).trimmingCharacters(in: .whitespacesAndNewlines)
            let content = ns.substring(with: m.range(at: 2)).trimmingCharacters(in: .whitespacesAndNewlines)
            out.append((path, content))
        }
        return out
    }

    func applyFileBlocks(_ blocks: [(String, String)]) -> (Int, Int) {
        var created = 0, updated = 0
        for (rel, content) in blocks {
            guard store.isSafe(domain: domain, rel: rel) else {
                log("已拦截越界写入：\(rel)", .warn); continue
            }
            let existed = store.readPage(domain: domain, rel: rel) != nil
            store.writePage(domain: domain, rel: rel, content: content)
            if existed { updated += 1 } else { created += 1 }
        }
        return (created, updated)
    }

    // MARK: - Network / atom helpers

    private func listAtoms() -> [URL] {
        let dir = store.wikiDir(domain).appendingPathComponent("atoms", isDirectory: true)
        return (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil))?
            .filter { $0.pathExtension == "md" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent } ?? []
    }

    private func unprocessedSources() -> [URL] {
        let ingested = store.sentinelSet(domain: domain, name: ".ingested")
        let skip = try? NSRegularExpression(
            pattern: #"(?i)(^|_)(news_)?list(_|$)|(^|_)index(_|$)|(^|_)column(_|$)|(^|_)node(_|$)"#)
        return store.listScraped(domain).filter { url in
            let name = url.lastPathComponent
            if ingested.contains(name) { return false }
            let stem = (name as NSString).deletingPathExtension
            if let skip,
               skip.firstMatch(in: stem, range: NSRange(location: 0, length: (stem as NSString).length)) != nil {
                return false
            }
            return true
        }
    }

    private func atomSlug(_ sourceName: String) -> String {
        let stem = (sourceName as NSString).deletingPathExtension
        var slug = stem.lowercased().replacingOccurrences(
            of: "[^a-z0-9\u{4E00}-\u{9FFF}]+", with: "-", options: .regularExpression)
        slug = slug.trimmingCharacters(in: ["-"])
        if slug.isEmpty { slug = "atom" }
        slug = String(slug.prefix(70))
        return "\(slug)-\(Store.stableHash(sourceName)).md"
    }

    /// Read the curated network pages to feed as "existing" context to Stage-2 assembly.
    ///
    /// Fix: previously large entries were `continue`-d (silently dropped), which meant
    /// relations.md could vanish from context once it grew past the cap.  Now we
    /// truncate entries to the remaining budget so they always appear, just shorter.
    private func readNetworkPages(cap: Int) -> String {
        var ordered = ["relations.md", "index.md"]
        for ref in store.listPages(domain) {
            let p = ref.relativePath
            if ordered.contains(p) || p.hasPrefix("atoms/") || p.hasPrefix("relations/") || p == "log.md" { continue }
            ordered.append(p)
        }
        var parts: [String] = []; var total = 0; var seen = Set<String>()
        for rel in ordered {
            if seen.contains(rel) { continue }; seen.insert(rel)
            guard let content = store.readPage(domain: domain, rel: rel) else { continue }
            var entry = "=== \(rel) ===\n\(content)"
            let remaining = cap - total
            guard remaining > 200 else { break }       // not worth a tiny fragment
            if entry.count > remaining {
                // Truncate to remaining budget; don't drop silently.
                entry = String(entry.prefix(remaining)) + "\n…（已截断）"
            }
            parts.append(entry); total += entry.count
        }
        return parts.joined(separator: "\n\n")
    }

    private func chunkByBudget(_ atoms: [URL], budget: Int) -> [[URL]] {
        var chunks: [[URL]] = []; var cur: [URL] = []; var curLen = 0
        for a in atoms {
            let len = (try? String(contentsOf: a, encoding: .utf8).count) ?? 0
            if !cur.isEmpty && curLen + len > budget { chunks.append(cur); cur = []; curLen = 0 }
            cur.append(a); curLen += len
        }
        if !cur.isEmpty { chunks.append(cur) }
        return chunks
    }

    private func joinAtoms(_ atoms: [URL]) -> String {
        atoms.compactMap { try? String(contentsOf: $0, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines) }
            .joined(separator: "\n\n---\n\n")
    }
}
