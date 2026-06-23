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
# Format fields: {entity_hint}, {existing_network}, {atoms}
# All knowledge atoms in → curated concept/entity/synthesis pages + relations.md.
ASSEMBLE_PROMPT_TEMPLATE = """你面前是这个知识库的全部「知识原子」——每个原子是一篇源文档蒸馏后的精华。
现在进行第二级蒸馏：**组装知识关系网**。这才是真正构建知识库的环节，不是简单罗列。

你的任务（按重要性排序）：
1. **实体归并**：识别指向同一对象的不同写法（如"国家制造业创新中心"与"制造业创新中心"），
   合并为统一的实体页，列出其全部已知事实。**务必参考下方【已登记实体】清单**：若本批实体
   与清单中某项指向同一对象，必须复用其页面名归并进去，绝不新建重复实体页。
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

【已登记实体（跨批次实体身份表，优先归并到这些已有实体页，避免碎片化重复建页）】
{entity_hint}

【当前已有知识库页面（若非空，必须保留已有知识并融合，不得丢失）】
{existing_network}

【全部知识原子】
{atoms}

输出 FILE_WRITE 块："""


# ── Stage 2 (chunked): incremental assembly when atoms exceed budget ──────────
# Format fields: {chunk_no}, {n_chunks}, {entity_hint}, {existing_network}, {atoms}
ASSEMBLE_INCREMENTAL_PROMPT_TEMPLATE = """继续组装知识关系网。知识原子数量超过单次上下文预算，
正在分批融合——这是第 {chunk_no}/{n_chunks} 批原子。

**关键**：下方已有知识库页面是前几批组装的成果。你要把本批新原子**融合进**这个已有关系网，
而不是另起炉灶：补充新事实、新增关系边、归并新出现的实体、更新受影响的概念页。
绝不丢失已有知识。每个结论后用（来源: 文件名）标注溯源。

**实体身份跨批一致性**：下方【已登记实体】是前几批已建立的实体页清单。受上下文预算所限，
它们的完整页面未必都出现在【已有知识库关系网】里——但只要本批原子提到的对象与清单某项一致，
就必须复用其页面名归并，绝不因为"没看到那一页"而新建重复实体页。这是防止知识库长期碎片化的关键。

按需更新这些页面（每页一个完整 FILE_WRITE 块）：
- `concepts/`、`entities/`、`synthesis/` 下的相关页面
- `relations.md`：补充本批带来的新关系边
- `index.md`：登记新增页面

【已登记实体（跨批次实体身份表，必须优先归并，不得重复建页）】
{entity_hint}

【已有知识库关系网（前几批成果，必须保留并融合）】
{existing_network}

【本批知识原子】
{atoms}

输出 FILE_WRITE 块："""


# ── Stage 3: relation-network sharding (scalability) ──────────────────────────
# Format fields: {relations_content}
# Triggered when relations.md exceeds RELATIONS_SHARD_THRESHOLD chars. Clusters
# the monolithic relation graph into topic shards + a lightweight index so
# queries load only the relevant shard instead of the whole growing file.
# Built with BUILD_MODEL (v4-pro) for relation-clustering reasoning quality.
RELATIONS_CONSOLIDATE_PROMPT_TEMPLATE = """下面是当前知识库的关系网总图（relations.md）。它已经变得较大，需要进行**主题分片**以保持在上下文预算内可管理。

你的任务：把这份关系网按**主题/领域/模块**聚类，每个主题输出一个独立分片文件，再输出一个轻量总索引。

输出规则：
1. 若干 `relations/<英文短横线主题名>.md`——每个主题的关系边子集，原样保留所有（来源: x.md）标注，不得增删或改写关系内容。
2. `relations/_index.md`——主题索引，格式：
   `- [[<英文短横线名>]]：<一句话说明该主题涵盖的实体与关系类型>`
   每行对应一个分片文件。
3. 每个分片控制在 3000 字以内；主题过细则合并，总分片数 3-8 个为宜。
4. **不要输出 `relations.md` 本身**（它由 Stage-2 管理，分片是其派生视图）。
5. 不要修改任何关系内容，只做聚类切割。

【当前 relations.md 内容】
{relations_content}

输出 FILE_WRITE 块："""


# ── Stage 4: cross-topic synthesis (the "reduce" — connect clues) ─────────────
# Format fields: {relations_content}, {index_content}, {existing_synthesis}
# After topic-clustered assembly (the "map"), this reduce pass reads the whole
# relation network + index and actively forges NON-OBVIOUS cross-topic links into
# explicit, sourced logic chains. This is what makes the wiki "贯通线索、创造新
# 逻辑链路" at build time rather than only at query time. Built with BUILD_MODEL.
# Hard rule: never fabricate facts — only connect what is already in the network;
# inferred (not directly stated) links must be tagged [推断].
CROSS_SYNTHESIS_PROMPT_TEMPLATE = """这是知识库构建的最高一层蒸馏：**跨主题贯通**。前面的环节已经把原子组装成
按主题聚类的实体页、概念页与关系网。现在请你站在全局视角，**把分散在不同主题里的线索连成显式的逻辑链路**——
这正是知识库的最高价值：发现单篇文档、单个主题里看不出来的关联。

你的任务：
1. **跨主题关联**：找出分属不同主题/领域、但存在因果、制约、促成、对比、前置条件等关系的实体或概念，
   显式写出它们之间的连接。优先那些非显而易见、需要跨页面才能看出的关联。
2. **逻辑链路**：把多步关联串成链，形如
   `[[A]] —(因为)→ [[B]] —(促成)→ [[C]]`，并用一段话解释这条链路的现实含义与推理依据。
3. **结论与洞察**：基于关系网给出有依据的跨主题结论、趋势判断、潜在风险或机会。
4. **严格不臆造**：只能连接关系网中已存在的实体/概念。
   - 关系网已显式记载的事实链接，照常用（来源: 文件名）溯源。
   - 你**推理推断**出（关系网未直接陈述）的链接，必须在该条目末尾加 **[推断]** 标注，
     绝不把推断写成既成事实。证据不足就不要写。
5. **增量融合**：下方若有已存在的 synthesis 页面，保留其有效内容并补充新链路，不要推翻重写、不要丢失。

产出（每页一个完整 FILE_WRITE 块）：
- `synthesis/<英文短横线主题名>.md`：每条跨主题洞察/逻辑链一节，含解释与溯源/[推断] 标注。
  按大主题组织，总数 2-6 页为宜，每页 3000 字以内。
- `index.md`：把新增/更新的 synthesis 页面登记进总目录（保留其余目录条目）。

内部链接用 `[[页面名]]`。用中文撰写，英文术语保留原文。

【知识关系网（推理骨架）】
{relations_content}

【知识库目录（可连接的实体/概念清单）】
{index_content}

【已有 synthesis 页面（必须保留并融合）】
{existing_synthesis}

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
