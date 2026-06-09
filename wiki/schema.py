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

# ── Stage 1: single-doc distillation → knowledge atom ──────────────────────────
# Format fields: {filename}, {body}
# One source document in → one compact, structured "knowledge atom" out.
# The atom is the high-density unit that Stage 2 assembles into a relation network.
DISTILL_ATOM_PROMPT_TEMPLATE = """把下面这一篇文档蒸馏成一个高密度的「知识原子」。

这是知识库构建的第一级蒸馏：你要用最精炼的结构化形式，保留这篇文档的全部关键知识，
丢弃冗余表述、客套、导航性文字。目标是让后续环节能在有限上下文里"看见"尽可能多的文档。

严格按以下格式输出，整体控制在 300 字以内，不要输出任何其他内容：

# （此处填文档的真实标题）
- 来源: {filename}
- 核心主张:
  - （1-4 条，每条是一个可独立成立的事实 / 论断 / 结论，具体而非泛泛）
- 关键实体: （机构、人物、产品、项目、政策名，用分号分隔；没有就写：无）
- 概念标签: （3-8 个该文档涉及的主题词，用逗号分隔）
- 潜在关联: （这篇内容可能与哪些其他主题/领域存在因果、从属、对比、制约关系，
  1-3 条，写明关系类型；无明显关联就写：无）

【来源文件名】{filename}
【文档正文】
{body}

输出知识原子："""


# ── Stage 2: full-corpus assembly → relation network ──────────────────────────
# Format fields: {existing_network}, {atoms}
# All knowledge atoms in → curated concept/entity/synthesis pages + relations.md.
ASSEMBLE_PROMPT_TEMPLATE = """你面前是这个知识库的全部「知识原子」——每个原子是一篇源文档蒸馏后的精华。
现在进行第二级蒸馏：**组装知识关系网**。这才是真正构建知识库的环节，不是简单罗列。

你的任务（按重要性排序）：
1. **实体归并**：识别指向同一对象的不同写法（如"国家制造业创新中心"与"制造业创新中心"），
   合并为统一的实体页，列出其全部已知事实。
2. **概念聚类**：把讲同一主题不同侧面的原子归到同一概念页，形成完整论述。
3. **关系提取**：显式写出实体/概念之间的因果、从属、对比、制约关系——这是关系网的核心。
4. **矛盾检测**：不同原子对同一事实有冲突描述时，并列呈现并加 [待核实] 标注。
5. **强制溯源**：每个结论或关系边后用（来源: 文件名）标注其原子来源，绝不切断与原文的链接，
   防止蒸馏误差在层间放大。

产出以下页面（每页一个 FILE_WRITE 块，内容完整）：
- `concepts/<英文短横线名>.md`：核心概念页（定义、关键事实、交叉链接）
- `entities/<英文短横线名>.md`：命名实体页（归并后的统一实体）
- `synthesis/<英文短横线名>.md`：跨原子的洞察、对比、结论
- `relations.md`：**知识关系网总图**，分类列出显式关系边，每条形如
  `[[实体A]] —(制约)→ [[项目B]]（来源: x.md）`
- `index.md`：总目录，按主题分类列出所有页面，每条带一句话摘要和 [[链接]]

内部链接用 `[[页面名]]`。用中文撰写，英文术语保留原文。

【当前已有知识库页面（若非空，必须保留已有知识并融合，不得丢失）】
{existing_network}

【全部知识原子】
{atoms}

输出 FILE_WRITE 块："""


# ── Stage 2 (chunked): incremental assembly when atoms exceed budget ──────────
# Format fields: {chunk_no}, {n_chunks}, {existing_network}, {atoms}
ASSEMBLE_INCREMENTAL_PROMPT_TEMPLATE = """继续组装知识关系网。知识原子数量超过单次上下文预算，
正在分批融合——这是第 {chunk_no}/{n_chunks} 批原子。

**关键**：下方已有知识库页面是前几批组装的成果。你要把本批新原子**融合进**这个已有关系网，
而不是另起炉灶：补充新事实、新增关系边、归并新出现的实体、更新受影响的概念页。
绝不丢失已有知识。每个结论后用（来源: 文件名）标注溯源。

按需更新这些页面（每页一个完整 FILE_WRITE 块）：
- `concepts/`、`entities/`、`synthesis/` 下的相关页面
- `relations.md`：补充本批带来的新关系边
- `index.md`：登记新增页面

【已有知识库关系网（前几批成果，必须保留并融合）】
{existing_network}

【本批知识原子】
{atoms}

输出 FILE_WRITE 块："""


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


# Format fields: {index_content}, {relations_content}, {pages_content}, {question}
QUERY_PROMPT_TEMPLATE = """您好！我是本知识库的专属智能服务助手。知识库采用多层蒸馏技术构建，凝聚了经过结构化整理的专业知识。

**服务规范**
- 您的问题将依据下方知识库内容进行专业、准确的解答，内容来源均有据可查。
- 回答中将标注引用来源 [[页面名]]，便于您进一步查阅原始资料。
- 如知识库中暂无相关信息，将如实告知，并提示您可补充采集的内容方向。
- 回答力求条理清晰、表达得体，以便您高效获取所需信息。

如有上文对话记录，请联系上下文，保持回答的连贯性与一致性。

【知识库目录】
{index_content}

【知识关系网（推理骨架）】
{relations_content}

【相关知识库页面】
{pages_content}

【您的问题】
{question}

请为您解答："""


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
GENERAL_CHAT_SYSTEM = """您好！我是 Reptile 智能知识库系统的专属服务助手，由 DeepSeek 大模型驱动。

本系统致力于为您提供专业的知识采集、结构化知识库构建及智能问答服务。

**当前服务说明**
目前处于通用对话模式（您尚未选择具体知识库）：
- 将为您提供准确、具体、条理清晰的专业解答。
- 如您的问题涉及已采集的网站或专项领域，建议选择对应知识库，以获得基于专属资料的精准回答。
- 如需帮助，请随时告知，我将竭诚为您服务。

回答使用规范中文，英文专业术语保留原文，表达得体、简洁明了。"""
