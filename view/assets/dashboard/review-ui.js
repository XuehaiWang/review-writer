(function () {
  const stages = [
    { id: "library", label: "Library", href: "/library", hint: "核对 PDF、MinerU Markdown、标题、作者、摘要、8 个结构化标签和路径。" },
    { id: "discovery", label: "Discovery", href: "/discovery", hint: "删除不相关关键词和论文，确认 20-30 篇候选文献。" },
    { id: "matrix", label: "Matrix", href: "/matrix", hint: "检查每篇文献的固定字段、1000 词主内容和最相关图。" },
    { id: "blueprint", label: "Blueprint", href: "/blueprint", hint: "确认章节、论点、分配论文、图表需求和写作约束。" },
    { id: "sections", label: "Sections", href: "/sections", hint: "检查分章节草稿是否按一段一文献展开，并绑定图候选。" },
    { id: "figures", label: "Figures", href: "/figures", hint: "核对源图是否定位成功，重绘图是否只改风格不改化学内容。" },
    { id: "draft", label: "Draft", href: "/draft", hint: "检查合并初稿的连贯性、图片插入、术语统一和剩余问题。" },
    { id: "final", label: "Final", href: "/final", hint: "最终核对内容、格式、引用、图片和 release report。" },
  ];
  stages.splice(5, 0, { id: "figure-review", label: "Figure Review", href: "/figure-review", hint: "Select the final source figure for every cited paper before batch redraw." });

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
    button.type = "button";
    button.textContent = "Delete project";
    button.title = "Permanently delete the selected project";
    button.style.cssText = "margin-left:8px;padding:7px 10px;border:1px solid #a2352c;border-radius:999px;background:#fff7f5;color:#a2352c;cursor:pointer;";
    const updateDisabledState = () => {
      button.disabled = !selector.value;
      button.style.opacity = button.disabled ? "0.5" : "1";
      button.style.cursor = button.disabled ? "not-allowed" : "pointer";
    };
    button.addEventListener("click", async () => {
      const projectId = selector.value || projectForAction();
      if (!projectId) return;
      const typed = window.prompt(
        `Type ${projectId} to permanently delete this project and all of its outputs.`,
        "",
      );
      if (typed !== projectId) {
        window.alert("Project ID did not match. Nothing was deleted.");
        return;
      }
      button.disabled = true;
      try {
        const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
        const result = await response.json().catch(() => ({ ok: false, error: `Server returned ${response.status}.` }));
        if (!response.ok || !result.ok) throw new Error(result.error || `Server returned ${response.status}.`);
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

  async function executeStage(current) {
    const button = document.querySelector("#stageExecute");
    const status = document.querySelector("#stageExecuteStatus");
    const projectId = projectForAction();
    if (!projectId) {
      status.textContent = "Select a project before continuing.";
      return;
    }
    setProject(projectId);
    button.disabled = true;
    status.textContent = "Generating stage outputs...";
    const runnableStages = new Set(["sections", "figures", "figure-review", "draft", "final"]);
    const endpoint = current.id === "blueprint"
      ? `/api/project/${encodeURIComponent(projectId)}/section-tasks`
      : runnableStages.has(current.id)
        ? `/api/project/${encodeURIComponent(projectId)}/run/${encodeURIComponent(current.id)}`
        : `/api/project/${encodeURIComponent(projectId)}/handoff/${encodeURIComponent(current.id)}`;
    try {
      const response = await fetch(endpoint, { method: "POST" });
      const result = await response.json().catch(() => ({ ok: false, error: `Server returned ${response.status}.` }));
      if (!response.ok || !result.ok) throw new Error(result.error || `Server returned ${response.status}.`);
      const nextPath = current.id === "blueprint"
        ? `/sections?project=${encodeURIComponent(projectId)}`
        : result.next_path;
      if (nextPath) {
        window.location.assign(nextPath);
      } else {
        status.textContent = "Final stage recorded.";
        button.disabled = false;
      }
    } catch (error) {
      status.textContent = error.message || String(error);
      button.disabled = false;
    }
  }

  function mountStageAction(current, actionLabel) {
    if (current.id === "final") return;
    const heads = Array.from(document.querySelectorAll(".panel .head"));
    const reviewGate = heads.find((head) => /review gate|quality gate|human check/i.test(head.textContent || ""));
    if (!reviewGate) return;
    const action = document.createElement("div");
    action.className = "stage-execute-wrap";
    action.innerHTML = `
      <button id="stageExecute" class="stage-execute">${actionLabel}</button>
      <div id="stageExecuteStatus" class="stage-execute-status"></div>
    `;
    const description = reviewGate.querySelector(".sub");
    if (description) description.insertAdjacentElement("afterend", action);
    else reviewGate.appendChild(action);
    document.querySelector("#stageExecute")?.addEventListener("click", () => executeStage(current));
  }

  function init() {
    const id = currentId();
    document.body.classList.add(`page-${id}`);
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
