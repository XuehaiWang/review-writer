# Review Writer 证据收集、写作组成与综述脉络优化实施方案

## 1. 文档用途

本文把当前项目的流程审计结论转化为可直接指导后续编码的实施契约，重点解决：

1. 证据如何从 PDF 和 MinerU 产物进入可引用事实；
2. 大纲与 Blueprint 如何形成稳定、科学的综述脉络；
3. 如何从论文事实构造跨研究论点和段落，而不是逐篇摘要；
4. 证据不足、模型失败和保底生成时，系统应如何降级；
5. 图像、引用、初稿和终稿如何保持同一论文与证据身份；
6. 如何在不新建平行工作流、不增加大量人工步骤的前提下完成改造。

本文是代码实施文档，不新增用户可见阶段，也不替代以下已有专项方案：

- `end-to-end-evidence-writing-optimization-plan.zh-CN.md`：端到端证据链总体方案；
- `reviewer-feedback-general-content-quality-optimization-plan.zh-CN.md`：审稿意见驱动的内容质量方案；
- `figure-caption-and-placement-optimization-plan.zh-CN.md`：图像、图注和正文放置方案；
- `bibliography-resolution-and-quality-repair-closed-loop-plan.zh-CN.md`：书目字段修复方案。

若这些文档出现重复，本文件只负责“证据查询—Blueprint—Writing Plan—章节—终稿”主链的代码契约，已有 Artifact 和阶段服务仍是唯一业务真相源。

## 2. 当前结论与改造边界

### 2.1 当前架构应保留

以下能力设计正确，不应推翻或重复建设：

- PostgreSQL 持久业务状态与任务状态；
- 不可变 Artifact、当前版本指针、内容哈希和 lineage hash；
- Library、Discovery、Planning、Sections、Images、Draft、Final 七个用户阶段；
- MinerU PDF、Markdown、内容块、表格、图注和图片资产；
- `paper_id`、`fact_id`、`evidence_key`、`claim_id`、`paragraph_id`、`figure_id`；
- 每篇论文只拥有一个正文主章节，允许在引言、结论和横向比较中辅助引用；
- 证据等级、`claim_eligible`、`support_level` 和 `assertion_ceiling`；
- 段落级编辑、候选重写、人工接受和版本回滚；
- 当前文本模型网关、Worker、重试、进度和计费；
- 当前 DOCX/PDF 导出能力。

### 2.2 本轮不做

- 不新增第二套 Evidence、Matrix、Manifest 或稿件状态；
- 不新增独立 Quality Service 或通用修复巨型接口；
- 不新增工作流阶段；
- 不为某个化学主题写死章节或分类；
- 不要求用户逐条确认事实、Claim 或段落；
- 不把一般学术质量问题全部变成硬阻断；
- 不让语言重写替代缺失证据的补检；
- 不把数字引用编号作为跨阶段身份；
- 不把模型生成成功等同于科学内容完整。

### 2.3 “已有能力”不等于“无需改进”

本文提到复用现有模块，是为了避免创建第二套真相源，不表示当前功能已经达到目标。后续编码必须对每项能力同时完成：

```text
确认当前入口和唯一数据源
  → 用回归样例证明当前缺口
  → 在原模块中补齐行为
  → 增加确定性测试和阶段集成测试
  → 用真实流程产物验证结果
```

例如：项目已经有 Classification Contract，但仍需修复混合主轴和无理由单篇章节；已经有 `generation_mode`，但仍需让保底深度真实影响章节 readiness；已经有 Figure Insertion Plan，但仍需增加候选资格、可见调用和读者解释。禁止只因为类、字段或函数已经存在就把对应优化标记为完成。

## 3. 审计发现的通用问题

`zzz` 只作为回归样本。以下问题来自共享逻辑，可能出现在任意主题项目中。

### 3.1 写作指令被当成科学主张检索

当前 Blueprint 的 `review_claims` 中混有两类内容：

- 科学主张，例如“某类方法在较低温度下实现了某种转化”；
- 写作指令，例如“对分配论文进行以论点为中心的综合”。

当前 `build_question_query_plans()` 会把 `must_cover_points` 全部转成 `required_claim_01` 等必需查询。写作指令不可能出现在原论文中，因此形成无法通过重写解决的伪证据缺口。

### 3.2 分类轴混用

同一级章节可能同时使用底物、反应类型、金属体系、机理步骤和立体化学模式。后果包括：

