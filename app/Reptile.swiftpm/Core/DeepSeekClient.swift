import Foundation

/// DeepSeek API client (OpenAI-compatible), the Swift/URLSession equivalent of
/// wiki/deepseek_client.py. Uses `deepseek-reasoner` (Pro / "thinking") tier
/// everywhere, matching the web project.
struct DeepSeekClient {
    static let baseURL = "https://api.deepseek.com/v1"
    static let defaultModel = "deepseek-reasoner"
    static let timeout: TimeInterval = 180
    static let assemblyTimeout: TimeInterval = 600

    var apiKey: String

    private func request(path: String, body: [String: Any], timeout: TimeInterval) throws -> URLRequest {
        guard !apiKey.isEmpty else { throw ReptileError.noAPIKey }
        guard let url = URL(string: "\(Self.baseURL)\(path)") else { throw ReptileError.http("bad url") }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = timeout
        return req
    }

    /// Validate the key via /models (no token cost). Mirrors validate_api_key.
    func validateKey() async -> Bool {
        guard !apiKey.isEmpty, let url = URL(string: "\(Self.baseURL)/models") else { return false }
        var req = URLRequest(url: url)
        req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        req.timeoutInterval = 15
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch { return false }
    }

    /// Non-streaming completion → full content string.
    func complete(messages: [[String: String]],
                  model: String = DeepSeekClient.defaultModel,
                  temperature: Double = 0.7,
                  timeout: TimeInterval = DeepSeekClient.timeout) async throws -> String {
        let body: [String: Any] = [
            "model": model, "messages": messages, "stream": false, "temperature": temperature,
        ]
        let req = try request(path: "/chat/completions", body: body, timeout: timeout)
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw ReptileError.http(String(data: data, encoding: .utf8) ?? "HTTP error")
        }
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let choices = json?["choices"] as? [[String: Any]]
        let msg = choices?.first?["message"] as? [String: Any]
        return msg?["content"] as? String ?? ""
    }

    /// Streaming completion → async sequence of text deltas (SSE parsing).
    func stream(messages: [[String: String]],
                model: String = DeepSeekClient.defaultModel,
                temperature: Double = 0.7) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let body: [String: Any] = [
                        "model": model, "messages": messages, "stream": true, "temperature": temperature,
                    ]
                    let req = try request(path: "/chat/completions", body: body, timeout: Self.timeout)
                    let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                    guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
                        throw ReptileError.http("HTTP \((resp as? HTTPURLResponse)?.statusCode ?? -1)")
                    }
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data: ") else { continue }
                        let payload = String(line.dropFirst(6))
                        if payload == "[DONE]" { break }
                        guard let d = payload.data(using: .utf8),
                              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any]
                        else { continue }
                        if let err = j["error"] as? [String: Any] {
                            let m = err["message"] as? String ?? "stream error"
                            throw ReptileError.http(m)
                        }
                        let choices = j["choices"] as? [[String: Any]]
                        let delta = choices?.first?["delta"] as? [String: Any]
                        if let content = delta?["content"] as? String, !content.isEmpty {
                            continuation.yield(content)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
