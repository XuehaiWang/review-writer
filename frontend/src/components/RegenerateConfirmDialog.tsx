import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { useUiText } from "../i18n/useUiText";

type Props = {
  open: boolean;
  title: string;
  description: string;
  consequence: string;
  confirmLabel: string;
  onCancel: () => void;
  onConfirm: () => void;
};

export function RegenerateConfirmDialog({ open, title, description, consequence, confirmLabel, onCancel, onConfirm }: Props) {
  const { text } = useUiText();
  const titleId = useId();
  const descriptionId = useId();
  const cancelButton = useRef<HTMLButtonElement>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    cancelButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onCancelRef.current(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [open]);

  if (!open) return null;
  return createPortal(<div className="confirmation-overlay" role="presentation" onPointerDown={event => {
    if (event.target === event.currentTarget) onCancel();
  }}>
    <section className="confirmation-dialog regeneration-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId}>
      <header className="confirmation-dialog-header">
        <span className="confirmation-dialog-icon" aria-hidden="true">!</span>
        <div><p className="eyebrow">{text("请确认操作", "Confirm action")}</p><h2 id={titleId}>{title}</h2></div>
        <button className="dialog-close" type="button" aria-label={text("关闭", "Close")} onClick={onCancel}>×</button>
      </header>
      <div className="confirmation-dialog-body" id={descriptionId}>
        <p>{description}</p>
        <p className="message message-warning">{consequence}</p>
      </div>
      <footer className="confirmation-dialog-actions">
        <button ref={cancelButton} className="button button-secondary" type="button" onClick={onCancel}>{text("保留当前内容", "Keep current content")}</button>
        <button className="button button-primary" type="button" onClick={onConfirm}>{confirmLabel}</button>
      </footer>
    </section>
  </div>, document.body);
}
