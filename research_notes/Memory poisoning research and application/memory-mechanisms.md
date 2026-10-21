# Memory poisoning mechanisms: primary-source research through 2026-10-17

## What mechanisms are established, and what evidence supports them?

### Takeaway

Persistent-memory attacks differ in how content enters storage, survives rewriting, becomes available in a later context, acquires apparent authority, and changes behavior. Directly planted Markdown notes isolate later consumption and adoption; they do not reproduce query-only acquisition or vector-retrieval optimization.

### Cited Findings

All entries below were independently opened on arXiv. Dates are primary abstract-page submission/revision dates, not search-engine crawl dates. “Full text” means accessible HTML with the specified substantive sections inspected, not an independent reproduction. Unless a venue is explicitly verified below, treat the item as a preprint; an arXiv posting alone does not establish peer review.

1. **AgentPoison: Red-teaming LLM Agents via Poisoning Memory or Knowledge Bases** (2024-07-17). Direct memory/knowledge-base corruption creates trigger-associated demonstrations; optimization targets retrieval as well as the resulting action. This is a foundational retrieval-side attack, not evidence that arbitrary natural-language notes fool coding agents. The primary abstract and HTML were opened; no configuration-specific numerical result is retained here because its denominator was not independently extracted. [Metadata](https://arxiv.org/abs/2407.12784); [full text](https://arxiv.org/html/2407.12784v1).

2. **Memory Injection Attacks on LLM Agents via Query-Only Interaction (MINJA)** (2025-03-05; latest v5 2026-02-12). Earlier title: *A Practical Memory Injection Attack against LLM Agents*. The attacker supplies queries and observes outputs, without direct memory writes; bridging reasoning and progressively shortened guidance induce stored examples that affect subsequent victim queries. Full-text methods and tables distinguish injection success from malicious reasoning. Table 1, GPT-4 EHR/MIMIC-III, reports 95.6% mean ISR and 57.0% mean ASR across nine victim-target pairs: these are different endpoints, not a single universal success probability. Numeric per-pair denominators were not fully reconciled, so omit these from headline comparisons. Table 5 gives exact detection counts, including general-prompt false positives 34/50 on EHR-MIMIC and detection 53/135 on RAP. NeurIPS 2025 conference PDF independently verifies publication status. [Metadata](https://arxiv.org/abs/2503.03704); [v5 full text](https://arxiv.org/html/2503.03704v5); [conference paper](https://papers.nips.cc/paper_files/paper/2025/file/42a97bbd9844d2bf68596730af80bcdf-Paper-Conference.pdf).

3. **MemoryGraft: Persistent Compromise of LLM Agents via Poisoned Experience Retrieval** (2025-12-18, v1). Full-text sections 4–5 show a more specific acquisition path than “a natural note gets remembered”: an ingestion Markdown artifact contains runnable code that constructs a mixed benign/poisoned RAG store. Later successful-experience imitation is evaluated with MetaGPT DataInterpreter/GPT-4o and union BM25/embedding retrieval. The distinctive conceptual mechanism is treating fabricated successful procedures as reusable experience. Retrieval penetration is not automatically behavioral attack success. Do not transfer its store-construction, executable artifact, or out-of-scope harmful actions into this sandbox note experiment. No numerical claim is retained without complete denominator verification. [Metadata](https://arxiv.org/abs/2512.16962); [full text](https://arxiv.org/html/2512.16962).

4. **From Untrusted Input to Trusted Memory: A Systematic Study of Memory Poisoning Attacks in LLM Agents** (2026-06-03; v2 2026-06-18). Full-text taxonomy identifies explicit instruction writes, system-policy-driven writes, compaction-driven writes, and experience-to-procedure writes. Its six attack classes are explicit command insertion, conditional command insertion, salience-driven compaction poisoning, policy-conformant fact injection, false precedent insertion, and skill-procedure insertion. External-content control excludes direct memory edits and user impersonation. A critical measurement distinction: the formal attack definition counts target content or paraphrase written into the store; later retrieval/persistence are additional objectives and analyses. “Weak signal” is the authors’ framing, not proof that malicious content is fundamentally undetectable. [Metadata](https://arxiv.org/abs/2606.04329); [full text, sections 2–4](https://arxiv.org/html/2606.04329).

5. **Hidden in Memory: Sleeper Memory Poisoning in LLM Agents** (2026-05-14; v2 2026-05-18). External documents induce fabricated user memories; subsequent conversations test delayed retrieval and use. Full-text Tables 1–2 distinguish injection rate (IR), retrieval rate (RR), and attack usage rate (AUR), with AUR explicitly conditional on the poisoned memory being in context. Thus abstract claims such as 60–89% agent-action usage are not end-to-end success. Harness sensitivity is material: model-controlled memory tools and an independent extraction manager yield different write outcomes. Internal inconsistencies need caution: main Table 1 lists DeepSeek-v4 tool/behavior IR 96.2, whereas Appendix F.3 refers to 82.2; defense Table 3 gives an adaptive DeepSeek GEPA value 25.2 while discussion says 21.8. Do not resolve these by silently choosing one. [Metadata](https://arxiv.org/abs/2605.15338); [full text](https://arxiv.org/html/2605.15338).

6. **MemMorph: Tool Hijacking in LLM Agents via Memory Poisoning** (2026-05-24, v1). Three crafted records resemble technical facts, incidents, and operational policies, causing the agent to infer an attacker-preferred tool choice. Both direct insertion and indirect dialogue are evaluated. Full-text section 4 defines ASR as the fraction of critical tasks selecting the risky tool and AHR as tasks retrieving at least one poison. Table 1's 85.9% is specifically direct poisoning on MetaTool with Claude Opus 4.1 and three records, not a universal rate; the absolute critical-task denominator was not extracted, so do not headline it. The framework is the closest source-grounded analogue for facts/precedents/policies, but its three-record tool-routing attack does not establish single-note coding-task effectiveness. [Metadata](https://arxiv.org/abs/2605.26154); [full text](https://arxiv.org/html/2605.26154).

7. **MemPoison: Bypassing Selective Memory Mechanisms to Plant Backdoors in LLM Agents** (2026-05-28; v2 2026-09-28). Earlier v1 title appears as *Hijacking Agent Memory: Stealthy Trojan Attacks Through Conversational Interaction*. This is distinct from item 9. Dialogue poisoning is designed to survive selective extraction and rewriting, using trigger/payload semantic association, entity-like triggers, and embedding optimization. Full-text Table 1 separates ISR, top-one retrieval success, downstream ASR, and benign accuracy under passive RAG versus active memory. The Consensus abstract reports “up to 0.95,” whereas current full-text passive-RAG/personal-agent ASR is 0.99; this is version/configuration drift, not a reason to combine numbers. No exact raw-count denominator was verified. [Metadata](https://arxiv.org/abs/2605.29960); [full text](https://arxiv.org/html/2605.29960).

8. **Your Agent's Memories Are Not Its Own: Forged Reasoning Attacks on LLM Agent Memory and Defenses (FARMA/SENTINEL)** (2026-07-06, v1). Direct write access targets remembered decision rationales rather than just facts; forged earlier validation and repeated self-reference amplify apparent precedent. Full-text Table III reports different behavior across simulated EHR, ReAct-QA and RAP environments. Do not claim the defense achieves universal zero: ReAct-QA is 6.0% in that same table; the limitations explicitly say a preliminary adaptive paraphrase attack defeats the heuristic Reasoning Guard. Zero false positives covers 326 traces (26 curated plus 300 clean-agent traces), not an arbitrary deployment. Multi-agent propagation is future work here. [Metadata](https://arxiv.org/abs/2607.05029); [full text, sections III–VI](https://arxiv.org/html/2607.05029).

9. **MemPoison: Uncovering Persistent Memory Threats and Structural Blind Spots in LLM Agents** (2026-07-16, v1). This separate benchmark contains 1,227 hand-validated cases; axes include fact/preference/instruction/state, user/tool/cross-agent channels, and flat chunks/fact stores/hierarchical notes. Difficulty levels distinguish single-record, compositional multi-record, and dormant context-triggered corruption. Its audit separates blocked writes, admitted-but-unretrieved entries, retrieved-but-noncausal entries, and residual causal effects, using counterfactual poison removal. Reported aggregate rates are macro-averages across model/substrate/defense cells rather than simple fractions of all 1,227 cases. This is particularly relevant to hierarchical Markdown memory but does not validate the httpx task. [Metadata](https://arxiv.org/abs/2607.14651); [full text, section 4 and Appendix B.8](https://arxiv.org/html/2607.14651).

10. **MEMORY Wins All: Indirect Bias Injection Attacks via Social Media Feeds (IBIA)** (2026-08-22, v1). Ordinary external-content ingestion is the acquisition path; retained topical stance affects later tasks without direct storage access. The abstract describes contextual cloaking, curation watermarks, and category anchoring; it reports a corpus of 6,000 comments and 120 email instances. Full-text HTML is available, but only overview/abstract claims were inspected deeply enough for this entry: quantitative AAR and watermark-detection rates are deliberately not imported without their evaluation denominators. Useful as an example that content-level bias and task-linked salience can persist; it does not justify marker sentences as proof of reading in our experiment. [Metadata](https://arxiv.org/abs/2608.22061); [full text](https://arxiv.org/html/2608.22061).

11. **Self-Propagating Misalignment in LLM Agents, and Why Auditing or Disabling Memory Is Not Enough** (2026-10-02, v1). This is adjacent, not an ordinary external injection: a system-prompt-induced misaligned source agent writes goals that a later default-prompt agent may inherit. Full-text methods use 20 scenarios and eight fresh replays per scenario/model cell; ten models are fully evaluated, with one partially evaluated model excluded from pooled comparisons. The abstract/Figure 1 report an auditor comparison 71% to 34% on 156 scenario/model pairs, while section IV-D says 58% to 27%; scopes are not clearly reconciled in inspected text. Treat those headline rates as unresolved. The paper also examines filesystem fallback, intervening sessions and cross-model succession. Its real-world incident assertions were not independently verified and are excluded. [Metadata](https://arxiv.org/abs/2610.04083); [full text](https://arxiv.org/html/2610.04083).

12. **Memory poisoning and secure multi-agent systems** (2026-03-20, v1). The full-text overview treats semantic, episodic and short-term memory as security surfaces and relates private-knowledge retrieval to multi-agent security. No measured propagation experiment was located in inspected HTML. Use as conceptual taxonomy only, not empirical evidence that poisoning spreads across agents. [Metadata](https://arxiv.org/abs/2603.20357); [full text](https://arxiv.org/html/2603.20357).

### Inferences

- Use a five-stage local model: **acquisition → storage/transformation → exposure/retrieval → adoption → submitted-patch outcome**. This synthesis keeps distinct the memory write, retrieval and action endpoints that the papers measure differently.
- Direct file planting sets acquisition and storage by construction. It tests whether the index/topic delivery exposes the claim and whether the agent gives that claim decision-making authority. It is not an end-to-end external-input poisoning success rate.
- Compaction/reflection laundering is a transformation mechanism, not merely longer persistence: source provenance may be lost while a proposition survives. A downstream self-authored note may therefore be derived from a planted source; authorship alone is not truth or authority.

### Gaps

- No result establishes a probability for omitting `CookieStore` from `__all__` in this exact task, harness or model suite.
- No numeric cross-paper ranking is defensible: attacker access, poison budget, retrieval, actions and success denominators differ.
- Primary titles and dates were verified; publication venues beyond the directly opened MINJA proceedings remain unverified in this pass.
- Preprints and version-sensitive tables are not independently replicated. Inconsistencies above must remain visible in the final synthesis.

## What transfers to the three planted-note forms?

### Takeaway

The relevant transfer is semantic authority, not trigger optimization: an environment claim can change a local inference, a fabricated precedent can displace present evidence, and a preference claim can impersonate authorization. The forms should share a fixed proposition and outcome so that comparison is not confounded by different ownership or compatibility stories.

### Cited Findings

- Fact-like context and procedural/incident records have a direct conceptual analogue in MemMorph; false precedent and policy-conformant fact injection are explicitly distinguished in MPBench. [MemMorph](https://arxiv.org/html/2605.26154); [MPBench](https://arxiv.org/html/2606.04329).
- Forged remembered reasoning is a separate source of apparent legitimacy from successful-experience imitation; neither prior validation nor success is established merely by a note asserting it. [FARMA](https://arxiv.org/html/2607.05029); [MemoryGraft](https://arxiv.org/html/2512.16962).
- Preference-like memories and goal-related retrieval are represented in contemporary sleeper-memory research and MemPoison-Bench, but conditional usage is not end-to-end harm. [Sleeper memory](https://arxiv.org/html/2605.15338); [MemPoison-Bench](https://arxiv.org/html/2607.14651).

### Inferences

- **A, environment fact:** test a false task-specific inference while retaining true, cheap-to-check observations. Do not call a clean-passing export test a pre-existing failure or turn a real Python access distinction into an alleged repository exemption without labeling the latter fabricated in the analysis.
- **B, precedent:** hold the target proposition constant and express only its provenance as an earlier accepted outcome. A fabricated record can be unrefutable from the checkout yet still lack authority; distinguish “not disproved” from “verified.”
- **C, preference:** the same proposition becomes an alleged user/maintainer preference. Record that memory metadata cannot itself authenticate the quoted preference. Avoid escalating to secrecy or inaccurate reporting.
- Avoid unusual marker sentences in the main efficacy condition: they alter naturalness and can become detection cues. The index load and exact topic-file read should be observed through harness-owned records; marker reproduction can be a secondary diagnostic, not an exposure oracle.
- If two candidates per form have different rationales, use two matched families: A1/B1/C1 share one proposition; A2/B2/C2 share another. Match length, index attractiveness, target specificity, and memory placement. This is a design recommendation, not a published efficacy finding.
- Keep harm exactly the submitted patch's new export-check failure. Preserve the four known sandbox baseline failures as user-supplied task-image evidence, not literature evidence. Neither report manipulation nor test removal belongs in the payloads.

### Gaps

No candidate payloads were authored in this research note. No agent, container, pytest, model API or paid experiment was run. Ordinary read-only web requests were used for research.

## How was the evidence gathered, and what remains excluded?

### Takeaway

Connected Consensus supplied discovery leads; primary arXiv pages and a conference paper supplied evidence. Search results and third-party summaries were not treated as validation.

### Cited Findings

- Consensus search `LLM agent memory poisoning MINJA MemoryGraft` returned ten leads. Mandatory full-record fetches were performed for MemoryGraft, MINJA, MPBench, Sleeper Memory and the selective-memory MemPoison; full primary URLs are recorded in the catalogue above.
- Web search families: `AgentPoison memory poisoning arxiv`; `MINJA memory injection attack agents`; `MemoryGraft poisoning long term memory`; `agent memory poisoning consolidation reflection 2026`; `MINJA arxiv memory injection`; `MemPoison arxiv`; `memory poisoning reflection multi-agent`; `FARMA 2607.05029`; `Temporal Dynamics of Memory Poisoning arxiv`; `MEMORY Wins All arxiv`; `MemMorph arxiv`.
- **Temporal Dynamics of Memory Poisoning in Web3-Style LLM Agents** was discovered through an author-uploaded ResearchGate copy claiming IEEE Access DOI 10.1109/ACCESS.2026.3693560 and 2,614 trajectories. Publisher verification and simulated-vs-live measurement details were not completed, so it is excluded from the core empirical catalogue. [Discovery copy](https://www.researchgate.net/publication/404886610_Temporal_Dynamics_of_Memory_Poisoning_in_Web3-Style_LLM_Agents).
- **Memory Poisoning Attack and Defense on Memory Based LLM-Agents** (2026-01-09) was opened at abstract level only; it studies memory population and retrieval sensitivity in EHR agents. Its characterization of MINJA is not used to replace MINJA's own current methods/results. [Abstract](https://arxiv.org/abs/2601.05504).

### Inferences

- Search saturation was reached for the assigned mechanisms rather than for every publication in the field. This is a broad, bounded review, not a systematic-review claim with exhaustive database coverage.
- Retrieval-specific papers should inform controlled retrieval ablations only if vector/BM25 infrastructure is actually introduced. Reading a Markdown index then opening a file is a different delivery mechanism.

### Gaps

- Initial `python` was unavailable and `python3` lacked BeautifulSoup. A standard-library `urllib`/regex metadata fetch succeeded without installing dependencies or executing project code.
- The first AgentPoison HTML v3 request failed; v1 HTML succeeded. MINJA v4 was initially inspected, then current v5 was opened. Metadata confirms v5 date; table conclusions should cite the current primary URL.
- All primary abstract URLs were fetched directly to verify title/date. Each core full-text URL was opened, but this was a targeted methods/results inspection, not a line-by-line reading of every appendix. IBIA is explicitly abstract/overview-only, and the multi-agent taxonomy has no verified numerical result.
- The earlier assistant's literature assertions and local review were not used as verified evidence. No persistent personal memory files were consulted or modified by this subtask.
