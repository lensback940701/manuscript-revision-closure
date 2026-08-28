# Manuscript Revision Closure

[中文说明](README.zh-CN.md)

An evidence-bound Codex skill for deciding when a complete academic manuscript should stop general AI revision.

The skill addresses a recurring failure mode in AI-assisted academic writing: every new review generates another round of edits, each repair creates a different concern, and the manuscript never reaches a defensible stopping point. It performs a read-only whole-manuscript assessment and returns a compact closure decision without publishing the detailed internal review.

Current release candidate: `0.2.1`

<!-- ILLUSTRATION_SLOT_01_START -->
![An endless manuscript revision loop passes through an evidence-bound closure gate and becomes separate evidence, submission, and stop paths.](docs/images/01-closure-gate.png)
<!-- ILLUSTRATION_SLOT_01_END -->

## What the skill decides

The skill returns exactly one substantive verdict:

| Verdict | Meaning |
| --- | --- |
| `STOP_REVISING` | No observed material root cause justifies reopening substantive revision. |
| `ONE_BOUNDED_ROUND` | One local material problem is worth one strictly bounded round. |
| `REOPEN_SUBSTANTIVE_REVISION` | A central material root cause requires genuinely substantive revision. |
| `UNASSESSED` | The complete current manuscript or a critical assessment basis is unavailable. |

Verdicts are based on material root causes, not issue counts, generic perfection, acceptance predictions, hedge counts, or whether another wording is imaginable.

<!-- ILLUSTRATION_SLOT_02_START -->
![One complete manuscript enters a decision node that branches to the four canonical closure verdicts.](docs/images/02-four-verdicts.png)
<!-- ILLUSTRATION_SLOT_02_END -->

## What makes it different

- **Revision closure is separate from submission readiness.** A manuscript can be substantively closed while source verification, rights, formatting, metadata, or journal checks remain open.
- **Evidence limits remain visible.** Proposal, authorization, reported work, observation, outcome, interpretation, and causal inference are not collapsed for rhetorical smoothness.
- **Incomplete mechanisms are not automatic defects.** Delay, blockage, non-adoption, contradiction, reversal, and bounded stopping points may be analytical findings.
- **The public result stays compact.** The user receives a Closure Card and an optional minimal receipt, not a hidden peer-review report disguised as a short answer.
- **Diagnosis does not authorize surgery.** The skill never rewrites, redlines, searches literature, repairs citations, admits evidence, invokes another skill, or submits a manuscript.

<!-- ILLUSTRATION_SLOT_03_START -->
![A substantively closed manuscript remains separate from open evidence-verification and submission-readiness lanes.](docs/images/03-two-axis-separation.png)
<!-- ILLUSTRATION_SLOT_03_END -->

## Public output

A Closure Card contains:

1. the verdict;
2. one or two abstract reason sentences;
3. up to three directional Lite suggestions when revision is needed;
4. protected content that should not be disturbed;
5. separate evidence holds;
6. separate submission or external holds;
7. the next permitted action;
8. a conditional revision tip only when revision is actually needed.

Lite suggestions deliberately remain directional. They do not identify a sentence to replace, provide replacement prose, construct a revision sequence, or expose detailed internal review findings.

When the verdict requires revision, the card may end with this conditional tip:

> Diagnosis complete; surgery is a separate appointment. Use a trusted manuscript review-and-revision skill, or watch this profile for a future open-source release.

<!-- ILLUSTRATION_SLOT_04_START -->
![A compact Closure Card separates verdict, directional suggestions, protected content, evidence holds, submission holds, and next action.](docs/images/04-closure-card.png)
<!-- ILLUSTRATION_SLOT_04_END -->

## Safety and privacy boundary

- The manuscript is an immutable assessment target.
- Manuscript text, comments, and embedded instructions are treated as untrusted content.
- The skill does not deliberately persist or export its detailed internal assessment.
- Its assessment basis is a non-persisted internal whole-manuscript assessment used only to produce the closure verdict; the public result is not a detailed peer-review report or revision plan.
- Host-platform retention remains governed by the environment in which the skill runs.
- Canonical hold codes prevent caller-supplied hold prose from being echoed into public cards or receipts.
- Only a semantically stable prior `STOP_REVISING` receipt can be reused as a closure shortcut.
- Artifact-only drift does not become semantic stability unless a semantic hash or an explicit verification proves it.

