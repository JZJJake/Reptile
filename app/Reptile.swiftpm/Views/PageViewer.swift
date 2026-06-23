import SwiftUI

/// Sheet that displays a wiki page or a raw source file, with citations inside
/// it remaining tappable (push-navigation via a small history stack).
struct PageViewer: View {
    let domain: String
    @State var stack: [Entry]
    @Environment(\.dismiss) private var dismiss

    struct Entry: Identifiable {
        let id = UUID()
        var title: String
        var content: String
        var isSource: Bool
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.bg.ignoresSafeArea()
                ScrollView {
                    if let top = stack.last {
                        VStack(alignment: .leading, spacing: 10) {
                            if top.isSource {
                                Text("📄 原始文件：\(top.title)")
                                    .font(.caption).foregroundColor(Theme.cite)
                                    .padding(6).background(Theme.panel).cornerRadius(6)
                            }
                            MarkdownView(text: top.content,
                                         onWikiLink: openWiki,
                                         onSourceLink: openSource)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding()
                    } else {
                        Text("内容为空").foregroundColor(Theme.muted).padding()
                    }
                }
            }
            .navigationTitle(stack.last?.title ?? "")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarColorScheme(.dark, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if stack.count > 1 {
                        Button { stack.removeLast() } label: { Image(systemName: "chevron.left") }
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("关闭") { dismiss() }
                }
            }
        }
    }

    // Resolve a [[name]] citation to a wiki page (filename-stem, then title match).
    private func openWiki(_ name: String) {
        if let content = WikiFinder.find(domain: domain, name: name) {
            stack.append(Entry(title: name, content: content, isSource: false))
        } else {
            stack.append(Entry(title: name, content: "未找到知识库页面：[[\(name)]]", isSource: false))
        }
    }

    private func openSource(_ file: String) {
        let url = Store.shared.scrapedDir(domain).appendingPathComponent(file)
        if let content = try? String(contentsOf: url, encoding: .utf8) {
            stack.append(Entry(title: file, content: content, isSource: true))
        } else {
            stack.append(Entry(title: file, content: "未找到源文件：\(file)", isSource: true))
        }
    }
}

/// Resolves a [[citation]] to a wiki page file. Pass 1: filename-stem match.
/// Pass 2: page-title match (each page's first `# Heading`, CJK-slug compared).
/// Ported from main.wiki_find_page.
enum WikiFinder {
    static func find(domain: String, name: String) -> String? {
        let store = Store.shared
        let pages = store.listPages(domain)
        let target = slug(name)

        // Pass 1: filename stem.
        for ref in pages {
            let stem = (ref.displayName as NSString).deletingPathExtension
            if slug(stem) == target || stem == name {
                return store.readPage(domain: domain, rel: ref.relativePath)
            }
        }
        // Pass 2: first heading match.
        for ref in pages where !ref.relativePath.hasPrefix("atoms/") {
            guard let content = store.readPage(domain: domain, rel: ref.relativePath) else { continue }
            if let firstHeading = content.components(separatedBy: "\n")
                .first(where: { $0.hasPrefix("# ") })?.dropFirst(2) {
                if slug(String(firstHeading)) == target { return content }
            }
        }
        return nil
    }

    static func slug(_ s: String) -> String {
        // Non-raw literal: Swift expands \u{...} to real CJK chars for ICU.
        s.lowercased().replacingOccurrences(
            of: "[^a-z0-9\u{4E00}-\u{9FFF}]+", with: "-", options: .regularExpression)
            .trimmingCharacters(in: ["-"])
    }
}
