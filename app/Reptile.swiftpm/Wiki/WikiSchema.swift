import Foundation

/// Prompt templates governing all LLM wiki operations — a faithful port of
/// wiki/schema.py, extended with Stage-3 relation-sharding prompts.
///
/// 混合模型策略：
/// - Stage 1 & 2 build prompts → DeepSeekClient.buildModel (v4-pro，质量优先)
/// - Stage 3 consolidation     → DeepSeekClient.buildModel (v4-pro，关系推理)
/// - Query / page-select       → DeepSeekClient.queryModel (v4-flash，速度优先)
enum WikiSchema {

    static let defaultSchema = """
    你是一个知识库（Wiki）管理员。你的职责是把零散的原始网页文档，提炼并整合成一个结构化、可检索、不断累积的知识库。这不是复制粘贴，而是知识的萃取与组织。

    ## 知识库目录结构
    - `index.md`：总目录。按主题分类列出所有页面，每条带一句话摘要和 [[链接]]。
    - `log.md`：只追加的操作日志。
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

    每次操作后都要更新 `index.md`。除 FILE_WRITE 块外不输出多余解释。
    """

    // MARK: - Stage 1: single-doc distillation → knowledge atom

    static func distillAtom(filename: String, body: String) -> String {
        """
        把下面这一篇文档蒸馏成一个高密度的「知识原子」。

        这是知识库构建的第一级蒸馏：用最精炼的结构化形式，保留这篇文档的全部关键知识，
        丢弃冗余表述、客套、导航性文字。

        严格按以下格式输出，整体控制在 300 字以内，不要输出任何其他内容：

        # （此处填文档的真实标题）
        - 来源: \(filename)
        - 核心主张:
          - （1-4 条，每条是一个可独立成立的事实 / 论断 / 结论，具体而非泛泛）
        - 关键实体: （机构、人物、产品、项目、政策名，用分号分隔；没有就写：无）
        - 概念标签: （3-8 个该文档涉及的主题词，用逗号分隔）
        - 潜在关联: （这篇内容可能与哪些其他主题/领域存在因果、从属、对比、制约关系，
          1-3 条，写明关系类型；无明显关联就写：无）

        【来源文件名】\(filename)
        【文档正文】
        \(body)

        输出知识原子：
        """
    }

    // MARK: - Stage 2: atoms → relation network

    static func assemble(existingNetwork: String, atoms: String) -> String {
        """
        你面前是这个知识库的全部「知识原子」——每个原子是一篇源文档蒸馏后的精华。
        现在进行第二级蒸馏：**组装知识关系网**。这才是真正构建知识库的环节，不是简单罗列。

        你的任务（按重要性排序）：
        1. **实体归并**：识别指向同一对象的不同写法，合并为统一的实体页，列出其全部已知事实。
        2. **概念聚类**：把讲同一主题不同侧面的原子归到同一概念页，形成完整论述。
        3. **关系提取**：显式写出实体/概念之间的因果、从属、对比、制约关系——这是关系网的核心。
        4. **矛盾检测**：不同原子对同一事实有冲突描述时，并列呈现并加 [待核实] 标注。
        5. **强制溯源**：每个结论或关系边后用（来源: 文件名）标注其原子来源。

        产出以下页面（每页一个 FILE_WRITE 块，内容完整）：
        - `concepts/<英文短横线名>.md`、`entities/<英文短横线名>.md`、`synthesis/<英文短横线名>.md`
        - `relations.md`：知识关系网总图，每条形如 `[[实体A]] —(制约)→ [[项目B]]（来源: x.md）`
        - `index.md`：总目录，按主题分类列出所有页面，每条带一句话摘要和 [[链接]]

        内部链接用 `[[页面名]]`。用中文撰写，英文术语保留原文。

        【当前已有知识库页面（若非空，必须保留已有知识并融合，不得丢失）】
        \(existingNetwork)

        【全部知识原子】
        \(atoms)

        输出 FILE_WRITE 块：
        """
    }

