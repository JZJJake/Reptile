import SwiftUI
import UniformTypeIdentifiers

// MARK: - Controller

@MainActor
final class BuildController: ObservableObject {
    @Published var logs: [LogLine] = []
    @Published var building = false
    @Published var lastResult: IngestResult?

    func run(domain: String, apiKey: String) async {
        start()
        let mgr = make(domain: domain, apiKey: apiKey)
        lastResult = await mgr.ingest()
        building = false
    }

    func importAndBuild(files: [URL], domain: String, apiKey: String) async {
        start()
        let mgr = make(domain: domain, apiKey: apiKey)
        lastResult = await mgr.ingestFromFiles(files)
        building = false
    }

    func rebuild(domain: String, apiKey: String, level: RebuildLevel) async {
        start()
        let mgr = make(domain: domain, apiKey: apiKey)
        switch level {
        case .stage2Only: lastResult = await mgr.forceRebuildStage2()
        case .full:       lastResult = await mgr.forceRebuildFull()
        }
        building = false
    }

    private func start() {
        building = true
        logs.removeAll()
        lastResult = nil
    }

    private func make(domain: String, apiKey: String) -> WikiManager {
        let mgr = WikiManager(domain: domain, apiKey: apiKey)
        mgr.onLog = { [weak self] line in self?.logs.append(line) }
        return mgr
    }
}

// MARK: - View

struct BuildView: View {
    @EnvironmentObject var session: AppSession
    @StateObject private var controller = BuildController()

    @State private var buildMode: BuildMode = .normal
    @State private var scrapedDomains: [String] = []
    @State private var wikiDomains: [String] = []

    // 导入模式
    @State private var importDomain = ""
    @State private var selectedFiles: [URL] = []
    @State private var showFilePicker = false

