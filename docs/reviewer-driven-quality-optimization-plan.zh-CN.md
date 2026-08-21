# 基于当前代码复核的审稿驱动质量优化方案

## 1. 文档定位

本文基于当前 Review Writer 本地代码，而不是旧版说明或理想化架构，对专业审稿意见所对应的功能改进进行二次审核。重点只放在改进内容、处理逻辑和实施边界：

1. 判断原改进方案中哪些能力项目已经具备，避免重复建设；
2. 找出真实缺口、逻辑冲突和可能放大成本的设计；
3. 给出尽量复用现有服务、产物和接口的实施方案；
4. 在保证科学正确性的同时，减少逐项人工确认和整条流程阻断。

本方案不针对联烯、铜催化或某一批论文写死规则。主题差异通过项目范围、Taxonomy Profile、规则包和用户选择表达。

## 2. 冗余审核与改造边界

### 2.1 总体结论

原文提出的审稿问题大多真实，但“新增 Review Quality Layer、5 个领域服务、8 份质量报告和通用 `/quality/*` 修复 API”会形成平行架构。当前项目已经按阶段拥有质量信息、不可变产物、依赖失效、任务进度和人工审核；新建一套质量层会造成：

- 同一事实存在两个当前版本；
- 阶段产物已经更新，但质量报告仍指向旧 hash；
- 相同检查在阶段服务和质量服务中重复运行；
- 前端不知道应该以阶段状态还是 quality 状态为准；
- 修复动作绕开现有阶段接口，破坏 stale 传播和版本追踪；
- 测试与维护成本显著增加。

正确方向是“扩展现有产物字段和阶段服务”，而不是新建平行质量平台。

### 2.2 逐项判断

| 原提议 | 当前已有能力 | 审核结论 | 修订方式 |
|---|---|---|---|
| `review_scope.json` | selected outline 和 Blueprint 已含 `scope_contract`、`scope_diagnostics` | 重复 | 扩展现有 scope 字段，加入范围声明、检索截止日和 coverage 指标 |
| `taxonomy_contract.json` + Taxonomy Validator | `classification_basis()`、`taxonomy_diagnostics()`、Planning 阻断和 Blueprint contract | 重复 | 在现有 classification basis 中增加 overview axis、辅轴映射和一致性检查 |
| `section_synthesis_report.json` | Blueprint synthesis requirements；Sections evidence package、synthesis state、writing plan | 大部分重复 | 在 `sections/synthesis_state.json` 增加比较覆盖率和论文角色重复度 |
| `figure_relevance_report.json` | 图像 source/output hash、论文来源绑定、审批策略、完整性门禁、manifest | 重复 | 在当前 selection/manifest 中记录论文级代表性、正文放置结果、caption/callout、edge 和 skip reason |
| `rendered_document_qa.json` | `final/pdf-qa.json`、`final/render_manifest.json` | 重复 | 扩展现有 PDF QA，并为 DOCX 增加结构检查字段 |
| 五个新的 domain services | Library/Planning/Sections/Figures/Drafts/Final 已承担相同阶段职责 | 过度拆分 | 继续在现有阶段服务中实现；只抽取无状态纯函数 helper |

## 3. 真实缺口与推荐改造

### 3.1 Metadata：缺少字段级跨源核验

现状：系统有不可变 Metadata 版本、编辑接口、Crossref/OpenAlex/Semantic Scholar/arXiv 连接器和基础校验，但没有完整的字段级来源证据、冲突置信度和人工选择界面。

Metadata 的规范字段仍保存在当前不可变 Metadata artifact 中；核验来源、冲突候选、置信度和 `checked_at` 属于“书目信息核验记录”，应保存在 Library 的运行状态中，而不是写入参与下游依赖哈希的 Metadata 内容。

建议的核验记录结构：

```json
{
  "bibliography_audit": {
    "status": "pending|verifying|verified|conflict|unavailable",
    "field_provenance": {
      "doi": [{"source": "crossref", "value": "...", "confidence": 0.99}],
      "year": [{"source": "pdf_first_page", "value": 2024, "confidence": 0.85}]
    },
    "conflicts": [],
    "resolved_by": "automatic|user|unresolved",
    "checked_at": "..."
  }
}
```

