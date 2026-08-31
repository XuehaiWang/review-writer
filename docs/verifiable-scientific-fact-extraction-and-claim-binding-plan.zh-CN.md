# Review Writer 可核验科学事实提取与正文主张精确绑定优化方案

## 1. 文档定位

本文只解决一条窄而关键的证据链：

```text
MinerU 解析结果
  → 可定位的原文证据
  → 可核验的结构化科学事实
  → Claim 与证据精确绑定
  → 受证据上限约束的正文句子
  → 段落级核验与定向修复
```

本文不是新的端到端架构，不新增用户阶段，也不建立第二套 Matrix、Evidence Package、Quality 或稿件状态。它是以下现有方案在“事实提取—Claim 绑定”层的实施细化：

- `end-to-end-evidence-writing-optimization-plan.zh-CN.md`；
- `evidence-writing-narrative-implementation-plan.zh-CN.md`；
- `evidence-chain-accuracy-general-optimization-plan.zh-CN.md`；
- `paper-agent-retrieval-rag-improvement-plan.zh-CN.md`；
- `stage-02-04-agent-evidence-classification-optimization-plan.zh-CN.md`。

发生重复时，以本文定义的职责边界和数据契约为该层实现依据；现有 Artifact、当前版本指针、内容哈希和 lineage 仍是唯一业务真相。

## 2. 当前问题与根因

### 2.1 解析内容数量不是唯一问题

以当前 `last` 项目作为回归样本：

- Evidence Registry 已包含 205 条证据；
- 39 个段落中 22 个来源核验通过；
- 8 个部分支持，9 个需要人工确认；
- 未通过段落通常已经绑定 1–7 条证据。

因此，主要断点不是“没有 MinerU 内容”，而是：

1. 召回片段与具体科学主张不完全对应；
2. 片段存在，但没有转化为稳定、可定位的科学事实；
3. 一句话合并了多个事实，而证据只覆盖其中一部分；
4. 否定性主张被要求用局部片段证明全文没有报告；
5. 图像调用语句被错误送入论文事实核验；
6. 多论文综合没有拆成逐论文事实和综述层推断；
7. Draft 修复可能重复补检相同问题，却没有稳定关闭根因。

### 2.2 当前“提取”仍偏向主题片段

现有全文检索能够返回正文、表格、图注和相邻块，但主题相关不等于事实完整。写作真正需要的是：

- 反应物、底物类别与产物；
- 催化剂、促进剂、配体、胺和添加剂的报告角色；
- 温度、时间、溶剂、配比、催化量；
- yield、ee、dr、de、规模和底物数；
- 适用范围、失败实例与作者声明的限制；
- 机理实验、计算、观察中间体和作者提出的机理；
- 每项事实所在论文、页码、原文块、表格、图注或 SI 位置。

如果只保存“大段相关文本”，模型仍可能把相邻事实拼接成原文没有直接支持的句子。

### 2.3 “没有证据”包含三种不同情况

系统必须区分：

```text
extraction_miss
  论文中存在，当前没有抽取或召回到

binding_mismatch
  已有证据，但与当前 Claim 的对象、条件或范围不完全一致

true_evidence_gap
  已检查允许范围，仍没有足够证据支持该 Claim
```

三者不能都显示为“章节证据包问题”，也不能都通过反复重写解决。

### 2.4 现有实现不是空白，后续只能增量升级

当前代码已经具备以下基础：

- Matrix enrich 能生成确定性 `fact_id`；
- Agent 被要求返回连续、可在候选内容中验证的 `support_excerpt`；
- `evidence_refs` 已保存 `evidence_key`、`chunk_id`、页码和 source lineage；
- `epistemic_status` 已区分直接来源报告、作者解释和摘要级报告；
- `assertion_ceiling` / `evidence_ceiling` 已限制摘要、图表和作者解释可支持的表达；
- `review_fact_readiness.py` 已根据证据和 support level 判断事实是否可用于比较；
- `claim_contracts.py` 已识别 fact ID、Evidence Ref 和 Claim 所需事实角色；
- Sections 和 Draft 已消费 Evidence Registry、Claim realization 和来源核验结果。

因此，本方案不是重新编写一套事实提取器，而是补齐以下缺口：

1. 提高事实角色覆盖和按 Claim 补检能力；
2. 把现有 Evidence Ref 统一成可复用的 Source Span 视图；
3. 增加 Claim 主体、谓词、值、限定条件和论文身份的 coverage；
4. 分离图像调用、强否定和真正科学事实；
5. 把 Draft 新事实安全提升回 Matrix，并原子更新依赖产物；
6. 建立用户隔离缓存、问题指纹和局部重算。

