# review-writer

**领域无关的文献综述自动生成系统**。输入一批 PDF 论文和用户指定的主题，输出一篇出版级的综述文章（.docx），含格式化引用、图表和参考文献——通过 11 阶段流水线实现，每阶段嵌入人工审核节点。

## 概览

```
topic + PDF 论文 → 11 阶段流水线 → final_review.docx
```

系统设计为**跨学科通用**（化学、机器学习、地球科学等均可）。使用 LLM 进行综合推理与写作，确定性脚本负责评分、合并和格式化。每个阶段完成后暂停等待人工确认（结论生成、总结图两个阶段由 Agent 自动完成）。

论文入库由用户自建的 **LabKAG** 服务（PDF 解析、LLM 抽取、taxonomy 打标签、按选题匹配候选，`labkag-review-skill` 包装）与在线检索（`review-online-paper-discovery`）共同完成，取代了早期版本里分开的 MinerU 解析 + metadata 编码技能。`labkag-review-skill` 只在 review-writer 仓库里维护一份（不在 LabKAG 自己的仓库里重复），避免两份拷贝互相漂移。

## 流水线

| # | 阶段 | Skill | 功能 |
|---|------|-------|------|
| 1 | 在线论文检索 | `review-online-paper-discovery` | 主题分解与消歧 → 关键词扩展 → Crossref/SciAtlas 检索 → 人工确认 → 解析下载来源（不自动下载，人工下载后用 `register-pdfs` 登记） |
| 2 | LabKAG 桥接 | `labkag-review-skill` | ingest 已登记 PDF → 构建/复用 taxonomy → 按选题 `match-topic` → 导出 `selected_discovery_results.json`（与阶段 1 共用 `00_discovery/` 目录，不单独占一个文件夹） |
| 3 | 文献矩阵 | `review-literature-matrix-outline` | 逐篇 ~1000 字摘要 → 文献矩阵 → 2-3 个大纲选项 |
| 4 | 章节蓝图 | `review-section-blueprint` | 论文分配到章节，定义论点、论断、图表需求、段落结构 |
| 5 | 章节撰写 | `review-section-drafting-figure-picking` | 撰写各章节 + 从解析产物中选择真实配图 |
| 6 | 图片重绘 | `review-figure-style-redraw` | （可选）通过图像 API 将源图重绘为统一风格 |
| 7 | 合并打磨 | `review-draft-merge-polish` | 合并章节 → paper_id 转为 `[N]` 引用 → ACS 格式参考文献 |
| 8 | 结论生成 | `review-conclusion-generator` | 基于已批准初稿生成有据可依的结论/挑战/展望章节（Agent 自动） |
| 9 | 终审发布 | `review-final-audit-release` | 整合已验证结论 → 格式扫描 + 内容审查 → 发布报告 |
| 10 | 结构总结图 | `review-outline-summary-chart` | 基于定稿生成全文与分章节 Mermaid 结构图（Agent 自动） |
| 11 | DOCX 导出 | `review-export-docx` | Markdown → 带样式的 Word 文档 |

## 目录结构

```
review-writer/
├── skills/                          # 技能实现
│   ├── review-online-paper-discovery/    # 在线检索 + 主题消歧 + 下载入库
│   ├── labkag-review-skill/              # 包装用户自建的 LabKAG 服务
│   ├── review-literature-matrix-outline/ # 文献矩阵构建
│   ├── review-section-blueprint/         # 章节蓝图与写作规则
│   ├── review-section-drafting-figure-picking/  # 章节撰写
│   ├── review-figure-style-redraw/       # （可选）图片重绘
│   ├── review-draft-merge-polish/        # 合并与引用编号
│   ├── review-conclusion-generator/      # 结论/挑战/展望章节生成
│   ├── review-final-audit-release/       # 格式与内容审计
│   ├── review-outline-summary-chart/     # 结构总结图生成
│   ├── review-export-docx/               # Markdown → DOCX
│   ├── review-writing-orchestrator/      # 流水线编排
│   ├── template/                         # 写作风格参考
│   ├── 使用说明.md                        # 使用指南
│   └── 项目总结.md                        # 项目总结
│
├── review-library/                  # 论文知识库
│   ├── paper_pdf/                   # 习惯上放原始 PDF，但不是强制位置——
│   │                                #   register-pdfs 的 --paper-pdf-dir 可以指向任意文件夹
│   ├── mineru-outputs/              # MinerU 解析产物，固定放在这里（衍生数据，不像 PDF 那样自由）
│   ├── metadata/papers/             # 逐篇元数据 JSON
│   └── registry/papers.jsonl        # 论文注册表
│
├── review-projects/                 # 综述项目
│   └── <project-id>/               # 每个项目独立目录
│       ├── 00_discovery/
│       ├── 01_matrix_outline/
│       ├── 02_section_blueprint/
│       ├── 03_section_drafting/
│       ├── 04_figure_redraw/
│       ├── 05_first_draft/
│       ├── 06_conclusion_generation/
│       ├── 07_final_audit/
│       ├── 08_summary_chart/
│       └── 09_docx_export/
│
├── view/                            # Web 看板
│   ├── serve_review_dashboard.py
│   └── assets/dashboard/
└── README.md
```

## 快速开始

### 前置依赖

- Python 3.10+
- 一个已配置好的 LabKAG 服务实例（PDF 解析、LLM 抽取、Neo4j 图存储）
- OpenAI 兼容 API 端点（用于 LLM 调用）