处理规则：

- PDF 完成 MinerU 和基础 Metadata 构建后，自动提交异步核验任务，查询当前可用的 Crossref/OpenAlex 等来源并比对 PDF 首页；
- 核验任务不阻塞 PDF 入库、文献检索和后续写作；外部来源暂时不可用时记录 `unavailable`，允许稍后单独重试；
- 只有 DOI 精确匹配，并且标题与作者集合也达到高置信一致时，系统才可以自动修正规范 Metadata；
- 标题、作者或年份的单项相似不能单独用于改写 DOI，也不能覆盖来源不明的现有字段；
- 无法确定的冲突保留当前规范值，同时展示字段、候选来源和差异，不自动修改、不自动排除论文，也不停止其他章节；
- 当前“标记为已审核”按钮继续保留，供用户在需要时确认冲突，但不是进入检索、章节、图像、初稿或终稿的必要门禁；
- 只有标题、作者、年份、DOI 等规范字段实际改变时，才使用现有 Metadata 更新接口发布新版本并触发下游过期；
- 仅重新核验、更新时间或改变核验状态时，不发布新的 Metadata artifact，避免无意义地使 Matrix、章节、初稿和终稿过期；
- 书目信息核验记录不是第二份书目真相，终稿始终读取当前规范 Metadata 字段。

外部来源失败处理：

1. Crossref 和 OpenAlex 分别记录 `pending/running/verified/not_found/rate_limited/unavailable`，一个来源失败不能取消另一个来源；
2. PDF 首页和 MinerU 提取结果始终作为基础值。Crossref/OpenAlex 都失败时，不修改规范 Metadata，项目仍可继续检索和写作；
3. 网络超时、连接错误和 HTTP 5xx 使用连接器自身的有限退避重试；HTTP 429 遵守 `Retry-After`；明确的 400、401、403 或 `not_found` 不做无意义循环重试；
4. 自动任务达到重试上限后保存失败状态、最后错误和最后尝试时间，不让后台任务长期停留在运行中；
5. Library 页面显示各来源状态，并提供“重新核验此论文”和“重新核验失败项”，重试只处理失败来源，不重复请求已经成功的来源；
6. DOI/标题查询结果按规范化查询键缓存，批量上传中的相同论文不重复访问外部服务；
7. 用户手动修正或点击“标记为已审核”后，当前人工结果优先；后续自动核验只能提示新的差异，不能静默覆盖人工值；
8. Crossref/OpenAlex 属于书目连接器，不经过文本或图像 Model Gateway，也不使用模型 Provider 的重试和并发额度；它们应有独立、较低的并发限制，避免触发外部限流。

### 3.2 检索覆盖度：范围契约已有，覆盖指标不足

现状：Scope Contract 已存在，检索也支持多来源与去重，但缺少主题簇、年代、关键术语和最新文献覆盖的量化说明。

推荐扩展现有 `scope_diagnostics`：

- `scope_statement` 和 `coverage_claim`：记录用户实际声明的主题边界及覆盖程度，不提供新的系统质量模式；
- `search_cutoff_date`；
- topic cluster coverage；
- year distribution 和 recent-paper ratio；
- required concept coverage；
- title/abstract/conclusion 中过度概括语句的位置；
- 扩大检索或缩小范围的建议。

该信息应在 Discovery/Planning 页面显示，并随 Selected Outline/Blueprint 版本保存。没有必要新增 `review_scope.json` 和 `literature_coverage_report.json` 两个相互依赖的文件。

覆盖度只能相对于用户声明的范围、已配置的数据源和实际检索结果进行诊断。没有完整领域基准集时，不得给出“已覆盖全领域 90%”一类虚假精确结论；应展示主题簇、年份和来源分布以及可解释的缺口。

### 3.3 Taxonomy 与总览图：扩展已有契约

现状：正文分类契约已经存在，但总览图的分类轴没有完全纳入同一 contract。

推荐在当前 `classification_basis` 中增加：

