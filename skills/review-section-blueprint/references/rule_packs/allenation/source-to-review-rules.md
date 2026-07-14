# Source-To-Review Rules

These rules migrate portable writing guidance from the older OpenClaw review-writing skills. They govern how one source paper is transformed into compact review prose. They are writing and evidence-handling rules only; they must not import historical paper facts, old wording, old citation numbers, catalyst examples, yields, conditions, substrate lists, DOIs, or completed-review content.

## Contamination Boundary

- Use previous rules as editorial behavior, not scientific evidence.
- Use the supplied paper text, abstract, figures, tables, notes, DOI record, or user-provided extraction as the only source for factual claims.
- Do not reuse old review prose as a model sentence.
- Do not infer reaction conditions, selectivity values, substrate scope, or mechanism from a style rule.
- If the input is a review article rather than a primary paper, use it for framing only; verify reaction conditions, yields, selectivity, substrate failures, and mechanistic proof against primary evidence before stating them as facts.

## Editorial Gates

Before returning a paragraph, apply these gates:

- Chemistry competence: no wrong reaction class, catalyst family, activation mode, product class, substrate scope, selectivity type, or mechanism evidence level.
- Claim traceability: each factual clause must be traceable to the supplied source material.
- Style fidelity: the paragraph should read as review synthesis, not as a paper-by-paper abstract, lab notebook entry, or promotional summary.
- Scope discipline: a scope or generality claim must name the substrate or system classes that justify it.
- Mechanism discipline: a proposed mechanism must remain proposed unless the source reports direct experimental, computational, or literature-data support.

## Source Selection Signals

For a single source, identify which role the paper plays in the review:

- Foundational method: first enabling report or clear demonstration of a transformation or concept.
- Strategic extension: expands substrate class, product topology, stereocontrol, material class, operating window, or application.
- Mechanistic anchor: provides controls, kinetics, labeling, trapping, spectroscopy, computation, observed intermediates, or other evidence that changes certainty.
- Boundary source: defines a failure class, limitation, incompatibility, weak scope, or unresolved question.
- Comparison source: clarifies why one system complements, improves on, or differs from another system.
- Application source: demonstrates route-level, device-level, biological, analytical, or practical utility.

The paragraph should make the source role visible when it affects the review narrative.

## Source-To-Paragraph Mapping

Use this default paragraph architecture:

1. Open with method identity, system identity, or the review-relevant problem the paper addresses.
2. State the source's main contribution in one sentence.
3. Compress the decisive evidence: core experiment, substrate/system scope, key metric, and comparison baseline.
4. Qualify mechanism, scope, and practicality using the evidence level actually available.
5. End with the source's role in the review: boundary, advance, mechanism anchor, comparison point, or application value.

Avoid one sentence per paper section. The final paragraph should have an editorial takeaway, not just a list of findings.

## Rule Updates From Paragraph-Source Comparison

When the user asks to compare a written paragraph with the original source and write the learned method into rules, follow this fixed sequence:

1. Read the existing rule files before editing them.
2. Read the original source material, not only the user's polished paragraph.
3. Write an internal source summary before comparing prose. Capture the paper's central contribution, complete method or system, decisive evidence, scope classes, comparison baseline, mechanism evidence level, practical signals, and limitations.
4. Build an evidence map from the source to the paragraph. Mark which source facts were selected, compressed, reordered, grouped, downgraded, omitted, or kept as caveats.
5. Identify the rhetorical relationship between the source and paragraph, such as gap filling, extension, tradeoff, scope boundary, mechanism qualification, practical validation, operational practicalization, or correction of a prior method.
6. Write the new rule at the most general level that would help future papers with the same relationship.
7. Add chemistry-specific safeguards only after the transferable rule is stated.
8. Keep examples short and illustrative. They should teach the pattern, not become the pattern.
9. Do not encode unsupported facts from the current paper as permanent truth. Conditions, yields, substrate classes, and mechanisms remain source-specific unless restated as placeholders or examples.

Use this rule-update template:

```text
When a source paragraph has [logical relationship], summarize it by [writing move]. Preserve [evidence class or metric] and qualify [limitation or mechanism]. Example: [short chemistry example using placeholders or clearly example-only facts].
```

### Source Summary And Evidence Map For Rule Learning

Use this intermediate layer when learning rules from a source-to-paragraph comparison. It is for reasoning before rule writing, not for final review prose unless the user asks to see it.

- Source summary: state the paper's contribution in one or two sentences using source-supported facts only.
- Evidence inventory: list the decisive conditions, materials or substrate classes, scope trends, numerical metrics, comparison baselines, mechanistic evidence, practical signals, and explicit limitations.
- Paragraph mapping: identify which evidence was promoted to the opening claim, which details became a scope phrase, which numbers were retained or generalized, which caveats were preserved, and which routine details were omitted.
- Writing logic: name the transformation from source to review prose, such as `optimization to tradeoff`, `platform transfer`, `practical protocol with weaker boundary class`, or `mechanism proposal to evidence`.
- Rule extraction: convert the writing logic into a reusable rule only after the evidence map explains why the paragraph chose that order and level of detail.

Do not skip this layer for dense method papers, scope-heavy papers, or papers where practical value depends on exact conditions, scale, catalyst loading, substrate boundaries, or comparison to an earlier protocol.

## Logical Transition Selection

Before drafting, identify the logic that connects this source to the surrounding review. Choose the sentence pattern from the relationship, not from the paper's abstract order.

- **Gap to solution**: Use when an earlier strategy left a product, substrate, selectivity, or operational class inaccessible. Start with the missing class only if it explains the new method.
  - Pattern: `Whereas [earlier strategy] provided access to [covered class], [new design] addressed [missing class] by [key change].`
  - Example: `Whereas ligand control had enabled more substituted alcohol products, a removable steric-control group was introduced to access the less substituted class.`
- **Low-reactivity partner to new product topology**: Use when a new subsection opens because an analogous but less reactive reaction partner enables a higher substitution pattern, ring topology, oxidation level, or product family only after a different promoter/component match is found. Lead with the inaccessible product topology, then state the screening set and the uniquely productive system.
  - Pattern: `[Earlier partner class] enabled [lower product topology], but [less reactive partner class] had not delivered [higher product topology]. Screening [component families] identified [matched system], which gave [new product topology] and then supported [representative product-family extensions].`
  - Example: `A ketone ATA section should open with trisubstituted allenes as the missing topology, then name the metal/amine pair that overcame ketone low reactivity before listing bisallenes or protected allenols as scope extensions.`
- **Limitation to complement**: Use when the later paper complements rather than replaces an earlier method. Name the changed axis and keep the old method's scope separate.
  - Pattern: `A later variant complemented this method by extending [axis] to [new class], although [remaining caveat].`
  - Example: `A later variant extended the aldehyde scope to aryl partners, although isolated yields remained modest.`
- **Optimization to tradeoff**: Use when improved selectivity, yield, rate, or reproducibility comes with a cost. State both sides in the same sentence.
  - Pattern: `[Modified condition] improved [metric or class] relative to [baseline], but [cost or remaining weakness] persisted.`
  - Example: `Sequential heating improved reproducibility and maintained high enantioselectivity, but the products were still isolated in only moderate yields.`
- **Stoichiometric enabling system to catalytic limitation**: Use when screening identifies the first workable reagent or promoter combination for a desired platform, but the mechanistic design would ideally be catalytic and the actual method still requires high loading. State the workable scope before the catalytic caveat.
  - Pattern: `After screening, [promoter/component combination] enabled [desired transformation], giving [best-supported product classes] from [best substrate classes], while [weaker classes] gave lower outcomes. Although the proposed pathway would regenerate [promoter/catalyst], [high loading] was required, which the authors attributed to [source-supported deactivation or sequestration rationale].`
  - Example: `A metal/amine combination that finally enables a normal-aldehyde allene platform should be written as a promoted or mediated reaction if nearly stoichiometric metal is required, even when the proposed cycle contains metal regeneration.`
- **Scope expansion to boundary**: Use when a source reports a broad table plus explicit failures. Mention compatible classes first, then the excluded classes.
  - Pattern: `The method tolerated [compatible classes], but [excluded classes] remained outside the scope.`
  - Example: `Alkyl aldehydes were broadly compatible, whereas aryl and conjugated aldehydes were not productive under the same protocol.`
- **Mechanistic proposal to evidence**: Use when the paper explains why a method works. Match the verb to the evidence level and do not turn rationale into fact.
  - Pattern: `[Observation/control/computation] was consistent with [proposed role], rather than establishing [stronger claim].`
  - Example: `Control reactions were consistent with a steric-directing role for the protecting group, but the catalytic pathway was still proposed.`
- **Practical validation to representative example**: Use when a procedure paper, scale-up, or checked synthesis supports usability for one case. Do not broaden it into full scope.
  - Pattern: `[Procedure/scale-up] validated [representative product] on [scale] in [outcome], while the broader scope remained defined by [method paper/table].`
  - Example: `A checked multi-gram preparation validated one cycloalkyl example, whereas the substrate range still came from the original method study.`
- **Operational practicalization of an existing platform**: Use when a later paper keeps the same transformation but reduces catalyst or reagent loading, defines a larger-scale standard protocol, or removes an operational burden. Lead with the practical protocol, then split the scope into the product classes that perform well and the compatible-but-weaker class.
  - Pattern: `A [scale/practicality] protocol for [product class] used [lower-loading or simplified conditions], providing [main functionalized classes] while [compatible weaker class] remained lower-yielding than [earlier/higher-loading protocol].`
  - Example: `A catalytic terminal-allene protocol can be framed around reduced copper/amine/paraformaldehyde loading and 10 mmol operation, while simple hydrocarbon alkynes are mentioned only as compatible if they underperform the earlier stoichiometric system.`
- **Catalytic repair with component matching**: Use when a later method converts a stoichiometric, high-temperature, microwave, or otherwise inconvenient platform into a catalytic protocol by optimizing a matched reagent, amine, ligand, additive, or partner. State the practical/catalytic goal first, then the key component match, the contamination or side-product logic, the common-component compromise if used, and the remaining substrate boundary.
  - Pattern: `To develop a catalytic version of [earlier platform], [screening variable] was optimized under [catalyst/loading], identifying [matched component] for [representative substrate/partner]. A mismatch caused [side product or contamination pathway], while [general component] broadened practicality at [tradeoff]. Compared with [earlier platform], the method improved [scope/practical axis], but [remaining boundary] persisted.`
  - Example: `If a secondary amine both enables allene formation and can hydrolyze to an aldehyde-derived contaminant, the review paragraph should preserve the match/mismatch logic rather than treating amine screening as routine optimization.`
- **Useful target to practical preparation**: Use when the source paper focuses on preparing a known useful reagent, monomer, building block, intermediate, or analyte rather than discovering a new product class. Open with the target's identity and use value only as much as needed to motivate preparation, then state the old-route burden and the new practical procedure.
  - Pattern: `[Target] is a useful [role] because of [functional features or representative applications]. Earlier access relied on [old-route burden], whereas [new procedure] used [key conditions] to furnish [scale/yield].`
  - Example: `A small allene alcohol can be introduced as a useful building block, followed by the multistep or hazardous-reagent limitation of older syntheses and then the new gram-scale preparation conditions.`
- **Platform to extensions**: Use when several follow-up papers use the same activation mode or catalyst logic. Summarize the platform once, then group extensions by product family or problem solved.
  - Pattern: `After establishing [platform], subsequent studies applied it to [extension class A] and [extension class B], with [metric] reported separately for each class.`
  - Example: `After establishing an aldehyde-alkyne allene platform, later studies applied it to alcohol, amide, and malonate product families without merging their selectivity metrics.`
- **Cumulative limitation inventory to platform-level repair**: Use when two or more earlier strategies solve different parts of a problem but share several recurring limitations, and a later catalyst platform addresses those limitations together. State the shared limitations on explicit axes before introducing the new platform, then verify each claimed repair on the same axis.
  - Pattern: `[Earlier strategy A] enabled [class A], whereas [strategy B] enabled [class B], but both required [operation/loading limitation] and excluded [scope classes]. A later [platform-defining catalyst] allowed [direct starting class], expanded [scope axes], and reduced [loading/operation burden], although [class-specific condition exception] remained. Follow-up papers extended the platform to [product families], with each family retaining its own yield and selectivity metric.`
  - Example: `A catalytic allene platform may be framed as simultaneously removing protecting-group manipulations, admitting aryl aldehydes, reaching longer-chain products, and lowering metal usage, but a high catalyst loading required for the aryl subclass must remain attached to that subclass.`
- **Correction or reproducibility repair**: Use when a later paper revisits a published method because yields, selectivity, or timing were unreliable. Keep the review tone neutral.
  - Pattern: `[Later study] re-examined [reported protocol] and identified [condition or reliability issue], leading to [revised protocol/outcome].`
  - Example: `A subsequent study re-examined the reported two-stage operation and used a staged-heating protocol to maintain selectivity more reproducibly.`
- **Contested report to revised solution**: Use when one paper reports a method, a later source reports non-reproducibility or inconsistent characterization, and another method addresses the same synthetic target. Separate the reported claim, the challenge, and the corrective method.
  - Pattern: `[Group A] reported [method/outcome], but [Group B] could not reproduce [metric] or found [characterization discrepancy]. A later [revised system] addressed [target problem] by [key design], giving [source-supported outcome].`
  - Example: `A reported high-yield asymmetric allene synthesis can be followed by a neutral note that a later study could not reproduce the yield/ee and observed inconsistent optical rotations, before introducing the revised catalytic system.`
- **Component-role partitioning**: Use when control experiments assign different roles to catalysts, metals, ligands, additives, or steps. State the role split as experimental interpretation, not a generic mechanism.
  - Pattern: `Control experiments indicated that [component A] mainly promoted [step A], whereas [component B or A+B] was required for [step B].`
  - Example: `One metal may accelerate intermediate formation, while a second metal or bimetallic combination promotes rearrangement to product.`
