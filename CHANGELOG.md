# Changelog

[中文说明](CHANGELOG.zh-CN.md)

## Standalone 0.6.4 — Intake and technical-HOLD diagnostics repair

- Added a versioned technical-HOLD receipt so an intake-PASS execution failure uses the same reason, action, and failed stage across the Closure Card, machine receipt, minimal receipt, GUI, and saved JSON.
- Retained bounded, sanitized provider status/code/detail without persisting raw responses, request bodies, prompts, manuscript text, credentials, or hidden diagnostics.
- Replaced format-sensitive intake blocking with `mrc-local-technical-preflight-1.0`; headings, section names/order, numbering, and front matter are advisory-only and cannot alter coverage routing.
- Added one-run `mrc-provider-transmission-consent-1.0`, bound to file SHA-256, provider, and model, with default refusal and a distinct user-canceled state.
- Added `mrc-semantic-manuscript-basis-1.0` to the existing first `mrc-whole-manuscript-coverage-3.0` response without another full-text request. Insufficient basis records one coverage usage/cost receipt, skips adjudication, and cannot masquerade as a technical failure.
- Complete usage receipts are counted independently from live price availability, so a missing price quote cannot erase known token accounting.
- Replaced candidate exact-ceiling semantics with `mrc-candidate-lower-bound-independent-additions-1.0`: every coverage candidate remains mandatory, while the independent second pass may recover only grounded canonical omissions. Zero candidates use a legal finite schema with a non-empty canonical enum; unknown, duplicate, unlocatable, speculative, or unexplained additions fail closed.
- Added `mrc-affirmative-stop-gate-1.0`. STOP now requires explicit positive sufficiency from both passes across the core contribution, argument, theory, methods, evidence, and coherence dimensions; caution or an empty candidate list alone is insufficient. Evidence-bound scope, status, rivals, and limitations remain protected without creating a default STOP bias.
- `mrc-schema-definition-lint-1.0` continues to reject empty/duplicate enums, invalid bounds, required/property mismatches, and unsupported schema structures before provider dispatch.
- Added a non-blocking low-risk heading-style advisory; numbering style no longer determines manuscript completeness.
- Preserved one physical attempt for timeout, network ambiguity, 429, 502, 503, and 504 responses.

## Standalone 0.6.3 — Provider contract and state integrity repair

- Disabled automatic full-request retries for coverage, adjudication, presentation repair, and interpretation; every logical call now has one physical HTTP attempt.
- Added bounded physical-request receipts, provider capability metadata, canonical schema hashes, and explicit unknown-potential-charge accounting.
- Embedded the canonical coverage and dynamic adjudication schemas in model-visible prompts while preserving strict API schema delivery where supported.
- Added dynamic candidate cardinality/enum binding, an independent exact-set verifier, and bounded missing/extra/duplicate diagnostics.
- Separated machine HOLD from presentation HOLD in the runtime and GUI without changing the Skill `0.2.1` academic decision contract.

## 0.2.1 — Public release candidate

- Added normalized receipt schema-family validation for absent, `0.1.x`, `0.2.0`, and `0.2.1` receipts.
- Rejected unsupported receipt versions instead of guessing their schema.
- Hardened directional Lite suggestions against command leakage across declared punctuation and wrapper boundaries.
- Preserved canonical hold codes, fixed labels, exact legacy migration, non-echo behavior, four substantive verdicts, dual-hash receipt semantics, and read-only routing.
- Added separate English and Simplified Chinese public documentation.
- Added four author-supplied documentation illustrations to the paired landing pages.
