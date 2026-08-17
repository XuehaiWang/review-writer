import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { useNavigate, useSearchParams } from "react-router-dom";

import { apiRequest, jsonBody } from "../../api/client";
import { queryKeys } from "../../api/queries";
import type { AuthConfig, Principal } from "../../api/types";
import { PublicHeader } from "../../components/PublicHeader";
import { useUiText } from "../../i18n/useUiText";
import { safeReturnPath } from "./paths";

type AuthMode = "login" | "register";
type AuthFields = {
  display_name: string;
  email: string;
  password: string;
};

export function AuthPage({ config }: { config: AuthConfig }) {
  const { text } = useUiText();
  const [mode, setMode] = useState<AuthMode>("login");
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { register, handleSubmit, reset, formState } = useForm<AuthFields>({
    defaultValues: { display_name: "", email: "", password: "" },
  });
  const authentication = useMutation({
    mutationFn: (values: AuthFields) =>
      apiRequest<Principal>(`/api/v1/auth/${mode}`, {
        method: "POST",
        ...jsonBody({
          email: values.email.trim(),
          password: values.password,
          ...(mode === "register" ? { display_name: values.display_name.trim() } : {}),
        }),
      }),
    onSuccess: async (principal) => {
      queryClient.setQueryData(queryKeys.me, principal);
      reset();
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      navigate(safeReturnPath(searchParams.get("next")), { replace: true });
    },
  });

  return (
    <div className="public-page auth-page">
      <PublicHeader authConfig={config} compact />
      <main className="auth-layout">
        <section className="auth-copy">
          <p className="eyebrow">{text("人在回路的科学写作", "Human-in-the-loop scientific writing")}</p>
          <h1>{text("欢迎回到可追溯的综述工作流。", "Welcome back to your traceable review workflow.")}</h1>
          <p className="lead">{text("登录后继续访问独立项目、文献处理记录、图像审核结果和模型配置。", "Sign in to continue with your isolated projects, literature records, figure reviews, and provider settings.")}</p>
          <ul className="feature-list">
            <li>{text("项目和产物按用户隔离", "Projects and artifacts are isolated per user")}</li>
            <li>{text("API密钥加密保存，不向页面返回明文", "API keys are encrypted and never returned to the page")}</li>
            <li>{text("登录会话跨刷新保持", "Your sign-in session persists across refreshes")}</li>
          </ul>
        </section>
        <section className="auth-card">
          <span className="step-label">{text("账户入口", "Account access")}</span>
          <h2>{mode === "register" ? text("注册 Review Writer", "Register for Review Writer") : text("登录 Review Writer", "Sign in to Review Writer")}</h2>
          <p className="auth-card-intro">{text("使用你的工作台账户继续当前项目。", "Use your workspace account to continue your project.")}</p>
          <div className="auth-tabs" aria-label={text("登录或注册", "Sign in or register")}>
            <button type="button" className={mode === "login" ? "auth-tab active" : "auth-tab"} onClick={() => setMode("login")}>{text("登录", "Sign in")}</button>
            <button
              type="button"
              className={mode === "register" ? "auth-tab active" : "auth-tab"}
              disabled={!config.registration_enabled}
              onClick={() => setMode("register")}
            >
              {text("注册", "Register")}
            </button>
          </div>
          <form onSubmit={handleSubmit((values) => authentication.mutate(values))}>
            {mode === "register" ? (
              <label>{text("显示名称", "Display name")}<input autoComplete="name" maxLength={200} {...register("display_name")} /></label>
            ) : null}
            <label>{text("邮箱", "Email")}<input type="email" autoComplete="email" required maxLength={320} {...register("email", { required: true })} /></label>
            <label>
              {text("密码", "Password")}
              <input
                type="password"
                autoComplete={mode === "register" ? "new-password" : "current-password"}
                required
                minLength={mode === "register" ? config.password_min_length : 1}
                maxLength={256}
                {...register("password", { required: true })}
              />
            </label>
            <button className="button button-primary button-block" type="submit" disabled={authentication.isPending || formState.isSubmitting}>
              {authentication.isPending ? text("请稍候…", "Please wait…") : mode === "register" ? text("注册并进入工作台", "Register and open workspace") : text("登录并进入工作台", "Sign in to workspace")}
            </button>
          </form>
          {authentication.error ? <p className="message message-error" role="alert">{authentication.error.message}</p> : null}
          <p className="fine-print">{text("登录状态使用HttpOnly Cookie；浏览器不会保存API密钥明文。", "Sign-in uses an HttpOnly cookie; the browser never stores plaintext API keys.")}</p>
        </section>
      </main>
    </div>
  );
}