```json
{
  "primary_axis": "substrate_class",
  "secondary_axes": ["catalyst", "mechanism"],
  "overview_axis": "substrate_class",
  "overview_secondary_axis": "catalyst",
  "axis_mapping_note": "..."
}
```

用户执行 Generate Overview Figure 时必须读取当前 Blueprint 的 contract。若 overview axis 与正文主轴不同：

- 有明确辅轴映射时允许生成，并自动写入图注解释；
- 没有映射时让本次生成按正文主轴构建总览图，不额外自动提交另一份图像任务；
- 不因分类角度不同阻断整个终稿。

### 3.4 章节综合：增加指标，不增加新服务

现状：跨论文综合逻辑已经存在，但缺少可观测的“综合是否真的发生”。

建议在 `sections/synthesis_state.json` 增加：

- 每节 comparison axes；
- 每个段落引用的独立论文数；
- cross-paper comparison sentence count；
- quantitative comparison coverage；
- paper primary owner 与 supporting role；
- 同一论文在多个章节的重复叙述比例；
- one-paper-one-summary 风险段落。

这些指标用于发现结构性问题，不能机械要求每个段落都引用多篇论文。方法首报、关键机理或单篇代表性案例可以由一篇论文主导，但章节整体必须体现比较、边界或演进关系。

Stage 04 尚未生成 Draft，不能在这里直接调用 Draft 段落候选接口。若综合指标不足，应由 SectionsService 增加按 `section_id` 或 `paragraph_id` 的局部章节重生成任务；进入 Stage 06 后发现的问题，才调用现有 Draft 段落候选和完整性校验流程。无需新增 `section_synthesis_report.json` 或另一套 Manuscript Quality Service。

### 3.5 成稿语言：清除内部工作语言

现状：当前反馈提示中仍可能出现 supplied evidence、available evidence、according to the provided material 等内部工作语言，尚无独立的成稿语言检测维度。

推荐新增两个无状态扩展：

- `review_writer_core/publication_voice.py`：确定性词表、句式和内部占位语言检测；
- 在现有 feedback loop rubric 中增加 `publication_voice` 分项。

修复方式：

- 检测范围限定为正文和需要展示的图注，跳过 References、代码块、隐藏 metadata 和原文直接引语，避免误报；
- 只重写命中的句子或段落；
- 锁定引用、数字、化学式、图号和 protected metadata；
- 候选通过完整性校验且评分提高后进入 proposal；
- 继续沿用现有候选对比、评分、完整性校验和用户确认流程；本轮不增加自动接受或批量接受模型改写。

### 3.6 图像：建立图文论证闭环，并补充边缘检查和局部排除

现状：身份绑定、化学完整性和人工审核较强，但“图片已插入文档”不等于“图片已经参与论证”。人工选中的候选图通常是该论文总体反应、核心策略、代表性结果或关键机理的视觉摘要，因此 Stage 05 应首先锁定论文身份和图像在该论文中的代表性角色，而不是提前强制绑定某个段落。`target_paragraph_id` 或 `inserted_figure` 等机器字段也不能替代读者可见的 Figure/Scheme 调用和学术解释。

每张进入正文的图必须形成以下闭环：

```text
来源论文、source figure 与论文级代表性角色
  → 当前批准的图像版本
  → 根据该论文在综述中的主要归属确定放置章节
  → 正式 Figure/Scheme 编号和图注
  → 正文中的可见编号调用
  → 基于整篇论文证据生成总体解释
  → 论文来源、编号和解释一致性复检
```

仅有图片、图注或隐藏 metadata，不算闭环完成。正文至少要有一句说明图中展示的机制、趋势、差异或证据如何支持当前论述，不能只机械插入“见图 X”。

论文绑定与正文放置必须分开保存。Selection 保存稳定的论文绑定；Figure Manifest 保存根据当前章节草稿派生的正文放置：

