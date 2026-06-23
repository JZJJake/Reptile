import Foundation

/// File-based persistence layer — the iOS sandbox equivalent of the web
/// project's `scraped_data/{domain}/` and `wiki/{domain}/` directory trees.
///
/// Layout under Documents/:
///   scraped_data/{domain}/<slug>.md          raw crawled docs (immutable)
///   wiki/{domain}/index.md, relations.md     LLM-curated knowledge base
///   wiki/{domain}/atoms/<slug>.md            Stage-1 knowledge atoms
///   wiki/{domain}/concepts|entities|...      Stage-2 curated pages
///   wiki/{domain}/.stage1_done               sentinel: sources distilled
///   wiki/{domain}/.stage2_done               sentinel: atoms assembled
///   wiki/{domain}/.ingested                  sentinel: sources fully ingested
///
/// All methods are synchronous; callers hop off the main actor where needed.
struct Store {
    static let shared = Store()

    private let fm = FileManager.default

    var documents: URL {
        fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }
    var scrapedRoot: URL { documents.appendingPathComponent("scraped_data", isDirectory: true) }
    var wikiRoot: URL    { documents.appendingPathComponent("wiki", isDirectory: true) }

    // MARK: domain helpers

    func scrapedDir(_ domain: String) -> URL {
        scrapedRoot.appendingPathComponent(domain, isDirectory: true)
    }
    func wikiDir(_ domain: String) -> URL {
        wikiRoot.appendingPathComponent(domain, isDirectory: true)
    }

    func ensureDir(_ url: URL) {
        try? fm.createDirectory(at: url, withIntermediateDirectories: true)
    }

    // MARK: scraped docs

    /// Save one crawled page as a markdown file with YAML-ish frontmatter,
    /// mirroring scraper.py's save format. Returns the saved filename.
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

    // MARK: wiki pages

    func readPage(domain: String, rel: String) -> String? {
        try? String(contentsOf: wikiDir(domain).appendingPathComponent(rel), encoding: .utf8)
    }

    func writePage(domain: String, rel: String, content: String) {
        let target = wikiDir(domain).appendingPathComponent(rel)
        ensureDir(target.deletingLastPathComponent())
        try? content.write(to: target, atomically: true, encoding: .utf8)
    }

    /// Path-containment guard: reject FILE_WRITE paths that escape this domain's
    /// wiki dir (".." traversal OR sibling-domain prefixes). Ported from the
    /// fix in wiki_manager._apply_file_blocks.
    func isSafe(domain: String, rel: String) -> Bool {
        let root = wikiDir(domain).standardizedFileURL
        let target = wikiDir(domain).appendingPathComponent(rel).standardizedFileURL
        if target == root { return true }
        // target must have root as an ancestor component-wise
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

    // MARK: sentinel sets (.stage1_done / .stage2_done / .ingested)

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

    func deleteWiki(_ domain: String) {
        try? fm.removeItem(at: wikiDir(domain))
    }

    // MARK: utilities

    /// URL → safe flat slug + md5-ish hash suffix, mirroring scraper.url_to_slug.
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

    /// Deterministic FNV-1a hash → 8 hex chars. Swift's `hashValue` is seeded
    /// per process, so it must NOT be used for on-disk slugs (filenames would
    /// change every launch, breaking dedup / resume).
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