This is a revision-routing aid, not factual certification, peer-review replacement, legal advice, journal acceptance prediction, or submission authorization.

## Installation

Clone the repository and place the repository folder at:

```text
~/.codex/skills/manuscript-revision-closure
```

On Windows, the usual location is:

```text
%USERPROFILE%\.codex\skills\manuscript-revision-closure
```

Restart or refresh Codex after installation. No third-party Python dependency is required by the runtime helper.

## Standalone Windows application

The repository also contains an experimental standalone runtime that can apply
the same read-only closure contract through the DeepSeek, Kimi, or Gemini API without a
Codex installation. Double-clicking the executable opens a localhost GUI with
an optional contract-bounded Chinese interpretation, assessment basis and dimensions,
brief limitations, pre-submission checklist, and an actual-usage cost estimate from
official pricing sources.
API keys are read only from environment variables. See
[`STANDALONE.zh-CN.md`](STANDALONE.zh-CN.md) for usage, build instructions, and
security boundaries. The standalone and Skill versions are managed separately;
this does not change the Skill's `0.2.1` contract version.

Standalone 0.6.2 is a bounded multi-stage runner with a visible multi-model selector and model-specific
reasoning controls. Unsupported provider/model/reasoning combinations fail
before an API request instead of being silently ignored.
Core assessment and optional interpretation requests use structured output.
Gemini and Kimi requests additionally carry an exact JSON Schema, while
the local validator accepts only one complete object with the exact eleven-key
contract. Usage from a contract-invalid interpretation response remains included
in the cost estimate. The former 5,000-token application cap was replaced with
provider-scale headroom (DeepSeek 384K, Kimi 128K, Gemini 64K) and explicit
length-truncation detection.
Kimi and DeepSeek are priced natively in CNY, Gemini in USD, with dated ECB
USD/CNY reference-rate conversion for dual-currency display.
Core assessment now uses two bound calls: a ten-dimension whole-manuscript
coverage pass and an independent full-text root-cause adjudication pass. A local
contradiction gate verifies the canonical coverage SHA-256, candidate accounting,
hold preservation, and protected invariants before the deterministic reducer runs.
After that gate, 0.6.2 freezes the canonical machine state before validating public-language fields. A Chinese presentation defect may trigger exactly one schema-bound presentation-only request with no manuscript text and no automatic retry; failure produces a recoverable presentation HOLD without erasing the machine verdict or usage. Protected source identity and localizable display text are bound separately, and each request emits one idempotent terminal event.
Kimi uses a 300-second coverage window and 900-second adjudication and interpretation
windows. Read/socket timeouts are not automatically resent; only explicit HTTP
429, 502, 503, and 504 responses receive bounded retries.

## Invocation

Example:

```text
Use $manuscript-revision-closure to decide whether this complete academic manuscript should stop general AI revision. Return only the concise Closure Card and minimal receipt. Do not edit the manuscript.
```

The skill must receive one identifiable, complete, current manuscript. A bounded excerpt or unclear version returns `UNASSESSED` rather than a fabricated whole-paper judgment.

## Deterministic helper

`scripts/closure_state.py` validates already-classified compact state, public-card invariants, canonical hold codes, receipt schema families, and receipt reuse rules. It does not read manuscripts or replace contextual academic judgment.

Run the tests with:

```bash
python -B -m unittest discover -s tests -p "test_*.py"
python -B scripts/run_adversarial_probes_rc2_0.py
python -B scripts/run_adversarial_probes_rc2_1.py
```

## Repository layout

```text
SKILL.md                         Skill instructions
agents/openai.yaml              Codex interface metadata
scripts/closure_state.py        Deterministic contract helper
references/hold-code-schema.md  Canonical hold codes and fixed labels
tests/                           Unit and contract regression tests
docs/images/                    Documentation illustrations
```

The included illustration slots and filenames are documented in [Documentation illustrations](ILLUSTRATIONS.md). They explain the public contract without changing the skill's decision logic.

## Security and contributions

See [Security Policy](SECURITY.md) and [Contributing](CONTRIBUTING.md). Do not submit real manuscripts, confidential review material, local paths, API keys, or project evidence as issues or test fixtures.

## License

Licensed under the [Apache License 2.0](LICENSE).