```json
{
  "selection": {
    "paper_id": "P137",
    "source_figure_id": "Scheme 2",
    "representative_role": "core_transformation|mechanism|scope|paper_overview",
    "evidence_ids": ["E12", "E18"],
    "status": "pass|warning|manual"
  },
  "manifest_placement": {
    "section_id": "S03",
    "target_paragraph_id": "S03-p2",
    "published_label": "Scheme 3",
    "visible_reference_found": true,
    "interpretation_found": true,
    "paper_citation_found": true,
    "evidence_consistent": true
  },
  "edge_check": {
    "ink_touches_edge": false,
    "margin_px": 18
  },
  "skip": {
    "excluded": false,
    "reason": ""
  }
}
```

生成与复检逻辑：

1. 图像阶段由人工确认该图能否代表来源论文的总体反应、核心策略、代表性结果或关键机理，只绑定 `paper_id`、`source_figure_id`、代表性角色和论文证据；
2. 图像阶段不强制指定固定段落，避免在正文结构变化后产生无意义的过期；
3. 在确认图像阶段时，FiguresService 根据当前章节草稿中对该论文的实际引用、primary owner section 和章节叙事派生放置位置，并把现有 Draft 组装所需的 `target_paragraph_id` 写入 Figure Manifest；该字段是可重新计算的放置结果，不是人工选图契约；
4. 如果找不到任何实际引用该论文的段落，不得随意插图；应显示“等待正文放置”，让用户返回章节补充该论文论述或排除该图；
5. 初稿合并继续读取 Figure Manifest 的 `target_paragraph_id`，不修改当前 Draft 插图接口；
6. 系统读取该论文的 Metadata、MinerU 全文、Evidence Package、关键结论和放置段落上下文，生成可见调用句和论文级总体解释；
7. 解释必须说明图片所代表的核心转化、催化策略、机理或研究意义，但不能把单张局部图片夸大成论文全部结论；
8. 调用句只能使用来源论文已有的机制、趋势和数据，不得根据图片外观推断新事实；
9. 对综合多个论文的段落，不要求整段只来源于该图片论文，但必须包含该论文的可追踪引用，且与图像解释相邻或逻辑连续；
10. 终稿检查每张图片是否有图注、至少一个正文调用、至少一个解释性语句，以及解释是否能回溯到绑定论文的证据；
11. 发现孤立图片、错误编号或来源冲突时，只标记该图片、调用句和实际放置段落，不使整篇稿件失效。

图片更新的失效范围必须区分：

- 只进行 SVG 排版编辑且 source identity 不变：保留正文调用，只重新检查图注、画布和边缘；
- 更换同一论文中的 source figure：重新判断代表性角色；角色不变时只使图注和解释性语句过期，角色改变时同时重新计算放置位置；
- 更换为另一篇论文的图片：使论文绑定、图注、调用句和实际放置位置的证据一致性全部过期；
- 排除图片：自动删除正文调用和孤立图注，但保留原图产物及操作记录。

交互调整保持现有源图选择、重绘、人工批准和阶段确认步骤，不合并成新的通用流程，仅补充：

- 对失败图提供“一键排除并继续”，只移除该图的正文 callout；
- 在图片详情中显示“绑定哪篇论文、代表该论文的哪类内容、被放入哪个章节、被哪些段落调用、是否包含解释性语句”；
- 对“等待正文放置”的图显示原因，并提供跳转到来源论文主要归属章节；
- 提供“跳转到正文调用”和“重新生成论文级图像说明”，后者只修改实际放置段落中的相关句子；
- 涉及化学结构真实性的 AI 图仍需人工审核，不能静默自动批准。

SVG 保存后即使 source identity 不变，也必须按当前图像审核逻辑重新确认图片本身；“保留正文调用”只表示无需重写论文级解释，不代表编辑后的化学图自动通过。

### 3.7 导出 QA：扩展现有产物，不建立第二份报告

现状：`final/pdf-qa.json` 已检查技术可用性，但尚未充分检查页面空白、文本越界、图片裁切、孤立图注和重叠。

推荐：

