import { LocalizedError } from "../../components/LocalizedError";
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { useNavigate, useSearchParams } from "react-router-dom";

import { apiRequest, jsonBody } from "../../api/client";
import { queryKeys } from "../../api/queries";
import type { AuthConfig, Principal } from "../../api/types";
import { PublicHeader } from "../../components/PublicHeader";
import { useUiText } from "../../i18n/useUiText";
import { safeReturnPath } from "./paths";

type AuthMode = "login" | "register" | "forgot" | "reset";
type AuthFields = {
  display_name: string;
  email: string;
  password: string;
  password_confirm: string;
  verification_code: string;
};
type AuthMessage = { message: string };

export function AuthPage({ config }: { config: AuthConfig }) {
  const { text } = useUiText();
  const [searchParams] = useSearchParams();
  const resetToken = String(searchParams.get("reset_token") || "").trim();
  const [mode, setMode] = useState<AuthMode>(resetToken ? "reset" : "login");
  const [notice, setNotice] = useState("");
  const [codeCooldown, setCodeCooldown] = useState(0);
  useEffect(() => {
    if (!codeCooldown) return;
    const timer = window.setTimeout(() => setCodeCooldown((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearTimeout(timer);
  }, [codeCooldown]);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { register, handleSubmit, reset: resetForm, getValues, trigger, formState } = useForm<AuthFields>({
    shouldUnregister: true,
    defaultValues: { display_name: "", email: "", password: "", password_confirm: "" },
  });

  const authentication = useMutation({
    mutationFn: (values: AuthFields) =>
      apiRequest<Principal>(`/api/v1/auth/${mode}`, {
        method: "POST",
        ...jsonBody({
          email: values.email.trim(),
          password: values.password,
          ...(mode === "register" ? { display_name: values.display_name.trim(), verification_code: values.verification_code?.trim() || "" } : {}),
        }),
      }),
    onSuccess: async (principal) => {
      queryClient.setQueryData(queryKeys.me, principal);
      resetForm();
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      navigate(safeReturnPath(searchParams.get("next")), { replace: true });
    },
  });
  const requestRegistrationCode = useMutation({
    mutationFn: (email: string) => apiRequest<AuthMessage>("/api/v1/auth/registration-code", {
      method: "POST", ...jsonBody({ email: email.trim() }),
    }),
    onSuccess: (result) => { setNotice(result.message); setCodeCooldown(60); },
  });
  const requestReset = useMutation({
    mutationFn: (values: AuthFields) =>
      apiRequest<AuthMessage>("/api/v1/auth/password-reset/request", {
        method: "POST",
        ...jsonBody({ email: values.email.trim() }),
      }),
    onSuccess: (result) => setNotice(result.message),
  });
  const completeReset = useMutation({
    mutationFn: (values: AuthFields) =>
      apiRequest<AuthMessage>("/api/v1/auth/password-reset/complete", {
        method: "POST",
        ...jsonBody({ token: resetToken, new_password: values.password }),
      }),
    onSuccess: (result) => {
      setNotice(result.message);
      setMode("login");
      resetForm({ display_name: "", email: "", password: "", password_confirm: "" });
      const next = searchParams.get("next");
      navigate(next ? `/login?next=${encodeURIComponent(next)}` : "/login", { replace: true });
    },
  });

  const switchMode = (nextMode: AuthMode) => {
    const email = getValues("email");
    authentication.reset();
    requestRegistrationCode.reset();
    requestReset.reset();
    completeReset.reset();
    setNotice("");
    resetForm({ display_name: "", email, password: "", password_confirm: "" });
    setMode(nextMode);
  };

  const heading = mode === "register"
    ? text("注册 Review Writer", "Register for Review Writer")
    : mode === "forgot"
      ? text("找回登录密码", "Recover your password")
      : mode === "reset"
        ? text("设置新密码", "Set a new password")
        : text("登录 Review Writer", "Sign in to Review Writer");

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
          <h2>{heading}</h2>
          <p className="auth-card-intro">
            {mode === "forgot"
              ? text(`输入注册邮箱，我们会发送一个${config.password_reset_expiry_minutes || 30}分钟内有效的一次性链接。`, `Enter your registered email to receive a one-time link valid for ${config.password_reset_expiry_minutes || 30} minutes.`)
              : mode === "reset"
                ? text("新密码保存后，其他设备上的旧登录会话会全部失效。", "Saving the new password signs out every existing session on other devices.")
                : mode === "register"
                  ? text("先验证邮箱，再创建账户。验证码10分钟内有效。", "Verify your email before creating an account. Codes expire in 10 minutes.")
                  : text("使用你的工作台账户继续当前项目。", "Use your workspace account to continue your project.")}
          </p>

          {mode === "login" || mode === "register" ? (
            <div className="auth-tabs" aria-label={text("登录或注册", "Sign in or register")}>
              <button type="button" className={mode === "login" ? "auth-tab active" : "auth-tab"} onClick={() => switchMode("login")}>{text("登录", "Sign in")}</button>
              <button type="button" className={mode === "register" ? "auth-tab active" : "auth-tab"} disabled={!config.registration_enabled} onClick={() => switchMode("register")}>{text("注册", "Register")}</button>
            </div>
          ) : null}

          {!config.registration_enabled && mode === "login" ? <p className="message message-warning" role="status">{text("注册邮件服务暂不可用，已有账户仍可登录。", "Registration email is unavailable. Existing accounts can still sign in.")}</p> : null}

          {mode === "forgot" ? (
            config.password_reset_enabled ? (
              <form onSubmit={handleSubmit((values) => requestReset.mutate(values))}>
                <label>{text("注册邮箱", "Registered email")}<input type="email" autoComplete="email" required maxLength={320} {...register("email", { required: true })} /></label>
                <button className="button button-primary button-block" type="submit" disabled={requestReset.isPending || Boolean(notice)}>
                  {requestReset.isPending ? text("正在发送…", "Sending…") : notice ? text("重置邮件已请求", "Reset email requested") : text("发送密码重置邮件", "Send password reset email")}
                </button>
              </form>
            ) : (
              <p className="message message-warning" role="status">{text("当前服务器尚未配置密码重置邮件，请联系管理员修改密码。", "Password reset email is not configured on this server. Contact an administrator to change your password.")}</p>
            )
          ) : null}

          {mode === "reset" ? (
            <form onSubmit={handleSubmit((values) => completeReset.mutate(values))}>
              <label>{text("新密码", "New password")}<input type="password" autoComplete="new-password" required minLength={config.password_min_length} maxLength={256} {...register("password", { required: true })} /></label>
              <label>{text("确认新密码", "Confirm new password")}<input type="password" autoComplete="new-password" required minLength={config.password_min_length} maxLength={256} {...register("password_confirm", { required: true, validate: (value) => value === getValues("password") || text("两次输入的密码不一致。", "Passwords do not match.") })} /></label>
              {formState.errors.password_confirm ? <p className="message message-error" role="alert">{String(formState.errors.password_confirm.message || "")}</p> : null}
              <button className="button button-primary button-block" type="submit" disabled={completeReset.isPending}>
                {completeReset.isPending ? text("正在保存…", "Saving…") : text("确认修改密码", "Change password")}
              </button>
            </form>
          ) : null}

          {mode === "login" || mode === "register" ? (
            <form onSubmit={handleSubmit((values) => authentication.mutate(values))}>
              {mode === "register" ? <label>{text("显示名称", "Display name")}<input autoComplete="name" maxLength={200} {...register("display_name")} /></label> : null}
              <label>{text("邮箱", "Email")}<input type="email" autoComplete="email" required maxLength={320} {...register("email", { required: true })} /></label>
              {mode === "register" ? <>
                <button type="button" className="button button-secondary" disabled={!config.registration_enabled || requestRegistrationCode.isPending || authentication.isPending || codeCooldown > 0}
                  onClick={async () => { if (await trigger("email")) { setNotice(""); requestRegistrationCode.mutate(getValues("email")); } }}>
                  {requestRegistrationCode.isPending ? text("正在发送…", "Sending…") : codeCooldown > 0 ? text(`${codeCooldown}秒后可重发`, `Resend in ${codeCooldown}s`) : text("发送邮箱验证码", "Send email code")}
                </button>
                <label>{text("邮箱验证码", "Email verification code")}<input inputMode="numeric" autoComplete="one-time-code" required minLength={6} maxLength={6} pattern="[0-9]{6}" {...register("verification_code", { required: mode === "register" })} /></label>
              </> : null}
              <label>{text("密码", "Password")}<input type="password" autoComplete={mode === "register" ? "new-password" : "current-password"} required minLength={mode === "register" ? config.password_min_length : 1} maxLength={256} {...register("password", { required: true })} /></label>
              {mode === "login" ? <div className="auth-form-assist"><button type="button" onClick={() => switchMode("forgot")}>{text("忘记密码？", "Forgot password?")}</button></div> : null}
              <button className="button button-primary button-block" type="submit" disabled={authentication.isPending || requestRegistrationCode.isPending || formState.isSubmitting || (mode === "register" && !config.registration_enabled)}>
                {authentication.isPending ? text("请稍候…", "Please wait…") : mode === "register" ? text("验证并注册", "Verify and register") : text("登录并进入工作台", "Sign in to workspace")}
              </button>
            </form>
          ) : null}

          {mode === "forgot" || mode === "reset" ? <button type="button" className="button button-quiet button-block auth-back" onClick={() => switchMode("login")}>{text("返回登录", "Back to sign in")}</button> : null}
          {notice ? <p className="message message-success" role="status">{notice}</p> : null}
          {authentication.error || requestRegistrationCode.error || requestReset.error || completeReset.error ? <p className="message message-error" role="alert"><LocalizedError error={(authentication.error || requestRegistrationCode.error || requestReset.error || completeReset.error)} /></p> : null}
          <p className="fine-print">{text("登录与重置令牌使用安全Cookie或单向哈希；服务器不会保存可读取的原密码。", "Sign-in and reset tokens use secure cookies or one-way hashes; the server never stores readable passwords.")}</p>
        </section>
      </main>
    </div>
  );
}
