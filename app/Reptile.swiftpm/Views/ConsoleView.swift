import SwiftUI

/// Crawl console — URL input, download-mode selector, live log. Mirrors the
/// scrape panel from static/index.html.
struct ConsoleView: View {
    @EnvironmentObject var session: AppSession
    @StateObject private var engine = CrawlEngine()

    @State private var url = ""
    @State private var mode: DownloadMode = .all
    @State private var dateFrom = ""        // "YYYY-MM"
    @State private var maxPages = 20

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.bg.ignoresSafeArea()
                VStack(spacing: 12) {
                    controls
                    logView
                }
                .padding()
            }
            .navigationTitle("网页采集")
            .toolbarColorScheme(.dark, for: .navigationBar)
        }
    }

    private var controls: some View {
        VStack(spacing: 10) {
            ReptileField(placeholder: "https://example.com/...", text: $url)

            Picker("下载模式", selection: $mode) {
                ForEach(DownloadMode.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)

            if mode == .byDate {
                ReptileField(placeholder: "发布日期 ≥ (YYYY-MM)", text: $dateFrom)
            }

            Stepper("最多采集 \(maxPages) 页", value: $maxPages, in: 1...200, step: 5)
                .foregroundColor(Theme.text)

            HStack {
                Button {
                    Task { @MainActor in
                        let domain = await engine.crawl(startURL: url.trimmingCharacters(in: .whitespaces),
                                                        mode: mode, dateFromYYYYMM: dateFrom, maxPages: maxPages)
                        session.selectedDomain = domain
                    }
                } label: {
                    Text(engine.running ? "采集中…(\(engine.scrapedCount))" : "开始采集")
                        .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 10)
                        .background(engine.running ? Theme.panel : Theme.accent)
                        .foregroundColor(engine.running ? Theme.muted : .black)
                        .cornerRadius(10)
                }
                .disabled(url.isEmpty || engine.running)

                if engine.running {
                    Button(role: .destructive) { engine.cancel() } label: {
                        Text("停止").padding(.vertical, 10).padding(.horizontal, 16)
                            .background(Theme.panel).cornerRadius(10)
                    }
                }
            }
        }
        .padding(14)
        .background(Theme.surface)
        .cornerRadius(14)
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.border))
    }

    private var logView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
                    if engine.logs.isEmpty {
                        Text("等待采集任务…").foregroundColor(Theme.muted).font(.callout).padding(.top, 40)
                    }
                    ForEach(engine.logs) { line in
                        Text(line.message)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundColor(Theme.levelColor(line.level))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(line.id)
                    }
                }
                .padding(10)
            }
            .background(Theme.bg)
            .cornerRadius(12)
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.border))
            .onChange(of: engine.logs.count) { _ in
                if let last = engine.logs.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
        }
    }
}
