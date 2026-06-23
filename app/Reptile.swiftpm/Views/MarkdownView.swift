import SwiftUI

/// Lightweight markdown renderer with clickable citations — the SwiftUI
/// equivalent of the web app's renderWikiContent():
///   [[页面名]]      → tappable wiki-page link
///   bare *.md name → tappable source-file link
///
/// Citations are encoded as a custom `reptile://` URL scheme and intercepted
/// via OpenURLAction, so taps route back to the host view.
struct MarkdownView: View {
    let text: String
    var onWikiLink: (String) -> Void = { _ in }
    var onSourceLink: (String) -> Void = { _ in }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, raw in
                lineView(raw)
            }
        }
        .environment(\.openURL, OpenURLAction { url in
            guard url.scheme == "reptile" else { return .systemAction }
            let q = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
            if url.host == "wiki", let name = q?.first(where: { $0.name == "name" })?.value {
                onWikiLink(name); return .handled
            }
            if url.host == "source", let file = q?.first(where: { $0.name == "file" })?.value {
                onSourceLink(file); return .handled
            }
            return .handled
        })
    }

    private var lines: [String] { text.components(separatedBy: "\n") }

    @ViewBuilder
    private func lineView(_ raw: String) -> some View {
        let trimmed = raw.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty {
            Color.clear.frame(height: 2)
        } else if let h = heading(trimmed) {
            Text(attributed(h.1))
                .font(.system(size: h.0, weight: .bold))
                .foregroundColor(Theme.text)
        } else if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("• ") {
            HStack(alignment: .top, spacing: 6) {
                Text("•").foregroundColor(Theme.muted)
                Text(attributed(String(trimmed.dropFirst(2))))
                    .foregroundColor(Theme.text)
            }
        } else {
            Text(attributed(trimmed)).foregroundColor(Theme.text)
        }
    }

    private func heading(_ s: String) -> (CGFloat, String)? {
        var n = 0
        for ch in s { if ch == "#" { n += 1 } else { break } }
        guard n > 0, n <= 6, s.count > n, s[s.index(s.startIndex, offsetBy: n)] == " " else { return nil }
        let sizes: [CGFloat] = [22, 19, 17, 16, 15, 14]
        let body = String(s.dropFirst(n + 1))
        return (sizes[min(n - 1, 5)], body)
    }

    /// Linkify citations then parse inline markdown into an AttributedString.
    private func attributed(_ line: String) -> AttributedString {
        var s = linkifySources(line)
        s = linkifyWiki(s)
        let opts = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace)
        if var attr = try? AttributedString(markdown: s, options: opts) {
            // Tint citation links (collect ranges first — don't mutate while iterating).
            let linkRanges = attr.runs.compactMap { $0.link != nil ? $0.range : nil }
            for r in linkRanges { attr[r].foregroundColor = Theme.cite }
            return attr
        }
        return AttributedString(line)
    }

    private func enc(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .urlQueryValueAllowed) ?? s
    }

    private func linkifyWiki(_ s: String) -> String {
        replace(s, pattern: #"\[\[([^\]]+)\]\]"#) { name in
            "[\(name)](reptile://wiki?name=\(enc(name)))"
        }
    }

    private func linkifySources(_ s: String) -> String {
        // Filenames may start uppercase (crawler slugs mirror URL paths verbatim).
        replace(s, pattern: #"([A-Za-z0-9][A-Za-z0-9_\-]{2,79}\.md)"#) { file in
            "[\(file)](reptile://source?file=\(enc(file)))"
        }
    }

    private func replace(_ s: String, pattern: String, _ transform: (String) -> String) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return s }
        let ns = s as NSString
        var result = ""
        var last = 0
        for m in re.matches(in: s, range: NSRange(location: 0, length: ns.length)) {
            result += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let cap = ns.substring(with: m.range(at: 1))
            result += transform(cap)
            last = m.range.location + m.range.length
        }
        result += ns.substring(from: last)
        return result
    }
}

extension CharacterSet {
    static let urlQueryValueAllowed: CharacterSet = {
        var cs = CharacterSet.urlQueryAllowed
        cs.remove(charactersIn: "&=?#")
        return cs
    }()
}