    static func assembleIncremental(chunkNo: Int, nChunks: Int,
                                    existingNetwork: String, atoms: String) -> String {
        """
        继续组装知识关系网。知识原子数量超过单次上下文预算，正在分批融合——这是第 \(chunkNo)/\(nChunks) 批原子。

        **关键**：下方已有知识库页面是前几批组装的成果。把本批新原子**融合进**这个已有关系网，
        而不是另起炉灶：补充新事实、新增关系边、归并新出现的实体、更新受影响的概念页。
        绝不丢失已有知识。每个结论后用（来源: 文件名）标注溯源。

        按需更新这些页面（每页一个完整 FILE_WRITE 块）：
        - `concepts/`、`entities/`、`synthesis/` 下的相关页面
        - `relations.md`：补充本批带来的新关系边
        - `index.md`：登记新增页面

        【已有知识库关系网（前几批成果，必须保留并融合）】
        \(existingNetwork)

        【本批知识原子】
        \(atoms)

        输出 FILE_WRITE 块：
        """
    }

    // MARK: - Stage 3: relation network sharding (scalability)
    //
    // Triggered when relations.md exceeds relationsShardThreshold.
    // Clusters relations into topic shards + a lightweight _index so queries
    // load only the relevant shard rather than the full monolithic file.
    // Uses buildModel (v4-pro) for relation-clustering reasoning quality.

    static func relationsConsolidate(relationsContent: String) -> String {
        """
        下面是当前知识库的关系网总图（relations.md）。它已经变得较大，需要进行**主题分片**
        以保持在上下文预算内可管理。

        你的任务：把这份关系网按**主题/领域/模块**聚类，每个主题输出一个独立分片文件，
        再输出一个轻量总索引。

        输出规则：
        1. 若干 `relations/<英文短横线主题名>.md`——每个主题的关系边子集，
           原样保留所有（来源: x.md）标注，不得增删或改写关系内容。
        2. `relations/_index.md`——主题索引，格式：
           `- [[<英文短横线名>]]：<一句话说明该主题涵盖的实体与关系类型>`
           每行对应一个分片文件。
        3. 每个分片控制在 3000 字以内；主题过细则合并，总分片数 3-8 个为宜。
        4. **不要输出 `relations.md` 本身**（它由 Stage-2 管理，分片是其派生视图）。
        5. 不要修改任何关系内容，只做聚类切割。

        【当前 relations.md 内容】
        \(relationsContent)

        输出 FILE_WRITE 块：
        """
    }

    // MARK: - Query: page selection + answer

    static func pageSelect(question: String, indexContent: String) -> String {
        """
        根据知识库目录，列出最适合回答该问题的页面路径（最多 6 个）。
        仅输出页面路径，每行一个，不要任何解释或序号。

        问题：\(question)

        知识库目录：
        \(indexContent)

        相关页面路径：
        """
    }

    static func query(indexContent: String, relationsContent: String,
                      pagesContent: String, question: String) -> String {
        """
        您好！我是本知识库的专属智能服务助手。知识库采用多层蒸馏技术构建，凝聚了经过结构化整理的专业知识。

        **服务规范**
        - 您的问题将依据下方知识库内容进行专业、准确的解答，内容来源均有据可查。
        - 回答中将标注引用来源 [[页面名]]，便于您进一步查阅原始资料。
        - 如知识库中暂无相关信息，将如实告知，并提示您可补充采集的内容方向。

        如有上文对话记录，请联系上下文，保持回答的连贯性与一致性。

        【知识库目录】
        \(indexContent)

        【知识关系网（推理骨架）】
        \(relationsContent)

        【相关知识库页面】
        \(pagesContent)

        【您的问题】
        \(question)

        请为您解答：
        """
    }

    static let generalChatSystem = """
    您好！我是 Reptile 智能知识库系统的专属服务助手，由 DeepSeek 大模型驱动。
    本系统致力于为您提供专业的知识采集、结构化知识库构建及智能问答服务。
    目前处于通用对话模式（您尚未选择具体知识库）：将为您提供准确、具体、条理清晰的专业解答。
    回答使用规范中文，英文专业术语保留原文，表达得体、简洁明了。
    """
}