### 1. 导入论文

两种方式，可搭配使用：

```bash
# 方式 A：在线检索，人工下载后登记（主题消歧 + Crossref/SciAtlas 检索 + 人工确认 → 解析下载来源）
python skills/review-online-paper-discovery/scripts/discover.py search \
  --topic "<review topic>" --project-id <project_id> --agent-keywords <path> --web-search
# ...人工确认候选集后...
python skills/review-online-paper-discovery/scripts/discover.py list-for-download --project-id <project_id>
# ...人工在浏览器里下载 PDF，放进任意文件夹后...
python skills/review-online-paper-discovery/scripts/discover.py register-pdfs \
  --project-id <project_id> --paper-pdf-dir <你存放 PDF 的文件夹>
```

```bash
# 方式 B：把已有 PDF 放进任意文件夹，再通过 labkag-review-skill 解析入库
python skills/labkag-review-skill/scripts/labkag_api.py batch-extract --input-dir <pdfs> --extractions-dir <cache> --project-id <id> --mineru-output-dir review-library/mineru-outputs
python skills/labkag-review-skill/scripts/labkag_api.py batch-ingest --extractions-dir <cache> --project-id <id>
```

**下载本身是人工步骤，不再自动化**：Unpaywall 能找到开放获取版本的比例对主流付费期刊化学论文并不高，且部分预印本站点（如 ChemRxiv）本身有 Cloudflare 防护，自动化下载无法绕过——这不是脚本缺陷，是真实的访问限制，所以脚本只负责解析下载来源（`list-for-download`），不再尝试自动抓取。解析不到来源会诚实报告 `no_pdf_source_found`；人工在浏览器里手动下载后，放进任意文件夹，用 `register-pdfs` 登记进知识库（先按建议文件名匹配，匹配不上时用论文标题做二次匹配）。

### 2. 运行编排器

```bash
python skills/review-writing-orchestrator/scripts/project_status.py \
  --project-id <主题英文短名称>
```

或通过 LLM 直接调起编排器：

```
"请使用 review-writing-orchestrator 技能，围绕 <你的主题> 生成一篇综述"
```

### 3. 启动看板（可选）

```bash
python view/serve_review_dashboard.py --review-root . --host 127.0.0.1 --port 8765
# 浏览器访问 http://127.0.0.1:8765
```

## 核心设计原则

- **人在回路中**：每个阶段完成后暂停等待人工确认（结论生成、总结图两阶段由 Agent 自动完成）。编排器每次只跑一个阶段。
- **在线检索的消歧优先于关键词扩展**：模糊主题词（如缩写）先用探测检索比对候选含义的真实文献证据，证据不足才问人工，而不是凭常识猜测。
- **成本控制**：文献矩阵限制单篇论文读取长度并分批处理，章节撰写构建共享内容缓存，终审逐章节审查而非一次性加载全部证据，LabKAG taxonomy 优先用小批量试点论文建立、避免整库重新打标签，在线检索只在人工确认候选集之后才解析下载来源（下载本身是人工操作）。
- **领域无关**：无硬编码学科词汇。7 类开放词表标签（`output`/`input`/`method`/`co_input`/`modifier`/`process_type`/`document_scope`）由 LLM 按论文内容自由撰写。
- **可审计**：每条元数据记录来源（`source`）、置信度（`confidence`）和人工审核状态（`human_checked`）。被排除的候选论文保留排除原因（`excluded_reason`）。
- **可续跑**：脚本支持 `--limit`、`--dry-run`、下载失败/未确认自动重试，批量操作跳过已有输出。

## 数据模型

每篇论文用以下结构的元数据 JSON 表示：

```json
{
  "paper_id": "P001",
  "title": {"value": "论文标题", "source": "llm+rule", "confidence": 0.88, "human_checked": false},
  "authors": {"value": ["作者1", "作者2"], ...},
  "year": {"value": 2023, ...},
  "structured_tags": {
    "value": {"output": "联烯", "input": "末端炔烃", "method": "铜催化", "process_type": "合成方法学", ...},
    "source": "llm",
    "confidence": 0.85
  },
  "source_paths": {"pdf": "...", "markdown": "...", "content_list": "..."},
  "quality": {"overall_confidence": 0.75, "needs_human_check": true}
}
```

## 引用格式（ACS 风格）

```text
[N] Last1, F.; Last2, F. 论文标题. *期刊* **年份**, *卷号*, 页码.
```

引用编号按全文阅读顺序全局分配（非按章节独立编号）。缺少年刊卷页等书目信息时，优雅地省略对应字段。

## Web 看板

看板为每个流水线阶段提供可视化界面：

| 页面 | 路径 | 用途 |
|------|------|------|
| 论文库 | `/library` | 浏览论文元数据 |
| 发现 | `/discovery` | 审核在线检索候选论文 |
| 矩阵 | `/matrix` | 审核文献矩阵行 |
| 蓝图 | `/blueprint` | 审核章节结构 |
| 章节 | `/sections` | 审核章节草稿 |
| 图片 | `/figures` | 审核配图候选 |
| 草稿 | `/draft` | 审核合并初稿 |
| 终稿 | `/final` | 终审与 DOCX 导出 |

## 项目状态

本仓库处于活跃开发中。`versatile` 分支包含领域无关通用版；`labkag-integration` 分支（基于 `versatile`）在此基础上把论文入库切换为用户自建的 LabKAG 服务；`main` 分支为原始化学特化版本。
