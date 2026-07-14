# Rewrite Rubric

## Where To Find Information In The Paper

Use the paper structure to locate evidence before drafting. Do not rely only on the abstract when a full text, figures, tables, or supporting information are available.

- Title and graphical abstract: identify the transformation, material class, target property, or headline claim; verify all details elsewhere before using them.
- Abstract: extract the main advance, system, headline metric, and author-framed significance; treat it as a map, not the only evidence source.
- Introduction: use for the knowledge gap, prior limitation, and why the method matters; avoid importing broad background into the paragraph unless it explains the paper's placement in the review.
- Results and Discussion: use as the primary source for reaction class, substrate class, catalyst or material design, mechanism, controls, scope, performance, comparisons, and interpretation.
- Optimization tables: use for decisive condition choices only when they explain selectivity, activity, practicality, or mechanism; do not copy routine screening details.
- Scope tables: extract class-level patterns, representative anchors, yield/selectivity ranges, and explicit failures or weak substrates.
- Mechanistic studies: extract observed intermediates, control reactions, isotopic labeling, crossover tests, kinetics, spectroscopy, electrochemistry, trapping experiments, and calculations.
- Figures and tables: use for exact numerical values, trends, selectivity, stability, substrate breadth, catalyst comparison, and structure-property relationships.
- Supporting Information: use for full conditions, characterization, extended substrate scope, failed or low-yielding examples, control details, scale, reproducibility, and practical caveats.
- Conclusion: use for the authors' own framing only after verifying the claim against Results and Discussion.

## Extraction Checklist

Capture these facts before drafting. Keep only source-supported details.

1. Citation identity: authors, year, compound/material name, reaction class, or citation marker if provided.
2. Review placement: why this paper matters to the section's argument.
3. Chemical object: catalyst, material, molecule, ligand, electrolyte, analyte, substrate class, target, or platform.
4. Design rationale: structural feature, composition, synthesis route, active site, electronic effect, steric effect, supramolecular interaction, or computational hypothesis.
5. Method: decisive experiment, characterization, spectroscopy, microscopy, electrochemistry, kinetics, computation, biological assay, or reaction condition.
6. Key result: quantitative performance and comparison baseline where available.
7. Scope and boundary: substrate scope, operating window, stability, recyclability, selectivity, tolerance, or matrix effects.
8. Mechanistic interpretation: active species, pathway, rate-determining step, structure-property relationship, or evidence for the proposed explanation.
9. Limitation or caveat: unresolved mechanism, narrow scope, harsh conditions, low stability, scalability issue, toxicity, reproducibility concern, or missing control.
10. Review relevance: how the finding supports, contrasts with, extends, or limits the review's narrative.

When extraction is being used to learn or update writing rules, add a source-summary layer before comparing to the final paragraph:

- One-sentence source summary: the source paper's contribution, system, and review-relevant role.
- Evidence map: the exact source facts that support method identity, scope, metrics, mechanism, comparison, practicality, and limitation claims.
- Compression map: how those facts could become paragraph-level moves, such as opening claim, condition phrase, scope grouping, metric retention, caveat, or omitted routine detail.
- Logic label: the source-to-paragraph relationship that the rule should capture.

## Evidence Strength

Assign each important claim one evidence mode before writing:

- `reported_data`: directly reported experimental data, source-reported literature data, controls, trapping, crossover tests, isotope labeling, stereochemical probes, kinetics, spectroscopy, observed or isolated intermediates, or computations.
- `author_inference`: source-author proposal, analogy, substrate-trend interpretation, plausible mechanism, or review-level synthesis that is consistent with data but not directly demonstrated.
- `unsupported`: missing from the source material available to you.

Use evidence-bearing verbs for `reported_data`, cautious verbs for `author_inference`, and omit or flag `unsupported` claims.

### Supplemental Evidence Gates From Review-Source Extraction

Use these gates when extracting evidence from review paragraphs that mix secondary reviews, historical papers, stereochemical measurements, and mechanistic interpretations.

