import Foundation

// MARK: - Crawl / extraction

struct ExtractedPage {
    var url: String
    var title: String
    var text: String
    var html: String
    var publishDate: String
    var links: [String]
}

enum DownloadMode: String, CaseIterable, Identifiable {
    case all    = "全站"
    case single = "单页"
    case byDate = "按日期"
    var id: String { rawValue }
}

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
    var rendered: Bool = false
}

// MARK: - Wiki

struct WikiPageRef: Identifiable, Hashable {
    var id: String { relativePath }
    var relativePath: String
    var category: String {
        let parts = relativePath.split(separator: "/")
        return parts.count > 1 ? String(parts[0]) : "root"
    }
    var displayName: String {
        (relativePath as NSString).lastPathComponent
    }
}

struct IngestResult {
    var pagesCreated: Int = 0
    var pagesUpdated: Int = 0
    var atomsMade: Int = 0
    var totalAtoms: Int = 0
    var noSources: Bool = false
}

// MARK: - Build modes

/// Top-level mode selector in BuildView.
enum BuildMode: String, CaseIterable, Identifiable {
    case normal      = "构建/更新"
    case importFiles = "导入文件"
    case rebuild     = "版本重建"
    var id: String { rawValue }
}

/// Granularity of a forced rebuild (version upgrade path).
enum RebuildLevel: String, CaseIterable, Identifiable {
    case stage2Only = "保留原子，重建Stage-2"
    case full       = "全量重建（重新蒸馏）"
    var id: String { rawValue }
    var warning: String {
        switch self {
        case .stage2Only:
            return "将清除 index / relations / concepts / entities / synthesis 页面并保留已蒸馏的知识原子，然后用现有原子重新组装关系网。适合 Stage-2 prompt 升级。"
        case .full:
            return "将清除整个知识库（含知识原子），从原始爬取文件完全重新蒸馏。适合 Stage-1 prompt 或数据结构重大升级，耗时最长。"
        }
    }
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