任何实现若重新定义 fact ID、Evidence Ref、epistemic status 或 assertion ceiling，均视为重复建设，应在代码审查中拒绝。

## 3. 优化目标与非目标

### 3.1 优化目标

1. 从 MinerU 内容中提取可定位、可复核的科学事实；
2. 每项进入写作的事实必须带稳定来源地址；
3. 每个科学 Claim 必须声明其事实、证据和论文身份；
4. 正文句子的断言强度不能超过证据上限；
5. 缺失证据时先定向补检，再收窄或删除 Claim；
6. 图像调用、引用格式和科学事实分别校验；
7. 自动解决大部分证据问题，只把真正的科学冲突交给用户；
8. 对未改变的证据和 Claim 不重复调用模型、不重复打开问题；
9. 方案可用于化学以外的综述主题，不写死 ATA、allene 或催化体系。

### 3.2 非目标

- 不要求用户逐条审核事实卡；
- 不让 Agent 自由改写 MinerU 原文；
- 不用 Agent 输出替代原始 PDF、Markdown、表格或图注；
- 不建立独立的 Fact Service、Claim Service 或 Evidence 微服务；
- 不让 Agent 自行修改论文身份、DOI 或书目信息；
- 不把所有低置信度事实都变成流程阻断；
- 不静默改变一级分类、大纲主轴或论文正文归属；
- 不通过联网搜索替代已上传论文的本地证据核读；
- 不因一次模型失败使整批事实提取或章节写作全部失败。

## 4. 总体架构与职责边界

### 4.1 目标流程

```text
PDF / MinerU immutable version
  ↓
Source Span 标准化与全文索引
  ↓
确定性候选发现
  ↓
Agent 受约束事实提取
  ↓
原文存在性、数值、身份和范围校验
  ↓
Matrix scientific_facts
  ↓
问题级 Evidence Package
  ↓
Claim—Fact—Evidence 绑定
  ↓
Writing Plan / Paragraph Realization
  ↓
Source Check
  ↓
定向补检、Claim 降级或局部重写
```

### 4.2 确定性程序负责

- 读取当前 MinerU Artifact；
- 保留页码、块 ID、顺序、表格、图注和图片关系；
- 规范化现有 `evidence_key`、`chunk_id`、页码和 `source_lineage_hash`，并校验内容哈希；
- 词法、结构、数字和可选向量召回；
- 校验 Agent 返回的原文是否真实存在；
- 校验 paper ID、数值、单位和化学式；
- 发布 Matrix、Evidence Package、Writing Plan 和 Quality Artifact；
- 处理缓存、版本依赖、重试、部分成功和计费；
- 执行图像调用、引用和格式检查；
- 根据问题指纹防止重复循环。

### 4.3 Agent 负责

Agent 只作为现有科学任务中的三种模式运行，不拆成独立服务：

```text
fact_extraction
claim_evidence_matching
evidence_conflict_resolution
```

- `fact_extraction`：把有限的原文候选转换为结构化事实；
- `claim_evidence_matching`：判断 Claim 是否被指定事实和证据完整支持；
- `evidence_conflict_resolution`：处理表格、正文、SI 或多论文之间的语义冲突。

三种模式均通过现有模型网关、Worker、任务状态、重试和计费链路执行。

### 4.4 Agent 不负责

- 凭记忆补充论文内容；
- 生成不存在的页码、原文或实验数据；
- 判断文件是否真实存在；
- 修改 Artifact 当前指针；
- 将检索失败解释为论文未报告；
- 将多个论文的事实合并成一个来源事实；
- 自动接受会改变分类主轴或科学结论的候选。

## 5. 统一数据契约

### 5.1 Source Span

Source Span 是对现有 Evidence Ref 的标准化读取视图，不是新的持久身份，也不新增平行原文库。它继续使用已经存在的 `evidence_key`、`chunk_id`、页码、`source_lineage_hash` 和 MinerU Artifact ID；如代码需要组合键，只能按这些字段确定性计算，不得再维护一套可独立变化的 `source_span_id`。

标准化视图如下：