- **Sequential platform assembly to direct one-pot repair**: Use when two individually successful steps are first combined because they share compatible conditions, but the combined operation still requires an intervention, and a later method removes or reduces that operational burden. Keep the assembled sequence, its required intervention, the remaining scope boundary, and the later direct protocol as separate claims.
  - Pattern: `Because [step A] and [step B] both worked under [shared condition], they were assembled into a [one-pot/telescoped/two- or three-step] route to [product]. The sequence still required [filtration/removal/addition/solvent switch] to avoid [lower yield/failed second step], and its scope remained limited to [productive classes]. A later direct protocol used [new cooperative system] to address [operation or scope limitation], with controls assigning [component roles].`
  - Example: `A ketone-alkyne-amine coupling that forms a propargylic amine and a metal-promoted amine-to-allene conversion may be combined only as a telescoped route if the first catalyst must be removed before rearrangement; a later cooperative one-pot system should then be written as the repair, not merged with the earlier sequence.`
- **Precedent mechanism to new platform design**: Use when a new subsection uses earlier papers as mechanistic or methodological precedents, then introduces a new one-pot design, substrate class, or reaction partner. Keep the reported precedent, the authors' design assumption, and the proposed mechanism as separate claim layers.
  - Pattern: `Building on [previous platform or result], [new target class] was pursued by combining [new upstream step] with the precedent [intermediate-to-product transformation]. Earlier [metal]-mediated studies had shown [validated step], while the direct [new one-pot reaction] required [new design element or hypothesis].`
  - Example: `Au- or Ag-mediated propargylic-amine-to-allene rearrangements can justify a proposed hydride-transfer/beta-elimination design, but they should not be written as prior examples of the full terminal alkyne/aldehyde/amine ATA reaction when those sources began from isolated propargylic amines.`
- **Subclass-specific controller switch**: Use when a platform works broadly but a substrate subclass requires a different ligand, chiral controller, additive, protecting group, solvent, or temperature for better performance. State the subclass first, then the switched element and the metric improved.
  - Pattern: `For [substrate/product subclass], replacing [original controller/condition] with [new controller/condition] improved [metric], while the underlying [platform/reaction class] remained the same.`
  - Example: `For a longer-chain product subclass, switching the chiral amine can be framed as a subclass-specific optimization rather than a new reaction class.`
- **Product utility after method scope**: Use when products are converted into several downstream functional groups or targets. Keep the direct method product separate from derivatized products.
  - Pattern: `The resulting [direct product class] could be converted into [downstream classes], supporting synthetic utility without expanding the direct reaction scope.`
  - Example: `Alcohol products that are oxidized, substituted, or converted into amides should still be described as products of the primary method only at the alcohol stage.`
  - Separate transformations demonstrated in the current paper from transformations mentioned only as known possibilities or supported through cited precedent. Use `was converted to` for demonstrated derivatives and `can serve as a precursor to` or `may be converted to` for literature-supported possibilities; do not present the latter as experiments from the current source.
- **Mechanistic evidence ladder for isotope-labeling paragraphs**: Use when a review paragraph combines intermediate experiments, isotope placement, a kinetic isotope effect, and a full stereochemical mechanism. Assign each claim only the certainty supplied by its evidence.
  - Isolation of an intermediate and its conversion to product support intermediate competence, but do not by themselves establish that it is the exclusive on-cycle species.
  - Positional isotope transfer establishes the origin and destination of the transferred atom. Without a crossover experiment or equivalent evidence, it does not alone establish that the transfer is intramolecular.
  - A primary `K_H/K_D` supports involvement of C-H/D cleavage in a kinetically important or turnover-limiting event. Unless detailed kinetics resolve the sequence, prefer `consistent with the hydride-transfer step contributing to rate limitation` over declaring a uniquely rate-determining elementary step.
  - Facial attack, coordinated transition structures, intermediate absolute configuration, syn/anti elimination geometry, and the cause of a controller-dependent ee difference remain proposed stereochemical models unless separately demonstrated.
  - Pattern: `[Intermediate experiment] supported [intermediate/pathway], while deuterium labeling established [atom transfer] and a KIE of [value] implicated [bond-cleavage event] in rate limitation. The ensuing [facial-attack/hydride-transfer/elimination] sequence and the explanation for [selectivity trend] were proposed to account for the observed stereochemistry.`
- **Section-opening baseline method**: Use at the start of a new review subsection when a racemic, parent, or operationally simple method provides the baseline for later variants. Open with the transformation and product class, then give the key modified conditions and comparison baseline.
  - Pattern: `[Subsection] begins with [baseline transformation], where [modified conditions] convert [starting class] to [product class] more effectively than [earlier protocol]. [Scope or functional-group tolerance] defines why the method serves as a platform for later developments.`
  - Example: `A racemic allene subsection can start from a modified terminal-alkyne-to-allene protocol before moving to enantioselective variants.`
- **Platform transfer to derivative substrates**: Use when a method developed for a parent substrate class is applied to a related functionalized or heteroatom-containing substrate class. State the inherited platform, the new substrate class, the new product class, and the new evidence of performance.
  - Pattern: `[Established platform] was applied to [derived substrate class], furnishing [derived product class] in [outcome]. Compared with [external or earlier protocol], [same-axis advantage] was observed.`
  - Example: `A terminal-alkyne allenation protocol may be transferred to N-propargyl amides, but the new paragraph should name the amide-derived allene products rather than repeating only the parent allene scope.`
- **Complex substrate-class extension**: Use when an existing method is adapted to densely functionalized, biomolecule-derived, oligomeric, polymeric, or otherwise complex substrates. Name the platform and complex substrate class, then compress the table into tolerated functional groups, representative scaffold families, and any high-density or multisite example.
  - Pattern: `[Platform] was extended to [complex substrate class] under [one or more condition sets], giving [product class] in [outcome range] while tolerating [functional-group classes]. The scope included [representative scaffold families] and [high-density/multisite example when supported].`
  - Example: `A terminal-alkyne allene platform can be summarized as an extension to carbohydrate-derived substrates by naming glycoside families and a per-allenylated sugar example, without listing every protected sugar entry.`
- **Intermediate platform to application cluster**: Use when one paper prepares a useful intermediate class and later papers use it in several transformations. Keep preparation and application claims separated by citation and wording.
  - Pattern: `[Intermediate class] prepared by [method] served as precursors to [application/product family] in later studies. The preparation paper supports [intermediate synthesis], while the later papers support [downstream transformations].`
  - Example: `Allene amides can be described as precursors to oxazoline derivatives, but oxazoline formation should not be treated as part of the allene-synthesis scope unless it occurs in the same transformation.`

Do not stack every transition marker in one paragraph. Use the dominant relationship, then fold secondary relationships into short caveats.


## Introduction Funnel And Historical-Anchor Pattern

Use this pattern when a method-focused review opens by moving from the broad value of a molecular class to the foundational reaction that created the field-specific problem.

- Build the opening as a narrowing funnel rather than a bibliography: `demonstrated chemical capability -> structure-derived or stereochemical value -> occurrence/application relevance -> demand for synthesis -> demand for selective or asymmetric synthesis -> foundational method -> historical bottleneck`.
- Assign each citation cluster a distinct clause-level role. Broad reactivity reviews may support transformation classes; chirality-transfer reviews may support conversion of axial information into central chirality; synthesis reviews may support field-wide accessibility or historical coverage; asymmetric-synthesis reviews may support the selective-synthesis challenge. Do not let one review cluster support all of these claims indiscriminately.
- Use secondary reviews for field framing and literature-state claims, but use the primary discovery papers for `first`, `seminal`, `original`, exact reagent combinations, representative yields, substrate effects, and failed examples.
- When a numbered reference contains several papers, separate their functions before writing. An early application paper may establish chronological origin, a short communication may define the general transformation, and a later full study may establish optimization, mechanism, substrate effects, or limitations. Do not attribute the full evidence bundle to the earliest paper alone.
- A historical anchor should preserve the transformation identity: starting-material classes, carbon source or coupling partner, amine/ligand role, metal status, and product topology. Avoid reducing it to an author-and-year sentence that does not explain why the reaction matters to the review.
- Preserve `catalyzed`, `promoted`, and `mediated` according to the reported loading and source wording. A substoichiometric but high metal loading should not be silently rewritten as low-loading catalysis.
- After the foundational result, select one positive structural trend and one explicit boundary only when they explain the later development of the field. These examples should diagnose the bottleneck rather than reproduce the original scope table.
- Convert a trend inferred from a small historical substrate set into bounded language such as `in the original examples`, `within the reported series`, or `under the original conditions`. Do not turn a few substrate effects into a universal reactivity law.
- When a multifunctional substrate gives competing partial and multiple transformations, retain the separate products and yields if the distribution is the evidence. Do not compress distinct outcomes into a single `low yield` statement.
- A long-standing field limitation, such as confinement to one coupling-partner class for many years, is a literature-state claim. Support it with synthesis reviews or a documented literature survey rather than assuming the original discovery paper proves the absence of later work.
- Attach citations as locally as possible when a sentence contains several evidence roles. A single citation block at the end of a dense paragraph should not obscure which sources support reactivity, chirality transfer, general synthesis, asymmetric synthesis, historical priority, or scope limitations.
- End the introduction by converting the historical bottleneck into the review's roadmap: name the variables or product classes that later sections will address, rather than repeating a generic statement that more research was needed.

Generic template:

```text
[Target molecular class] participates in [major reaction families], while [structure-derived feature] enables [distinct stereochemical or synthetic function]. Together with its occurrence in [application context], these properties motivate efficient, and where relevant stereoselective, synthesis. The field-specific route began with [primary historical transformation], which converted [inputs] into [product topology] under [defining components]. In the original examples, [bounded positive trend], whereas [bounded weak or failed class] exposed the method's limitation. The subsequent persistence of [literature-supported bottleneck] defines the developments reviewed below.
```

## Method-Improvement Summary Pattern

Use this pattern when the paper's main contribution is an improved condition, reagent combination, catalyst system, or operational variant.

- Open with the modified or newly optimized method, not with broad field background.
- Preserve the complete new condition or system when it is the contribution, such as catalyst/salt plus amine, ligand, additive, solvent, promoter, or activator.
- If the source emphasizes preparative scale, operational simplicity, or gram-scale utility, place the scale or practicality signal early rather than burying it in the scope sentence.
- When the main advance is catalytic or operational practicalization of an existing protocol, preserve the changed loading or burden-reduction axis, such as lower catalyst loading, lower reagent equivalents, no inert atmosphere, scale, solvent, and temperature, before listing scope.
- Compress the scope into chemically meaningful product or substrate classes rather than listing individual examples.
- For practicalized follow-up protocols, order scope by review relevance: primary functionalized product classes first, protected/unprotected variants next, and compatible but lower-performing parent or nonfunctionalized substrates last.
- Bind yield or performance language to the relevant scope classes. Prefer exact ranges when provided; otherwise use restrained source-supported phrases such as `moderate to good yields`.
- State the baseline method or traditional condition before claiming improvement.
- Limit the claimed advantage to the class where it is actually apparent. Do not turn a class-specific benefit into a general superiority claim.
- Separate compatible-but-weaker substrate classes from the main scope. Write both compatibility and weakness when the source shows that a substrate class works but is lower-yielding than the benchmark.
- If the summary is about condition improvement and scope, do not add a mechanism unless the paper supplies direct or proposed mechanistic evidence.

Generic template:

```text
With the modified [new system], [substrate/product classes] could be prepared in [source-supported outcome range]. Compared with [baseline condition], [new system] shows [specific advantage], especially for [substrate/product class where the advantage is demonstrated].
```

Practical-scale template:

```text
An efficient and operationally simple [scale] protocol for [product class] was demonstrated using [complete key conditions]. The method accommodated [functionalized substrate/product classes], including [protected/unprotected or derivative categories]. [Boundary substrate class] was also compatible, although [specific weakness] relative to [baseline or modified protocol].
```

Catalytic-practicalization template:

```text
A [scale] catalytic variant of [established transformation] used [lower-loading catalyst/reagent system and operating conditions] to prepare [main functionalized product classes]. It performed well for [best-supported subclasses], while [parent/nonfunctionalized or weaker class] remained compatible but [lower-yielding or otherwise weaker] relative to [earlier higher-loading or modified protocol].
```

## Catalytic Component-Matching Summary Pattern

Use this pattern when the advance is a catalytic or more practical version of an existing reaction, but success depends on matching a reagent component to a substrate or byproduct pathway.

- Start with the practical target: catalytic loading, lower temperature, broader aldehyde class, larger scale, or avoidance of a stoichiometric promoter.
- Preserve the catalyst loading and the component class that was screened when those define the advance.
- Do not summarize all screening entries. Report the winning component and any chemically meaningful structural trend, such as normal dialkylamine versus branched amine or cyclic amine.
- If a component mismatch creates a second product, contamination, or crossover product, explain the source-supported pathway in one sentence and keep it next to the matched-component result.
- When a common component is introduced for convenience, state the tradeoff: it may avoid contamination or simplify use while giving slightly lower yields.
- Compare with the earlier method on the same axis, such as catalytic loading versus stoichiometric promoter, aliphatic aldehyde generality, functionalized substrate tolerance, or milder operation.
- End with the residual boundary if it shapes future development. Do not expand a missing class into a mechanistic failure unless the source provides that evidence.

Generic template:

```text
To make [earlier transformation] more practical or catalytic, [catalyst/loading] was combined with a screened [component class]. [Matched component] gave the best result for [representative substrate/partner], whereas mismatched components led to [contamination or side product] through [source-supported pathway]. [Common component] could be used to avoid this issue at the cost of [yield or scope tradeoff]. Relative to [earlier method], the protocol improved [specific axis], although [remaining substrate or operational boundary] persisted.
```

## Stoichiometric Enabling Platform With Catalytic-Limitation Pattern

