"""Deterministic journal-style modern-survey LuaLaTeX serialization."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


TEMPLATE_VERSION = "modern-survey/2"
SUPPORTED_PROFILES = frozenset({"en", "zh-CN"})


MATH_SPAN = re.compile(r"(?<!\\)\$([^$\n]+)\$|\\\((.+?)\\\)")
CITATION_SPAN = re.compile(r"\[([0-9][0-9,;\s\-–—]*)\]")
REFERENCE_TITLES = frozenset({"references", "reference list", "bibliography", "参考文献"})
FIGURE_LABEL = re.compile(r"^\s*(?:figure|fig\.?|scheme|图|反应式)\s*\d+\s*[.:：\-]?\s*", re.IGNORECASE)
TABLE_LABEL = re.compile(r"^\s*(?:table|表)\s*\d+\s*[.:：\-]?\s*", re.IGNORECASE)
ALLOWED_MATH_COMMANDS = frozenset(
    {
        "alpha", "beta", "gamma", "delta", "epsilon", "theta", "lambda",
        "mu", "pi", "sigma", "phi", "omega", "Delta", "Gamma", "Sigma",
        "Phi", "Omega", "mathrm", "mathbf", "mathit", "text", "frac",
        "sqrt", "times", "cdot", "le", "ge", "pm", "rightarrow", "leftrightarrow",
    }
)


def _escape_plain(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        # TeX Gyre Termes renders these Unicode glyphs, but its PDF ToUnicode
        # map exposes them as replacement characters to deterministic text
        # extraction. Use semantic math commands so the visual meaning and
        # machine-readable PDF text remain intact.
        "′": r"\ensuremath{^{\prime}}",
        "≡": r"\ensuremath{\equiv}",
        "≠": r"\ensuremath{\neq}",
        "₀": r"\ensuremath{_{0}}",
        "₁": r"\ensuremath{_{1}}",
        "₂": r"\ensuremath{_{2}}",
        "₃": r"\ensuremath{_{3}}",
        "₄": r"\ensuremath{_{4}}",
        "₅": r"\ensuremath{_{5}}",
        "₆": r"\ensuremath{_{6}}",
        "₇": r"\ensuremath{_{7}}",
        "₈": r"\ensuremath{_{8}}",
        "₉": r"\ensuremath{_{9}}",
    }
    escaped = "".join(replacements.get(char, char) for char in text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"\\emph{\1}", escaped)
    escaped = re.sub(r"`([^`]+)`", r"\\texttt{\1}", escaped)
    return escaped


def _safe_math(raw: str) -> str | None:
    commands = re.findall(r"\\([A-Za-z]+)", raw)
    if any(command not in ALLOWED_MATH_COMMANDS for command in commands):
        return None
    if any(token in raw for token in ("\\write", "\\input", "\\include", "\\openout", "\\catcode")):
        return None
    return "$" + raw.replace("%", r"\%").replace("#", r"\#").replace("&", r"\&") + "$"


def latex_escape(value: Any) -> str:
    text = str(value or "")
    output: list[str] = []
    cursor = 0
    for match in MATH_SPAN.finditer(text):
        output.append(_escape_plain(text[cursor : match.start()]))
        raw = str(match.group(1) or match.group(2) or "")
        safe = _safe_math(raw)
        output.append(safe if safe is not None else _escape_plain(match.group(0)))
        cursor = match.end()
    output.append(_escape_plain(text[cursor:]))
    escaped = "".join(output)
    return CITATION_SPAN.sub(
        lambda match: r"\textcolor{AcademicBlue}{[" + match.group(1) + "]}",
        escaped,
    )


def _heading(level: int, text: str) -> str:
    command = {
        1: "section",
        2: "section",
        3: "subsection",
        4: "subsubsection",
        5: "paragraph",
        6: "subparagraph",
    }.get(int(level), "section")
    return rf"\{command}{{{latex_escape(text)}}}"


def _table(block: dict[str, Any]) -> str:
    header = list(block.get("header") or [])
    rows = [list(row) for row in block.get("rows") or []]
    width = max(1, len(header), *(len(row) for row in rows))
    columns = "@{}" + " ".join([r">{\RaggedRight\arraybackslash}X"] * width) + "@{}"
    def row(values: list[Any]) -> str:
        padded = [*values, *([""] * (width - len(values)))]
        return " & ".join(latex_escape(value) for value in padded[:width]) + r" \\"
    caption = TABLE_LABEL.sub("", str(block.get("caption") or "").strip()).strip()
    if len(rows) <= 20:
        lines = [
            r"\begin{table*}[t]",
            r"\centering",
            r"\setlength{\tabcolsep}{3.2pt}",
            r"\renewcommand{\arraystretch}{1.08}",
            r"\fontsize{7.2}{8.4}\selectfont",
            *( [rf"\caption{{{latex_escape(caption)}}}"] if caption else [] ),
            rf"\begin{{tabularx}}{{\textwidth}}{{{columns}}}",
            r"\toprule",
            r"\rowcolor{AcademicHeader}",
            row(header),
            r"\midrule",
        ]
        for index, values in enumerate(rows):
            if index % 2 == 0:
                lines.append(r"\rowcolor{AcademicRow}")
            lines.append(row(values))
        lines.extend([r"\bottomrule", r"\end{tabularx}", r"\end{table*}"])
        return "\n".join(lines)
    long_columns = "@{}" + " ".join(
        [rf">{{\RaggedRight\arraybackslash}}p{{{0.94 / width:.3f}\textwidth}}"] * width
    ) + "@{}"
    lines = [
        r"\clearpage\onecolumn",
        r"\begingroup\fontsize{7.2}{8.4}\selectfont",
        rf"\begin{{longtable}}{{{long_columns}}}",
        *( [rf"\caption{{{latex_escape(caption)}}} \\"] if caption else [] ),
        r"\toprule",
        row(header),
        r"\midrule",
        r"\endfirsthead",
        r"\toprule",
        row(header),
        r"\midrule",
        r"\endhead",
    ]
    lines.extend(row(values) for values in rows)
    lines.extend(
        [r"\bottomrule", r"\end{longtable}", r"\endgroup", r"\clearpage\twocolumn"]
    )
    return "\n".join(lines)


def _front_matter(state: dict[str, Any], profile: str) -> str:
    front = dict(state.get("front_matter") or {})
    abstract = str(front.get("abstract") or "").strip()
    keywords = [str(item).strip() for item in front.get("keywords") or [] if str(item).strip()]
    authors = [str(item).strip() for item in front.get("authors") or [] if str(item).strip()]
    affiliations = [str(item).strip() for item in front.get("affiliations") or [] if str(item).strip()]
    released_date = str(front.get("date") or "").strip()
    review_label = "学术综述" if profile == "zh-CN" else "ACADEMIC SURVEY"
    abstract_label = "摘要" if profile == "zh-CN" else "Abstract"
    keyword_label = "关键词" if profile == "zh-CN" else "Keywords"
    lines = [
        r"\begin{tcolorbox}[journalpanel]",
        rf"{{\sffamily\bfseries\fontsize{{21}}{{24}}\selectfont\color{{AcademicInk}} {latex_escape(state.get('title') or 'Scientific Review')}\par}}",
        r"\vspace{0.55em}",
        rf"{{\sffamily\bfseries\fontsize{{8.2}}{{9.4}}\selectfont\color{{AcademicBlue}} {latex_escape(review_label)}\par}}",
    ]
    if authors:
        lines.extend(
            [r"\vspace{0.45em}", rf"{{\sffamily\bfseries\small {latex_escape(', '.join(authors))}\par}}"]
        )
    if affiliations:
        lines.append(rf"{{\footnotesize\color{{AcademicMuted}} {latex_escape(' | '.join(affiliations))}\par}}")
    if abstract:
        lines.extend(
            [
                r"\vspace{0.75em}",
                rf"{{\sffamily\bfseries\footnotesize\color{{AcademicInk}} {latex_escape(abstract_label)}\par}}",
                rf"{{\fontsize{{8.6}}{{10.4}}\selectfont\color{{AcademicInk}} {latex_escape(abstract)}\par}}",
            ]
        )
    if keywords:
        lines.extend(
            [
                r"\vspace{0.55em}",
                rf"{{\footnotesize\color{{AcademicInk}}\textbf{{{latex_escape(keyword_label)}:}} {latex_escape('; '.join(keywords))}\par}}",
            ]
        )
    footer_parts = ["Review Writer", TEMPLATE_VERSION]
    if released_date:
        footer_parts.append(released_date)
    lines.extend(
        [
            r"\vspace{0.55em}",
            rf"{{\sffamily\fontsize{{7.5}}{{8.5}}\selectfont\color{{AcademicMuted}} {latex_escape(' | '.join(footer_parts))}\par}}",
            r"\end{tcolorbox}",
        ]
    )
    return "\n".join(lines)


def render_body(state: dict[str, Any]) -> str:
    lines: list[str] = []
    consumed = {
        int(index)
        for index in (state.get("front_matter") or {}).get("consumed_block_indexes") or []
    }
    for block_index, block in enumerate(state.get("blocks") or []):
        if block_index in consumed:
            continue
        kind = str(block.get("kind") or "")
        if kind == "heading":
            heading_text = str(block.get("text") or "")
            if re.sub(r"^\s*\d+(?:\.\d+)*[.)、：:\-]?\s*", "", heading_text).strip().casefold() in REFERENCE_TITLES:
                # Flush every double-column figure before the bibliography so
                # queued method figures cannot appear after References.
                lines.extend([r"\clearpage", r"\balance"])
            lines.extend([_heading(int(block.get("level") or 2), heading_text), ""])
        elif kind == "paragraph":
            lines.extend([latex_escape(block.get("text")), ""])
        elif kind == "list":
            environment = "enumerate" if block.get("ordered") else "itemize"
            lines.append(rf"\begin{{{environment}}}")
            lines.extend(rf"\item {latex_escape(item)}" for item in block.get("items") or [])
            lines.extend([rf"\end{{{environment}}}", ""])
        elif kind == "table":
            lines.extend([_table(block), ""])
        elif kind == "image":
            resolved = str(block.get("resolved_path") or "")
            if not resolved:
                continue
            raw_caption = str(block.get("caption") or block.get("alt") or "Figure").strip()
            caption = FIGURE_LABEL.sub("", raw_caption).strip()
            caption_name = ""
            if re.match(r"^\s*scheme\s*\d+", raw_caption, re.IGNORECASE):
                caption_name = "Scheme"
            elif re.match(r"^\s*反应式\s*\d+", raw_caption):
                caption_name = "反应式"
            lines.extend(
                [
                    r"\begin{figure*}[!tbp]",
                    r"\centering",
                    rf"\includegraphics[width=\textwidth,height=0.56\textheight,keepaspectratio]{{\detokenize{{{Path(resolved).as_posix()}}}}}",
                    *( [rf"\captionsetup{{name={latex_escape(caption_name)}}}"] if caption_name else [] ),
                    rf"\caption{{{latex_escape(caption or 'Figure')}}}",
                    r"\end{figure*}",
                    "",
                ]
            )
        elif kind == "reference":
            lines.extend(
                [
                    rf"\begingroup\fontsize{{7.35}}{{9.0}}\selectfont\noindent\hangindent=1.55em\hangafter=1 \textcolor{{AcademicBlue}}{{[{int(block.get('number') or 0)}]}} {latex_escape(block.get('text'))}\par\endgroup",
                    r"\vspace{0.22em}",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def language_preamble(profile: str) -> str:
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"Unsupported PDF language profile: {profile}")
    if profile == "zh-CN":
        return r"""\usepackage[fontset=none,scheme=plain]{ctex}
