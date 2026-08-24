import { Link } from "react-router-dom";

import type { AuthConfig, Principal } from "../../api/types";
import { PublicHeader } from "../../components/PublicHeader";
import { useUiText } from "../../i18n/useUiText";

const workflowStages = [
  ["01–02", "文献准备与检索", "Literature preparation & discovery", "批量上传PDF，经MinerU精确解析和全文索引后，与联网结果、metadata及项目领域规则共同完成召回和Matrix确认。", "Upload PDFs in batches, parse them precisely with MinerU, build full-text indexes, and combine local evidence, web results, metadata, and project-specific rules before Matrix confirmation."],
  ["03–04", "证据规划与章节写作", "Evidence planning & section writing", "从已确认Matrix生成综述大纲、章节Blueprint与写作任务，使论点、段落和主要论文保持明确绑定。", "Turn the confirmed Matrix into an outline, section blueprints, and writing tasks that keep claims, paragraphs, and primary papers explicitly linked."],
  ["05–06", "图像与初稿优化", "Figures & draft optimization", "人工选择源图后进行AI重绘和可编辑SVG修订，并通过段落评估、安全重写与人工对比持续优化初稿。", "Select source figures before AI redraw and editable SVG refinement, then improve the draft through paragraph evaluation, safe rewrites, and human comparison."],
  ["07", "审计与交付", "Audit & delivery", "生成综述总览图和结论，完成引用、格式与版本审计，并交付排版后的Word和PDF。", "Generate the review overview and conclusion, audit references, formatting, and versions, then deliver formatted Word and PDF files."],
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
            <p className="eyebrow">{text("面向科研团队的可追溯综述生产系统", "Traceable review production for research teams")}</p>
            <h1>{text("从文献证据到可交付终稿，始终保持可追溯。", "From literature evidence to a deliverable review—fully traceable.")}</h1>
            <p className="lead">{text("Review Writer 把PDF精确解析、混合检索、证据矩阵、章节写作、科研图像处理、质量评估、人工确认和Word/PDF交付组织在同一个持久工作流中。", "Review Writer brings precise PDF parsing, hybrid retrieval, evidence matrices, section writing, scientific figure processing, quality evaluation, human approval, and Word/PDF delivery into one persistent workflow.")}</p>
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
              <article className="complete"><b>03</b><span>{text("大纲", "Outline")}</span><small>{text("11 节已确认", "11 confirmed")}</small></article>
              <article className="complete"><b>05</b><span>{text("图像", "Figures")}</span><small>{text("20 张已审核", "20 reviewed")}</small></article>
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
            <article><span>01</span><h3>{text("解析与混合检索", "Parsing and hybrid retrieval")}</h3><p>{text("MinerU结构化内容、本地全文索引、metadata与联网结果进入统一候选池；领域规则只在项目分类匹配时参与扩展。", "MinerU structure, local full-text indexes, metadata, and web results enter one candidate pool, while domain rules expand queries only for matching project profiles.")}</p></article>
            <article><span>02</span><h3>{text("确认后生效的阶段依赖", "Confirmation-aware dependencies")}</h3><p>{text("重新检索只产生待确认候选；确认采用Matrix后，系统才根据主题和论文集合的实际变化判断后续产物是否过期。", "A rerun creates pending candidates only; downstream artifacts are evaluated for staleness after you confirm the Matrix and its topic or paper set has actually changed.")}</p></article>
            <article><span>03</span><h3>{text("证据绑定的写作与图像", "Evidence-bound writing and figures")}</h3><p>{text("Blueprint、章节论点和段落绑定主要论文；源图经人工选择后再重绘，并保留完整性审核、SVG与Ketcher编辑。", "Blueprints, section claims, and paragraphs stay linked to primary papers; source figures are selected by people before redraw, integrity review, SVG editing, and Ketcher refinement.")}</p></article>
            <article><span>04</span><h3>{text("质量闭环与双格式交付", "Quality loop and dual-format delivery")}</h3><p>{text("段落评分、安全优化、人工版本确认、引用与格式审计共同约束终稿，并输出排版后的Word和PDF。", "Paragraph scoring, safe optimization, human version approval, and reference and format audits govern the final formatted Word and PDF outputs.")}</p></article>
          </div>
        </section>

        <section className="product-section workflow-showcase" id="workflow">
          <header className="product-section-heading split">
            <div><p className="eyebrow">{text("七阶段核心工作流", "Seven-stage core workflow")}</p><h2>{text("每一步都有输入、产物与下一步依赖。", "Every step has inputs, artifacts, and explicit downstream dependencies.")}</h2></div>
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
            <p>{text("账户、项目、文献权限、任务与用量按用户隔离；文本模型按项目选择，文本、图像和MinerU凭据由服务器统一加密管理，浏览器不会取得API Key。", "Accounts, projects, library permissions, jobs, and usage are isolated per user. Text models are selected per project, while text, image, and MinerU credentials are encrypted and managed centrally without exposing API keys to the browser.")}</p>
          </div>
          <ul>
            <li><strong>{text("持久任务状态", "Persistent job state")}</strong><span>{text("跨页面与刷新保持上传、解析和生成进度", "Uploads, parsing, and generation survive navigation and refreshes")}</span></li>
            <li><strong>{text("服务端模型网关", "Server-side model gateway")}</strong><span>{text("按项目锁定模型、记录Token和实际结算成本", "Lock models per project and track tokens and settled usage costs")}</span></li>
            <li><strong>{text("人工门控与审计", "Human gates and audits")}</strong><span>{text("关键论文、图像、重写和最终产物均可检查确认", "Review and approve critical papers, figures, rewrites, and final artifacts")}</span></li>
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