```json
{
  "paper_id": "P001",
  "mineru_artifact_id": "...",
  "source_content_sha256": "...",
  "evidence_key": "sha256:...",
  "chunk_id": "chunk-102",
  "source_block_ids": ["block-102", "block-103"],
  "page_start": 6,
  "page_end": 6,
  "source_type": "body|table|caption|supplementary|abstract",
  "section_heading": "Substrate scope",
  "verbatim_text": "原文片段",
  "verbatim_text_sha256": "...",
  "context_before": "有限相邻上下文",
  "context_after": "有限相邻上下文"
}
```

规则：

- `verbatim_text` 必须能够从指定 Artifact 中确定性复现；
- `evidence_key`、`chunk_id` 和 `source_lineage_hash` 继续是代码使用的来源身份；
- Source Span 只负责统一读取这些字段，不单独发布 Artifact 或数据库记录；
- 表格单元格必须同时保存行列标题；
- 图注只能支持图中明确表达的内容；
- abstract 默认不能证明全文中的具体条件和完整 scope；
- 来源位置不明的文本只能作为 `context_only`，不得直接支持定量主张。

### 5.2 Scientific Fact

Scientific Fact 继续写入现有 Matrix 行的 `scientific_facts`，不新增 `paper_fact_cards.json`：

```json
{
  "fact_id": "FACT-P001-...",
  "paper_id": "P001",
  "field_id": "reaction_conditions.temperature",
  "fact_type": "reported_result|condition|scope|limitation|mechanism_evidence|author_interpretation",
  "subject": "reported transformation or substrate set",
  "predicate": "was conducted at",
  "value": "80 °C",
  "normalized_value": 80,
  "unit": "°C",
  "qualifiers": {
    "substrate_scope": ["aromatic aldehydes"],
    "condition_scope": "optimized conditions"
  },
  "epistemic_status": "direct_source_report|source_author_interpretation|abstract_level_report",
  "confidence": 0.94,
  "evidence_refs": [
    {
      "evidence_key": "sha256:...",
      "chunk_id": "chunk-102",
      "source_lineage_hash": "..."
    }
  ],
  "assertion_ceiling": "study_specific_reported_result",
  "extraction": {
    "mode": "agent_verified",
    "schema_version": "scientific-fact/2",
    "prompt_version": "fact-extraction/1",
    "model": "configured-scientific-model"
  }
}
```

通用字段由核心代码定义；主题 Profile 只能增加字段，不得改变来源地址、事实身份和证据上限语义。

现有 `epistemic_status` 枚举保持不变，避免形成第二套兼容分支。更细的“实验观察、方法条件、范围、机理证据、作者推断”等差异由 `fact_type` 表达。已有确定性 `fact_id` 生成方式继续复用，新 Schema 只增加字段，不重新分配旧事实身份。

### 5.3 Claim—Evidence Binding

Writing Plan 中现有 Claim 增强为：

```json
{
  "claim_id": "S02-p3-C01",
  "paragraph_id": "S02-p3",
  "claim_kind": "reported_result|comparison|mechanism_interpretation|limitation|transition",
  "proposition": "待写入正文的科学主张",
  "paper_ids": ["P001"],
  "fact_ids": ["FACT-P001-..."],
  "evidence_refs": [
    {
      "paper_id": "P001",
      "evidence_key": "sha256:...",
      "support_role": "direct|qualifier|boundary|counterexample"
    }
  ],
  "support_status": "supported|partially_supported|conflicted|missing",
  "coverage": {
    "subject": true,
    "predicate": true,
    "value": true,
    "qualifiers": true,
    "paper_identity": true
  },
  "allowed_assertion": "证据允许的最强表达",
  "assertion_ceiling": "study_specific_reported_result",
  "repair_policy": "retrieve|narrow|omit|human_scientific_choice"
}
```

一句话包含多个独立事实时，必须拆成多个 Claim 或明确列出多个 fact ID。不能使用一条模糊 Evidence Ref 为整句话兜底。

### 5.4 Source Check Result

Quality 中现有 `source_check.entries` 继续作为段落核验结果，增加明确根因：

```json
{
  "paragraph_id": "S02-p3",
  "claim_id": "S02-p3-C01",
  "source_check_status": "verified|partially_supported|needs_human_review",
  "evidence_problem_type": "extraction_miss|binding_mismatch|true_evidence_gap|conflict|none",
  "failed_coverage_fields": ["qualifiers"],
  "repair_route": "targeted_retrieval|claim_narrowing|paragraph_rewrite|human_scientific_choice",
  "issue_fingerprint": "..."
}
```

## 6. 事实提取流程

### 6.1 第一步：确定性候选发现