- 同类论文被拆散；
- 单篇论文形成一个顶级章节；
- 出现无分类意义的兜底标题；
- 同一论文在多个章节被重复介绍；
- 总览图、章节标题和正文比较轴不一致。

### 3.3 Blueprint 有写法，没有足够明确的科学 thesis

“比较相关工作并保留证据边界”是写作要求，不是本节最终要建立的科学认识。缺少实际 thesis 时，Writing Plan 容易生成结构完整但信息较浅的段落。

### 3.4 计划深度没有成为章节完成条件

Blueprint 具有目标段落数和目标词数，但章节生成可以在显著低于目标时被视为完成。模型不可用时的 `safe_evidence_fallback` 也容易与正常章节混为一类。

### 3.5 必需证据缺口没有触发正确回流

当前缺口可能继续流入写作和 Draft 评估，再尝试通过改写解决。正确路径应为：

```text
证据缺口
  → 定向补检全文、表格、图注和相邻块
  → 仍缺失则调整或降级 Claim
  → 删除无法支持的具体断言
  → 最后才重新生成受影响段落
```

### 3.6 保底章节被过度视为正常产物

保底输出对于任务健壮性有价值，但它只保证安全使用已验证 Claim，不保证：

- 达到计划深度；
- 完成跨论文比较；
- 有良好的综述语言；
- 形成章节综合结论。

### 3.7 图像来源正确不等于图文论证闭环

论文级候选图可以代表论文总体反应或核心贡献，不必永久绑定某一个段落。但进入终稿时仍需要：

- 明确所属论文；
- 满足最低候选质量；
- 有正文可见调用；
- 有读者可见解释；
- 放在第一次实质讨论该论文之后；
- 使用正确的来源图、AI 重绘图或人工编辑图状态。

### 3.8 Draft 质量结论与 Final 状态不完全一致

当 Draft 决策仍为 `REGENERATE_SECTIONS` 时，系统可以为了用户查看而生成工作稿，但不能把它表现为已经通过科学质量检查的最终结果。

## 4. 目标主链

```text
PDF / MinerU 不可变版本
  → Canonical Metadata 与全文索引
  → Discovery 范围和论文集合
  → Matrix 科学事实
  → 单一主轴 Outline
  → 有真实科学 thesis 的 Blueprint
  → 科学问题级 Evidence Package
  → 比较矩阵与 Synthesis State
  → Claim / Paragraph Writing Plan
  → 证据约束章节
  → Draft 评估与问题定向回流
  → 论文级图片池和正文级插图计划
  → Final 语义一致性检查
  → DOCX / PDF
```

每一阶段只读取上游“当前 Artifact”，并把上游 Artifact ID、内容哈希和契约版本写入自己的依赖。下游产物不得通过目录扫描猜测当前内容。

## 5. 统一概念模型

### 5.1 科学主张与写作要求分离

Blueprint section 增加明确字段，旧 `review_claims` 在迁移期只作为兼容输入：

```json
{
  "section_id": "S02",
  "scientific_thesis": {
    "text": "本节要由证据建立的科学认识",
    "status": "proposed|evidence_supported|partially_supported",
    "evidence_scope": ["P001", "P002"]
  },
  "scientific_claims": [
    {
      "claim_id": "S02-C01",
      "proposition": "可由原论文证明或限制的科学陈述",
      "claim_type": "reported_result|comparison|mechanism|scope|limitation",
      "primary_papers": ["P001"],
      "comparison_papers": ["P002"],
      "required_fact_roles": ["method_conditions", "quantitative_results"],
      "required_for_section": true
    }
  ],
  "writing_requirements": [
    {
      "requirement_id": "WR-S02-01",
      "type": "cross_study_synthesis",
      "instruction": "避免逐篇摘要，按共享比较轴组织"
    }
  ]
}
```

规则：

- 只有 `scientific_claims` 可以生成 `required_claim_*` 查询；
- `writing_requirements` 只进入 Blueprint 校验、Writing Plan 和写作提示词；
- 旧 Claim 只有在具有明确科学对象、事实角色、来源论文并且能够被证实或证伪时，才迁移为 scientific Claim；仅包含操作要求的内容迁移为写作要求；`draft`、`write`、`synthesize`、`compare assigned papers` 等词只能作为辅助信号；
- 无法确定时标记 `legacy_unclassified`，不得自动成为必需证据查询。

### 5.2 统一科学 Claim 状态

```text
planned
  → evidence_supported
  → partially_supported
  → evidence_missing
  → omitted
```

