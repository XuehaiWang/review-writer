(function () {
  const stages = [
    { id: "library", label: "Library", href: "/library", hint: "Verify PDFs, MinerU Markdown, titles, authors, abstracts, eight structured tags, and paths." },
    { id: "discovery", label: "Discovery", href: "/discovery", hint: "Remove irrelevant keywords and papers, then confirm the candidate literature set." },
    { id: "matrix", label: "Matrix", href: "/matrix", hint: "Review fixed paper fields, full-reading notes, and the most relevant figure." },
    { id: "blueprint", label: "Blueprint", href: "/blueprint", hint: "Confirm sections, claims, assigned papers, visual needs, and writing constraints." },
    { id: "sections", label: "Sections", href: "/sections", hint: "Review section prose, paper grounding, and paragraph-level figure candidates." },
    { id: "figures", label: "Figures", href: "/figures", hint: "Verify source resolution and ensure redraws preserve all chemical content." },
    { id: "draft", label: "Draft", href: "/draft", hint: "Review coherence, figure placement, terminology, citations, and remaining issues." },
    { id: "final", label: "Final", href: "/final", hint: "Complete the final content, format, reference, figure, and release audit." },
  ];
  stages.splice(5, 0, { id: "figure-review", label: "Figure Review", href: "/figure-review", hint: "Select the final source figure for every cited paper before batch redraw." });

  function t(value) {
    return window.reviewI18n?.t(value) || value;
  }

  function message(key, params) {
    return window.reviewI18n?.message(key, params) || key;
  }

  function currentId() {
    const path = location.pathname.replace(/^\/+/, "") || "library";
    return stages.some((s) => s.id === path) ? path : "library";
  }

  function selectedProject() {
    return new URLSearchParams(location.search).get("project") || "";
  }

  function initialProject(projects) {
    const projectId = selectedProject();
    return projects.find((project) => project.project_id === projectId) || projects[0] || null;
  }

  function setProject(projectId) {
    const url = new URL(location.href);
    const params = url.searchParams;
    if (projectId) {
      params.set("project", projectId);
    } else {
      params.delete("project");
    }
    history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    syncProjectLinks();
  }

  function syncProjectLinks() {
    const projectId = selectedProject();
    document.querySelectorAll('a[href^="/"]').forEach((link) => {
      const url = new URL(link.getAttribute("href"), location.origin);
      if (!stages.some((stage) => stage.href === url.pathname)) return;
      if (projectId) url.searchParams.set("project", projectId);
      else url.searchParams.delete("project");
      link.href = `${url.pathname}${url.search}${url.hash}`;
    });
  }

  function projectForAction() {
    return selectedProject() || document.querySelector("#projectSelect, #globalProjectSelect")?.value || "";
  }

  function mountProjectDeleteControl() {
    const selector = document.querySelector("#projectSelect, #globalProjectSelect");
    if (!selector || document.querySelector("#delete-project")) return;
    const button = document.createElement("button");
    button.id = "delete-project";
    button.className = "project-delete";
    button.type = "button";
    button.textContent = t("Delete project");
    button.title = t("Permanently delete the selected project");
    const updateDisabledState = () => {
      button.disabled = !selector.value;
      button.style.opacity = button.disabled ? "0.5" : "1";
      button.style.cursor = button.disabled ? "not-allowed" : "pointer";
    };
    button.addEventListener("click", async () => {
      const projectId = selector.value || projectForAction();
      if (!projectId) return;
      const typed = window.prompt(message("deletePrompt", { projectId }), "");
      if (typed !== projectId) {
        window.alert(t("Project ID did not match. Nothing was deleted."));
        return;
      }
      button.disabled = true;
      try {
        const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
        const result = await response.json().catch(() => ({ ok: false, error: message("serverReturned", { status: response.status }) }));
        if (!response.ok || !result.ok) throw new Error(result.error || message("serverReturned", { status: response.status }));
        const projects = await (await fetch("/api/projects")).json();
        const nextProjectId = projects[0]?.project_id || "";
        const url = new URL(location.href);
        if (nextProjectId) url.searchParams.set("project", nextProjectId);
        else url.searchParams.delete("project");
        window.location.assign(`${url.pathname}${url.search}${url.hash}`);
      } catch (error) {
        window.alert(error.message || String(error));
        updateDisabledState();
      }
    });
    selector.insertAdjacentElement("afterend", button);
    selector.addEventListener("change", updateDisabledState);
    new MutationObserver(updateDisabledState).observe(selector, { childList: true, subtree: true });
    updateDisabledState();
  }

  function mountWorkspaceChrome() {
    const app = document.querySelector(".app");
    if (!app) return;
    const panels = Array.from(app.children).filter((child) => child.classList.contains("panel"));
    const workspace = panels[1];
    if (!workspace) return;
    workspace.classList.add("rw-workspace-panel");
    const heading = Array.from(workspace.children).find((child) =>
      child.matches(".head, .meta-head, .brand")
    );
    heading?.classList.add("rw-workspace-head");
    const tabs = Array.from(workspace.children).find((child) =>
      child.matches(".tabs, .detail-tabs")
    );
    tabs?.classList.add("rw-workspace-tabs");
  }

  async function executeStage(current) {
    const button = document.querySelector("#stageExecute");
    const status = document.querySelector("#stageExecuteStatus");
    const projectId = projectForAction();
    if (!projectId) {
      status.textContent = t("Select a project before continuing.");
      return;
    }
    setProject(projectId);
    button.disabled = true;
    status.textContent = t("Generating stage outputs...");
    const runnableStages = new Set(["sections", "figures", "figure-review", "draft", "final"]);
    const endpoint = current.id === "blueprint"
      ? `/api/project/${encodeURIComponent(projectId)}/section-tasks`
      : runnableStages.has(current.id)
        ? `/api/project/${encodeURIComponent(projectId)}/run/${encodeURIComponent(current.id)}`
        : `/api/project/${encodeURIComponent(projectId)}/handoff/${encodeURIComponent(current.id)}`;
    try {
      const response = await fetch(endpoint, { method: "POST" });
      const result = await response.json().catch(() => ({ ok: false, error: message("serverReturned", { status: response.status }) }));
      if (!response.ok || !result.ok) throw new Error(result.error || message("serverReturned", { status: response.status }));
      const nextPath = current.id === "blueprint"
        ? `/sections?project=${encodeURIComponent(projectId)}`
        : result.next_path;
      if (nextPath) {
        window.location.assign(nextPath);
      } else {
        status.textContent = t("Final stage recorded.");
        button.disabled = false;
      }
    } catch (error) {
      status.textContent = error.message || String(error);
      button.disabled = false;
    }
  }

  function stageActionHost(current) {
    const actionHostSelectors = {
      library: "#libraryStageAction",
      discovery: "#projectInfo",
      matrix: "#summary",
      blueprint: "#summary",
      sections: "#summary",
      "figure-review": "#savedStatus",
      figures: "#summary",
      draft: "#summaryBox",
    };
    const anchor = document.querySelector(actionHostSelectors[current.id] || "");
    if (anchor?.hasAttribute("data-stage-action-host")) return anchor;
    const directHead = anchor?.closest(".head, .meta-head, .brand");
    if (directHead) return directHead;
    const panel = anchor?.closest(".panel");
    const stableHead = panel?.querySelector(".head, .meta-head, .brand");
    if (stableHead) return stableHead;
    const heads = Array.from(document.querySelectorAll(".panel .head, .panel .meta-head, .panel .brand"));
    return heads.find((head) => /review gate|quality gate|human check|审核门控|质量门控|人工检查/i.test(head.textContent || "")) || null;
  }

  function mountStageAction(current, actionLabel) {
    if (current.id === "final") return;
    const reviewGate = stageActionHost(current);
    if (!reviewGate) return;
    const action = document.createElement("div");
    action.className = "stage-execute-wrap";
    action.innerHTML = `
      <button id="stageExecute" class="stage-execute">${t(actionLabel)}</button>
      <div id="stageExecuteStatus" class="stage-execute-status"></div>
    `;
    const description = reviewGate.querySelector(".sub");
    if (description) description.insertAdjacentElement("afterend", action);
    else reviewGate.appendChild(action);
    document.querySelector("#stageExecute")?.addEventListener("click", () => executeStage(current));
  }

  function refreshReviewUiLanguage() {
    const id = currentId();
    const current = stages.find((stage) => stage.id === id) || stages[0];
    const next = stages[stages.findIndex((stage) => stage.id === current.id) + 1];
    const actionLabel = current.id === "final"
      ? "Validate Final Stage"
      : `Execute ${current.label} and Enter ${next?.label || "Next Stage"}`;
    const kicker = document.querySelector(".stage-kicker");
    const stageName = document.querySelector(".stage-name");
    const stageHint = document.querySelector(".stage-hint");
    if (kicker) kicker.textContent = t("Human Check Stage");
    if (stageName) stageName.textContent = t(current.label);
    if (stageHint) stageHint.textContent = t(current.hint);
    document.querySelectorAll(".stage-step").forEach((link, index) => {
      if (stages[index]) link.textContent = t(stages[index].label);
    });
    const executeButton = document.querySelector("#stageExecute");
    if (executeButton) executeButton.textContent = t(actionLabel);
    const deleteButton = document.querySelector("#delete-project");
    if (deleteButton) {
      deleteButton.textContent = t("Delete project");
      deleteButton.title = t("Permanently delete the selected project");
    }
  }

  function init() {
    const id = currentId();
    document.body.classList.add(`page-${id}`);
    mountWorkspaceChrome();
    const nav = document.querySelector(".nav");
    if (!nav) return;
    mountProjectDeleteControl();
    if (document.querySelector(".stage-strip")) return;
    const current = stages.find((s) => s.id === id) || stages[0];
    const next = stages[stages.findIndex((stage) => stage.id === current.id) + 1];
    const actionLabel = current.id === "final"
      ? "Validate Final Stage"
      : `Execute ${current.label} and Enter ${next?.label || "Next Stage"}`;
    const strip = document.createElement("div");
    strip.className = "stage-strip";
    strip.innerHTML = `
      <div class="stage-current">
        <div class="stage-kicker">Human Check Stage</div>
        <div class="stage-name">${current.label}</div>
        <div class="stage-hint">${current.hint}</div>
      </div>
      <div class="stage-steps">
        ${stages.map((s, i) => `<a class="stage-step ${s.id === id ? "active" : ""}" data-index="${i + 1}" href="${s.href}">${s.label}</a>`).join("")}
      </div>
    `;
    nav.insertAdjacentElement("afterend", strip);
    syncProjectLinks();
    mountStageAction(current, actionLabel);
  }

  window.reviewUi = { setProject, initialProject };
  document.addEventListener("change", (event) => {
    if (event.target.matches("#projectSelect, #globalProjectSelect")) {
      setProject(event.target.value);
    }
  });
  window.addEventListener("review-language-change", refreshReviewUiLanguage);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
