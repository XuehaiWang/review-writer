import { useDraftScratch } from "./useDraftScratch";
import { useUiText } from "../../i18n/useUiText";
import { CandidateComparison, type DialogueCandidate } from "./ParagraphDialogue";

export function CandidateBrowser({ candidates, disabled, decide, checked, toggle, stateKey }: {
  stateKey?: string;
  candidates: DialogueCandidate[]; disabled?: boolean;
  decide: (id: string, action: "accept" | "reject") => void;
  checked?: string[]; toggle?: (candidate: DialogueCandidate, selected: boolean) => void;
}) {
  const { text } = useUiText();
  const [filter, setFilter] = useDraftScratch(stateKey ? stateKey + ":filter" : undefined, candidates.some(c => c.status === "pending") ? "pending" : "all");
  const [selected, setSelected] = useDraftScratch(stateKey ? stateKey + ":selected" : undefined, "");
  const filtered = candidates.filter(c => filter === "all" || c.status === filter);
  const items = filter === "pending" ? [...new Map(filtered.map(c => [c.paragraph_key, c])).values()] : filtered;
  const index = Math.max(0, items.findIndex(c => c.candidate_id === selected));
  const current = items[index];
  return <section className="candidate-browser">
    <label>{text("候选状态", "Candidate status")} <select value={filter} onChange={e => setFilter(e.target.value)}>
      <option value="pending">{text("最新待确认", "Latest pending")}</option>
      <option value="accepted">{text("已保存", "Saved")}</option>
      <option value="rejected">{text("已放弃", "Discarded")}</option>
      <option value="all">{text("全部及历史", "All and history")}</option>
    </select> · {items.length}</label>
    <div className="draft-review-split"><nav className="draft-item-list" aria-label={text("候选列表", "Candidates")}>
      {items.map((c, i) => <button type="button" className={c === current ? "selected" : ""} aria-pressed={c === current}
        key={c.candidate_id} onClick={() => setSelected(c.candidate_id)}><strong>{c.paragraph_id}</strong><span>{i + 1} · {c.reply}</span></button>)}
    </nav><div className="draft-review-detail">
      {current ? <><div className="draft-local-toolbar"><strong>{current.paragraph_id} · {index + 1}/{items.length}</strong>
        <button className="button button-secondary" disabled={!index} onClick={() => setSelected(items[index - 1].candidate_id)}>{text("上一项", "Previous")}</button>
        <button className="button button-secondary" disabled={index === items.length - 1} onClick={() => setSelected(items[index + 1].candidate_id)}>{text("下一项", "Next")}</button>
        {toggle && current.status === "pending" ? <label><input type="checkbox" checked={checked?.includes(current.candidate_id) || false}
          onChange={e => toggle(current, e.target.checked)} />{text("选中", "Select")}</label> : null}</div>
        <CandidateComparison candidate={current} disabled={disabled} decide={decide} /></> : <p role="status">{text("此状态下没有候选，可切换查看历史。", "No matching candidates. Select another status to view history.")}</p>}
    </div></div>
  </section>;
}