对每篇进入 Matrix 的论文，按事实角色构造候选，而不是只使用一个主题查询：

```text
reaction_identity
substrates_and_products
catalyst_and_component_roles
conditions
quantitative_results
scope_and_failures
scale_and_application
mechanism_evidence
author_interpretation
limitations
```

采用“基础事实一次提取＋Claim 缺口按需补充”的混合策略，不对每篇论文的每个事实角色分别执行一次模型调用：

- Matrix 阶段把反应/研究身份、关键条件、主要结果、范围/限制和机理证据候选合并为一到少量有界提取批次；
- 当前主题不会使用的事实角色不做穷举式 Agent 提取；
- Blueprint、Writing Plan 或 Draft 出现具体 Claim 缺口时，再对指定 `paper_id + fact_role + claim` 做定向补充；
- 已验证基础事实直接复用，不因章节或段落重试而重新提取；
- 单篇论文调用预算、输入字符数和补检轮次由任务配置统一限制。

召回顺序：

1. 标题、摘要用于定位研究主题；
2. Experimental、Optimization、Scope、Mechanism、Conclusion 等章节；
3. 表格行列、图注和相邻正文；
4. SI 内容（存在且已解析时）；
5. 全文词法召回；
6. 可选语义召回；
7. 仍不足时才进入 Agent 冲突或缺口判断。

召回必须限定当前 `paper_id`，不能用另一篇论文的事实补齐该论文的事实卡。

### 6.2 第二步：Agent 结构化提取

Agent 每次只接收：

- 一篇论文身份；
- 一个或少量事实角色；
- 有界 Source Span；
- JSON Schema；
- 禁止补全和推测的约束。

不得把整库全文一次性交给模型，也不得要求模型自由撰写论文总结后再反向拆事实。

模型必须区分：

- 原文明确报告；
- 实验观察；
- 作者提出或推测；
- 综述作者可做的受限比较；
- 当前片段无法判断。

### 6.3 第三步：程序硬校验

每个事实至少经过：

1. paper ID 与 Source Span 一致；
2. 原文哈希可复现；
3. 数字和单位在原文中可定位；
4. 化学式中的上下标、价态、正负号和计量符号可追溯；
5. 表格值的行列标题同时存在；
6. Agent 不得扩大主语、底物范围和条件范围；
7. `epistemic_status` 与原文语言一致；
8. 事实不能只由系统生成的图像调用语句支持。

硬校验失败时：

- 不发布该事实；
- 记录结构化 rejection reason；
- 允许一次缩小上下文后的重试；
- 再失败则保留原始 Evidence Hit，不把它升级为事实。

### 6.4 第四步：发布到现有 Matrix

`matrix.enrich` 将通过校验的事实写入当前 Matrix 的论文行。每篇论文同时记录：

- 事实提取版本；
- 当前 MinerU Artifact ID 与哈希；
- 完成和缺失的事实角色；
- rejected fact 数量与原因；
- 是否缺少 SI；
- 是否使用安全保底。

Matrix 更新按现有 Artifact 版本和依赖规则发布，不直接覆盖旧版本。

## 7. Claim 与正文精确绑定流程

### 7.1 Claim 先于句子生成

章节写作必须保持：

```text
科学问题
  → Claim Plan
  → Fact / Evidence Binding
  → Assertion Ceiling
  → Paragraph Realization
```

禁止先生成完整段落，再让模型从证据中寻找看起来相似的引用。

### 7.2 Claim 绑定判定

`claim_evidence_matching` 只判断以下问题：

- 主体是否一致；
- 反应、方法或对象是否一致；
- 数值、条件和限定范围是否一致；
- 证据是观察、报告、提出还是推断；
- 单篇证据能否支持单篇结论；
- 多篇综合是否包含每篇必要证据；
- 是否存在反例或冲突。

Agent 只给语义判断，最终状态由确定性程序根据 coverage 字段计算。

### 7.3 写作模型的输入边界

写作模型只能看到：

- 当前段落职责；
- 已验证 Claim；
- 对应 fact ID 和 Evidence Ref；
- allowed assertion；
- assertion ceiling；
- 必须保留的引用和图像元数据；
- 禁止生成的缺失事实。

`missing` Claim 不得进入正文；`partially_supported` Claim 只能使用限定性表达。

### 7.4 正文完成后的反向核验

生成段落后，将每个事实句映射回 Claim：

