"use strict";

let authConfig = null;
let currentIdentity = null;

const byId = (id) => document.getElementById(id);
const views = {
  loading: byId("loadingView"),
  auth: byId("authView"),
  workspace: byId("workspaceView"),
};

function setPrimaryView(name) {
  Object.entries(views).forEach(([key, node]) => { node.hidden = key !== name; });
}

function setMessage(node, text, isError = false) {
  node.textContent = text || "";
  node.hidden = !text;
  node.classList.toggle("message-error", Boolean(isError));
}

function showToast(text) {
  const toast = byId("toast");
  toast.textContent = text;
  toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 3200);
}

async function responseJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.error || `请求失败（${response.status}）`);
  return payload;
}

function showLoggedOut(message = "") {
  currentIdentity = null;
  byId("logoutButton").hidden = true;
  setPrimaryView("auth");
  switchAuthMode("login");
  setMessage(byId("authError"), message, Boolean(message));
}

async function apiFetch(path, options = {}) {
  const response = await fetch(path, { ...options, credentials: "same-origin" });
  if (response.status === 401 && authConfig?.enabled) showLoggedOut("登录已失效，请重新登录。");
  return response;
}

function switchAuthMode(mode) {
  const registering = mode === "register";
  byId("loginForm").hidden = registering;
  byId("registerForm").hidden = !registering;
  byId("authTitle").textContent = registering ? "注册 Review Writer" : "登录 Review Writer";
  document.querySelectorAll("[data-auth-mode]").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.authMode === mode);
  });
  setMessage(byId("authError"), "");
}

async function authenticate(event, mode) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = new FormData(form);
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  setMessage(byId("authError"), mode === "register" ? "正在创建账户…" : "正在登录…");
  const payload = {
    email: String(values.get("email") || "").trim(),
    password: String(values.get("password") || ""),
  };
  if (mode === "register") payload.display_name = String(values.get("display_name") || "").trim();
  try {
    const identity = await responseJson(await fetch(`/api/v1/auth/${mode}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
    form.reset();
    showWorkspace(identity);
    await loadProjects();
  } catch (error) {
    setMessage(byId("authError"), error.message || String(error), true);
  } finally { submit.disabled = false; }
}

function showWorkspace(identity) {
  currentIdentity = identity;
  const name = identity.display_name || identity.email || "研究者";
  byId("welcomeTitle").textContent = `${name}，欢迎回来`;
  byId("identityLine").textContent = identity.email
    ? `${identity.email} · 资源仅对当前账户可见`
    : "本地单用户工作区";
  byId("logoutButton").hidden = !authConfig.enabled;
  byId("settingsTab").hidden = !authConfig.enabled;
  setPrimaryView("workspace");
}

function projectCard(project) {
  const article = document.createElement("article");
  article.className = "project-card";
  const head = document.createElement("div");
  head.className = "project-card-head";
  const title = document.createElement("h3");
  title.textContent = project.slug;
  const status = document.createElement("span");
  status.className = "badge";
  status.textContent = project.discovery_status || "pending";
  head.append(title, status);
  const topic = document.createElement("p");
  topic.textContent = project.topic || "尚未填写研究主题";
  const meta = document.createElement("div");
  meta.className = "project-meta";
  const completed = document.createElement("span");
  completed.textContent = project.completed_stages?.length
    ? `已完成：${project.completed_stages.join("、")}`
    : "尚未执行阶段";
  const identifier = document.createElement("span");
  identifier.textContent = `ID ${project.project_id.slice(0, 8)}`;
  const workflow = document.createElement("a");
  workflow.className = "button button-quiet";
  workflow.href = `/library?project=${encodeURIComponent(project.slug)}`;
  workflow.textContent = "进入七阶段工作台";
  meta.append(completed, identifier, workflow);
  article.append(head, topic, meta);
  return article;
}

async function loadProjects() {
  const list = byId("projectList");
  list.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "empty-state";
  loading.textContent = "正在加载项目…";
  list.append(loading);
  try {
    const payload = await responseJson(await apiFetch("/api/v1/projects"));
    list.replaceChildren();
    if (!payload.items.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "还没有项目。右侧填写主题即可创建第一个综述项目。";
      list.append(empty);
      return;
    }
    payload.items.forEach((project) => list.append(projectCard(project)));
  } catch (error) {
    loading.textContent = error.message || String(error);
  }
}

async function createProject(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const message = byId("projectFormMessage");
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  setMessage(message, "正在创建…");
  const values = new FormData(form);
  try {
    await responseJson(await apiFetch("/api/v1/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        slug: String(values.get("slug") || "").trim(),
        topic: String(values.get("topic") || "").trim(),
        taxonomy_profile: String(values.get("taxonomy_profile") || "chemistry_general"),
      }),
    }));
    form.reset();
    setMessage(message, "项目已创建。", false);
    await loadProjects();
  } catch (error) {
    setMessage(message, error.message || String(error), true);
  } finally { submit.disabled = false; }
}

function fillProviderForm(form, record) {
  if (!record) return;
  ["base_url", "model_name", "wire_api"].forEach((name) => {
    const input = form.elements.namedItem(name);
    if (input) input.value = record[name] || "";
  });
  const status = form.querySelector("[data-key-status]");
  status.textContent = record.api_key_configured
    ? `已配置 ${record.api_key_hint || "加密密钥"}`
    : "尚未配置";
  status.classList.toggle("configured", Boolean(record.api_key_configured));
}

async function loadProviderSettings() {
  try {
    const payload = await responseJson(await apiFetch("/api/v1/provider-settings"));
    const records = new Map(payload.items.map((item) => [item.provider_kind, item]));
    document.querySelectorAll("[data-provider]").forEach((form) => {
      fillProviderForm(form, records.get(form.dataset.provider));
      form.elements.namedItem("api_key").value = "";
      setMessage(form.querySelector("[data-message]"), "");
    });
  } catch (error) { showToast(error.message || String(error)); }
}

async function saveProviderSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const provider = form.dataset.provider;
  const values = new FormData(form);
  const message = form.querySelector("[data-message]");
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  setMessage(message, "正在加密保存…");
  const key = String(values.get("api_key") || "").trim();
  try {
    const record = await responseJson(await apiFetch(`/api/v1/provider-settings/${encodeURIComponent(provider)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: String(values.get("base_url") || "").trim(),
        model_name: String(values.get("model_name") || "").trim(),
        wire_api: String(values.get("wire_api") || ""),
        api_key: key || null,
        enabled: true,
      }),
    }));
    form.elements.namedItem("api_key").value = "";
    fillProviderForm(form, record);
    setMessage(message, "设置已保存。", false);
  } catch (error) {
    setMessage(message, error.message || String(error), true);
  } finally { submit.disabled = false; }
}

