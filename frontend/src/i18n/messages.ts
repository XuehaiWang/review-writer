import type { Language } from "../state/preferences";

const messages = {
  "zh-CN": {
    productSubtitle: "科学综述工作台",
    connecting: "正在连接",
    hosted: "托管模式",
    local: "本地模式",
    logout: "退出登录",
    home: "首页",
    workspace: "工作台",
    loadingTitle: "正在准备工作台",
    loadingBody: "正在读取登录状态和服务配置。",
    projects: "项目",
    settings: "API 设置",
    library: "文献库",
    discovery: "检索",
    planning: "分析与大纲",
    sections: "章节",
    images: "图像",
    draft: "初稿",
    final: "终稿",
  },
  en: {
    productSubtitle: "Scientific Review Workspace",
    connecting: "Connecting",
    hosted: "Hosted",
    local: "Local",
    logout: "Log out",
    home: "Home",
    workspace: "Workspace",
    loadingTitle: "Preparing your workspace",
    loadingBody: "Reading the session and service configuration.",
    projects: "Projects",
    settings: "API Settings",
    library: "Library",
    discovery: "Discovery",
    planning: "Analysis & Outline",
    sections: "Sections",
    images: "Images",
    draft: "Draft",
    final: "Final",
  },
} as const;

export type MessageKey = keyof (typeof messages)["zh-CN"];

export function translate(language: Language, key: MessageKey): string {
  return messages[language][key] || messages.en[key] || key;
}
