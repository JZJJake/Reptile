import Foundation
import WebKit

/// Native crawler primitive — the WKWebView equivalent of a Playwright page.
/// Loads a URL offscreen, lets JavaScript render, then injects an extraction
/// script that returns {title, text, html, date, links}. This is how the iOS
/// port replaces Playwright/Chromium, which cannot run on-device.
@MainActor
final class WebScraper: NSObject, WKNavigationDelegate {

    private var webView: WKWebView?
    private var loadContinuation: CheckedContinuation<Void, Error>?

    /// Fetch + extract one page. `settle` adds a small delay after `didFinish`
    /// so client-rendered (SPA) content has a chance to paint before extraction.
    func extract(urlString: String, timeout: TimeInterval = 30,
                 settle: TimeInterval = 0.8) async throws -> ExtractedPage {
        guard let url = URL(string: urlString) else { throw ReptileError.extractionFailed }

        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        let wv = WKWebView(frame: CGRect(x: 0, y: 0, width: 1024, height: 1366),
                           configuration: config)
        wv.navigationDelegate = self
        webView = wv

        wv.load(URLRequest(url: url, cachePolicy: .useProtocolCachePolicy, timeoutInterval: timeout))

        // Wait for navigation to finish, with a hard timeout that resumes the
        // same continuation (resumeLoad guards against a double-resume).
        try await waitForLoad(timeout: timeout)

        if settle > 0 {
            try? await Task.sleep(nanoseconds: UInt64(settle * 1_000_000_000))
        }

        let raw = try await wv.evaluateJavaScript(Self.extractionJS)
        webView = nil

        guard let dict = raw as? [String: Any] else { throw ReptileError.extractionFailed }
        let host = url.host ?? ""
        let links = (dict["links"] as? [String] ?? []).filter { link in
            URL(string: link)?.host.map { $0 == host || $0 == "www.\(host)" || "www.\($0)" == host } ?? false
        }
        return ExtractedPage(
            url: urlString,
            title: (dict["title"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
            text: (dict["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
            html: dict["html"] as? String ?? "",
            publishDate: (dict["date"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
            links: Array(Set(links)).sorted()
        )
    }

    private func waitForLoad(timeout: TimeInterval) async throws {
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            loadContinuation = c
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                self?.resumeLoad(.failure(ReptileError.timeout))
            }
        }
    }

    private func resumeLoad(_ result: Result<Void, Error>) {
        guard let c = loadContinuation else { return }
        loadContinuation = nil
        switch result {
        case .success: c.resume()
        case .failure(let e): c.resume(throwing: e)
        }
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        resumeLoad(.success(()))
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        resumeLoad(.failure(error))
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        resumeLoad(.failure(error))
    }

    // MARK: Extraction JS

    /// Runs in the page's JS context. Heuristically picks the main content
    /// container (article/main/role=main/largest text block), strips
    /// nav/header/footer/aside/script/style, and returns text + links + date.
    /// This mirrors what trafilatura/readability do on the server side.
    static let extractionJS = #"""
    (function () {
      function clean(root) {
        var clone = root.cloneNode(true);
        var junk = clone.querySelectorAll('script,style,noscript,nav,header,footer,aside,form,iframe,svg,.nav,.menu,.sidebar,.footer,.header,.ad,.advertisement');
        for (var i = 0; i < junk.length; i++) { junk[i].remove(); }
        return clone;
      }
      function textOf(el) {
        return (el.innerText || el.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
      }
      // Pick main container.
      var candidates = [];
      var sels = ['article', 'main', '[role=main]', '#content', '.content', '.article', '.post', '.main-content'];
      for (var s = 0; s < sels.length; s++) {
        var found = document.querySelectorAll(sels[s]);
        for (var k = 0; k < found.length; k++) candidates.push(found[k]);
      }
      if (candidates.length === 0) candidates.push(document.body);
      var best = candidates[0], bestLen = 0;
      for (var c = 0; c < candidates.length; c++) {
        var len = textOf(candidates[c]).length;
        if (len > bestLen) { bestLen = len; best = candidates[c]; }
      }
      var cleaned = clean(best);
      var text = textOf(cleaned);

      // Title.
      var title = '';
      var h1 = document.querySelector('h1');
      if (h1) title = (h1.innerText || '').trim();
      if (!title) title = (document.title || '').trim();

      // Best-effort publish date.
      var date = '';
      var t = document.querySelector('time[datetime]');
      if (t) date = t.getAttribute('datetime') || t.innerText || '';
      if (!date) {
        var m = document.body.innerText.match(/(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})/);
        if (m) date = m[0];
      }

      // Same-doc links (absolute).
      var links = [];
      var as = document.querySelectorAll('a[href]');
      for (var a = 0; a < as.length; a++) {
        var href = as[a].href;
        if (href && href.indexOf('http') === 0 && href.indexOf('#') === -1) links.push(href);
      }

      return {
        title: title,
        text: text,
        date: date,
        html: best.outerHTML.slice(0, 200000),
        links: links
      };
    })();
    """#
}