- `evidence_supported`：可以按 assertion ceiling 写入正文；
- `partially_supported`：只能使用限定表述；
- `evidence_missing`：先进入定向补检；补检后仍缺失则降级或删除；
- `omitted`：保留审计记录，不进入正文；
- Writing 模型不得把 `evidence_missing` 自动改写成来源没有报告。

### 5.3 章节生成状态分层

章节业务层对外提供派生字段 `section_readiness`：

```text
scientific_complete
evidence_safe_but_shallow
provider_fallback
needs_evidence_repair
needs_structure_repair
failed
```

`section_readiness` 不是第二套可独立修改的状态。它必须由共享纯函数根据现有 `generation_mode`、Evidence gaps、比较覆盖、深度诊断和校验结果重新计算。Job 可以 `succeeded`，但派生 readiness 仍可为 `evidence_safe_but_shallow`；前端不能单独写入该字段。

## 6. Planning：大纲与 Blueprint 改造

### 6.1 分类契约

每个项目必须只有一个一级主轴，辅助轴只能用于二级组织和横向比较：

```json
{
  "primary_axis_id": "当前 classification contract 中的真实科学轴 ID",
  "secondary_axes": ["stereochemical_regime", "catalyst_or_method"],
  "axis_source": "user_selected|topic_inferred|reference_style_adapted",
  "section_partition_policy": "single_primary_axis",
  "minimum_body_papers": 2,
  "single_paper_section_policy": "merge_unless_scientifically_justified"
}
```

`topic_guided` 只能作为 `axis_source` 或 Outline 生成方式，不能作为科学轴 ID。主轴不使用固定枚举；`product`、`application`、`ligand_or_chiral_source` 以及项目 Profile 定义的其他轴均可成为主轴。所有阶段继续读取现有 `classification_contract.primary_axis_id` 和 fingerprint，不新增平行分类契约。

`minimum_body_papers` 是默认合并建议，不是绝对硬门。单篇章节只有满足以下任一条件才保留：

- 用户明确要求；
- 代表独立且重要的方法学转折；
- 无法归入邻近类别且有独立科学 thesis；
- Blueprint 写入非空 `single_paper_justification`。

### 6.2 章节自动重路由

Blueprint 生成后执行确定性诊断：

1. 一级标题是否都属于同一主轴；
2. 是否存在 `other`、`miscellaneous`、`allenation of ...` 等无边界兜底标题；
3. 单篇章节是否缺少 justification；
4. 同一论文是否有多个正文 primary owner；
5. 同类论文是否因辅助轴被拆散；
6. 引言和结论是否只使用 supporting papers；
7. `topic_partition` 和 `boundary_rationale` 是否非空。

自动修复顺序：

```text
归入语义最接近的现有章节
  → 合并相邻单篇章节
  → 将辅助轴降为二级标题或段落比较轴
  → 仍无法路由时生成“待路由”诊断，不创建正文兜底章节
```

### 6.3 科学 thesis 生成

每个正文 section 的 thesis 必须由结构化事实产生，至少包括：

- 研究对象或输入；
- 共享转化或方法问题；
- 至少一个比较轴；
- 证据能够支持的共同认识；
- 仍不能得出的结论。

禁止仅以“本节将比较分配论文”作为 thesis。

若事实不足，生成 `thesis_status=provisional` 并要求 Matrix/Evidence 补检，而不是生成空泛 thesis。

### 6.4 目标深度契约

兼容旧 Blueprint 中的 `target_words` 数值或范围字符串，同时在新契约中增加可计算字段：

```json
{
  "target_paragraph_count": 5,
  "target_word_min": 1000,
  "target_word_max": 1500,
  "minimum_comparison_paragraphs": 2,
  "requires_section_synthesis_exit": true
}
```

目标不是硬性凑字数。低于下限时，系统判断缺少的是证据、比较还是语言展开，并给出对应科学状态。

## 7. Evidence Package：问题级检索和缺口回流

### 7.1 查询来源

问题计划只由以下结构生成：

- `scientific_thesis`；
- `scientific_claims`；
- `required_fact_roles`；
- 比较矩阵缺失字段；
- 已验证的用户科学问题。

不得由 `writing_requirements`、UI 文案或内部审计语言生成检索词。

### 7.2 查询流程

每个科学问题执行：

```text
精确短语和结构字段检索
  → 关键词组检索
  → 同义词和领域 Profile 扩展
  → 表格、图注和脚注检索
  → 命中块的前后相邻块
  → 必要时按页或章节局部扩大
```

每个 scientific Claim 至少生成两条互补路线：

