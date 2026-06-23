import Foundation

/// File-based persistence layer — the iOS sandbox equivalent of the web
/// project's `scraped_data/{domain}/` and `wiki/{domain}/` directory trees.
///
/// Layout under Documents/:
///   scraped_data/{domain}/<slug>.md          raw crawled docs (immutable)
///   wiki/{domain}/index.md, relations.md     LLM-curated knowledge base
///   wiki/{domain}/relations/_index.md        Stage-3 shard index (derived)
///   wiki/{domain}/relations/<topic>.md       Stage-3 topic shards (derived)
///   wiki/{domain}/atoms/<slug>.md            Stage-1 knowledge atoms
///   wiki/{domain}/concepts|entities|...      Stage-2 curated pages
///   wiki/{domain}/.stage1_done               sentinel: sources distilled
///   wiki/{domain}/.stage2_done               sentinel: atoms assembled
///   wiki/{domain}/.ingested                  sentinel: sources fully ingested
struct Store {
    static let shared = Store()

    private let fm = FileManager.default

    var documents: URL {
        fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }
    var scrapedRoot: URL { documents.appendingPathComponent("scraped_data", isDirectory: true) }
    var wikiRoot: URL    { documents.appendingPathComponent("wiki", isDirectory: true) }

    // MARK: - domain helpers

    func scrapedDir(_ domain: String) -> URL {
        scrapedRoot.appendingPathComponent(domain, isDirectory: true)
    }
    func wikiDir(_ domain: String) -> URL {
        wikiRoot.appendingPathComponent(domain, isDirectory: true)
    }

    func ensureDir(_ url: URL) {
        try? fm.createDirectory(at: url, withIntermediateDirectories: true)
    }

    // MARK: - scraped docs

    @discardableResult
    func saveScraped(domain: String, page: ExtractedPage) -> String {
        let dir = scrapedDir(domain)
        ensureDir(dir)
        let slug = Self.urlToSlug(page.url)
        let filename = "\(slug).md"
        let body = """
        ---
        url: \(page.url)
        title: \(page.title)
        date: \(page.publishDate)
        ---

        # \(page.title)

        \(page.text)
        """
        try? body.write(to: dir.appendingPathComponent(filename), atomically: true, encoding: .utf8)
        return filename
    }

    func listScraped(_ domain: String) -> [URL] {
        (try? fm.contentsOfDirectory(at: scrapedDir(domain),
                                     includingPropertiesForKeys: nil))?
            .filter { $0.pathExtension == "md" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent } ?? []
    }

    func listScrapedDomains() -> [String] {
        (try? fm.contentsOfDirectory(at: scrapedRoot, includingPropertiesForKeys: nil))?
            .filter { $0.hasDirectoryPath }
            .map { $0.lastPathComponent }
            .sorted() ?? []
    }

    func scrapedFileCount(_ domain: String) -> Int {
        listScraped(domain).count
    }

    /// Copy an external file (from Files app / document picker security scope)
    /// into scraped_data/{domain}/. Returns true on success.
    @discardableResult
    func importFile(from src: URL, domain: String) -> Bool {
        ensureDir(scrapedDir(domain))
        let dest = scrapedDir(domain).appendingPathComponent(src.lastPathComponent)
        do {
            if fm.fileExists(atPath: dest.path) { try fm.removeItem(at: dest) }
            try fm.copyItem(at: src, to: dest)
            return true
        } catch { return false }
    }

    // MARK: - wiki pages

    func readPage(domain: String, rel: String) -> String? {
        try? String(contentsOf: wikiDir(domain).appendingPathComponent(rel), encoding: .utf8)
    }

    func writePage(domain: String, rel: String, content: String) {
        let target = wikiDir(domain).appendingPathComponent(rel)
        ensureDir(target.deletingLastPathComponent())
        try? content.write(to: target, atomically: true, encoding: .utf8)
    }

    /// Path-containment guard: reject FILE_WRITE paths that escape this domain's
    /// wiki dir (".." traversal OR sibling-domain prefixes).
    func isSafe(domain: String, rel: String) -> Bool {
        let root = wikiDir(domain).standardizedFileURL
        let target = wikiDir(domain).appendingPathComponent(rel).standardizedFileURL
        if target == root { return true }
        let rootParts = root.pathComponents
        let targetParts = target.pathComponents
        guard targetParts.count > rootParts.count else { return false }
        return Array(targetParts.prefix(rootParts.count)) == rootParts
    }

    func listPages(_ domain: String) -> [WikiPageRef] {
        let root = wikiDir(domain)
        guard let en = fm.enumerator(at: root, includingPropertiesForKeys: nil) else { return [] }
        var out: [WikiPageRef] = []
        let base = root.standardizedFileURL.path
        for case let u as URL in en where u.pathExtension == "md" {
            var p = u.standardizedFileURL.path
            if p.hasPrefix(base) { p = String(p.dropFirst(base.count)).trimmingCharacters(in: ["/"]) }
            out.append(WikiPageRef(relativePath: p))
        }
        return out.sorted { $0.relativePath < $1.relativePath }
    }

