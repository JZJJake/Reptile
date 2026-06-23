import SwiftUI

/// Wiki Q&A — streamed DeepSeek answers with tappable citations. Mirrors the
/// chat pane in static/index.html, including multi-turn history.
struct ChatView: View {
    @EnvironmentObject var session: AppSession
    @State private var domains: [String] = []
    @State private var messages: [ChatMessage] = []
    @State private var input = ""
    @State private var busy = false
    @State private var viewer: PageViewer.Entry?

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.bg.ignoresSafeArea()
                VStack(spacing: 0) {
                    domainBar
                    messagesView
                    inputBar
                }
            }
            .navigationTitle("智能问答")
            .toolbarColorScheme(.dark, for: .navigationBar)
            .onAppear { domains = Store.shared.listWikiDomains() }
            .sheet(item: $viewer) { entry in
                PageViewer(domain: session.selectedDomain ?? "", stack: [entry])
            }
        }
    }

    private var domainBar: some View {
        HStack {
            Text("知识库").foregroundColor(Theme.muted).font(.caption)
            Picker("", selection: Binding(
                get: { session.selectedDomain ?? "" },
                set: { session.selectedDomain = $0.isEmpty ? nil : $0 }
            )) {
                Text("通用对话").tag("")
                ForEach(domains, id: \.self) { Text($0).tag($0) }
            }
            .tint(Theme.accent)
            Spacer()
            Button { messages.removeAll() } label: { Image(systemName: "trash") }
                .disabled(messages.isEmpty)
        }
        .padding(.horizontal).padding(.vertical, 8)
        .background(Theme.surface)
    }

    private var messagesView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if messages.isEmpty {
                        Text(session.selectedDomain == nil
                             ? "通用对话模式。选择知识库后可基于采集内容问答。"
                             : "向「\(session.selectedDomain!)」知识库提问。")
                            .foregroundColor(Theme.muted).font(.callout).padding(.top, 40)
                    }
                    ForEach(messages) { msg in bubble(msg).id(msg.id) }
                }
                .padding()
            }
            .onChange(of: messages.last?.content) { _ in
                if let last = messages.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
        }
    }

    @ViewBuilder
    private func bubble(_ msg: ChatMessage) -> some View {
        HStack {
            if msg.role == .user { Spacer(minLength: 40) }
            VStack(alignment: .leading) {
                if msg.role == .assistant {
                    MarkdownView(text: msg.content,
                                 onWikiLink: openWiki,
                                 onSourceLink: openSource)
                } else {
                    Text(msg.content).foregroundColor(Theme.text)
                }
            }
            .padding(10)
            .background(msg.role == .user ? Theme.blue.opacity(0.25) : Theme.panel)
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.border))
            .cornerRadius(12)
            if msg.role == .assistant { Spacer(minLength: 40) }
        }
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            TextField("输入问题…", text: $input, axis: .vertical)
                .lineLimit(1...4)
                .padding(10).background(Theme.panel).foregroundColor(Theme.text)
                .cornerRadius(10)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.border))
            Button { Task { await send() } } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 30))
                    .foregroundColor(busy || input.isEmpty ? Theme.muted : Theme.accent)
            }
            .disabled(busy || input.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        .padding().background(Theme.surface)
    }

    @MainActor private func send() async {
        let q = input.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        input = ""
        busy = true
        defer { busy = false }

        messages.append(ChatMessage(role: .user, content: q))
        let history = messages.dropLast().map { ["role": $0.role.rawValue, "content": $0.content] }
        var assistant = ChatMessage(role: .assistant, content: "")
        messages.append(assistant)
        let idx = messages.count - 1

        do {
            let stream: AsyncThrowingStream<String, Error>
            if let domain = session.selectedDomain {
                let mgr = WikiManager(domain: domain, apiKey: session.apiKey)
                // query() is now async: Step 1 (LLM page-select, v4-flash) runs
                // before the stream starts; Step 2 streams the answer.
                stream = await mgr.query(question: q, history: Array(history))
            } else {
                var msgs: [[String: String]] = [["role": "system", "content": WikiSchema.generalChatSystem]]
                msgs += history
                msgs.append(["role": "user", "content": q])
                stream = session.client.stream(messages: msgs)
            }
            for try await delta in stream {
                assistant.content += delta
                messages[idx] = assistant
            }
        } catch {
            assistant.content += "\n\n【查询出错：\(error.localizedDescription)】"
            messages[idx] = assistant
        }
    }

    private func openWiki(_ name: String) {
        guard let domain = session.selectedDomain else { return }
        let content = WikiFinder.find(domain: domain, name: name) ?? "未找到知识库页面：[[\(name)]]"
        viewer = PageViewer.Entry(title: name, content: content, isSource: false)
    }

    private func openSource(_ file: String) {
        guard let domain = session.selectedDomain else { return }
        let url = Store.shared.scrapedDir(domain).appendingPathComponent(file)
        let content = (try? String(contentsOf: url, encoding: .utf8)) ?? "未找到源文件：\(file)"
        viewer = PageViewer.Entry(title: file, content: content, isSource: true)
    }
}
