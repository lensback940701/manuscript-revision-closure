"""Validate and render a minimal manuscript-closure decision.

This module consumes an already-classified, compact decision state. It never
reads a manuscript, writes an assessment, stores a review narrative, or
decides a contextual academic issue from document text. Its purpose is to
keep the four public verdicts, receipt eligibility, deterministic identity
comparison, hold separation, and closed public-card schema consistent.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Mapping


PUBLIC_VERDICTS = frozenset(
    {
        "STOP_REVISING",
        "ONE_BOUNDED_ROUND",
        "REOPEN_SUBSTANTIVE_REVISION",
        "UNASSESSED",
    }
)
REVISION_VERDICTS = frozenset(
    {"ONE_BOUNDED_ROUND", "REOPEN_SUBSTANTIVE_REVISION"}
)
REUSABLE_RECEIPT_VERDICTS = frozenset({"STOP_REVISING"})
LEGAL_INVALIDATION_EVENTS = frozenset(
    {
        "semantic_content_changed",
        "new_material_evidence_contradiction",
        "new_reviewer_or_editor_requirement",
        "journal_requirements_materially_changed",
        "author_withdraws_prior_cutoff",
    }
)
IGNORED_NON_INVALIDATING_EVENTS = frozenset(
    {
        "artifact_only_drift",
        "comments_or_formatting_changed",
        "rights_or_metadata_changed",
        "generic_recheck_request",
    }
)
REQUIRED_SUGGESTION_FIELDS = (
    "Direction",
    "Why it matters",
    "What to protect",
)
COMMON_CARD_FIELDS = frozenset(
    {
        "Verdict",
        "Reason",
        "Lite directional suggestions",
        "Protected / Do not disturb",
        "Evidence holds",
        "Submission / external holds",
        "Next permitted action",
        "Conditional tip",
    }
)
STOP_CARD_OPTIONAL_FIELDS = frozenset(
    {"Parked opportunities", "Parked opportunities note"}
)
TECHNICAL_CARD_OPTIONAL_FIELDS = frozenset(
    {"Failed stage", "Technical hold contract version"}
)
PUBLIC_PROHIBITED_KEYS = frozenset(
    {
        "internal_review",
        "issue_register",
        "issue_ids",
        "locations",
        "quotes",
        "review_narrative",
        "revision_plan",
        "rewrite_plan",
        "hidden_reasoning",
        "chain_of_thought",
        "acceptance_tests",
    }
)
RECEIPT_ALLOWED_FIELDS = frozenset(
    {
        "manuscript_identity",
        "verdict",
        "reason_category",
        "evidence_hold_codes",
        "submission_hold_codes",
        "evidence_hold_summary",
        "submission_hold_summary",
        "invalidation_conditions",
        "next_permitted_action",
        "assessment_time",
        "skill_version",
        "artifact_sha256",
        "semantic_content_sha256",
        "technical_hold_contract_version",
        "failed_stage",
        "whole_manuscript_basis",
        "basis_reason_codes",
        "basis_explanation",
        "basis_contract_version",
        "provider_transmission_consent",
    }
)
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
ASSESSMENT_TIME_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?$"
)
RECEIPT_SCHEMA_LEGACY_UNSPECIFIED = "LEGACY_UNSPECIFIED"
RECEIPT_SCHEMA_LEGACY_0_1 = "LEGACY_0_1"
RECEIPT_SCHEMA_CANONICAL_0_2 = "CANONICAL_0_2"
RECEIPT_SCHEMA_UNSUPPORTED = "UNSUPPORTED"
SKILL_VERSION = "0.2.1"
TECHNICAL_HOLD_CONTRACT_VERSION = "mrc-technical-hold-receipt-1.0"
TECHNICAL_FAILED_STAGES = frozenset(
    {
        "local_preflight",
        "provider_configuration",
        "coverage_schema_definition",
        "coverage_context_budget",
        "coverage_provider",
        "coverage_contract",
        "coverage_incomplete",
        "adjudication_context_budget",
        "adjudication_schema_definition",
        "adjudication_provider",
        "adjudication_contract",
        "contradiction_gate",
    }
)


def _parse_receipt_schema_version(value: Any) -> str:
    """Normalize and classify a receipt version once at the receipt boundary."""

    if value is None:
        return RECEIPT_SCHEMA_LEGACY_UNSPECIFIED
    if not isinstance(value, str) or not value.strip():
        raise ClosureStateError("prior_receipt.skill_version must be a version scalar")
    normalized = value.strip()
    if not VERSION_RE.fullmatch(normalized):
        raise ClosureStateError("prior_receipt.skill_version must be a version scalar")
    numeric_core = re.split(r"[-+]", normalized, maxsplit=1)[0]
    components = numeric_core.split(".")
    if len(components) == 3 and components[:2] == ["0", "1"]:
        return RECEIPT_SCHEMA_LEGACY_0_1
    if len(components) == 3 and components[:2] == ["0", "2"] and components[2] in {"0", "1"}:
        return RECEIPT_SCHEMA_CANONICAL_0_2
    return RECEIPT_SCHEMA_UNSUPPORTED

EVIDENCE_HOLD_CODES = frozenset(
    {
        "SOURCE_VERIFICATION_REQUIRED",
        "SOURCE_PACKAGE_MISSING",
        "EVIDENCE_CONFLICT_REQUIRES_QUERY",
        "CLAIM_STATUS_UNRESOLVED",
        "SECOND_VERIFIER_REQUIRED",
        "BOUNDED_MECHANISM_STOPPING_POINT",
        "OTHER_EVIDENCE_HOLD",
    }
)
SUBMISSION_HOLD_CODES = frozenset(
    {
        "QUOTE_PERMISSION_UNRESOLVED",
        "IMAGE_RIGHTS_UNRESOLVED",
        "FORMAT_QA_PENDING",
        "COMMENTS_OR_TRACKING_REMAIN",
        "JOURNAL_CONTRACT_UNCHECKED",
        "ANONYMIZATION_PENDING",
        "AUTHOR_METADATA_MISSING",
        "DECLARATIONS_OR_CONTRACT_PENDING",
        "LICENSING_UNRESOLVED",
        "REVISION_AUTHORIZATION_PENDING",
        "OTHER_SUBMISSION_HOLD",
    }
)
HOLD_CODE_LABELS = {
    "en": {
        "SOURCE_VERIFICATION_REQUIRED": "Source verification required",
        "SOURCE_PACKAGE_MISSING": "Source package missing",
        "EVIDENCE_CONFLICT_REQUIRES_QUERY": "Evidence conflict requires author query",
        "CLAIM_STATUS_UNRESOLVED": "Claim status unresolved",
        "SECOND_VERIFIER_REQUIRED": "Second verifier required",
        "BOUNDED_MECHANISM_STOPPING_POINT": "Bounded mechanism stopping point requires verification",
        "OTHER_EVIDENCE_HOLD": "Other evidence hold requires human clarification",
        "QUOTE_PERMISSION_UNRESOLVED": "Quote permission unresolved",
        "IMAGE_RIGHTS_UNRESOLVED": "Image rights unresolved",
        "FORMAT_QA_PENDING": "Format QA pending",
        "COMMENTS_OR_TRACKING_REMAIN": "Comments or tracking remain",
        "JOURNAL_CONTRACT_UNCHECKED": "Journal contract unchecked",
        "ANONYMIZATION_PENDING": "Anonymization pending",
        "AUTHOR_METADATA_MISSING": "Author metadata missing",
        "DECLARATIONS_OR_CONTRACT_PENDING": "Declarations or contract pending",
        "LICENSING_UNRESOLVED": "Licensing unresolved",
        "REVISION_AUTHORIZATION_PENDING": "Revision authorization pending",
        "OTHER_SUBMISSION_HOLD": "Other submission hold requires human clarification",
    },
    "zh": {
        "SOURCE_VERIFICATION_REQUIRED": "来源核验待完成",
        "SOURCE_PACKAGE_MISSING": "来源材料包缺失",
        "EVIDENCE_CONFLICT_REQUIRES_QUERY": "证据冲突待作者确认",
        "CLAIM_STATUS_UNRESOLVED": "主张状态待确认",
        "SECOND_VERIFIER_REQUIRED": "第二核验者待完成",
        "BOUNDED_MECHANISM_STOPPING_POINT": "有界机制停止点待核验",
        "OTHER_EVIDENCE_HOLD": "其他证据事项待人工澄清",
        "QUOTE_PERMISSION_UNRESOLVED": "引文许可未解决",
        "IMAGE_RIGHTS_UNRESOLVED": "图像权利未解决",
        "FORMAT_QA_PENDING": "格式核查待完成",
        "COMMENTS_OR_TRACKING_REMAIN": "批注或修订痕迹仍存在",
        "JOURNAL_CONTRACT_UNCHECKED": "期刊合同待核查",
        "ANONYMIZATION_PENDING": "匿名化待完成",
        "AUTHOR_METADATA_MISSING": "作者信息缺失",
        "DECLARATIONS_OR_CONTRACT_PENDING": "声明或合同待完成",
        "LICENSING_UNRESOLVED": "许可事项未解决",
        "REVISION_AUTHORIZATION_PENDING": "修订授权待确认",
        "OTHER_SUBMISSION_HOLD": "其他投稿事项待人工澄清",
    },
}
LEGACY_HOLD_MAP = {
    "source verification required": ("SOURCE_VERIFICATION_REQUIRED",),
    "image rights unresolved": ("IMAGE_RIGHTS_UNRESOLVED",),
    "quote permission unresolved": ("QUOTE_PERMISSION_UNRESOLVED",),
    "format qa pending": ("FORMAT_QA_PENDING",),
    "comments or tracking remain": ("COMMENTS_OR_TRACKING_REMAIN",),
    "comments or formatting": ("COMMENTS_OR_TRACKING_REMAIN", "FORMAT_QA_PENDING"),
    "journal contract unchecked": ("JOURNAL_CONTRACT_UNCHECKED",),
    "anonymization pending": ("ANONYMIZATION_PENDING",),
    "author metadata missing": ("AUTHOR_METADATA_MISSING",),
    "licensing unresolved": ("LICENSING_UNRESOLVED",),
    "revision authorization pending": ("REVISION_AUTHORIZATION_PENDING",),
    "mechanism ends at documented blockage": ("BOUNDED_MECHANISM_STOPPING_POINT",),
    "来源核验待完成": ("SOURCE_VERIFICATION_REQUIRED",),
    "图像权利未解决": ("IMAGE_RIGHTS_UNRESOLVED",),
    "格式核查待完成": ("FORMAT_QA_PENDING",),
    "作者信息缺失": ("AUTHOR_METADATA_MISSING",),
}
for _code, _label in HOLD_CODE_LABELS["en"].items():
    LEGACY_HOLD_MAP.setdefault(_label.casefold(), (_code,))
for _code, _label in HOLD_CODE_LABELS["zh"].items():
    LEGACY_HOLD_MAP.setdefault(_label, (_code,))
ALL_HOLD_CODES = EVIDENCE_HOLD_CODES | SUBMISSION_HOLD_CODES

DECISION_FIELDS = frozenset(
    {
        "verdict",
        "reason_category",
        "prior_receipt_valid",
        "prior_receipt_stale",
        "prior_receipt_unverified",
        "material_root_cause",
        "central_root_cause",
        "evidence_hold_codes",
        "submission_hold_codes",
        "protected",
        "lite_suggestions",
        "next_permitted_action",
        "show_revision_tip",
    }
)
DECISION_REASON_CATEGORIES = frozenset(
    {
        "NO_MATERIAL_ROOT_CAUSE",
        "LOCAL_MATERIAL_ROOT_CAUSE",
        "CENTRAL_MATERIAL_ROOT_CAUSE",
        "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS",
        "TECHNICAL_EXECUTION_HOLD",
        "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED",
        "PRIOR_CLOSURE_STILL_VALID",
        "ARTIFACT_CHANGED_CONTENT_STABLE",
        "VERIFIED_ARTIFACT_ONLY_DRIFT",
        "SEMANTIC_CONTENT_STABLE",
    }
)
STOP_PRIOR_REASON_CATEGORIES = frozenset(
    {
        "PRIOR_CLOSURE_STILL_VALID",
        "ARTIFACT_CHANGED_CONTENT_STABLE",
        "VERIFIED_ARTIFACT_ONLY_DRIFT",
        "SEMANTIC_CONTENT_STABLE",
    }
)

TIP_ZH = "诊断到此，手术另约。请接入经过核实的审稿改稿 skill；或者，蹲一下本 profile 后续开源。"
TIP_EN = "Diagnosis complete; surgery is a separate appointment. Use a trusted manuscript review-and-revision skill, or watch this profile for a future open-source release."
FORMAL_TIP_ZH = "诊断完成；修订执行应另行授权。请使用经过核实的稿件审阅与修订 skill，或等待本 profile 的后续开源版本。"
FORMAL_TIP_EN = "Diagnosis is complete; any revision must be separately authorized. Use a trusted manuscript review-and-revision skill, or wait for a future open-source release."
STOP_PARKED_NOTE_EN = "These are not reasons to reopen the current manuscript. Reconsider them only if a new reviewer, journal requirement, evidence conflict, or author decision changes the task."
STOP_PARKED_NOTE_ZH = "这些不是重新打开当前稿件的理由。只有新的审稿人要求、期刊要求、证据冲突或作者决定改变任务时，才重新考虑它们。"
BASIS_PUBLIC_REASONS_EN = {
    "FRAGMENT_OR_EXCERPT_ONLY": "The first coverage pass found that the supplied text is materially only a fragment or excerpt, so no whole-manuscript revision-closure verdict was formed.",
    "SUBSTANTIVE_MATERIAL_MISSING": "The first coverage pass found that substantive manuscript material needed for a whole-manuscript revision-closure judgment is missing.",
    "INSUFFICIENT_ANALYTIC_CONTENT": "The first coverage pass found too little substantive argument, method, evidence, or analysis for a whole-manuscript revision-closure judgment.",
    "UNREADABLE_OR_CORRUPTED_EXTRACT": "The first coverage pass found that the extracted text is too unreadable or corrupted for a whole-manuscript revision-closure judgment.",
    "IDENTITY_OR_SCOPE_AMBIGUOUS": "The first coverage pass could not establish a sufficiently clear whole-manuscript identity or scope for revision closure.",
    "OTHER_SUBSTANTIVE_BASIS_GAP": "The first coverage pass found a substantive whole-manuscript basis gap, so no revision-closure verdict was formed.",
}

# These patterns apply to user-facing directions/protected text/parked text,
# not to hold labels. They are best-effort leakage checks, not a claim of
# deterministic semantic privacy in every language.
EN_LEAKAGE_PATTERNS = (
    re.compile(r"\b(?:section|subsection|paragraph|sentence|line|page)\s*(?:\d+(?:\.\d+)*)\b", re.I),
    re.compile(r"\b(?:in|at|under|within)\s+(?:section|subsection|paragraph|sentence|line|page)\b", re.I),
    re.compile(r"\b(?:replace|rewrite|delete|insert|add|remove|move|change)\s+(?:the\s+)?(?:section|subsection|paragraph|sentence|line|page)\b", re.I),
    re.compile(r"\b(?:exact quote|paragraph anchor|section anchor|replacement sentence|step-by-step revision plan|issue register)\b", re.I),
)
ZH_LEAKAGE_PATTERNS = (
    re.compile(r"第\s*[0-9一二三四五六七八九十百千万]+\s*(?:章|节|小节|段|句|行|页)"),
    re.compile(r"(?:小节|子节|段落|句子).{0,24}(?:第\s*[0-9一二三四五六七八九十百千万]+\s*(?:句|行|页)|上一句|下一句)"),
    re.compile(r"(?:请|应|需要|把|将|删除|新增|添加|替换|重写|改写|修改).{0,50}(?:第\s*[0-9一二三四五六七八九十百千万]+|小节|子节|段落|句子)"),
    re.compile(r"(?:删除|新增|添加|替换|重写|改写|修改|改成).{0,50}(?:句子|段落|小节|子节|第\s*[0-9一二三四五六七八九十百千万]+)"),
)
_EN_EDIT_VERB = (
    r"(?:edit|edited|editing|rewrite|rewritten|rewriting|replace|replaced|replacing|"
    r"delete|deleted|deleting|insert|inserted|inserting|move|moved|moving|remove|"
    r"removed|removing|add|added|adding|change|changed|changing|revise|revised|"
    r"revising|modify|modified|modifying)"
)
_EN_MANUSCRIPT_TARGET = (
    r"(?:methods?|methodology|introduction|conclusion|discussion|results?|findings?|"
    r"literature\s+review|theory|framework|abstract|title|table|figure|footnote|"
    r"citation|claim|mechanism|section|subsection|paragraph|sentence|line|page)"
)
EN_IMPLEMENTATION_PATTERNS = (
    re.compile(
        rf"\b{_EN_EDIT_VERB}\b(?:\s+\w+){{0,12}}\s+\b{_EN_MANUSCRIPT_TARGET}\b",
        re.I,
    ),
    re.compile(
        rf"\b{_EN_MANUSCRIPT_TARGET}\b(?:\s+\w+){{0,12}}\s+\b{_EN_EDIT_VERB}\b",
        re.I,
    ),
)
_ZH_EDIT_VERB = r"(?:重写|改写|替换|删除|插入|移动|新增|添加|改成|修改)"
_ZH_MANUSCRIPT_TARGET = (
    r"(?:方法(?:部分|论)?|引言|导论|结论|讨论|结果|发现|文献综述|理论|框架|摘要|标题|"
    r"表格|图表|脚注|引用|引文|论点|主张|机制|章节|小节|段落|句子|行|页)"
)
ZH_IMPLEMENTATION_PATTERNS = (
    re.compile(rf"{_ZH_EDIT_VERB}.{{0,40}}{_ZH_MANUSCRIPT_TARGET}"),
    re.compile(rf"{_ZH_MANUSCRIPT_TARGET}.{{0,40}}{_ZH_EDIT_VERB}"),
)
EN_EXPLICIT_EDIT_COMMAND_PATTERNS = (
    re.compile(rf"^\s*(?:please\s+)?{_EN_EDIT_VERB}\b", re.I),
    re.compile(
        rf"\b(?:should|must|need(?:s)? to|has to|have to|is|are|was|were|be|being|been|to)"
        rf"(?:\s+\w+){{0,6}}\s+\b{_EN_EDIT_VERB}\b",
        re.I,
    ),
    re.compile(
        rf"\b{_EN_EDIT_VERB}\b(?:\s+\w+){{0,8}}\s+\b(?:this|the)\s+"
        rf"(?:text|material|wording)\b",
        re.I,
    ),
)
ZH_EXPLICIT_EDIT_COMMAND_PATTERN = re.compile(
    rf"(?:请|应|需要|必须|要|把|将)?{_ZH_EDIT_VERB}"
)
NON_HOLD_DETAIL_TERMS = (
    "detailed peer-review report",
    "issue register",
    "hidden reasoning",
    "chain of thought",
    "内部审稿意见",
    "逐段审稿",
    "问题清单",
)

NEXT_PRIOR_STOP = "Keep the prior closure decision; do not start a new generic AI review without a legal invalidation event."
NEXT_STOP = "Do not start another generic AI revision; address any listed evidence or submission hold separately if authorized."
NEXT_UNASSESSED = "Provide one complete, identifiable current manuscript and the basis needed for a whole-manuscript assessment; this lane does not rewrite bounded material."
NEXT_TECHNICAL_HOLD = "Resolve the reported technical execution failure, then explicitly start one new assessment; do not treat this receipt as a manuscript verdict."
NEXT_PROVIDER_CONSENT = "Explicitly confirm provider transmission for one new run after verifying the bound file hash, provider, and model; cancellation sends nothing."
NEXT_ONE_ROUND = "Authorize one bounded revision round only, then rerun closure; this lane does not execute the round."
NEXT_REOPEN = "Obtain a separately authorized substantive review/revision workflow; this closure lane does not edit the manuscript."
NEXT_REWRITE_SUFFIX = " The request to rewrite is outside this read-only lane."
APPROVED_TIPS = frozenset({TIP_ZH, TIP_EN, FORMAL_TIP_ZH, FORMAL_TIP_EN})


class ClosureStateError(ValueError):
    """Raised when a compact closure or public-card contract is contradictory."""


def _bool(state: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = state.get(key, default)
    if not isinstance(value, bool):
        raise ClosureStateError(f"{key} must be boolean")
    return value


def _strings(state: Mapping[str, Any], key: str) -> list[str]:
    value = state.get(key, [])
    if not isinstance(value, list):
        raise ClosureStateError(f"{key} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ClosureStateError(f"{key} must contain non-empty strings")
        result.append(item.strip())
    return result


def _dedupe_codes(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _validate_hold_codes(value: Any, field: str, namespace: frozenset[str]) -> list[str]:
    if not isinstance(value, list):
        raise ClosureStateError(f"{field} must be a list of canonical hold codes")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in namespace:
            raise ClosureStateError(f"{field} contains an unknown or cross-namespace hold code")
        result.append(item)
    return _dedupe_codes(result)


def _normalise_legacy_label(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        raise ClosureStateError(f"{field} contains an unknown legacy hold label")
    normalized = " ".join(value.strip().split()).casefold()
    mapped = LEGACY_HOLD_MAP.get(normalized)
    if mapped is None:
        raise ClosureStateError(f"{field} contains an unknown legacy hold label; use a canonical hold code")
    return mapped


def _migrate_legacy_holds(value: Any, field: str, namespace: frozenset[str]) -> list[str]:
    if not isinstance(value, list):
        raise ClosureStateError(f"{field} must be a list of exact legacy hold labels")
    result: list[str] = []
    for item in value:
        mapped = _normalise_legacy_label(item, field)
        if any(code not in namespace for code in mapped):
            raise ClosureStateError(f"{field} maps across hold namespaces")
        result.extend(mapped)
    return _dedupe_codes(result)


def _normalise_state_holds(state: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    evidence_codes_key = "evidence_hold_codes" in state
    submission_codes_key = "submission_hold_codes" in state
    evidence_codes_present = evidence_codes_key and state["evidence_hold_codes"] != []
    submission_codes_present = submission_codes_key and state["submission_hold_codes"] != []
    legacy_evidence = state.get("evidence_holds", [])
    legacy_submission = state.get("submission_holds", [])
    legacy_external = state.get("external_holds", [])
    # Empty legacy lists are tolerated as inert RC1.x placeholders so a caller
    # can migrate incrementally. Any non-empty or malformed legacy value is
    # supplied state and cannot coexist with the new code representation.
    def supplied_legacy(key: str, value: Any) -> bool:
        return key in state and value != []

    any_legacy_supplied = any(
        (
            supplied_legacy("evidence_holds", legacy_evidence),
            supplied_legacy("submission_holds", legacy_submission),
            supplied_legacy("external_holds", legacy_external),
        )
    )
    if (evidence_codes_present or submission_codes_present) and any_legacy_supplied:
        raise ClosureStateError("canonical hold codes and legacy hold fields are ambiguous")

    if evidence_codes_present:
        evidence_codes = _validate_hold_codes(
            state["evidence_hold_codes"], "evidence_hold_codes", EVIDENCE_HOLD_CODES
        )
    elif "evidence_holds" in state:
        evidence_codes = _migrate_legacy_holds(
            legacy_evidence, "evidence_holds", EVIDENCE_HOLD_CODES
        )
    elif evidence_codes_key:
        evidence_codes = _validate_hold_codes(
            state["evidence_hold_codes"], "evidence_hold_codes", EVIDENCE_HOLD_CODES
        )
    else:
        evidence_codes = []

    if submission_codes_present:
        submission_codes = _validate_hold_codes(
            state["submission_hold_codes"], "submission_hold_codes", SUBMISSION_HOLD_CODES
        )
    elif "submission_holds" in state or "external_holds" in state:
        submission_codes = []
        if "submission_holds" in state:
            submission_codes.extend(
                _migrate_legacy_holds(legacy_submission, "submission_holds", SUBMISSION_HOLD_CODES)
            )
        if "external_holds" in state:
            submission_codes.extend(
                _migrate_legacy_holds(legacy_external, "external_holds", SUBMISSION_HOLD_CODES)
            )
        submission_codes = _dedupe_codes(submission_codes)
    elif submission_codes_key:
        submission_codes = _validate_hold_codes(
            state["submission_hold_codes"], "submission_hold_codes", SUBMISSION_HOLD_CODES
        )
    else:
        submission_codes = []
    return evidence_codes, submission_codes


def _render_hold_labels(codes: list[str], language: str, namespace: frozenset[str]) -> list[str]:
    if any(code not in namespace for code in codes):
        raise ClosureStateError("hold code cannot render across namespaces")
    return [HOLD_CODE_LABELS[language][code] for code in codes]


def _validate_public_hold_labels(value: Any, field: str, namespace: frozenset[str]) -> None:
    allowed = {HOLD_CODE_LABELS[language][code] for language in HOLD_CODE_LABELS for code in namespace}
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in allowed for item in value):
        raise ClosureStateError(f"{field} must contain only fixed mapped hold labels")


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _normalise_events(state: Mapping[str, Any]) -> set[str]:
    events = _strings(state, "invalidation_events")
    unknown = [
        event
        for event in events
        if event not in LEGAL_INVALIDATION_EVENTS | IGNORED_NON_INVALIDATING_EVENTS
    ]
    if unknown:
        raise ClosureStateError("unknown invalidation event: " + ", ".join(unknown))
    return set(events)


def _hash_value(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ClosureStateError(f"{field} must be a 64-character hexadecimal SHA-256")
    return value.casefold()


def _allowed_next_actions(verdict: str) -> frozenset[str]:
    actions = {
        "STOP_REVISING": frozenset(
            {
                NEXT_STOP,
                NEXT_PRIOR_STOP,
                NEXT_STOP + NEXT_REWRITE_SUFFIX,
                NEXT_PRIOR_STOP + NEXT_REWRITE_SUFFIX,
            }
        ),
        "UNASSESSED": frozenset(
            {
                NEXT_UNASSESSED,
                NEXT_UNASSESSED + NEXT_REWRITE_SUFFIX,
                NEXT_TECHNICAL_HOLD,
                NEXT_PROVIDER_CONSENT,
            }
        ),
        "ONE_BOUNDED_ROUND": frozenset(
            {NEXT_ONE_ROUND, NEXT_ONE_ROUND + NEXT_REWRITE_SUFFIX}
        ),
        "REOPEN_SUBSTANTIVE_REVISION": frozenset(
            {NEXT_REOPEN, NEXT_REOPEN + NEXT_REWRITE_SUFFIX}
        ),
    }
    try:
        return actions[verdict]
    except KeyError as exc:
        raise ClosureStateError("verdict has no approved route actions") from exc


def _allowed_receipt_reason_categories(verdict: str) -> frozenset[str]:
    categories = {
        "STOP_REVISING": frozenset({"NO_MATERIAL_ROOT_CAUSE"}) | STOP_PRIOR_REASON_CATEGORIES,
        "ONE_BOUNDED_ROUND": frozenset({"LOCAL_MATERIAL_ROOT_CAUSE"}),
        "REOPEN_SUBSTANTIVE_REVISION": frozenset({"CENTRAL_MATERIAL_ROOT_CAUSE"}),
        "UNASSESSED": frozenset(
            {
                "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS",
                "TECHNICAL_EXECUTION_HOLD",
                "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED",
            }
        ),
    }
    try:
        return categories[verdict]
    except KeyError as exc:
        raise ClosureStateError("verdict has no receipt reason categories") from exc


def _validate_receipt_cross_fields(
    verdict: str,
    reason_category: Any,
    next_action: Any,
    field_prefix: str,
) -> None:
    if reason_category is not None:
        if reason_category not in _allowed_receipt_reason_categories(verdict):
            raise ClosureStateError(
                f"{field_prefix}.reason_category is incompatible with its verdict"
            )
    if next_action is None:
        return
    if next_action not in _allowed_next_actions(verdict):
        raise ClosureStateError(
            f"{field_prefix}.next_permitted_action is not an approved route boundary"
        )
    if reason_category is None:
        return
    if reason_category == "NO_MATERIAL_ROOT_CAUSE":
        expected = frozenset({NEXT_STOP, NEXT_STOP + NEXT_REWRITE_SUFFIX})
    elif reason_category in STOP_PRIOR_REASON_CATEGORIES:
        expected = frozenset({NEXT_PRIOR_STOP})
    elif reason_category == "LOCAL_MATERIAL_ROOT_CAUSE":
        expected = frozenset({NEXT_ONE_ROUND, NEXT_ONE_ROUND + NEXT_REWRITE_SUFFIX})
    elif reason_category == "CENTRAL_MATERIAL_ROOT_CAUSE":
        expected = frozenset({NEXT_REOPEN, NEXT_REOPEN + NEXT_REWRITE_SUFFIX})
    elif reason_category == "TECHNICAL_EXECUTION_HOLD":
        expected = frozenset({NEXT_TECHNICAL_HOLD})
    elif reason_category == "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED":
        expected = frozenset({NEXT_PROVIDER_CONSENT})
    else:
        expected = frozenset({NEXT_UNASSESSED, NEXT_UNASSESSED + NEXT_REWRITE_SUFFIX})
    if next_action not in expected:
        raise ClosureStateError(
            f"{field_prefix}.reason_category and next_permitted_action are incompatible"
        )


def _validate_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ClosureStateError("prior_receipt must be an object or null")
    unknown = set(receipt).difference(RECEIPT_ALLOWED_FIELDS)
    if unknown:
        raise ClosureStateError("unknown prior_receipt fields: " + ", ".join(sorted(unknown)))
    manuscript_identity = receipt.get("manuscript_identity")
    if not isinstance(manuscript_identity, str) or not manuscript_identity.strip():
        raise ClosureStateError("prior_receipt.manuscript_identity is required")
    verdict = receipt.get("verdict")
    if not isinstance(verdict, str) or verdict not in PUBLIC_VERDICTS:
        raise ClosureStateError("prior_receipt.verdict must be a public verdict")
    for field in ("reason_category", "next_permitted_action", "assessment_time", "skill_version"):
        if field in receipt:
            value = receipt[field]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ClosureStateError(f"prior_receipt.{field} must be null or non-empty text")
            if field == "assessment_time" and value is not None and not ASSESSMENT_TIME_RE.fullmatch(value.strip()):
                raise ClosureStateError("prior_receipt.assessment_time must be an ISO-like date or date-time")
    schema_family = _parse_receipt_schema_version(receipt.get("skill_version"))
    if schema_family == RECEIPT_SCHEMA_UNSUPPORTED:
        raise ClosureStateError("prior_receipt.skill_version uses an unsupported receipt schema")
    _validate_receipt_cross_fields(
        verdict,
        receipt.get("reason_category"),
        receipt.get("next_permitted_action"),
        "prior_receipt",
    )
    technical_version = receipt.get("technical_hold_contract_version")
    failed_stage = receipt.get("failed_stage")
    if receipt.get("reason_category") == "TECHNICAL_EXECUTION_HOLD":
        if technical_version != TECHNICAL_HOLD_CONTRACT_VERSION:
            raise ClosureStateError(
                "prior_receipt technical hold must use the registered technical contract version"
            )
        if not isinstance(failed_stage, str) or failed_stage not in TECHNICAL_FAILED_STAGES:
            raise ClosureStateError("prior_receipt technical hold has an invalid failed_stage")
    elif technical_version is not None or failed_stage is not None:
        raise ClosureStateError(
            "prior_receipt technical fields require TECHNICAL_EXECUTION_HOLD"
        )
    code_fields_present = {
        "evidence_hold_codes" in receipt,
        "submission_hold_codes" in receipt,
    }
    legacy_fields_present = {
        "evidence_hold_summary" in receipt,
        "submission_hold_summary" in receipt,
    }
    if any(code_fields_present) and any(legacy_fields_present):
        raise ClosureStateError("prior_receipt cannot mix submission code and legacy fields")
    if schema_family == RECEIPT_SCHEMA_CANONICAL_0_2 and (
        "evidence_hold_summary" in receipt or "submission_hold_summary" in receipt
    ):
        raise ClosureStateError("canonical receipts cannot use legacy hold summary fields")
    if schema_family == RECEIPT_SCHEMA_CANONICAL_0_2 and not (
        "evidence_hold_codes" in receipt and "submission_hold_codes" in receipt
    ):
        raise ClosureStateError("canonical receipts must contain both canonical hold-code fields")

    if "evidence_hold_codes" in receipt:
        evidence_hold_codes = _validate_hold_codes(
            receipt["evidence_hold_codes"],
            "prior_receipt.evidence_hold_codes",
            EVIDENCE_HOLD_CODES,
        )
    elif "evidence_hold_summary" in receipt:
        evidence_hold_codes = _migrate_legacy_holds(
            receipt["evidence_hold_summary"],
            "prior_receipt.evidence_hold_summary",
            EVIDENCE_HOLD_CODES,
        )
    else:
        evidence_hold_codes = []

    if "submission_hold_codes" in receipt:
        submission_hold_codes = _validate_hold_codes(
            receipt["submission_hold_codes"],
            "prior_receipt.submission_hold_codes",
            SUBMISSION_HOLD_CODES,
        )
    elif "submission_hold_summary" in receipt:
        submission_hold_codes = _migrate_legacy_holds(
            receipt["submission_hold_summary"],
            "prior_receipt.submission_hold_summary",
            SUBMISSION_HOLD_CODES,
        )
    else:
        submission_hold_codes = []

    for field in ("invalidation_conditions",):
        if field in receipt:
            value = receipt[field]
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ClosureStateError(f"prior_receipt.{field} must be a string list")
    if "invalidation_conditions" in receipt:
        unknown_events = [
            item
            for item in receipt["invalidation_conditions"]
            if item not in LEGAL_INVALIDATION_EVENTS
        ]
        if unknown_events:
            raise ClosureStateError(
                "prior_receipt.invalidation_conditions contains unknown event: "
                + ", ".join(unknown_events)
            )
    for field in ("artifact_sha256", "semantic_content_sha256"):
        if field in receipt and receipt[field] is None:
            raise ClosureStateError(f"prior_receipt.{field} must be a SHA-256 when supplied")
    basis_fields = {
        "whole_manuscript_basis",
        "basis_reason_codes",
        "basis_explanation",
        "basis_contract_version",
    }
    present_basis_fields = basis_fields.intersection(receipt)
    if present_basis_fields and present_basis_fields != basis_fields:
        raise ClosureStateError("prior_receipt semantic basis fields must be present together")
    if present_basis_fields:
        if receipt["whole_manuscript_basis"] not in {"SUFFICIENT", "INSUFFICIENT"}:
            raise ClosureStateError("prior_receipt whole_manuscript_basis is invalid")
        codes = receipt["basis_reason_codes"]
        if not isinstance(codes, list) or not codes or any(
            not isinstance(item, str) or not item.strip() for item in codes
        ):
            raise ClosureStateError("prior_receipt basis_reason_codes must be a non-empty string list")
        explanation = receipt["basis_explanation"]
        if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 240:
            raise ClosureStateError("prior_receipt basis_explanation must be bounded text")
        if receipt["basis_contract_version"] != "mrc-semantic-manuscript-basis-1.0":
            raise ClosureStateError("prior_receipt basis_contract_version is invalid")
    consent = receipt.get("provider_transmission_consent")
    if consent is not None:
        if not isinstance(consent, Mapping) or set(consent) != {
            "contract_version",
            "status",
            "binding_match",
            "artifact_sha256",
            "provider",
            "model",
            "recorded_at",
        }:
            raise ClosureStateError("prior_receipt provider consent must match the compact schema")
        if consent["contract_version"] != "mrc-provider-transmission-consent-1.0":
            raise ClosureStateError("prior_receipt provider consent contract version is invalid")
        if consent["status"] not in {"CONFIRMED", "NOT_AUTHORIZED"}:
            raise ClosureStateError("prior_receipt provider consent status is invalid")
        if not isinstance(consent["binding_match"], bool):
            raise ClosureStateError("prior_receipt provider consent binding_match must be boolean")
        _hash_value(consent["artifact_sha256"], "prior_receipt.provider_transmission_consent.artifact_sha256")
        for field in ("provider", "model", "recorded_at"):
            if not isinstance(consent[field], str) or not consent[field].strip():
                raise ClosureStateError(f"prior_receipt provider consent {field} must be text")
        if not ASSESSMENT_TIME_RE.fullmatch(consent["recorded_at"].strip()):
            raise ClosureStateError("prior_receipt provider consent recorded_at is invalid")
    return {
        "manuscript_identity": manuscript_identity.strip(),
        "verdict": verdict,
        "evidence_hold_codes": evidence_hold_codes,
        "submission_hold_codes": submission_hold_codes,
        "artifact_sha256": _hash_value(receipt.get("artifact_sha256"), "prior_receipt.artifact_sha256"),
        "semantic_content_sha256": _hash_value(
            receipt.get("semantic_content_sha256"),
            "prior_receipt.semantic_content_sha256",
        ),
        "reason_category": receipt.get("reason_category"),
        "technical_hold_contract_version": technical_version,
        "failed_stage": failed_stage,
    }


def _receipt_reuse_gate(state: Mapping[str, Any]) -> bool:
    """Return whether current basis can even be considered for receipt reuse."""

    if not _bool(state, "manuscript_complete"):
        return False
    if not _bool(state, "current_identity_clear"):
        return False
    if _bool(state, "bounded_scope"):
        return False
    current_identity = state.get("current_manuscript_identity")
    if not isinstance(current_identity, str) or not current_identity.strip():
        if _bool(state, "current_identity_clear"):
            raise ClosureStateError(
                "current_identity_clear=true requires current_manuscript_identity"
            )
        return False
    return True


def _receipt_assessment(state: Mapping[str, Any]) -> dict[str, Any]:
    receipt_raw = state.get("prior_receipt")
    if receipt_raw is None:
        return {
            "valid": False,
            "stale": False,
            "unverified": False,
            "category": "NO_PRIOR_RECEIPT",
        }
    receipt = _validate_receipt(receipt_raw)
    events = _normalise_events(state)
    if not _receipt_reuse_gate(state):
        return {
            "valid": False,
            "stale": False,
            "unverified": False,
            "category": "PRIOR_RECEIPT_NOT_ELIGIBLE_WITHOUT_CURRENT_BASIS",
        }
    current_identity = str(state["current_manuscript_identity"]).strip()
    if receipt["verdict"] == "UNASSESSED":
        return {
            "valid": False,
            "stale": True,
            "unverified": False,
            "category": "PRIOR_UNASSESSED_NOT_REUSABLE",
        }
    if receipt["manuscript_identity"] != current_identity:
        return {
            "valid": False,
            "stale": True,
            "unverified": False,
            "category": "PRIOR_RECEIPT_IDENTITY_MISMATCH",
        }
    if receipt["verdict"] not in REUSABLE_RECEIPT_VERDICTS:
        return {
            "valid": False,
            "stale": False,
            "unverified": False,
            "category": "PRIOR_REVISION_DECISION_NOT_REUSABLE",
        }
    if events & LEGAL_INVALIDATION_EVENTS:
        return {
            "valid": False,
            "stale": True,
            "unverified": False,
            "category": "LEGAL_INVALIDATION_EVENT",
        }

    artifact_only_drift_verified = _bool(state, "artifact_only_drift_verified")
    current_artifact = _hash_value(
        state.get("current_artifact_sha256"), "current_artifact_sha256"
    )
    current_semantic = _hash_value(
        state.get("current_semantic_content_sha256"),
        "current_semantic_content_sha256",
    )
    receipt_artifact = receipt["artifact_sha256"]
    receipt_semantic = receipt["semantic_content_sha256"]

    # No deterministic identities were supplied on either side. The clearly
    # identical manuscript label is the legacy-but-explicit binding.
    if not any((current_artifact, current_semantic, receipt_artifact, receipt_semantic)):
        return {
            "valid": True,
            "stale": False,
            "unverified": False,
            "category": "PRIOR_CLOSURE_STILL_VALID",
        }

    if current_semantic is not None and receipt_semantic is not None:
        if current_semantic != receipt_semantic:
            return {
                "valid": False,
                "stale": True,
                "unverified": False,
                "category": "SEMANTIC_CONTENT_CHANGED",
            }
        semantic_stable = True
    else:
        semantic_stable = False

    if current_artifact is not None and receipt_artifact is not None:
        if current_artifact == receipt_artifact:
            # Equal artifact identity is sufficient even if an optional
            # semantic hash was omitted on one side.
            return {
                "valid": True,
                "stale": False,
                "unverified": False,
                "category": "PRIOR_CLOSURE_STILL_VALID",
            }
        if semantic_stable:
            return {
                "valid": True,
                "stale": False,
                "unverified": False,
                "category": "ARTIFACT_CHANGED_CONTENT_STABLE",
            }
        if artifact_only_drift_verified:
            return {
                "valid": True,
                "stale": False,
                "unverified": False,
                "category": "VERIFIED_ARTIFACT_ONLY_DRIFT",
            }
        return {
            "valid": False,
            "stale": False,
            "unverified": True,
            "category": "ARTIFACT_DRIFT_WITHOUT_SEMANTIC_PROOF",
        }

    if semantic_stable:
        return {
            "valid": True,
            "stale": False,
            "unverified": False,
            "category": "SEMANTIC_CONTENT_STABLE",
        }

    # A hash on only one side cannot bind the current manuscript to the
    # receipt. Do not infer stability from a label or from an unpaired hash.
    return {
        "valid": False,
        "stale": False,
        "unverified": True,
        "category": "UNPAIRED_DETERMINISTIC_IDENTITY",
    }


def prior_receipt_status(state: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return ``(still_valid, stale_or_unbound)`` for compatibility."""

    assessment = _receipt_assessment(state)
    return bool(assessment["valid"]), bool(assessment["stale"] or assessment["unverified"])


