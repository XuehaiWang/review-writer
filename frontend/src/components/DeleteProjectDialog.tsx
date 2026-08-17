import { useEffect, useId, useRef, useState } from "react";

import type { Project } from "../api/types";
import { useUiText } from "../i18n/useUiText";

type DeleteProjectDialogProps = {
  project: Project | null;
  deleting?: boolean;
  error?: string;
  onCancel: () => void;
  onConfirm: (project: Project) => void;
};

export function DeleteProjectDialog({ project, deleting = false, error, onCancel, onConfirm }: DeleteProjectDialogProps) {
  const { text } = useUiText();
  const [typedName, setTypedName] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const deletingRef = useRef(deleting);
  const cancelRef = useRef(onCancel);
  const titleId = useId();
  const descriptionId = useId();

  deletingRef.current = deleting;
  cancelRef.current = onCancel;

  useEffect(() => {
    if (!project) return;
    setTypedName("");
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => inputRef.current?.focus(), 0);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !deletingRef.current) cancelRef.current();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [project]);

  if (!project) return null;

  const nameMatches = typedName === project.slug;
  const submit = () => {
    if (nameMatches && !deleting) onConfirm(project);
  };

  return (
    <div
      className="confirmation-overlay"
      role="presentation"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget && !deleting) onCancel();
      }}
    >
      <section
        className="confirmation-dialog delete-project-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
      >
        <header className="confirmation-dialog-header">
          <span className="confirmation-dialog-icon" aria-hidden="true">!</span>
          <div>
            <p className="eyebrow">{text("危险操作", "Destructive action")}</p>
            <h2 id={titleId}>{text("永久删除项目", "Permanently delete project")}</h2>
          </div>
          <button className="dialog-close" type="button" aria-label={text("关闭", "Close")} disabled={deleting} onClick={onCancel}>×</button>
        </header>

        <div className="confirmation-dialog-body">
          <p id={descriptionId}>
            {text("此操作将永久删除该项目及其所有阶段产物，删除后无法恢复。", "This permanently deletes the project and all stage outputs. This action cannot be undone.")}
          </p>
          <div className="delete-project-summary">
            <span>{text("待删除项目", "Project to delete")}</span>
            <strong>{project.slug}</strong>
            {project.topic ? <small>{project.topic}</small> : null}
          </div>
          <label className="delete-project-name-field" htmlFor={`${titleId}-name`}>
            <span>{text("输入项目名称以确认", "Type the project name to confirm")}</span>
            <input
              ref={inputRef}
              id={`${titleId}-name`}
              value={typedName}
              autoComplete="off"
              spellCheck={false}
              placeholder={project.slug}
              aria-label={text("输入项目名称以确认", "Type the project name to confirm")}
              aria-invalid={typedName.length > 0 && !nameMatches}
              onChange={(event) => setTypedName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  submit();
                }
              }}
            />
            <small className={typedName.length > 0 && !nameMatches ? "name-mismatch" : ""}>
              {typedName.length > 0 && !nameMatches
                ? text("项目名称不一致，请重新输入。", "The project name does not match.")
                : text("名称必须完全一致，前后不能有多余字符。", "The name must match exactly.")}
            </small>
          </label>
          {error ? <p className="message message-error dialog-error" role="alert">{error}</p> : null}
        </div>

        <footer className="confirmation-dialog-actions">
          <button className="button button-quiet" type="button" disabled={deleting} onClick={onCancel}>{text("取消", "Cancel")}</button>
          <button className="button button-danger button-danger-solid" type="button" disabled={!nameMatches || deleting} onClick={submit}>
            {deleting ? text("正在删除…", "Deleting…") : text("永久删除", "Delete permanently")}
          </button>
        </footer>
      </section>
    </div>
  );
}
