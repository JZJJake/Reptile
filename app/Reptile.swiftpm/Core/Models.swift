import Foundation

// MARK: - Crawl / extraction

/// A page extracted from the live web via WKWebView + injected JS.
/// Mirrors what scraper.py produces from a Playwright page.
struct ExtractedPage {
    var url: String
    var title: String
    var text: String          // cleaned main-content text (markdown-ish)
    var html: String          // raw outerHTML (kept for debugging / re-parse)
    var publishDate: String   // best-effort date string, "" if none found
    var links: [String]       // same-host absolute links discovered on the page
}

enum DownloadMode: String, CaseIterable, Identifiable {
    case all     = "全站"
    case single  = "单页"
    case byDate  = "按日期"
    var id: String { rawValue }
}

/// One line in the live crawl/build console.
struct LogLine: Identifiable, Equatable {
    enum Level: String {
        case info, log, success, warn, error, done
    }
    let id = UUID()
    var level: Level
    var message: String
    var time: Date = Date()
}

// MARK: - Chat

struct ChatMessage: Identifiable, Equatable {
    enum Role: String { case user, assistant }
    let id = UUID()
    var role: Role
    var content: String
    var rendered: Bool = false   // streamed plain → rendered markdown when complete
}

// MARK: - Wiki

/// A reference to a wiki page on disk (relative path under wiki/{domain}/).
struct WikiPageRef: Identifiable, Hashable {
    var id: String { relativePath }
    var relativePath: String          // e.g. "concepts/transformer.md"
    var category: String {            // top-level folder, or "root"
        let parts = relativePath.split(separator: "/")
        return parts.count > 1 ? String(parts[0]) : "root"
    }
    var displayName: String {
        (relativePath as NSString).lastPathComponent
    }
}

/// Result of an ingest run (mirrors WikiManager.ingest()'s dict).
struct IngestResult {
    var pagesCreated: Int = 0
    var pagesUpdated: Int = 0
    var atomsMade: Int = 0
    var totalAtoms: Int = 0
    var noSources: Bool = false
}

// MARK: - Errors

enum ReptileError: LocalizedError {
    case http(String)
    case timeout
    case extractionFailed
    case noAPIKey
    case cancelled

    var errorDescription: String? {
        switch self {
        case .http(let m):       return "网络/接口错误：\(m)"
        case .timeout:           return "请求超时"
        case .extractionFailed:  return "页面内容抽取失败"
        case .noAPIKey:          return "尚未配置 DeepSeek API Key"
        case .cancelled:         return "已取消"
        }
    }
}
