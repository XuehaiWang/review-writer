import { useEffect, useId, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import type { AuthConfig, Principal } from "../api/types";
import { useSessionLogout } from "../hooks/useSessionLogout";
import { translate } from "../i18n/messages";
import { usePreferences } from "../state/preferences";

export function UserMenu({ identity, authConfig }: { identity: Principal; authConfig: AuthConfig }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const location = useLocation();
  const language = usePreferences(state => state.language);
  const setLanguage = usePreferences(state => state.setLanguage);
  const logout = useSessionLogout();
  const name = identity.display_name || identity.email || "Researcher";
  const project = new URLSearchParams(location.search).get("project");
  const settingsHref = project ? `/settings?project=${encodeURIComponent(project)}` : "/settings";

  useEffect(() => { setOpen(false); }, [location.pathname, location.search]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);

  return <div className="user-menu" ref={root}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false); }}
    onKeyDown={event => { if (event.key === "Escape") { setOpen(false); trigger.current?.focus(); } }}>
    <button className={`user-menu-trigger${open ? " active" : ""}`} type="button" ref={trigger}
      aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(value => !value)}
      aria-label={`${language === "en" ? "Account menu" : "用户菜单"}：${name}`}>
      <span className="user-menu-avatar" aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span>
      <span className="user-menu-name">{name}</span><span className="user-menu-chevron" aria-hidden="true">⌄</span>
    </button>
    {open ? <div className="user-menu-panel" id={panelId}>
      <div className="user-menu-profile"><strong>{name}</strong><small>{identity.email}</small>
        <small>{translate(language, authConfig.enabled ? "hosted" : "local")}</small></div>
      <Link className="user-menu-action" to={settingsHref} onClick={() => setOpen(false)}>{translate(language, "settings")}<span aria-hidden="true">↗</span></Link>
      {identity.permissions.includes("provider:manage") ? <Link className="user-menu-action" to="/admin" onClick={() => setOpen(false)}>{language === "en" ? "Admin" : "管理后台"}<span aria-hidden="true">↗</span></Link> : null}
      <div className="user-menu-language"><span>{language === "en" ? "Language" : "界面语言"}</span>
        <div className="language-switch">
          <button type="button" className={language === "zh-CN" ? "active" : ""} aria-pressed={language === "zh-CN"} onClick={() => setLanguage("zh-CN")}>中文</button>
          <button type="button" className={language === "en" ? "active" : ""} aria-pressed={language === "en"} onClick={() => setLanguage("en")}>English</button>
        </div>
      </div>
      {authConfig.enabled ? <button className="user-menu-action user-menu-logout" type="button" disabled={logout.isPending} onClick={() => logout.mutate()}>
        {logout.isPending ? (language === "en" ? "Signing out…" : "正在退出…") : translate(language, "logout")}
      </button> : null}
      {logout.error ? <p className="user-menu-error" role="alert">{logout.error.message}</p> : null}
    </div> : null}
  </div>;
}