1. `support_query`：寻找直接支持、条件和定量结果；
2. `boundary_query`：寻找例外、限制、相反结果、适用边界和不可比条件。

不能只检索预设 Claim 的同义表达。`scientific_thesis` 先作为待验证假设，Evidence Package 完成后再由 Claim 状态计算为 `evidence_supported` 或 `partially_supported`，避免确认偏差。

每次命中保留：

- `evidence_key`；
- `paper_id`；
- `chunk_id`、页码和章节；
- `source_channel`；
- `retrieval_pass`；
- `support_level`；
- `claim_eligible`；
- `evidence_level`；
- `assertion_ceiling`；
- `source_lineage_hash`。

### 7.3 必需缺口处理

`required_for_section=true` 的问题未命中时：

1. 自动执行一次定向补检；
2. 检查 Matrix 是否已有对应 fact；
3. 若有事实但 Evidence Package 未命中，修复检索映射；
4. 若只有部分证据，Claim 设为 `partially_supported`；
5. 若无证据，将 Claim 设为 `evidence_missing`；
6. Writing Plan 删除或改写为不超出证据的上位概括；
7. 记录缺口，但不向 Draft 产生伪事实。

语言重写不得被当作证据修复。

## 8. Synthesis State 与 Writing Plan

### 8.1 比较矩阵必须参与写作

每个正文 section 构造小型比较矩阵，字段由该项目的 classification contract 和 scientific claims 决定。例如：

```json
{
  "comparison_axes": [
    "research_object",
    "method_or_catalyst",
    "conditions",
    "outcome_metric",
    "scope_boundary",
    "mechanism_evidence",
    "practical_limit"
  ],
  "paper_rows": [],
  "comparable_axis_count": 4,
  "noncomparable_axes": []
}
```

比较字段必须泛化，具体字段通过 Profile 扩展，不能把某一反应体系写入通用核心。

### 8.2 段落角色

Writing Plan 的段落必须声明角色：

```text
section_frame
anchor_case
method_extension
cross_study_comparison
mechanism_boundary
scope_limitation
section_synthesis_exit
```

普通正文 section 至少包含：

- 1 个 `section_frame`；
- 1 个 `anchor_case` 或方法基线；
- 达到 Blueprint 要求数量的 `cross_study_comparison`；
- 1 个 `section_synthesis_exit`。

只有一篇论文的特殊章节可以没有跨论文比较，但必须说明它与相邻章节的关系。

“比较”不等于强制性能排序。若研究对象、指标、条件或数据基础不一致，一个证据充分的“为什么当前不能直接比较”段落也计入比较性综合，但必须明确不可比轴和支持该判断的论文证据。

### 8.3 Claim 计划

每条计划 Claim 必须包含：

```json
{
  "claim_id": "S02-P03-C01",
  "proposition": "...",
  "epistemic_status": "reported_finding|author_interpretation|review_synthesis",
  "support_status": "supported|partially_supported",
  "paper_ids": ["P001", "P002"],
  "evidence_refs": ["EV-..."],
  "assertion_ceiling": "...",
  "comparison_axis": "conditions",
  "citation_policy": "adjacent"
}
```

没有 `evidence_refs` 的具体事实不得进入正常写作计划。纯结构过渡句可以没有 evidence，但必须标记 `claim_type=transition`。

## 9. 章节生成与保底策略

### 9.1 正常生成

章节模型只读取：

- section thesis；
- Writing Plan；
-允许使用的 Evidence Registry 行；
- citation map；
-写作要求和出版语言约束。

不得读取未选论文、历史章节或旧候选图作为事实来源。

### 9.2 生成后确定性校验

至少检查：

- Claim ID 是否完整实现；
- 具体数字、试剂、材料、对象和结果是否有允许的 evidence anchor；
- 引用是否邻接；
- 是否超出 assertion ceiling；
- 是否泄漏“supplied evidence”“claim eligible”等内部语言；
- 段落角色是否实现；
- 是否达到最低比较覆盖；
- 是否有章节综合出口；
- 是否明显低于目标深度。

### 9.3 保底输出

模型不可用或某一章节反复失败时，继续使用现有安全保底，但必须：

- 只使用已验证 Writing Plan 和 Evidence；
- 设置 `generation_mode=safe_evidence_fallback`；
- 设置科学状态 `provider_fallback` 或 `evidence_safe_but_shallow`；
- 不把 `PASS_WITH_WARNINGS` 显示为正常高质量完成；
- 后续重试只补充失败或保底章节；
- 已成功章节不重复付费生成；
- Final 可以生成工作稿，但质量摘要必须保留保底章节列表。

