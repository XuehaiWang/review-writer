(function () {
  const stages = [
    { id: "library", label: "Library", href: "/library", hint: "Verify PDFs, MinerU Markdown, titles, authors, abstracts, eight structured tags, and paths." },
    { id: "discovery", label: "Discovery", href: "/discovery", hint: "Remove irrelevant keywords and papers, then confirm the candidate literature set." },
    { id: "planning", label: "Analysis & Planning", href: "/planning?tab=matrix", hint: "Review the literature matrix, organize the outline, and confirm the section blueprint in one workspace." },
    { id: "sections", label: "Sections", href: "/sections", hint: "Review section prose, paper grounding, and paragraph-level figure candidates." },
    { id: "images", label: "Image Processing", href: "/images?tab=review", hint: "Select source figures, redraw them, edit SVG or chemical structures, and complete human approval." },
    { id: "draft", label: "Draft", href: "/draft", hint: "Edit the first draft, evaluate quality, review rewrite candidates, and human-approve the exact version." },
    { id: "final", label: "Final", href: "/final", hint: "Assemble approved content, conclusion, overview figure, audit, and release files." },
  ];
  const workspaceDefinitions = Object.freeze({
    planning: Object.freeze([
      Object.freeze({ id: "matrix", label: "Literature Matrix", href: "/planning?tab=matrix" }),
      Object.freeze({ id: "blueprint", label: "Outline & Blueprint", href: "/planning?tab=blueprint" }),
    ]),
    images: Object.freeze([
      Object.freeze({ id: "review", label: "Source Figure Review", href: "/images?tab=review" }),
      Object.freeze({ id: "redraw", label: "AI Redraw & Manual Edit", href: "/images?tab=redraw" }),
    ]),
  });
  const legacyStageIds = Object.freeze({
    matrix: "planning",
    blueprint: "planning",
    "figure-review": "images",
    figures: "images",
  });

  function currentId(pathname = "") {
    const rawPath = pathname || (typeof location !== "undefined" ? location.pathname : "/library");
    const path = String(rawPath).replace(/^\/+|\/+$/g, "") || "library";
    const canonical = legacyStageIds[path] || path;
    return stages.some((stage) => stage.id === canonical) ? canonical : "library";
  }

  function workspaceTabs(stageId) {
    return Array.from(workspaceDefinitions[stageId] || [], (tab) => ({ ...tab }));
  }

  function workspaceStepPlacement(stageId) {
    return ["planning", "images"].includes(stageId) ? "middle-header" : "below-stage-strip";
  }

  function activeWorkspaceTab(stageId, search = "") {
    const tabs = workspaceDefinitions[stageId] || [];
    if (!tabs.length) return "";
    if (stageId === "planning" && typeof location !== "undefined") {
      if (/\/blueprint\/?$/.test(location.pathname)) return "blueprint";
      if (/\/matrix\/?$/.test(location.pathname)) return "matrix";
    }
    const requested = new URLSearchParams(String(search || "").replace(/^\?/, "")).get("tab") || "";
    return tabs.some((tab) => tab.id === requested) ? requested : tabs[0].id;
  }

  function withProject(path, projectId) {
    const url = new URL(path, "http://review-writer.local");
    if (projectId) url.searchParams.set("project", projectId);
    return `${url.pathname}${url.search}`;
  }

  function stageActionSpec(stageId, tab, projectId) {
    const encoded = encodeURIComponent(projectId || "");
    if (stageId === "planning" && tab === "matrix") return {
      backendStage: "matrix",
      endpoint: `/api/v1/projects/${encoded}/planning/blueprint`,
      nextPath: withProject("/planning?tab=blueprint", projectId),
      label: "Generate Blueprint from Current Matrix and Outline",
    };
    if (stageId === "planning") return {
      backendStage: "blueprint",
      endpoint: `/api/v1/projects/${encoded}/planning/blueprint/confirm`,
      nextPath: withProject("/sections", projectId),
      label: "Confirm Blueprint and Enter Sections",
    };
    if (stageId === "images" && tab === "review") return {
      backendStage: "figure-review",
      endpoint: `/api/v1/projects/${encoded}/figures/review/confirm`,
      nextPath: withProject("/images?tab=redraw", projectId),
      label: "Confirm Source Figures and Continue to AI Redraw",
    };
    if (stageId === "images") return {
      backendStage: "figures",
      endpoint: `/api/v1/projects/${encoded}/figures/confirm`,
      nextPath: withProject("/draft", projectId),
      label: "Confirm Images and Enter Draft",
    };
    return null;
  }

  const stageModel = {
    stages,
    currentId,
    workspaceTabs,
    workspaceStepPlacement,
    activeWorkspaceTab,
    stageActionSpec,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = stageModel;
  if (typeof window === "undefined" || typeof document === "undefined") return;

  function t(value) {
    return window.reviewI18n?.t(value) || value;
  }

  function message(key, params) {
    return window.reviewI18n?.message(key, params) || key;
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
    const stagePaths = new Set(stages.map((stage) => new URL(stage.href, location.origin).pathname));
    document.querySelectorAll('a[href^="/"]').forEach((link) => {
      const url = new URL(link.getAttribute("href"), location.origin);
      if (!stagePaths.has(url.pathname)) return;
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
        const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
        if (!response.ok) {
          const result = await response.json().catch(() => ({}));
          throw new Error(result.detail || result.error?.message || message("serverReturned", { status: response.status }));
        }
        const projectPayload = await (await fetch("/api/v1/projects")).json();
        const nextProjectId = projectPayload.items?.[0]?.project_id || "";
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

  function stageActionReadiness(current) {
    if (
      current.id !== "images"
      || activeWorkspaceTab(current.id, location.search) !== "redraw"
    ) return null;
    if (typeof window.reviewFigureStageReadiness !== "function") {
      return { ready: false, message: "Loading image readiness..." };
    }
    return window.reviewFigureStageReadiness();
  }

  function syncStageActionReadiness(current) {
    const readiness = stageActionReadiness(current);
    if (!readiness) return;
    const button = document.querySelector("#stageExecute");
    const status = document.querySelector("#stageExecuteStatus");
    if (!button) return;
    button.disabled = !readiness.ready;
    button.setAttribute("aria-disabled", String(!readiness.ready));
    if (status) status.textContent = t(readiness.message || "");
  }

  async function executeStage(current) {
    const button = document.querySelector("#stageExecute");
    const status = document.querySelector("#stageExecuteStatus");
    // Library is a workspace-wide source collection, not a project stage.
    // Project creation belongs to Discovery, so entering Discovery must never
    // be gated by a project that does not exist yet.
    if (current.id === "library") {
      button.disabled = true;
      status.textContent = t("Opening project creation...");
      window.location.assign("/discovery?create=1");
      return;
    }
    const projectId = projectForAction();
    if (!projectId) {
      status.textContent = t("Select a project before continuing.");
      return;
    }
    const readiness = stageActionReadiness(current);
    if (readiness && !readiness.ready) {
      syncStageActionReadiness(current);
      return;
    }
    setProject(projectId);
    button.disabled = true;
    status.textContent = t("Generating stage outputs...");
    if (current.id === "sections" && typeof window.reviewSectionsExecuteStage === "function") {
      try {
        const result = await window.reviewSectionsExecuteStage({ button, status, projectId });
        if (result?.nextPath) window.location.assign(result.nextPath);
        else {
          button.disabled = false;
          status.textContent = t("Current section outputs are ready for review.");
        }
      } catch (error) {
        button.disabled = false;
        status.textContent = t(error.message || String(error));
      }
      return;
    }
    const workspaceAction = stageActionSpec(
      current.id,
      activeWorkspaceTab(current.id, location.search),
      projectId,
    );
    const endpoint = workspaceAction?.endpoint || "";
    try {
      if (current.id === "draft" && typeof window.reviewDraftSaveForHandoff === "function") {
        status.textContent = t("Saving current draft...");
        const saved = await window.reviewDraftSaveForHandoff();
        if (!saved) throw new Error(t("Save the current draft before continuing."));
        if (typeof window.reviewDraftApproveForHandoff === "function") {
          status.textContent = t("Confirming evaluated draft...");
          const approved = await window.reviewDraftApproveForHandoff();
          if (!approved) throw new Error(t("Evaluate and approve the current draft before continuing."));
        }
        status.textContent = t("Handing off current draft...");
        window.location.assign(`/final?project=${encodeURIComponent(projectId)}`);
        return;
      }
      if (!workspaceAction) throw new Error(t("This stage has no pending transition action."));
      const request = { method: "POST" };
      if (current.id === "planning") {
        const planning = typeof window.reviewPlanningState === "function"
          ? window.reviewPlanningState()
          : {};
        request.headers = { "Content-Type": "application/json" };
        request.body = JSON.stringify({ revision: Number(planning.blueprintRevision || 0) });
      }
      if (current.id === "images") {
        const imageState = activeWorkspaceTab(current.id, location.search) === "review"
          ? (typeof window.reviewFigureReviewState === "function" ? window.reviewFigureReviewState() : {})
          : (typeof window.reviewFiguresState === "function" ? window.reviewFiguresState() : {});
        request.headers = { "Content-Type": "application/json" };
        request.body = JSON.stringify({ revision: Number(imageState.revision || 0) });
      }
      const response = await fetch(endpoint, request);
      const result = await response.json().catch(() => ({ ok: false, error: message("serverReturned", { status: response.status }) }));
      if (!response.ok || result?.ok === false) throw new Error(result?.error?.message || result?.detail || result?.error || message("serverReturned", { status: response.status }));
      const nextPath = workspaceAction.nextPath || result.next_path;
      if (nextPath) {
        window.location.assign(nextPath);
      } else {
        status.textContent = t("Final stage recorded.");
        button.disabled = false;
      }
    } catch (error) {
      button.disabled = false;
      syncStageActionReadiness(current);
      status.textContent = t(error.message || String(error));
    }
  }

  function stageActionHost(current) {
    const workspaceTab = activeWorkspaceTab(current.id, location.search);
    const actionHostSelectors = {
      library: "#libraryStageAction",
      discovery: "#projectInfo",
      planning: "#summary",
      sections: "#summary",
      images: workspaceTab === "review" ? "#savedStatus" : "#summary",
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
    // Discovery already has a stronger "Confirm and continue" action: it
    // saves the review, confirms it, synchronizes Matrix, and navigates.
    // A second generic handoff button only duplicates that transition and
    // takes space away from the keyword review list.
    if (current.id === "final") return;
    if (current.id === "discovery") return;
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
    syncStageActionReadiness(current);
  }

  function refreshReviewUiLanguage() {
    const id = currentId();
    const current = stages.find((stage) => stage.id === id) || stages[0];
    const next = stages[stages.findIndex((stage) => stage.id === current.id) + 1];
    const workspaceAction = stageActionSpec(current.id, activeWorkspaceTab(current.id, location.search), projectForAction());
    const actionLabel = workspaceAction?.label || (current.id === "library"
      ? "Enter Discovery and Create Project"
      : current.id === "final"
        ? "Validate Final Stage"
        : `Execute ${current.label} and Enter ${next?.label || "Next Stage"}`);
    const kicker = document.querySelector(".stage-kicker");
    const stageName = document.querySelector(".stage-name");
    const stageHint = document.querySelector(".stage-hint");
    if (kicker) kicker.textContent = t("Human Check Stage");
    if (stageName) stageName.textContent = t(current.label);
    if (stageHint) stageHint.textContent = t(current.hint);
    document.querySelectorAll(".stage-step").forEach((link, index) => {
      if (stages[index]) link.textContent = t(stages[index].label);
    });
    document.querySelectorAll("[data-workspace-step]").forEach((link) => {
      const tab = workspaceTabs(id).find((item) => item.id === link.dataset.workspaceStep);
      if (tab) link.querySelector("span:last-child").textContent = t(tab.label);
    });
    const executeButton = document.querySelector("#stageExecute");
    if (executeButton) executeButton.textContent = t(actionLabel);
    syncStageActionReadiness(current);
    const deleteButton = document.querySelector("#delete-project");
    if (deleteButton) {
      deleteButton.textContent = t("Delete project");
      deleteButton.title = t("Permanently delete the selected project");
    }
  }

  function mountWorkspaceSteps(stageId, stageStrip) {
    const tabs = workspaceTabs(stageId);
    if (!tabs.length || document.querySelector(".workspace-step-strip")) return;
    const activeTab = activeWorkspaceTab(stageId, location.search);
    const strip = document.createElement("nav");
    strip.className = "workspace-step-strip";
    strip.setAttribute("aria-label", t("Current stage steps"));
    strip.innerHTML = tabs.map((tab, index) => `
      <a class="workspace-step ${tab.id === activeTab ? "active" : ""}" data-workspace-step="${tab.id}" href="${tab.href}">
        <span class="workspace-step-number">${index + 1}</span>
        <span>${t(tab.label)}</span>
      </a>
    `).join("");
    if (workspaceStepPlacement(stageId) === "middle-header") {
      const panels = Array.from(document.querySelector(".app")?.children || [])
        .filter((child) => child.classList.contains("panel"));
      const middlePanel = panels[1];
      if (middlePanel) {
        const heading = Array.from(middlePanel.children)
          .find((child) => child.matches(".head, .meta-head, .brand"));
        const viewTabs = Array.from(middlePanel.children)
          .find((child) => child.matches(".tabs, .detail-tabs"));
        if (heading) {
          heading.classList.add("rw-workspace-flow-head");
          viewTabs?.classList.add("rw-workspace-view-tabs");
          strip.classList.add("workspace-step-strip-inline", "workspace-step-strip-head");
          heading.insertAdjacentElement("beforeend", strip);
          return;
        }
      }
    }
    stageStrip.insertAdjacentElement("afterend", strip);
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
    const workspaceAction = stageActionSpec(current.id, activeWorkspaceTab(current.id, location.search), projectForAction());
    const actionLabel = workspaceAction?.label || (current.id === "library"
      ? "Enter Discovery and Create Project"
      : current.id === "final"
        ? "Validate Final Stage"
        : `Execute ${current.label} and Enter ${next?.label || "Next Stage"}`);
    const strip = document.createElement("div");
    strip.className = "stage-strip";
    strip.innerHTML = `
      <div class="stage-current">
        <div class="stage-kicker">Human Check Stage</div>
        <div class="stage-name">${t(current.label)}</div>
        <div class="stage-hint">${t(current.hint)}</div>
      </div>
      <div class="stage-steps">
        ${stages.map((s, i) => `<a class="stage-step ${s.id === id ? "active" : ""}" data-index="${i + 1}" href="${s.href}">${t(s.label)}</a>`).join("")}
      </div>
    `;
    nav.insertAdjacentElement("afterend", strip);
    mountWorkspaceSteps(id, strip);
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
  window.addEventListener("review-stage-readiness-change", () => {
    const id = currentId();
    const current = stages.find((stage) => stage.id === id) || stages[0];
    syncStageActionReadiness(current);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