```text
sentence
  → claim_id
  → fact_ids
  → evidence_keys
  → chunk_id / page / source_lineage_hash
```

出现以下情况时只重写相关句或段：

- 新增了未计划科学主张；
- 数字、化学实体或论文身份改变；
- 断言强度超过 ceiling；
- 多个 Claim 被合并后丢失限定条件；
- 引用组与事实来源不一致。

## 8. 特殊证据策略

### 8.1 否定性主张

“论文没有报告”“研究没有建立”不能由若干局部片段直接证明；即使全文和 SI 已完成索引，检索没有命中也不能单独证明强否定。

只有满足以下条件之一才允许强否定：

- 原文明确声明没有观察、没有测量、没有效果或没有形成相关结论；
- 这是封闭、确定性结构中的缺失，例如给定完整表格、明确实验清单或由字段定义允许作缺失判断的记录。

“全文已索引＋覆盖式检索无命中”只能提高补检完成度，不能把无命中升级为论文级强否定。

否则自动改写为：

> 当前检索到的证据没有直接建立……

或：

> 在本文所核验的来源片段中，尚不能确定……

这类收窄可自动执行，不要求用户证明整篇论文绝对没有相关内容。

### 8.2 图像调用语句

以下句子不进入论文事实 Source Check：

- “Figure 1 summarizes …”；
- “Scheme 2 depicts …”；
- 图号、放置位置和可见调用。

它们交由现有 Figure Insertion、caption/callout 和图文绑定检查处理。图像内容中的科学解释仍需绑定对应论文事实。

### 8.3 跨论文比较

跨论文结论必须拆为：

1. 论文 A 的独立事实；
2. 论文 B 的独立事实；
3. 可比较条件是否一致；
4. 综述层比较结论及其边界。

没有直接 head-to-head experiment 时，正文必须使用“cross-study comparison”“under the reported conditions”等限定，不能写成受控优劣结论。

### 8.4 机理主张

机理 Claim 必须区分：

- 实验观察；
- 同位素、动力学、捕获或控制实验；
- 计算结果；
- 提出中间体；
- 作者建议的循环；
- 综述作者推断。

仅有 proposed scheme 时，assertion ceiling 不得高于 `author_proposed_mechanism`。

### 8.5 数字和化学实体

数字、单位、温度、催化量、yield、ee、dr、化学价态和试剂身份使用程序优先核验。Agent 只处理上下文含义，不负责纠正原文中不存在的值。

## 9. Draft 定向修复与防循环

### 9.1 自动修复顺序

```text
识别具体 Claim
  → 判断 extraction_miss / binding_mismatch / true_evidence_gap
  → 定向检索当前 paper 的全文、表格、图注、相邻块和 SI
  → 新事实通过硬校验后加入当前 Evidence Package
  → 重新绑定 Claim
  → 仍不足则收窄或省略 Claim
  → 只重写受影响段落
  → 只复评受影响段落
  → 增量更新总体评分
```

### 9.2 Artifact 更新边界

- 正常 Matrix enrich：事实写入 `matrix/literature_matrix.json`；
- 章节生成：从 Matrix 选取事实进入 `sections/evidence_package.json`；
- Draft 定向补证发现通过硬校验的新事实后，必须增量发布新的 Matrix 版本，避免事实只存在于 Evidence Package 并在后续章节重建时丢失；
- 同一次修复使用一个原子发布单元：先准备增量 Matrix、受影响 Evidence Package、候选 Draft、Quality 和 repair history，全部校验成功后再一起切换当前指针；任一产物失败时均不切换；
- 增量 Matrix 只能增加或完善当前论文的已验证事实，不得借 Draft 修复静默改变分类主轴、论文身份、论文主章节归属或已有事实的科学含义；
- 依赖失效按 fact ID、paper ID、section ID 和 paragraph ID 计算，只重建受影响章节和段落；其他章节继续引用其原有有效事实；
- 用户拒绝修复候选时，Matrix、Evidence Package 和 Draft 当前指针全部保持不变；
- 如确需改变论文分配或章节 thesis，生成 Planning/Sections 候选并向用户说明影响范围。

这一原子链解决“Matrix 是事实真相，但 Draft 新事实只存 Evidence Package”的矛盾。Evidence Package 仍是面向章节问题的证据视图，不成为第二套事实真相。

### 9.3 问题状态

每个根因使用稳定 `issue_fingerprint`，状态为：

```text
auto_repairable
repairing
resolved
needs_source
needs_scientific_choice
failed_twice
```