function switchPanel(viewName) {
  document.querySelectorAll("[data-view]").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === viewName);
  });
  byId("projectsPanel").hidden = viewName !== "projects";
  byId("settingsPanel").hidden = viewName !== "settings";
  if (viewName === "settings") loadProviderSettings();
}

async function logout() {
  try {
    await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" });
  } finally { showLoggedOut(); }
}

async function initialize() {
  try {
    authConfig = await responseJson(await fetch("/api/v1/auth/config", { cache: "no-store" }));
    byId("environmentBadge").textContent = authConfig.enabled ? "托管模式" : "本地模式";
    const registerPassword = byId("registerForm").elements.namedItem("password");
    registerPassword.minLength = Number(authConfig.password_min_length) || 10;
    registerPassword.placeholder = `至少 ${registerPassword.minLength} 个字符`;
    const response = await fetch("/api/v1/me", { credentials: "same-origin" });
    if (response.status === 401 && authConfig.enabled) {
      showLoggedOut();
      return;
    }
    const identity = await responseJson(response);
    showWorkspace(identity);
    await loadProjects();
    if (window.location.hash === "#settings" && authConfig.enabled) switchPanel("settings");
  } catch (error) {
    showLoggedOut(error.message || String(error));
  }
}

document.querySelectorAll("[data-auth-mode]").forEach((tab) => {
  tab.addEventListener("click", () => switchAuthMode(tab.dataset.authMode));
});
byId("loginForm").addEventListener("submit", (event) => authenticate(event, "login"));
byId("registerForm").addEventListener("submit", (event) => authenticate(event, "register"));
byId("logoutButton").addEventListener("click", logout);
byId("refreshProjects").addEventListener("click", loadProjects);
byId("refreshSettings").addEventListener("click", loadProviderSettings);
byId("newProjectFocus").addEventListener("click", () => {
  switchPanel("projects");
  byId("createProjectCard").scrollIntoView({ behavior: "smooth", block: "start" });
  byId("createProjectForm").elements.namedItem("slug").focus();
});
byId("createProjectForm").addEventListener("submit", createProject);
document.querySelectorAll("[data-provider]").forEach((form) => form.addEventListener("submit", saveProviderSettings));
document.querySelectorAll("[data-view]").forEach((tab) => tab.addEventListener("click", () => switchPanel(tab.dataset.view)));

initialize();
