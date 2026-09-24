"""Versioned, human-confirmed Final caption corrections; never regenerate Draft."""
import hashlib
import json
import re

from review_writer_api.database import utc_now
from review_writer_api.errors import WorkflowConflict, WorkflowNotFound, WorkflowValidationError
from review_writer_api.security import Permission
from review_writer_core.workflow.artifacts import DRAFT_MANUSCRIPT, FIGURE_MANIFEST

FINAL_FIGURE_REVIEWS = "final/figure-reviews.json"
INSERTED_FIGURE_METADATA = re.compile(r"<!--\s*inserted_figure:\s*(\{.*?\})\s*-->", re.DOTALL)


def figure_blocks(markdown):
    for match in INSERTED_FIGURE_METADATA.finditer(markdown):
        try:
            row = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        # Only the adjacent image and caption belong to this metadata record.
        block = re.match(r"\s*(!\[[^\n]*\]\([^\n]+\))\s*\n\s*\*([^\n]*)\*", markdown[match.end():])
        if block:
            yield match, block, row


def apply_figure_reviews(markdown, reviews, draft_id, manifest_id):
    for match, block, row in reversed(list(figure_blocks(markdown))):
        saved = (reviews.get("figures") or {}).get(str(row.get("figure_id"))) or {}
        if (saved.get("source_draft_id") != draft_id or saved.get("source_manifest_id") != manifest_id
                or saved.get("output_artifact_id") != row.get("output_artifact_id")):
            continue
        row.update(interpretation_basis="human_source_review", caption_quality={"status": "human_confirmed"},
                   human_source_review={"reviewed_at": saved["reviewed_at"], "reviewed_by": saved["reviewed_by"],
                                        "source_location": saved.get("source_location", "")})
        caption = saved["caption"]
        # Preserve source credit that was inserted by Draft.
        credit = re.search(r"(?:Reproduced|Adapted) from Ref\. \d+\.", block.group(2))
        if credit and credit.group() not in caption:
            caption = caption.rstrip(".") + ". " + credit.group()
        replacement = "<!-- inserted_figure: " + json.dumps(row, ensure_ascii=False) + " -->\n"
        replacement += block.group(1) + "\n*" + str(row.get("published_label") or "") + ". " + caption + "*"
        markdown = markdown[:match.start()] + replacement + markdown[match.end() + block.end():]
    return markdown


class FinalFigureReviewMixin:
    def figure_review(self, principal, project_id, figure_id):
        self._owned_project(principal, project_id)
        markdown, draft, _ = self._approved_draft(principal, project_id)
        matches = [(m, b, r) for m, b, r in figure_blocks(markdown) if r.get("figure_id") == figure_id]
        if len(matches) != 1:
            raise WorkflowNotFound("无法唯一定位这张图，请先同步终稿；仍无法定位时联系管理员。")
        match, block, row = matches[0]
        manifest, artifact = self._read_json(principal, project_id, FIGURE_MANIFEST)
        source = next((r for r in manifest.get("figures", []) if r.get("figure_id") == figure_id
                       and r.get("output_artifact_id") == row.get("output_artifact_id")), {})
        source_id = str(source.get("source_artifact_id") or source.get("source_image_artifact_id") or "")
        blockers = []
        if not source or source.get("paper_id") != row.get("paper_id"):
            blockers.append("图片来源记录与当前稿件不一致，请在图像阶段核对后重新同步。")
        for key, name in (("output_artifact_id", "当前图片"),):
            try:
                resolved = self.artifacts.resolve_owned_artifact(principal.user_id, str(row.get(key) or ""))
                if resolved.artifact.project_id != project_id:
                    raise WorkflowNotFound("Figure does not belong to this project.")
            except (WorkflowNotFound, WorkflowConflict, ValueError):
                blockers.append(f"{name}不可用，请在图像阶段检查。")
        try:
            self.artifacts.resolve_owned_artifact(principal.user_id, source_id)
        except (WorkflowNotFound, WorkflowConflict, ValueError):
            blockers.append("原图来源不可用，请在图像阶段补全来源或重新选择图片。")
            source_id = ""
        paper = self._catalog(principal, [str(row.get("paper_id") or "")]).get(row.get("paper_id"))
        if paper is None:
            blockers.append("来源论文不可用，请先到文献库核对。")
        if row.get("source_identity_status") == "unresolved" or not row.get("source_label"):
            blockers.append("原论文图号尚未确认，请在图像阶段核对来源，不能直接标记通过。")
        manifest_id = artifact.id if artifact else ""
        reviews, review_artifact = self._read_json(principal, project_id, FINAL_FIGURE_REVIEWS)
        fingerprint = hashlib.sha256(f"{draft.id}:{manifest_id}:{review_artifact.id if review_artifact else ''}".encode()).hexdigest()
        displayed = apply_figure_reviews(markdown, reviews, draft.id, manifest_id)
        current = next(b.group(2) for _, b, r in figure_blocks(displayed) if r.get("figure_id") == figure_id)
        caption = re.sub(r"^" + re.escape(str(row.get("published_label") or "")) + r"\.\s*", "", current)
        return {"figure_id": figure_id, "published_label": row.get("published_label"), "caption": caption,
                "output_artifact_id": row.get("output_artifact_id"), "source_artifact_id": source_id,
                "paper_id": row.get("paper_id"), "paper_title": paper.title if paper else "",
                "source_label": row.get("source_label"),
                "source_caption": source.get("source_caption_text") or source.get("caption_source_text") or "",
                "context_md": markdown[max(0, markdown.rfind("\n\n", 0, max(0, match.start()-2))):match.start()].strip(),
                "blockers": blockers, "fingerprint": fingerprint, "source_draft_id": draft.id,
                "source_manifest_id": manifest_id, "revision": self._revision(principal, project_id)}

    def save_figure_review(self, principal, project_id, figure_id, payload):
        principal.require(Permission.PROJECT_WRITE)
        current = self.figure_review(principal, project_id, figure_id)
        if current["fingerprint"] != payload.fingerprint:
            raise WorkflowConflict("图片、初稿或核对记录已变化，请重新打开窗口。")
        if current["blockers"]:
            raise WorkflowValidationError("；".join(current["blockers"]))
        caption = " ".join(payload.caption.split())
        if any(c in caption for c in "<>*`") or re.search(r"!?\[[^\]]*\]\(", caption):
            raise WorkflowValidationError("图注请使用纯文本，不要插入图片、HTML 或 Markdown 标记。")
        value, artifact = self._read_json(principal, project_id, FINAL_FIGURE_REVIEWS)
        value.setdefault("figures", {})[figure_id] = {
            "caption": caption, "source_location": payload.source_location,
            "source_draft_id": current["source_draft_id"], "source_manifest_id": current["source_manifest_id"],
            "output_artifact_id": current["output_artifact_id"],
            "reviewed_at": utc_now().isoformat(), "reviewed_by": principal.user_id,
        }
        published, state = self._publish_files(principal, project_id,
            {FINAL_FIGURE_REVIEWS: ((json.dumps(value, ensure_ascii=False)+"\n").encode(), "json")},
            expected_revision=current["revision"], expected_current_artifacts={
                DRAFT_MANUSCRIPT: current["source_draft_id"], FIGURE_MANIFEST: current["source_manifest_id"],
                FINAL_FIGURE_REVIEWS: artifact.id if artifact else ""}, metadata={"operation": "figure-review"})
        return {"saved": True, "revision": state.revision}