Use this pattern when a paper establishes a desired one-pot or platform reaction only after screening a high-loading metal salt, promoter, amine, additive, or reagent, while the proposed mechanism suggests that the key component should be regenerated.

- Open with the enabling combination only after naming the transformation it makes possible.
- Preserve the component identity and loading when the high loading is part of the limitation.
- Compress scope by productive class, functionalized subclass, and weaker boundary class, rather than listing the whole substrate table.
- Keep functionalized products, such as alcohols, amides, sulfamides, or heteroatom-containing substrates, as scope extensions if they are not the main parent-substrate series.
- Do not call the method catalytic if the source uses substoichiometric or near-stoichiometric loading that is required for useful yield. Use `mediated`, `promoted`, or `enabled by` unless the source demonstrates turnover at catalytic loading.
- If the mechanism predicts regeneration of the metal or promoter but the experiment requires high loading, present this as a mechanistic/practical tension.
- Attribute deactivation, sequestration, water effects, product/byproduct coordination, or other catalyst-loading explanations to the authors unless directly proven by controls.
- Separate evidence from rationale: control reactions, isolated/intercepted intermediates, or additive effects support the mechanism at a different level than proposed catalyst deactivation.

Generic template:

```text
After screening, [promoter/amine or component combination] enabled the one-pot [transformation] of [starting classes] to [product class]. [Best substrate classes] furnished [main products] in [outcome], including [functionalized subclasses] when supported, whereas [weaker substrate classes] gave lower yields or required harsher conditions. Although the proposed mechanism would regenerate [component], the reaction still required [high loading], which was attributed to [source-supported deactivation or sequestration rationale].
```

## Practical Preparation Of Useful Targets

Use this pattern when the paragraph is about making a useful reagent, building block, monomer, intermediate, analyte, or standard compound, and several references play different roles.

- Separate citation roles: application papers support usefulness, older synthesis papers support the access problem, and the new method or checked procedure supports the preparation conditions and yield.
- Open with the target identity only when the target's role explains why preparation matters.
- Keep the usefulness statement compact. Group applications by product family or reaction type instead of listing every downstream paper.
- State old-route drawbacks in concrete source-supported terms: multistep operation, difficult separation, explosive intermediate, hazardous reductant, high loading, pressure equipment, poor yield, or scale limitation.
- If the new paper reports multiple practical procedures, keep them as separate options when the operating regimes differ, such as reflux versus autoclave, stoichiometric versus catalytic metal, or normal pressure versus high pressure.
- Preserve the discriminating condition for each option: scale, starting material amount, solvent, catalyst loading, reagent equivalents, temperature or reflux/autoclave condition, and yield range.
- If a later checked procedure or follow-up source revises the yield range or verifies operational details, use it to calibrate the practical claim while keeping the original method paper as the source for method development.
- Do not let application references support preparation yield, and do not let a practical preparation source support broad downstream synthetic utility beyond the examples it cites or demonstrates.

Generic template:

```text
[Target] is a useful [building-block/reagent/intermediate] for [grouped application class]. Earlier syntheses required [old-route burden], motivating [new practical preparation]. After [optimization or workup change], [procedure A] furnished [target] on [scale] in [yield] under [conditions], while [procedure B] used [distinct operating regime] to give [yield or advantage].
```

## Sequential Platform Assembly To Direct One-Pot Repair Pattern

Use this pattern when a review paragraph summarizes a sequence of papers in which an upstream intermediate-forming reaction and a downstream product-forming reaction are first joined because their conditions are compatible, followed by a later method that makes the process more direct or broader in scope.

- Assign citation roles before writing: the first source supports the upstream step, the second source supports the downstream step and the telescoped or filtered sequence, and the later source supports the direct protocol, controls, and scope extension.
- State the shared operational feature that justified assembly, such as the same solvent or temperature window, only if the sources support it.
- Preserve the operation label accurately. Use `two-step`, `three-step`, `telescoped`, or `via filtration` when an intermediate workup, catalyst removal, filtration, solvent switch, or sequential addition is required. A `one-pot two-step` process may use one vessel while still requiring sequential reagent addition, completion of the first stage, and a temperature change; do not collapse it into a single-step or all-components-at-once reaction.
- Keep the intervention as evidence, not trivia. A required filtration, catalyst removal, or reagent exchange explains why the assembled route was not yet the final practical solution.
- Separate operational repair from scope repair. A later promoter or cooperative component may remove filtration or catalyst isolation without expanding the substrate classes. In that case, frame the advance as simplified operation and state that the earlier substrate boundary remained.
- Place the earlier scope boundary immediately before or after the later repair when the paragraph is explaining progress. This makes clear whether the later paper solved the operation problem, the scope problem, or both.
- When the later direct system has control experiments, summarize the component roles by step and evidence level: which component forms the intermediate, which component converts the intermediate to product, and whether the later promoter is merely compatible with residual upstream catalyst or is shown to act cooperatively.
- Do not turn source language such as `harmony`, `synergy`, or `cooperation` into a mechanistic claim unless the controls establish a specific interaction. Compatibility demonstrated by unchanged yield or selectivity is sufficient for an operational claim.
- If an explicit failure class is stated only in a later review while the primary paper reports only the positive scope, either verify the negative experiment in the Supporting Information or write the narrower claim that the reported scope remained confined to the demonstrated substrate class.
- Do not let the later direct system retroactively change the mechanism, scope, or catalyst identity of the earlier assembled sequence.

Generic template:

```text
Because [upstream reaction] and [downstream conversion] both operated under [shared condition], [authors/source] combined them into a [telescoped/two- or three-step] route from [starting classes] to [product class]. The process still required [intervention] after [step] to avoid [failed or lower-yielding outcome], and productive examples were mainly [earlier scope classes]. A later direct protocol used [new component set] to expand the scope to [new classes]; control experiments indicated that [component A/additive] promoted [intermediate formation], whereas [component B] was more effective for [intermediate-to-product conversion].
```

## Multi-Paper Platform-Extension Summary Pattern

Use this pattern when one review paragraph compresses a main platform paper together with follow-up extension papers.

- Open with the platform-defining discovery, catalyst, or transformation only if it explains why later papers belong together.
- First summarize the main platform: starting-material classes, chiral controller or catalyst system, product class, and decisive outcome metrics.
- State generality by chemically parallel axes, such as substrate position, substitution level, aldehyde class, product family, protecting-group status, or functionalized versus nonfunctionalized products.
- Keep scale-up and class-specific condition exceptions near the core method instead of burying them in an extension list.
- Add follow-up papers as platform extensions grouped by product family, not as separate mini-abstracts.
- Use expansion language such as `was further applied to`, `also enabled`, or `was extended to`, while keeping each extension tied to its own product class and evidence level.
- Preserve distinct performance metrics by product family. Do not collapse ee, de, dr, yield, conversion, or scale into a single generic selectivity or efficiency claim.
- Preserve class-specific operational exceptions when the source supports them, such as a higher temperature or higher catalyst loading required for one aldehyde class.
- Treat narrative origin claims, such as accidental discovery, as optional historical context. In neutral review prose, prefer `was identified as` or `was found to be` unless the author-centered narrative is requested.
- Replace evaluative praise such as `impressive` with evidence-based language: broad scope, demonstrated scale, excellent ee/de, tolerated unprotected functionality, or enabled previously difficult product classes.

Generic template:

```text
[Catalyst or platform] was identified as an effective system for [problem/transformation]. From [starting material classes] and [chiral controller or catalyst system], the method delivered [main product class] in [yield range] with [selectivity range], tolerating [scope axes]. The reaction was scalable to [scale], while [substrate class] required [condition exception]. The same platform was further applied to [extension product families], including [functionalized classes] and [nonfunctionalized classes], with [product-specific metric] retained where reported.
```

## Complex Substrate Extension Summary Pattern

Use this pattern when a source paper extends an established method to a complex substrate family and the review paragraph needs to show breadth without becoming a substrate table.

- Open with the platform-extension relationship rather than generic importance of the complex class unless that context is essential.
- Preserve the number and identity of distinct condition sets when the source frames them as the advance, but omit exact loadings and times when the paragraph's job is scope placement rather than procedure comparison.
- Name the direct product class created by the method, then group scope by scaffold family, substitution position, linker length, heteroatom linkage, protection state, or functional group class.
- Use one aggregate yield range only when it spans the same product family and metric. If subclasses have materially different ranges, keep the ranges separate.
- Include a high-density, multicomponent, oligomeric, polymeric, or multisite example when it shows a meaningful scope endpoint. State it as an example, not as evidence that all related substrates will fully convert.
- Keep tolerance language tied to observed functional groups, such as ether, ester, amide, malonate, halide, hydroxyl, or protected alcohol. Do not upgrade tolerated substituents into mechanistic claims.
- Do not infer biological activity, stereodictation, selectivity transfer, or downstream utility from the complex scaffold unless the source directly tests it.

Generic template:

```text
[Established platform] was extended to [complex substrate family], using [condition-set count or key condition identity] to afford [direct product class] in [yield range]. The scope tolerated [functional-group classes] and covered [representative scaffold families], including [high-density/multisite example] when reported.
```

## Section-Opening Mechanistic Bridge Pattern

Use this pattern when a review opens a new subsection by transferring logic from the previous subsection and from mechanistic precedent papers into a new reaction design.

- Start from the subsection-level move: name the previous platform or solved problem, then state the new product class, substrate class, or reaction partner.
- Treat earlier papers as precedents only for the step they actually demonstrated. If they begin from an isolated intermediate, do not use them as evidence for a new one-pot sequence that generates that intermediate in situ.
- Separate the three claim layers explicitly: prior literature result, authors' design assumption, and proposed catalytic or redox mechanism.
- When the new paragraph contains a mechanism, identify which steps are inherited from precedent and which are added by the new design.
- Use cautious verbs for inferred mechanisms: `was proposed`, `was assumed`, `was consistent with`, or `was designed to`, unless the source gives direct experimental proof.
- Keep the mechanism as a bridge only if it explains why the new subsection follows from the earlier one. Do not expand it into a full mechanistic review unless the paragraph's purpose is mechanism.
- If the transformation is described as redox, state what is oxidized and reduced only when the source or review paragraph identifies those species.

Generic template:

```text
Encouraged by [previous platform], [new product class] was targeted through a one-pot design involving [new upstream intermediate-forming step] followed by [precedent intermediate-to-product step]. Earlier [metal]-mediated studies had established [precedent transformation], so the new work proposed that [component combination] could generate [intermediate] in situ and then promote [key rearrangement/elimination] to give [product class].
```

## Strategy-Bifurcation Section-Opening Pattern

Use this pattern when a review opens a new section by converting a source paper's conceptual scheme into two or more alternative strategies for reaching the same target.

- Use the established parent transformation only as the causal bridge into the new section; do not repeat its full scope or conditions unless they explain why the new strategies became possible.
- Define each strategy by the location and source of control, such as chiral ligand control versus a chiral reagent, substrate, auxiliary, catalyst, or intermediate. Do not rely on short labels without stating what changes chemically.
- Preserve the distinct input sets for the alternative strategies, then identify the common intermediate, elementary stage, or design bottleneck on which they converge.
- Organize the opening as a funnel: `parent platform -> alternative strategies -> defining components -> shared intermediate or bottleneck`.
- When the paragraph is a conceptual section opener, defer catalyst loadings, individual yields, scope tables, operational details, and substrate-specific limitations to the subsequent method paragraphs unless one of those details is necessary to distinguish the strategies.
- If the downstream conversion can erode, invert, or racemize stereochemical information, do not imply that a highly selective intermediate automatically guarantees a highly selective product. State that intermediate selectivity and stereochemical fidelity of the downstream step are separate requirements.
- A review may normalize a source paper's longer descriptive phrase into a concise strategy label only when the mapping is chemically exact; retain the original distinction when the shorter label would broaden or change the mechanism of control.
- In the first method paragraph after a strategy-bifurcation opener, a racemic or nonselective sequence may be retained as the operational baseline when its step order, catalyst removal, solvent continuity, or promoter switch explains how the asymmetric variant was assembled and where selectivity may be lost.
- Assign citation roles by reaction stage. Earlier asymmetric papers may support only enantioselective formation of an upstream intermediate, whereas the current source supports its incorporation into the full sequence, the final-product outcome, and any downstream selectivity bottleneck. Do not let an upstream precedent citation support the final transformation.
- When a current paper selects a ligand or catalyst because prior papers gave high selectivity for analogous intermediates, compress the precedent to the details that explain the design choice: controller identity, relevant substrate class, selectivity range, and a decisive limitation such as long reaction time or narrow scope. Omit unrelated ligand synthesis, protecting-group chemistry, or applications.
- If the current intermediate was not isolated or its selectivity was not measured, a comparison with structurally similar literature intermediates is a benchmark, not direct proof of the current intermediate's ee. Describe downstream racemization, erosion, or poor chirality transfer as inferred or attributed unless the current sequence directly measures both intermediate and product.
- Keep performance axes separate. A sequence may proceed smoothly or give an acceptable yield while failing stereochemically; pair the yield or conversion with the low selectivity in the same sentence so that reactivity language does not imply overall success.
- When the first concrete example of the alternative strategy is inspired by papers that start from an isolated chiral intermediate, describe those papers as a `downstream half-reaction` or `partial-sequence precedent`. If a review calls them an `incomplete synthesis`, make clear that the incompleteness concerns coverage of the new multicomponent sequence, not poor yield, conversion, or selectivity in the precedent paper itself.
- For a controller-screening proof of concept, organize the paragraph as `structural or mechanistic precedent -> controller family screened -> best matched substrate/controller example -> immediate scope boundary`. This order shows both why the controller was selected and why a single high-selectivity result did not yet establish a general platform.
- Do not let a headline ee from one optimized substrate carry the whole method claim. Preserve the isolated yield, conversion, recovered starting material, or condition burden when those data materially qualify the example, and place explicit weak or failed substrate classes in the same or next sentence.
- When several negative examples differ in outcome, compress them only to the strongest common conclusion. `Low yield with ee not determined` and `trace product` may jointly support `poor results` or `extremely limited scope`, but they do not support the stronger claim that every class was unreactive or racemized.