    func wikiExists(_ domain: String) -> Bool {
        fm.fileExists(atPath: wikiDir(domain).path)
    }

    func listWikiDomains() -> [String] {
        (try? fm.contentsOfDirectory(at: wikiRoot, includingPropertiesForKeys: nil))?
            .filter { $0.hasDirectoryPath }
            .map { $0.lastPathComponent }
            .sorted() ?? []
    }

    func appendLog(domain: String, operation: String, detail: String) {
        let ts = Self.utcStamp()
        let line = "[\(ts)] \(operation): \(detail)\n"
        let url = wikiDir(domain).appendingPathComponent("log.md")
        ensureDir(url.deletingLastPathComponent())
        if let h = try? FileHandle(forWritingTo: url) {
            h.seekToEndOfFile()
            h.write(Data(line.utf8))
            try? h.close()
        } else {
            try? line.write(to: url, atomically: true, encoding: .utf8)
        }
    }

    // MARK: - sentinel sets

    func sentinelSet(domain: String, name: String) -> Set<String> {
        guard let txt = try? String(contentsOf: wikiDir(domain).appendingPathComponent(name),
                                    encoding: .utf8) else { return [] }
        return Set(txt.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
                      .filter { !$0.isEmpty })
    }

    func sentinelAppend(domain: String, name: String, lines: [String]) {
        guard !lines.isEmpty else { return }
        let url = wikiDir(domain).appendingPathComponent(name)
        ensureDir(url.deletingLastPathComponent())
        let text = lines.map { $0.trimmingCharacters(in: .whitespaces) }.joined(separator: "\n") + "\n"
        if let h = try? FileHandle(forWritingTo: url) {
            h.seekToEndOfFile()
            h.write(Data(text.utf8))
            try? h.close()
        } else {
            try? text.write(to: url, atomically: true, encoding: .utf8)
        }
    }

    func sentinelCount(domain: String, name: String) -> Int {
        sentinelSet(domain: domain, name: name).count
    }

    /// Delete a single sentinel file (used to invalidate a stage).
    func deleteSentinel(domain: String, name: String) {
        try? fm.removeItem(at: wikiDir(domain).appendingPathComponent(name))
    }

    // MARK: - wiki destruction helpers

    /// Delete the entire wiki/{domain}/ directory.
    func deleteWiki(_ domain: String) {
        try? fm.removeItem(at: wikiDir(domain))
    }

    /// Delete atoms/ directory (for full rebuild).
    func deleteAtoms(_ domain: String) {
        try? fm.removeItem(at: wikiDir(domain).appendingPathComponent("atoms"))
    }

    /// Delete Stage-3 shard directory relations/ (derived; Stage-2 keeps relations.md).
    func deleteRelationsShards(_ domain: String) {
        try? fm.removeItem(at: wikiDir(domain).appendingPathComponent("relations"))
    }

    /// Delete all curated wiki pages but keep atoms/ + .stage1_done + .ingested.
    /// Used by forceRebuildStage2() to start a clean Stage-2 assembly.
    func deleteCuratedPages(_ domain: String) {
        let wikiDir = self.wikiDir(domain)
        guard let contents = try? fm.contentsOfDirectory(at: wikiDir,
                                                         includingPropertiesForKeys: nil) else { return }
        let keep: Set<String> = ["atoms", ".stage1_done", ".ingested"]
        for item in contents {
            if keep.contains(item.lastPathComponent) { continue }
            try? fm.removeItem(at: item)
        }
    }

    // MARK: - utilities

    static func urlToSlug(_ url: String) -> String {
        let comps = URLComponents(string: url)
        var path = (comps?.path ?? "").trimmingCharacters(in: ["/"])
        path = path.replacingOccurrences(of: #"[^a-zA-Z0-9\-]"#, with: "_",
                                         options: .regularExpression)
        path = path.replacingOccurrences(of: #"_+"#, with: "_", options: .regularExpression)
        path = path.trimmingCharacters(in: ["_"])
        if path.isEmpty { path = "index" }
        path = String(path.prefix(60))
        return "\(path)_\(stableHash(url))"
    }

    /// Deterministic FNV-1a hash → 8 hex chars.
    static func stableHash(_ s: String) -> String {
        var h: UInt64 = 1469598103934665603
        for b in s.utf8 { h = (h ^ UInt64(b)) &* 1099511628211 }
        return String(format: "%08x", UInt32(truncatingIfNeeded: h))
    }

    static func host(of url: String) -> String {
        var h = URLComponents(string: url)?.host ?? url
        if h.hasPrefix("www.") { h.removeFirst(4) }
        return h.replacingOccurrences(of: #"[^a-zA-Z0-9_\-]"#, with: "_",
                                      options: .regularExpression)
    }

    static func utcStamp() -> String {
        let f = DateFormatter()
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd HH:mm 'UTC'"
        return f.string(from: Date())
    }
}
