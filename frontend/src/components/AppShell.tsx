import { useEffect, useRef } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

import type { AuthConfig, Principal } from "../api/types";
import { useSessionLogout } from "../hooks/useSessionLogout";
import { translate, type MessageKey } from "../i18n/messages";
import { usePreferences } from "../state/preferences";

type AppShellProps = {
  authConfig: AuthConfig;
  identity: Principal;
  children: React.ReactNode;
};

const workflowLinks: Array<{ href: string; label: MessageKey; stage: string; hintZh: string; hintEn: string }> = [
  { href: "/library", label: "library", stage: "01", hintZh: "文献准备", hintEn: "Sources" },
  { href: "/discovery", label: "discovery", stage: "02", hintZh: "检索筛选", hintEn: "Screening" },
  { href: "/planning?tab=matrix", label: "planning", stage: "03", hintZh: "分析规划", hintEn: "Planning" },
  { href: "/sections", label: "sections", stage: "04", hintZh: "章节生成", hintEn: "Sections" },
  { href: "/images?tab=review", label: "images", stage: "05", hintZh: "选图重绘", hintEn: "Figures" },
  { href: "/draft", label: "draft", stage: "06", hintZh: "编辑优化", hintEn: "Revision" },
  { href: "/final", label: "final", stage: "07", hintZh: "审计导出", hintEn: "Release" },
];

function withCurrentProject(href: string, search: string): string {
  const project = new URLSearchParams(search).get("project");
  if (!project) return href;
  const url = new URL(href, window.location.origin);
  url.searchParams.set("project", project);
  return `${url.pathname}${url.search}`;
}

export function AppShell({ authConfig, identity, children }: AppShellProps) {
  const location = useLocation();
  const workflowNavRef = useRef<HTMLDivElement>(null);
  const language = usePreferences((state) => state.language);
  const setLanguage = usePreferences((state) => state.setLanguage);
  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);
  useEffect(() => {
    const nav = workflowNavRef.current;
    const active = nav?.querySelector<HTMLElement>(".workflow-link.active");
    if (!nav || !active || nav.scrollWidth <= nav.clientWidth) return;
    const centered = active.offsetLeft - (nav.clientWidth - active.offsetWidth) / 2;
    nav.scrollLeft = Math.max(0, Math.min(centered, nav.scrollWidth - nav.clientWidth));
  }, [location.pathname, location.search]);
  const logout = useSessionLogout();

  return (
    <div className="app-frame">
      <header className="topbar">
        <div className="topbar-inner">
          <Link className="brand" to="/" aria-label={language === "en" ? "Review Writer home" : "Review Writer 首页"}>
            <span className="brand-mark" aria-hidden="true">RW</span>
            <span>
              <strong>Review Writer</strong>
              <small>{translate(language, "productSubtitle")}</small>
            </span>
          </Link>
          <div className="top-actions">
            <Link className="button button-quiet topbar-home" to="/">{language === "en" ? "Home" : "首页"}</Link>
            <div className="language-switch" aria-label={language === "en" ? "Language" : "语言"}>
              <button
                type="button"
                className={language === "zh-CN" ? "active" : ""}
                onClick={() => setLanguage("zh-CN")}
              >
                中
              </button>
              <button
                type="button"
                className={language === "en" ? "active" : ""}
                onClick={() => setLanguage("en")}
              >
                EN
              </button>
            </div>
            <span className="badge">{authConfig.enabled ? translate(language, "hosted") : translate(language, "local")}</span>
            <span className="identity-chip" title={identity.email}>{identity.display_name || identity.email || "Researcher"}</span>
            {authConfig.enabled ? (
              <button
                className="button button-quiet topbar-logout"
                type="button"
                disabled={logout.isPending}
                onClick={() => logout.mutate()}
              >
                {logout.isPending ? (language === "en" ? "Signing out…" : "正在退出…") : translate(language, "logout")}
              </button>
            ) : null}
          </div>
        </div>
      </header>

      <nav className="workflow-nav" aria-label={language === "en" ? "Review workflow" : "综述工作流"}>
        <div className="workflow-nav-inner" ref={workflowNavRef}>
          <NavLink to="/workspace" end className={({ isActive }) => (isActive ? "workflow-link workflow-home active" : "workflow-link workflow-home")}>
            <span className="workflow-link-index">00</span>
            <span className="workflow-link-copy"><strong>{translate(language, "projects")}</strong><small>{language === "en" ? "Workspace" : "工作台"}</small></span>
          </NavLink>
          {workflowLinks.map((item) => (
            <NavLink
              key={item.href}
              className={({ isActive }) => (isActive ? "workflow-link active" : "workflow-link")}
              to={withCurrentProject(item.href, location.search)}
            >
              <span className="workflow-link-index">{item.stage}</span>
              <span className="workflow-link-copy"><strong>{translate(language, item.label)}</strong><small>{language === "en" ? item.hintEn : item.hintZh}</small></span>
            </NavLink>
          ))}
          <NavLink to="/settings" className={({ isActive }) => (isActive ? "workflow-link workflow-settings active" : "workflow-link workflow-settings")}>
            <span className="workflow-link-index">⚙</span>
            <span className="workflow-link-copy"><strong>{translate(language, "settings")}</strong><small>{language === "en" ? "Providers" : "模型配置"}</small></span>
          </NavLink>
          {identity.permissions.includes("provider:manage") ? (
            <NavLink to="/admin" className={({ isActive }) => (isActive ? "workflow-link workflow-admin active" : "workflow-link workflow-admin")}>
              <span className="workflow-link-index">◆</span>
              <span className="workflow-link-copy"><strong>{language === "en" ? "Admin" : "管理后台"}</strong><small>{language === "en" ? "Server" : "服务器"}</small></span>
            </NavLink>
          ) : null}
        </div>
      </nav>

      {children}
    </div>
  );
}