- `secondary-source-evidence-ceiling`: Reviews, perspectives, accounts, books, and reference works may support terminology, historical framing, field breadth, and source discovery, but they cannot upgrade an underlying experimental claim to `reported_data`. Treat the claim as `review_synthesis` until the primary experiment is available.
- `missing-selectivity-source-gate`: If a qualitative or quantitative selectivity statement depends on a missing citation, classify that statement as `missing_source` even when another available source reports optical activity, product formation, or a related stereochemical outcome.
- `priority-claim-quality-gate`: Words such as `first`, `earliest`, `first example`, `breakthrough`, or equivalent priority language require direct historical evidence plus a comparison basis. An author's own priority statement supports an author claim, not an independently verified exhaustive field conclusion.
- `incomplete-book-content-boundary`: Book frontmatter, table of contents, or chapter titles may establish identity, scope, and organization, but they do not support technical claims assigned to unavailable chapters.
- `availability-wording-verification`: Distinguish `reported`, `available in the literature`, `widely used`, `readily accessible`, and `commercially available`. Commercial availability requires direct source support.
- `review-cluster-progress-calibration`: When multiple reviews are grouped for a field-state statement, record the shared positive trend and any shared residual limitation. Do not count overlapping reviews as independent experimental confirmations.
- `chirality-transfer-efficiency-metric-gate`: Quantitative central-to-axial or point-to-axial chirality-transfer efficiency requires compatible stereochemical-composition measurements for both precursor and product. Optical rotation alone is insufficient.
- `paired-ee-transfer-audit`: Compare precursor and product ee or er directly. Distinguish high transfer, complete transfer, no product racemization, and small measurable ee erosion.
- `unknown-pure-rotation-ee-gate`: If the specific rotation of an enantiopure reference is unknown or uncertain, do not convert product rotation into reliable ee, er, optical purity, or chirality-transfer efficiency.
- `intrinsic-transfer-vs-product-erosion`: If the allene or product can racemize under the reaction conditions, final ee or optical yield reflects both formation and post-formation erosion unless stability controls separate them.
- `downstream-retention-paired-metric-gate`: Claims such as `retention of enantiopurity` or `no appreciable loss` require paired precursor and product ee or er values for each downstream transformation class.
- `mechanistic-branch-evidence-ranking`: Rank mechanistic evidence as direct observation or isolation > trapping or conversion of a competent intermediate > diagnostic condition effect > stereochemical correlation > analogy. Do not assign all evidence levels equal strength.
- `retrospective-mechanism-label-gate`: When a review applies a modern reaction-class or mechanistic label to a historical method, verify whether the original paper demonstrated that pathway, proposed it tentatively, or proposed a different mechanism.
- `conditional-exception-quality-gate`: Before accepting a general stereochemical or mechanistic conclusion, check the cited sources for condition-dependent reversal, competing pathways, substrate-class exceptions, and negative controls.
- `chemical-yield-stereochemical-efficiency-split`: Track chemical formation and stereochemical fidelity as separate evidence fields. A method may give useful yield while remaining inadequate for enantioenriched-product synthesis.
- `utility-claim-evidence-decomposition`: Support `useful` or `versatile building block` claims by identifying the downstream reaction or product classes documented in the cited application literature; do not let utility sources support preparation yields.
- `proposed-functional-group-cause-gate`: A functional group's demonstrated influence on a reaction does not prove that it caused a broader methodological gap. Classify the broad causal explanation as `author_inference` or `review_synthesis` unless directly tested.
- `single-chiral-example-generality-gate`: A single enantioenriched-substrate experiment can establish feasibility and a stereochemical relationship, but it does not establish broad stereospecificity across a platform.
- `salt-composition-evidence-field`: Record residual salts, counterions, reagent preparation, and additive composition when they alter ee, racemization, conversion, or pathway; the nominal reagent name alone may be insufficient.
- `chemical-correlation-measurement-label`: Distinguish product ee or configuration measured directly from stereochemical purity inferred by conversion into a known derivative.
- `solvent-comparison-strength-rule`: Call a solvent effect strong, dramatic, or enabling only when comparative screening shows a substantial change in conversion, yield, selectivity, or product identity under otherwise comparable conditions.
- `condition-dependent-product-topology-audit`: When the same reagent class gives different product skeletons in different solvents or conditions, preserve the condition and competing product instead of compressing the reactions into a generic derivatization statement.
- `common-symptom-multiple-causes-audit`: When several reactions show the same symptom, such as ee erosion, do not assume a common mechanism. Map each example to the reaction stage at which stereochemical information may be lost.
- `scope-count-denominator-rule`: A statement such as `only [number] products exceeded [threshold]` must identify the exact scope table, product subset, or source set used as the denominator.
- `cross-paragraph-citation-dependency`: A comparative conclusion relying on evidence cited in an earlier paragraph should be marked as cross-paragraph synthesis rather than treated as fully supported by the current paragraph's citations.
- `unmeasured-ee-exclusion`: Exclude products whose ee could not be determined from quantitative chirality-transfer comparisons; do not count them as either successes or failures against an ee threshold.
- `topology-controlled-transfer-audit`: Compare chirality transfer separately for terminal and internal unsaturation or substitution patterns rather than averaging them into one method-level selectivity statement.
- `chiral-ligand-double-stereodifferentiation-gate`: When an enantioenriched substrate is reacted with a chiral catalyst or ligand, classify the result as combined substrate transfer and catalyst stereodifferentiation unless matched/mismatched controls exclude that contribution.
- `intermediate-racemization-evidence-gate`: Product-stability controls may exclude product racemization, but they do not directly prove the identity or interconversion pathway of a metal-bound intermediate.
- `pressure-dependent-method-quality-gate`: Preserve gas pressure when it affects ee, rate, or conversion and when evaluating mildness, practicality, or transfer efficiency.
- `si-only-failure-evidence-gate`: A failed substrate stated in a review but documented only in unavailable Supporting Information must be marked `missing_source`, even when the successful substrate class strongly suggests the boundary.
- `racemic-substrate-process-classification`: Classify a racemic-substrate reaction as catalytic asymmetric induction, kinetic resolution, dynamic kinetic asymmetric transformation, or a mixed process according to recovered-substrate and intermediate-equilibration evidence.
- `ligand-sar-mechanism-ceiling`: Ligand structure-activity relationships support design correlations but do not by themselves prove the geometry, interaction, or energetic origin of enantioselection.
- `complete-catalytic-system-extraction`: When review prose centers a method on a named ligand, the extraction record must still retain the metal precursor, loading, additive, solvent, temperature, gas pressure, and substrate-dependent condition changes.
- `reagent-screen-vs-substrate-scope-count`: Count chiral-reagent, ligand, catalyst, additive, and solvent screening separately from substrate scope. Multiple optimization entries do not establish generality.
- `partially-catalytic-asymmetry-classification`: When a metal is catalytic but the stereochemistry-controlling reagent is stoichiometric, describe the process as stoichiometric asymmetric induction within a metal-catalyzed sequence, not fully catalytic asymmetric synthesis.
- `dynamic-process-evidence-threshold`: A racemic product from an enantioenriched precursor supports rapid loss of stereochemical information but does not by itself quantify dynamic intermediate equilibration.
- `empirical-configuration-assignment-label`: Absolute configuration assigned by an empirical optical-rotation rule must be distinguished from X-ray, chemical correlation, direct stereochemical determination, or direct chiral analysis.
- `within-error-complete-retention-gate`: `Complete retention of enantiopurity` may be accepted when precursor and product values overlap within stated analytical uncertainty, but retain both measurements and uncertainty.
- `transient-intermediate-evidence-label`: Distinguish an isolated and characterized intermediate from a transient TLC signal assigned by analogy and from a purely proposed structure.
- `one-operation-not-one-step-gate`: A one-operation or one-pot synthesis may contain ordered additions, temperature stages, activation events, and fragmentation steps; record these separately.
- `exact-hydrogen-shift-evidence-gate`: Do not accept an exact `[1,n]-H`-transfer assignment as `reported_data` unless supported by pathway-specific evidence or explicitly demonstrated in the primary source.
- `complementary-condition-branch-audit`: When a method uses two optimized condition sets, record the substrate class assigned to each branch and the failure observed when they are interchanged.
- `source-consistent-review-causal-upgrade-gate`: If a primary paper says an outcome is `consistent with` a mechanism, review prose must not upgrade it to a strict, proved, or uniquely causal sequence without additional evidence.
- `free-alcohol-reactive-state-extraction`: For methods described as direct transformations of free alcohols, record whether the operative species is an in situ metal alkoxide, metal-bound alcohol, or activated derivative.
- `si-racemization-detail-boundary`: A main-text statement of racemization may support a qualitative boundary, but detailed rates, time dependence, and magnitude remain `missing_source` when assigned to unavailable SI.
- `scope-axis-disambiguation`: When a source is described as scope-limited, identify whether the limitation concerns substrate structure, partner class, product topology, functional groups, stereochemical fidelity, or operational conditions.
- `competent-vs-obligatory-intermediate-gate`: Isolation and productive conversion of a proposed intermediate establish chemical competence but not necessarily obligatory on-cycle status.
- `historical-mechanism-retrofit-gate`: Do not assign a later mechanistic model retrospectively to a foundational paper that explicitly reported insufficient mechanistic evidence.
- `mixed-yield-method-audit`: Keep GLC, GC, NMR, conversion-normalized, and isolated yields separately labeled when comparing historical and modern sources.
- `yield-basis-conversion-audit`: Record whether isolated yield is calculated from charged substrate or from converted substrate. High yield based on conversion can coexist with poor overall material throughput.
- `stereochemical-source-component-audit`: Identify whether stereochemical information originates from a chiral catalyst, stoichiometric chiral reagent, auxiliary, enantioenriched substrate, or chiral product-stability selection.
- `chemical-scope-vs-high-fidelity-scope`: Separate all products that can be formed from the subset that retains high ee or dr.
- `precatalyst-active-species-distinction`: Distinguish the metal oxidation state charged as precatalyst from the experimentally inferred reactive oxidation state.
- `failed-example-metric-specificity`: Describe each failed substrate by the metric actually measured; keep low yield, trace formation, incomplete conversion, substrate consumption, unmeasured ee, and low selectivity distinct.
- `structural-variable-confounding-audit`: When successful and failed substrates differ in several structural variables, report a bounded correlation rather than assigning causality to one feature.
- `paper-title-branch-classification-gate`: Classify each mechanistic or stereochemical branch independently; do not let a paper title determine whether every branch is catalytic, asymmetric, racemic, or transfer-based in the same way.
- `source-economic-claim-conflict`: When sources disagree on practical descriptors such as cost, convenience, availability, or operational simplicity, preserve the disagreement and prioritize objective facts such as loading, equivalents, recovery, scale, and handling.
- `reproducibility-claim-source-separation`: Record an originally reported result and a later failed reproduction as separate evidence objects with separate provenance.
- `unresolved-analytical-discrepancy-gate`: Report discrepancies in optical rotation, calibrated rotation, or related analytical values neutrally unless the source establishes their cause.
- `staged-one-vessel-classification`: Distinguish sequential addition or staged heating in one vessel from a two-step procedure involving intermediate isolation.
- `si-dependent-reproducibility-detail`: When exact replication tables, reagent-source tests, rotation comparisons, or failed-substrate records occur only in unavailable SI, retain the qualitative main-text claim but mark numerical detail as `missing_source`.
- `multistep-yield-denominator-gate`: Identify whether an isolated yield covers one transformation or a reaction-plus-deprotection, derivatization, workup, or fragmentation sequence.
- `protecting-group-multifunction-evidence-split`: Separate directly observed protecting-group effects on yield, selectivity, stability, and removability from proposed steric, coordination, or stereodirecting roles.
- `dual-chiral-input-stereoisomer-audit`: For access to multiple stereoisomers, identify which chiral input controls each stereochemical element and whether different substrate enantiomers, ligand enantiomers, or chiral reagents are required.
- `selectivity-metric-type-separation`: Record ee, er, de, dr, optical rotation, optical purity, and configurational assignment as distinct measurements tied to the stereochemical relationship each one describes.
- `relay-stage-metric-separation`: In a stereochemical relay, extract intermediate and final-product metrics separately; if the intermediate metric is absent, label transfer efficiency as inferred.
- `negative-control-source-attribution`: Attribute a failed control to the paper that performed it, while using earlier papers only to define the successful precedent.
- `branch-specific-failure-gate`: Do not transfer a failed substrate from one mechanistic or stereochemical branch to another unless direct evidence supports the same boundary.
- `prospective-method-status-gate`: Treat statements that a more practical, one-pot, catalytic, or broader procedure is under development as prospective, not as reported methodology.
- `ligand-screen-vs-substrate-scope-audit`: Count unique substrates separately from ligand, catalyst, temperature, time, additive, or solvent entries; an extensive optimization table may still represent a one-substrate study.
- `indirect-allene-ee-label`: When a reactive allene reagent is analyzed through a derivative or downstream product, identify the measured compound and the assumption linking its ee to the original allene.
- `maximum-vs-distribution-selectivity-gate`: Evaluate method performance from the distribution of reported selectivities, not the maximum value alone.
- `ligand-improvement-not-scope-expansion`: Classify a later ligand that improves one benchmark substrate as optimization unless new substrate classes are tested.
- `unstable-product-derivative-analysis-gate`: When a primary product is unstable and ee is measured after derivatization, record the primary product, derivative, conversion sequence, and analytical assumption separately.
- `product-stage-yield-separation`: Keep crude yield, NMR yield, and isolated yield of a downstream derivative or stable product as separate fields.
- `asymmetric-catalysis-background-audit`: Quantify catalyst-free product formation when a background pathway competes with the chiral catalytic cycle and can dilute observed ee.
- `qualitative-selectivity-bin-calibration`: Do not group low, moderate, high, and excellent selectivity values under one descriptor when the source spans sharply different performance levels.
- `structural-feature-role-partition`: When several structural features are called critical, assign each to the metric directly supported by controls: activity, chemoselectivity, regioselectivity, enantioselectivity, compatibility, or product stability.
- `inactive-intermediate-mechanism-label`: An isolated metal complex that fails to produce product should be classified as off-cycle or deactivation evidence, not a productive intermediate.
- `functional-class-vs-steric-causality`: Success of several members of one functional class and failure of another class establish correlation, not a monotonic steric-bulk rule.
- `review-source-compound-number-map`: Record renumbering when a review assigns a different ligand, reagent, compound, scheme, or table number from the primary source.
- `historical-optical-activity-vs-ee-gate`: When historical papers report only specific rotation or indirectly calibrated optical purity, preserve those evidence forms rather than converting them into modern direct ee values.
- `yield-denominator-audit-consumed-substrate`: When isolated yields are calculated from consumed starting material, disclose that denominator whenever efficiency or practicality is compared.
- `optimized-sequence-vs-one-flask-separation`: Do not merge an optimized sequential protocol with a simplified one-flask variant; attribute the best data to the procedure that generated them.
- `cross-era-stereochemical-analysis-comparability`: Identify which stereochemical quantities are directly comparable when sources use rotation, resolution, degradation, CD, derivatization, chiral-shift NMR, or chiral chromatography.
- `review-compound-number-remapping`: Treat compound numbers as local identifiers and remap review numbering to primary-source structures before evidence extraction.
- `single-regeneration-is-not-cycle-durability`: A recovery, regeneration, and one-reuse experiment is evidence for one demonstrated reuse operation only. Do not upgrade it to repeated-cycle recyclability, durability, or long-term stability.
- `threshold-count-requires-defined-dataset`: Before accepting "only N products reached X" or similar threshold language, define whether the count refers to unique products, table entries, condition entries, reagent variants, or a selected scheme subset.
- `uncited-conceptual-scheme-is-not-evidence`: Treat an uncited mechanistic, intermediate, or organizational scheme as `review_synthesis` until each intermediate, selectivity relation, and scope claim is matched to cited primary evidence.
- `optical-rotation-is-not-enantiopurity`: Keep specific rotation, optical yield, ee, and er as separate evidence types. Do not infer a numerical or qualitative ee from rotation without a validated reference or calibration.
- `literature-frequency-claim-needs-review-level-support`: Claims such as "rare", "few examples", "widely used", or "little explored" are field-level synthesis unless supported by a defined survey or comprehensive secondary source.
- `distinguish-combined-recovery-from-product-yield`: When a table reports combined recovery of starting material plus product, extract product fraction or isolated product yield separately; combined recovery is not product yield.
- `verify-denominator-before-most`: Before using "most", "the majority", or a dominant-range statement, count the entries within the defined table or product set and name the outlier classes.
- `quantify-intermediate-contamination-before-minor`: Do not call residual intermediate or byproduct "minor" without a ratio or fraction. Ratios near 3:1 are material impurities, not trace contamination.
- `single-validation-example-is-competence-not-generality`: One independently tested substrate establishes feasibility or catalyst competence for a component reaction, but not substrate generality.
- `attach-each-metric-to-defined-species`: In regioisomeric or diastereomeric mixtures, record which species each yield, rr, dr, and ee value describes; combined yield cannot be applied to one desired product without support.
- `separate-regio-diastereo-and-enantio-evidence`: Regioselectivity, diastereoselectivity, and enantioselectivity are independent evidence fields. "Regiospecific" does not mean single diastereomer, and high ee does not establish high rr or dr.
- `preserve-strict-threshold-operators`: Preserve the distinction among `>`, `>=`, and `=` when converting tables into threshold statements; a value equal to a threshold does not exceed it.
- `distinguish-measured-and-calculated-ee`: Record whether ee was directly measured, inferred from optical rotation, measured by NMR, or calculated from literature rotation; these analytical modes are not equivalent without qualification.
- `extract-both-kinetic-resolution-fractions`: For kinetic resolution, extract conversion plus yield and enantiopurity for both the transformed product and the recovered starting material.
- `adjacent-source-no-backfill`: Do not use an available adjacent or follow-up source to validate a compound-specific result, metric, limitation, or mechanism assigned to an unavailable citation, even when the chemistry is closely related.
- `multi-criterion-system-selection`: When a catalyst, ligand, enzyme, controller, or integrated system is selected by multiple criteria, preserve all decisive axes: activity, selectivity, compatibility, stability, solubility, loading, and operational robustness.
- `selected-success-subset-audit`: If a review highlights only high-performing examples from a mixed scope, record weaker and failed entries before deciding whether the highlighted cases represent the method or only its upper-performance subset.
- `stage-specific-metric-provenance`: Attach every yield, conversion, ee, er, dr, rr, selectivity factor, and recovery value to the exact operation and chemical object measured; downstream reporter metrics support upstream precursor claims only indirectly.
- `untested-class-not-failed-class`: Absence from a scope table is not a failed substrate. Distinguish `not tested`, `not reported`, `low-performing`, and `failed` unless the paper or supplied SI reports an attempted example.
- `clustered-source-exception-audit`: When a review compresses several primary papers through an earlier secondary review, inspect each primary dataset and keep counterexamples visible before accepting a field-level trend.
- `molecularity-contrast-verification`: Verify `intermolecular` and `intramolecular` contrasts directly against the primary source; reversing molecularity is a claim-level fidelity error, not a style variation.
- `threshold-count-inclusion-audit`: For threshold counts across optimization and scope tables, define the inclusion rule, including yield, conversion, scale, table status, and whether the operator is `>`, `>=`, or `=`.
- `mechanistic-cluster-evidence-ladder`: In citation bundles for mechanism, classify each source separately as secondary background, isolated-product evidence, intermediate competence, control-supported inference, unobserved proposal, or speculative mechanism.
- `uncited-conclusion-claim-audit`: Divide uncited conclusion claims into retrospective synthesis, mechanistic interpretation, field-exhaustiveness claims, and future predictions; only the retrospective portion can inherit earlier verified support.

