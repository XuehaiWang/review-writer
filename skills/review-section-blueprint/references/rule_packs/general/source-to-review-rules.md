# Source-To-Review Rules

These rules govern how one source paper is transformed into compact review prose. They are writing and evidence-handling rules only; they must not import facts, wording, citation numbers, metrics, or content from any prior review project.

## Contamination Boundary

- Use these rules as editorial behavior, not as a source of scientific/technical facts.
- Use the supplied paper text, abstract, figures, tables, notes, DOI record, or user-provided extraction as the only source for factual claims.
- Do not reuse prior review prose as a model sentence.
- Do not infer conditions, values, scope, or explanation from a style rule.
- If the input is itself a review article rather than a primary paper, use it for framing only; verify specific claims, results, and limitations against primary evidence before stating them as facts.

## Editorial Gates

Before returning a paragraph, apply these gates:

- Subject-matter competence: no wrong method type, object-of-study class, output class, or evidence level for the field.
- Claim traceability: each factual clause must be traceable to the supplied source material.
- Style fidelity: the paragraph should read as review synthesis, not as a paper-by-paper abstract, lab notebook entry, or promotional summary.
- Scope discipline: a scope or generality claim must name the classes that justify it.
- Explanation discipline: a proposed explanation must remain proposed unless the source reports direct supporting evidence.

## Source Selection Signals

For a single source, identify which role the paper plays in the review:

- Foundational method: first enabling report or clear demonstration of an approach or concept.
- Strategic extension: expands scope, class coverage, operating window, or application relative to prior work.
- Evidence anchor: provides controls, measurements, or other evidence that changes certainty about a claim.
- Boundary source: defines a failure class, limitation, incompatibility, or unresolved question.
- Comparison source: clarifies why one approach complements, improves on, or differs from another.
- Application source: demonstrates practical, downstream, or real-world utility.

The paragraph should make the source's role visible when it affects the review narrative.

## Source-To-Paragraph Mapping

Use this default paragraph architecture:

1. Open with the method identity, system identity, or the review-relevant problem the paper addresses.
2. State the source's main contribution in one sentence.
3. Compress the decisive evidence: core method/experiment, scope, key metric, and comparison baseline.
4. Qualify explanation, scope, and practicality using the evidence level actually available.
5. End with the source's role in the review: boundary, advance, evidence anchor, comparison point, or application value.

Avoid one sentence per paper section. The final paragraph should have an editorial takeaway, not just a list of findings.

## Logical Transition Selection

Before drafting, identify the logic that connects this source to the surrounding review. Choose the sentence pattern from the relationship, not from the paper's abstract order.

- **Gap to solution**: use when an earlier approach left some class or condition unaddressed.
  - Pattern: `Whereas [earlier approach] provided [covered class], [new approach] addressed [missing class] by [key change].`
- **Limitation to complement**: use when the later paper complements rather than replaces an earlier method. Name the changed axis and keep the old method's scope separate.
  - Pattern: `While [earlier method] remains the standard for [covered class], [new method] complements it by addressing [uncovered class or condition], at the cost of [tradeoff].`
- **Extension**: use when a paper broadens the class coverage of an established approach.
  - Pattern: `Building on [prior demonstration], the authors extended the approach to [new class], showing [key result].`
- **Contrast**: use when two papers take genuinely different approaches to a comparable problem.
  - Pattern: `In contrast to [approach A], [approach B] achieves [comparable goal] by [different mechanism/method], with [tradeoff or scope difference].`
- **Mechanistic/explanatory bridge**: use when a paper's main contribution is explaining why an earlier result occurs.
  - Pattern: `The origin of [earlier observed effect] was addressed by [method], which indicated that [explanation, marked by evidence strength].`

Adapt these patterns freely; they exist to keep transitions logic-driven rather than chronological, not to be filled in verbatim.