相同输入 Artifact、Claim 和 Evidence 哈希不允许重新打开 `resolved` 问题。自动尝试最多两次：

- 第一次：定向补检和重新绑定；
- 第二次：保守收窄或省略；
- 仍失败：转为明确的单项科学选择，不再循环调用模型。

### 9.4 人工确认最小化

只有以下情况交给用户：

- 两个可靠来源给出无法自动调和的冲突；
- 化学实体或机理身份存在真正歧义；
- 当前论文全文/SI 不可用，而主张又不能安全删除；
- 修复会改变一级分类、章节核心 thesis 或论文主归属。

界面不能只显示内部错误码，应提供：问题句、对应论文、已核验原文、系统建议和有限操作选项。

## 10. 缓存、版本和失败降级

### 10.1 缓存键

事实提取缓存至少包含：

```text
paper_id
MinerU artifact id
source content hash
fact schema version
prompt version
model identity
taxonomy profile version（仅领域扩展字段）
```

同一事实角色和相同输入不得重复收费。PDF、MinerU 版本、Schema 或提示版本变化时才重新提取对应部分。

缓存按用户隔离：

- 同一用户在不同项目中使用内容哈希相同的 PDF 时，可以复用已经通过硬校验的事实提取缓存；
- 不同用户之间默认不共享事实缓存，即使 PDF 内容哈希相同；
- 缓存只是可复验的计算结果，不是业务真相；新项目仍需把缓存事实重新核对当前 MinerU lineage、主题所需事实角色和 Schema 后写入该项目 Matrix；
- 缓存不得持有可独立修改的当前指针，也不得绕过项目 Artifact 依赖；
- 可优先复用现有 Library Artifact/索引的用户级隔离能力；只有现有存储无法表达版本键时才增加专用缓存表。

### 10.2 部分成功

一篇论文某个事实角色失败，不得使整篇论文或整批 Matrix 失败：

- 已校验事实正常发布；
- 失败角色记录 `missing_or_unresolved`；
- 后续 Claim 不能使用失败角色；
- 任务进度按论文和事实角色实时更新；
- 重试只恢复失败项。

### 10.3 模型不可用

模型不可用时：

- 保留确定性召回结果为 Evidence Hit；
- 不把未结构化片段升级为高置信事实；
- 允许只使用已有已验证事实生成安全但较浅的章节；
- 页面明确显示“事实提取未完成”，但不丢失已完成结果；
- 后续重试只运行缺失的 Agent 任务。

## 11. 前端交互

### 11.1 不新增用户阶段

用户仍按现有 Library、Discovery、Planning、Sections、Images、Draft、Final 流程操作。事实提取与绑定作为现有任务的内部步骤展示进度。

### 11.2 Matrix/Planning 可见信息

每篇论文显示简洁状态：

- 已提取事实角色数；
- 缺失或冲突角色；
- 是否缺少全文/SI；
- 是否使用安全保底；
- “查看证据”可展开原文位置，但不要求逐项确认。

### 11.3 Draft 修复结果

批量优化后显示：

- 自动补充多少事实和证据；
- 收窄或删除多少不受支持 Claim；
- 实际重写哪些段落；
- 哪些问题已关闭；
- 剩余问题需要补全文还是科学判断；
- 新旧段落与评分对比。

“修复位置：章节证据包”应替换为可执行说明，例如：

> S04-p3 的催化组分作用缺少直接原文支持。系统将重新检索 P001 的 Mechanism、Optimization 和 SI；若仍缺失，将把该句收窄为当前证据边界。

## 12. 代码落点与复用策略

### 12.1 共享核心纯函数

优先扩展现有模块，不复制逻辑：

- `review_writer_core/evidence_queries.py`：事实角色查询、Claim 定向查询；
- `review_writer_core/evidence_integrity.py`：Source Span、原文、数字和身份硬校验；
- `review_writer_core/review_fact_readiness.py`：事实可用性、否定性主张资格；
- `review_writer_core/claim_contracts.py`：Claim coverage、support status、assertion ceiling；
- `review_writer_core/draft_issue_routing.py`：根因分类、修复路由和稳定指纹。

若多个脚本需要相同 Source Span 或 Fact 校验，必须放入核心纯函数，不得分别在 Matrix、Sections 和 Draft 脚本中复制。

### 12.2 Matrix