Evidence-status template:

```text
[Field-level statement] is supported by [secondary-source class], whereas [specific experiment, metric, priority claim, or mechanism] requires [primary source or paired measurement]. Because [required source/control/metric] is unavailable or incomplete, [claim] remains [review_synthesis / missing_source / author_inference / unsupported].
```

## Compression Rules

- Lead with the paper's distinct contribution, not the field's generic background.
- Combine method and result when possible: "Using in situ IR and isotope labeling, the authors assigned..."
- Retain numbers that change the interpretation; drop numbers that only decorate the claim.
- Replace long compound inventories with representative examples unless the full scope is the key result.
- Keep mechanisms conditional when the source is conditional: use "suggested", "consistent with", or "attributed to" instead of "proved" when evidence is indirect.
- Preserve negative or limiting findings if they affect how the paper should be interpreted in a review.
- Avoid repeating the paper title in paraphrased form.
- Compress source tables into substrate classes, functional-group tolerance classes, steric/electronic boundaries, and one representative anchor where needed.
- Keep the main limitation in the same sentence or immediately adjacent sentence when it changes how the advance should be judged.
- Do not generalize one catalyst, substrate class, material, cell line, analyte, or test condition to another without qualification.

## Paragraph Template

Use this logic, but do not force the wording:

```text
[Citation/authors if needed] reported/developed [main system or transformation], which [review-relevant advance]. Using [key method or evidence], they showed [central result with values when available]. The study [mechanistic interpretation or scope boundary], but [main limitation/caveat if relevant], making it [specific role in the review narrative].
```

## Quality Gate

Before returning the paragraph, answer internally:

- Does the paragraph say what was studied, how it was studied, what was found, and why it matters?
- Are all central quantitative results and chemical identifiers preserved?
- Are unsupported claims removed?
- Is the causal or mechanistic language no stronger than the evidence?
- Would a reader understand the paper's role in the review without reading the original abstract?
- Is the paragraph concise enough to fit into a review section without sounding like a standalone abstract?

If any answer is no, revise before responding.
