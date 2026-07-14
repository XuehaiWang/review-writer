# review-writer

**领域无关的文献综述自动生成系统**。输入一批 PDF 论文和用户指定的主题，输出一篇出版级的综述文章（.docx），含格式化引用、图表和参考文献——通过 8 阶段流水线实现，每阶段嵌入人工审核节点。

## 概览

```
topic + PDF 论文 → 8 阶段流水线 → final_review.docx
```

系统设计为**跨学科通用**（化学、机器学习、地球科学等均可）。使用 LLM 进行综合推理与写作，确定性脚本负责评分、合并和格式化。每个阶段完成后暂停等待人工确认。

## 流水线

| # | 阶段 | Skill | 功能 |
|---|------|-------|------|
| 0 | PDF 解析 | `mineru-precise-parse-review-writer` | 通过 MinerU API 将 PDF 转为 Markdown + 图片 |
| 1 | 元数据准备 | `review-metadata-prep` | 提取标题/作者/年份/期刊/DOI + 7 类开放词表标签 |
| 2 | 论文发现 | `review-topic-paper-discovery` | 主题扩展为关键词 → 本地论文评分 + 网络检索 → 20-30 篇候选 |
| 3 | 文献矩阵 | `review-literature-matrix-outline` | 逐篇 ~1000 字摘要 → 文献矩阵 → 2-3 个大纲选项 |
| 4 | 章节蓝图 | `review-section-blueprint` | 论文分配到章节，定义论点、论断、图表需求、段落结构 |
| 5 | 章节撰写 | `review-section-drafting-figure-picking` | 撰写各章节 + 从 MinerU 产物中选择真实配图 |
| 6 | 图片重绘 | `review-figure-style-redraw` | （可选）通过图像 API 将源图重绘为统一风格 |
| 7 | 合并打磨 | `review-draft-merge-polish` | 合并章节 → paper_id 转为 `[N]` 引用 → ACS 格式参考文献 |
| 8 | 终审发布 | `review-final-audit-release` | 格式扫描 + 内容审查 → 发布报告 |
| 9 | DOCX 导出 | `review-export-docx` | Markdown → 带样式的 Word 文档 |

## 目录结构

```
review-writer/
├── skills/                          # 12 个技能实现
│   ├── review-metadata-prep/        #   元数据提取与校验
│   ├── review-topic-paper-discovery/ #  主题 → 关键词 → 论文评分
│   ├── review-literature-matrix-outline/ 文献矩阵构建
│   ├── review-section-blueprint/    #   章节蓝图与写作规则
│   ├── review-section-drafting-figure-picking/  章节撰写
│   ├── review-figure-style-redraw/  # （可选）图片重绘
│   ├── review-draft-merge-polish/   #   合并与引用编号
│   ├── review-final-audit-release/  #   格式与内容审计
│   ├── review-export-docx/          #   Markdown → DOCX
│   ├── review-writing-orchestrator/ #   流水线编排
│   ├── mineru-precise-parse-review-writer/  PDF → Markdown
│   ├── template/                    #   写作风格参考
│   ├── 使用说明.md                   #   使用指南
│   └── 项目总结.md                   #   项目总结
│
├── review-library/                  # 论文知识库
│   ├── metadata/papers/             # 逐篇元数据 JSON
│   ├── registry/papers.jsonl        # 论文注册表
│   └── metadata/library_vocabulary.json  # 标签值 + 标题汇总
│
├── mineru-outputs/                  # MinerU 解析产物
│   ├── markdown/                    # 全文 Markdown
│   └── extracted/                   # 图片、content_list.json
│
├── review-projects/                 # 综述项目
│   └── <project-id>/               # 每个项目独立目录
│       ├── 00_discovery/
│       ├── 01_matrix_outline/
│       ├── 02_section_blueprint/
│       ├── 03_section_drafting/
│       ├── 04_figure_redraw/
│       ├── 05_first_draft/
│       ├── 06_final_audit/
│       └── 07_docx_export/
│
├── xmart_55_library/                # 源 PDF 论文（177 篇）
├── view/                            # Web 看板
│   ├── serve_review_dashboard.py
│   └── assets/dashboard/
├── run_dashboard.cmd               # Windows 看板启动脚本
└── README.md
```

## 快速开始

### 前置依赖

- Python 3.10+
- MinerU API Token（用于 PDF 解析）
- OpenAI 兼容 API 端点（用于 LLM 调用）

### 1. 导入论文

将 20-30 篇 PDF 放入知识库目录，然后：

```bash
# 通过 MinerU 将 PDF 解析为 Markdown
python skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py

# 提取元数据（规则模式 + 可选 LLM）
python skills/review-metadata-prep/scripts/prepare_metadata.py --use-llm
```

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
run_dashboard.cmd
# 浏览器访问 http://127.0.0.1:8765
```

## 核心设计原则

- **人在回路中**：每个阶段完成后暂停等待人工确认。编排器每次只跑一个阶段。
- **成本控制**：两遍式标签（大型知识库先规则提取 → 只对入选论文 LLM 打标签）、论文内容缓存避免重复读全文、分级用模型（提取用便宜模型、推理用大模型）。
- **领域无关**：无硬编码学科词汇。7 类开放词表标签（`output`/`input`/`method`/`co_input`/`modifier`/`process_type`/`document_scope`）由 LLM 按论文内容自由撰写。
- **可审计**：每条元数据记录来源（`source`）、置信度（`confidence`）和人工审核状态（`human_checked`）。被排除的论文保留排除原因（`excluded_reason`）。
- **可续跑**：所有脚本支持 `--limit`、`--force`、`--dry-run`，批量操作跳过已有输出。

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
| 发现 | `/discovery` | 审核候选论文 |
| 矩阵 | `/matrix` | 审核文献矩阵行 |
| 蓝图 | `/blueprint` | 审核章节结构 |
| 章节 | `/sections` | 审核章节草稿 |
| 图片 | `/figures` | 审核配图候选 |
| 草稿 | `/draft` | 审核合并初稿 |
| 终稿 | `/final` | 终审与 DOCX 导出 |

## 项目状态

本仓库处于活跃开发中。`versatile` 分支包含领域无关通用版；`main` 分支为原始化学特化版本。