- `skills/review-literature-matrix-outline/scripts/enrich_matrix_facts.py`：受约束事实提取、验证和 Matrix 发布输入；
- 保留现有 `scientific_facts`，只升级 Schema，不新增平行事实文件；
- 事实抽取模板和领域扩展由 taxonomy profile 提供，通用验证在核心库。

### 12.3 Sections

- `skills/review-section-drafting-figure-picking/scripts/generate_section_drafts.py`：Claim Plan、Fact/Evidence 绑定、受限段落实现；
- `review_writer_api/domain_services/sections.py`：当前 Artifact、任务进度、部分成功与恢复；
- Evidence Package 继续是章节和 Draft 使用的证据视图。

### 12.4 Draft

- `skills/review-first-draft-feedback-loop/scripts/feedback_loop.py`：Claim 级来源核验、限定表达和局部复评；
- `review_writer_api/domain_services/drafts.py`：定向补证、候选发布、根因关闭和增量评分；
- 图像调用必须在 Figure 逻辑中处理，不进入论文科学事实核验。

### 12.5 模型网关

- 复用当前 Gateway、Worker、provider credential、retry 和 billing；
- 只增加任务模式和 Schema，不新增微服务；
- `fact_extraction` 按论文/事实角色分片；
- `claim_evidence_matching` 按段落/Claim 分片；
- 冲突裁决仅在确定性规则无法决策时调用。

## 13. 实施顺序

### P0：消除当前伪证据问题

1. 图像调用句退出论文 Source Check；
2. 强否定仅接受原文明示或封闭结构的确定性缺失；其他无命中情况自动使用来源范围限定表达；
3. Source Check 输出三类根因；
4. Claim coverage 检查主体、谓词、值、限定条件和论文身份；
5. 相同问题指纹在输入未改变时不得重新打开；
6. Draft 自动修复先补检，再收窄，再局部重写；
7. 增加 `last` 的通用化回归样例，但测试不得依赖其具体主题词。

### P1：结构化事实提取

1. 将现有 `scientific_facts` 向后兼容升级为 `scientific-fact/2`，并把 Source Span 实现为现有 Evidence Ref 的标准化视图；
2. 在 Matrix enrich 中实现混合式基础事实召回，合并事实角色批次而不是每角色一次模型调用；
3. 增加 `fact_extraction` Agent 模式；
4. 实现原文、数值、单位、化学实体和范围校验；
5. 将通过校验的事实写入现有 `scientific_facts`；
6. 增加同一用户内的内容哈希缓存、部分成功和失败项恢复；
7. 为旧 Matrix 提供按论文增量升级，不要求重建全部项目。

### P2：Claim 精确绑定与冲突裁决

1. 增加 `claim_evidence_matching` 模式；
2. Writing Plan Claim 保存 coverage 和 allowed assertion；
3. 章节完成后执行句子到 Claim 的反向核验；
4. 跨论文比较拆分逐论文事实和综述推断；
5. 仅对冲突和语义歧义调用 `evidence_conflict_resolution`；
6. Draft 缺口按 Claim 触发定向事实补充，不重复运行整篇基础提取；
7. 通过校验的新事实以原子发布方式同步更新增量 Matrix、受影响 Evidence Package、Draft 和 Quality；
8. Draft 批量优化展示事实、Claim、段落和评分的完整修复结果。

## 14. 测试方案

### 14.1 单元测试

- Source Span 可从指定 MinerU Artifact 复现；
- Agent 虚构原文、页码或数字时被拒绝；
- 表格值缺少行列标题时不能成为定量事实；
- abstract 证据不能支持完整实验条件；
- paper ID 不一致时绑定失败；
- 部分覆盖不能升级为 fully supported；
- 图像调用句不进入科学 Source Check；
- 没有原文明示或封闭结构依据时，强否定自动收窄；
- 未改变输入的问题不重新打开；
- 单项失败不取消同批其他论文。

### 14.2 集成测试

```text
MinerU content
  → Matrix fact extraction
  → Evidence Package
  → Claim Plan
  → Section paragraph
  → Draft source check
  → targeted repair
```

验证每一步的 Artifact ID、内容哈希、fact ID、evidence key、claim ID 和 paragraph ID 可追溯。

### 14.3 泛化回归

至少覆盖：

- 化学反应方法学；
- 材料或器件性能；
- 生物医学实验；
- 计算方法或模型比较。

测试 Schema 的通用事实层，主题专用字段只能来自 Profile 扩展。

### 14.4 真实项目回归

`last` 仅作为一个真实回归样本，重点验证：