def _material_issue(issue: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return ``(material, central)`` without retaining issue detail."""

    required_bool_fields = (
        "observed",
        "locatable",
        "style_only",
        "hold_only",
        "verification_only",
        "expected_benefit_exceeds_risk",
    )
    for key in required_bool_fields:
        if not isinstance(issue.get(key), bool):
            raise ClosureStateError(f"material root-cause field {key} must be boolean")
    affects = issue.get("affects")
    if isinstance(affects, str):
        affects_present = bool(affects.strip())
    elif isinstance(affects, list):
        affects_present = bool(affects) and all(
            isinstance(item, str) and item.strip() for item in affects
        )
    else:
        raise ClosureStateError("material root-cause field affects must be text or a string list")
    scope = issue.get("scope")
    if scope not in {"local", "central"}:
        raise ClosureStateError("material root-cause field scope must be local or central")
    material = (
        issue["observed"]
        and issue["locatable"]
        and affects_present
        and not issue["style_only"]
        and not issue["hold_only"]
        and not issue["verification_only"]
        and issue["expected_benefit_exceeds_risk"]
    )
    return material, material and scope == "central"


def _contains_explicit_edit_command(value: str) -> bool:
    if any(pattern.search(value) for pattern in EN_IMPLEMENTATION_PATTERNS + ZH_IMPLEMENTATION_PATTERNS):
        return True
    if any(pattern.search(value) for pattern in EN_EXPLICIT_EDIT_COMMAND_PATTERNS):
        return True
    return bool(ZH_EXPLICIT_EDIT_COMMAND_PATTERN.search(value))


def _contains_directional_leakage(value: str) -> bool:
    lowered = value.casefold()
    if any(term in lowered for term in NON_HOLD_DETAIL_TERMS):
        return True
    if _contains_explicit_edit_command(value):
        return True
    return any(
        pattern.search(value)
        for pattern in (
            EN_LEAKAGE_PATTERNS
            + ZH_LEAKAGE_PATTERNS
            + EN_IMPLEMENTATION_PATTERNS
            + ZH_IMPLEMENTATION_PATTERNS
        )
    )


LITE_CLAUSE_SEPARATOR_RE = re.compile(r"[;；\n.!?。！？:：,，、—–]")
LITE_BOUNDARY_CHARS = frozenset(
    " \t\n\"'“”‘’`´「」『』《》〈〉([{)]}（ ）【】〔〕［］｛｝<>"
    "•◦‣▪▫⁃·*+->#"
)
LITE_NUMBERED_PREFIX_RE = re.compile(
    r"^(?:\(?[0-9一二三四五六七八九十百千万]+\)?\s*[.)、）]\s*)"
)


def _strip_lite_clause_boundary(value: str) -> str:
    """Remove only wrapper punctuation and bullet markers at clause edges."""

    cleaned = value.strip()
    while cleaned:
        numbered = LITE_NUMBERED_PREFIX_RE.sub("", cleaned, count=1)
        if numbered != cleaned:
            cleaned = numbered.strip()
            continue
        stripped = cleaned.lstrip("".join(LITE_BOUNDARY_CHARS)).rstrip(
            "".join(LITE_BOUNDARY_CHARS)
        )
        if stripped == cleaned:
            break
        cleaned = stripped.strip()
    return cleaned


def _iter_lite_clauses(value: str):
    """Yield normalized Lite clauses without changing their words."""

    if not isinstance(value, str):
        raise ClosureStateError("Lite suggestion text must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    for raw_clause in LITE_CLAUSE_SEPARATOR_RE.split(normalized):
        clause = _strip_lite_clause_boundary(raw_clause)
        if clause:
            yield clause


def _contains_lite_leakage(value: str) -> bool:
    if _contains_directional_leakage(value):
        return True
    return any(_contains_directional_leakage(clause) for clause in _iter_lite_clauses(value))


def _suggestions(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ClosureStateError("lite_suggestions must be a list")
    if len(raw) > 3:
        raise ClosureStateError("lite_suggestions must contain at most three items")
    result: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ClosureStateError("each lite suggestion must be an object")
        if set(item) != set(REQUIRED_SUGGESTION_FIELDS):
            raise ClosureStateError(
                "each lite suggestion must use only the three directional fields"
            )
        clean: dict[str, str] = {}
        for key in REQUIRED_SUGGESTION_FIELDS:
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ClosureStateError(f"lite suggestion field {key} must be non-empty text")
            if _contains_lite_leakage(value):
                raise ClosureStateError(
                    "lite suggestions must remain directional and location-free"
                )
            clean[key] = value.strip()
        result.append(clean)
    return result


def _output_language(state: Mapping[str, Any]) -> str:
    raw = state.get("output_language")
    if raw is None or raw == "":
        return "zh"
    if not isinstance(raw, str):
        raise ClosureStateError("output_language must be a string when supplied")
    value = raw.casefold().strip()
    if value in {"zh", "zh-cn", "zh-hans", "中文", "chinese"}:
        return "zh"
    if value in {"en", "en-us", "en-gb", "english"}:
        return "en"
    raise ClosureStateError("output_language must be zh or en when supplied")


def _conditional_tip(state: Mapping[str, Any]) -> str:
    language = _output_language(state)
    formal = _bool(state, "formal_tone")
    if language == "zh":
        return FORMAL_TIP_ZH if formal else TIP_ZH
    return FORMAL_TIP_EN if formal else TIP_EN


def _validate_concise_public_list(value: Any, field: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or len(item.strip()) > 240
        for item in value
    ):
        raise ClosureStateError(f"{field} must be a concise list of non-empty strings")
    if any(_contains_directional_leakage(item) for item in value):
        raise ClosureStateError(f"{field} cannot contain detailed implementation text")


def _validate_decision_mapping(decision: Any) -> dict[str, Any]:
    """Validate the exact compact decision contract before receipt emission."""

    if not isinstance(decision, Mapping):
        raise ClosureStateError("decision must be the canonical compact decision object")
    missing = DECISION_FIELDS.difference(decision)
    unknown = set(decision).difference(DECISION_FIELDS)
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append("missing: " + ", ".join(sorted(missing)))
        if unknown:
            parts.append("unknown: " + ", ".join(sorted(unknown)))
        raise ClosureStateError("decision must match the canonical compact schema (" + "; ".join(parts) + ")")

    verdict = decision["verdict"]
    if not isinstance(verdict, str) or verdict not in PUBLIC_VERDICTS:
        raise ClosureStateError("decision.verdict must be a public verdict")
    reason_category = decision["reason_category"]
    if not isinstance(reason_category, str) or reason_category not in DECISION_REASON_CATEGORIES:
        raise ClosureStateError("decision.reason_category must be a canonical category")
    if verdict == "UNASSESSED" and reason_category not in {
        "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS",
        "TECHNICAL_EXECUTION_HOLD",
        "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED",
    }:
        raise ClosureStateError("UNASSESSED decision has an incompatible reason category")
    if verdict == "ONE_BOUNDED_ROUND" and reason_category != "LOCAL_MATERIAL_ROOT_CAUSE":
        raise ClosureStateError("ONE_BOUNDED_ROUND decision has an incompatible reason category")
    if verdict == "REOPEN_SUBSTANTIVE_REVISION" and reason_category != "CENTRAL_MATERIAL_ROOT_CAUSE":
        raise ClosureStateError("REOPEN decision has an incompatible reason category")
    if verdict == "STOP_REVISING":
        if decision["prior_receipt_valid"]:
            if reason_category not in STOP_PRIOR_REASON_CATEGORIES:
                raise ClosureStateError("valid prior STOP decision has an incompatible reason category")
        elif reason_category != "NO_MATERIAL_ROOT_CAUSE":
            raise ClosureStateError("fresh STOP decision has an incompatible reason category")

    for field in (
        "prior_receipt_valid",
        "prior_receipt_stale",
        "prior_receipt_unverified",
        "material_root_cause",
        "central_root_cause",
        "show_revision_tip",
    ):
        if not isinstance(decision[field], bool):
            raise ClosureStateError(f"decision.{field} must be boolean")
    if decision["prior_receipt_stale"] and decision["prior_receipt_unverified"]:
        raise ClosureStateError("decision prior receipt cannot be both stale and unverified")
    if decision["prior_receipt_valid"]:
        if verdict != "STOP_REVISING" or decision["prior_receipt_stale"] or decision["prior_receipt_unverified"]:
            raise ClosureStateError("only a stable prior STOP may be marked valid")
        if decision["next_permitted_action"] != NEXT_PRIOR_STOP:
            raise ClosureStateError("valid prior STOP must use the prior-closure route action")

    if verdict in {"STOP_REVISING", "UNASSESSED"}:
        if decision["material_root_cause"] or decision["central_root_cause"]:
            raise ClosureStateError(f"{verdict} cannot fabricate root-cause flags")
    elif verdict == "ONE_BOUNDED_ROUND":
        if not decision["material_root_cause"] or decision["central_root_cause"]:
            raise ClosureStateError("ONE_BOUNDED_ROUND requires local materiality only")
    elif not (decision["material_root_cause"] and decision["central_root_cause"]):
        raise ClosureStateError("REOPEN requires material and central root causes")

    _validate_hold_codes(
        decision["evidence_hold_codes"], "decision.evidence_hold_codes", EVIDENCE_HOLD_CODES
    )
    _validate_hold_codes(
        decision["submission_hold_codes"],
        "decision.submission_hold_codes",
        SUBMISSION_HOLD_CODES,
    )
    _validate_concise_public_list(decision["protected"], "decision.protected")
    if not isinstance(decision["lite_suggestions"], list):
        raise ClosureStateError("decision.lite_suggestions must be a list")
    suggestions = _suggestions(decision["lite_suggestions"])
    if verdict in {"STOP_REVISING", "UNASSESSED"} and suggestions:
        raise ClosureStateError(f"{verdict} cannot contain Lite suggestions")
    if not isinstance(decision["next_permitted_action"], str) or decision["next_permitted_action"] not in _allowed_next_actions(verdict):
        raise ClosureStateError("decision.next_permitted_action is not an approved route boundary")
    if decision["show_revision_tip"] != (verdict in REVISION_VERDICTS):
        raise ClosureStateError("decision.show_revision_tip is incompatible with its verdict")
    return dict(decision)


def decide_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact internal decision and no detailed manuscript review."""

    if not isinstance(state, Mapping):
        raise ClosureStateError("state must be an object")
    _normalise_events(state)
    evidence_hold_codes, submission_hold_codes = _normalise_state_holds(state)
    identity_clear = _bool(state, "current_identity_clear")
    current_identity = state.get("current_manuscript_identity")
    if identity_clear and (
        not isinstance(current_identity, str) or not current_identity.strip()
    ):
        raise ClosureStateError(
            "current_identity_clear=true requires a non-empty current_manuscript_identity"
        )
    _bool(state, "artifact_only_drift_verified")
    _hash_value(state.get("current_artifact_sha256"), "current_artifact_sha256")
    _hash_value(
        state.get("current_semantic_content_sha256"),
        "current_semantic_content_sha256",
    )
    receipt_assessment = _receipt_assessment(state)
    prior_stale = bool(receipt_assessment["stale"])
    technical_hold = _bool(state, "technical_execution_hold")
    if technical_hold:
        failed_stage = state.get("technical_failed_stage")
        if not isinstance(failed_stage, str) or failed_stage not in TECHNICAL_FAILED_STAGES:
            raise ClosureStateError("technical execution hold requires one registered failed stage")
        return {
            "verdict": "UNASSESSED",
            "reason_category": "TECHNICAL_EXECUTION_HOLD",
            "prior_receipt_valid": False,
            "prior_receipt_stale": prior_stale,
            "prior_receipt_unverified": bool(receipt_assessment["unverified"]),
            "material_root_cause": False,
            "central_root_cause": False,
            "evidence_hold_codes": evidence_hold_codes,
            "submission_hold_codes": submission_hold_codes,
            "protected": _dedupe(_strings(state, "protected")),
            "lite_suggestions": [],
            "next_permitted_action": NEXT_TECHNICAL_HOLD,
            "show_revision_tip": False,
        }
    if receipt_assessment["valid"]:
        receipt = _validate_receipt(state["prior_receipt"])
        verdict = receipt["verdict"]
        return {
            "verdict": verdict,
            "reason_category": receipt_assessment["category"],
            "prior_receipt_valid": True,
            "prior_receipt_stale": False,
            "prior_receipt_unverified": False,
            "material_root_cause": False,
            "central_root_cause": False,
            "evidence_hold_codes": evidence_hold_codes,
            "submission_hold_codes": submission_hold_codes,
            "protected": _dedupe(_strings(state, "protected")),
            "lite_suggestions": [],
            "next_permitted_action": NEXT_PRIOR_STOP,
            "show_revision_tip": False,
        }

    if "provider_transmission_authorized" in state and not _bool(
        state, "provider_transmission_authorized"
    ):
        return {
            "verdict": "UNASSESSED",
            "reason_category": "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED",
            "prior_receipt_valid": False,
            "prior_receipt_stale": prior_stale,
            "prior_receipt_unverified": bool(receipt_assessment["unverified"]),
            "material_root_cause": False,
            "central_root_cause": False,
            "evidence_hold_codes": evidence_hold_codes,
            "submission_hold_codes": submission_hold_codes,
            "protected": _dedupe(_strings(state, "protected")),
            "lite_suggestions": [],
            "next_permitted_action": NEXT_PROVIDER_CONSENT,
            "show_revision_tip": False,
        }

    completeness_fields = (
        "manuscript_complete",
        "current_identity_clear",
        "whole_manuscript_read",
        "critical_basis_available",
    )
    complete = all(_bool(state, key) for key in completeness_fields)
    bounded_scope = _bool(state, "bounded_scope")
    if not complete or bounded_scope:
        return {
            "verdict": "UNASSESSED",
            "reason_category": "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS",
            "prior_receipt_valid": False,
            "prior_receipt_stale": prior_stale,
            "prior_receipt_unverified": bool(receipt_assessment["unverified"]),
            "material_root_cause": False,
            "central_root_cause": False,
            "evidence_hold_codes": evidence_hold_codes,
            "submission_hold_codes": submission_hold_codes,
            "protected": _dedupe(_strings(state, "protected")),
            "lite_suggestions": [],
            "next_permitted_action": NEXT_UNASSESSED,
            "show_revision_tip": False,
        }

    raw_issues = state.get("material_root_causes", [])
    if not isinstance(raw_issues, list):
        raise ClosureStateError("material_root_causes must be a list")
    material_count = 0
    central_count = 0
    for issue in raw_issues:
        if not isinstance(issue, Mapping):
            raise ClosureStateError("each material root-cause candidate must be an object")
        material, central = _material_issue(issue)
        if material:
            material_count += 1
            central_count += int(central)

    if central_count:
        verdict = "REOPEN_SUBSTANTIVE_REVISION"
        reason_category = "CENTRAL_MATERIAL_ROOT_CAUSE"
        next_action = NEXT_REOPEN
    elif material_count:
        verdict = "ONE_BOUNDED_ROUND"
        reason_category = "LOCAL_MATERIAL_ROOT_CAUSE"
        next_action = NEXT_ONE_ROUND
    else:
        if not _bool(state, "affirmative_stop_gate_passed"):
            raise ClosureStateError(
                "STOP_REVISING requires a completed two-stage affirmative sufficiency gate"
            )
        verdict = "STOP_REVISING"
        reason_category = "NO_MATERIAL_ROOT_CAUSE"
        next_action = NEXT_STOP

    if verdict == "ONE_BOUNDED_ROUND" and not material_count:
        raise ClosureStateError("ONE_BOUNDED_ROUND requires a material root cause")
    if verdict == "REOPEN_SUBSTANTIVE_REVISION" and not (material_count and central_count):
        raise ClosureStateError(
            "REOPEN_SUBSTANTIVE_REVISION requires material and central root causes"
        )

    if _bool(state, "rewrite_requested"):
        next_action += NEXT_REWRITE_SUFFIX

    return {
        "verdict": verdict,
        "reason_category": reason_category,
        "prior_receipt_valid": False,
        "prior_receipt_stale": prior_stale,
        "prior_receipt_unverified": bool(receipt_assessment["unverified"]),
        "material_root_cause": bool(material_count),
        "central_root_cause": bool(central_count),
        "evidence_hold_codes": evidence_hold_codes,
        "submission_hold_codes": submission_hold_codes,
        "protected": _dedupe(_strings(state, "protected")),
        "lite_suggestions": _suggestions(state.get("lite_suggestions", []))
        if verdict in REVISION_VERDICTS
        else [],
        "next_permitted_action": next_action,
        "show_revision_tip": verdict in REVISION_VERDICTS,
    }


def validate_public_card(card: Mapping[str, Any]) -> None:
    """Validate the exact public schema and verdict-specific invariants."""

    if not isinstance(card, Mapping):
        raise ClosureStateError("public card must be an object")
    missing = COMMON_CARD_FIELDS.difference(card.keys())
    if missing:
        raise ClosureStateError("public card missing fields: " + ", ".join(sorted(missing)))
    verdict = card["Verdict"]
    if verdict not in PUBLIC_VERDICTS:
        raise ClosureStateError("public card uses an unknown verdict")
    allowed = COMMON_CARD_FIELDS
    if verdict == "STOP_REVISING":
        allowed |= STOP_CARD_OPTIONAL_FIELDS
    if verdict == "UNASSESSED":
        allowed |= TECHNICAL_CARD_OPTIONAL_FIELDS
    unknown = set(card).difference(allowed)
    if unknown:
        raise ClosureStateError("public card contains unknown fields: " + ", ".join(sorted(unknown)))

    reason = card["Reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ClosureStateError("Reason must be non-empty text")
    if len(re.findall(r"[.!?。！？]+", reason)) > 2:
        raise ClosureStateError("Reason must contain at most two abstract sentences")
    if _contains_directional_leakage(reason):
        raise ClosureStateError("Reason must remain abstract and location-free")

    suggestions = card["Lite directional suggestions"]
    parsed_suggestions = _suggestions(suggestions)
    if verdict in {"STOP_REVISING", "UNASSESSED"} and parsed_suggestions:
        raise ClosureStateError(f"{verdict} cannot contain Lite directional suggestions")

    for field in ("Protected / Do not disturb",):
        value = card[field]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() or len(item.strip()) > 240
            for item in value
        ):
            raise ClosureStateError(f"{field} must be a concise list of non-empty strings")
        if any(_contains_directional_leakage(item) for item in value):
            raise ClosureStateError(f"{field} cannot contain detailed implementation text")

    _validate_public_hold_labels(
        card["Evidence holds"], "Evidence holds", EVIDENCE_HOLD_CODES
    )
    _validate_public_hold_labels(
        card["Submission / external holds"],
        "Submission / external holds",
        SUBMISSION_HOLD_CODES,
    )

    next_action = card["Next permitted action"]
    if not isinstance(next_action, str) or not next_action.strip():
        raise ClosureStateError("Next permitted action must be non-empty text")
    if next_action not in _allowed_next_actions(verdict):
        raise ClosureStateError("Next permitted action is not an approved route boundary")
    tip = card["Conditional tip"]
    if verdict in REVISION_VERDICTS:
        if not isinstance(tip, str) or tip not in APPROVED_TIPS:
            raise ClosureStateError("revision-needed cards require an approved conditional tip")
    elif tip not in (None, ""):
        raise ClosureStateError(f"{verdict} cannot show a revision tip")

    has_parked = "Parked opportunities" in card
    has_note = "Parked opportunities note" in card
    if verdict != "STOP_REVISING" and (has_parked or has_note):
        raise ClosureStateError("Parked opportunities are STOP_REVISING-only")
    if verdict == "UNASSESSED" and (has_parked or has_note):
        raise ClosureStateError("UNASSESSED cannot contain parked opportunities")
    has_failed_stage = "Failed stage" in card
    has_technical_version = "Technical hold contract version" in card
    technical_reason = (
        card["Reason"]
        == "Local technical preflight passed, but the machine assessment did not form because a bounded technical execution stage failed."
    )
    if technical_reason:
        if not has_failed_stage or not has_technical_version:
            raise ClosureStateError("technical card requires failed stage and contract version")
        if card["Failed stage"] not in TECHNICAL_FAILED_STAGES:
            raise ClosureStateError("technical card has an invalid failed stage")
        if card["Technical hold contract version"] != TECHNICAL_HOLD_CONTRACT_VERSION:
            raise ClosureStateError("technical card has an invalid contract version")
    elif has_failed_stage or has_technical_version:
        raise ClosureStateError("technical card fields require a technical execution reason")
    if verdict == "STOP_REVISING":
        if has_note and not has_parked:
            raise ClosureStateError("parked note requires parked opportunities")
        parked = card.get("Parked opportunities", [])
        if not isinstance(parked, list) or len(parked) > 2 or any(
            not isinstance(item, str) or not item.strip() for item in parked
        ):
            raise ClosureStateError("STOP_REVISING may park at most two opportunities")
        if any(_contains_directional_leakage(item) for item in parked):
            raise ClosureStateError("parked opportunities must remain non-specific")
        if parked:
            note = card.get("Parked opportunities note")
            if note not in {STOP_PARKED_NOTE_EN, STOP_PARKED_NOTE_ZH}:
                raise ClosureStateError("parked opportunities require the non-reopening note")

    serialized = json.dumps(card, ensure_ascii=False).casefold()
    for term in NON_HOLD_DETAIL_TERMS:
        if term in serialized:
            raise ClosureStateError("public card contains prohibited detailed-review language")


def public_card(state: Mapping[str, Any]) -> dict[str, Any]:
    """Render only the public card from a compact classified state."""

    decision = decide_state(state)
    verdict = decision["verdict"]
    if decision["prior_receipt_valid"]:
        reason = "A valid prior closure decision exists for the same manuscript, and no legal invalidation event was supplied."
    elif decision["reason_category"] == "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED":
        reason = "Provider transmission was not authorized for this run; no manuscript text was sent and no manuscript or technical judgment was formed."
    elif decision["reason_category"] == "TECHNICAL_EXECUTION_HOLD":
        reason = "Local technical preflight passed, but the machine assessment did not form because a bounded technical execution stage failed."
    elif verdict == "UNASSESSED":
        reason_codes = state.get("whole_manuscript_basis_reason_codes", [])
        first_code = reason_codes[0] if isinstance(reason_codes, list) and reason_codes else None
        reason = BASIS_PUBLIC_REASONS_EN.get(
            first_code,
            "A reliable whole-manuscript cutoff cannot be made from the supplied current basis.",
        )
    elif verdict == "REOPEN_SUBSTANTIVE_REVISION":
        reason = "A central material root cause remains capable of affecting the manuscript's contribution, validity, or whole-paper coherence."
    elif verdict == "ONE_BOUNDED_ROUND":
        reason = "A local material problem remains, but its expected repair benefit supports one strictly bounded round."
    else:
        reason = "No observed material root cause justifies reopening substantive revision; remaining holds are separate from the revision cutoff."

    language = _output_language(state)
    card: dict[str, Any] = {
        "Verdict": verdict,
        "Reason": reason,
        "Lite directional suggestions": decision["lite_suggestions"],
        "Protected / Do not disturb": decision["protected"],
        "Evidence holds": _render_hold_labels(
            decision["evidence_hold_codes"], language, EVIDENCE_HOLD_CODES
        ),
        "Submission / external holds": _render_hold_labels(
            decision["submission_hold_codes"], language, SUBMISSION_HOLD_CODES
        ),
        "Next permitted action": decision["next_permitted_action"],
        "Conditional tip": _conditional_tip(state) if decision["show_revision_tip"] else None,
    }
    if decision["reason_category"] == "TECHNICAL_EXECUTION_HOLD":
        card["Failed stage"] = state["technical_failed_stage"]
        card["Technical hold contract version"] = TECHNICAL_HOLD_CONTRACT_VERSION
    if verdict == "STOP_REVISING":
        parked = _strings(state, "parked_opportunities")
        if parked:
            if len(parked) > 2:
                raise ClosureStateError("STOP_REVISING may park at most two opportunities")
            card["Parked opportunities"] = parked
            card["Parked opportunities note"] = (
                STOP_PARKED_NOTE_ZH if language == "zh" else STOP_PARKED_NOTE_EN
            )
    validate_public_card(card)
    return card


def minimal_receipt(
    decision: Mapping[str, Any],
    manuscript_identity: str,
    *,
    artifact_sha256: str | None = None,
    semantic_content_sha256: str | None = None,
    skill_version: str = SKILL_VERSION,
    failed_stage: str | None = None,
    basis_receipt: Mapping[str, Any] | None = None,
    consent_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an allowed minimal receipt without detailed assessment fields."""

    if not isinstance(manuscript_identity, str) or not manuscript_identity.strip():
        raise ClosureStateError("manuscript_identity must be non-empty")
    canonical_decision = _validate_decision_mapping(decision)
    if not isinstance(skill_version, str) or not skill_version.strip():
        raise ClosureStateError("skill_version must be non-empty text")
    artifact_sha256 = _hash_value(artifact_sha256, "artifact_sha256")
    semantic_content_sha256 = _hash_value(
        semantic_content_sha256,
        "semantic_content_sha256",
    )
    receipt: dict[str, Any] = {
        "manuscript_identity": manuscript_identity.strip(),
        "verdict": canonical_decision["verdict"],
        "reason_category": canonical_decision["reason_category"],
        "evidence_hold_codes": list(canonical_decision["evidence_hold_codes"]),
        "submission_hold_codes": list(canonical_decision["submission_hold_codes"]),
        "invalidation_conditions": sorted(LEGAL_INVALIDATION_EVENTS),
        "next_permitted_action": canonical_decision["next_permitted_action"],
        "skill_version": skill_version,
    }
    if canonical_decision["reason_category"] == "TECHNICAL_EXECUTION_HOLD":
        if not isinstance(failed_stage, str) or failed_stage not in TECHNICAL_FAILED_STAGES:
            raise ClosureStateError("technical minimal receipt requires one registered failed_stage")
        receipt["technical_hold_contract_version"] = TECHNICAL_HOLD_CONTRACT_VERSION
        receipt["failed_stage"] = failed_stage
    elif failed_stage is not None:
        raise ClosureStateError("failed_stage is only valid for a technical minimal receipt")
    if artifact_sha256:
        receipt["artifact_sha256"] = artifact_sha256
    if semantic_content_sha256:
        receipt["semantic_content_sha256"] = semantic_content_sha256
    if basis_receipt is not None:
        receipt.update(deepcopy(dict(basis_receipt)))
    if consent_receipt is not None:
        receipt["provider_transmission_consent"] = deepcopy(dict(consent_receipt))
    _validate_receipt(receipt)
    return receipt