Generic template:

```text
Building on [parent transformation], two complementary approaches were identified for [new target]: [strategy A], in which [source/location of control and defining inputs], and [strategy B], in which [alternative source/location of control and defining inputs]. Both converge on [common intermediate or stage], making [shared selectivity, reactivity, or stereochemical-fidelity requirement] the central design problem.
```

## Low-Reactivity Partner Section-Opening Pattern

Use this pattern when the review begins a new subsection by moving from a successful reaction partner class to a less reactive partner class that changes the attainable product topology.

- Open with the chemical barrier and the missing product class, not with broad importance of the products.
- Name the previous successful partner class only to define the contrast.
- Preserve the screening logic when the solution comes from an unusual or non-obvious component, such as a different metal family, salt, amine, ligand, additive, or promoter.
- If only one component in a screened family gives the desired product while the others mainly give intermediate, trace product, or no product, state that selectivity at the family level.
- Keep the optimized system visible when it defines the advance: promoter identity, loading, matched amine or ligand, solvent, and temperature.
- Compress the scope by product topology and product family, such as higher-substituted products, bis-products, protected alcohols, chiral products, or scale-up examples.
- Treat product-family utility as a scope-placement device unless the paper demonstrates downstream transformations. Do not let `important in synthesis` replace direct product evidence.
- Preserve stoichiometric or high-loading status. Do not call the method catalytic if the source uses a promoter or mediator in near-stoichiometric amount.

Generic template:

```text
Because [less reactive partner class] had prevented access to [new product topology] under [earlier platform], [component-family screening] was used to identify [matched system]. Under [key conditions], [starting classes] afforded [product topology] in [outcome], while related screened components were ineffective or diverted to [intermediate/byproduct] when relevant. The scope included [representative product families], establishing the new subsection platform while leaving [remaining limitation] as a caveat if supported.
```

## Gap-Filling Directed-Group Summary Pattern

Use this pattern when a paper solves a missing substrate or product class by adding a removable directing, protecting, or steric-control group.

- Open with the unmet class only when it frames the advance, such as a primary product class not reached by an existing ligand or catalyst strategy.
- Convert author narrative language such as `inspired by`, `we envisioned`, and `to our delight` into a neutral design statement: `[group] was used as a removable [protecting/directing/steric-control] group`.
- Preserve the dual role of a group only when the source supports both roles, for example protection plus stereochemical or reactivity control. Separate direct observations, such as improved yield, selectivity, or time-stability relative to the unprotected substrate, from the authors' proposed steric, coordination, or racemization-suppression explanation.
- If the design is inspired by a previously successful bulky substituent, describe the analogy as a hypothesis rather than a proven structure-effect law. When screening shows an optimum steric window, summarize the balance: a removable group may outperform the unprotected substrate, while still bulkier groups can reduce yield or reactivity.
- State the enabled product class, coupling partners, promoter or catalyst, deprotection step, yield range, and ee/de range in one compact evidence sentence. If the reported yield is measured after deprotection, label it as an overall or two-step isolated yield rather than the yield of the protected intermediate.
- If the strategy also controls multiple stereochemical permutations, state the control variables and the product set, not every scheme entry. Map each control variable to the stereochemical element it determines when the source supports that assignment, and keep `ee`, `de`, and `dr` distinct.
- Keep exclusions close to the scope claim. Do not call a method general if aromatic, alpha,beta-unsaturated, or other named substrate classes are not applicable.
- Do not let a successful design example become a mechanism unless the source provides mechanistic evidence. Use `was proposed to`, `was interpreted as`, or `is consistent with` for steric-directing explanations.
- Treat background citations on product utility as motivation only. They may justify why the missing product class matters, but they do not support the current reaction conditions, scope, selectivity, or claim that the class was previously inaccessible.

Generic template:

```text
To address the absence of [missing product/substrate class] under [earlier strategy], [removable group] was used as a [protecting/directing] element in [reaction class]. Under [promoter/catalyst] conditions, [starting-material classes] with [compatible partner classes] afforded [product class] after [deprotection or downstream step] in [yield range] and [ee/de range]. The same control logic enabled [stereochemical/product extension] by varying [configuration/control variables], but [excluded substrate classes] remained outside the protocol.
```

## Adjacent Follow-Up Method Pattern

Use this pattern when a paragraph follows directly after a related method and describes a later variant that fixes or complements one limitation.

- Use a temporal bridge such as `later`, `subsequently`, or `in a follow-up study` only when the chronology matters to the review narrative.
- Make the changed axis explicit: starting-material class, product family, aldehyde class, protection state, operating sequence, or catalyst/promoter system.
- Do not let the prior paragraph's scope bleed into the follow-up method. Re-state the new substrate and product classes if they differ.
- If the follow-up complements a limitation rather than broadly improves the platform, write it as `extended access to [class]` or `addressed [limitation]`, not as a generally superior method.
- If the changed axis is purely operational, lead with the removed burden, such as filtration or catalyst removal, and then state whether yield/selectivity were retained and whether the substrate boundary remained unchanged.
- Balance high selectivity with yield or operational caveats in the same sentence when both are central to the source.
- Preserve non-identical operation labels such as `one-pot`, `two-stage`, `sequential heating`, or `two-step` only when the source uses or supports that distinction. Treat `one-pot two-step` as sequential processing in one vessel, not as a single mechanistic event.

Generic template:

```text
Later, [modified operation or related platform] extended [reaction class] from [previous class] to [new substrate/product class]. Using [key conditions], [starting-material classes] with [partner classes] afforded [product class] with [selectivity range], although [yield, scope, reproducibility, or operational caveat] limited the advance.
```

## Historical, Review-Cluster, And Stereochemical Learning Patterns

Use these patterns when a review paragraph derives its logic from historical milestones, secondary review clusters, or a progression from chirality transfer to catalytic asymmetric induction.

### Concept-Milestone-Gap Framing

Use this pattern when an introduction links a fundamental stereochemical concept, a historical prediction, an early experimental demonstration, and the unmet need addressed by the review.

- Select the minimum conceptual distinction needed to define the field.
- Compress historical theory into the prediction relevant to the review topic.
- Use the primary experiment as the milestone anchor.
- End with the source-supported limitation that motivates later developments.
- Keep missing selectivity, scope, or priority evidence visible.

Generic template:

```text
[Concept] was proposed in [period] from [basis] and later demonstrated through [primary experiment]. Although subsequent work established [field-level advance], [source-supported limitation] continued to motivate [modern direction].
```

### Review-Cluster To Bounded Field-State Pattern

Use this pattern when a paragraph cites multiple reviews to summarize development status.

- Group reviews by the aspect they actually cover.
- Identify the shared progress signal and the shared residual limitation.
- Convert the cluster into a bounded literature-state statement, not a universal experimental claim.
- Do not let every source in the cluster support every clause.

Generic template:

```text
Reviews covering [subfields and period] collectively indicate progress in [bounded development], particularly through [innovation channels]. However, they also identify [shared remaining limitation], so the advance should be framed as [qualified field-state conclusion].
```

### Split Review Clusters From Primary Milestones

Use this pattern when one paragraph cites a large review cluster for field development and a primary paper for a specific historical experiment.

- Group secondary reviews into field-level framing.
- Attribute exact transformation, conditions, measured results, and mechanistic observations only to the primary source.
- Label limitations derived from reviews as review synthesis unless primary evidence is supplied.

Generic template:

```text
A body of reviews documents [field-level development], whereas the foundational report specifically showed that [primary experiment and direct observation]. The broader claim remains [review synthesis], while the experimental statement may be treated as [reported data].
```

### Platform Progress Versus Target-Specific Progress

Use this pattern when development of a broad platform enables, but does not itself constitute, progress in a narrower asymmetric or stereochemical target.

- Identify the enabling development, such as improved racemic synthesis or precursor access.
- Identify the separate asymmetric-control development, such as a chiral ligand, chiral catalyst, chiral reagent, or resolution strategy.
- Explain the connection without merging evidence classes.

Generic template:

```text
Expansion of [enabling synthetic platform] increased access to [intermediate or racemic product class], while independent advances in [asymmetric catalyst, ligand, or resolution strategy] enabled control of [stereochemical feature]. These developments are related but should be evaluated separately because [remaining boundary].
```

### Selective Review Scope Criteria

Use this pattern when review authors declare that the review is critical, selective, privileged, representative, or non-exhaustive.

- State inclusion and exclusion dimensions explicitly.
- Separate editorial preference from evidence-based method assessment.
- Define evaluative terms through observable criteria such as scope, selectivity, mechanistic distinctiveness, practicality, reproducibility, or historical influence.

Generic template:

```text
This review provides a selective assessment of [topic], prioritizing studies that demonstrate [criterion 1], [criterion 2], or [criterion 3], rather than attempting exhaustive coverage of [excluded or lower-priority material].
```

### Foundation To Platform To Stereochemical Boundary

Use this pattern when a foundational transformation later develops into a broad synthetic platform and an early stereochemical experiment reveals both opportunity and uncertainty.

- Identify the foundational reaction with its original substrate and reagent classes.
- Attribute broader platform scope to later literature.
- Select the earliest stereochemical observation as the strategic extension.
- End with the missing measurement, weak selectivity, or adverse result that prevents a stronger conclusion.

Generic template:

```text
The initial study established [foundational transformation] for [original substrate class]. Later work broadened the method to [expanded platform classes]. Application to [enantioenriched precursor] indicated [stereochemical opportunity], although [missing metric or adverse result] prevented quantitative assessment of [transfer efficiency or generality].
```

### Reaction-Class Naming Versus Mechanism Proof

Use this pattern when review prose uses a familiar reaction-class term but the primary literature contains evolving or conflicting mechanisms.

- Use the reaction-class label for net connectivity only.
- State mechanistic interpretations separately and, when useful, chronologically.
- Calibrate certainty according to the evidence behind each proposal.

Generic template:

```text
The transformation is conventionally classified as [reaction class] on the basis of [net connectivity change]. The original study proposed [initial pathway], whereas later [experimental evidence] supported [revised pathway]; accordingly, the reaction-class label should not be presented as direct proof of a single elementary mechanism.
```

### Qualitative Transfer Before Quantitative Efficiency

Use this pattern when optically active product supports stereochemical transfer but the measurements needed for transfer efficiency are absent.

- Report the directly observed optical or configurational evidence.
- State that the result indicates qualitative transfer.
- Withhold `efficient`, `complete`, `high-transfer`, or numerical transfer language unless paired stereochemical composition is available.

Generic template:

```text
Conversion of [enantioenriched precursor] gave an optically active [product], and [measurement or assignment] was consistent with [qualitative transfer relationship]. Because [product ee/er] was not determined, the transfer efficiency could not be quantified.
```

### Conditional Mechanistic Platform Partition

Use this pattern when a reaction family contains more than one source-supported pathway selected by substrate, reagent, solvent, additive, halide, ligand, or temperature.

- Define each branch by the substrate and conditions for which it is supported.
- State the diagnostic evidence for each branch.
- Preserve pathway switching, mixed-pathway interpretations, and condition-dependent syn/anti or retention/inversion outcomes.

Generic template:

```text
For [substrate and condition class], [diagnostic evidence] supported [pathway A], whereas [different evidence] indicated [pathway B] for [second class]. Because [variable] could switch the observed [syn/anti or stereochemical outcome], the pathways should be treated as conditional rather than mutually exclusive universal categories.
```

### Optimization To Racemization To Mechanism

Use this pattern when apparent selectivity trends cannot be interpreted until product stability under the reaction conditions is evaluated.

- Report the optimization trend first.
- Introduce independent product-racemization or erosion evidence.
- Reinterpret final selectivity as the net result of formation and post-formation erosion.
- Discuss elementary stereochemical pathways only after that qualification.

Generic template:

```text
Although [optimization variable] improved the isolated product's [selectivity metric], separate exposure experiments showed that [product class] underwent [racemization or isomerization] under [conditions]. The observed selectivity therefore represents the balance between [stereoselective formation] and [post-formation erosion], rather than direct measurement of [elementary-step selectivity].
```

### Preserve Exceptions To Compact Mechanism Summaries

Use this pattern when a review compresses a large mechanistic literature into a small number of pathways.

- State the dominant pathway.
- Immediately add the principal source-supported exception.
- Identify the variable responsible for the reversal.
- Avoid a categorical closing sentence contradicted by cited sources.

Generic template:

```text
The dominant outcome under [conditions A] was [stereochemical relationship], consistent with [pathway]. Under [conditions B], however, changing [key variable] reversed the outcome to [opposite relationship], showing that the mechanistic model is condition-dependent.
```

### Reagent Redesign As Limitation Repair

Use this pattern when a later method replaces a reactive reagent class to address racemization, incompatibility, poor regioselectivity, or selectivity problems in an established platform.

- Name the earlier failure mode and comparison baseline.
- Identify the changed reagent class.
- State which performance dimensions improved.
- Preserve co-optimized catalyst, ligand, base, solvent, temperature, and residual failures.

Generic template:

```text
Replacing [earlier reagent class] with [milder reagent class] reduced [documented limitation] and enabled [reaction outcome] under [catalyst system]. The new protocol delivered [metrics], although [nonreactive class or operational dependence] remained.
```

### Parallel Reports As Complementary Validation

Use this pattern when nearly simultaneous or mutually aware papers independently establish overlapping methods with different catalyst systems or scope emphases.

- Establish the chronological overlap only if it matters.
- Identify the shared platform.
- Separate each study's distinctive catalyst, coupling-partner scope, optimization insight, mechanism evidence, and limitation.
- Present the second paper as validation plus extension rather than repetition.

Generic template:

```text
Nearly simultaneous studies established [shared platform]. One report emphasized [scope or catalyst branch], whereas the independent study used [different catalyst or conditions] to extend the method to [complementary scope] and clarify [optimization or mechanistic point].
```