1. Final Manuscript State 继续作为 DOCX/PDF 的唯一语义输入；
2. PDF 导出后渲染页面缩略图，向现有 `final/pdf-qa.json` 增加 `page_findings`；
3. 检测页面空白率、内容边界、低分辨率图片、图注跨页、异常空页和重叠风险；
4. DOCX 先做 OOXML 结构与关系文件检查；
5. 仅在部署环境存在 LibreOffice/Word renderer 时，做 DOCX 页面级渲染 QA；
6. 不把“DOCX 转 PDF”设为所有部署的强制链路，因为当前 PDF 是独立渲染产物，强行改变会造成双排版源。

页面缩略图检测应使用随部署提供的可移植渲染器，例如 PyMuPDF、Poppler 或镜像内固定版本的 Chromium，不能再次依赖仅 Windows 存在的 Microsoft Edge。

除文件损坏、正文不可见、引用目标不存在等确定性问题外，页面问题只显示具体警告，不禁止下载带警告版本。

## 4. 已确认的逻辑问题与实现约束

### 4.1 重试存在多层放大

当前至少存在三层相关重试：

- `review_writer_api/model_gateway.py`：Provider 级暂态错误最多 3 次；
- `review_writer_core/model_gateway_client.py`：网关 HTTP/传输错误最多 3 次；
- `review_writer_api/routers/sections.py`：章节任务对暂态 provider 错误再执行最多 3 次。

最坏情况下，同一失败对象可能触发多轮嵌套请求，增加 503、并发拥塞、费用和等待时间。虽然 request key 和 in-flight join 能减少部分重复，但失败状态后的再执行仍可能放大。

特别是章节任务，如果外层重试重新执行整个章节批次，已经成功并付费的前序模型调用也可能被再次执行。修复时必须以章节或段落为 checkpoint 恢复，不能简单重放整个 Stage Job。

推荐统一职责：

```text
服务器 Model Gateway
  → 唯一负责 Provider 级重试、退避、并发和 usage 记录

Gateway Client
  → 只处理短暂断线、查询/加入同一幂等请求，不重新放大 Provider 调用

Stage Job
  → 按对象 checkpoint；失败对象记为 failed，继续后续对象；用户可单独恢复
```

服务器当前没有项目级备用 Provider 路由。不要在 skill 或客户端脚本里自行切换 URL/Key；若未来支持备用路由，应在 Model Gateway 内统一实现并记录 provider_id、尝试次数和费用。

### 4.2 Stage 05 单图失败可能阻断整个阶段

当前图像阶段对全部 selected figures 的可用数量有整体要求。对于不关键或无法重绘的图片，这会带来不必要的人工成本。

推荐增加由用户明确触发的 `exclude/skip with reason`，更新当前图像选择并重新发布 Manifest，使 `selected_count` 与实际使用图一致，同时只移除对应 callout。系统不能因为生成失败而自动排除用户选中的图；化学完整性不通过的图不能直接进入正文，但用户可以选择排除后继续完成阶段。

### 4.3 PDF QA 与 DOCX QA 边界不清

原方案要求 DOCX 一律转换 PDF 后检查，与当前独立 PDF 渲染链冲突，也依赖部署环境中的 Office/LibreOffice。应把 PDF 页面检测、DOCX OOXML 检测和可选 DOCX 渲染检测分开，避免部署差异造成假阻断。

### 4.4 产物修改必须保持现有版本边界

任何改变规范 Metadata、段落、图像或终稿内容的修复，都必须调用现有阶段服务并发布新 ArtifactVersion。只更新核验时间或核验状态时保存在相应运行记录中，不制造内容版本。新增检查逻辑不得直接覆盖 workspace 文件，否则当前指针、hash、依赖和 stale 状态会失真。

## 5. 减少人工参与的推荐流程

保留少量有科学责任的确认，将重复操作合并：

| 阶段 | 推荐人工动作 | 自动完成的内容 |
|---|---|---|
| 文献库 | 只处理低置信 Metadata 身份冲突 | MinerU、字段归一化、高置信合并、质量标记 |
| 检索 | 一次确认项目语料和范围 | 去重、基础标签、覆盖诊断、建议检索式 |
| Planning | 一次确认 Blueprint | Matrix、论文 owner、比较轴、taxonomy diagnostics |
| Sections | 保持当前一次整体章节确认；不增加逐章确认 | 按章生成、checkpoint、综合指标、失败章单独恢复 |
| Figures | 保持当前源图选择、AI 图人工批准和阶段确认 | 身份校验、边缘检查、状态同步；失败图由用户决定重试或排除 |
| Draft | 沿用现有候选查看与用户确认 | 逐段评估、候选评分、完整性校验、增量更新 |
| Final | 保持当前结论、总览图、终稿和导出操作 | 引用清洗和页面 QA 随对应任务执行 |