## 10. Draft 评估与问题回流

### 10.1 问题路由

Draft issue 必须带 `repair_route`：

| 问题类型 | 修复阶段 |
|---|---|
| 缺论文或范围不足 | Discovery |
| Metadata/事实冲突 | Library / Matrix |
| 分类重叠、单篇兜底章节 | Planning |
| `required_claim` 缺证据 | Evidence Package |
| 比较不足、章节没有综合出口 | Synthesis State / Writing Plan |
| 语言、衔接、重复 | Draft paragraph rewrite |
| 图文缺调用或位置错误 | Figures / Final insertion |
| 书目字段错误 | Bibliography repair |

模型重写只能处理语言和已有证据范围内的组织问题。

### 10.2 增量复评

- 单段候选只评估该段及受影响的局部章节指标；
- 保存候选后更新第一部分段落正文、段落分数和总体聚合分数；
- 不因一段变化重复评估全文；
- 只有章节结构、论文身份或引用集合发生改变时，才触发受影响章节级复评；
- 全文质量分数由最新段落/章节状态聚合，保留评估版本和来源 Draft Artifact ID。

### 10.3 Draft 与 Final 状态一致

若 Draft 决策为 `REGENERATE_SECTIONS`：

- 允许生成和下载“当前工作稿”；
- Final 页面明确显示受影响章节和保底状态；
- 不使用“已通过科学检查”等成功文案；
- 用户修改 Draft 后，Final 必须读取该 Draft 当前 Artifact，而不是旧 Section 合并稿。

本规则不新增用户可见质量模式，也不改变现有继续/导出控制逻辑，只修正文案和来源选择。

## 11. 图像与正文脉络

### 11.1 两层关系

保留以下分层：

1. 论文级图片池：用户选择能代表论文总体反应或核心贡献的图片；
2. 正文级插图计划：决定是否插入、插入位置、图号、图注和正文解释。

候选图片不要求在 Stage 5 永久绑定段落。

### 11.2 候选资格

自动入选前必须满足：

- 来源 Artifact 当前有效；
- 有稳定 `paper_id`；
- 未命中明确排除原因；
- 评分达到最低阈值；
- 表格截图、装置照片、页面残片和损坏图注不能因为“本论文最高分”而自动入选；
- 没有合格图时允许该论文无图，不强制选择负分候选。

### 11.3 输出状态

图像状态必须语义准确：

```text
source_original
ai_redrawn
manually_edited
approved_source_original
approved_ai_redrawn
approved_manually_edited
failed
```

`ai_redraw_performed=false` 的图不能标为 `redrawn`。

终稿图像来源分为：

```text
source_paper
multi_paper_synthesis
review_generated
```

- 来源论文图绑定一个规范 `paper_id`；
- 跨研究综合图绑定 supporting paper IDs 和对应 Claim IDs；
- Review Overview Figure 绑定 Blueprint、Writing Plan 和 Final lineage，不要求虚构单一来源论文。

### 11.4 终稿插入

最终插图计划根据正文当前内容选择位置：

- 默认放在第一次实质讨论所属论文之后；
- 正文必须有可见 Figure/Scheme 调用；
- 相邻句解释该图展示的反应、比较或机理信息；
- 图注使用规范论文身份和读者可读说明；
- 论文整体代表图不必声称只证明某一个段落；
- Conclusion 和 References 不作为普通来源图的默认插入位置。

## 12. Final 与出版检查

### 12.1 Final 只组合当前产物

Final 构建必须固定读取：

- 当前 Draft Artifact；
- 当前批准图片及插图计划；
- 当前 Conclusion；
- 当前 Overview Figure（如果存在）；
- 当前 Canonical Bibliography；
- 对应上游 Artifact IDs 和内容哈希。

可选产物不存在时不应阻止 Final 生成；存在时必须使用其当前版本。

### 12.2 语义一致性

确定性检查：

- 正文 `paper_id` 到最终数字引文映射一致；
- DOI、题名、期刊、年份和作者没有明显跨字段冲突；
- 每张插图的来源 paper、图号、正文调用和图注一致；
- Overview 只概括正文已经建立的分类和结论；
- Conclusion 不新增正文没有支撑的比较；
- 内部 Artifact 注释可以保留用于程序读取，但不得显示在 Word/PDF；
- 工作稿状态与 Draft 质量状态一致。

## 13. 代码改造位置

### 13.1 优先复用或修改