### High Transfer With Measurable Erosion

Use this pattern when precursor and product remain highly enantioenriched but their ee or er values are not identical.

- Report paired values or a bounded difference.
- Describe transfer as high or excellent only when proportionate.
- Separate intrinsic stereochemical leakage from product racemization when controls permit.

Generic template:

```text
Conversion of [precursor ee/er] material furnished [product ee/er] product with predominantly [stereochemical relationship]. Because [stability control] showed [no/limited] racemization, the small erosion was assigned to [minor competing pathway or incomplete intrinsic transfer].
```

### Value To Gap To Weak Precedent

Use this pattern when a valuable target class lacks a reliable stereoselective preparation and the best historical precedent is chemically productive but stereochemically inadequate.

- Establish value with demonstrated downstream chemistry.
- Define the unresolved access criterion.
- Summarize the strongest precedent.
- Separate productive chemical formation from poor or uncertain stereochemical control.

Generic template:

```text
[Target class] is useful in [documented application classes], but direct access to [defined stereochemical form] remained limited. An early [reaction class] furnished [target topology] in [chemical-yield description]; however, [stereochemical metric or measurement limitation] showed that the method did not provide a reliable general solution.
```

### Separate Value Source From Preparation Source

Use this pattern when one citation supports importance or downstream utility and another supports a specific preparation attempt.

- Assign value statements to application or reactivity sources.
- Assign reaction conditions and outcomes to primary preparation papers.
- Do not let the value source act as experimental evidence for preparation.

Generic template:

```text
[Value source] establishes the utility of [target class] through [applications], whereas [primary source] reports its preparation by [method] with [measured outcome]. These roles should remain distinct in the paragraph and evidence map.
```

### Functional-Group Gap Explanation Calibration

Use this pattern when a review attributes a methodological limitation to a functional group whose mechanistic role is only partially established.

- State the observed influence of the functional group.
- Separate acid-base state, coordination, neighboring-group effects, and product-stability effects.
- Present the broader causal explanation as tentative unless directly tested.

Generic template:

```text
The [functional group] affected [observed reaction feature], possibly through [proposed role]. However, because [alternative process or limited evidence] also influenced the outcome, its responsibility for the broader methodological limitation remains tentative.
```

### Reagent Substitution Plus Solvent Activation

Use this pattern when a milder nucleophile avoids a problem of an earlier reagent class but requires solvent activation to become synthetically useful.

- State the earlier reagent-related limitation.
- Introduce the alternative nucleophile and its composition.
- Present comparative solvent data as the enabling factor.
- Validate the redesigned system with a stereochemical test when available.

Generic template:

```text
[Alternative reagent class] was introduced to avoid [earlier reagent limitation], but useful reactivity emerged only in [solvent class]. Relative to [comparison solvents], [optimal solvent] increased [yield or conversion], and an enantioenriched [substrate] furnished [product] with [bounded stereochemical outcome].
```

### Benefit-Reversal Negative Control

Use this pattern when a method's principal advantage disappears under a closely related reagent, salt, or additive condition.

- State the beneficial condition.
- Pair it with the adverse control.
- Define the operational boundary explicitly.

Generic template:

```text
Under [controlled reagent composition], [desired stereochemical outcome] was retained. Addition or presence of [specific salt/additive] instead caused [racemization or selectivity loss], showing that the method's advantage depends on [operational requirement].
```

### Solvent-Screening Mechanism Calibration

Use this pattern when solvent screening reveals a strong performance trend and the authors propose an activation explanation.

- Report comparative performance first.
- Identify the solvent descriptor correlated with the trend.
- Label the molecular explanation as inference unless the activated species is directly characterized.

Generic template:

```text
[High-donor or otherwise activating solvents] promoted [reaction] more effectively than [comparison solvents]. The authors correlated this trend with [solvent parameter] and interpreted [spectroscopic shift or model evidence] as evidence for [proposed reagent activation], although the reactive solvated species was not directly observed.
```

### Chirality Transfer To Catalytic Induction

Use this pattern when a new study removes the requirement for an enantioenriched substrate and creates the stereochemical element through a chiral catalyst.

- Identify the stereochemical input required by the previous platform.
- Define the achiral or prochiral replacement substrate.
- State the catalyst-controlled stereochemical output.
- Explain the precise limitation overcome.

Generic template:

```text
Whereas earlier [reaction class] transferred chirality from [enantioenriched precursor], [new method] used prochiral [substrate class] with [chiral catalyst system] to generate [stereochemical element] catalytically, furnishing [product class] with [bounded performance metrics].
```

### Functionalized Chiral Intermediate Divergence

Use this pattern when the primary asymmetric product retains a functional handle that supports multiple stereospecific downstream pathways.

- Present the first product as a functionalized chiral intermediate.
- Separate each downstream pathway by reagent and conditions.
- Report stereochemical retention for each branch.
- Preserve competing pathways that emerge under altered conditions.

Generic template:

```text
The enantioenriched [functionalized intermediate] served as a branch point: under [conditions A] it furnished [product class A], whereas [conditions B] produced [product class B]. Paired [ee/er] values showed [degree of retention], although [alternative condition] redirected the reaction to [competing topology].
```

### Breakthrough Through Specific Gap

Use this pattern when review prose characterizes a method as a major breakthrough.

- Replace generic praise with the specific methodological transition.
- Name the previous limitation and newly enabled capability.
- Preserve remaining operational and scope restrictions.

Generic template:

```text
The study addressed the prior dependence on [limiting feature] by enabling [new capability] from [new substrate class] under [catalytic system]. Its significance lies in [specific repaired gap], although [remaining boundary] prevents treatment as a universal solution.
```

### Selectivity-Loss Reaction-Stage Partition

Use this pattern when several methods lose stereochemical fidelity but evidence places the loss at different points in the reaction sequence.

- Group examples first by observed metric loss.
- Repartition them by pre-intermediate ionization, intermediate equilibration, product racemization, or measurement uncertainty.
- Compare mechanisms only after this partition.

Generic template:

```text
Although [methods] all showed erosion of [ee/er], the evidence places the loss at different stages: [method class A] was affected by [process before or during intermediate formation], whereas [method class B] underwent [intermediate or product-stage process]. The mechanisms should therefore be compared as distinct explanations for a common experimental symptom.
```

### Scope Boundary To Mechanistic Hypothesis

Use this pattern when a structural scope boundary motivates a mechanistic explanation.

- Identify the structural change correlated with selectivity collapse.
- Preserve the comparison to the higher-performing substrate class.
- State the proposed kinetic or intermediate-based explanation with calibrated certainty.

Generic template:

```text
Whereas [substrate class A] retained [stereochemical metric], introduction of [structural feature] in [class B] caused [measured loss and competing outcome]. The authors proposed that [effect on intermediate lifetime or reaction rate] allowed [isomerization/racemization process], but the intermediate process was not directly observed.
```

### Catalyst-Class Erosion Contrast

Use this pattern when a review compares stereochemical erosion in two catalyst classes.

- State the shared outcome.
- Identify the dominant source-supported erosion process for each catalyst class.
- Cite each side separately.
- Preserve mixed or secondary pathways.

Generic template:

```text
Both [catalyst class A] and [catalyst class B] can reduce [stereochemical purity], but the dominant evidence differs: [A] shows [product-stage process], whereas [B] is consistent with [intermediate-stage process]. This contrast is mechanistic rather than merely descriptive.
```

### Topology Failure To Ligand Repair

Use this pattern when a method performs well for one substrate topology but fails after introduction of an additional substituent or structural feature.

- Establish the successful baseline.
- Identify the structural boundary using paired metrics.
- Introduce the later ligand or catalyst redesign as a targeted repair.
- Preserve residual limitations after repair.

Generic template:

```text
[Baseline topology] underwent [reaction] with [degree of stereochemical retention], whereas [structural modification] caused [measured erosion]. Redesign of [ligand/catalyst environment] restored [bounded performance], although success remained dependent on [conditions or substrate limits].
```

### Chiral Versus Achiral Ligand Repair

Use this pattern when two papers address the same selectivity problem using a chiral ligand and an achiral ligand, respectively.

- Present the chiral-ligand system as possible double stereodifferentiation with the enantioenriched substrate.
- Present the achiral-ligand system as preservation of substrate-derived stereochemical information.
- Compare evidence without treating the systems as mechanistically identical.

Generic template:

```text
A chiral [ligand class] improved the reaction through a matched combination with [enantioenriched substrate], whereas achiral [ligand class] preserved substrate-derived chirality by [proposed stabilization or kinetic effect]. These strategies repair the same limitation through distinct stereochemical logic.
```

### Stabilize And Trap Labile Intermediate

Use this pattern when selectivity improves through both ligand-controlled intermediate stabilization and acceleration of the next irreversible step.

- Identify the configurationally labile intermediate.
- Separate the proposed stabilizing role of the ligand from the rate effect of the trapping reagent, pressure, or concentration.
- Support each component with its corresponding optimization trend.

Generic template:

```text
[Ligand] was proposed to reduce equilibration of [configurationally labile intermediate], while increasing [trapping reagent concentration or pressure] accelerated conversion to [product-forming intermediate]. The combined stabilization-and-trapping model is supported by [optimization trends] but remains mechanistic inference unless the intermediate is directly observed.
```

### Racemic Substrate To Catalyst-Controlled Axial Chirality

Use this pattern when a new catalytic method replaces the need for an enantioenriched precursor by converting a racemic substrate into an enantioenriched axially chiral product.

- Contrast the stereochemical input of earlier and later methods.
- Name the chiral catalyst or ligand as the source of asymmetric control.
- Preserve evidence for kinetic resolution, dynamic equilibration, or mixed behavior.

Generic template:

```text
Whereas the earlier strategy required enantioenriched [precursor class], catalytic asymmetric [reaction] converted racemic [substrate class] into [axially chiral product] using [chiral catalyst system]. Switching [catalyst stereochemistry] reversed the product configuration, while [recovered-substrate or intermediate evidence] indicated that [kinetic-resolution or dynamic-process contribution] also operated.
```

### Ligand Optimization To Bounded Design Rationale

Use this pattern when systematic ligand modification identifies a high-performing derivative but does not directly reveal the molecular origin of selectivity.

- Report structural modifications and performance trend.
- State the authors' steric or electronic interpretation as a proposal.
- Preserve nonmonotonic or contradictory entries.

Generic template:

```text
Modification of [parent ligand scaffold] at [positions] changed [rate/yield/ee], and [optimized substituent pattern] gave the best practical balance. The authors attributed the improvement to [steric/electronic environment], although the nonmonotonic ligand series supports this as a design hypothesis rather than a proved transition-state model.
```

### Scope Success To Evidence-Located Boundary

Use this pattern when a review pairs a successful scope summary with a categorical failed-substrate statement.

- Summarize demonstrated successful classes.
- Identify the failed class and where its evidence appears.
- Downgrade the boundary if the supporting experiment is unavailable.

Generic template:

```text
The demonstrated scope covered [successful substrate classes] with [bounded outcomes]. [Failed class] was reported as incompatible in [source location]; because [source location] is unavailable, that boundary remains `missing_source` rather than verified experimental evidence.
```

### Failed Transfer To Asymmetric Trapping

Use this pattern when loss of precursor stereochemical information is converted from a problem into an opportunity for enantioselective trapping of an equilibrating intermediate.

- Begin with the failed chirality-transfer control.
- Identify the equilibrating racemic intermediate.
- Introduce the chiral trapping reagent.
- Present the result as a strategy change rather than an optimization of the original transfer process.

Generic template:

```text
An attempted chirality-transfer reaction gave [racemic/eroded product], indicating loss of stereochemical information at [intermediate stage]. The authors therefore replaced transfer with enantioselective trapping of the equilibrating [intermediate class] using [chiral reagent], furnishing [product] with [selectivity].
```

### Reagent Breadth Without Substrate Breadth

Use this pattern when several asymmetric reagents are compared using only one substrate.

- Summarize the reagent-structure trend.
- State the substrate count explicitly.
- Limit the conclusion to feasibility and reagent selection.

Generic template:

```text
Screening of [chiral reagent classes] with a single [substrate framework] identified [best reagent class] as the most selective. Because substrate variation was not examined, the study establishes the viability of [strategy] but not its general scope.
```

### Mixed Catalytic And Stoichiometric Control

Use this pattern when a catalytic metal sequence contains a stoichiometric chiral reagent that determines enantioselectivity.

- Separate the catalyst's bond-forming or intermediate-generating role from the chiral reagent's stereodetermining role.
- State stoichiometry explicitly.
- Classify the method according to the stereochemistry-controlling component.

Generic template:

```text
[Metal catalyst] generates [reactive intermediate], but asymmetric induction occurs during reaction with [stoichiometric chiral reagent]. The sequence is therefore metal-catalyzed yet relies on stoichiometric chiral control.
```

### Unactivated Precursor To In Situ Activated Pathway

Use this pattern when review prose emphasizes direct use of an unactivated precursor but the primary method creates the reactive leaving group or intermediate in situ.

- Preserve the starting functional group as the strategic advantage.
- State the in situ activation sequence.
- Distinguish operational directness from mechanistic directness.

Generic template:

```text
The method begins directly from [unactivated precursor class], avoiding isolation of a preactivated derivative. Under [activation system], however, the precursor is converted in situ into [reactive intermediate], which subsequently undergoes [product-forming process].
```

### Paired Metrics To Calibrated Retention

Use this pattern when a review compresses paired precursor and product stereochemical measurements into `retention`, `complete transfer`, or `no loss`.

- Report both values and analytical uncertainty.
- State whether they are identical, overlap within error, or show measurable erosion.
- Identify the measurement method.

Generic template:

```text
[Precursor] of [value +/- error] gave [product] of [value +/- error], indicating [complete within error / high but incomplete / measurable erosion] stereochemical transfer as determined by [method].
```

