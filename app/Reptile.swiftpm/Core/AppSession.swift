import SwiftUI

/// App-wide session state: API key (persisted), auth status, selected domain.
@MainActor
final class AppSession: ObservableObject {
    @Published var apiKey: String {
        didSet { UserDefaults.standard.set(apiKey, forKey: Self.keyDefaults) }
    }
    @Published var isAuthenticated = false
    @Published var validating = false
    @Published var selectedDomain: String?

    private static let keyDefaults = "deepseek_api_key"

    init() {
        apiKey = UserDefaults.standard.string(forKey: Self.keyDefaults) ?? ""
    }

    var client: DeepSeekClient { DeepSeekClient(apiKey: apiKey) }

    func login() async -> Bool {
        guard !apiKey.isEmpty else { return false }
        validating = true
        defer { validating = false }
        let ok = await client.validateKey()
        isAuthenticated = ok
        return ok
    }

    func logout() {
        isAuthenticated = false
    }
}
