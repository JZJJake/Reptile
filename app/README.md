# Reptile — Apple 原生移植 (iPhone / iPad)

把 Web 版 Reptile（FastAPI + Playwright + Karpathy 多层蒸馏知识库）移植为
**纯原生 SwiftUI 应用**，以 **Swift Playgrounds** 为本地开发验证平台，最终在
iPhone / iPad 上独立运行。Web 版项目（仓库根目录）完全保留、不受影响。

## 为什么是重写而非"搬运"

iOS / iPadOS **不能运行 Python，也不能运行 Playwright / Chromium**（无法 spawn
浏览器进程）。因此：

| 层 | Web 版 | iOS 原生移植 |
|----|--------|-------------|
| 爬虫 | Playwright (Chromium) | **WKWebView**（系统 WebKit）离屏加载 + 注入 JS 抽取 |
| LLM | httpx → DeepSeek | `URLSession`（流式用 `bytes(for:)` 解析 SSE） |
| 蒸馏管线 | `wiki/wiki_manager.py` | `Wiki/WikiManager.swift`（逻辑等价移植） |
| 存储 | `scraped_data/`、`wiki/` 目录 + SQLite | `FileManager` 沙盒目录（同构）+ 哨兵文件 |
| 前端 | 纯 HTML/CSS/JS | SwiftUI |

**混合模型策略**（成本/质量分层，见 `Core/DeepSeekClient.swift`）：

| 环节 | 模型 | 理由 |
|------|------|------|
| 知识库建设（一级蒸馏 + 二级关系组装） | `deepseek-v4-pro` | 蒸馏与跨文档关系推理需要强推理，质量优先 |
| 问答 / 通用对话 | `deepseek-v4-flash` | 交互式、调用量大，速度与成本优先 |

> 改模型只需调整 `DeepSeekClient.buildModel` / `queryModel` 两个常量。

## 如何打开

这是一个 **App Playground (`.swiftpm`)**：

- **iPad**：用 Swift Playgrounds 打开 `app/Reptile.swiftpm`，直接运行。
- **Mac**：用 Swift Playgrounds 或 **Xcode 15+** 打开同一目录。

> `Package.swift` 里的 `import AppleProductTypes` 由 Swift Playgrounds / Xcode
> 工具链提供，普通 Linux 上的 SwiftPM 无法解析——这是预期行为，请在苹果设备上构建。

## 工程结构

```
Reptile.swiftpm/
  Package.swift                App Playground 清单（iOS 16+，iPhone/iPad）
  App/ReptileApp.swift         @main，登录↔主标签页路由
  Core/
    Models.swift               领域模型（ExtractedPage / ChatMessage / ...）
    AppSession.swift           会话：API Key（持久化）/ 登录态 / 当前域
    Store.swift                FileManager 存储层（scraped_data/ + wiki/ 同构）
    DeepSeekClient.swift       DeepSeek 客户端（complete / stream(SSE)）
  Scraper/
    WebScraper.swift           WKWebView 单页抓取 + JS 正文/链接/日期抽取
    CrawlEngine.swift          BFS 爬取编排（单页/全站/按日期 三模式）
  Wiki/
    WikiSchema.swift           Prompt 模板（移植自 wiki/schema.py）
    WikiManager.swift          三级蒸馏：Stage1 原子 → Stage2 关系网 → Stage3 分片 + 查询
  Views/
    Theme.swift                品牌色（GitHub 暗色 + 绿色强调）
    LoginView.swift            API Key 登录（星座/绿叶 wordmark）
    ConsoleView.swift          采集控制台（URL / 模式 / 实时日志）
    BuildView.swift            知识库构建（选择域 → 三级蒸馏 → 日志）
    ChatView.swift             智能问答（流式 + 可点引用）
    WikiBrowserView.swift      知识库浏览（按分类树）
    MarkdownView.swift         轻量 Markdown + [[页面]] / *.md 可点引用渲染
    PageViewer.swift           页面/源文件查看（含 WikiFinder 标题回溯）
```

## 已移植的关键修复（与 Web 版一致）

- **增量 Stage-2 组装**（`.stage2_done`）：重复构建不会重复推导旧原子的关系/事实。
- **FILE_WRITE 路径校验**：拒绝越界写入（`..` 及同名兄弟域前缀）。
- **relations.md 截断**：查询时限制大小，避免挤占页面上下文。
- **引用可点**：`[[页面名]]` 与裸 `*.md`（含大写开头）均可点击回溯原文。
- **确定性 slug**：用 FNV-1a 而非 Swift 进程随机种子 `hashValue`，保证断点续传。

## 端到端流程

1. **登录**：输入 DeepSeek API Key（`/models` 校验，存本设备 UserDefaults）。
2. **采集**：输入 URL，选模式（全站 / 单页 / 按日期），WKWebView 抓取并存为
   `scraped_data/{域}/*.md`。
3. **构建**：选择域，运行三级蒸馏 → `wiki/{域}/`（atoms → relations/concepts/...）。
4. **问答**：基于知识库流式问答，回答中的 `[[页面]]` / `*.md` 可点开回溯。
5. **浏览**：按分类查看全部知识库页面。

## 已知限制 / 后续迭代（milestone 1）

- **ATS**：iOS 默认仅允许 https；纯 http 站点会被拦截（绝大多数政务/新闻站已 https）。
- **抓取串行**：WKWebView 必须在主线程驱动，故逐页抓取（同时也起到限速作用）。
- 文件 IO 目前在主 actor，海量文档构建时可进一步移出主线程。
- API Key 现存 UserDefaults，后续可迁移到 Keychain。
- 页面选择采用关键词打分（移植了评分逻辑）；可再加一步 LLM 选页以对齐 Web 版。

> 本目录为云端 Linux 环境生成，未在苹果设备编译。请在 Swift Playgrounds / Xcode
> 中构建运行并迭代；如遇编译细节问题，多为 SDK 版本差异，按提示微调即可。