人工确认应集中到阶段末的问题清单，不应逐条弹浏览器对话框。普通警告不阻止保存、继续生成或下载；确定性问题只限制对应对象或损坏的导出文件。

## 6. 数据和接口改造原则

### 6.1 扩展现有产物

优先扩展：

- Library 书目信息核验记录：`bibliography_audit`，异步更新且不参与规范 Metadata 的下游依赖哈希；
- Selected Outline/Blueprint：`scope_contract`、`scope_diagnostics`、`coverage_diagnostics`、扩展 classification basis；
- `sections/synthesis_state.json`：比较覆盖、论文角色和重复度；
- figure selection/manifest：paper binding、representative role、正文 placement/callout、paper-level interpretation、edge check、skip reason；
- `draft/quality.json`：publication voice、coverage consistency、synthesis 指标；
- `final/pdf-qa.json`：页面级 findings；
- `final/render_manifest.json`：DOCX 结构检查和可选渲染检查信息。

不新增与当前阶段产物平行的质量真相文件。

### 6.2 复用现有接口

- Metadata 修复走现有 Metadata 更新接口；
- 范围与大纲修复走 Planning 保存接口；
- 段落修复走 Draft paragraph/candidate/proposal 接口；
- 图像修复走 figure selection、redraw、approval、SVG save 接口；
- 终稿修复走 Final 生成、validation 和 release 接口。

必要时增加一个明确的单图排除接口；不要新增 Readiness 接口或通用 repair dispatcher。各阶段现有接口直接返回问题、受影响对象和可执行动作。

继续保持当前阶段直连模式：

- Draft Quality 保留现有 `issue_id`、`paragraph_id` 和 `route`，前端继续直接调用段落编辑、重写候选和接受/拒绝接口；
- Metadata 问题直接在 Library 页面调用 Metadata 保存接口；
- 范围与分类问题直接在 Planning 页面保存 Outline/Blueprint；
- 图像问题直接调用候选选择、AI 重绘、人工批准、SVG 保存或新增的单图排除接口；
- 终稿和导出问题直接重新调用对应 Final 生成或导出任务；
- JobService 继续使用明确的 job type，不改成通用 issue repair job；
- Model Gateway 只处理由具体阶段任务发起的模型请求，不接收 issue，也不负责选择业务修复动作。

本轮不新增跨阶段 Issue 数据库、全局 issue id、action registry、通用修复入口或问题动作分发层。

## 7. 实施优先级

### P0：先修逻辑一致性与错误放大

1. 收敛模型重试到服务器 Model Gateway；
2. 阶段任务按论文/章节/段落/图像 checkpoint，单对象失败继续；
3. Stage 04 增加章节或段落级恢复，避免调用尚不存在的 Draft 接口；
4. 图像论文绑定与 Manifest 放置分离，同时继续生成当前 Draft 所需的 `target_paragraph_id`；
5. Stage 05 支持用户显式排除失败图后继续；
6. 扩展当前 final/PDF QA，保持 DOCX/PDF 单一语义源。

### P1：补齐审稿暴露的真实质量缺口

1. Metadata 字段级来源、冲突与置信度；
2. Scope coverage diagnostics；
3. publication voice 检测与局部重写；
4. synthesis state 的跨论文比较指标；
5. Overview axis 纳入现有 taxonomy contract；
6. 图像的论文级代表性、正文 placement/callout、论文级解释和 edge 检查；
7. PDF 页面栅格 QA 与 DOCX OOXML QA。

### P2：减少人工成本和改善可解释性

