import SwiftUI

/// Drives a knowledge-base build and surfaces its live log. WikiManager is
/// created per run; this observable controller forwards its log lines so the
/// view re-renders during the build.
@MainActor
final class BuildController: ObservableObject {
    @Published var logs: [LogLine] = []
    @Published var building = false
    @Published var lastResult: IngestResult?

    func run(domain: String, apiKey: String) async {
        building = true
        logs.removeAll()
        let mgr = WikiManager(domain: domain, apiKey: apiKey)
        mgr.onLog = { [weak self] line in self?.logs.append(line) }
        lastResult = await mgr.ingest()
        building = false
    }
}

/// Knowledge-base build panel — pick a crawled domain, run the two-stage
/// distillation ingest, watch the live log.
struct BuildView: View {
    @EnvironmentObject var session: AppSession
    @StateObject private var controller = BuildController()
    @State private var domains: [String] = []

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.bg.ignoresSafeArea()
                VStack(spacing: 12) {
                    domainPicker
                    if controller.logs.isEmpty && !controller.building {
                        Spacer()
                        Text(domains.isEmpty
                             ? "尚无已采集的网站，请先在「采集」标签抓取内容。"
                             : "请选择一个域开始构建知识库。")
                            .foregroundColor(Theme.muted).multilineTextAlignment(.center).padding()
                        Spacer()
                    } else {
                        logView
                    }
                }
                .padding()
            }
            .navigationTitle("知识库构建")
            .toolbarColorScheme(.dark, for: .navigationBar)
            .onAppear { refresh() }
        }
    }

    private var domainPicker: some View {
        VStack(spacing: 10) {
            HStack {
                Text("已采集域").foregroundColor(Theme.muted).font(.caption)
                Spacer()
                Button { refresh() } label: { Image(systemName: "arrow.clockwise") }
            }
            if domains.isEmpty {
                Text("（无）").foregroundColor(Theme.muted).frame(maxWidth: .infinity, alignment: .leading)
            } else {
                Picker("域", selection: Binding(
                    get: { session.selectedDomain ?? domains.first ?? "" },
                    set: { session.selectedDomain = $0 }
                )) {
                    ForEach(domains, id: \.self) { Text($0).tag($0) }
                }
                .pickerStyle(.menu).tint(Theme.accent)
            }

            Button {
                guard let domain = session.selectedDomain ?? domains.first else { return }
                Task { await controller.run(domain: domain, apiKey: session.apiKey) }
            } label: {
                Text(controller.building ? "构建中…" : "构建 / 更新知识库")
                    .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 10)
                    .background(controller.building ? Theme.panel : Theme.accent)
                    .foregroundColor(controller.building ? Theme.muted : .black)
                    .cornerRadius(10)
            }
            .disabled(domains.isEmpty || controller.building)
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
                    ForEach(controller.logs) { line in
                        Text(line.message)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundColor(Theme.levelColor(line.level))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(line.id)
                    }
                }.padding(10)
            }
            .background(Theme.bg).cornerRadius(12)
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.border))
            .onChange(of: controller.logs.count) { _ in
                if let last = controller.logs.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
        }
    }

    private func refresh() {
        domains = Store.shared.listScrapedDomains()
        if session.selectedDomain == nil { session.selectedDomain = domains.first }
    }
}
