import Foundation

/// BFS crawl orchestration on top of WebScraper — the iOS equivalent of
/// scraper.crawl_worker. Saves each page via Store and reports progress.
///
/// WKWebView can only be driven from the main actor, so crawling is sequential
/// (one offscreen web view at a time). That is acceptable on-device and avoids
/// hammering a domain; a per-page settle delay also acts as a light rate limit.
@MainActor
final class CrawlEngine: ObservableObject {
    @Published var logs: [LogLine] = []
    @Published var running = false
    @Published var scrapedCount = 0

    private var cancelled = false
    private let scraper = WebScraper()

    func log(_ msg: String, _ level: LogLine.Level = .log) {
        logs.append(LogLine(level: level, message: msg))
    }

    func cancel() { cancelled = true }

    /// Crawl starting at `startURL`. `mode`/`dateFrom`/`maxPages` mirror the
    /// web UI's download-mode selector.
    func crawl(startURL: String, mode: DownloadMode, dateFromYYYYMM: String,
               maxPages: Int) async -> String {
        running = true
        cancelled = false
        scrapedCount = 0
        defer { running = false }

        let domain = Store.host(of: startURL)
        log("开始采集：\(startURL)（模式：\(mode.rawValue)，域：\(domain)）", .info)

        let cutoff = parseCutoff(dateFromYYYYMM)
        var queue: [String] = [startURL]
        var seen: Set<String> = [normalize(startURL)]
        let startHost = URLComponents(string: startURL)?.host

        while !queue.isEmpty && scrapedCount < maxPages {
            if cancelled { log("已取消采集", .warn); break }
            let url = queue.removeFirst()
            do {
                let page = try await scraper.extract(urlString: url)

                // Date filter (only in 按日期 mode).
                if mode == .byDate, let cutoff = cutoff,
                   let d = parseDate(page.publishDate), d < cutoff {
                    log("📅 跳过（早于 \(dateFromYYYYMM)）：\(page.title)", .warn)
                } else if page.text.count < 80 {
                    log("内容过短，跳过：\(url)", .warn)
                } else {
                    let file = Store.shared.saveScraped(domain: domain, page: page)
                    scrapedCount += 1
                    log("✅ [\(scrapedCount)] \(page.title) → \(file)", .success)
                }

                // Link discovery (skip in 单页 mode).
                if mode != .single {
                    for link in page.links where scrapedCount + queue.count < maxPages {
                        let key = normalize(link)
                        if seen.contains(key) { continue }
                        if URLComponents(string: link)?.host != startHost { continue }
                        seen.insert(key)
                        queue.append(link)
                    }
                }
            } catch {
                log("抓取失败 \(url)：\(error.localizedDescription)", .error)
            }
        }

        log("采集完成：共保存 \(scrapedCount) 篇文档到 scraped_data/\(domain)/", .done)
        return domain
    }

    // MARK: helpers

    private func normalize(_ url: String) -> String {
        var s = url.lowercased()
        if let i = s.firstIndex(of: "#") { s = String(s[..<i]) }
        if s.hasSuffix("/") { s.removeLast() }
        return s
    }

    private func parseCutoff(_ yyyymm: String) -> Date? {
        guard yyyymm.count >= 7 else { return nil }
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM"
        return f.date(from: String(yyyymm.prefix(7)))
    }

    private func parseDate(_ s: String) -> Date? {
        guard !s.isEmpty else { return nil }
        let patterns = ["yyyy-MM-dd", "yyyy/MM/dd", "yyyy年MM月dd日", "yyyy年M月d日", "yyyy-MM-dd'T'HH:mm:ss"]
        let f = DateFormatter()
        for p in patterns {
            f.dateFormat = p
            if let d = f.date(from: s) { return d }
        }
        // Loose YYYY-?MM-?DD via regex fallback.
        if let m = s.range(of: #"(\d{4})\D(\d{1,2})\D(\d{1,2})"#, options: .regularExpression) {
            let comps = s[m].split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
            if comps.count == 3 {
                var dc = DateComponents()
                dc.year = comps[0]; dc.month = comps[1]; dc.day = comps[2]
                return Calendar.current.date(from: dc)
            }
        }
        return nil
    }
}