    // 重建模式
    @State private var rebuildDomain = ""
    @State private var rebuildLevel: RebuildLevel = .stage2Only
    @State private var showRebuildConfirm = false

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.bg.ignoresSafeArea()
                VStack(spacing: 12) {
                    modePicker
                    switch buildMode {
                    case .normal:      normalPanel
                    case .importFiles: importPanel
                    case .rebuild:     rebuildPanel
                    }
                    if !controller.logs.isEmpty || controller.building {
                        logView
                    }
                }
                .padding()
            }
            .navigationTitle("知识库构建")
            .toolbarColorScheme(.dark, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { refreshDomains() } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
            .onAppear { refreshDomains() }
            .fileImporter(
                isPresented: $showFilePicker,
                allowedContentTypes: [.plainText],
                allowsMultipleSelection: true
            ) { result in
                if case .success(let urls) = result {
                    selectedFiles = urls.filter { $0.pathExtension.lowercased() == "md" }
                }
            }
            .confirmationDialog(
                rebuildLevel == .full ? "确认全量重建" : "确认重建 Stage-2",
                isPresented: $showRebuildConfirm,
                titleVisibility: .visible
            ) {
                Button(rebuildLevel == .full ? "全量重建（不可撤销）" : "重建 Stage-2",
                       role: .destructive) {
                    guard !rebuildDomain.isEmpty else { return }
                    Task { await controller.rebuild(domain: rebuildDomain,
                                                    apiKey: session.apiKey,
                                                    level: rebuildLevel) }
                }
                Button("取消", role: .cancel) { }
            } message: {
                Text(rebuildLevel.warning)
            }
        }
    }

    // MARK: - Mode picker

    private var modePicker: some View {
        Picker("模式", selection: $buildMode) {
            ForEach(BuildMode.allCases) { Text($0.rawValue).tag($0) }
        }
        .pickerStyle(.segmented)
        .onChange(of: buildMode) { _ in controller.logs.removeAll() }
    }

    // MARK: - Normal build panel

    private var normalPanel: some View {
        VStack(spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("已采集域").foregroundColor(Theme.muted).font(.caption)
                    Text("模型：\(DeepSeekClient.buildModel) → \(DeepSeekClient.queryModel)")
                        .foregroundColor(Theme.accent).font(.caption2)
                }
                Spacer()
            }

            if scrapedDomains.isEmpty {
                Text("尚无已采集的网站，请先在「采集」标签抓取内容。")
                    .foregroundColor(Theme.muted).font(.callout)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                domainPicker(domains: scrapedDomains,
                             selection: Binding(
                                get: { session.selectedDomain ?? scrapedDomains.first ?? "" },
                                set: { session.selectedDomain = $0 }))
                if let d = session.selectedDomain ?? scrapedDomains.first {
                    domainStats(domain: d)
                }
            }

            buildButton(
                label: controller.building ? "构建中…" : "构建 / 更新知识库",
                disabled: scrapedDomains.isEmpty || controller.building
            ) {
                guard let d = session.selectedDomain ?? scrapedDomains.first else { return }
                Task { await controller.run(domain: d, apiKey: session.apiKey) }
            }
        }
        .cardStyle()
    }

    // MARK: - Import files panel

    private var importPanel: some View {
        VStack(spacing: 10) {
            HStack {
                Text("将本地 .md 文件直接导入知识库，无需重新爬取")
                    .foregroundColor(Theme.muted).font(.caption)
                Spacer()
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("目标域名").foregroundColor(Theme.muted).font(.caption)
                HStack {
                    ReptileField(placeholder: "输入新域名或选择已有域", text: $importDomain)
                    if !wikiDomains.isEmpty {
                        Menu {
                            ForEach(wikiDomains + scrapedDomains, id: \.self) { d in
                                Button(d) { importDomain = d }
                            }
                        } label: {
                            Image(systemName: "chevron.down.circle")
                                .foregroundColor(Theme.accent)
                        }
                    }
                }
            }

            Button {
                showFilePicker = true
            } label: {
                HStack {
                    Image(systemName: "folder.badge.plus")
                    Text(selectedFiles.isEmpty
                         ? "选择 .md 文件…"
                         : "已选 \(selectedFiles.count) 个文件")
                    Spacer()
                    if !selectedFiles.isEmpty {
                        Button { selectedFiles.removeAll() } label: {
                            Image(systemName: "xmark.circle.fill").foregroundColor(Theme.muted)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(10)
                .background(Theme.panel)
                .cornerRadius(10)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.border))
                .foregroundColor(selectedFiles.isEmpty ? Theme.muted : Theme.text)
            }

            if !selectedFiles.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(selectedFiles.prefix(5), id: \.lastPathComponent) { f in
                        Text("• \(f.lastPathComponent)")
                            .font(.caption2).foregroundColor(Theme.muted)
                    }
                    if selectedFiles.count > 5 {
                        Text("…以及另外 \(selectedFiles.count - 5) 个文件")
                            .font(.caption2).foregroundColor(Theme.muted)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            buildButton(
                label: controller.building ? "导入建库中…" : "导入并建立知识库",
                disabled: selectedFiles.isEmpty || importDomain.trimmingCharacters(in: .whitespaces).isEmpty || controller.building
            ) {
                let d = importDomain.trimmingCharacters(in: .whitespaces)
                guard !d.isEmpty, !selectedFiles.isEmpty else { return }
                Task {
                    await controller.importAndBuild(files: selectedFiles, domain: d,
                                                     apiKey: session.apiKey)
                    refreshDomains()
                }
            }
        }
        .cardStyle()
    }

    // MARK: - Rebuild panel

    private var rebuildPanel: some View {
        VStack(spacing: 10) {
            HStack {
                Text("当知识库架构版本升级时，用现有文件重建，无需重新爬取")
                    .foregroundColor(Theme.muted).font(.caption)
                Spacer()
            }

            if wikiDomains.isEmpty {
                Text("尚无已构建的知识库。").foregroundColor(Theme.muted).font(.callout)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Text("目标知识库").foregroundColor(Theme.muted).font(.caption)
                    domainPicker(domains: wikiDomains,
                                 selection: Binding(get: { rebuildDomain.isEmpty ? (wikiDomains.first ?? "") : rebuildDomain },
                                                    set: { rebuildDomain = $0 }))
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text("重建范围").foregroundColor(Theme.muted).font(.caption)
                    Picker("重建范围", selection: $rebuildLevel) {
                        ForEach(RebuildLevel.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.segmented)
                }

                // Warning card
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: rebuildLevel == .full ? "exclamationmark.triangle.fill" : "info.circle.fill")
                        .foregroundColor(rebuildLevel == .full ? .orange : Theme.accent)
                    Text(rebuildLevel.warning)
                        .font(.caption).foregroundColor(Theme.text)
                }
                .padding(10)
                .background(Theme.panel)
                .cornerRadius(10)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.border))

                // Stats for selected domain
                let d = rebuildDomain.isEmpty ? (wikiDomains.first ?? "") : rebuildDomain
                if !d.isEmpty { rebuildStats(domain: d) }
            }

            buildButton(
                label: controller.building ? "重建中…" : "开始重建",
                disabled: wikiDomains.isEmpty || controller.building,
                destructive: true
            ) {
                let d = rebuildDomain.isEmpty ? (wikiDomains.first ?? "") : rebuildDomain
                guard !d.isEmpty else { return }
                rebuildDomain = d
                showRebuildConfirm = true
            }
        }
        .cardStyle()
    }

    // MARK: - Shared sub-views

    private func domainPicker(domains: [String], selection: Binding<String>) -> some View {
        Picker("域", selection: selection) {
            ForEach(domains, id: \.self) { Text($0).tag($0) }
        }
        .pickerStyle(.menu)
        .tint(Theme.accent)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func domainStats(domain: String) -> some View {
        let scraped  = Store.shared.scrapedFileCount(domain)
        let atoms    = Store.shared.sentinelCount(domain: domain, name: ".stage1_done")
        let assembled = Store.shared.sentinelCount(domain: domain, name: ".stage2_done")
        return HStack(spacing: 16) {
            stat("原始文档", "\(scraped)")
            stat("知识原子", "\(atoms)")
            stat("已组装", "\(assembled)")
        }
        .padding(.vertical, 4)
    }

    private func rebuildStats(domain: String) -> some View {
        let atoms     = Store.shared.sentinelCount(domain: domain, name: ".stage1_done")
        let pages     = Store.shared.listPages(domain).filter { !$0.relativePath.hasPrefix("atoms/") && $0.relativePath != "log.md" }.count
        let scraped   = Store.shared.scrapedFileCount(domain)
        return HStack(spacing: 16) {
            stat("原始文档", "\(scraped)")
            stat("已蒸馏原子", "\(atoms)")
            stat("策展页面", "\(pages)")
        }
        .padding(.vertical, 4)
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(spacing: 2) {
            Text(value).fontWeight(.semibold).foregroundColor(Theme.accent)
            Text(label).font(.caption2).foregroundColor(Theme.muted)
        }
        .frame(maxWidth: .infinity)
    }

    private func buildButton(label: String, disabled: Bool,
                              destructive: Bool = false,
                              action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .fontWeight(.semibold)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .background(disabled ? Theme.panel : (destructive ? Color.orange : Theme.accent))
                .foregroundColor(disabled ? Theme.muted : .black)
                .cornerRadius(10)
        }
        .disabled(disabled)
    }

    private var logView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
                    ForEach(controller.logs) { line in
                        Text(line.message)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundColor(Theme.levelColor(line.level))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(line.id)
                    }
                }.padding(10)
            }
            .background(Theme.bg)
            .cornerRadius(12)
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.border))
            .frame(minHeight: 120)
            .onChange(of: controller.logs.count) { _ in
                if let last = controller.logs.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    // MARK: - Helpers

    private func refreshDomains() {
        scrapedDomains = Store.shared.listScrapedDomains()
        wikiDomains    = Store.shared.listWikiDomains()
        if session.selectedDomain == nil { session.selectedDomain = scrapedDomains.first }
        if rebuildDomain.isEmpty { rebuildDomain = wikiDomains.first ?? "" }
    }
}

// MARK: - Card style modifier

private extension View {
    func cardStyle() -> some View {
        self
            .padding(14)
            .background(Theme.surface)
            .cornerRadius(14)
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.border))
    }
}
