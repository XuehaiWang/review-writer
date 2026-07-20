# 化学图分层混合重绘设计

日期：2026-07-20
状态：已确认，待实施计划

## 1. 背景

当前 `review-figure-style-redraw` 将整张来源图发送到图像编辑接口，并通过提示词要求模型只改变视觉风格、保留结构、立体化学、文字、反应条件和布局。该流程保留来源与重绘 manifest，并要求人工逐图核验，但化学正确性主要依赖模型遵循提示词和人工检查，缺少结构化化学表示、自动差异检测及可靠的可编辑输出。

本设计将该流程升级为“化学语义重建 + 确定性排版 + 自动校验 + 风险分级人工确认”的分层混合系统。

## 2. 目标与范围

### 2.1 目标

- 在化学准确性、视觉统一度和批量处理效率之间取得平衡。
- 重点支持分子结构、常规反应式及机理图。
- 以开源、本地处理为主，必要时允许调用图像模型或外部 API。
- 同时交付出版级 PNG、可编辑 SVG 和 CDXML；无法安全生成 CDXML 时明确降级，不能伪装成完整成功。
- 将每张图的来源、识别结果、人工修订、渲染配置和验证结果完整留痕。

### 2.2 第一版范围

第一版自动处理：

- 单分子结构图；
- 常规单步反应图；
- 版式清晰的多步反应图。

第一版半自动处理：

- 催化循环；
- 复杂机理弯箭；
- 低清扫描、特殊配体、Markush/R-group 或识别置信度不足的图。

曲线图、普通表格、装置图和照片不属于本设计的主要自动重建范围，应由其他图表处理流程或明确的旧版图像编辑回退模式处理。

## 3. 设计原则

1. 化学关键内容不得由生成式图像模型自由重绘。
2. 原生矢量和文本的提取优先级高于 OCR/OCSR。
3. 识别、语义表示、渲染、验证和人工修订必须解耦。
4. 任何低置信度结果都进入人工确认，不自动猜测。
5. 所有输出必须能够追溯到来源区域、工具版本和配置哈希。
6. 下游只消费已验证且已批准的图片。

## 4. 总体架构

```text
来源 PDF / 图片
      |
      v
原生提取与预处理
      |
      v
版面分割与对象分类
      |
      +---- 分子区域 ---- OCSR 双引擎 ---- SMILES/Molfile
      +---- 文字区域 ---- OCR ------------ 原文与置信度
      +---- 箭头区域 ---- 几何/视觉检测 -- 箭头语义
      +---- 装饰区域 ---- 保留或辅助清理
      |
      v
chemical_figure_ir.json
      |
      +---- RDKit / Indigo / Ketcher ---- 化学对象
      +---- SVG compositor -------------- 版式、文字、箭头
      |
      v
SVG / PNG / CDXML
      |
      v
化学、文字、关系和布局校验
      |
      v
自动通过 / 快速人工检查 / 强制人工修订
```

## 5. 组件设计

### 5.1 来源解析与预处理器

职责：

- 解析现有 `figure_candidates.json`；
- 复用当前来源图定位逻辑；
- 优先从 PDF 提取嵌入图片、文本和矢量绘图；
- 对位图执行去噪、纠偏、缩放和清晰度评分；
- 识别 panel 边界和图片类别；
- 为所有来源数据计算 SHA-256。

输出为标准化来源包，包含原图、来源 PDF/页码、裁剪坐标、清晰度评分和来源哈希。

### 5.2 版面分割器

将图分为：

- `molecule`：分子、催化剂、配体和中间体；
- `text`：条件、收率、编号和注释；
- `arrow`：反应箭头、平衡箭头和机理弯箭；
- `decoration`：panel 标签、框线和非化学视觉元素。

每个对象保留边界框、mask、原始裁剪、类别置信度和所在 panel。分割器不解释化学，只确定对象和空间范围。

### 5.3 化学结构识别器

默认采用两个独立 OCSR 引擎，例如 DECIMER 与 MolNexTR。每个引擎输出原始预测、isomeric SMILES、Molfile 和自身置信信息。

系统将输出标准化后比较：

- canonical isomeric SMILES 完全一致：提高可信度；
- 骨架一致但立体化学不同：高风险；
- 分子式一致但连接关系不同：中高风险；
- 无法解析或结果完全不同：识别失败。

RDKit sanitization 只能证明结构在工具规则下可解析，不能证明它与原图相同，因此不得单独作为通过条件。

### 5.4 文字与箭头识别器

文字对象同时保存：

- OCR 原始结果；
- 经过空白规范化的结果；
- 字符级置信度；
- 原始截图；
- 是否含数字、单位、化学式或立体化学标记。

