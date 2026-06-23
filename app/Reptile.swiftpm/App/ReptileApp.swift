import SwiftUI

@main
struct ReptileApp: App {
    @StateObject private var session = AppSession()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(session)
                .preferredColorScheme(.dark)
                .tint(Theme.accent)
        }
    }
}

/// Switches between login and the main tabbed workspace.
struct RootView: View {
    @EnvironmentObject var session: AppSession

    var body: some View {
        if session.isAuthenticated {
            MainTabView()
        } else {
            LoginView()
        }
    }
}

struct MainTabView: View {
    var body: some View {
        TabView {
            ConsoleView()
                .tabItem { Label("采集", systemImage: "antenna.radiowaves.left.and.right") }
            BuildView()
                .tabItem { Label("知识库", systemImage: "books.vertical") }
            ChatView()
                .tabItem { Label("问答", systemImage: "bubble.left.and.bubble.right") }
            WikiBrowserView()
                .tabItem { Label("浏览", systemImage: "doc.text.magnifyingglass") }
        }
        .tint(Theme.accent)
    }
}
