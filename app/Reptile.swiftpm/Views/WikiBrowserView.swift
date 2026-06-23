import SwiftUI

/// File-tree browser for the generated knowledge base — pick a domain, browse
/// pages grouped by category, tap to read (with live citations). Mirrors the
/// wiki browser tab in static/index.html.
struct WikiBrowserView: View {
    @EnvironmentObject var session: AppSession
    @State private var domains: [String] = []
    @State private var domain: String = ""
    @State private var pages: [WikiPageRef] = []
    @State private var viewer: PageViewer.Entry?

    private var grouped: [(String, [WikiPageRef])] {
        Dictionary(grouping: pages.filter { $0.relativePath != "log.md" }) { $0.category }
            .sorted { categoryOrder($0.key) < categoryOrder($1.key) }
            .map { ($0.key, $0.value.sorted { $0.relativePath < $1.relativePath }) }
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.bg.ignoresSafeArea()
                VStack(spacing: 0) {
                    picker
                    if pages.isEmpty {
                        Spacer()
                        Text(domains.isEmpty ? "尚无知识库，请先构建。" : "该知识库暂无页面。")
                            .foregroundColor(Theme.muted)
                        Spacer()
                    } else {
                        list
                    }
                }
            }
            .navigationTitle("知识库浏览")
            .toolbarColorScheme(.dark, for: .navigationBar)
            .onAppear(perform: refresh)
            .sheet(item: $viewer) { entry in
                PageViewer(domain: domain, stack: [entry])
            }
        }
    }

    private var picker: some View {
        HStack {
            Text("知识库").foregroundColor(Theme.muted).font(.caption)
            Picker("", selection: $domain) {
                ForEach(domains, id: \.self) { Text($0).tag($0) }
            }
            .tint(Theme.accent)
            .onChange(of: domain) { _ in loadPages() }
            Spacer()
            Button { refresh() } label: { Image(systemName: "arrow.clockwise") }
        }
        .padding(.horizontal).padding(.vertical, 8)
        .background(Theme.surface)
    }

    private var list: some View {
        List {
            ForEach(grouped, id: \.0) { category, refs in
                Section {
                    ForEach(refs) { ref in
                        Button { open(ref) } label: {
                            HStack {
                                Image(systemName: icon(category)).foregroundColor(Theme.accent)
                                Text(ref.displayName).foregroundColor(Theme.text)
                                Spacer()
                                Image(systemName: "chevron.right").foregroundColor(Theme.muted).font(.caption)
                            }
                        }
                        .listRowBackground(Theme.surface)
                    }
                } header: {
                    Text(categoryLabel(category)).foregroundColor(Theme.muted)
                }
            }
        }
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
    }

    private func refresh() {
        domains = Store.shared.listWikiDomains()
        if domain.isEmpty { domain = session.selectedDomain ?? domains.first ?? "" }
        loadPages()
    }

    private func loadPages() {
        pages = domain.isEmpty ? [] : Store.shared.listPages(domain)
    }

    private func open(_ ref: WikiPageRef) {
        let content = Store.shared.readPage(domain: domain, rel: ref.relativePath) ?? "（内容为空）"
        viewer = PageViewer.Entry(title: ref.displayName, content: content, isSource: false)
    }

    private func categoryOrder(_ c: String) -> Int {
        ["root": 0, "concepts": 1, "entities": 2, "synthesis": 3, "summaries": 4, "atoms": 9][c] ?? 5
    }
    private func categoryLabel(_ c: String) -> String {
        ["root": "总览", "concepts": "概念", "entities": "实体",
         "synthesis": "综合", "summaries": "摘要", "atoms": "知识原子"][c] ?? c
    }
    private func icon(_ c: String) -> String {
        ["root": "doc.text", "concepts": "lightbulb", "entities": "person.2",
         "synthesis": "sparkles", "summaries": "doc.plaintext", "atoms": "atom"][c] ?? "doc"
    }
}