规范化不得改变负号、上下标、百分号、温度、当量、时间、`ee/dr` 或元素大小写。

箭头对象记录类型、方向、起止点、所属 panel、连接对象以及上方/下方条件。空间接近度只是关系判断的一个特征；还必须结合箭头方向、panel 边界、加号、多行基线和编号。

### 5.5 化学图中间表示

每张图生成 `chemical_figure_ir.json`，建议包含：

```json
{
  "schema_version": "1.0",
  "source": {
    "image_path": "source/F001.png",
    "sha256": "<source-sha256>",
    "paper_id": "P001",
    "source_label": "Scheme 1"
  },
  "canvas": {
    "width": 1800,
    "height": 900
  },
  "panels": [],
  "objects": [
    {
      "id": "mol_01",
      "type": "molecule",
      "bbox": [80, 200, 360, 500],
      "isomeric_smiles": "<recognized-isomeric-smiles>",
      "molfile_path": "structures/mol_01.mol",
      "source_crop": "crops/mol_01.png",
      "recognition_confidence": 0.97,
      "human_status": "unreviewed"
    }
  ],
  "relations": [],
  "style_profile": "organic-review-clean-v2"
}
```

IR 是唯一受支持的渲染输入。人工修订也必须先更新 IR，禁止只修改最终 PNG 而不回写语义数据。

### 5.6 规范化渲染器

- RDKit：结构合法性处理、二维坐标及 SVG 分子绘制；
- Indigo：反应对象、结构布局及格式转换；
- Ketcher：浏览器人工编辑、反应修订和 CDXML 导出；
- SVG compositor：组合分子 SVG、文字、箭头、panel 和编号；
- SVG rasterizer：从同一份 SVG 产生最终 PNG。

风格必须由结构化配置控制，而不是只有自由文本名称。最低配置项包括键长、键宽、字体、字号、箭头宽度、颜色、对象间距、panel 间距和画布边距。

CDXML 由经过确认的结构/反应对象导出。若 CDXML 无法表达或可靠保留完整机理版式，系统输出结构/反应 CDXML 与完整 SVG，并在 manifest 中标记 `cdxml_status: partial`，不得标记为完整成功。

### 5.7 自动验证器

自动验证包含：

1. 化学合法性：价态、芳香性、电荷、同位素和立体标记可解析；
2. 双模型一致性：比较独立 OCSR 结果；
3. 渲染回读：对新渲染的分子再次 OCSR，并与确认后的 IR 比较；
4. 文字一致性：对渲染图 OCR，逐字符比较关键文字；
5. 关系一致性：检查反应物、箭头、产物和条件归属；
6. 布局一致性：检查 panel 顺序、对象相对顺序和越界/重叠。

验证结果写入 `verification_report.json`，并生成风险分数：

- 0–20：可进入快速人工确认；
- 21–50：必须检查系统标出的差异区域；
- 51–100：必须人工修订后重新验证。

第一版不启用完全自动批准；所有图片最终仍需明确的人工 `approved` 状态。

### 5.8 图像模型辅助器

图像模型只允许用于：

- 扫描背景清理；
- 非化学装饰修复；
- 区域分割建议；
- 无法结构化区域的视觉适配。

分子、反应条件、数字、箭头和立体标记所在区域必须由确定性渲染结果覆盖。任何使用旧版整图编辑模式的输出均设置高风险，并强制逐图人工核验。

## 6. 状态机与数据流

每张图使用可恢复状态机：

```text
source_resolved
  -> preprocessed
  -> segmented
  -> recognized
  -> human_corrected（按风险需要）
  -> rendered
  -> verified
  -> approved
```

每个阶段保存输入哈希、输出哈希、工具/模型版本、配置版本、运行时间和错误信息。输入及配置哈希未变化时允许断点续跑；任一上游工件变化都使所有下游状态失效。

## 7. 失败与回退

- 原生矢量可用：优先复用，不执行不必要的 OCSR。
- OCSR 低置信度：保留原始结构裁剪并要求人工重建/确认。
- OCR 不确定：要求人工转录；不得用语言模型猜测条件。
- 弯箭识别失败：结构可继续重绘，但机理箭头必须人工编辑。
- CDXML 导出失败：保留 SVG、PNG、Molfile/KET，并标记部分交付。
- API 不可用：本地结构化处理仍可继续。
- 图片不在支持范围：进入明确的 `unsupported` 或 `legacy_image_edit` 状态。
- 任一严重差异：不能进入 `verified`。

严重差异包括原子/键增删、键级错误、立体化学变化、电荷/自由基/同位素错误、条件或数值变化、反应方向错误及对象归属错误。

## 8. 人工修订界面

人工界面采用四区布局：