| 文件/模块 | 改造重点 |
|---|---|
| `review_writer_core/evidence_queries.py` | 只从 scientific claims 生成必需查询；迁移旧 must-cover 内容 |
| `skills/review-section-blueprint/scripts/init_section_blueprint.py` | 分离 scientific claims 与 writing requirements；生成真实 thesis 和可计算深度 |
| `review_writer_api/domain_services/planning.py` | 分类契约、单篇章节诊断、自动重路由、Blueprint Artifact 发布 |
| `review_writer_core/review_structure.py` | 共享章节主轴、归属、合并和标题清洗纯函数 |
| `review_writer_api/domain_services/sections.py` | Evidence 缺口回流、Synthesis State、Writing Plan 和章节科学状态 |
| `skills/review-section-drafting-figure-picking/scripts/generate_section_drafts.py` | 深度校验、比较覆盖、保底状态、内部语言清洗 |
| `review_writer_api/domain_services/drafts.py` | issue 路由、增量复评、当前 Draft Artifact 一致性 |
| `review_writer_api/domain_services/figures.py` | 候选最低资格、输出状态、论文级图片池 |
| `review_writer_core/figure_insertion.py` | 正文级插图计划、调用和解释闭环 |
| `review_writer_api/domain_services/final.py` | 当前产物组合、Final 状态与语义一致性 |
| `review_writer_core/bibliography_audit.py` | 规范书目跨字段一致性 |

### 13.2 在现有实现上完成闭环

| 现有实现位置 | 当前仍需完成的改进 | 禁止的重复建设 |
|---|---|---|
| repository / artifact service | 写入真实 Artifact dependency、支持语义 freshness、保证原子 current promotion | 不再用目录扫描或新建旁路版本库 |
| `review_fact_readiness.py` | 统一 Claim 缺口、否定性主张资格和章节 readiness 输入 | 不在 Planning、Sections、Draft 各写一套 readiness |
| `classification_axes.py` / `academic_contracts.py` | 增加同级主轴漂移、无边界 catch-all、单篇章节 justification 检查 | 不新增第二个 Classification Contract |
| `publication_caption.py` / `figure_insertion.py` | 增加最低候选资格、来源类型、调用解释和正文位置 | 不在 Final 中临时重新猜图片位置 |
| `draft_bibliography.py` | 保持 `paper_id` 到最终数字编号的一次性映射并验证反向身份 | 不在多个导出脚本分别重排编号 |
| `final_issue_details.py` | 聚合新增语义问题并给出具体目标 | 不新建通用 Quality Service |
| `skills/review-cross-study-synthesis` | 让已有比较规则真正参与 Writing Plan 和章节完成判断 | 不创建第二个综合 Skill |
| Sections `generation_mode` 和验证结果 | 派生 `section_readiness` 并影响 UI、重试和 Final 摘要 | 不增加可独立写入的第二套章节状态 |

API service 负责读取当前 Artifact、调用核心纯函数、编排任务和原子发布；稳定判断放在 `review_writer_core`，Skill 负责具体生成过程，前端只展示和触发操作。

## 14. 兼容与迁移

### 14.1 契约版本

新增或提升以下三个语义版本，版本号进入对应 Artifact 内容和依赖指纹：

```text
PLANNING_ACADEMIC_CONTRACT_VERSION
SECTION_ACADEMIC_CONTRACT_VERSION
FINAL_COMPOSITION_CONTRACT_VERSION
```

现有 `ACADEMIC_SCHEMA_VERSION`、`CLASSIFICATION_CONTRACT_VERSION` 和 Matrix extraction version 继续保留；仅在其负责的语义真正变化时提升。不得为每个 JSON 文件机械增加版本号。版本变化只表示该阶段需要在下次运行时按新语义重建，不允许服务启动时批量静默改写历史 Artifact。

### 14.2 旧 Blueprint

读取旧 `review_claims` 时执行一次内存规范化：

- 科学命题映射到 `scientific_claims`；
- 操作性写作语言映射到 `writing_requirements`；
- 具有明确科学对象、事实角色、论文身份且可由来源证实或证伪的内容映射到 `scientific_claims`；
- 只有操作要求而没有可证伪科学命题的内容映射到 `writing_requirements`；
- 不明确内容映射到 `legacy_unclassified`；
- 规范化结果随新 Blueprint Artifact 保存，不修改旧 Artifact。

关键词如 `compare`、`synthesize` 只能作为辅助信号，不能单独决定 Claim 类型。

### 14.3 旧 Evidence Package

如果 package 没有 Claim 类型来源：

