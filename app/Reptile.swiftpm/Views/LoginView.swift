import SwiftUI

struct LoginView: View {
    @EnvironmentObject var session: AppSession
    @State private var error: String?

    var body: some View {
        ZStack {
            Theme.bg.ignoresSafeArea()
            VStack(spacing: 22) {
                Spacer()
                // Wordmark
                VStack(spacing: 10) {
                    Image(systemName: "leaf.fill")
                        .font(.system(size: 52))
                        .foregroundColor(Theme.accent)
                        .shadow(color: Theme.accent.opacity(0.5), radius: 18)
                    Text("Reptile")
                        .font(.system(size: 34, weight: .bold, design: .rounded))
                        .foregroundColor(Theme.text)
                    Text("自织知识星座 · 智能知识库")
                        .font(.subheadline)
                        .foregroundColor(Theme.muted)
                }

                VStack(spacing: 12) {
                    ReptileField(placeholder: "DeepSeek API Key", text: $session.apiKey, secure: true)
                    if let error {
                        Text(error).font(.caption).foregroundColor(Theme.levelColor(.error))
                    }
                    Button {
                        Task { await doLogin() }
                    } label: {
                        HStack {
                            if session.validating { ProgressView().tint(.black) }
                            Text(session.validating ? "验证中…" : "进入")
                                .fontWeight(.semibold)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                        .background(Theme.accent)
                        .foregroundColor(.black)
                        .cornerRadius(10)
                    }
                    .disabled(session.apiKey.isEmpty || session.validating)
                }
                .padding(20)
                .background(Theme.surface)
                .cornerRadius(16)
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(Theme.border))
                .padding(.horizontal, 28)

                Text("密钥仅保存在本设备，用于直接调用 DeepSeek 接口。")
                    .font(.caption2).foregroundColor(Theme.muted)
                Spacer()
            }
        }
    }

    @MainActor private func doLogin() async {
        error = nil
        let ok = await session.login()
        if !ok { error = "API Key 无效或网络不可用，请检查后重试。" }
    }
}
