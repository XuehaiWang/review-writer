import { Link } from "react-router-dom";

import type { AuthConfig, Principal } from "../../api/types";
import { PublicHeader } from "../../components/PublicHeader";
import { useUiText } from "../../i18n/useUiText";

const workflowStages = [
  ["01–02", "文献与检索", "Literature & discovery", "批量导入、MinerU解析、metadata构建、检索筛选与人工确认。", "Batch import, MinerU parsing, metadata enrichment, discovery, screening, and human confirmation."],
  ["03–04", "分析与写作", "Analysis & writing", "由证据矩阵建立大纲与章节任务，让内容生成始终绑定已确认文献。", "Turn the evidence matrix into an outline and section tasks so writing stays grounded in confirmed papers."],
  ["05", "图像审核", "Figure review", "候选图选择、AI化学重绘、完整性校验与可编辑SVG人工修订。", "Select candidate figures, redraw chemistry with AI, validate integrity, and refine editable SVGs."],
  ["06–07", "优化与交付", "Revision & delivery", "段落反馈循环、终稿审计、引用核对以及Word文档导出。", "Run paragraph feedback loops, final audits, reference checks, and Word export."],
] as const;

export function LandingPage({ authConfig, identity }: { authConfig: AuthConfig; identity: Principal | null }) {
  const { text } = useUiText();
  const workspaceHref = identity ? "/workspace" : "/login";

  return (
    <div className="public-page">
      <PublicHeader authConfig={authConfig} identity={identity} />
      <main>
        <section className="product-hero">
          <div className="product-hero-copy">
            <p className="eyebrow">{text("面向科研团队的综述生产系统", "Review production for research teams")}</p>
            <h1>{text("从文献证据到可交付终稿，始终保持可追溯。", "From literature evidence to a deliverable review—fully traceable.")}</h1>
            <p className="lead">{text("Review Writer 把文献解析、主题检索、结构规划、章节写作、化学图像重绘、人工审核和终稿导出组织在同一个有状态工作流中。", "Review Writer brings literature parsing, discovery, planning, section writing, chemistry figure redraw, human review, and final delivery into one stateful workflow.")}</p>
            <div className="product-hero-actions">
              <Link className="button button-primary product-cta" to={workspaceHref}>{identity ? text("进入工作台", "Open workspace") : text("登录后开始", "Sign in to start")}</Link>
              <a className="button button-quiet product-cta" href="#workflow">{text("查看完整流程", "Explore the workflow")}</a>
            </div>
            <div className="product-trust-row" aria-label={text("产品特性", "Product qualities")}>
              <span>{text("证据驱动", "Evidence-grounded")}</span>
              <span>{text("人在回路", "Human-in-the-loop")}</span>
              <span>{text("版本可追溯", "Version-traceable")}</span>
            </div>
          </div>
          <div className="product-hero-visual" aria-label={text("综述工作流预览", "Review workflow preview")}>
            <div className="hero-status-line"><span>{text("当前项目", "Current project")}</span><strong>{text("轴手性联烯综述", "Axially chiral allene review")}</strong><em>{text("阶段 06", "Stage 06")}</em></div>
            <div className="hero-flow-map">
              <article className="complete"><b>01</b><span>{text("文献库", "Library")}</span><small>{text("30 篇已解析", "30 parsed")}</small></article>
              <article className="complete"><b>03</b><span>{text("大纲", "Outline")}</span><small>{text("12 节已确认", "12 confirmed")}</small></article>
              <article className="complete"><b>07</b><span>{text("图像", "Figures")}</span><small>{text("28 张已审核", "28 reviewed")}</small></article>
              <article className="active"><b>06</b><span>{text("初稿", "Draft")}</span><small>{text("正在优化", "Optimizing")}</small></article>
            </div>
            <div className="hero-document-preview">
              <div><span /><span /><span /></div>
              <p /><p /><p className="short" />
              <section><i /><i /><i /></section>
              <p /><p className="short" />
            </div>
          </div>
        </section>

        <section className="product-section" id="capabilities">
          <header className="product-section-heading">
            <p className="eyebrow">{text("核心能力", "Core capabilities")}</p>
            <h2>{text("不只是生成文本，而是管理一套科研生产流程。", "More than text generation: a managed research production system.")}</h2>
          </header>
          <div className="capability-grid">
            <article><span>01</span><h3>{text("统一证据底座", "Unified evidence base")}</h3><p>{text("PDF、MinerU内容、metadata、人工阅读记录和检索状态形成一致的数据来源。", "PDFs, MinerU content, metadata, reading notes, and screening decisions share one source of truth.")}</p></article>
            <article><span>02</span><h3>{text("明确阶段依赖", "Explicit stage dependencies")}</h3><p>{text("handoff与内容哈希识别上游变化，避免旧大纲、旧图像或旧段落进入新版本。", "Handoffs and content hashes detect upstream changes before stale outlines, figures, or paragraphs move forward.")}</p></article>
            <article><span>03</span><h3>{text("化学图像可控编辑", "Controlled chemistry figures")}</h3><p>{text("按图像类型重绘，支持人工完整性审核、SVG编辑和Ketcher结构修订。", "Redraw by figure type with human integrity approval, SVG editing, and Ketcher structure refinement.")}</p></article>
            <article><span>04</span><h3>{text("质量闭环", "Quality feedback loop")}</h3><p>{text("段落评分、rubrics、目标阈值、引用审计与Word导出共同约束最终交付。", "Paragraph scoring, rubrics, quality goals, citation audits, and Word export govern the final deliverable.")}</p></article>
          </div>
        </section>

        <section className="product-section workflow-showcase" id="workflow">
          <header className="product-section-heading split">
            <div><p className="eyebrow">{text("九阶段工作流", "Nine-stage workflow")}</p><h2>{text("每一步都有输入、产物与下一步依赖。", "Every step has inputs, artifacts, and explicit downstream dependencies.")}</h2></div>
            <p>{text("可以在任意阶段暂停、审核或返回修订；系统根据当前产物版本判断后续内容是否仍然有效。", "Pause, review, or revise at any stage while artifact versions determine whether downstream content remains current.")}</p>
          </header>
          <div className="workflow-stage-grid">
            {workflowStages.map(([stage, zhTitle, enTitle, zhBody, enBody]) => (
              <article key={stage}><span>{stage}</span><h3>{text(zhTitle, enTitle)}</h3><p>{text(zhBody, enBody)}</p></article>
            ))}
          </div>
        </section>

        <section className="product-section governance-section" id="governance">
          <div>
            <p className="eyebrow">{text("可信与隔离", "Trust and isolation")}</p>
            <h2>{text("面向真实项目，而不是一次性演示。", "Designed for real projects, not one-off demos.")}</h2>
            <p>{text("账户、项目、任务和模型设置按用户隔离；API密钥由服务端加密保存，登录状态使用HttpOnly Cookie维持。", "Accounts, projects, jobs, and provider settings are isolated per user. API keys are encrypted server-side and sessions use HttpOnly cookies.")}</p>
          </div>
          <ul>
            <li><strong>{text("持久状态", "Persistent state")}</strong><span>{text("跨页面与刷新保持任务进度", "Jobs survive navigation and refreshes")}</span></li>
            <li><strong>{text("人工门控", "Human gates")}</strong><span>{text("关键图像与段落由用户确认", "Users approve critical figures and prose")}</span></li>
            <li><strong>{text("可审计交付", "Auditable delivery")}</strong><span>{text("引用、格式和产物版本可检查", "References, formatting, and versions remain inspectable")}</span></li>
          </ul>
        </section>

        <section className="product-final-cta">
          <div><p className="eyebrow">Review Writer</p><h2>{text("把综述项目放进一条清晰、连续、可审核的生产线。", "Put your review into a clear, continuous, reviewable production line.")}</h2></div>
          <Link className="button button-primary product-cta" to={workspaceHref}>{identity ? text("继续当前工作", "Continue your work") : text("登录工作台", "Sign in to workspace")}</Link>
        </section>
      </main>
      <footer className="product-footer"><span>Review Writer</span><p>{text("证据驱动的科学综述工作台", "Evidence-grounded scientific review workspace")}</p></footer>
    </div>
  );
}