- 继续允许只读展示；
- 重新生成章节时按新契约重建；
- 不把旧 `required_claim_*` 自动继承为必需问题。

### 14.4 旧章节和终稿

- 旧 Artifact 保留历史和下载能力；
- 只有用户重新执行受影响阶段时生成新契约产物；
- 新代码不得静默覆盖人工编辑的当前 Draft；
- 依赖内容真实变化时才传播 stale，单纯增加诊断字段不应让全部下游过期。

### 14.5 Artifact 依赖与 stale 传播

| 当前产物 | 必需上游依赖 | 触发失效的内容变化 |
|---|---|---|
| `planning/selected_outline.json` | 当前 Discovery、Matrix | 论文集合、Topic、用户大纲或分类主轴变化 |
| `blueprint/section_blueprint.json` | 当前 Outline、Matrix | 章节结构、论文主归属、科学事实或契约版本变化 |
| `sections/evidence_package.json` | 当前 Blueprint、Matrix、论文 MinerU Artifact | Scientific Claim、来源版本或证据查询契约变化 |
| `sections/synthesis_state.json` | 当前 Blueprint、Evidence Package | 比较轴、Claim 状态或证据集合变化 |
| `sections/writing_plan.json` | 当前 Blueprint、Evidence Package、Synthesis State | 段落角色、Claim、Evidence 或 assertion ceiling 变化 |
| `sections/section_drafts.json` | 当前 Writing Plan、Evidence Package | 计划正文、证据或章节生成契约变化 |
| Draft 可编辑正文及质量状态 | 当前 Sections、人工编辑基线 | 章节正文或用户保存的正文内容变化 |
| Draft 组合预览 | 当前可编辑正文、当前批准图像计划 | 正文或图片选择/批准变化；只重组预览，不重新评估未变段落 |
| `final/manuscript.md` | 当前 Draft、Canonical Bibliography、当前批准图片、可选 Conclusion/Overview | 任一实际组合内容变化 |

以下变化默认不传播下游失效：

- 任务进度、日志或错误文案；
- 只增加诊断而不改变业务内容；
- 置信度展示格式变化；
- 用户仅打开、查看或下载 Artifact；
- 保留值不变的 Metadata 审核状态刷新。

为实现这一规则，在 Artifact metadata 中记录只包含下游实际消费字段的 `semantic_fingerprint`。Freshness 先比较语义 fingerprint，再比较 Artifact 可用性；不得只因诊断文本或 Artifact ID 改变就让下游失效。现有 Artifact ID 仍用于并发保护、精确 lineage 和历史定位。

发布新 Artifact 时必须原子完成“写文件/对象存储、保存 Artifact 记录、写依赖、切换 current pointer”。中途失败继续保留旧 current Artifact。

## 15. 实施顺序

### P0：修复错误证据查询和章节状态

1. 分离 `scientific_claims` 与 `writing_requirements`；
2. 修复 `required_claim_*` 来源；
3. 增加 Claim 缺口补检、降级和省略逻辑；
4. 增加章节科学状态和保底状态；
5. 补充旧 Blueprint 兼容测试。

完成标准：写作指令不再产生证据缺口；缺失证据不会通过语言重写伪装修复。

### P1：修复大纲脉络和写作深度

1. 实施单一主轴诊断；
2. 自动合并无理由单篇章节；
3. 生成证据派生 thesis；
4. 增加比较矩阵、段落角色和 synthesis exit；
5. 校验目标深度和比较覆盖。

完成标准：同一级标题分类维度一致；正文不再主要由孤立论文摘要组成。

### P2：Draft 回流和图文闭环

1. Draft issue 定向路由；
2. 单段增量复评与总体分数更新；
3. 图像最低候选资格；
4. 区分来源原图、AI 重绘和人工编辑状态；
5. 构建论文级图片池到正文级插图计划。

完成标准：语言问题由重写解决，证据问题返回 Evidence；负分候选不再自动进入正文。

### P3：Final 语义一致性

1. Final 固定读取当前 Draft；
2. 引文身份与参考文献字段一致性；
3. Conclusion/Overview 不新增无支撑信息；
4. Draft 质量状态与工作稿文案一致；
5. DOCX/PDF 隐藏内部元数据并完成图文 QA。

完成标准：Final 的来源版本可解释，正文—引用—图片—书目身份闭合。

## 16. 测试计划

### 16.1 单元测试

