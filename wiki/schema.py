"""Wiki schema: prompts that govern all LLM wiki operations.

Karpathy LLM-Wiki approach: an LLM acts as a librarian, maintaining a
compounding, structured, human-readable wiki from raw source documents.
Not RAG — the LLM curates and merges knowledge, not retrieves raw chunks.

NOTE: All templates are used with str.format(). Literal braces inside
prompt text MUST be doubled: {{ and }}. Only the named fields listed in
each template's docstring may appear as single {name} placeholders.
"""

DEFAULT_SCHEMA = """你是一个知识库（Wiki）管理员。你的职责是把零散的原始网页文档，提炼并整合成一个结构化、可检索、不断累积的知识库。这不是复制粘贴，而是知识的萃取与组织。

## 知识库目录结构
- `index.md`：总目录。按主题分类列出所有页面，每条带一句话摘要和 [[链接]]。
- `log.md`：只追加的操作日志。每条格式：`[YYYY-MM-DD HH:MM UTC] {{operation}}: {{detail}}`
- `summaries/<英文短横线名>.md`：每篇源文档的精炼摘要（提炼要点，非照抄全文）。
- `concepts/<英文短横线名>.md`：核心概念、定义、事实、关系，可跨多文档汇总同一概念。
- `entities/<英文短横线名>.md`：人物、机构、产品、项目等命名实体。
- `synthesis/<英文短横线名>.md`：跨文档的洞察、对比、结论。

## 规则
- 内部链接用 `[[页面名]]`（不带路径前缀）。
- 文件名：小写、仅用短横线、语义化（如 `transformer-architecture.md`）。
- 每个页面以 `# 标题` 开头；用中文撰写，英文术语保留原文。
- 每个页面底部加 `## 来源` 节，列出来源文件名或 URL。
- **增量更新**：若页面已存在（见提供的现有内容），输出其完整新版本来补充/修订事实，绝不丢失已有知识。
- 优先沉淀能"复利累积"的知识：定义、事实、因果、对比、结论——而非流水账。

## 输出格式（严格遵守）
写入或更新页面时，用 FILE_WRITE 块，每块包含一个页面的完整内容：
<<<FILE: summaries/example.md>>>
# 标题
（页面完整内容）
<<<END>>>

每次操作后都要更新 `index.md` 并向 `log.md` 追加一条记录。除 FILE_WRITE 块外不输出多余解释。"""


# ── Ingest prompts ─────────────────────────────────────────────────────────────

# Format fields: {count}, {index_content}, {doc_titles}
PLAN_PROMPT_TEMPLATE = """你将整合 {count} 篇新文档进知识库。

请先分析这些文档的标题，规划哪些知识库页面需要新建、哪些已有页面需要更新。
仅输出 JSON，格式如下（不要其他文字）：
{{"new": ["summaries/topic-a.md", "concepts/key-concept.md"], "update": ["concepts/existing.md"]}}

规则：
- "new"：需要新建的页面路径（summaries/、concepts/、entities/、synthesis/ 下）
- "update"：已存在且需要补充/修订的内容页路径（根据下方 index.md 判断哪些已存在）
- index.md 和 log.md 每次操作都会自动更新，不需要列在 update 里
- 路径格式举例：summaries/deepseek-intro.md，concepts/transformer.md

【当前 index.md（据此判断已存在哪些页面）】
{index_content}

【待整合文档列表（标题速览）】
{doc_titles}

输出 JSON："""


# Format fields: {count}, {index_content}, {documents}
# Used when there are no existing pages to merge (first ingest or plan failed).
INGEST_PROMPT_TEMPLATE = """以下是 {count} 篇待整合进知识库的原始文档。

请逐篇处理，把知识沉淀到对应页面：
1. 为每篇文档写摘要页 `summaries/<主题名>.md`（提炼要点，不要照抄全文）。
2. 抽取重要概念和实体，写 `concepts/` 与 `entities/` 下的页面；跨文档涉及同一概念时合并进同一页。
3. 更新 `index.md`，登记所有新增页面。
4. 向 `log.md` 追加本次操作记录。

目标：每批产出 5-15 个页面。聚焦可累积的知识（定义、事实、关系、洞察）。

【当前 index.md】
{index_content}

---原始文档---
{documents}
---原始文档结束---

输出 FILE_WRITE 块："""


# Format fields: {count}, {index_content}, {existing_pages}, {documents}
# Used when there are existing pages to merge — the model sees their full content.
WRITE_WITH_CONTEXT_PROMPT_TEMPLATE = """整合以下 {count} 篇新文档，写入或更新知识库页面。

**重要**：下方列出了本次操作涉及的已有页面全文。对于这些页面，输出时必须保留所有已有知识，
将新内容融合进去——不得丢失任何原有事实或观点。对于新建页面，直接撰写完整内容。
每次操作后更新 index.md 并向 log.md 追加记录。

【当前 index.md】
{index_content}

【需更新的已有页面（必须保留现有内容并融合新知识）】
{existing_pages}

---新源文档---
{documents}
---新源文档结束---

输出 FILE_WRITE 块："""


# ── Query prompts ──────────────────────────────────────────────────────────────

# Format fields: {question}, {index_content}
PAGE_SELECT_PROMPT_TEMPLATE = """根据知识库目录，列出最适合回答该问题的页面路径（最多 6 个）。
仅输出页面路径，每行一个，不要任何解释或序号。

问题：{question}

知识库目录：
{index_content}

相关页面路径："""


# Format fields: {index_content}, {pages_content}, {question}
QUERY_PROMPT_TEMPLATE = """你是这个知识库的专属问答助手。请仅依据下方提供的知识库页面内容来回答。

回答要求：
- 只使用下方知识库内容作答；不使用自己的常识补充或编造。
- 用 [[页面名]] 标注引用来源页面。
- 知识库中若找不到答案，明确说明"知识库中未找到相关信息"，并指出可能需要补充采集哪类内容。
- 回答具体、聚焦、有条理，直接针对问题，不泛泛而谈。

【知识库目录】
{index_content}

【相关知识库页面】
{pages_content}

【问题】
{question}

回答："""


# ── Lint prompt ────────────────────────────────────────────────────────────────

# Format fields: {pages_content}
LINT_PROMPT_TEMPLATE = """检查下面所有知识库页面的质量问题，对需要修改的页面输出修订版：
- 修正页面之间的矛盾
- 标注过时或不确定的论断（加 [待核实]）
- 为相关页面补充缺失的 [[交叉链接]]
- 确保孤立页面已登记在 index.md 中

【当前所有知识库页面】
{pages_content}

对每个需要更新的页面输出一个 FILE_WRITE 块（完整新内容）；无需改动的页面跳过。"""


# ── General chat system prompt (no wiki selected) ──────────────────────────────
GENERAL_CHAT_SYSTEM = """你是 Reptile 智能知识库系统的专属 AI 助手，由 DeepSeek 驱动。该系统用于：采集网页资料、构建结构化知识库、基于知识库进行专业问答。

当前处于通用对话模式（用户尚未选择具体知识库）：
- 针对问题给出准确、具体、有条理的专业回答。
- 若问题涉及某个已采集网站或领域，提醒用户选择对应知识库以获得基于其资料的精准回答。
- 回答聚焦直接，不空泛客套。用中文回答，英文术语保留原文。"""
