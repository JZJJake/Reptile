import Foundation

/// Karpathy LLM-Wiki manager — Swift port of wiki/wiki_manager.py, including
/// the recent fixes: incremental Stage-2 assembly (.stage2_done), path
/// containment for FILE_WRITE blocks, and a relations.md size cap in query().
@MainActor
final class WikiManager {
    var logs: [LogLine] = []
    var onLog: ((LogLine) -> Void)?

    let domain: String
    private let client: DeepSeekClient
    private let store = Store.shared

    // Budgets (chars — coarse but matches the Python char-based caps).
    private let maxContextChars = 80_000
    private let relationsCapChars = 20_000
    private let stage1DocChars = 40_000
    private let assemblyBudgetChars = 120_000   // ~60k tokens, CJK-heavy
    private let existingNetworkCapChars = 60_000

    init(domain: String, apiKey: String) {
        self.domain = domain
        self.client = DeepSeekClient(apiKey: apiKey)
    }

    private func log(_ m: String, _ l: LogLine.Level = .log) {
        let line = LogLine(level: l, message: m)
        logs.append(line)
        onLog?(line)
    }

    // MARK: - Ingest (two-stage)

    @discardableResult
    func ingest() async -> IngestResult {
        let sources = unprocessedSources()
        let existingAtoms = listAtoms()
        if sources.isEmpty && existingAtoms.isEmpty {
            log("未发现待处理文档（scraped_data/\(domain)/ 下无新 .md 文件）", .warn)
            return IngestResult(noSources: true)
        }

        // Stage 1 — distill each new source into a compact atom.
        var atomsMade = 0
        if !sources.isEmpty {
            log("发现 \(sources.count) 篇新文档，开始两级蒸馏建设知识库", .info)
            atomsMade = await stage1DistillAll(sources)
        }

        // Stage 2 — assemble newly-distilled atoms into the relation network.
        let (created, updated) = await stage2Assemble()

        let total = listAtoms().count
        store.appendLog(domain: domain, operation: "ingest",
                        detail: "stage1_new_atoms=\(atomsMade) total_atoms=\(total) created=\(created) updated=\(updated)")
        log("知识库建设完成：蒸馏 \(total) 个知识原子 → 组装关系网（新建 \(created) 页 / 更新 \(updated) 页）", .done)
        return IngestResult(pagesCreated: created, pagesUpdated: updated,
                            atomsMade: atomsMade, totalAtoms: total)
    }

    // MARK: Stage 1