- 已有大量 Evidence Hit 时不误判为完全缺证据；
- 图像调用不再制造伪证据问题；
- 否定性主张能够安全收窄；
- 条件、催化组分和范围事实可定向补检；
- 批量修复后问题数真实下降且不反复打开。

## 15. 验收标准

### 15.1 事实质量

- 进入 Matrix 的事实 100% 具有 paper ID 和可复现 Source Span；
- 数值型事实 100% 通过原文值和单位校验；
- Agent 无法定位原文的输出 100% 被拒绝或降为 Evidence Hit；
- 事实提取 Precision 在人工金标准集上达到 95% 以上；
- 通用事实角色 Recall 在人工金标准集上达到 90% 以上。

### 15.2 Claim 绑定

- 正文科学事实句均可追溯到 claim ID、fact ID 和 evidence key；
- paper ID、数值或限定条件不一致时不得标记 verified；
- 跨论文比较包含每篇参与论文的独立事实；
- 图像调用导致的来源核验问题为 0；
- 没有原文明示或封闭结构依据的强否定主张为 0。

### 15.3 修复闭环

- 相同输入下已关闭根因的重新打开率为 0；
- 可自动解决的 extraction miss 和 binding mismatch 不交给用户；
- 批量任务部分失败后可只重试失败论文/事实角色；
- Draft 修复仅失效和重写受影响目标；
- 用户只处理真正的来源缺失、科学冲突或结构性选择。

### 15.4 性能与成本

- 同一用户内相同 PDF、MinerU 版本和事实 Schema 不重复提取；
- Agent 输入只包含单篇论文的有界候选片段；
- 冲突裁决调用比例受监控，不能成为默认路径；
- 章节或 Draft 重试不重复运行已完成的事实提取任务。

## 16. 风险与控制

| 风险 | 控制方式 |
|---|---|
| Agent 生成看似合理但不存在的事实 | 原文存在性和内容哈希硬校验 |
| 事实 Schema 过于化学专用 | 核心通用字段＋taxonomy profile 扩展 |
| 事实过细导致成本增加 | 按 Matrix 论文、事实角色和 Claim 需要增量提取 |
| 事实过粗仍无法支持句子 | coverage 检查主体、谓词、值和限定条件 |
| Draft 补证修改上游导致大面积过期 | 原子发布增量 Matrix、受影响 Evidence Package、Draft 与 Quality，并按事实和段落做局部失效 |
| 多 Agent 增加部署复杂度 | 仅增加现有网关任务模式，不增加服务 |
| 否定性主张反复人工确认 | 强否定仅接受原文明示或封闭结构缺失，其他情况自动使用来源范围限定表达 |
| 同一问题循环出现 | 稳定 issue fingerprint、两次自动尝试上限和 Claim disposition 记忆 |
| 模型或供应商不稳定 | 分片、部分成功、断点恢复和确定性保底 |

## 17. 最终决策

本层优化采用“确定性来源地址＋受约束 Agent 语义分析＋程序硬校验”的混合架构：

1. MinerU Artifact 继续是原始事实来源；
2. Source Span 只是现有 Evidence Ref 的标准化视图，不创建新的来源身份；
3. 现有 `epistemic_status` 枚举保持不变，细分语义写入 `fact_type`；
4. Matrix `scientific_facts` 是项目级结构化事实真相，不创建第二套事实卡文件；
5. 同一用户可以按 PDF 和 MinerU 内容哈希复用已验证计算缓存，但缓存不是业务真相；
6. Evidence Package 是章节和 Draft 的问题级证据视图；
7. Claim 必须在写作前绑定事实和证据；
8. 事实提取采用“基础事实一次提取＋Claim 缺口按需补充”，不对每篇论文穷举独立调用；
9. Agent 只负责事实语义提取、Claim 匹配和必要的冲突裁决；
10. 原文、数字、论文身份、版本发布和问题关闭由确定性程序负责；
11. Draft 补检发现的新事实通过原子链增量发布到 Matrix、Evidence Package、Draft 和 Quality；
12. 图像调用和科学事实分开校验；
13. 强否定只接受原文明示或封闭结构依据；其他证据缺口先补检，再收窄或省略；
14. 不通过循环重写制造“已修复”假象；
15. 用户只处理无法由本地来源确定的科学选择。

该方案在不增加新阶段、不拆分新微服务、不复制现有证据逻辑的前提下，补齐“解析结果能够真正成为可核验科学事实，并精确约束正文句子”的核心能力。