### Intermediate Evidence To Mechanism-Ceiling Mapping

Use this pattern when a review converts partial intermediate evidence into a detailed rearrangement mechanism.

- Identify which analogue was isolated.
- Identify which operative intermediate was only transiently detected or proposed.
- Limit the mechanistic claim to the strongest direct evidence.

Generic template:

```text
Isolation of [model intermediate] supports the feasibility of [intermediate class], while [operative intermediate] was assigned from [transient observation or analogy]. The subsequent [specific rearrangement] should therefore be described as proposed unless supported by [pathway-specific evidence].
```

### Substrate-Defined Complementary Protocols

Use this pattern when optimization produces two successful protocols that apply to different electronic or structural substrate classes.

- Divide scope by the feature governing condition choice.
- Present each condition with its preferred substrate class.
- Preserve the mismatched-condition failure.

Generic template:

```text
Two complementary protocols were identified: [condition A] was optimal for [substrate class A], whereas [condition B] was required for [class B]. Application of [A] to [B] caused [failure], while [B] applied to [A] gave [different failure or reduced metric].
```

### Performance To Hidden-Condition Boundary

Use this pattern when high yield and stereochemical fidelity depend on avoiding a specific reagent excess, solvent, or side-reaction condition.

- State the high-performing result.
- Immediately preserve the condition that destroys the advantage.
- Distinguish formation selectivity from post-formation erosion.

Generic template:

```text
Under [controlled stoichiometry and solvent], [product class] was obtained with [performance]. Excess [salt/reagent] caused [racemization], while [alternative reagent or solvent] led to [side reaction or ee loss], showing that the apparent robustness depends on a narrow operational boundary.
```

### Stereochemical Outcome To Calibrated Mechanistic Sequence

Use this pattern when stereochemical retention is used to infer a sequence of syn or anti elementary steps.

- Report the observed precursor/product stereochemical relationship.
- State the proposed sequence as consistent with the result.
- Identify which intermediates or elementary steps were not directly observed.

Generic template:

```text
The conversion from [precursor stereochemistry] to [product stereochemistry] is consistent with [addition geometry] followed by [elimination geometry]. Because [intermediate or elementary step] was not directly observed, the sequence should remain a mechanistic model rather than a proved explanation.
```

### Foundation To Optimization To Partner Expansion

Use this pattern when a reaction platform develops through a foundational report, a condition-improvement paper, and later expansion to new partner classes.

- State the foundational reaction and original boundary.
- Identify the exact variable repaired by optimization.
- Present each later partner class with its own catalyst or mediator and product topology.
- End with a calibrated unifying model.

Generic template:

```text
The original [reaction platform] coupled [substrate] with [partner A] under [component set A] to give [product A], but remained limited in [defined performance or scope axis]. Replacement with [component set B] improved [specific metric]. The platform was subsequently extended to [partner B] and [partner C] using [systems C and D], furnishing [products B and C].
```

### Component-Role Partition Across Platform Branches

Use this pattern when different metals or additives control different stages of a multicomponent transformation.

- Separate intermediate-forming and intermediate-consuming roles.
- Use comparison experiments to assign operational roles.
- Avoid a single undifferentiated catalyst label.

Generic template:

```text
[Component A] efficiently promoted formation of [intermediate] but did not convert it to [product], whereas [component B] enabled [intermediate-to-product step] and, under the complete conditions, supported both stages. The components therefore should be assigned stage-specific roles rather than grouped as equivalent mediators.
```

### Intermediate Evidence To Bounded Unified Mechanism

Use this pattern when several platform papers converge on a shared intermediate but differ in the strength of evidence for subsequent steps.

- Identify the common intermediate and evidence level in each source.
- Separate intermediate formation from the proposed rearrangement.
- Present the full sequence as a framework rather than a proved universal mechanism.

Generic template:

```text
Evidence from [sources] supports [intermediate class] as a chemically competent species in [platform]. Conversion to [product] required [metal/additive], and the authors proposed [hydrogen-transfer/elimination sequence]. Because [pathway-specific evidence] was absent, the sequence remains a mechanistic framework rather than a universally demonstrated pathway.
```

### Precursor Route To Axis Transfer

Use this pattern when one source provides a stereoselective precursor route and later sources convert those precursors into a different stereochemical topology.

- Divide the narrative into precursor construction and downstream conversion.
- Identify the stereochemical source in each stage.
- Verify which later substrates were directly demonstrated upstream and which were prepared analogously.

Generic template:

```text
[Precursor source] prepared [centrally chiral precursor class] through [reaction] using [catalyst] and [chiral component]. [Downstream source] then converted these or related precursors into [axially chiral product class] by [method]. The route should be described as directly linked only for [verified overlapping substrates], with other examples identified as analogous extensions.
```

### Metal Replacement As Selectivity Repair With Operational Cost

Use this pattern when a second metal system is introduced to prevent racemization or broaden high-fidelity scope.

- Define the defect of the first metal.
- State the performance repaired by the second metal.
- Preserve the new metal loading, conversion, light, time, or equipment burden.

Generic template:

```text
Because [metal system A] gave [substrate-dependent ee erosion or racemization], [metal system B] was selected to retain [product chirality]. The replacement improved [scope or paired stereochemical metric] but required [high loading, incomplete conversion, darkness, or microwave irradiation].
```

### Conversion-Normalized Yield Tradeoff

Use this pattern when a study reports high isolated yield based on consumed starting material but leaves substantial substrate unreacted.

- Report conversion before yield.
- State the yield denominator.
- Distinguish chemoselectivity among converted material from total process productivity.

Generic template:

```text
The reaction reached [conversion], and [product] was isolated in [yield] based on converted substrate. Thus, product formation was efficient among consumed material, but the overall yield relative to charged substrate was limited by incomplete conversion.
```

### Fidelity-Qualified Scope

Use this pattern when a review says a method is confined to one product class, but the source forms additional classes at lower selectivity.

- Separate chemical accessibility from acceptable stereochemical performance.
- Replace categorical confinement with a fidelity-qualified boundary.

Generic template:

```text
Although [method] formed [broader product classes], high stereochemical fidelity was concentrated in [defined subset]. The method is therefore best described as most effective for [subset], rather than chemically confined to it.
```

### Racemic Platform To Multiple Asymmetric-Control Sites

Use this pattern when a racemic multicomponent reaction can be rendered enantioselective by controlling different mechanistic stages.

- Identify the racemic platform and common intermediate.
- Name each possible stereocontrol site.
- Separate stoichiometric chiral-reagent control from catalytic ligand or catalyst control.

Generic template:

```text
The racemic [platform] proceeds through [intermediate], allowing asymmetric control through [strategy A] or [strategy B]. The present development uses [selected strategy] to furnish [product class] through [chirality-transfer or enantioinduction step].
```

### Proof Of Concept To Matching Boundary

Use this pattern when an asymmetric proof of concept succeeds for one specialized substrate but fails for simpler or structurally different analogues.

- Report the successful example quantitatively.
- Contrast it with measured failures using the actual failure metric.
- Describe the result as a substrate-matching boundary, not general scope.

Generic template:

```text
The initial asymmetric reaction succeeded for [specialized substrate], giving [product] in [yield] and [selectivity]. A simpler [substrate class] gave [measured failure], establishing a narrow matched combination rather than general scope.
```

### Follow-Up Cluster As Diagnosis And Separate Repairs

Use this pattern when a grouped citation contains several follow-up papers that diagnose one initial limitation through different solutions.

- Use the initial paper for the proof of concept.
- Use later papers to confirm boundaries or provide repairs.
- Assign each follow-up its distinct operational or mechanistic repair role.

Generic template:

```text
The initial study established [proof of concept] but exposed [boundary]. Follow-up studies addressed it separately through [repair A], [repair B], and [repair C].
```

### Practical Descriptor Conflict Across A Development Series

Use this pattern when an evolving publication series uses conflicting descriptors for cost, convenience, or practicality.

- Report objective operating facts first.
- Preserve conflicting qualitative descriptors as source-specific wording.

Generic template:

```text
[Reagent] is [availability status] but required at [loading/equivalents]. One source identified [practical limitation], whereas another used [different descriptor]; practicality should be evaluated from objective operating facts.
```

### Original Result To Reproducibility Dispute

Use this pattern when one group reports a high-performing method and another group later reports that the result could not be reproduced.

- State the original result and later replication result separately.
- Identify variables tested by the replication source.
- Leave the disagreement unresolved unless independently adjudicated.

Generic template:

```text
[Original group] reported [method] with [yield/selectivity]. [Later group] reported that it could not reproduce these metrics after testing [variables]. The discrepancy remains unresolved.
```

### Optical-Rotation Discrepancy Calibration

Use this pattern when two studies report comparable ee but different specific rotations for nominally the same product.

- Preserve measurement conditions and ee method.
- State that calibrated rotations differed.
- Avoid choosing one value as correct without evidence.

Generic template:

```text
For nominally the same [product] at comparable [ee], the studies reported different specific rotations under [conditions]. The source did not establish the cause.
```

### Monometallic Limitation To Bimetallic Repair

Use this pattern when a second metal is introduced because the original metal performs one stage inefficiently.

- Establish the monometallic baseline.
- Show the matched improvement.
- Partition component roles using intermediate and single-metal controls.

Generic template:

```text
[Metal A] alone gave [baseline outcome], while addition of [metal B] improved [metric]. Controls indicate that [component] promotes [stage A] and [component] is more active in [stage B], with the combination performing best.
```

### Improved Method With Retained Moderate-Yield Ceiling

Use this pattern when a redesign improves yield relative to a weak baseline but most scope entries remain moderate.

- Report matched improvement and absolute scope-wide yield distribution separately.
- Keep stereoselectivity and yield as separate axes.

Generic template:

```text
Relative to [baseline], [redesigned system] increased [matched metric]. Across the scope, however, absolute yields remained [range], while stereoselectivity was [range].
```

### Functional Group To Protecting-Group Repair

Use this pattern when a free functional group suppresses the desired transformation and a removable protecting group restores reactivity and selectivity.

- State the unprotected failure.
- Report the protecting-group screen.
- Identify the practical optimum and preserve the deprotection step.

Generic template:

```text
The free [functional group] caused [failure mode]. Screening removable [protecting groups] identified [optimized group] as the best balance of [reactivity, selectivity, stability, removability], followed by [deprotection].
```

### Nonmonotonic Protecting-Group Rationale

Use this pattern when intermediate-sized protecting groups outperform both the unprotected substrate and bulkier alternatives.

- Preserve lower- and higher-bulk comparisons.
- Describe the optimum as a balance rather than a maximum-bulk solution.

Generic template:

```text
[Groups A and B] improved performance relative to the free substrate, whereas bulkier [groups C and D] reduced it. The selected group reflects an optimum, not a monotonic bulk effect.
```

### Independent Center And Axis Control

Use this pattern when one chiral input fixes a central stereocenter and another determines an allene axis.

- Assign each stereochemical element to its controlling input.
- Report ee, de, dr, and stereoisomer combinations separately.

Generic template:

```text
The configuration of [chiral substrate] was retained at [central center], while [chiral reagent/catalyst] controlled [axial element]. Combining both configurations furnished [stereoisomer set] with separate [ee/de/dr] values.
```

### Product-Class Utility To Method Relevance

Use this pattern when a method paragraph opens by explaining why a functionalized product class is valuable.

- Use the background source only to establish downstream utility.
- Transition to the primary methodological gap.

Generic template:

```text
Because [product class] contains [functional handle] useful for [downstream transformations], direct asymmetric access to [topology] is valuable. [Primary method] addresses this access problem through [strategy].
```

### Catalytic Intermediate Formation To Axial Relay

Use this pattern when a chiral catalyst establishes central chirality in an intermediate and a second metal or step converts that intermediate into an axially chiral product.

- Divide asymmetric intermediate formation from downstream chirality transfer.
- Report transfer fidelity only from paired evidence.

Generic template:

```text
[Metal A/chiral ligand] generated [central-chiral intermediate]. After [operational transition], [metal B] converted it into [axially chiral product]. Transfer fidelity is [measured/inferred] from [metrics].
```

### Coordinating Functional Group As Two-Stage Repair

Use this pattern when introduction of a functional group correlates with improved selectivity in both intermediate formation and downstream conversion.

- State the low-selectivity baseline.
- Present the improved functionalized scope.
- Separate observed performance from the proposed coordination model.

Generic template:

```text
The nonfunctionalized [substrate] gave [baseline], whereas incorporation of [functional group] improved [metrics]. Coordination to [metal A] and [metal B] was proposed but not directly observed.
```

### Precedent To Substrate-Specific Catalyst Failure

Use this pattern when a catalyst succeeds for one substrate family but fails on a differently functionalized analogue.

- State the successful precedent and substrate class.
- Report the matched failure as a substrate-condition mismatch rather than universal inactivity.

Generic template:

```text
Although [metal system] converted [precedent substrate class], application to [new functionalized substrate] gave [failure] with [recovery]. The result indicates a substrate-specific mismatch.
```

### Ligand-Axis Substrate-Center Matrix

Use this pattern when the chiral substrate controls a retained center and the chiral ligand controls a newly generated allene axis.

- Assign center and axis to their respective inputs.
- Report the full substrate/ligand matrix with separate ee, de, and dr.

Generic template:

```text
[Substrate configuration] determined [central center], while [ligand configuration] selected [allene axis]. The full matrix provided [stereoisomer set] with separate [ee/de/dr].
```

### Filtration Bottleneck To One-Pot Goal

Use this pattern when an effective sequence requires catalyst removal or filtration and the review identifies one-pot integration as the next goal.

- State the operation that prevents one-pot execution.
- Explain why it is required.
- Keep integration prospective until demonstrated.

Generic template:

```text
The sequence required removal of [residual component] before [second stage] because its presence caused [problem]. A one-pot variant remained a future target.
```

### Parallel Addition Branches To One Platform Assessment

