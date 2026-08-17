const FIGURE_TYPE_LABELS: Record<string, { zh: string; en: string }> = {
  auto: { zh: "自动判断", en: "Automatic" },
  "mechanism-cycle": { zh: "反应机理 / 催化循环", en: "Mechanism / catalytic cycle" },
  "simple-scheme": { zh: "简单反应式", en: "Simple reaction scheme" },
  "reaction-scope": { zh: "底物范围", en: "Reaction scope" },
  "complex-multipanel": { zh: "复杂多面板化学图", en: "Complex multi-panel chemistry" },
  "low-resolution": { zh: "低清晰度 / 细线化学图", en: "Low-resolution / thin-line chemistry" },
  "colored-chemistry": { zh: "彩色化学图 / 去除装饰填充", en: "Colored chemistry / remove decorative fills" },
  "data-table": { zh: "数据表格", en: "Data table" },
  "scientific-plot": { zh: "科学图表", en: "Scientific plot" },
  "general-scientific": { zh: "综合科学示意图", en: "General scientific overview" },
};

export function figureTypeLabel(
  value: string,
  language: string,
  fallback = value,
) {
  const normalizedLanguage = language === "en" ? "en" : "zh";
  return FIGURE_TYPE_LABELS[value]?.[normalizedLanguage] || fallback;
}