1. 各阶段内部集中显示例外，不建立跨阶段 Issue 系统；
2. 问题到段落、图片、参考文献或页面的准确跳转；
3. 不改变现有模型改写接受流程和阶段确认步骤。

## 8. 验收标准

### 8.1 版本与依赖

- 任何改变规范内容的修复都产生新 ArtifactVersion；仅核验时间或核验状态变化不应让下游产物过期；
- 上游变化只使真实依赖的下游产物过期；
- 刷新、切换阶段或服务重启后，任务与问题状态可以恢复；
- 所有问题列表只引用当前版本，不引用旧 hash。

### 8.2 模型任务

- 一次 Provider 失败不会被三层嵌套放大；
- 批量任务中一个对象失败，其他对象继续；
- 重试不重复计费同一成功的幂等请求；
- 进度显示已完成、成功、失败、重试中和当前对象。

### 8.3 学术质量

- Metadata 冲突定位到字段和来源；
- 范围诊断能解释用户声明的主题边界和覆盖程度是否与现有语料一致；
- 每节至少能报告比较轴和跨论文综合覆盖；
- 内部审计语言能定位并生成受保护的局部候选；
- 同一论文的 primary owner 和 supporting use 可追踪。

### 8.4 图像

- 图像来源、候选、重绘、SVG 输出和正文 callout 使用同一论文身份链；
- 人工候选图在图像阶段绑定来源论文和代表性角色，不由用户强制锁死某个段落；
- Figure Manifest 根据当前章节草稿派生 `target_paragraph_id`，现有 Draft 组装仍能正常插图；
- 每张进入正文的图都有正式编号、图注、至少一个可见正文调用和解释性语句；
- 图像解释基于整篇来源论文的 Metadata、全文和 Evidence Package，并可回溯到论文证据；
- 综合多个论文的段落必须包含图片来源论文的引用，但不要求整段只讨论该论文；
- 隐藏 metadata、插图注释或单独图注不能被误判为正文调用；
- 单图失败可以排除并继续，不会误用旧输出；
- 更换同论文 source figure 只重新判断代表性并使图注、解释过期；更换论文才重新计算放置位置；
- 边缘裁切和画布比例问题有明确检查；
- 未经人工确认的化学 AI 图不会自动进入最终正文。

### 8.5 导出

- Final Manuscript State 是 DOCX 和 PDF 的共同语义来源；
- DOCX 通过 OOXML/关系文件检查；
- PDF 页面 QA 能定位空页、异常空白、裁切、低分辨率图和孤立图注；
- 非致命警告不阻止下载；只有文件损坏、正文不可见等确定性错误才阻止对应导出。

## 9. 不建议采用的做法

- 新建与 scope、taxonomy、synthesis、figure、draft、final 平行的质量真相文件；
- 在 quality service 中直接覆盖 workspace 文件；
- 在 skill 脚本中各自配置备用 API、Key 或 Provider；
- 在客户端、阶段任务和服务器网关同时进行 Provider 级重试；
- 因一张图或一条参考文献失败而停止所有对象；
- 静默接受可能改变科学事实的模型重写；
- 把 DOCX 转 PDF 作为所有部署环境的强制前提；
- 将主题名称、论文编号或特定金属类别硬编码进通用规则。

## 10. 最终结论

当前项目并不缺少质量框架；它已经具备不可变产物、依赖失效、持久任务、范围与分类契约、跨论文综合、图像身份、Rubrics/Loop 和终稿审计。真正需要的不是再建设一套 Review Quality Layer，而是：

1. 在现有阶段数据中补充必要的诊断字段，区分规范内容版本与运行时核验记录；
2. 收敛模型重试和批量失败处理；
3. 补齐 Metadata 跨源核验、覆盖度、成稿语言、图像相关性和页面级 QA；
4. 保持现有人工确认边界；确定性清洗可以自动执行，模型改写仍生成候选并由用户确认，单对象问题采用局部恢复或由用户排除。

按此方案实施，可在不破坏当前七阶段、版本依赖、后台任务、SVG/Ketcher、反馈循环和导出功能的前提下，提高综述的可信度、成稿质量和系统健壮性，同时避免重复服务、重复产物和重复人工审核。