Use this pattern when two reagent classes undergo the same formal addition topology to give related but chemically distinct functionalized products.

- Introduce the shared transformation.
- Separate each reagent/product branch.
- Reunite them only for common strategic value and shared boundaries.

Generic template:

```text
[Catalyst]-catalyzed asymmetric [addition] of [reagent A] gave [allene class A], whereas [reagent B] gave [allene class B]. Both served as [shared downstream function], but branch-specific scope and performance differed.
```

### Best-Case Advance To Typical-Performance Limitation

Use this pattern when one or two optimized examples reach high selectivity but most entries remain moderate.

- State the maximum as a benchmark.
- Describe the typical distribution.
- Connect the high result to its operational cost or substrate restriction.

Generic template:

```text
Although optimization raised the model reaction to [maximum selectivity], most combinations remained within [typical range]. The maximum required [special conditions or substrate].
```

### Ligand Redesign To Model-Reaction Repair

Use this pattern when a later paper redesigns ligand architecture and improves a previously reported benchmark reaction.

- Preserve the earlier benchmark.
- Compare the redesigned ligand under matched conditions.
- State whether new scope was tested.

Generic template:

```text
Replacing [original ligand] with [redesigned ligand] improved the benchmark from [baseline] to [new metrics]. Because only [model substrate] was tested, this is optimization rather than scope expansion.
```

### Functionalized Allene Downstream Value

Use this pattern when a review justifies a narrow method through the synthetic utility of reactive allene products.

- State the functionalized allene class and demonstrated downstream reaction.
- Separate product utility from formation scope.

Generic template:

```text
The resulting [functionalized allene] served as [downstream reagent] with [partner], transferring chirality into [product element]. This utility does not expand formation scope.
```

### Unstable Product To Analytical Derivative

Use this pattern when the product formed in the catalytic step cannot be isolated or analyzed directly and is converted into a stable derivative.

- Name the primary product.
- Describe derivatization.
- Assign yield and ee to the correct stage.

Generic template:

```text
The catalytic reaction generated [unstable product] in [crude yield]. It was converted with [reagents] into [stable derivative], isolated in [yield] and analyzed at [ee].
```

### Chiral Catalysis Versus Background Pathway

Use this pattern when substantial product forms without the chiral catalyst.

- Report the catalyst-free reaction.
- Compare catalytic and background pathways.
- Explain how catalyst loading affects competition and ee.

Generic template:

```text
The substrate and reagents produced [background yield] without [chiral catalyst]. The catalytic pathway imposed asymmetric control, and higher loading changed competition with the background process.
```

### Essential Trapping Reagent As Pathway Controller

Use this pattern when a trapping reagent is required for the desired addition topology rather than merely stabilizing the product.

- Present the reaction with and without the reagent.
- State the change in reaction class.
- Include the reagent in method identity.

Generic template:

```text
Productive [addition topology] required [organometallic reagent] and [trapping reagent]. Without the latter, [desired product] was absent; changing reagent speciation redirected the reaction to [alternative topology].
```

### Matched Framework And Scaffold-Sensitive Selectivity

Use this pattern when high selectivity occurs only for one combination of substituent and core scaffold.

- Define the complete high-performing framework.
- Test whether the same substituent retains performance on a modified scaffold.

Generic template:

```text
High selectivity was obtained for [substituent] on [specific core]. Changing [core feature] while retaining the substituent lowered [metric], showing framework dependence.
```

### Proposed Enantiodetermining Isomerization

Use this pattern when a catalytic cycle assigns asymmetric induction to interconversion of two metal-bound intermediates.

- Separate observed products and controls from proposed intermediates.
- Use inferential language for the stereodetermining step.
- Identify evidence for later cycle steps.

Generic template:

```text
[Intermediate A] was proposed to isomerize to [intermediate B], with enantioselection assigned to that step. [Independent experiment] supports later steps, but the intermediates were not directly observed.
```

### Multi-Component Platform Redesign

Use this pattern when a later paper extends an earlier catalytic reaction by simultaneously changing substrate directing group, nucleophile precursor, and chiral ligand class.

- State the earlier limitation.
- Identify each redesigned component.
- Assign improvement to the redesigned system rather than one variable.

Generic template:

```text
The earlier [platform] was limited by [boundary]. A later design replaced [components] with [new components], enabling [product class] in [performance range]. Improvement belongs to the redesigned system as a whole.
```

### Substrate-Ligand Regioselectivity Partition

Use this pattern when substrate functionality, terminal substitution, and ligand structure each affect a different level of regioselectivity.

- Use substrate-analogue controls for reaction mode.
- Use terminal-group controls for insertion compatibility.
- Use ligand controls for productive versus deactivating insertion.

Generic template:

```text
[Functional group] controls [reaction mode], [terminal group] controls [insertion compatibility], and [ligand feature] favors productive insertion over deactivation.
```

### Isolated Deactivation Complex To Ligand Rationale

Use this pattern when an inferior ligand permits isolation of a stable, catalytically inactive metal intermediate.

- Report low activity.
- State structural evidence and catalytic-competence test.
- Use the species to explain deactivation rather than the productive cycle.

Generic template:

```text
Under [inferior ligand], the reaction stalled and [metal complex] was isolated. It was [structurally assigned] and catalytically inactive, supporting a deactivation pathway.
```

### Uniform High Ee With Bounded Reaction-Class Scope

Use this pattern when a scope table gives consistently high ee but remains confined to specific nucleophile and substrate classes.

- Report the narrow ee distribution.
- State structural classes actually varied.
- Preserve failed classes outside the boundary.

Generic template:

```text
Across [successful class variations], the method gave [products] in [yield range] and [narrow ee range]. Generality remained bounded by failure of [nucleophile class] and [substrate class].
```

### Critical-Feature Claim Calibration

Use this pattern when review prose labels structural features critical based on mixed optimization and mechanistic evidence.

- Decompose `critical` into direct necessity, strong correlation, and proposed causal role.

Generic template:

```text
[Feature A] was directly required for [outcome], whereas [feature B] correlated with [activity/selectivity]. The proposed explanation involving [effect] remains inference because [comparison limitation].
```

### Historical Precedent To Limitation To Improvement

Use this pattern when early asymmetric precedents are linked to a later quantitatively improved method.

- State the shared bond-forming concept.
- Define the historical limitation using original analytical evidence.
- Introduce the later design and remaining boundary.

Generic template:

```text
Early studies established [transformation] using [historical chiral-source designs], but stereochemical evidence remained [rotation/optical purity]. A later method used [new controller and precursor strategy] to give [product class] in [yield/selectivity range], with [boundary].
```

### Ketene-Delivery Architecture Separation

Use this pattern when acid chlorides, discrete ketenes, in situ ketenes, and ketene equivalents are grouped under one ketene-olefination heading.

- Group them only at field-classification level.
- Preserve physical precursor and ketene state in source-level prose.

Generic template:

```text
[Allene class] was formed from [isolated ketene / in situ ketene / ketene equivalent] using [nucleophile class]. The ketene-delivery mode should remain explicit because it governs [stability and operation].
```

### Stoichiometric Chiral-Source Improvement

Use this pattern when redesign of a stoichiometric chiral reagent substantially improves ee.

- Describe the result as improved asymmetric induction.
- Preserve the stoichiometric role of the chiral source.

Generic template:

```text
Replacing [earlier chiral reagent] with [redesigned stoichiometric controller] improved asymmetric induction from [baseline] to [new selectivity], without changing the stoichiometric nature of the chiral source.
```

### Range Plus Failure Boundary

Use this pattern when a review quotes successful yield and ee ranges from a scope table.

- Report explicit no-product entries that define required substitution patterns or intermediate stability.

Generic template:

```text
Successful substrates gave [yield/selectivity range], whereas [failed precursor class] produced [failure mode], defining the required [substitution or stability feature].
```

### Mechanistic Evidence Level Across Generations

Use this pattern when a historical progression contains stereochemical models, kinetic interpretations, and modern control experiments.

- Keep each source's mechanism at its own evidence level rather than combining them into one proved pathway.

Generic template:

```text
[Historical source] proposed [model], [later source] inferred [kinetic relationship], and [modern source] supplied [control experiment]. These observations support related but not identical mechanistic claims.
```

### Performance Boundary Mechanism Triad

Use this pattern when a source combines strong performance metrics, narrow demonstrated scope, practical recovery data, and mechanistic assignment experiments.

- Open with the transformation and headline performance.
- Put the scope boundary in the same sentence or the next one.
- Separate recovery or reuse evidence from mechanism evidence.
- Describe mechanistic controls as supporting an assignment unless direct observation proves the species throughout the cycle.

Generic template:

```text
[System] furnished [product class] in [metric range], although the demonstrated scope was confined mainly to [boundary]. [Recovered component] was regenerated and reused in [reuse evidence]. Comparisons of [candidate species] and [observed by-products/intermediates] supported assignment of [operative intermediate].
```

### Split Claims Across Platform Sequence

Use this pattern when an initial communication establishes a platform and a later full paper modifies the design, expands the scope, and reports limitations.

- Group the reports as one development sequence, but assign each claim to the source that supports it.
- Present initial platform, follow-up modification, expanded scope axes, and performance distribution in that order.
- Treat maximum values as exceptions within the distribution, not as the method's typical performance.

Generic template:

```text
The initial report established [platform] for [transformation], and the subsequent study modified [design element] while extending [defined scope axes]. Although [maximum metric] was achieved, most examples remained within [general regime], with only [defined subset] reaching [threshold].
```

### Uncited Section Bridge As Review Synthesis

Use this pattern when a review opens a subsection with a shared intermediate or conceptual framework but provides no citation for that bridge paragraph.

- Use the framework to explain the review's organization.
- Mark intermediate formation and product divergence as review synthesis.
- Defer factual mechanism and scope claims to individually cited method paragraphs.

Generic template:

```text
As an organizational framework, [starting-material classes] may converge on [proposed intermediate class] and give [product classes] after [reaction step]; the existence and selectivity of this pathway require verification from the individual cited studies.
```

### Optical Precedent To Quantified Catalysis

Use this pattern when older methods report optical activity or optical yield and a later catalytic method reports measured ee.

- Group older papers as historical precedent.
- Distinguish chiral-source or chiral-medium outcomes from catalytic enantioinduction.
- Introduce the later catalytic method with exact yield, ee, and example count.
- Preserve the remaining asymmetric-scope boundary.

Generic template:

```text
Earlier [reaction class] studies generated optically active [product class] using [chiral reagent/medium], but enantiopurity was [unquantified/weak]. A later [catalyst class]-catalyzed variant delivered [product class] in [yield] and [ee], although the asymmetric study comprised only [defined example set].
```

### Single Asymmetric Probe Within Racemic Platform

Use this pattern when a paper develops a multi-entry racemic platform and adds only one asymmetric experiment.

- Describe racemic platform and asymmetric probe as separate evidence units.
- Use the chiral example to establish feasibility, not scope.
- Preserve condition penalties or yield-selectivity tradeoffs.

Generic template:

```text
The achiral [catalytic platform] showed [racemic scope], while a single experiment with [chiral controller] established asymmetric feasibility at [yield] and [selectivity]. Broader asymmetric generality was not demonstrated.
```

### Majority Range With Named Outliers

Use this pattern when a method gives a consistent performance range for most scope entries but a small set of meaningful outliers.

- State the dominant range only after counting the relevant examples.
- Group exceptions by substrate feature.
- Keep conversion, isolation, and selectivity limitations separate.

Generic template:

```text
[Method] provided [product class] with [dominant metric range] for [number/fraction] of the examined [substrate set], whereas [outlier classes] gave lower [metric]. High performance required [structural prerequisite], and [conversion/isolation limitation] remained separate from the selectivity outcome.
```

### Table Data Over Author Adjective

Use this pattern when source prose characterizes a substrate class favorably but table values reveal weaker members.

- Use numerical table data as the calibration baseline.
- Preserve favorable performance where justified.
- Identify weaker entries as exceptions rather than repeating a class-wide promotional adjective.

Generic template:

```text
Although the source described [substrate class] as [author characterization], the table shows [dominant performance] with [defined exception] at [lower performance regime]; the review should therefore present the class as [qualified assessment].
```

### Limitation Circumvented By Platform Change

Use this pattern when a later paper reaches the target product class by changing the starting-material pathway rather than improving the earlier reaction on the same substrates.

- State the earlier limitation.
- Identify the new starting-material and mechanistic platform.
- Describe the later method as complementing or circumventing the earlier limitation.
- Avoid implying that the original system itself was repaired.

Generic template:

```text
Whereas [earlier platform] was limited by [conversion/scope/isolation problem], a later method circumvented this limitation through [new starting-material pathway], enabling [new product class] while retaining [remaining caveat].
```

### Intermediate Accumulation To Corrective Step

Use this pattern when reaction monitoring identifies a productive intermediate that accumulates under one-pot conditions but can be consumed in a second operation.

- State the sequence supported by time-course or interconversion experiments.
- Report initial product/intermediate purity.
- Explain the corrective operation and whether stereochemical purity is retained.

Generic template:

```text
Reaction monitoring showed initial formation of [intermediate], followed by conversion to [product]. Because [condition/component] retarded the second step, some entries retained [intermediate ratio]; re-exposure to [corrective conditions] increased [product purity] without [selectivity erosion].
```

### Tandem Scope Does Not Prove Direct Scope

Use this pattern when a catalyst has a broad tandem-process scope but only one isolated component reaction is tested.

- Treat tandem scope and direct-reaction validation as separate evidence sets.
- Use the isolated experiment to establish catalyst competence.
- Reserve generality language for a multi-substrate direct scope.

Generic template:

```text
The catalyst showed a broad [tandem-process] scope, while one independent [component-reaction] experiment confirmed competence for [step]. Generality of the isolated [component reaction] remained untested.
```

### Electrophile-Specific Selectivity Partition

Use this pattern when one pronucleophile platform reacts with different electrophile classes and each branch has a different selectivity bottleneck.