    private func stage1DistillAll(_ sources: [URL]) async -> Int {
        let done = store.sentinelSet(domain: domain, name: ".stage1_done")
        let todo = sources.filter { !done.contains($0.lastPathComponent) }
        if todo.isEmpty { log("阶段①：所有文档已蒸馏（断点续传）", .info); return 0 }

        log("阶段① 单篇蒸馏：\(todo.count) 篇文档 → 知识原子", .info)
        var made = 0
        for (i, src) in todo.enumerated() {
            if let atom = await distillOne(src) {
                let name = atomSlug(src.lastPathComponent)
                store.writePage(domain: domain, rel: "atoms/\(name)", content: atom)
                store.sentinelAppend(domain: domain, name: ".stage1_done", lines: [src.lastPathComponent])
                store.sentinelAppend(domain: domain, name: ".ingested", lines: [src.lastPathComponent])
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
                    ["role": "user", "content": WikiSchema.distillAtom(filename: name, body: body)],
                ],
                model: DeepSeekClient.buildModel,   // 知识库建设：v4-pro
                temperature: 0.2)
            atom = atom.trimmingCharacters(in: .whitespacesAndNewlines)
            if atom.isEmpty { log("蒸馏结果为空：\(name)", .warn); return nil }
            // Enforce source pointer (anti error-propagation), like the Python.
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

    // MARK: Stage 2

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
            log("阶段② 关系组装：\(newAtoms.count) 个原子一次性全量组装", .info)
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
                    ["role": "user", "content": prompt],
                ],
                model: DeepSeekClient.buildModel,   // 知识库建设：v4-pro
                timeout: DeepSeekClient.assemblyTimeout)
            let blocks = parseFileBlocks(resp)
            if blocks.isEmpty { log("\(label)：未输出有效 FILE_WRITE 块（格式不符）", .warn) }
            return applyFileBlocks(blocks)
        } catch {
            log("\(label) 失败：\(error.localizedDescription)", .error)
            return (0, 0)
        }
    }

    // MARK: - Query (two-step)

    /// Stream an answer from the wiki. `history` is prior [role,content] turns.
    func query(question: String, history: [[String: String]]) -> AsyncThrowingStream<String, Error> {
        var indexContent = store.readPage(domain: domain, rel: "index.md") ?? "(空目录——知识库尚无内容)"
        if indexContent.contains("尚无内容") || indexContent.contains("空目录") {
            let atomCount = store.listPages(domain).filter { $0.relativePath.hasPrefix("atoms/") }.count
            if atomCount > 0 {
                indexContent = "（知识库建设中：已完成第一级蒸馏 \(atomCount) 个知识原子，二级关系网组装尚未完成。以下知识来自原子层，请据此回答。）"
            }
        }

        var relations = store.readPage(domain: domain, rel: "relations.md") ?? "（暂无显式关系网页面）"
        if relations.count > relationsCapChars {
            relations = String(relations.prefix(relationsCapChars)) + "\n\n（关系网内容过长，已截断）"
        }

        let pages = selectRelevantPages(question: question)
        let prompt = WikiSchema.query(indexContent: indexContent, relationsContent: relations,
                                      pagesContent: pages, question: question)

        var messages: [[String: String]] = [["role": "system", "content": WikiSchema.defaultSchema]]
        for m in history where (m["role"] == "user" || m["role"] == "assistant") && !(m["content"]?.isEmpty ?? true) {
            messages.append(m)
        }
        messages.append(["role": "user", "content": prompt])
        return client.stream(messages: messages, model: DeepSeekClient.queryModel)  // 问答：v4-flash
    }

    /// Keyword-scored page selection (no extra API call) — a pragmatic stand-in
    /// for the Python's LLM page-select + keyword fallback. Curated pages first,
    /// then atoms.
    private func selectRelevantPages(question: String) -> String {
        var curated: [(Int, String, String)] = []
        var atoms: [(Int, String, String)] = []
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
        // ASCII words
        for w in question.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }) where w.count > 1 {
            if lower.contains(w) { score += 1 }
        }
        // CJK bigrams + singletons
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
        // <<<FILE: path>>> ... <<<END(>>>|>>|>|)  — matches truncated END variants.
        let pattern = #"<<<FILE:\s*([^\n>]{1,200})>>>[^\n]*\n([\s\S]*?)<<<END(?:>>>|>>|>|)"#
        guard let re = try? NSRegularExpression(pattern: pattern) else { return [] }
        let ns = text as NSString
        var out: [(String, String)] = []
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let path = ns.substring(with: m.range(at: 1)).trimmingCharacters(in: .whitespacesAndNewlines)
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

    // MARK: - atoms / network helpers

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
            if let skip = skip,
               skip.firstMatch(in: stem, range: NSRange(location: 0, length: (stem as NSString).length)) != nil {
                return false
            }
            return true
        }
    }

    private func atomSlug(_ sourceName: String) -> String {
        let stem = (sourceName as NSString).deletingPathExtension
        // Non-raw literal so Swift expands \u{...} to the real CJK chars before
        // ICU sees the pattern (ICU does not understand \u{NNNN} brace syntax).
        var slug = stem.lowercased().replacingOccurrences(
            of: "[^a-z0-9\u{4E00}-\u{9FFF}]+", with: "-", options: .regularExpression)
        slug = slug.trimmingCharacters(in: ["-"])
        if slug.isEmpty { slug = "atom" }
        slug = String(slug.prefix(70))
        return "\(slug)-\(Store.stableHash(sourceName)).md"
    }

    private func readNetworkPages(cap: Int) -> String {
        let priority = ["relations.md", "index.md"]
        var ordered = priority
        for ref in store.listPages(domain) {
            let p = ref.relativePath
            if priority.contains(p) || p.hasPrefix("atoms/") || p == "log.md" { continue }
            ordered.append(p)
        }
        var parts: [String] = []; var total = 0; var seen = Set<String>()
        for rel in ordered {
            if seen.contains(rel) { continue }; seen.insert(rel)
            guard let content = store.readPage(domain: domain, rel: rel) else { continue }
            let entry = "=== \(rel) ===\n\(content)"
            if total + entry.count > cap { continue }
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
