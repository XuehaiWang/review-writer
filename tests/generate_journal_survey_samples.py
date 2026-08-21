"""Generate local visual-QA samples for the modern-survey/2 journal layout.

The production PDF remains the LuaLaTeX renderer. ReportLab is used here only
to inspect the layout on developer machines without a TeX distribution.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf"
PAGE_W, PAGE_H = A4
MARGIN = 52
GAP = 20
COL_W = (PAGE_W - 2 * MARGIN - GAP) / 2
INK = colors.HexColor("#151A1E")
BLUE = colors.HexColor("#006BE6")
MUTED = colors.HexColor("#5F686F")
PANEL = colors.HexColor("#F0F3F5")
TABLE_HEADER = colors.HexColor("#D9D9D9")
TABLE_ROW = colors.HexColor("#F2F2F2")


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("JournalSerif", r"C:\Windows\Fonts\times.ttf"))
    pdfmetrics.registerFont(TTFont("JournalSerifBold", r"C:\Windows\Fonts\timesbd.ttf"))
    pdfmetrics.registerFont(TTFont("JournalSans", r"C:\Windows\Fonts\arial.ttf"))
    pdfmetrics.registerFont(TTFont("JournalSansBold", r"C:\Windows\Fonts\arialbd.ttf"))
    pdfmetrics.registerFont(TTFont("JournalCJK", r"C:\Windows\Fonts\simsun.ttc", subfontIndex=0))
    pdfmetrics.registerFont(TTFont("JournalCJKBold", r"C:\Windows\Fonts\simhei.ttf"))


def style(name: str, *, zh: bool, size: float, leading: float, color=INK, bold: bool = False) -> ParagraphStyle:
    font = (
        "JournalCJKBold" if zh and bold else
        "JournalCJK" if zh else
        "JournalSerifBold" if bold else
        "JournalSerif"
    )
    return ParagraphStyle(
        name,
        fontName=font,
        fontSize=size,
        leading=leading,
        textColor=color,
        alignment=TA_JUSTIFY,
        wordWrap="CJK" if zh else None,
        splitLongWords=True,
        spaceAfter=0,
    )


def draw_paragraph(c: canvas.Canvas, text: str, x: float, y: float, width: float, paragraph_style: ParagraphStyle, *, gap: float = 6) -> float:
    paragraph = Paragraph(text, paragraph_style)
    _, height = paragraph.wrap(width, PAGE_H)
    paragraph.drawOn(c, x, y - height)
    return y - height - gap


def draw_series(c: canvas.Canvas, paragraphs: list[str], x: float, y: float, width: float, paragraph_style: ParagraphStyle) -> float:
    for text in paragraphs:
        y = draw_paragraph(c, text, x, y, width, paragraph_style, gap=7)
    return y


def header(c: canvas.Canvas, title: str, page: int, *, zh: bool) -> None:
    sans = "JournalCJK" if zh else "JournalSans"
    sans_bold = "JournalCJKBold" if zh else "JournalSansBold"
    c.setFillColor(MUTED)
    c.setFont(sans, 7.2)
    c.drawString(MARGIN, PAGE_H - 28, "学术综述" if zh else "ACADEMIC SURVEY")
    c.setFillColor(BLUE)
    c.setFont(sans_bold, 7.2)
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - 28, "REVIEW WRITER")
    c.setFillColor(MUTED)
    c.setFont(sans, 7.5)
    c.drawCentredString(PAGE_W / 2, 25, str(page))


def page_number(c: canvas.Canvas, page: int, *, zh: bool) -> None:
    c.setFillColor(MUTED)
    c.setFont("JournalCJK" if zh else "JournalSans", 7.5)
    c.drawCentredString(PAGE_W / 2, 25, str(page))


def section_heading(c: canvas.Canvas, number: str, title: str, x: float, y: float, *, zh: bool, size: float = 12.2) -> float:
    c.setFillColor(BLUE)
    c.setFont("JournalCJKBold" if zh else "JournalSansBold", size)
    c.drawString(x, y, f"{number}  {title}")
    return y - size - 7


def subheading(c: canvas.Canvas, number: str, title: str, x: float, y: float, *, zh: bool) -> float:
    c.setFillColor(BLUE)
    c.setFont("JournalCJKBold" if zh else "JournalSansBold", 9.3)
    c.drawString(x, y, f"{number}  {title}")
    return y - 14


def draw_title_panel(c: canvas.Canvas, *, zh: bool) -> tuple[str, float]:
    title = "轴手性丙二烯的不对称合成：策略、机理与设计原则" if zh else "Asymmetric Synthesis of Axially Chiral Allenes: Strategies, Mechanisms, and Design Principles"
    authors = "证据驱动学术综述" if zh else "Evidence-grounded academic survey"
    affiliation = "Review Writer - Final Manuscript State"
    abstract = (
        "轴手性丙二烯的构建依赖于对区域选择性、立体决定步骤与构型稳定性的协同控制。本综述按照从头构建、手性转移以及动力学拆分与去对称化三类生成策略组织证据，比较不同催化体系的适用范围、机理依据和底物边界。通过统一分析底物类型、催化金属、配体环境、反应条件、收率与对映选择性，本文区分直接测量、原作者解释和跨文献推断，并将重复出现的范围限制追溯到可能的选择性决定步骤。最终形成从观察、机理根因、设计方向到验证指标的路线图，为配体设计与反应开发提出可检验建议。"
        if zh else
        "Axially chiral allenes require the joint control of regioselectivity, stereodetermining steps, and configurational stability. This review organizes evidence by de novo construction, chirality transfer, and kinetic resolution or desymmetrization and compares the scope, mechanistic basis, and substrate boundaries of catalytic systems. A shared matrix of substrate class, catalyst, ligand environment, conditions, yield, and enantioselectivity distinguishes direct measurement, source-author interpretation, and cross-source inference. Recurrent scope limitations are traced to candidate selectivity-determining steps rather than listed as isolated caveats. The resulting field map connects observation, mechanistic root, design direction, and validation metric to produce testable priorities for ligand design and reaction development."
    )
    keywords = (
        "关键词：轴手性；丙二烯；不对称催化；手性转移；动力学拆分"
        if zh else
        "Keywords: axial chirality; allenes; asymmetric catalysis; chirality transfer; kinetic resolution"
    )
    panel_x = MARGIN
    panel_top = PAGE_H - 48
    panel_h = 294 if zh else 306
    c.setFillColor(PANEL)
    c.roundRect(panel_x, panel_top - panel_h, PAGE_W - 2 * MARGIN, panel_h, 11, fill=1, stroke=0)
    y = panel_top - 22
    y = draw_paragraph(c, title, panel_x + 18, y, PAGE_W - 2 * MARGIN - 36, style("title", zh=zh, size=20.2, leading=22.2, bold=True), gap=8)
    c.setFillColor(BLUE)
    c.setFont("JournalCJKBold" if zh else "JournalSansBold", 8.2)
    c.drawString(panel_x + 18, y, "学术综述" if zh else "ACADEMIC SURVEY")
    y -= 18
    c.setFillColor(INK)
    c.setFont("JournalCJKBold" if zh else "JournalSansBold", 9)
    c.drawString(panel_x + 18, y, authors)
    y -= 14
    c.setFillColor(MUTED)
    c.setFont("JournalCJK" if zh else "JournalSans", 7.8)
    c.drawString(panel_x + 18, y, affiliation)
    y -= 19
    c.setFillColor(INK)
    c.setFont("JournalCJKBold" if zh else "JournalSansBold", 8.1)
    c.drawString(panel_x + 18, y, "摘要" if zh else "Abstract")
    y -= 6
    y = draw_paragraph(c, abstract, panel_x + 18, y, PAGE_W - 2 * MARGIN - 36, style("abstract", zh=zh, size=8.35, leading=10.2), gap=7)
    y = draw_paragraph(c, keywords, panel_x + 18, y, PAGE_W - 2 * MARGIN - 36, style("keywords", zh=zh, size=7.7, leading=9.2, bold=False), gap=0)
    c.setFillColor(MUTED)
    c.setFont("JournalCJK" if zh else "JournalSans", 7.3)
    c.drawString(panel_x + 18, panel_top - panel_h + 15, "Review Writer | modern-survey/2 | LuaLaTeX production profile")
    return title, panel_top - panel_h - 23


def draw_intro(c: canvas.Canvas, y: float, *, zh: bool) -> None:
    y = section_heading(c, "1", "引言" if zh else "Introduction", MARGIN, y, zh=zh)
    left = ([
        "轴手性丙二烯同时承载结构独特性与合成可塑性，因此在天然产物、药物化学和手性材料中持续受到关注 <font color='#006BE6'>[1, 2]</font>。高质量综述不能只罗列最高收率与 ee，而应解释不同策略为何能够进入同一领域地图，以及选择性决定步骤如何受到配体环境、底物结构和反应条件的共同约束。",
        "丙二烯轴的构型来源于两个正交 π 键及其末端取代模式。与经典联芳基阻转异构不同，合成过程中既要建立轴手性，也要避免后处理、纯化和应用条件下的构型侵蚀。因此，反应选择性与产物稳定性必须在同一证据框架中讨论。",
        "早期工作主要依赖手性底物或化学计量试剂，现代方法则逐步转向催化不对称构建。铜、钯及其他金属体系拓展了亲核取代、加成与偶联路径，但不同研究使用的底物矩阵和报告指标并不一致，直接横向排序容易产生误导。",
    ] if zh else [
        "Axially chiral allenes combine structural distinctiveness with synthetic versatility and remain important in natural products, medicinal chemistry, and chiral materials <font color='#006BE6'>[1, 2]</font>. A useful review should not merely rank maximum yield and ee; it should explain why distinct strategies belong on one field map and how stereodetermining steps are jointly constrained by ligand environment, substrate structure, and reaction conditions.",
        "The allene axis arises from two orthogonal pi bonds and the substitution pattern at their termini. Unlike classical biaryl atropisomers, an allene synthesis must both establish axial chirality and preserve it during workup, purification, and use. Reaction selectivity and product stability therefore belong in one evidence framework.",
        "Foundational studies relied heavily on chiral substrates or stoichiometric reagents, whereas modern work increasingly favors catalytic asymmetric construction. Copper, palladium, and other systems opened substitution, addition, and coupling pathways, but their substrate matrices and reporting conventions differ enough to make simple ranking misleading.",
    ])
    right = ([
        "本文以生成逻辑为主要组织轴：从头构建直接由前手性底物建立轴手性；手性转移考察中心手性或其他手性元素向丙二烯轴的传递；动力学拆分与去对称化则依赖速率差异或对称性破缺。该结构避免“其他或未指定”章节，并为跨策略比较提供一致维度。",
        "每项方法均在共享维度上比较，包括底物类型、催化体系、温度、收率、ee、区域选择性与已知局限。机理陈述进一步区分实验观察、计算支持和作者提议，避免把合理假说写成已证实事实。",
        "综述的目标不仅是总结已发表结果，还要凝练能够指导下一轮实验的规律。为此，章节按照问题、比较、解释和边界推进，结论再把跨章节观察转化为配体设计、底物扩展和构型稳定性验证的具体方向。",
    ] if zh else [
        "The primary navigation axis is generative logic. De novo construction establishes axial chirality from prochiral inputs; chirality transfer examines the relay of a stereogenic center or another chiral element to the allene axis; kinetic resolution and desymmetrization rely on rate differentiation or symmetry breaking. This structure removes an 'Other or unspecified' category and supplies shared dimensions for comparison.",
        "Each method is compared on substrate type, catalytic system, temperature, yield, ee, regioselectivity, and reported boundary. Mechanistic statements further separate experimental observation, computational support, and author proposal so that a plausible hypothesis is not presented as an established fact.",
        "The objective is not only to summarize published outcomes but to condense patterns that can guide the next experiment. Sections therefore advance through question, comparison, explanation, and boundary, while the conclusion converts cross-section observations into specific priorities for ligand design, substrate expansion, and configurational-stability testing.",
    ])
    body = style("body", zh=zh, size=8.35, leading=10.5)
    draw_series(c, left, MARGIN, y, COL_W, body)
    draw_series(c, right, MARGIN + COL_W + GAP, y, COL_W, body)


def draw_taxonomy_figure(c: canvas.Canvas, y: float, *, zh: bool) -> float:
    labels = (
        [
            ("1.1 从头构建", "亲核取代、加成与催化偶联"),
            ("1.2 手性转移", "中心到轴的立体信息传递"),
            ("1.3 拆分/去对称", "速率差异与对称性破缺"),
            ("1.4 设计规则", "机理根因与验证指标"),
        ]
        if zh else
        [
            ("1.1 De novo", "Substitution, addition, and catalytic coupling"),
            ("1.2 Chirality transfer", "Center-to-axis stereochemical relay"),
            ("1.3 Resolution", "Rate differentiation and symmetry breaking"),
            ("1.4 Design rules", "Mechanistic roots and validation metrics"),
        ]
    )
    fills = ["#EAF4FF", "#ECF7EF", "#FFF2E8", "#F2EEFF"]
    box_w = 112
    box_h = 112
    start_x = MARGIN
    gap = (PAGE_W - 2 * MARGIN - 4 * box_w) / 3
    for index, (label, text) in enumerate(labels):
        x = start_x + index * (box_w + gap)
        c.setFillColor(colors.HexColor(fills[index]))
        c.setStrokeColor(colors.HexColor("#C9D5E1"))
        c.roundRect(x, y - box_h, box_w, box_h, 15, fill=1, stroke=1)
        c.setFillColor(BLUE)
        c.setFont("JournalCJKBold" if zh else "JournalSansBold", 8)
        c.drawCentredString(x + box_w / 2, y - 20, label)
        draw_paragraph(c, text, x + 10, y - 35, box_w - 20, style(f"fig-{index}", zh=zh, size=7.4, leading=9.2), gap=0)
        if index < len(labels) - 1:
            c.setStrokeColor(BLUE)
            c.setLineWidth(1.2)
            c.line(x + box_w + 3, y - box_h / 2, x + box_w + gap - 4, y - box_h / 2)
    caption = (
        "<b><font color='#006BE6'>图 1</font></b> 轴手性丙二烯合成方法的生成策略分类。分类以反应如何建立或传递轴手性为主轴，并把机理根因与验证指标连接到后续设计建议。"
        if zh else
        "<b><font color='#006BE6'>Figure 1</font></b> Generative taxonomy of methods for axially chiral allene synthesis. The classification follows how axial chirality is established or transferred and connects mechanistic roots to validation metrics."
    )
    return draw_paragraph(c, caption, MARGIN, y - box_h - 12, PAGE_W - 2 * MARGIN, style("caption", zh=zh, size=7.7, leading=9.3), gap=9)


def page_two(c: canvas.Canvas, title: str, *, zh: bool) -> None:
    header(c, title, 2, zh=zh)
    y = PAGE_H - 55
    y = draw_taxonomy_figure(c, y, zh=zh)
    y = subheading(c, "2.1", "比较维度与证据边界" if zh else "Comparison dimensions and evidence boundaries", MARGIN, y, zh=zh)
    body = style("body-2", zh=zh, size=8.35, leading=10.5)
    left = ([
        "<b>正向综合。</b> 选择性差异应通过选择性决定步骤、配体环境和底物边界共同解释，而不是在每段末尾重复否定性限制。直接测量、原作者解释与跨文献推断需要保持不同的措辞强度 <font color='#006BE6'>[3-5]</font>。",
        "<b>底物边界。</b> 高位阻、强配位或电子性质极端的底物经常表现出较低转化率或选择性。只有当多项研究采用可比较条件时，这些观察才能上升为跨体系规律；否则应保留为来源限定的边界。",
        "<b>选择性来源。</b> ee 的变化需要与区域选择性、反应速率及副反应共同分析。单一最高值不能证明某种配体具有普遍优势，也不能代替对过渡态几何和竞争路径的解释。",
    ] if zh else [
        "<b>Positive synthesis.</b> Selectivity differences should be explained through stereodetermining steps, ligand environments, and substrate boundaries instead of ending every paragraph with repeated defensive limitations. Direct measurements, source-author interpretations, and cross-source inferences require distinct wording strength <font color='#006BE6'>[3-5]</font>.",
        "<b>Substrate boundaries.</b> Hindered, strongly coordinating, or electronically extreme substrates often show reduced conversion or selectivity. These observations become a cross-system pattern only when several studies use comparable conditions; otherwise they remain source-bounded limits.",
        "<b>Sources of selectivity.</b> Changes in ee should be analyzed together with regioselectivity, rate, and competing pathways. A single maximum cannot establish the general superiority of a ligand or replace an explanation of transition-state geometry.",
    ])
    right = ([
        "<b>机理证据。</b> 通用催化循环图应抽象共同步骤，同时明确哪些中间体得到实验支持、哪些只来自计算或机理提议。对轴手性消旋风险的讨论还需连接温度、时间尺度与构型稳定性数据。",
        "<b>跨文献比较。</b> 催化金属可以作为重要变量，但不应与反应机理、底物类别和手性诱导方式混在同一层级。主分类轴保持一致，其他维度通过表格和交叉引用呈现。",
        "<b>可复核结论。</b> 每个综合性判断都关联到明确的 Claim、引用组和证据键。若证据不足以支持因果或普适性表述，系统降低措辞强度，而不是生成无法追溯的确定性结论。",
    ] if zh else [
        "<b>Mechanistic evidence.</b> A generalized catalytic cycle should abstract shared steps while distinguishing experimentally supported intermediates from computational or proposed states. Discussion of configurational erosion must connect temperature, timescale, and stability evidence.",
        "<b>Cross-study comparison.</b> Catalytic metal is an important variable, but it should not be mixed at one hierarchy level with mechanism, substrate class, or stereochemical induction. The primary axis remains consistent while other dimensions are expressed through tables and cross-references.",
        "<b>Auditable conclusions.</b> Every synthetic judgment is linked to a Claim, citation group, and evidence key. When evidence cannot support causal or universal wording, the system lowers claim strength rather than producing an untraceable assertion.",
    ])
    draw_series(c, left, MARGIN, y, COL_W, body)
    draw_series(c, right, MARGIN + COL_W + GAP, y, COL_W, body)
    c.showPage()


def draw_comparison_table(c: canvas.Canvas, y: float, *, zh: bool) -> float:
    headers = ["策略", "主要输入", "选择性来源", "证据重点", "主要边界"] if zh else ["Strategy", "Primary input", "Selectivity source", "Evidence focus", "Main boundary"]
    rows = (
        [
            ["从头构建", "前手性底物", "手性催化剂", "过渡态与 ee", "底物依赖"],
            ["手性转移", "手性中心", "传递保真度", "构型保持", "消旋风险"],
            ["动力学拆分", "外消旋底物", "速率差异", "选择因子", "理论收率"],
            ["去对称化", "对称底物", "面/位点区分", "区域与对映选择", "对称性要求"],
        ]
        if zh else
        [
            ["De novo", "Prochiral", "Chiral catalyst", "Transition state and ee", "Substrate dependence"],
            ["Transfer", "Chiral center", "Transfer fidelity", "Configuration retention", "Racemization risk"],
            ["Kinetic resolution", "Racemic", "Rate difference", "Selectivity factor", "Yield ceiling"],
            ["Desymmetrization", "Symmetric", "Face/site control", "Regio- and enantioselectivity", "Symmetry requirement"],
        ]
    )
    widths = [92, 92, 104, 116, PAGE_W - 2 * MARGIN - 404]
    row_h = 34
    x_positions = [MARGIN]
    for width in widths:
        x_positions.append(x_positions[-1] + width)
    c.setFillColor(BLUE)
    c.setFont("JournalCJKBold" if zh else "JournalSansBold", 7.8)
    table_label = "表 1" if zh else "Table 1"
    c.drawString(MARGIN, y, table_label)
    c.setFillColor(INK)
    c.drawString(MARGIN + 34, y, "跨策略的共享比较维度" if zh else "Shared comparison dimensions across generative strategies")
    y -= 12
    for row_index, values in enumerate([headers, *rows]):
        c.setFillColor(TABLE_HEADER if row_index == 0 else TABLE_ROW if row_index % 2 else colors.white)
        c.rect(MARGIN, y - row_h, PAGE_W - 2 * MARGIN, row_h, fill=1, stroke=0)
        for column, value in enumerate(values):
            font = "JournalCJKBold" if zh and row_index == 0 else "JournalSansBold" if not zh and row_index == 0 else "JournalCJK" if zh else "JournalSerif"
            cell_style = ParagraphStyle(
                f"cell-{row_index}-{column}",
                fontName=font,
                fontSize=6.9,
                leading=8.0,
                textColor=INK,
                wordWrap="CJK" if zh else None,
            )
            paragraph = Paragraph(value, cell_style)
            _, height = paragraph.wrap(widths[column] - 8, row_h - 4)
            paragraph.drawOn(c, x_positions[column] + 4, y - (row_h + height) / 2)
        y -= row_h
    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    c.line(MARGIN, y + row_h * 5, PAGE_W - MARGIN, y + row_h * 5)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    return y - 15


def page_three(c: canvas.Canvas, title: str, *, zh: bool) -> None:
    header(c, title, 3, zh=zh)
    y = PAGE_H - 58
    y = draw_comparison_table(c, y, zh=zh)
    y = subheading(c, "3.1", "从比较结果到设计路线图" if zh else "From comparison to a design roadmap", MARGIN, y, zh=zh)
    body = style("body-3", zh=zh, size=8.35, leading=10.5)
    left = ([
        "比较表的价值不在于收集单个最高值，而在于暴露可迁移规律。若多个催化体系都在高位阻底物上出现范围收缩，综述应进一步追问限制来自配位几何、迁移步骤还是构型稳定性，并提出能够区分这些解释的实验。",
        "第一类实验可以系统改变配体位阻和配位能力，同时保持底物电子性质接近，以观察速率和 ee 是否共同变化。第二类实验可改变温度和反应时间，区分选择性建立不足与产物后续消旋。",
        "只有同时报告失败底物、竞争副产物和测量不确定性，模型才能区分真正的范围边界与尚未优化的条件。负结果因此不是附注，而是建立设计规则的重要证据。",
    ] if zh else [
        "The value of a comparison table is not the collection of isolated maxima but the exposure of transferable patterns. If several catalytic systems narrow on hindered substrates, the review should ask whether coordination geometry, migration, or configurational stability is limiting and propose experiments that distinguish those explanations.",
        "One experiment can vary ligand sterics and coordination ability while holding substrate electronics near constant to test whether rate and ee move together. A second can vary temperature and residence time to distinguish weak stereochemical induction from post-formation racemization.",
        "Only when failed substrates, competing products, and measurement uncertainty are reported can a model distinguish a genuine scope boundary from conditions that remain under-optimized. Negative outcomes are therefore evidence for design rules rather than marginal notes.",
    ])
    right = ([
        "路线图按照“观察 - 机理根因 - 设计方向 - 验证指标”展开。未来配体设计建议必须对应可测量结果，例如底物矩阵、速率、区域选择性与 ee 的联合变化，而不是停留在“开发更好催化剂”的空泛表述。",
        "若配体改变主要影响迁移步骤，预期速率与选择性应在特定底物类别中表现相关变化；若构型稳定性是瓶颈，则不同催化体系可能得到相似的温度依赖性。这样的预测使路线图可以被证伪。",
        "跨策略比较最终应回答哪些规律已经建立、哪些只在有限体系中观察到，以及哪些机制仍然相互竞争。结论据此给出优先级，而不是简单重复各章节的局限性。",
    ] if zh else [
        "The roadmap follows an observation - mechanistic root - design direction - validation metric chain. A future ligand-design proposal should predict measurable joint changes in substrate matrix, rate, regioselectivity, and ee instead of ending with the generic instruction to develop better catalysts.",
        "If ligand modification primarily affects migration, rate and selectivity should change together for defined substrate classes. If configurational stability is limiting, distinct catalytic systems may converge on a similar temperature dependence. Such predictions make the roadmap falsifiable.",
        "Cross-strategy comparison should finally distinguish patterns that are established, observations restricted to narrow systems, and mechanisms that remain competing explanations. The conclusion can then assign priorities instead of repeating each section's limitations.",
    ])
    draw_series(c, left, MARGIN, y, COL_W, body)
    draw_series(c, right, MARGIN + COL_W + GAP, y, COL_W, body)
    c.showPage()


def page_four(c: canvas.Canvas, title: str, *, zh: bool) -> None:
    header(c, title, 4, zh=zh)
    y = PAGE_H - 55
    y = section_heading(c, "4", "结论" if zh else "Conclusion", MARGIN, y, zh=zh)
    body = style("body-4", zh=zh, size=8.2, leading=10.1)
    conclusion = (
        "生成策略分类、共享比较维度和证据等级共同构成了可审计的领域地图。该框架既保留化学综述对机理与选择性的专业要求，也允许其他学科替换领域组件而不改变 Claim/Citation 闭环。"
        if zh else
        "Generative taxonomy, shared comparison dimensions, and evidence levels jointly provide an auditable field map. The framework preserves chemistry-specific expectations for mechanism and selectivity while allowing other domains to replace knowledge components without changing the Claim/Citation closure."
    )
    y_left = draw_paragraph(c, conclusion, MARGIN, y, COL_W, body, gap=10)
    c.setFillColor(BLUE)
    c.setFont("JournalCJKBold" if zh else "JournalSansBold", 12)
    c.drawString(MARGIN, y_left, "参考文献" if zh else "References")
    y_left -= 16
    journals = ["Journal of Catalysis", "Chemical Science", "ACS Catalysis", "Angewandte Chemie", "Chemical Reviews", "Nature Synthesis", "Organic Letters", "JACS Au", "Accounts of Chemical Research", "Advanced Synthesis and Catalysis", "Chemistry A European Journal", "Chemical Society Reviews"]
    references = [
        f"[{index}] {chr(64 + ((index - 1) % 26) + 1)}. Author et al. {journals[(index - 1) % len(journals)]} {2020 + index % 7}, {10 + index}, {100 * index}-{100 * index + 12}."
        for index in range(1, 25)
    ]
    ref_style = style("reference", zh=zh, size=7.15, leading=8.7)
    y_right = y
    split = 12
    for reference in references[:split]:
        y_left = draw_paragraph(c, reference, MARGIN, y_left, COL_W, ref_style, gap=4)
    for reference in references[split:]:
        y_right = draw_paragraph(c, reference, MARGIN + COL_W + GAP, y_right, COL_W, ref_style, gap=4)
    c.showPage()


def make_sample(path: Path, *, zh: bool) -> None:
    c = canvas.Canvas(
        str(path),
        pagesize=A4,
        pageCompression=1,
        invariant=1,
        initialFontName="JournalCJK" if zh else "JournalSerif",
        initialFontSize=9,
    )
    title, y = draw_title_panel(c, zh=zh)
    draw_intro(c, y, zh=zh)
    page_number(c, 1, zh=zh)
    c.showPage()
    page_two(c, title, zh=zh)
    page_three(c, title, zh=zh)
    page_four(c, title, zh=zh)
    c.setTitle(title)
    c.setAuthor("Review Writer modern-survey/2 visual QA")
    c.save()


def main() -> None:
    register_fonts()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    make_sample(OUTPUT / "modern-survey-v2-journal-layout-sample-en.pdf", zh=False)
    make_sample(OUTPUT / "modern-survey-v2-journal-layout-sample-zh-CN.pdf", zh=True)


if __name__ == "__main__":
    main()