- Introduce the shared activation platform once.
- Divide the paragraph by electrophile class.
- Attach catalyst, product topology, rr, dr, ee, and failure classes to the correct branch.

Generic template:

```text
[Shared pronucleophile] was activated by [common activation mode] for reaction with two electrophile classes. With [electrophile A], [catalyst A] controlled [selectivity bottleneck A] to give [product outcome]. With [electrophile B], [catalyst B] maintained [selectivity metric] but introduced [different selectivity or competing-product limitation].
```

### Qualify Poorer As Relative Comparison

Use this pattern when one branch is regiospecific and another gives variable but sometimes high regioisomeric ratios.

- State the benchmark branch first.
- Describe the second branch as lower or poorer relative to that benchmark.
- Preserve the numerical range so strong individual entries remain visible.

Generic template:

```text
Whereas [branch A] was [regiospecific/selectivity benchmark], [branch B] showed lower and substrate-dependent regioselectivity, with [desired product]/[competing product] ratios ranging from [range].
```

### Dual-Fraction Resolution To Bounded Scope

Use this pattern when a kinetic-resolution paper examines multiple substrates but only a narrow structural subset reaches a high-selectivity threshold.

- Introduce the resolution platform.
- Preserve both enantiomeric fractions.
- Define the threshold operator exactly.
- Place rate, conversion, and inhibition boundaries next to the selectivity conclusion.

Generic template:

```text
[Resolution system] was evaluated across [number] racemic [substrates], furnishing [transformed fraction] and recovered [unreacted fraction]. Only [defined structural subset] reached [exact threshold] for [specified fraction], while [rate/inhibition/conversion boundary] limited broader application.
```

### Source Generality Claim To Critical Table Assessment

Use this pattern when source authors describe a method as potentially general but the review evaluates the demonstrated scope more critically.

- Separate the authors' prospective claim from the reported table.
- Base the review assessment on demonstrated selectivity, conversion, and rate.
- Preserve preliminary or unoptimized status without converting it into proof of impossibility.

Generic template:

```text
Although the authors proposed that [method] could become generally useful, the reported series showed [performance distribution], with strong outcomes confined to [subset]. Because the study was [unoptimized/preliminary], this defines the demonstrated boundary rather than a definitive impossibility.
```

### Screening To Scope Boundary

Use this pattern when a source first identifies a preferred controller through comparative screening and then defines a productive substrate trend plus a structural failure boundary.

- Compress screening to the selected controller and decisive comparison metric.
- Convert the scope table into supported substrate classes, not an inventory.
- Place structurally diagnostic weak examples after the positive trend.
- Keep selectivity, conversion, and rate separate when they do not move together.

Generic template:

```text
Screening [controller classes] identified [preferred controller] for [transformation]. The reported scope favored [supported class] over [weaker class] according to [metric], while removal of [structural feature] reduced [rate, conversion, or selectivity], defining a boundary of the method.
```

### Coupled Process Rate Compatibility Balance

Use this pattern when two catalytic components perform different steps and the integrated process succeeds only within a rate, selectivity, and compatibility window.

- Assign one function to each component before naming the combined platform.
- State the kinetic or selectivity relationship required for productive coupling.
- Compress screening into the complete selected system and its criteria.
- Present the preparative result together with the main operating or substrate-class tradeoff.

Generic template:

```text
To overcome [limitation of process A], [component A] for [step A] was coupled with [component B] for [step B]. Productive integration required [rate or selectivity relationship] and [compatibility condition]. The selected system provided [product class and performance], although [operational or scope limitation] remained.
```

### Substrate Topology Controller Reversal

Use this pattern when a catalyst, enzyme, ligand, or additive that performs best for one substrate topology becomes inferior after a change in substitution pattern, steric environment, or product topology.

- Establish the controller used for the original substrate class.
- Introduce the structural change that invalidates the earlier ranking.
- Compare controllers on matched axes such as rate, conversion, selectivity, loading, and side-product formation.
- Present the replacement controller as subclass-specific.

Generic template:

```text
Although [controller A] was preferred for [substrate class A], extension to [substrate class B] revealed [rate, selectivity, or practical limitation]. Comparative screening identified [controller B] as more effective for the new topology according to [matched metrics], although [weaker subclass or failure] remained.
```

### Application Paper Upstream Method Extraction

Use this pattern when a source paper focuses on a downstream transformation but contains an upstream resolution, preparation, or activation step relevant to another review section.

- Identify the source's primary contribution and the upstream operation being extracted.
- Reconstruct the upstream operation only from its own procedure and measurements.
- Separate downstream application metrics from upstream preparation metrics.
- Use stereochemical transfer or reporter-product data as indirect support only when the source establishes the relationship.

Generic template:

```text
Although the source primarily developed [downstream application], it prepared [intermediate class] through [upstream method]. The upstream step gave [direct outcome], while [downstream metric] only indirectly supported [precursor property] through [transfer evidence]. As a standalone preparation, the method remained limited by [recovery, scope, or workup limitation].
```

### Productive Kinetic Resolution Dual Branch

Use this pattern when kinetic resolution converts the faster-reacting enantiomer into a new product while the slower-reacting enantiomer is isolated as recovered starting material.

- Name the discriminatory reaction and chiral controller.
- Report recovered and transformed branches separately.
- Keep yield and stereochemical metrics attached to the correct chemical object.
- Interpret recovery yield relative to kinetic-resolution conversion.

Generic template:

```text
[Chiral controller]-catalyzed [reaction] kinetically resolved racemic [substrate class], furnishing recovered [starting material] in [yield] with [ee] and [transformed product class] in [yield] with [selectivity]. The demonstrated scope included [classes], while [boundary class] was [explicitly weak, failed, or not examined].
```

### Secondary Review Guided Primary Cluster

Use this pattern when an earlier review is cited to justify compressing a cluster of related primary methods.

- Use the secondary review to identify platform structure.
- Verify experimental claims against each primary paper.
- Group sources by shared reaction architecture while preserving electrophile, nucleophile, product topology, catalyst, and metric differences.
- State field-level trends only after checking all primary datasets for exceptions.

Generic template:

```text
[Secondary review] organized a family of [shared platform] methods, while the primary reports show that [electrophile classes] and [nucleophile classes] furnished [product families] across [performance range]. [Trend] was supported in [systems] but not universal because [counterexample], and [remaining scope axis] remained limited.
```

### Intermolecular To Tethered Intramolecular

Use this pattern when a later paper converts an external-nucleophile reaction into a tethered intramolecular process.

- State the earlier molecularity correctly.
- Introduce tethering as the design change.
- Preserve the activation or leaving-group event enabling internal attack.
- Separate general enantioselectivity from matched/mismatched diastereocontrol.

Generic template:

```text
Whereas earlier [platform] used an external [nucleophile], [tether design] enabled intramolecular [reaction event] from [substrate class], furnishing [product class] in [yield and ee]. With [chiral substrates], [substrate-catalyst combinations] controlled [diastereomer set] in [dr or de and ee], while [tested limitation] defined the scope boundary.
```

### Cross-Platform Threshold Qualified Comparison

Use this pattern when a review compares related catalytic platforms from separate papers and uses a numerical threshold to summarize the weaker platform.

- Define the common transformation and comparison axes.
- State that the comparison is cross-study if no controlled experiment exists.
- Summarize the full performance distribution before identifying threshold-qualified examples.
- Separate preparative examples from low-yield optimization outliers.

Generic template:

```text
[Platform B] provided an alternative to [platform A] for [shared transformation], giving [product class] across [yield and selectivity range]. Although its reported selectivities were generally lower on the chosen comparison axis, this conclusion is based on cross-study data. [Number] preparatively useful examples reached [threshold], while [low-yield or optimization-only result] is best treated separately.
```

### Proposed Intermediate To Intercepted Product

Use this pattern when a later study redesigns an earlier cascade so that a previously proposed transient intermediate becomes the isolated product.

- Present the earlier cascade and its intermediate evidence level.
- Identify the downstream step that normally consumes the intermediate.
- State the condition or catalyst redesign intended to interrupt the cascade.
- Treat isolation of the new product as support for pathway viability, not retrospective proof of the full earlier mechanism.

Generic template:

```text
An earlier [cascade] was proposed to generate [intermediate] before [downstream step] furnished [product A]. Modification of [controller or conditions] interrupted this sequence, allowing isolation of [product B] in [yield and selectivity]. This supports the viability of [intermediate-forming event], although [unresolved mechanistic point] remains. The method was limited to [verified substrate class].
```

### Repeated Enabler To Qualified Agenda

Use this pattern when several methods show that one design variable repeatedly improves performance.

- Aggregate matched examples across distinct systems.
- Preserve systems in which the intervention remained insufficient.
- Convert the pattern into a qualified research opportunity rather than a certain forecast.

Generic template:

```text
Across [reaction classes], variation of [design variable] repeatedly improved [metrics], identifying it as a productive research axis. Because [limitations] persisted, further development represents a plausible direction rather than a universal solution.
```

### Limitation To Concrete Hypothetical Target

Use this pattern when an established method family remains confined to one reaction mode and a conclusion proposes a new bond-forming event.

- State the demonstrated reaction mode and its limitations.
- Identify the new partner, bond formation, and desired product topology.
- Label the proposed transformation as hypothetical.
- Avoid exhaustive "only known" claims without a documented search boundary.

Generic template:

```text
Existing [method family] converts [substrate] through [reaction mode] but remains limited by [boundaries]. Trapping [supported intermediate] with [new partner] could provide [new topology], although control of [competing pathways] remains unverified.
```

### Demonstrated Platform To Speculative Extension

Use this pattern when a proven overall transformation is used to propose other metals or related elementary steps.

- Separate direct product evidence from mechanistic assignment.
- Preserve limitations of the established catalyst system.
- State which catalytic functions a new metal or elimination mode must reproduce.
- Present the extension as a hypothesis.

Generic template:

```text
[Established catalyst] directly provides [product class], while formation through [elementary step] is supported by [evidence level]. Extension to [new metal or reaction class] remains hypothetical and requires validation of [activation, selectivity, and turnover functions].
```

### Specific Gaps To Bounded Outlook

Use this pattern when closing a review after several specific unresolved problems have been identified.

- Retain the most consequential scope, selectivity, mechanism, or practicality gaps.
- Distinguish incremental platform extension from a conceptually new method.
- Use conditional rather than inevitable future language.

Generic template:

```text
Further progress will depend on resolving [gap A], [gap B], and [gap C]. Recent advances in [demonstrated platforms] justify continued investigation, while the success of new reaction concepts remains uncertain.
```

## Claim Compression

- Retain mechanistic experiments when they change certainty.
- Retain selectivity or performance numbers when they justify a comparison or define the method boundary.
- Retain condition details only when they explain reactivity, selectivity, mechanism, practicality, or comparison.
- Compress substrate tables into substrate families, functional-group classes, electronic or steric trends, and explicit failures.
- Compress repeated examples into a trend statement when they do not change the chemical interpretation.
- Omit routine optimization variants, ordinary characterization details, administrative metadata, and unsupported discussion claims.
- Do not turn a single attractive example into a field-wide trend unless the paragraph explicitly calls it a representative case.

## Claim Calibration

Use verbs according to source support:

- Neutral reported result: `reported`, `described`, `observed`, `identified`, `showed`.
- Directly supported claim: `demonstrated`, `established`, `confirmed`, `verified`, `elucidated`.
- Indirect or partial support: `suggested`, `indicated`, `supported`, `was consistent with`.
- Source-author proposal: `was proposed to`, `may involve`, `could proceed through`, `was interpreted as`.
- Conflict or limitation: `challenged`, `questioned`, `failed to explain`, `remained limited by`.

Do not let the source authors' promotional wording upgrade the evidence level.

## Comparison Rules

- Compare like with like: substrate class, product topology, activation mode, catalyst system, selectivity regime, mechanism evidence, operational practicality, or application value.
- Name the comparison axis before making a value judgment.
- Do not write "better", "improved", "efficient", "practical", "mild", "robust", "general", or "broad" unless the source gives the evidence needed for that adjective.
- If the source only permits a narrow comparison, state the boundary rather than smoothing it into a general improvement claim.

## Citation Behavior

If the user provides citation markers or asks for citation-ready prose:

- Use one citation for a paper-specific yield, selectivity, condition, substrate, method, or mechanism claim.
- Use grouped citations only for a genuinely shared trend or method family.
- Do not attach several citations to a narrow claim that only one source supports.
- Do not use a citation at the paragraph end to support multiple unrelated claims.
- Keep internal IDs, evidence IDs, and workflow scaffolding out of release prose.
- In paragraphs that combine a reported method, a reproduction challenge, and a later correction, attach citations by claim role: original report to the reported method, challenge source to non-reproducibility or characterization discrepancy, and later source to the corrected method and its controls. A paper that both reports its own revised method and challenges an earlier report may support both clauses, but the earlier report must still be cited for what it originally claimed.
- When one catalyst platform is represented by a foundation paper and several scope-extension papers, cite the foundation paper for the core catalyst/substrate platform, each extension paper for its own product family, and each paper's own metric. Do not let a grouped citation make an ee range from one family appear to apply to a carbohydrate series reported in de, or vice versa.
- In paragraphs that combine method transfer, benchmark comparison, and downstream applications, attach citations by function: method-transfer paper for the new substrate/product synthesis, benchmark paper for the comparison baseline, and application papers for later product use.

## Anti-Patterns

Block these failures:

- Serial abstract summary with no review-level takeaway.
- Adjective inflation without benchmark or numbers.
- Condition dumping that hides the conceptual signal.
- Unsupported mechanism promotion.
- Unstable synonym switching for core chemistry terms.
- Mixing yield, conversion, and selectivity metrics.
- Hiding limitations that materially affect interpretation.
- Citation stacking around a narrow paper-specific claim.
- Treating review articles as primary evidence for experimental results.
- Copying old review sentences or using old completed reviews as positive exemplars.