- 写作指令不会生成 `required_claim_*`；
- 科学命题可以生成问题级查询；
- 缺证据 Claim 被降级或省略；
- 单篇章节无 justification 时会被建议合并；
- 辅助轴不会成为同级主标题；
- fallback 章节不会标为 `scientific_complete`；
- 负分和明确排除的图片不会自动入选；
- `ai_redraw_performed=false` 不会产生 redrawn 状态；
- 单段复评只更新受影响的局部分数；
- Final 使用当前 Draft Artifact。

### 16.2 集成测试

至少建立以下通用样例：

1. 多论文、同一主轴、两个辅助轴；
2. 只有一篇论文的潜在章节；
3. Blueprint 中混入写作指令；
4. 必需科学 Claim 检索不到；
5. 模型中途不可用并触发保底；
6. 用户编辑 Draft 后直接生成 Final；
7. 用户更换候选图后重新构建 Draft/Final；
8. 图像没有合格候选；
9. DOI 和期刊字段相互冲突；
10. 非化学主题，确保没有使用化学硬编码。

### 16.3 回归项目

使用由 `zzz` 问题结构提炼的脱敏最小 fixture 作为回归数据，不提交真实 PDF、运行时 Artifact、用户 ID 或 API 信息。至少验证：

- 原写作指令不再形成 `required_claim_01`；
- 醛基 ATA 论文不会因机理辅助轴被拆成无理由单篇顶级章节；
- Au 相关工作不会进入无分类意义的兜底标题；
- 保底章节列表在 Draft/Final 可见；
- 计划深度与实际深度差距被正确诊断；
- 被排除的表格截图或实验照片不会因“论文最高分”自动入选；
- Final 使用最新人工编辑 Draft。

## 17. 验收指标

### 17.1 证据链

- 100% 具体事实 Claim 具有 `paper_id + evidence_ref`；
- 0 条写作指令进入必需证据查询；
- 0 条 `evidence_missing` Claim 进入正常正文；
- 所有引用可反向解析到规范论文身份。

### 17.2 脉络和综合

- 正文同级章节主轴一致；
- 无理由单篇顶级章节为 0；
- 正文章节均有明确 scientific thesis；
- 达到 Blueprint 规定的最低比较性综合要求；证据充分的不可比性说明可以计入；
- 每个正文 section 有 synthesis exit，特殊单篇章节除外但需 justification。

### 17.3 章节质量

- 正常章节和保底章节状态明确区分；
- 实际深度不足时有具体原因，不仅显示“未达字数”；
- 内部审计语言不进入出版正文；
- Draft 的问题类型能够路由到正确阶段。

### 17.4 图像和终稿

- 无合格候选时允许无图，不使用负分图；
- 图像来源状态与实际处理方式一致；
- 每张终稿图片都有 `source_paper`、`multi_paper_synthesis` 或 `review_generated` provenance，并有可见调用、解释和图注；
- Final、DOCX、PDF 使用同一当前 Draft 和图片版本；
- 工作稿不会被描述为已通过全部科学质量检查。

## 18. 风险和控制

| 风险 | 控制方式 |
|---|---|
| 新契约使旧项目全部过期 | 只在重新运行受影响阶段时迁移；诊断字段变化不传播 stale |
| 分类自动合并错误 | 保留重路由报告和人工大纲编辑，不静默删除论文 |
| 目标深度导致模型冗长 | 先判断证据/比较缺口，不为凑字数重复内容 |
| 保底状态降低用户信心 | 明确说明“安全但较浅”，并支持只重试这些章节 |
| 过多警告增加人工成本 | 自动修复可确定问题，只把真实科学歧义交给用户 |
| 规则过拟合化学主题 | 通用核心只使用轴、事实角色和证据状态；学科术语放入可选 Profile |
| 代码重复 | 稳定判断放入 `review_writer_core` 纯函数，阶段服务只负责编排和发布 Artifact |

## 19. 完成定义

本方案完成不是指增加了字段或页面文案，而是同时满足：

1. 新旧 Blueprint 均能安全规范化；
2. 科学 Claim 和写作要求在数据、检索和 UI 上完全分离；
3. 证据缺口能够补检、降级或省略，不再依赖语言重写；
4. 大纲、Blueprint、章节比较和总览图使用一致主轴；
5. 章节保底状态、深度和比较覆盖真实可见；
6. Draft 问题能返回正确上游阶段；
7. 图像与论文身份、正文解释和终稿位置闭环；
8. Final 使用全部当前 Artifact，并保留准确质量语义；
9. 单元、集成和非化学泛化测试通过；
10. 不破坏现有人工编辑、导出、版本和项目隔离功能。
