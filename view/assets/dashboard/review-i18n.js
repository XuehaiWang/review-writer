(function () {
  "use strict";

  const STORAGE_KEY = "review-writer-ui-language";
  const SUPPORTED = new Set(["en", "zh-CN"]);
  const EN_ZH = Object.freeze({
    "← Back to workspace": "← 返回工作台",
    "Back to workspace": "返回工作台",
    "Settings": "设置",
    "⚙ Settings": "⚙ 设置",
    "Local provider configuration": "本地服务商配置",
    "Local workspace settings": "本地工作区设置",
    "Active workspace": "当前工作区",
    "Checking settings": "正在检查设置",
    "Settings saved locally": "设置已保存到本地",
    "Not saved locally": "尚未保存到本地",
    "Unknown workspace": "未知工作区",
    "API Provider Settings": "API 服务商设置",
    "Configure MinerU parsing, text generation, and image generation providers. Saved values apply to newly started tasks in this workspace.": "配置 MinerU 解析、文本生成和图像生成服务商。保存后将应用于此工作区中新启动的任务。",
    "MinerU parsing": "MinerU 解析",
    "Required for uploaded PDF Markdown, content blocks, and figure extraction.": "用于生成上传 PDF 的 Markdown、内容块和图像提取结果。",
    "Text generation": "文本生成",
    "Used by section writing, conclusion generation, metadata AI, and final draft merging.": "用于章节撰写、结论生成、元数据 AI 和最终稿合并。",
    "Image generation": "图像生成",
    "Used by AI figure redraw and the review overview figure.": "用于 AI 图像重绘和综述总览图。",
    "MinerU key": "MinerU 密钥",
    "Text API URL": "文本 API 地址",
    "Text API key": "文本 API 密钥",
    "Image API URL": "图像 API 地址",
    "Image API key": "图像 API 密钥",
    "Model": "模型",
    "API format": "接口格式",
    "Show": "显示",
    "Hide": "隐藏",
    "Reload": "重新加载",
    "Save settings": "保存设置",
    "Secrets stay on this computer": "密钥仅保存在这台电脑",
    "Local settings": "本地设置",
    "Environment / .env": "环境变量 / .env",
    "Legacy token file": "旧令牌文件",
    "Not configured": "未配置",
    "Loading": "加载中",
    "Loading settings…": "正在加载设置…",
    "Saving and applying settings…": "正在保存并应用设置…",
    "Settings loaded. Blank key fields preserve existing secrets.": "设置已加载。密钥输入框留空将保留已有密钥。",
    "API settings saved and applied.": "API 设置已保存并应用。",
    "Step 1 · Choose a structure": "步骤 1 · 选择组织结构",
    "Choose your review structure": "选择综述大纲结构",
    "Start from the literature metadata and the current Matrix, or upload a reference review to derive its organization.": "可基于文献元数据与当前矩阵选择大纲，也可上传参考综述并提取其组织结构。",
    "Current selection": "当前选择",
    "Selection required": "需要选择",
    "Built-in structures": "内置大纲结构",
    "Choose the logic that best matches the scientific story.": "选择最符合综述科学叙事的组织逻辑。",
    "Substrate structure": "按底物结构组织",
    "Organize papers by substrate class and structural variation.": "按照底物类别与结构变化组织文献。",
    "Catalyst / method structure": "按催化剂／方法组织",
    "Compare catalytic systems, ligands, and methodological families.": "对比催化体系、配体和方法类别。",
    "Reaction-type structure": "按反应类型组织",
    "Follow transformation logic and mechanistic strategy.": "按照转化逻辑与机理策略组织内容。",
    "Custom outline": "自定义大纲",
    "Open a completely blank outline and organize every section yourself.": "打开完全空白的大纲，由你自行组织所有章节。",
    "Use this outline": "使用此大纲",
    "Reference review": "参考综述",
    "Upload a published review to derive an outline that follows its organization.": "上传一篇已发表综述，提取并沿用其组织方式。",
    "Outline upload": "上传大纲参考文件",
    "PDF, DOCX, Markdown, or TXT · the uploaded file is analyzed before it becomes selectable.": "支持 PDF、DOCX、Markdown 或 TXT；文件解析完成后即可选择。",
    "No reference outline has been uploaded yet.": "尚未上传参考综述大纲。",
    "Imported reference outlines": "已导入的参考大纲",
    "Select": "选择",
    "Details are displayed in the right panel.": "详情已显示在右侧栏目中。",
    "Analyzing reference format and writing style with AI...": "正在使用 AI 分析参考综述的格式与写法……",
    "Generated outline preview": "生成的大纲预览",
    "Matrix-derived options": "基于文献矩阵生成",
    "Current selected outline": "当前已选大纲",
    "Used by Stage 4": "第四阶段使用",
    "Manually edited": "已手动编辑",
    "Editable selected outline": "可编辑的当前大纲",
    "Save outline changes": "保存大纲修改",
    "Saving outline changes...": "正在保存大纲修改……",
    "Saved. Stage 4 will use this edited outline.": "已保存，第四阶段将使用此编辑后的大纲。",
    "Unsaved outline changes.": "大纲有尚未保存的修改。",
    "Blank custom outline is ready. Write it below and save your changes.": "空白自定义大纲已就绪，请在下方编写并保存。",
    "Blank custom draft": "空白自定义草稿",
    "Reset to blank": "重置为空白",
    "Reset the current custom outline to a completely blank editor?": "确定要将当前自定义大纲重置为完全空白的编辑器吗？",
    "Write your custom outline here...": "请在此编写自定义大纲……",
    "Write at least one section and save it before Stage 4.": "请至少编写一个章节并保存，然后才能进入第四阶段。",
    "blank custom draft": "空白自定义草稿",
    "Write and save the custom outline before Blueprint.": "请先编写并保存自定义大纲，再进入写作蓝图。",
    "Visual editor": "可视化编辑",
    "Advanced Markdown": "高级 Markdown",
    "Outline editing mode": "大纲编辑模式",
    "Visual outline builder": "可视化大纲构建器",
    "Add sections, assign papers, and drag cards to reorder them.": "添加章节、分配论文，并可拖动卡片调整顺序。",
    "Add Introduction & Conclusion": "添加简介与结论",
    "Add section": "添加章节",
    "Add first section": "添加第一个章节",
    "Start with a blank visual outline": "从空白可视化大纲开始",
    "Add your first section, or create Introduction and Conclusion cards as a starting point.": "添加第一个章节，或者先创建简介和结论卡片。",
    "Recommend papers": "推荐论文",
    "Section title": "章节标题",
    "Section purpose": "章节目的",
    "What question should this section answer?": "本章节需要回答什么问题？",
    "Select papers": "选择论文",
    "Filter selected Matrix papers": "筛选矩阵中的论文",
    "Additional instructions / figure plan": "补充要求／图表计划",
    "Optional comparison rules, mechanism focus, figures, tables, or transitions.": "可填写比较规则、机理重点、图表需求或章节衔接。",
    "Drag to reorder": "拖动调整顺序",
    "Move section up": "章节上移",
    "Move section down": "章节下移",
    "Delete": "删除",
    "Use level-2 headings for major sections and add an": "主要章节请使用二级标题，并为每个章节添加",
    "line to every section.": "行。",
    "Complete every section title and paper assignment before saving.": "保存前请补全每个章节的标题和论文分配。",
    "Ready to save.": "可以保存。",
    "Outline selected.": "大纲已选择。",
    "Choose a built-in outline, create a custom outline, or upload a reference review. Stage 4 Blueprint remains unavailable until an outline is selected.": "请选择内置大纲、自定义大纲或上传参考综述；完成选择前无法进入第四阶段写作蓝图。",
    "Edit Markdown directly. Use level-2 headings such as": "可直接编辑 Markdown。主要章节请使用二级标题，例如",
    "for major sections. Keep or update": "。如需明确分配论文，请保留或修改",
    "when you want explicit paper assignments.": "。",
    "Choose a built-in outline or upload a reference review. Stage 4 Blueprint remains unavailable until an outline is selected.": "请选择一个内置大纲或上传参考综述；完成选择前无法进入第四阶段写作蓝图。",
    "Candidate outline comparison": "候选大纲对比",
    "Compare the current saved version with Matrix-generated alternatives": "对比当前保存版本与文献矩阵生成的候选方案",
    "Current saved version": "当前保存版本",
    "System-generated candidates": "系统生成的候选方案",
    "View all Matrix-generated alternatives": "查看文献矩阵生成的全部备选方案",
    "Current Matrix outline": "当前矩阵使用的大纲",
    "This outline will be used to build the next-stage Blueprint.": "此大纲将用于构建下一阶段的写作蓝图。",
    "Return to Outline Options and choose or upload an outline.": "请返回大纲选项，选择内置大纲或上传参考综述。",
    "Current Matrix": "当前文献矩阵",
    "Review Writer": "综述写作系统",
    "Review Writer Dashboard": "综述写作系统",
    "Review Library Audit": "综述文献库审核",
    "Review Topic Discovery": "综述主题检索",
    "Review Matrix": "综述文献矩阵",
    "Review Blueprint": "综述写作蓝图",
    "Review Sections": "综述分节草稿",
    "Figure Review": "图像候选审核",
    "Review Figures": "综述图像重绘",
    "Review First Draft": "综述初稿",
    "Review Final": "综述终稿",
    "Draft Quality": "初稿质量",
    "Human Approval": "人工确认",
    "Final Preparation": "最终生成准备",
    "Final Generation": "最终生成",
    "Draft quality controls": "初稿质量控制",
    "Evaluate Current Draft": "评估当前初稿",
    "Batch Safe Optimize": "批量安全优化",
    "Generation actions": "生成操作",
    "Approved Draft": "已确认初稿",
    "Current overall score": "当前全文分数",
    "Previous score (out of date)": "旧评分（已失效）",
    "Evaluation state": "评估状态",
    "Human approval": "人工确认",
    "Problem queue": "问题队列",
    "Manual edit": "人工编辑",
    "AI rewrite": "AI 重写",
    "Return to Draft Approval": "返回初稿确认",
    "Approved quality score": "确认时质量分数",
    "First draft quality loop": "初稿质量循环",
    "Optional draft quality loop": "可选初稿质量循环",
    "Overall goal": "全文目标分",
    "Paragraph goal": "段落目标分",
    "Max iterations": "最大迭代次数",
    "Minimum words": "最少词数",
    "Maximum words": "最多词数",
    "Evaluate & Improve": "评估并优化",
    "Evaluate Only": "仅评估",
    "Stop": "停止",
    "Draft quality": "初稿质量",
    "Overall score": "全文得分",
    "Progress": "进度",
    "State": "状态",
    "No paragraph scores yet. Run Evaluate Only or Evaluate & Improve. This optional step does not block the existing Final actions.": "尚无段落评分。可运行“仅评估”或“评估并优化”；此可选步骤不会阻断原有终稿功能。",
    "Review Writer · Overview Schemes": "综述写作系统 · 总览图",
    "Library": "文献库",
    "Discovery": "检索",
    "Analysis & Planning": "文献分析与写作规划",
    "Image Processing": "图像处理",
    "Literature Matrix": "文献矩阵",
    "Outline & Blueprint": "大纲与章节蓝图",
    "Source Figure Review": "候选源图审核",
    "AI Redraw & Manual Edit": "AI 重绘与人工编辑",
    "Current stage steps": "当前阶段步骤",
    "Review the literature matrix, organize the outline, and confirm the section blueprint in one workspace.": "在同一工作区审核文献矩阵、组织大纲并确认章节蓝图。",
    "Select source figures, redraw them, edit SVG or chemical structures, and complete human approval.": "选择候选源图、完成重绘、编辑 SVG 或化学结构并进行人工审核。",
    "Confirm Matrix and Continue to Outline & Blueprint": "确认文献矩阵并继续大纲与章节蓝图",
    "Generate Writing Tasks and Enter Sections": "生成写作任务并进入分节写作",
    "Confirm Source Figures and Continue to AI Redraw": "确认候选源图并继续 AI 重绘",
    "Confirm Images and Enter Draft": "确认图像并进入初稿",
    "Loading image readiness...": "正在检查图像完成状态…",
    "Figure generation is still running. Wait until it finishes before entering Draft.": "图像仍在生成，请等待全部任务完成后再进入初稿。",
    "No manuscript figure is selected. Return to Source Figure Review and select at least one figure.": "尚未选择正文图像，请返回候选源图审核并至少选择一张图像。",
    "Figure outputs are out of date. Redraw the affected figures before entering Draft.": "图像结果已过期，请重新生成受影响的图像后再进入初稿。",
    "Used by Outline & Blueprint": "用于大纲与章节蓝图",
    "Saved. Outline & Blueprint will use this edited outline.": "已保存，大纲与章节蓝图将使用此编辑版本。",
    "Planning handoff time is unavailable.": "未记录写作规划交接时间。",
    "The planning blueprint changed. Regenerate section drafts from the current writing requirements.": "写作规划中的章节蓝图已更新，请根据当前写作要求重新生成分节草稿。",
    "The planning blueprint changed. Old section drafts remain on disk but are hidden because they do not match the current blueprint. Regenerate section drafts from the current writing requirements.": "写作规划中的章节蓝图已更新。旧分节草稿仍保留在磁盘中，但因与当前蓝图不一致而暂不显示；请根据当前写作要求重新生成。",
    "Final assembles and audits the human-approved Draft.": "最终生成会组装并审计已由人工确认的初稿。",
    "Matrix": "矩阵",
    "Blueprint": "蓝图",
    "Sections": "分节",
    "Figures": "图像",
    "Draft": "初稿",
    "Final": "终稿",
    "Human Check Stage": "人工审核阶段",
    "Paper Audit Desk": "论文审核台",
    "Review PDFs, MinerU Markdown, and structured metadata before topic-specific writing.": "在按主题写作前，审核 PDF、MinerU Markdown 与结构化 metadata。",
    "Find & Download OA": "检索并下载开放文献",
    "Upload local PDFs": "上传本地 PDF",
    "Batch upload builds searchable metadata and full-text Markdown.": "批量上传后自动构建可检索 metadata 与全文 Markdown。",
    "Search title, author, keyword, tag": "检索标题、作者、关键词或标签",
    "No papers match the current search.": "没有找到符合当前检索条件的文献。",
    "Try fewer or more specific keywords.": "请减少关键词，或换用更准确的关键词。",
    "PDF": "PDF",
    "Markdown": "Markdown",
    "Metadata": "Metadata",
    "Reload": "刷新",
    "Save": "保存",
    "Confirm": "确认",
    "Close": "关闭",
    "Cancel": "取消",
    "Edit": "编辑",
    "Preview": "预览",
    "Summary": "摘要",
    "Report": "报告",
    "Raw JSON": "原始 JSON",
    "Mark reviewed": "标记为已审核",
    "Editable JSON source of truth": "可编辑的 JSON 权威数据源",
    "Find open-access journal articles": "检索开放获取期刊文章",
    "PDF lookup falls back through Crossref, Europe PMC, Semantic Scholar, and optional Unpaywall. Selection is always manual.": "PDF 检索依次使用 Crossref、Europe PMC、Semantic Scholar 和可选的 Unpaywall；候选文献始终由人工选择。",
    "English topic": "英文检索主题",
    "From year": "起始年份",
    "To year": "截止年份",
    "Results": "结果数量",
    "Unpaywall email (optional)": "Unpaywall 邮箱（可选）",
    "Search Crossref": "检索 Crossref",
    "Download selected": "下载所选文献",
    "Enter a topic to start.": "输入主题后开始检索。",
    "No online search has been run in this workspace.": "当前工作区尚未进行联网检索。",
    "Optional Unpaywall lookup": "可选的 Unpaywall 检索",
    "Add an email to include Unpaywall, or continue without it. Crossref, Europe PMC, and Semantic Scholar will still be tried.": "填写邮箱即可同时使用 Unpaywall，也可以跳过；系统仍会尝试 Crossref、Europe PMC 和 Semantic Scholar。",
    "Continue without Unpaywall": "不使用 Unpaywall 继续",
    "Use email and download": "使用邮箱并下载",
    "Select review project": "选择综述项目",
    "Shared Literature Library": "共享文献库",
    "Enter Discovery and Create Project": "进入检索并创建项目",
    "Opening project creation...": "正在打开项目创建界面…",
    "Delete project": "删除项目",
    "Permanently delete the selected project": "永久删除所选项目",
    "Metadata saved": "Metadata 已保存",
    "PDF batch completed with errors": "PDF 批量处理完成，但存在错误",
    "PDF metadata is ready": "PDF metadata 已就绪",
    "Searching Crossref…": "正在检索 Crossref…",
    "Enter an English literature topic.": "请输入英文文献主题。",
    "Select at least one candidate.": "请至少选择一个候选文献。",
    "Download cancelled.": "下载已取消。",
    "Enter a valid email address.": "请输入有效的邮箱地址。",
    "Discovery Check": "检索审核",
    "New discovery project": "新建检索项目",
    "Change topic": "更换主题",
    "Change topic and restart this project": "更换主题并重新检索当前项目",
    "Run this project's first discovery": "运行当前项目的首次检索",
    "Restart discovery": "重新检索",
    "Cancel topic change": "取消更换主题",
    "Restarting replaces this project's candidate pool and clears Matrix, outlines, drafts, and figure work. PDFs in Library are kept. Existing project data stays unchanged if the new search fails.": "重新检索会替换当前候选池，并清除文献矩阵、大纲、草稿和图像阶段结果。Library 中的 PDF 会保留；如果新检索失败，现有项目不会发生变化。",
    "Enter a new topic. The current project is changed only after Discovery succeeds.": "请输入新主题。只有新检索成功后，当前项目才会被替换。",
    "Replace this project topic and clear all downstream Matrix, outline, draft, and figure work? Library PDFs will be kept.": "确定更换当前项目主题，并清除后续文献矩阵、大纲、草稿和图像结果吗？Library 中的 PDF 会保留。",
    "Restart cancelled. The existing project was not changed.": "已取消重新检索，现有项目未发生变化。",
    "Topic change cancelled. The existing project was not changed.": "已取消更换主题，现有项目未发生变化。",
    "Searching the new topic before replacing the current project...": "正在检索新主题；成功前不会替换当前项目…",
    "Topic changed and Discovery restarted.": "主题已更换，并已重新生成检索结果。",
    "Discovery review controls": "检索审核操作",
    "Save review": "保存审核",
    "Confirm and continue": "确认并继续",
    "Confirm selected papers and continue": "确认所选文献并继续",
    "Clear selected papers": "清空已选文献",
    "Top papers": "前 N 篇",
    "Number of top-ranked local papers": "按相关性排序的本地论文数量",
    "Include top papers in Matrix": "将前 N 篇纳入矩阵",
    "Replaces the current local paper selection": "替换当前本地论文选择",
    "Enter a valid number of papers.": "请输入有效的论文数量。",
    "No local candidate papers are available.": "当前没有可纳入矩阵的本地候选论文。",
    "Keyword groups": "关键词组",
    "Unique papers": "唯一命中文献",
    "Selected papers": "已选文献",
    "Candidate papers": "候选文献",
    "Keyword hits": "关键词命中次数",
    "Library papers": "文献库文章",
    "Paper Selection": "文献选择",
    "Return to Discovery and confirm the current paper selection.": "返回检索阶段并确认当前文献选择。",
    "Loading project...": "正在加载项目…",
    "Loading...": "正在加载…",
    "Project ID": "项目 ID",
    "Verify PDFs, MinerU Markdown, titles, authors, abstracts, eight structured tags, and paths.": "核验 PDF、MinerU Markdown、题名、作者、摘要、八类结构化标签及文件路径。",
    "Remove irrelevant keywords and papers, then confirm the candidate literature set.": "移除无关关键词和论文，然后确认候选文献集合。",
    "Review fixed paper fields, full-reading notes, and the most relevant figure.": "审核论文固定字段、全文阅读笔记和最相关图像。",
    "Confirm sections, claims, assigned papers, visual needs, and writing constraints.": "确认章节、论点、分配论文、图像需求和写作约束。",
    "Review section prose, paper grounding, and paragraph-level figure candidates.": "审核章节正文、论文依据和段落级图像候选。",
    "Select the final source figure for every cited paper before batch redraw.": "批量重绘前，为每篇引用论文选择最终源图。",
    "Verify source resolution and ensure redraws preserve all chemical content.": "核验源图解析，并确保重绘保留全部化学内容。",
    "Review coherence, figure placement, terminology, citations, and remaining issues.": "审核连贯性、图像位置、术语、引文和剩余问题。",
    "Complete the final content, format, reference, figure, and release audit.": "完成最终内容、格式、参考文献、图像和发布审计。",
    "Topic": "主题",
    "Keywords (optional)": "关键词（可选）",
    "Include Crossref results": "包含 Crossref 结果",
    "Run discovery": "运行检索",
    "All centers": "全部中心",
    "General / Review": "综合 / 综述",
    "Transfer / Template": "转移 / 模板",
    "Non-metal": "非金属",
    "Keyword Results": "关键词结果",
    "Toggle keyword keep": "切换关键词保留状态",
    "Paper Detail": "论文详情",
    "Filter keywords": "筛选关键词",
    "comma-separated chemistry terms": "使用逗号分隔化学术语",
    "e.g. syntheses of axial-chiral allenes": "例如：轴手性联烯的合成",
    "No discovery projects found.": "未找到检索项目。",
    "Enter a project ID before running discovery.": "运行检索前请输入项目 ID。",
    "Project ID is required.": "项目 ID 为必填项。",
    "Enter a topic before running discovery.": "运行检索前请输入主题。",
    "Topic is required.": "主题为必填项。",
    "Searching local metadata...": "正在检索本地 metadata…",
    "Discovery complete": "检索完成",
    "Discovery failed.": "检索失败。",
    "Select a paper result.": "请选择一篇论文结果。",
    "Select a local or web paper to inspect.": "请选择一篇本地或网络论文查看详情。",
    "No local Markdown for web result.": "网络结果暂无本地 Markdown。",
    "Saved": "已保存",
    "Could not save Discovery.": "无法保存检索结果。",
    "Literature Matrix": "文献矩阵",
    "Paper Detail": "论文详情",
    "Paper": "论文",
    "Outline": "大纲",
    "Selected Outline": "已选大纲",
    "Filter papers": "筛选论文",
    "Reading progress is optional; select an outline before Blueprint.": "全文阅读进度为可选项；进入蓝图前必须选择大纲。",
    "No papers match the filter.": "没有论文符合筛选条件。",
    "Confirm Discovery to populate the Matrix.": "确认检索结果后即可填充文献矩阵。",
    "Authors unavailable": "作者信息不可用",
    "Year unavailable": "年份不可用",
    "Journal unavailable": "期刊信息不可用",
    "Abstract": "摘要",
    "Full-Paper Reading Note": "全文阅读笔记",
    "Edit reading note": "编辑阅读笔记",
    "Most Relevant Figure": "最相关图像",
    "Source:": "来源：",
    "Page:": "页码：",
    "Caption:": "图注：",
    "Relevance:": "相关性：",
    "Not specified": "未指定",
    "I completed the full-paper reading for this paper.": "我已完成该论文的全文阅读。",
    "Save Reading Note": "保存阅读笔记",
    "Outline Options": "大纲选项",
    "Select an outline to convert the current Matrix into a Blueprint.": "选择一个大纲，将当前文献矩阵转换为写作蓝图。",
    "Upload Reference Review": "上传参考综述",
    "No outline options yet.": "暂无大纲选项。",
    "Selected for the current Matrix": "已用于当前矩阵",
    "Choose an outline for the current Matrix": "请为当前矩阵选择大纲",
    "No current outline selected.": "当前尚未选择大纲。",
    "Full Reading": "全文阅读",
    "Next Gate": "下一门控",
    "Saving...": "正在保存…",
    "Saving outline...": "正在保存大纲…",
    "Uploading reference...": "正在上传参考综述…",
    "Section Blueprint": "章节蓝图",
    "Blueprint Detail": "蓝图详情",
    "Section": "章节",
    "Writing Plan": "写作计划",
    "Generate Section Tasks and Enter Sections": "生成章节任务并进入分节阶段",
    "Review Gate": "审核门控",
    "Confirm every section has papers, claims, and figure/table needs.": "确认每个章节都包含论文、论点以及图表需求。",
    "No section blueprint found.": "未找到章节蓝图。",
    "Untitled": "未命名",
    "Ready for section drafting": "可开始章节写作",
    "Needs blueprint check": "需要检查蓝图",
    "Incomplete Sections": "不完整章节",
    "Human Check": "人工检查",
    "Creating section tasks from Blueprint...": "正在根据蓝图创建章节任务…",
    "Section Thesis": "章节论点",
    "Target Paragraphs": "目标段落数",
    "Target Length": "目标篇幅",
    "Review Question": "综述问题",
    "Organizing Logic": "组织逻辑",
    "Core Papers": "核心论文",
    "Claims to Establish": "待建立论点",
    "Figure and Table Needs": "图表需求",
    "Depth Requirements": "深度要求",
    "Writing Guardrails": "写作约束",
    "Section Transition": "章节衔接",
    "Claim Id": "论点编号",
    "Claim Type": "论点类型",
    "Logic Relationship": "逻辑关系",
    "Evidence Strength": "证据强度",
    "Supporting Papers": "支撑论文",
    "Comparison Axes": "比较维度",
    "Wording Constraints": "措辞约束",
    "Candidate Papers": "候选论文",
    "From Previous": "承接上一节",
    "To Next": "引出下一节",
    "Type": "类型",
    "paragraphs": "段",
    "words": "词",
    "papers": "论文",
    "figures": "图表",
    "Introduction": "简介",
    "Cross-category comparison and conclusion": "跨类别比较与结论",
    "Other or unspecified": "其他或未指定",
    "propargylic alcohols": "炔丙醇",
    "terminal alkynes": "末端炔烃",
    "propargylic acetates": "炔丙基乙酸酯",
    "allenic acids and esters": "累积二烯酸及其酯",
    "ketones": "酮",
    "allenic alcohols": "累积二烯醇",
    "reaction_type": "反应类型",
    "foundation": "基础论点",
    "extension": "扩展论点",
    "foundation_to_extension": "从基础到扩展",
    "limitation_repair": "局限性修正",
    "needs verification": "需要核验",
    "scheme": "反应示意图",
    "Thesis:": "章节论点：",
    "Major papers:": "核心论文：",
    "Claims:": "论点：",
    "Figure/table need:": "图表需求：",
    "Supporting Papers:": "支撑论文：",
    "Comparison Axes:": "比较维度：",
    "Wording Constraints:": "措辞约束：",
    "Section Drafts": "章节草稿",
    "Section Preview": "章节预览",
    "All Drafts": "全部草稿",
    "Section Draft": "章节正文",
    "Merged Preview": "合并预览",
    "Writing Requirements": "写作要求",
    "Section workspace": "章节工作区",
    "Core Argument": "核心论点",
    "Assigned Papers": "分配文献",
    "Must Cover": "必写要点",
    "Figure Requirements": "图表要求",
    "No figure requirements specified.": "未指定图表要求。",
    "No required points specified.": "未指定必写要点。",
    "No writing guardrails specified.": "未指定写作约束。",
    "No core argument specified.": "未指定核心论点。",
    "No writing requirements are available. Return to Blueprint and execute the handoff.": "没有可用的写作要求，请返回蓝图阶段并执行交接。",
    "Section draft ready for review.": "章节草稿已可审核。",
    "Stage 4 handoff time is unavailable.": "未记录第四阶段交接时间。",
    "Stage 4 Blueprint changed. Old section drafts remain on disk but are hidden because they do not match the current Blueprint. Regenerate section drafts from the current writing requirements.": "第四阶段蓝图已更新。旧章节草稿仍保留在磁盘中，但由于与当前蓝图不一致，暂不显示。请根据当前写作要求重新生成章节草稿。",
    "Stage 4 Blueprint changed. Regenerate section drafts from the current writing requirements.": "第四阶段蓝图已更新，请根据当前写作要求重新生成章节草稿。",
    "Check section prose and whether each important paragraph has a figure candidate.": "检查章节正文，并确认每个重要段落是否具有图像候选。",
    "Section Files": "章节文件",
    "Figure Candidates": "图像候选",
    "Unresolved Source Images": "未解析的源图",
    "No Useful Figure Notes": "无可用图像的说明",
    "Ready for figure check": "可开始图像检查",
    "Needs section/figure check": "需要检查章节或图像",
    "Cited Papers": "引用论文",
    "Select a paper": "选择论文",
    "Selection Note": "选择说明",
    "This choice becomes the only input to the batch redraw.": "该选择将成为批量重绘的唯一输入。",
    "Reason for selecting this figure": "选择此图的原因",
    "Run Sections to produce figure candidates.": "请先运行分节阶段以生成图像候选。",
    "Section outputs changed. Regenerate candidates before reviewing figures.": "章节产物已变化，请重新生成候选图后再审核。",
    "No candidate figures are available.": "没有可用的候选图。",
    "Select a candidate.": "请选择一个候选图。",
    "Image path unavailable.": "图像路径不可用。",
    "Selected": "已选择",
    "Use this candidate": "使用此候选图",
    "Candidate saved. Execute Figure Review when all papers are selected.": "候选图已保存；全部论文选择完成后执行图像候选审核。",
    "Figure Redraw": "图像重绘",
    "Figure Check": "图像检查",
    "Figure": "图像",
    "Image Preview": "图像预览",
    "Redraw Report": "重绘报告",
    "Redraw": "重绘",
    "Do not proceed unless source image and redrawn output are both verified.": "只有源图与重绘结果都核验通过后才能继续。",
    "Status": "状态",
    "Candidates": "候选图",
    "Manuscript Selected": "正文已选",
    "Source Images Resolved": "源图已解析",
    "Current Redrawn Outputs": "当前重绘产物",
    "Needs Human Check": "需要人工检查",
    "Required Check": "必要检查",
    "Source Candidate": "源图候选",
    "Redrawn Output": "重绘结果",
    "Why selected": "选择原因",
    "What it shows": "图像内容",
    "Claim fit": "论点匹配",
    "Caption": "图注",
    "Recommended action": "建议操作",
    "Source PDF": "来源 PDF",
    "Project Draft": "项目初稿",
    "Save Draft": "保存初稿",
    "Draft Preview": "初稿预览",
    "Full-text Edit": "全文编辑",
    "Review Checks": "检查与问题",
    "Read the composed manuscript and edit individual paragraphs in context.": "阅读合并后的正文，并在上下文中编辑单个段落。",
    "Edit the complete Markdown source when structure or multiple paragraphs must change together.": "需要调整结构或同时修改多个段落时，编辑完整 Markdown 源文。",
    "Review merge notes and unresolved human checks together before Final.": "进入最终生成前，集中检查合并说明和待人工确认事项。",
    "Reviewed figures are available to the current draft.": "已审核图像可供当前初稿使用。",
    "Read the manuscript and edit individual paragraphs in context.": "阅读正文并在上下文中编辑单个段落。",
    "Edit and save the complete Markdown source.": "编辑并保存完整 Markdown 源文。",
    "Merge evidence and open human-review items before Final.": "进入最终生成前检查合并依据和待人工确认事项。",
    "Merge notes and remaining issues are read-only evidence, so they are grouped here. Correct manuscript text in Draft Preview or Full-text Edit.": "合并说明和剩余问题都是只读检查依据，因此集中显示在这里；正文修改请在“初稿预览”或“全文编辑”中完成。",
    "Merge and normalization": "合并与规范化",
    "report available": "已有报告",
    "no report": "无报告",
    "Confirms how section text, transitions, terminology, citations, and figures were assembled. It is not a second editable draft.": "用于确认章节正文、过渡、术语、引用和图像如何合并；它不是第二份可编辑初稿。",
    "Human review items": "人工确认事项",
    "review required": "需要确认",
    "none recorded": "未记录问题",
    "Check scientific interpretation, scope boundaries, citations, and every retained figure before continuing to Final.": "进入最终生成前，请核对科学解释、范围边界、引用以及每一张保留图像。",
    "Resolve integrity failures and approve the exact draft version before Final.": "进入最终生成前，请解决完整性问题并确认精确的初稿版本。",
    "Confirm merge evidence, hard integrity checks, and the exact approved Draft version before Final.": "进入最终生成前，请确认合并依据、硬性完整性检查和精确的已确认初稿版本。",
    "Merge Report": "合并报告",
    "Issues": "问题",
    "Project Status": "项目状态",
    "Same project across discovery, drafting, redraw, and merge.": "检索、写作、重绘与合并阶段使用同一个项目。",
    "Final Output": "最终产物",
    "Conclusion": "结论",
    "Overview Figure": "综述总览图",
    "Final Draft": "最终稿",
    "Audit": "审计",
    "Release": "发布",
    "Independent generation": "独立生成",
    "Generate Conclusion (Optional)": "生成结论（可选）",
    "Generate Overview Figure": "生成综述总览图",
    "Generate Final Draft": "生成最终稿",
    "Generate & Download Word": "生成并下载 Word",
    "Final generation actions": "最终生成操作",
    "This file has not been generated.": "该文件尚未生成。",
    "Generate the overview figure to view the template-matched visual.": "生成综述总览图后即可查看与模板匹配的图像。",
    "Overall review outline.": "综述全文大纲图。",
    "Upstream content changed. Regenerate the final draft first.": "上游内容已变化，请先重新生成最终稿。",
    "Select a project before generating outputs.": "生成产物前请选择项目。",
    "Generating…": "正在生成…",
    "Generation complete.": "生成完成。",
    "Select a project before exporting.": "导出前请选择项目。",
    "Generating Word document…": "正在生成 Word 文档…",
    "Word export failed.": "Word 导出失败。",
    "Word document generated. Download started.": "Word 文档已生成，下载已开始。",
    "Download DOCX": "下载 DOCX",
    "The existing Word file is out of date. Use Generate & Download Word.": "现有 Word 文件已过期，请使用“生成并下载 Word”。",
    "ready": "就绪",
    "not ready": "未就绪",
    "present": "存在",
    "missing": "缺失",
    "selected": "已选择",
    "needs selection": "需要选择",
    "reviewed": "已审核",
    "needs check": "需要检查",
    "full reading complete": "全文阅读已完成",
    "needs full reading": "需要全文阅读",
    "No safe runnable action is available.": "当前没有可安全运行的操作。",
    "Select a project before continuing.": "继续前请选择项目。",
    "Generating stage outputs...": "正在生成阶段产物…",
    "Final stage recorded.": "最终阶段已记录。",
    "Project ID did not match. Nothing was deleted.": "项目 ID 不匹配，未删除任何内容。",
    "Blueprint Tasks": "Blueprint 任务",
    "Transferred Blueprint Tasks": "已传入的 Blueprint 任务",
    "The current Blueprint has no section tasks to transfer.": "当前 Blueprint 没有可传入的章节任务。",
    "No section files are available. Review Blueprint tasks, then generate drafts.": "没有章节文件。请先查看 Blueprint 任务后再生成草稿。",
    "The Blueprint changed. Existing section drafts remain on disk but are hidden because they do not match the current Blueprint. Generate new section drafts from the tasks below.": "Blueprint 已更新。旧章节草稿仍保留在磁盘中，但由于与当前 Blueprint 不一致，暂不显示。请根据下方任务生成新的章节草稿。",
    "No tasks are available. Return to Blueprint and execute the handoff.": "没有可用任务。请返回 Blueprint 并执行交接。",
    "New Blueprint tasks are ready. Generate section drafts from them.": "新的 Blueprint 任务已就绪，请据此生成章节草稿。",
    "stale": "已过期",
    "Core argument:": "核心论点：",
    "Assigned papers:": "分配文献：",
    "Required points:": "必写要点：",
    "Figure/table needs:": "图表需求：",
    "Not assigned": "未分配",
    "Figure candidates are stale": "图候选已过期",
    "The Blueprint changed. Regenerate section drafts and figure candidates first; old candidates and redraws will not be used in the current workflow.": "Blueprint 已更新。请先重新生成章节草稿和图候选，旧图候选与重绘结果不会用于当前流程。",
    "Edit base image": "编辑底图",
    "Source image": "原图",
    "AI redrawn image": "AI 重绘图",
    "Redraw current chemical figure": "AI 重绘当前化学结构",
    "Generate AI comparison version (preserve provider canvas)": "生成 AI 对照版本（保留服务商画布）",
    "Online SVG editor": "在线编辑 SVG",
    "Download editable SVG": "下载可编辑 SVG",
    "Human review approved": "人工审核已通过",
    "Awaiting human review": "等待人工审核",
    "Approve after human review": "人工审核通过，允许进入正文",
    "Approve all": "全部通过",
    "Only current successful redraws will be approved. Failed, active, stale, or missing outputs remain unchanged.": "只会批准当前已成功生成的重绘图；生成失败、生成中、已过期或缺少输出的图保持原状态。",
    "No successful redraw is awaiting approval.": "当前没有等待人工审核的成功重绘图。",
    "This output failed automatic integrity validation and is currently view/download only. Check every chemical structure, bond, label, arrow, process, and layout item before manually approving it for the manuscript.": "未通过自动完整性校验：此图目前仅供查看和下载。请逐项核对化学结构、化学键、文字、箭头、流程和版式；确认正确后可人工批准进入正文。",
    "Human review approved this output for the manuscript; approval is bound to the current source candidate and output file.": "已由人工审核批准进入正文；批准记录绑定当前候选原图和此输出文件。",
    "Drawing tools": "绘制工具",
    "Choose a canvas operation": "选择一种画布操作",
    "Tool settings": "工具参数",
    "Only settings for the active tool are shown": "只显示当前工具需要的设置",
    "Content & structure": "内容与结构",
    "Text and chemical structures": "文本和化学结构",
    "Edit actions": "编辑操作",
    "Select, delete, and undo": "选择、删除和撤回",
    "File actions": "文件操作",
    "Save the current result or exit": "保存当前结果或退出",
    "Select / Move": "选择 / 移动",
    "Marquee": "框选",
    "Eraser": "橡皮擦",
    "Text box": "文本框",
    "Line": "直线",
    "Arrow": "箭头",
    "Add chemical structure": "添加化学结构",
    "Edit selected structure": "编辑所选结构",
    "Apply text style": "应用文本样式",
    "Cancel current arrow": "取消当前箭头",
    "Delete selected": "删除所选",
    "Undo": "撤回一步",
    "Download SVG": "下载 SVG",
    "Save edits": "保存编辑",
    "Close editor": "关闭编辑器",
    "Insert": "插入",
    "History": "历史",
    "Delete": "删除",
    "Include in Matrix": "纳入文献矩阵",
    "Exclude from Matrix": "不纳入文献矩阵",
    "Remove from Matrix": "移出文献矩阵",
    "Included in Matrix": "已纳入文献矩阵",
    "Keep as external reference": "保留为外部参考文献",
    "Kept as external reference": "已保留为外部参考文献",
    "Remove external reference": "移除外部参考文献",
    "Viewing": "正在查看",
    "Paragraph saved": "段落已保存",
    "The upstream sections or reviewed figures changed. Regenerate the first draft.": "上游章节或审核图片已更新，请先重新生成初稿。",
    "The first draft is stale": "初稿已过期",
    "The current Blueprint, sections, or reviewed figures changed.": "当前 Blueprint、章节或审核图片已经更新。",
    "Regenerate the first draft. The previous draft remains on disk but will not be displayed or saved as current workflow content.": "请重新生成初稿。旧初稿保留在磁盘中，但不会作为当前流程内容显示或保存。",
    "Per-paper review": "逐篇论文审核",
    "Manual candidate replacement": "手动替换候选图",
    "Overall overview figure": "综述全文总览图",
    "Redraw all with AI": "全部 AI 重绘",
    "Call AI one figure at a time. The job continues in the background after navigating away.": "逐张顺序调用 AI，切换页面后任务仍在后台继续。",
    "Stop all generation": "全部停止生成",
    "Redrawing all with AI…": "全部 AI 重绘中…",
    "Stopping…": "正在停止…",
    "Redrawn Output — Human review approved": "重绘结果 — 人工审核已通过",
    "AI Preview — Awaiting human review": "AI 预览 — 等待人工审核",
    "Ready after human image verification": "人工核验图像后可继续",
    "1. Source images must genuinely come from the corresponding papers.\n2. Redrawing may change only visual style, never reactions, conditions, products, or mechanisms.\n3. Candidates without a located source image cannot enter the final draft.": "1. 源图必须真实来自对应论文。\n2. 重绘图只能改变视觉风格，不能改变反应、条件、产物和机理。\n3. 未定位源图的候选不能进入终稿。",
    "1. Every section must be complete review prose, not an outline.\n2. Every paragraph should center on one paper's work.\n3. Figure candidates must genuinely correspond to paragraph content.\n4. Unlocated images must be traced back to the source paper.": "1. 每节是否是完整综述段落，而不是提纲。\n2. 每段是否围绕一篇文献工作展开。\n3. 图候选是否真实对应段落内容。\n4. 未定位的图片是否已回到原文定位。",
    "1. Every section must have a clear thesis.\n2. major_papers must be sufficient and correctly assigned.\n3. review_claims must support paragraph writing.\n4. figure_or_table_needs must be explicit.": "1. 每节是否有明确 thesis。\n2. major_papers 是否足够且无明显错配。\n3. review_claims 是否能支撑段落写作。\n4. figure_or_table_needs 是否明确。",
    "Click an object to select and drag it; arrow endpoints appear as adjustable control points.": "点击对象进行选择并拖动；箭头端点会显示为可调整控制点。",
    "Drag a selection box on the canvas to select, move, or delete multiple objects.": "在画布拖出选择框，可一次选择并移动或删除多个对象。",
    "Erase only the original layer; text, lines, arrows, and structures inserted later remain above it.": "仅擦除原始图层；后来插入的文字、线条、箭头和结构不会被覆盖。",
    "Drag a text box on the canvas and enter content; clicking outside applies it, and clicking the text again edits it.": "在画布拖出文本框并输入内容；点击外部自动应用，再点文字可重新编辑。",
    "Press and drag on the canvas to set the line start and end points.": "在画布按下并拖动确定直线起点和终点。",
    "Choose an arrow style, click the canvas to insert it, then drag endpoints or control points to adjust the path.": "选择箭头样式后点击画布插入，并拖动终点或控制点调整路径。",
    "The selected line was deleted.": "所选直线已删除。",
    "Drag to set the line endpoint; a click creates a default-length horizontal line.": "拖动以确定直线终点；直接单击会生成一条默认长度的水平直线。",
    "Line added; select it to delete, or press Ctrl+Z to undo.": "直线已添加；可选择后删除，或按 Ctrl+Z 撤回。",
    "Existing SVG edit operations loaded.": "已加载已有 SVG 编辑操作。",
    "The eraser affects only the original layer; text, lines, arrows, and structures inserted later remain above it.": "橡皮擦只擦除原始图层；之后插入的文本、直线、箭头和结构会保持在上方。",
    "redrawn": "已重绘"
  });

  const NAMED = Object.freeze({
    deletePrompt: {
      en: "Type {projectId} to permanently delete this project and all of its outputs.",
      "zh-CN": "输入 {projectId} 以永久删除该项目及其全部产物。"
    },
    serverReturned: {
      en: "Server returned {status}.",
      "zh-CN": "服务器返回 {status}。"
    }
  });

  const ZH_EN = Object.freeze(Object.fromEntries(
    Object.entries(EN_ZH).map(([english, chinese]) => [chinese, english])
  ));

  const PATTERNS = [
    [/^(\d+)\/(\d+) selected manuscript figures are usable; (\d+) still need redraw or approval\.$/, (m) => `正文已选图像中 ${m[1]}/${m[2]} 张可用，还有 ${m[3]} 张需要重绘或人工审核。`],
    [/^All (\d+) selected manuscript figures are usable\. You can enter Draft\.$/, (m) => `全部 ${m[1]} 张正文已选图像均可用，可以进入初稿。`],
    [/^Execute (.+) and Enter (.+)$/, (m) => `执行“${toChinese(m[1])}”并进入“${toChinese(m[2])}”`],
    [/^Select at most (\d+) PDFs per batch\.$/, (m) => `每批最多选择 ${m[1]} 个 PDF。`],
    [/^(.+) is not a PDF\.$/, (m) => `${m[1]} 不是 PDF 文件。`],
    [/^Uploading and extracting metadata (\d+)\/(\d+): (.+)$/, (m) => `正在上传并提取 metadata ${m[1]}/${m[2]}：${m[3]}`],
    [/^Batch complete: (\d+) added, (\d+) duplicate, (\d+) failed\.(.*)$/, (m) => `批量处理完成：新增 ${m[1]}，重复 ${m[2]}，失败 ${m[3]}。${m[4]}`],
    [/^(\d+) PDF\(s\) need OCR\.$/, (m) => `${m[1]} 个 PDF 需要 OCR。`],
    [/^First error: (.+)$/, (m) => `首个错误：${m[1]}`],
    [/^Search failed: (.+)$/, (m) => `检索失败：${m[1]}`],
    [/^Found (\d+) ranked candidates\. Select papers to download\.$/, (m) => `找到 ${m[1]} 个排序候选，请选择要下载的论文。`],
    [/^Downloading (\d+)\/(\d+)(.*)$/, (m) => `正在下载 ${m[1]}/${m[2]}${m[3]}`],
    [/^Download job failed: (.+)$/, (m) => `下载任务失败：${m[1]}`],
    [/^Download finished: (\d+) added\/already present, (\d+) unavailable or failed\.$/, (m) => `下载完成：新增或已存在 ${m[1]}，不可用或失败 ${m[2]}。`],
    [/^Project (.+) is ready: (\d+) local matches and (\d+) external matches across (\d+) keyword groups\.$/, (m) => `项目 ${m[1]} 已就绪：${m[4]} 个关键词组中有 ${m[2]} 个本地匹配和 ${m[3]} 个外部匹配。`],
    [/^Project (.+) is ready: found (\d+) candidate papers \((\d+) keyword hits\) across (\d+) keyword groups\. Selected for Matrix: (\d+); include candidates to build the selection\.$/, (m) => `项目 ${m[1]} 已就绪：检索命中 ${m[3]} 次、候选池 ${m[2]} 篇文献，共 ${m[4]} 个关键词组；已选入 Matrix ${m[5]} 篇，请勾选候选文献以构建选择。`],
    [/^Project (.+) is ready: found (\d+) candidate papers \((\d+) keyword hits\) and (\d+) unique external results across (\d+) keyword groups\. Selected for Matrix: (\d+); include candidates to build the selection\.$/, (m) => `项目 ${m[1]} 已就绪：检索命中 ${m[3]} 次、候选池 ${m[2]} 篇文献、${m[4]} 条唯一外部结果，共 ${m[5]} 个关键词组；已选入 Matrix ${m[6]} 篇，请勾选候选文献以构建选择。`],
    [/^(\d+) active$/, (m) => `${m[1]} 个有效关键词组`],
    [/^(\d+) hits$/, (m) => `${m[1]} 次命中`],
    [/^(\d+) selected$/, (m) => `${m[1]} 篇已选`],
    [/^Table candidates without a located source image cannot be passed to the figure redraw skill\. Select a candidate with an available image\.$/, () => '未定位到源图的表格候选不能进入图像重绘流程，请选择有可用图像的候选。'],
    [/^(P\d+) needs a candidate with a located source image before it can be redrawn\.$/, (m) => `${m[1]} 需要先选择一个已定位到源图的候选，才能进行重绘。`],
    [/^Selected top (\d+) of (\d+) candidate papers for Matrix\.$/, (m) => `已将按相关性排序的前 ${m[1]} 篇纳入矩阵候选（共 ${m[2]} 篇）。`],
    [/^Discovery confirmed\. (\d+) selected papers were synchronized to Matrix\.$/, (m) => `检索结果已确认，${m[1]} 篇所选论文已同步到矩阵。`],
    [/^(\d+) selected · synchronized$/, (m) => `已选择 ${m[1]} 篇 · 已同步`],
    [/^(\d+) selected · reconfirm in Discovery$/, (m) => `已选择 ${m[1]} 篇 · 请在检索阶段重新确认`],
    [/^(\d+)\/(\d+) papers selected( · selection set needs revalidation)?$/, (m) => `${m[1]}/${m[2]} 篇论文已选择${m[3] ? " · 选择集合需要重新验证" : ""}`],
    [/^(\d+) candidates\. Choose one source for batch redraw\.$/, (m) => `${m[1]} 个候选图；请选择一个作为批量重绘源图。`],
    [/^Candidate (\d+)$/, (m) => `候选图 ${m[1]}`],
    [/^Saved (.+)$/, (m) => `已保存 ${m[1]}`],
    [/^(\d+) words$/, (m) => `${m[1]} 词`],
    [/^(\d+) core papers · (\d+) review claims$/, (m) => `${m[1]} 篇核心论文 · ${m[2]} 个综述论点`],
    [/^(\d+) section writing requirements$/, (m) => `${m[1]} 个章节写作要求`],
    [/^(\d+) section drafts$/, (m) => `${m[1]} 个章节草稿`],
    [/^Transferred from Analysis & Planning at (.+)$/, (m) => `文献分析与写作规划交接时间：${m[1]}`],
    [/^(sec\d+) · (\d+) words$/, (m) => `${m[1]} · ${m[2]} 词`],
    [/^(sec\d+) · (.+)$/, (m) => `${m[1]} · ${toChinese(m[2])}`],
    [/^(sec\d+)\. (.+)$/, (m) => `${m[1]}. ${toChinese(m[2])}`],
    [/^(\d+)\. (.+)$/, (m) => `${m[1]}. ${toChinese(m[2])}`],
    [/^Thesis: (.+)$/, (m) => `章节论点：${m[1]}`],
    [/^Major papers: (.+)$/, (m) => `核心论文：${m[1]}`],
    [/^Claim Id: (.+)$/, (m) => `论点编号：${m[1]}`],
    [/^Claim Type: (.+)$/, (m) => `论点类型：${toChinese(m[1])}`],
    [/^Logic Relationship: (.+)$/, (m) => `逻辑关系：${toChinese(m[1])}`],
    [/^Evidence Strength: (.+)$/, (m) => `证据强度：${toChinese(m[1])}`],
    [/^Candidate Papers: (.+)$/, (m) => `候选论文：${m[1]}`],
    [/^Type: (.+)$/, (m) => `类型：${toChinese(m[1])}`],
    [/^(.+) Papers: (.+)$/, (m) => `${m[1]} 论文：${m[2]}`],
    [/^Figure\/table need: ([^-]+) - (.+)$/, (m) => `图表需求：${toChinese(m[1].trim())} - ${m[2]}`],
    [/^missing (.+)$/, (m) => `缺少 ${m[1].split(',').map(value => toChinese(value.trim())).join('、')}`],
    [/^Server returned (\d+)\.$/, (m) => `服务器返回 ${m[1]}。`]
  ];

  const ZH_PATTERNS = [
    [/^(\d+) 篇文献$/, (m) => `${m[1]} papers`],
    [/^Blueprint 任务交接完成时间：(.+)。$/, (m) => `Blueprint task handoff completed at ${m[1]}.`],
    [/^已记录 (\d+) 个路径点；至少 2 点后点击“完成当前箭头”。$/, (m) => `${m[1]} path points recorded; after at least 2 points, click Finish current arrow.`],
    [/^AI 重绘进度：(\d+\/\d+)（(.+)）(.*)$/, (m) => `AI redraw progress: ${m[1]} (${m[2]})${m[3]}`],
    [/^当前：(.+)$/, (m) => `Current: ${m[1]}`],
    [/^成功 (\d+)，失败 (\d+)$/, (m) => `Succeeded ${m[1]}, failed ${m[2]}`],
    [/^当前有 (\d+) 张重绘图未通过最新的来源、尺寸或完整性规则；请逐张检查并重新重绘。$/, (m) => `${m[1]} redraws fail the latest source, size, or integrity rules; inspect and redraw them individually.`],
    [/^候选源图审核结果已载入；(\d+)\/(\d+) 个重绘结果与当前选图匹配，其余需要重新重绘。$/, (m) => `Source-figure review is loaded; ${m[1]}/${m[2]} redraws match the current selections, and the rest must be redrawn.`],
    [/^画布比例不匹配：原图 (.+)，重绘图 (.+)。该旧输出不能进入正文，请重新 AI 重绘。$/, (m) => `Canvas aspect ratio mismatch: source ${m[1]}, redraw ${m[2]}. This old output cannot enter the manuscript; redraw it with AI.`],
    [/^服务器返回 (\d+)。$/, (m) => `Server returned ${m[1]}.`]
  ];

  let language = readInitialLanguage();
  const textSources = new WeakMap();
  const attributeSources = new WeakMap();

  function readInitialLanguage() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (SUPPORTED.has(stored)) return stored;
    } catch (_) {}
    return String(navigator.language || "").toLowerCase().startsWith("zh") ? "zh-CN" : "en";
  }

  function toChinese(value) {
    const text = String(value || "").trim();
    if (EN_ZH[text]) return EN_ZH[text];
    for (const [pattern, render] of PATTERNS) {
      const match = text.match(pattern);
      if (match) return render(match);
    }
    return text;
  }

  function toEnglish(value) {
    const text = String(value || "").trim();
    if (ZH_EN[text]) return ZH_EN[text];
    for (const [pattern, render] of ZH_PATTERNS) {
      const match = text.match(pattern);
      if (match) return render(match);
    }
    return text;
  }

  function translateCore(value, targetLanguage) {
    const text = String(value || "");
    const match = text.match(/^(\s*)([\s\S]*?)(\s*)$/);
    const prefix = match ? match[1] : "";
    const core = match ? match[2] : text;
    const suffix = match ? match[3] : "";
    if (!core) return text;
    const translated = targetLanguage === "zh-CN"
      ? toChinese(core)
      : toEnglish(core);
    return prefix + translated + suffix;
  }

  function message(key, params) {
    const template = NAMED[key]?.[language] || NAMED[key]?.en || key;
    return String(template).replace(/\{(\w+)\}/g, (_, name) => String(params?.[name] ?? ""));
  }

  function shouldSkip(node) {
    const element = node.nodeType === 1 ? node : node.parentElement;
    if (!element) return true;
    return Boolean(element.closest(
      "script, style, pre, code, textarea, [contenteditable='true'], [data-i18n-skip], .markdown, .draft-view, .article-content"
    ));
  }

  function processTextNode(node) {
    if (!node || node.nodeType !== Node.TEXT_NODE || shouldSkip(node) || !node.nodeValue?.trim()) return;
    const raw = node.nodeValue;
    let source = textSources.get(node);
    if (source === undefined) {
      source = raw;
      textSources.set(node, source);
    } else {
      const expected = translateCore(source, language);
      if (raw !== source && raw !== expected) {
        source = raw;
        textSources.set(node, source);
      }
    }
    const target = translateCore(source, language);
    if (node.nodeValue !== target) node.nodeValue = target;
  }

  function processAttributes(element) {
    if (!element || element.nodeType !== Node.ELEMENT_NODE || shouldSkip(element)) return;
    const names = ["placeholder", "title", "aria-label"];
    let sources = attributeSources.get(element);
    if (!sources) {
      sources = {};
      attributeSources.set(element, sources);
    }
    for (const name of names) {
      if (!element.hasAttribute(name)) continue;
      const raw = element.getAttribute(name) || "";
      if (!(name in sources)) sources[name] = raw;
      else {
        const expected = translateCore(sources[name], language);
        if (raw !== sources[name] && raw !== expected) sources[name] = raw;
      }
      const target = translateCore(sources[name], language);
      if (raw !== target) element.setAttribute(name, target);
    }
  }

  function processTree(root) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) {
      processTextNode(root);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE && root.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
    if (root.nodeType === Node.ELEMENT_NODE) processAttributes(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeType === Node.TEXT_NODE) processTextNode(node);
      else processAttributes(node);
    }
  }

  function updateSwitch() {
    document.querySelectorAll(".rw-language-option").forEach((button) => {
      const active = button.dataset.language === language;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function applyLanguage() {
    document.documentElement.lang = language;
    document.body?.classList.toggle("rw-lang-zh", language === "zh-CN");
    if (!textSources.has(document.documentElement)) {
      textSources.set(document.documentElement, document.title);
    }
    const sourceTitle = textSources.get(document.documentElement) || document.title;
    document.title = translateCore(sourceTitle, language);
    processTree(document.body);
    updateSwitch();
  }

  function setLanguage(nextLanguage) {
    if (!SUPPORTED.has(nextLanguage) || nextLanguage === language) return;
    language = nextLanguage;
    try { localStorage.setItem(STORAGE_KEY, language); } catch (_) {}
    applyLanguage();
    window.dispatchEvent(new CustomEvent("review-language-change", { detail: { language } }));
  }

  function mountSwitch() {
    const nav = document.querySelector(".nav");
    if (!nav || nav.querySelector(".rw-language-switch")) return;
    const control = document.createElement("div");
    control.className = "rw-language-switch";
    control.setAttribute("role", "group");
    control.setAttribute("aria-label", "Language / 语言");
    control.setAttribute("data-i18n-skip", "");
    control.innerHTML = `
      <span class="rw-language-mark" aria-hidden="true">文/A</span>
      <button type="button" class="rw-language-option" data-language="zh-CN" aria-pressed="false">中文</button>
      <button type="button" class="rw-language-option" data-language="en" aria-pressed="false">EN</button>
    `;
    const navRight = nav.querySelector(".nav-right");
    if (navRight) nav.insertBefore(control, navRight);
    else nav.appendChild(control);
    control.addEventListener("click", (event) => {
      const button = event.target.closest("[data-language]");
      if (button) setLanguage(button.dataset.language);
    });
  }

  function mountSettingsLink() {
    const nav = document.querySelector(".nav");
    const navRight = document.querySelector(".nav-right");
    if (!nav) return;
    if (!document.querySelector("#rw-settings-shortcut-style")) {
      const style = document.createElement("style");
      style.id = "rw-settings-shortcut-style";
      style.textContent = `
        .nav .rw-settings-shortcut{display:inline-flex!important;align-items:center;gap:5px;flex:0 0 auto;padding:7px 10px;border:1px solid rgba(29,102,85,.22);border-radius:8px;background:#fffdf7;color:#1d6655;text-decoration:none;font-size:12px;font-weight:700;white-space:nowrap}
        .nav .rw-settings-shortcut.active{color:#fff;background:#1d6655;border-color:#1d6655}
        @media(max-width:720px){.nav .rw-settings-shortcut{position:absolute;top:9px;right:116px;padding:6px 8px}}
      `;
      document.head.appendChild(style);
    }
    const onSettingsPage = window.location.pathname === "/settings";
    const currentLocation = window.location.pathname + window.location.search;
    const settingsHref = onSettingsPage ? "/settings" : `/settings?return=${encodeURIComponent(currentLocation)}`;
    if (!onSettingsPage && !nav.querySelector(".rw-settings-shortcut")) {
      const shortcut = document.createElement("a");
      shortcut.className = "rw-settings-shortcut";
      shortcut.href = settingsHref;
      shortcut.textContent = "⚙ Settings";
      shortcut.title = "API Provider Settings";
      if (navRight) nav.insertBefore(shortcut, navRight);
      else nav.appendChild(shortcut);
    }
    if (navRight && !navRight.querySelector('a[href="/settings"]')) {
      const link = document.createElement("a");
      link.href = settingsHref;
      link.textContent = "Settings";
      if (window.location.pathname === "/settings") link.classList.add("active");
      navRight.appendChild(link);
    }
  }

  function observeDynamicUi() {
    const observer = new MutationObserver((records) => {
      const roots = new Set();
      for (const record of records) {
        if (record.type === "characterData") roots.add(record.target);
        if (record.type === "attributes") roots.add(record.target);
        for (const node of record.addedNodes || []) roots.add(node);
      }
      roots.forEach(processTree);
    });
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["placeholder", "title", "aria-label"]
    });
  }

  function init() {
    mountSettingsLink();
    mountSwitch();
    applyLanguage();
    observeDynamicUi();
  }

  window.reviewI18n = {
    getLanguage: () => language,
    setLanguage,
    t: (value) => translateCore(value, language),
    message
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