- 左侧：原始对象裁剪；
- 中间：规范化渲染结果与差异高亮；
- 右侧：嵌入 Ketcher 的结构/反应编辑器；
- 底部：原始文字、OCR 结果、关系和验证错误。

用户可修正结构、立体化学、文字、箭头类型及对象关系。保存操作更新 IR，随后重新渲染和验证。

## 9. 工件与现有流程集成

保留现有工件：

- `style_config.json`；
- `source_figure_manifest.json`；
- `redrawn_figure_manifest.json`；
- `figure_redraw_report.md`。

每张图新增目录：

```text
03_figure_redraw/F001/
  source.png
  crops/
  structures/
  chemical_figure_ir.json
  figure.svg
  figure.png
  figure.cdxml
  verification_report.json
  source_comparison.html
```

manifest 至少扩展：

```json
{
  "status": "verified",
  "processing_mode": "hybrid_structured",
  "ir_path": "03_figure_redraw/F001/chemical_figure_ir.json",
  "svg_path": "03_figure_redraw/F001/figure.svg",
  "png_path": "03_figure_redraw/F001/figure.png",
  "cdxml_path": "03_figure_redraw/F001/figure.cdxml",
  "cdxml_status": "complete",
  "verification_report": "03_figure_redraw/F001/verification_report.json",
  "risk_score": 18,
  "human_check_status": "approved"
}
```

草稿插图和最终审计只接受同时满足以下条件的图片：

- `status == "verified"`；
- `human_check_status == "approved"`；
- PNG/SVG 路径存在且来源哈希仍然有效；
- `verification_report.json` 没有严重差异。

用户明确选择无图稿时，仍使用现有非空 `skip_reason.md` 机制。

## 10. 测试与验收

### 10.1 金标准数据集

从项目真实文献中建立 100–200 张人工标注样本，覆盖清晰电子图、扫描图、单分子、单步/多步反应、手性、E/Z、轴手性、金属配合物、缩写基团、电荷、自由基、同位素、催化循环和机理弯箭。

每个样本提供 Molfile/isomeric SMILES、准确文字、对象关系、panel 顺序和人工认可的输出。数据按论文划分训练、验证和测试集合，避免同一论文的相似图跨集合泄漏。

### 10.2 指标

| 指标 | 第一阶段目标 | 稳定阶段目标 |
|---|---:|---:|
| 分子连接关系完全正确 | ≥90% | ≥97% |
| 立体化学完全正确 | ≥85% | ≥95% |
| 条件文字字符准确率 | ≥98% | ≥99.5% |
| 反应物—产物关系正确 | ≥95% | ≥99% |
| 自动验证漏报严重错误 | ≤2% | ≤0.5% |
| 普通反应图无需人工修改 | ≥50% | ≥80% |
| SVG/CDXML 成功输出 | ≥90% | ≥98% |

这些指标用于阶段验收，不代表低于目标的单张图片可以绕过人工门禁。

### 10.3 测试层次

- 单元测试：IR schema、SMILES/立体比较、文字规范化、风险计算、SVG 对象和状态转换；
- 集成测试：来源图到验证报告的完整流水线；
- 回归测试：楔形/虚线键、轴手性、缩写基团、金属配体、多组分反应、弯箭、可逆箭头及关键条件；
- 视觉快照测试：SVG 渲染后的字体、间距、箭头和 panel 布局；
- 失败注入测试：模型/API 不可用、损坏图片、无效结构、陈旧缓存和部分导出。

## 11. 分阶段实施

### 阶段一：安全基础版

- 结构化风格配置；
- 来源预处理和版面分割；
- OCSR 接口抽象与双引擎结果模型；
- OCR 与字符差异检查；
- `chemical_figure_ir.json`；
- RDKit SVG、PNG 和分子 Molfile；
- 验证报告、风险评分和人工门禁。

### 阶段二：完整反应图重建

- 反应箭头、条件归属和多步关系；
- Indigo/Ketcher 集成；
- CDXML 输出；
- 浏览器人工修订界面；
- 缓存、断点续跑和工件失效机制。

### 阶段三：复杂机理与规模化

- 催化循环和机理弯箭；
- 多模型选择与任务队列；
- GPU 调度、成本和耗时统计；
- 基于人工修订数据的持续评估；
- 满足长期指标后再评估低风险图片自动批准。

## 12. 参考技术

- DECIMER.ai：<https://www.nature.com/articles/s41467-023-40782-0>
- MolNexTR：<https://github.com/CYF2000127/MolNexTR>
- RDKit drawing：<https://www.rdkit.org/docs/source/rdkit.Chem.Draw.html>
- Ketcher：<https://lifescience.opensource.epam.com/ketcher/index.html>
- PyMuPDF drawing extraction：<https://pymupdf.readthedocs.io/>
