import { Link } from "react-router-dom";

import type { AuthConfig, Principal } from "../api/types";
import { useSessionLogout } from "../hooks/useSessionLogout";
import { translate } from "../i18n/messages";
import { usePreferences } from "../state/preferences";

type PublicHeaderProps = {
  authConfig: AuthConfig;
  identity?: Principal | null;
  compact?: boolean;
};

export function PublicHeader({ authConfig, identity = null, compact = false }: PublicHeaderProps) {
  const language = usePreferences((state) => state.language);
  const setLanguage = usePreferences((state) => state.setLanguage);
  const logout = useSessionLogout();

  return (
    <header className={compact ? "public-header compact" : "public-header"}>
      <div className="public-header-inner">
        <Link className="brand" to="/" aria-label={language === "en" ? "Review Writer product home" : "Review Writer 产品首页"}>
          <span className="brand-mark" aria-hidden="true">RW</span>
          <span>
            <strong>Review Writer</strong>
            <small>{translate(language, "productSubtitle")}</small>
          </span>
        </Link>

        {!compact ? (
          <nav className="public-navigation" aria-label={language === "en" ? "Product navigation" : "产品导航"}>
            <a href="#capabilities">{language === "en" ? "Capabilities" : "核心能力"}</a>
            <a href="#workflow">{language === "en" ? "Workflow" : "工作流程"}</a>
            <a href="#governance">{language === "en" ? "Governance" : "可信保障"}</a>
          </nav>
        ) : null}

        <div className="top-actions public-header-actions">
          <div className="language-switch" aria-label={language === "en" ? "Language" : "语言"}>
            <button type="button" className={language === "zh-CN" ? "active" : ""} onClick={() => setLanguage("zh-CN")}>中</button>
            <button type="button" className={language === "en" ? "active" : ""} onClick={() => setLanguage("en")}>EN</button>
          </div>
          {compact ? <Link className="button button-quiet" to="/">{language === "en" ? "Home" : "首页"}</Link> : null}
          {identity ? (
            <>
              <Link className="button button-primary" to="/workspace">{language === "en" ? "Open workspace" : "进入工作台"}</Link>
              {authConfig.enabled ? (
                <button className="button button-quiet" type="button" disabled={logout.isPending} onClick={() => logout.mutate()}>
                  {logout.isPending ? (language === "en" ? "Signing out…" : "正在退出…") : translate(language, "logout")}
                </button>
              ) : null}
            </>
          ) : (
            <Link className="button button-primary" to="/login">{language === "en" ? "Sign in" : "登录"}</Link>
          )}
        </div>
      </div>
      {logout.error ? <p className="public-header-error" role="alert">{logout.error.message}</p> : null}
    </header>
  );
}