\IfFontExistsTF{TeX Gyre Termes}{\setmainfont{TeX Gyre Termes}}{\setmainfont{DejaVu Serif}}
\IfFontExistsTF{TeX Gyre Heros}{\setsansfont{TeX Gyre Heros}}{\setsansfont{DejaVu Sans}}
\IfFontExistsTF{Noto Serif CJK SC}{\setCJKmainfont{Noto Serif CJK SC}}{\setCJKmainfont{WenQuanYi Zen Hei}}
\IfFontExistsTF{Noto Sans CJK SC}{\setCJKsansfont{Noto Sans CJK SC}}{\setCJKsansfont{WenQuanYi Zen Hei}}"""
    return r"""\IfFontExistsTF{TeX Gyre Termes}{\setmainfont{TeX Gyre Termes}}{\setmainfont{DejaVu Serif}}
\IfFontExistsTF{TeX Gyre Heros}{\setsansfont{TeX Gyre Heros}}{\setsansfont{DejaVu Sans}}"""


def render_tex(state: dict[str, Any], *, profile: str, template: str) -> str:
    return (
        str(template)
        .replace("%__LANGUAGE_PREAMBLE__%", language_preamble(profile))
        .replace("%__FRONT_MATTER__%", _front_matter(state, profile))
        .replace("%__DOCUMENT_BODY__%", render_body(state))
    )
