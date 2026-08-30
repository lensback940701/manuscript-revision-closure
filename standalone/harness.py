"""Deterministic intake, context, and cross-stage harness contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from scripts.closure_state import EVIDENCE_HOLD_CODES, SUBMISSION_HOLD_CODES


INTAKE_CONTRACT_VERSION = "mrc-local-technical-preflight-1.0"
TITLE_EVIDENCE_CONTRACT_VERSION = "mrc-format-advisory-1.0"
COVERAGE_CONTRACT_VERSION = "mrc-whole-manuscript-coverage-3.0"
MANUSCRIPT_BASIS_CONTRACT_VERSION = "mrc-semantic-manuscript-basis-1.0"
ADJUDICATION_CONTRACT_VERSION = "mrc-root-cause-adjudication-2.0"
CONTRADICTION_GATE_VERSION = "mrc-cross-stage-contradiction-gate-2.0"
SCHEMA_DELIVERY_CONTRACT_VERSION = "mrc-canonical-schema-delivery-3.0"
DYNAMIC_ADJUDICATION_SCHEMA_VERSION = "mrc-dynamic-adjudication-schema-3.0"
CANDIDATE_BINDING_CONTRACT_VERSION = "mrc-candidate-lower-bound-independent-additions-1.0"
CANDIDATE_EXACT_SET_CONTRACT_VERSION = CANDIDATE_BINDING_CONTRACT_VERSION
AFFIRMATIVE_STOP_CONTRACT_VERSION = "mrc-affirmative-stop-gate-1.0"
SCHEMA_DEFINITION_LINT_VERSION = "mrc-schema-definition-lint-1.0"
HEADING_NUMBERING_STYLE_REVIEW = "HEADING_NUMBERING_STYLE_REVIEW"
HEADING_NUMBERING_STYLE_REVIEW_ZH = (
    "当前稿件章节结构完整，但标题编号或层级形式可能需要统一。"
    "请依据目标期刊格式，选择统一编号或统一无编号样式。"
)
STRUCTURE_FORMAT_REVIEW = "STRUCTURE_FORMAT_REVIEW"
STRUCTURE_FORMAT_REVIEW_ZH = (
    "本地结构识别仅供格式复核参考，不影响全文发送或 revision-closure 路由。"
)
WHOLE_MANUSCRIPT_BASIS_STATES = frozenset({"SUFFICIENT", "INSUFFICIENT"})
BASIS_REASON_CODES = frozenset(
    {
        "SUFFICIENT_SUBSTANTIVE_WHOLE_MANUSCRIPT",
        "FRAGMENT_OR_EXCERPT_ONLY",
        "SUBSTANTIVE_MATERIAL_MISSING",
        "INSUFFICIENT_ANALYTIC_CONTENT",
        "UNREADABLE_OR_CORRUPTED_EXTRACT",
        "IDENTITY_OR_SCOPE_AMBIGUOUS",
        "OTHER_SUBSTANTIVE_BASIS_GAP",
    }
)
BASIS_SUFFICIENT_CODE = "SUFFICIENT_SUBSTANTIVE_WHOLE_MANUSCRIPT"

COVERAGE_DIMENSIONS = (
    "contribution",
    "whole_paper_argument",
    "theory_and_concepts",
    "methods_and_research_design",
    "evidence_and_analysis",
    "rivals_negative_findings_and_limitations",
    "section_roles_and_coherence",
    "claim_ceiling_and_scope_conditions",
    "evidence_status_and_provenance",
    "revision_vs_submission_boundary",
)
AFFIRMATIVE_STOP_DIMENSIONS = (
    "contribution",
    "whole_paper_argument",
    "theory_and_concepts",
    "methods_and_research_design",
    "evidence_and_analysis",
    "section_roles_and_coherence",
)
COVERAGE_STATUSES = frozenset(
    {"CLEAR", "NON_MATERIAL_CONCERN", "POTENTIAL_MATERIAL_ROOT_CAUSE", "UNASSESSED"}
)
SUFFICIENCY_REASON_CODES = frozenset(
    {
        "AFFIRMATIVE_MANUSCRIPT_SUPPORT",
        "SUFFICIENT_WITH_NON_MATERIAL_LIMITS",
        "UNRESOLVED_MATERIAL_CONCERN",
        "NOT_APPLICABLE",
        "UNASSESSED",
    }
)
ROOT_CAUSE_ORIGINS = frozenset({"COVERAGE_CANDIDATE", "INDEPENDENT_ADDITION"})
ROOT_CAUSE_DISPOSITION_CODES = frozenset(
    {
        "MATERIAL_CONCERN_CONFIRMED",
        "NOT_OBSERVED",
        "NOT_LOCATABLE",
        "STYLE_ONLY",
        "HOLD_ONLY",
        "VERIFICATION_ONLY",
        "BENEFIT_NOT_ABOVE_RISK",
        "EQUIVALENT_SUFFICIENCY_PRESENT",
    }
)
APPLICABILITY_STATES = frozenset({"APPLICABLE", "NOT_APPLICABLE"})
PROTECTED_INVARIANT_KEYS = frozenset(
    {
        "claim_ceiling_preserved",
        "evidence_status_distinctions_preserved",
        "rivals_and_negative_findings_preserved",
    }
)
DIMENSION_KEYS = frozenset(
    {
        "dimension",
        "applicability",
        "assessed",
        "status",
        "affirmative_sufficiency",
        "sufficiency_reason_code",
    }
)
AFFIRMATIVE_SUFFICIENCY_KEYS = frozenset(
    {
        "dimension",
        "assessed",
        "affirmative_sufficiency",
        "unresolved_material_concern",
        "sufficiency_reason_code",
    }
)


class HarnessContractError(ValueError):
    """Raised when a deterministic or model harness contract fails closed."""


class SchemaContractError(HarnessContractError):
    """Raised with a bounded path/key receipt for one canonical JSON schema failure."""

    def __init__(self, message: str, receipt: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.contract_receipt = dict(receipt)


class SchemaDefinitionError(HarnessContractError):
    """Raised before dispatch when one provider schema definition is invalid."""

    error_code = "SCHEMA_DEFINITION_INVALID"

    def __init__(self, message: str, receipt: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.contract_receipt = dict(receipt)


class CandidateSetContractError(HarnessContractError):
    """Raised when lower-bound candidates or independent additions violate binding."""

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        super().__init__(
            "adjudication must account for every coverage candidate and only grounded canonical additions"
        )
        self.contract_receipt = dict(receipt)


@dataclass(frozen=True, slots=True)
class IntakeReceipt:
    character_count: int
    title_present: bool
    abstract_present: bool
    conclusion_present: bool
    references_present: bool
    conclusion_before_references: bool
    heading_count: int
    complete_structure: bool
    local_preflight_passed: bool = False
    effective_text_present: bool = False
    format_advisory_only: bool = True
    routing_invariant: str = "FORMAT_CANNOT_BLOCK_COVERAGE"
    advisory_codes: tuple[str, ...] = ()
    advisories: tuple[dict[str, Any], ...] = ()
    major_heading_level: int | None = None
    contract_version: str = INTAKE_CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "character_count": self.character_count,
            "title_present": self.title_present,
            "abstract_present": self.abstract_present,
            "conclusion_present": self.conclusion_present,
            "references_present": self.references_present,
            "conclusion_before_references": self.conclusion_before_references,
            "heading_count": self.heading_count,
            "complete_structure": self.complete_structure,
            "local_preflight_passed": self.local_preflight_passed,
            "effective_text_present": self.effective_text_present,
            "format_advisory_only": self.format_advisory_only,
            "routing_invariant": self.routing_invariant,
            "advisory_codes": list(self.advisory_codes),
            "advisories": [deepcopy(item) for item in self.advisories],
            "major_heading_level": self.major_heading_level,
        }


@dataclass(frozen=True, slots=True)
class ContextBudgetReceipt:
    provider: str
    model: str
    context_limit_tokens: int
    estimated_input_tokens: int
    safety_margin_tokens: int
    requested_max_output_tokens: int
    passed: bool
    estimator: str = "mrc-conservative-mixed-script-token-estimator-1.0"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "context_limit_tokens": self.context_limit_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "requested_max_output_tokens": self.requested_max_output_tokens,
            "passed": self.passed,
            "estimator": self.estimator,
        }


_ABSTRACT_RE = re.compile(r"^(?:abstract|摘要)\s*[:：]?$", re.I)
_CONCLUSION_RE = re.compile(
    r"^(?:conclusions?|discussion\s+and\s+conclusions?|"
    r"concluding\s+discussion|结论|结语|讨论与结论|结论与讨论)\s*[:：]?$",
    re.I,
)
_REFERENCES_RE = re.compile(
    r"^(?:references|bibliography|works\s+cited|参考文献|参考资料)\s*[:：]?$",
    re.I,
)
_TITLE_SECTION_RE = re.compile(
    r"^(?:"
    r"abstract|摘要|keywords?|关键词|"
    r"introduction|引言|导论|"
    r"methods?|methodology|materials\s+and\s+methods|方法|研究方法|材料与方法|"
    r"results?|findings?|结果|研究结果|发现|"
    r"discussion|讨论|"
    r"conclusions?|discussion\s+and\s+conclusions?|concluding\s+discussion|"
    r"结论|结语|讨论与结论|结论与讨论|"
    r"references|bibliography|works\s+cited|参考文献|参考资料"
    r")(?:\s*[:：]\s*.*)?$",
    re.I,
)
_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_HEADING_PREFIX_RE = re.compile(
    r"^(?P<prefix>"
    r"S\s*(?P<snum>\d+(?:\.\d+)*)|"
    r"(?P<anum>\d+(?:\.\d+)*)|"
    r"第(?P<cnum>[一二三四五六七八九十百零〇两0-9]+)章"
    r")[\s.)、:：-]+(?P<label>.+)$",
    re.I,
)
_PLAIN_NUMBERED_HEADING_RE = re.compile(
    r"^(?:S\s*\d+(?:\.\d+)*|\d+(?:\.\d+)*|第[一二三四五六七八九十百零〇两0-9]+章)"
    r"[\s.)、:：-]+.+$",
    re.I,
)
_YAML_TOP_LEVEL_TITLE_RE = re.compile(r"^title[ \t]*:[ \t]*(.*)$", re.I)
_YAML_BLOCK_SCALAR_RE = re.compile(r"^[|>](?:[+-]?\d?|\d?[+-]?)$")
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _clean_heading(line: str) -> str:
    value = line.strip()
    atx = _ATX_HEADING_RE.fullmatch(value)
    if atx:
        value = atx.group(2).strip()
    return " ".join(value.split())


def _heading_surface(line: str) -> tuple[int | None, str, str | None, int | None]:
    """Parse raw ATX level before producing the normalized semantic surface."""

    raw = line.strip()
    atx = _ATX_HEADING_RE.fullmatch(raw)
    level = len(atx.group(1)) if atx else None
    surface = " ".join((atx.group(2) if atx else raw).strip().split())
    prefix = _HEADING_PREFIX_RE.fullmatch(surface)
    if not prefix:
        return level, surface, None, None
    label = " ".join(prefix.group("label").split())
    if prefix.group("snum"):
        return level, label, "S", int(prefix.group("snum").split(".")[0])
    if prefix.group("anum"):
        return level, label, "ARABIC", int(prefix.group("anum").split(".")[0])
    raw_chinese = prefix.group("cnum")
    if raw_chinese.isdigit():
        number = int(raw_chinese)
    else:
        number = _CHINESE_NUMBERS.get(raw_chinese)
    return level, label, "CHAPTER", number


def _is_semantic_heading(surface: str) -> bool:
    return bool(
        _ABSTRACT_RE.fullmatch(surface)
        or _CONCLUSION_RE.fullmatch(surface)
        or _REFERENCES_RE.fullmatch(surface)
    )


def title_evidence_contract() -> dict[str, Any]:
    return {
        "version": TITLE_EVIDENCE_CONTRACT_VERSION,
        "purpose": "best_effort_format_advisory_only",
        "may_block_coverage": False,
        "may_change_machine_verdict": False,
        "may_create_evidence_or_submission_holds": False,
        "unknown_parse_is_nonblocking": True,
    }


def _inline_yaml_title_present(value: str) -> bool:
    candidate = value.strip()
    if not candidate or candidate.startswith("#"):
        return False
    if _YAML_BLOCK_SCALAR_RE.fullmatch(candidate):
        return False
    if candidate.casefold() in {"null", "~"}:
        return False
    if candidate[0] in {'"', "'"}:
        if len(candidate) < 2 or candidate[-1] != candidate[0]:
            return False
        return bool(candidate[1:-1].strip())
    if candidate.startswith(("[", "{")):
        return False
    return True


def _block_yaml_title_present(lines: Sequence[str], title_index: int) -> bool:
    for line in lines[title_index + 1 :]:
        if not line.strip():
            continue
        if line[:1].isspace():
            content = line.strip()
            if content and not content.startswith("#"):
                return True
            continue
        break
    return False


def _front_matter_title_present(lines: Sequence[str]) -> bool:
    matches: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if line[:1].isspace():
            continue
        match = _YAML_TOP_LEVEL_TITLE_RE.fullmatch(line)
        if match:
            matches.append((index, match.group(1)))
    if len(matches) != 1:
        return False
    index, value = matches[0]
    candidate = value.strip()
    if _YAML_BLOCK_SCALAR_RE.fullmatch(candidate):
        return _block_yaml_title_present(lines, index)
    return _inline_yaml_title_present(value)


def _split_front_matter(raw_lines: Sequence[str]) -> tuple[int, bool]:
    start = 0
    while start < len(raw_lines) and not raw_lines[start].strip():
        start += 1
    if start >= len(raw_lines) or raw_lines[start].strip() != "---":
        return 0, False
    for end in range(start + 1, len(raw_lines)):
        if raw_lines[end].strip() in {"---", "..."}:
            return end + 1, _front_matter_title_present(raw_lines[start + 1 : end])
    return len(raw_lines), False


def _heading_advisory(
    headings: Sequence[tuple[int, int | None, str, str | None, int | None]],
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...], int | None]:
    if not headings:
        return (), (), None
    title_level = headings[0][1]
    atx_levels = [
        level
        for _index, level, _surface, _style, _number in headings[1:]
        if level is not None and (title_level is None or level > title_level)
    ]
    major_level = min(atx_levels) if atx_levels else None
    major_rows = []
    level_jump = False
    major_numbers_at_level = {
        number
        for _index, level, _surface, style, number in headings[1:]
        if level == major_level and style is not None and number is not None
    }
    for _index, level, surface, style, number in headings[1:]:
        if _ABSTRACT_RE.fullmatch(surface) or _REFERENCES_RE.fullmatch(surface):
            continue
        is_numbered = style is not None
        is_conclusion = bool(_CONCLUSION_RE.fullmatch(surface))
        if major_level is None:
            if is_numbered or is_conclusion:
                major_rows.append((style, number))
            continue
        if level == major_level:
            major_rows.append((style, number))
        elif is_conclusion or (
            is_numbered and number is not None and number not in major_numbers_at_level
        ):
            level_jump = True
    styles = [style for style, _number in major_rows]
    numbered_styles = {style for style in styles if style is not None}
    numbers = [number for style, number in major_rows if style is not None and number is not None]
    unnumbered = any(style is None for style in styles)
    mixed = len(numbered_styles) > 1 or (bool(numbered_styles) and unnumbered)
    discontinuous = len(numbers) > 1 and any(
        current != previous + 1 for previous, current in zip(numbers, numbers[1:])
    )
    all_unnumbered = bool(major_rows) and not numbered_styles
    needs_review = level_jump or mixed or discontinuous or all_unnumbered
    if not needs_review:
        return (), (), major_level
    advisory = {
        "code": HEADING_NUMBERING_STYLE_REVIEW,
        "severity": "LOW",
        "blocking": False,
        "message_zh": HEADING_NUMBERING_STYLE_REVIEW_ZH,
        "scope": "FORMAT_ONLY",
    }
    return (HEADING_NUMBERING_STYLE_REVIEW,), (advisory,), major_level


def analyze_intake_structure(text: str, *, minimum_characters: int = 1000) -> IntakeReceipt:
    raw_lines = text.splitlines()
    if raw_lines:
        raw_lines[0] = raw_lines[0].lstrip("\ufeff")
    body_start, front_matter_title_present = _split_front_matter(raw_lines)
    lines = [_clean_heading(line) for line in raw_lines]
    content_lines = [
        (index, line)
        for index, line in enumerate(lines)
        if index >= body_start and line and not (line.startswith("---") and line.endswith("---"))
    ]
    parsed = {
        index: _heading_surface(raw_lines[index])
        for index, _line in content_lines
    }
    body_title_present = bool(
        content_lines
        and 2 <= len(content_lines[0][1]) <= 300
        and not _TITLE_SECTION_RE.fullmatch(parsed[content_lines[0][0]][1])
    )
    title_present = front_matter_title_present or body_title_present
    abstract_positions = [
        index for index, _line in content_lines if _ABSTRACT_RE.fullmatch(parsed[index][1])
    ]
    conclusion_positions = [
        index for index, _line in content_lines if _CONCLUSION_RE.fullmatch(parsed[index][1])
    ]
    reference_positions = [
        index for index, _line in content_lines if _REFERENCES_RE.fullmatch(parsed[index][1])
    ]
    abstract_present = bool(abstract_positions)
    conclusion_present = bool(conclusion_positions)
    references_present = bool(reference_positions)
    conclusion_before_references = bool(
        conclusion_positions and reference_positions and min(conclusion_positions) < max(reference_positions)
    )
    headings: list[tuple[int, int | None, str, str | None, int | None]] = []
    for index, line in content_lines:
        level, surface, style, number = parsed[index]
        if len(line) <= 140 and (
            level is not None
            or _PLAIN_NUMBERED_HEADING_RE.fullmatch(line)
            or _is_semantic_heading(surface)
        ):
            headings.append((index, level, surface, style, number))
    heading_count = len(headings)
    del minimum_characters  # retained only for call compatibility; semantic sufficiency is model-assessed
    effective_text_present = bool(text.strip())
    numbering_codes, numbering_advisories, major_heading_level = (
        _heading_advisory(headings) if effective_text_present else ((), (), None)
    )
    structural_uncertainty = not all(
        (
            title_present,
            abstract_present,
            conclusion_present,
            references_present,
            conclusion_before_references,
        )
    )
    advisory_codes = list(numbering_codes)
    advisories = list(numbering_advisories)
    if effective_text_present and structural_uncertainty:
        advisory_codes.append(STRUCTURE_FORMAT_REVIEW)
        advisories.append(
            {
                "code": STRUCTURE_FORMAT_REVIEW,
                "severity": "LOW",
                "blocking": False,
                "message_zh": STRUCTURE_FORMAT_REVIEW_ZH,
                "scope": "FORMAT_ONLY",
            }
        )
    return IntakeReceipt(
        character_count=len(text),
        title_present=title_present,
        abstract_present=abstract_present,
        conclusion_present=conclusion_present,
        references_present=references_present,
        conclusion_before_references=conclusion_before_references,
        heading_count=heading_count,
        complete_structure=effective_text_present,
        local_preflight_passed=effective_text_present,
        effective_text_present=effective_text_present,
        format_advisory_only=True,
        advisory_codes=tuple(dict.fromkeys(advisory_codes)),
        advisories=tuple(advisories),
        major_heading_level=major_heading_level,
    )


def provider_context_limit(provider: str, model: str) -> int:
    name = provider.casefold().strip()
    model_id = model.casefold().strip()
    if name == "deepseek":
        return 1_048_576
    if name == "kimi":
        if model_id == "kimi-k3":
            return 1_048_576
        if model_id.startswith(("kimi-k2.5", "kimi-k2.6", "kimi-k2.7")):
            return 262_144
        return 131_072
    if name == "gemini":
        return 1_048_576
    raise HarnessContractError("context budget requires one registered provider")


def provider_output_ceiling(provider: str) -> int:
    return {"deepseek": 393_216, "kimi": 131_072, "gemini": 65_536}[provider]


def estimate_message_tokens(messages: Sequence[Mapping[str, str]]) -> int:
    text = "\n".join(str(message.get("content", "")) for message in messages)
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    ascii_visible = len(re.findall(r"[\x21-\x7e]", text))
    other = max(0, len(text) - cjk - ascii_visible)
    estimate = cjk + ascii_visible / 3.0 + other / 2.0
    return int(math.ceil(estimate * 1.30)) + 2048


def context_budget(
    messages: Sequence[Mapping[str, str]],
    *,
    provider: str,
    model: str,
    minimum_output_tokens: int = 8192,
) -> ContextBudgetReceipt:
    context_limit = provider_context_limit(provider, model)
    estimated_input = estimate_message_tokens(messages)
    safety_margin = max(4096, int(context_limit * 0.03))
    remaining = context_limit - estimated_input - safety_margin
    requested = min(provider_output_ceiling(provider), max(0, remaining))
    return ContextBudgetReceipt(
        provider=provider,
        model=model,
        context_limit_tokens=context_limit,
        estimated_input_tokens=estimated_input,
        safety_margin_tokens=safety_margin,
        requested_max_output_tokens=requested,
        passed=requested >= minimum_output_tokens,
    )


COVERAGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "coverage_contract_version",
        "whole_manuscript_basis",
        "basis_reason_codes",
        "basis_explanation",
        "manuscript_identity_confirmed",
        "full_span_covered",
        "dimensions",
        "root_cause_candidate_dimensions",
        "evidence_hold_codes",
        "submission_hold_codes",
        "protected_invariants",
    ],
    "properties": {
        "coverage_contract_version": {"type": "string", "enum": [COVERAGE_CONTRACT_VERSION]},
        "whole_manuscript_basis": {
            "type": "string",
            "enum": sorted(WHOLE_MANUSCRIPT_BASIS_STATES),
        },
        "basis_reason_codes": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "enum": sorted(BASIS_REASON_CODES)},
        },
        "basis_explanation": {"type": "string", "minLength": 1, "maxLength": 240},
        "manuscript_identity_confirmed": {"type": "boolean"},
        "full_span_covered": {"type": "boolean"},
        "dimensions": {
            "type": "array",
            "minItems": len(COVERAGE_DIMENSIONS),
            "maxItems": len(COVERAGE_DIMENSIONS),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(DIMENSION_KEYS),
                "properties": {
                    "dimension": {"type": "string", "enum": list(COVERAGE_DIMENSIONS)},
                    "applicability": {"type": "string", "enum": sorted(APPLICABILITY_STATES)},
                    "assessed": {"type": "boolean"},
                    "status": {"type": "string", "enum": sorted(COVERAGE_STATUSES)},
                    "affirmative_sufficiency": {"type": "boolean"},
                    "sufficiency_reason_code": {
                        "type": "string",
                        "enum": sorted(SUFFICIENCY_REASON_CODES),
                    },
                },
            },
        },
        "root_cause_candidate_dimensions": {
            "type": "array",
            "items": {"type": "string", "enum": list(COVERAGE_DIMENSIONS)},
        },
        "evidence_hold_codes": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(EVIDENCE_HOLD_CODES)},
        },
        "submission_hold_codes": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(SUBMISSION_HOLD_CODES)},
        },
        "protected_invariants": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(PROTECTED_INVARIANT_KEYS),
            "properties": {key: {"type": "boolean"} for key in sorted(PROTECTED_INVARIANT_KEYS)},
        },
    },
}


ADJUDICATION_REQUIRED_KEYS = [
    "coverage_digest_sha256",
    "material_root_causes",
    "affirmative_sufficiency",
    "evidence_hold_codes",
    "submission_hold_codes",
    "protected",
    "parked_opportunities",
    "lite_suggestions",
]


_ADJUDICATION_SCHEMA_TEMPLATE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ADJUDICATION_REQUIRED_KEYS,
    "properties": {
        "coverage_digest_sha256": {"type": "string"},
        "material_root_causes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "observed",
                    "locatable",
                    "dimension",
                    "origin",
                    "coverage_disagreement",
                    "disposition_reason_code",
                    "author_decision_required",
                    "style_only",
                    "hold_only",
                    "verification_only",
                    "expected_benefit_exceeds_risk",
                    "scope",
                ],
                "properties": {
                    "observed": {"type": "boolean"},
                    "locatable": {"type": "boolean"},
                    "dimension": {"type": "string", "enum": list(COVERAGE_DIMENSIONS)},
                    "origin": {"type": "string", "enum": sorted(ROOT_CAUSE_ORIGINS)},
                    "coverage_disagreement": {"type": "boolean"},
                    "disposition_reason_code": {
                        "type": "string",
                        "enum": sorted(ROOT_CAUSE_DISPOSITION_CODES),
                    },
                    "author_decision_required": {"type": "boolean"},
                    "style_only": {"type": "boolean"},
                    "hold_only": {"type": "boolean"},
                    "verification_only": {"type": "boolean"},
                    "expected_benefit_exceeds_risk": {"type": "boolean"},
                    "scope": {"type": "string", "enum": ["local", "central"]},
                },
            },
        },
        "affirmative_sufficiency": {
            "type": "array",
            "minItems": len(AFFIRMATIVE_STOP_DIMENSIONS),
            "maxItems": len(AFFIRMATIVE_STOP_DIMENSIONS),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(AFFIRMATIVE_SUFFICIENCY_KEYS),
                "properties": {
                    "dimension": {
                        "type": "string",
                        "enum": list(AFFIRMATIVE_STOP_DIMENSIONS),
                    },
                    "assessed": {"type": "boolean"},
                    "affirmative_sufficiency": {"type": "boolean"},
                    "unresolved_material_concern": {"type": "boolean"},
                    "sufficiency_reason_code": {
                        "type": "string",
                        "enum": sorted(SUFFICIENCY_REASON_CODES),
                    },
                },
            },
        },
        "evidence_hold_codes": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(EVIDENCE_HOLD_CODES)},
        },
        "submission_hold_codes": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(SUBMISSION_HOLD_CODES)},
        },
        "protected": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "maxLength": 240},
        },
        "parked_opportunities": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "maxLength": 240},
        },
        "lite_suggestions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["Direction", "Why it matters", "What to protect"],
                "properties": {
                    "Direction": {"type": "string", "maxLength": 240},
                    "Why it matters": {"type": "string", "maxLength": 240},
                    "What to protect": {"type": "string", "maxLength": 240},
                },
            },
        },
    },
}


def canonical_json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def schema_sha256(schema: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_text(schema).encode("utf-8")).hexdigest()


_SCHEMA_DEFINITION_KEYS_BY_TYPE = {
    "object": frozenset({"type", "additionalProperties", "required", "properties"}),
    "array": frozenset({"type", "items", "minItems", "maxItems"}),
    "string": frozenset({"type", "enum", "minLength", "maxLength"}),
    "boolean": frozenset({"type"}),
}


def _raise_schema_definition_error(
    schema: Mapping[str, Any],
    *,
    root_digest: str,
    path: str,
    error_kind: str,
    message: str,
) -> None:
    bounded_message = " ".join(message.split())[:240]
    raise SchemaDefinitionError(
        f"{path}: {bounded_message}",
        {
            "contract_version": SCHEMA_DEFINITION_LINT_VERSION,
            "error_code": "SCHEMA_DEFINITION_INVALID",
            "schema_sha256": root_digest,
            "failed_path": path,
            "error_kind": error_kind,
            "error_message": bounded_message,
            "request_dispatched": False,
        },
    )


def validate_schema_definition(
    schema: Mapping[str, Any],
    *,
    path: str = "$",
    root_schema_sha256: str | None = None,
) -> dict[str, Any]:
    """Lint the finite provider-compatible JSON-schema subset before dispatch."""

    if not isinstance(schema, Mapping):
        raise SchemaDefinitionError(
            f"{path}: schema node must be an object",
            {
                "contract_version": SCHEMA_DEFINITION_LINT_VERSION,
                "error_code": "SCHEMA_DEFINITION_INVALID",
                "schema_sha256": root_schema_sha256,
                "failed_path": path,
                "error_kind": "schema_node_type",
                "error_message": "schema node must be an object",
                "request_dispatched": False,
            },
        )
    root_digest = root_schema_sha256 or schema_sha256(schema)
    schema_type = schema.get("type")
    allowed_keys = _SCHEMA_DEFINITION_KEYS_BY_TYPE.get(schema_type)
    if allowed_keys is None:
        _raise_schema_definition_error(
            schema,
            root_digest=root_digest,
            path=path + ".type",
            error_kind="unsupported_type",
            message="schema type must be object, array, string, or boolean",
        )
    unknown_keys = sorted(set(schema).difference(allowed_keys))
    if unknown_keys:
        _raise_schema_definition_error(
            schema,
            root_digest=root_digest,
            path=path,
            error_kind="unsupported_keyword",
            message="unsupported schema keywords: " + ", ".join(unknown_keys),
        )

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".enum",
                error_kind="empty_enum",
                message="enum must be a non-empty array",
            )
        canonical_values = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in enum]
        if len(set(canonical_values)) != len(canonical_values):
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".enum",
                error_kind="duplicate_enum",
                message="enum values must be unique",
            )
        if schema_type == "string" and any(not isinstance(item, str) for item in enum):
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".enum",
                error_kind="enum_type",
                message="string schema enum values must be strings",
            )

    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping):
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".properties",
                error_kind="properties_type",
                message="object properties must be an object",
            )
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".required",
                error_kind="required_type",
                message="required must be a string array",
            )
        if len(set(required)) != len(required):
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".required",
                error_kind="duplicate_required",
                message="required values must be unique",
            )
        missing_properties = sorted(set(required).difference(properties))
        unrequired_properties = sorted(set(properties).difference(required))
        if missing_properties or unrequired_properties:
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".required",
                error_kind="required_properties_mismatch",
                message=(
                    "required and properties must match exactly; missing="
                    + ",".join(missing_properties)
                    + "; unrequired="
                    + ",".join(unrequired_properties)
                ),
            )
        if schema.get("additionalProperties") is not False:
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".additionalProperties",
                error_kind="additional_properties",
                message="object schemas must set additionalProperties to false",
            )
        for key in sorted(properties):
            validate_schema_definition(
                properties[key],
                path=f"{path}.properties.{key}",
                root_schema_sha256=root_digest,
            )

    if schema_type == "array":
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        for field, value in (("minItems", minimum), ("maxItems", maximum)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                _raise_schema_definition_error(
                    schema,
                    root_digest=root_digest,
                    path=f"{path}.{field}",
                    error_kind="array_bound_type",
                    message=f"{field} must be a non-negative integer",
                )
        if minimum is not None and maximum is not None and minimum > maximum:
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path,
                error_kind="array_bounds_order",
                message="minItems cannot exceed maxItems",
            )
        items = schema.get("items")
        if not isinstance(items, Mapping):
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path + ".items",
                error_kind="items_type",
                message="array items must be one schema object",
            )
        validate_schema_definition(
            items,
            path=path + ".items",
            root_schema_sha256=root_digest,
        )

    if schema_type == "string":
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        for field, value in (("minLength", minimum), ("maxLength", maximum)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                _raise_schema_definition_error(
                    schema,
                    root_digest=root_digest,
                    path=f"{path}.{field}",
                    error_kind="string_bound_type",
                    message=f"{field} must be a non-negative integer",
                )
        if minimum is not None and maximum is not None and minimum > maximum:
            _raise_schema_definition_error(
                schema,
                root_digest=root_digest,
                path=path,
                error_kind="string_bounds_order",
                message="minLength cannot exceed maxLength",
            )

    return {
        "contract_version": SCHEMA_DEFINITION_LINT_VERSION,
        "schema_sha256": root_digest,
        "status": "PASS",
    }


def _candidate_ids(coverage: Mapping[str, Any]) -> list[str]:
    observed = coverage.get("root_cause_candidate_dimensions", [])
    if not isinstance(observed, list) or any(item not in COVERAGE_DIMENSIONS for item in observed):
        raise HarnessContractError("coverage root-cause candidate dimensions are invalid")
    if len(set(observed)) != len(observed):
        raise HarnessContractError("coverage root-cause candidate dimensions contain duplicates")
    order = {dimension: index for index, dimension in enumerate(COVERAGE_DIMENSIONS)}
    return sorted(observed, key=order.__getitem__)


def build_adjudication_json_schema(coverage: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the adjudication lower bound while permitting grounded canonical additions."""

    candidates = _candidate_ids(coverage)
    schema = deepcopy(_ADJUDICATION_SCHEMA_TEMPLATE)
    causes = schema["properties"]["material_root_causes"]
    causes["minItems"] = len(candidates)
    causes["maxItems"] = len(COVERAGE_DIMENSIONS)
    causes["items"]["properties"]["dimension"]["enum"] = list(COVERAGE_DIMENSIONS)
    validate_schema_definition(schema)
    return schema


def schema_definition_lint_contract() -> dict[str, Any]:
    return {
        "version": SCHEMA_DEFINITION_LINT_VERSION,
        "supported_types": sorted(_SCHEMA_DEFINITION_KEYS_BY_TYPE),
        "keywords_by_type": {
            key: sorted(value) for key, value in sorted(_SCHEMA_DEFINITION_KEYS_BY_TYPE.items())
        },
        "enum_nonempty": True,
        "enum_unique": True,
        "required_unique_and_exact_properties": True,
        "array_bounds_ordered": True,
        "failure_error_code": "SCHEMA_DEFINITION_INVALID",
        "failure_request_dispatched": False,
    }


def dynamic_adjudication_schema_contract() -> dict[str, Any]:
    zero = build_adjudication_json_schema({"root_cause_candidate_dimensions": []})
    one = build_adjudication_json_schema(
        {"root_cause_candidate_dimensions": [COVERAGE_DIMENSIONS[0]]}
    )
    many = build_adjudication_json_schema(
        {"root_cause_candidate_dimensions": list(COVERAGE_DIMENSIONS[:2])}
    )
    return {
        "version": DYNAMIC_ADJUDICATION_SCHEMA_VERSION,
        "zero_candidate_schema_sha256": schema_sha256(zero),
        "one_candidate_schema_sha256": schema_sha256(one),
        "many_candidate_schema_sha256": schema_sha256(many),
        "zero_candidate_cardinality": [0, len(COVERAGE_DIMENSIONS)],
        "zero_candidate_dimension_enum": list(COVERAGE_DIMENSIONS),
        "candidate_minimum_equals_coverage_candidate_count": True,
        "maximum_is_finite_canonical_dimension_count": True,
        "canonical_dimension_enum_allows_grounded_independent_additions": True,
    }


def schema_delivery_contract() -> dict[str, Any]:
    return {
        "version": SCHEMA_DELIVERY_CONTRACT_VERSION,
        "schema_definition_lint_version": SCHEMA_DEFINITION_LINT_VERSION,
        "lint_before_provider_dispatch": True,
        "strict_schema_delivery_retained": True,
        "deepseek_canonical_prompt_schema_retained": True,
    }


def candidate_binding_contract() -> dict[str, Any]:
    return {
        "version": CANDIDATE_BINDING_CONTRACT_VERSION,
        "coverage_candidates_are_required_lower_bound": True,
        "grounded_canonical_independent_additions_allowed": True,
        "unknown_duplicate_unlocatable_or_speculative_additions_rejected": True,
        "candidate_disposition_must_be_explained": True,
        "coverage_digest_binding_retained": True,
    }


def candidate_exact_set_contract() -> dict[str, Any]:
    """Compatibility alias returning the replacement candidate-binding contract."""

    return candidate_binding_contract()


def schema_delivery_block(schema: Mapping[str, Any], *, contract_version: str) -> str:
    canonical = canonical_json_text(schema)
    return (
        f"Canonical schema delivery contract: {SCHEMA_DELIVERY_CONTRACT_VERSION}\n"
        f"Stage contract version: {contract_version}\n"
        f"Canonical schema SHA-256: {schema_sha256(schema)}\n"
        f"Canonical JSON schema: {canonical}\n"
        "The schema is authoritative: preserve every required key, reject additional keys, use exact "
        "JSON types and enum values, and obey every array cardinality."
    )


def _schema_failure_receipt(
    schema: Mapping[str, Any],
    *,
    contract_version: str,
    path: str,
    observed: Any,
    error_kind: str,
    root_schema_sha256: str | None = None,
) -> dict[str, Any]:
    required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
    observed_keys = sorted(observed) if isinstance(observed, Mapping) else []
    return {
        "contract_version": contract_version,
        "schema_sha256": root_schema_sha256 or schema_sha256(schema),
        "required_keys": sorted(str(item) for item in required),
        "observed_keys": [str(item) for item in observed_keys],
        "missing_keys": sorted(str(item) for item in set(required).difference(observed_keys)),
        "extra_keys": sorted(str(item) for item in set(observed_keys).difference(required)),
        "failed_path": path,
        "error_kind": error_kind,
    }


def validate_json_schema_contract(
    value: Any,
    schema: Mapping[str, Any],
    *,
    contract_version: str,
    path: str = "$",
    root_schema_sha256: str | None = None,
) -> None:
    """Validate the finite JSON-schema subset used by the two model stages."""

    expected_type = schema.get("type")
    root_digest = root_schema_sha256 or schema_sha256(schema)
    if expected_type == "object":
        if not isinstance(value, Mapping):
            receipt = _schema_failure_receipt(
                schema, contract_version=contract_version, path=path, observed=value, error_kind="type",
                root_schema_sha256=root_digest,
            )
            raise SchemaContractError(f"{path} must be an object", receipt)
        required = set(schema.get("required", []))
        observed_keys = set(value)
        if observed_keys != required:
            receipt = _schema_failure_receipt(
                schema, contract_version=contract_version, path=path, observed=value, error_kind="key_set",
                root_schema_sha256=root_digest,
            )
            raise SchemaContractError(f"{path} key set mismatch", receipt)
        properties = schema.get("properties", {})
        for key in sorted(required):
            validate_json_schema_contract(
                value[key],
                properties[key],
                contract_version=contract_version,
                path=f"{path}.{key}",
                root_schema_sha256=root_digest,
            )
        return
    if expected_type == "array":
        if not isinstance(value, list):
            receipt = _schema_failure_receipt(
                schema, contract_version=contract_version, path=path, observed=value, error_kind="type",
                root_schema_sha256=root_digest,
            )
            raise SchemaContractError(f"{path} must be an array", receipt)
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if (isinstance(minimum, int) and len(value) < minimum) or (
            isinstance(maximum, int) and len(value) > maximum
        ):
            receipt = _schema_failure_receipt(
                schema, contract_version=contract_version, path=path, observed=value, error_kind="cardinality",
                root_schema_sha256=root_digest,
            )
            raise SchemaContractError(f"{path} has invalid cardinality", receipt)
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                validate_json_schema_contract(
                    item,
                    item_schema,
                    contract_version=contract_version,
                    path=f"{path}[{index}]",
                    root_schema_sha256=root_digest,
                )
        return
    if expected_type == "boolean" and not isinstance(value, bool):
        receipt = _schema_failure_receipt(
            schema, contract_version=contract_version, path=path, observed=value, error_kind="type",
            root_schema_sha256=root_digest,
        )
        raise SchemaContractError(f"{path} must be boolean", receipt)
    if expected_type == "string":
        if not isinstance(value, str):
            receipt = _schema_failure_receipt(
                schema, contract_version=contract_version, path=path, observed=value, error_kind="type",
                root_schema_sha256=root_digest,
            )
            raise SchemaContractError(f"{path} must be a string", receipt)
        allowed = schema.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            receipt = _schema_failure_receipt(
                schema, contract_version=contract_version, path=path, observed=value, error_kind="enum",
                root_schema_sha256=root_digest,
            )
            raise SchemaContractError(f"{path} enum mismatch", receipt)
        maximum_length = schema.get("maxLength")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            receipt = _schema_failure_receipt(
                schema, contract_version=contract_version, path=path, observed=value, error_kind="max_length",
                root_schema_sha256=root_digest,
            )
            raise SchemaContractError(f"{path} exceeds maximum length", receipt)


def _cause_dimension(cause: Mapping[str, Any]) -> str | None:
    if isinstance(cause.get("dimension"), str):
        return str(cause["dimension"])
    affects = cause.get("affects")
    if isinstance(affects, list) and len(affects) == 1 and isinstance(affects[0], str):
        return str(affects[0])
    return None


def _cause_is_material(cause: Mapping[str, Any]) -> bool:
    return bool(
        cause.get("observed") is True
        and cause.get("locatable") is True
        and cause.get("style_only") is False
        and cause.get("hold_only") is False
        and cause.get("verification_only") is False
        and cause.get("expected_benefit_exceeds_risk") is True
    )


def _cause_disposition_is_consistent(cause: Mapping[str, Any]) -> bool:
    code = cause.get("disposition_reason_code")
    if code not in ROOT_CAUSE_DISPOSITION_CODES:
        return False
    material = _cause_is_material(cause)
    if cause.get("author_decision_required") is True and not material:
        return False
    if material:
        return code == "MATERIAL_CONCERN_CONFIRMED"
    if code == "MATERIAL_CONCERN_CONFIRMED":
        return False
    return bool(
        (code == "NOT_OBSERVED" and cause.get("observed") is False)
        or (code == "NOT_LOCATABLE" and cause.get("locatable") is False)
        or (code == "STYLE_ONLY" and cause.get("style_only") is True)
        or (code == "HOLD_ONLY" and cause.get("hold_only") is True)
        or (code == "VERIFICATION_ONLY" and cause.get("verification_only") is True)
        or (
            code in {"BENEFIT_NOT_ABOVE_RISK", "EQUIVALENT_SUFFICIENCY_PRESENT"}
            and cause.get("expected_benefit_exceeds_risk") is False
        )
    )


def candidate_binding_receipt(
    coverage: Mapping[str, Any], model_state: Mapping[str, Any]
) -> dict[str, Any]:
    required = _candidate_ids(coverage)
    observed: list[str] = []
    unknown: list[str] = []
    invalid_origin: list[str] = []
    invalid_disposition: list[str] = []
    ungrounded_additions: list[str] = []
    for cause in model_state.get("material_root_causes", []):
        if not isinstance(cause, Mapping):
            continue
        dimension = _cause_dimension(cause)
        if dimension is None:
            invalid_disposition.append("<missing-dimension>")
            continue
        observed.append(dimension)
        if dimension not in COVERAGE_DIMENSIONS:
            unknown.append(dimension)
            continue
        is_required = dimension in required
        expected_origin = "COVERAGE_CANDIDATE" if is_required else "INDEPENDENT_ADDITION"
        expected_disagreement = not is_required
        if (
            cause.get("origin") != expected_origin
            or cause.get("coverage_disagreement") is not expected_disagreement
        ):
            invalid_origin.append(dimension)
        if not _cause_disposition_is_consistent(cause):
            invalid_disposition.append(dimension)
        if not is_required and not _cause_is_material(cause):
            ungrounded_additions.append(dimension)
    duplicates = sorted({item for item in observed if observed.count(item) > 1})
    required_set = set(required)
    observed_set = set(observed)
    return {
        "contract_version": CANDIDATE_BINDING_CONTRACT_VERSION,
        "required_candidates": required,
        "observed_candidates": sorted(observed),
        "missing_candidates": sorted(required_set.difference(observed_set)),
        "independent_additions": sorted(observed_set.difference(required_set)),
        "unknown_dimensions": sorted(set(unknown)),
        "duplicate_candidates": duplicates,
        "invalid_origin_or_disagreement": sorted(set(invalid_origin)),
        "invalid_disposition": sorted(set(invalid_disposition)),
        "ungrounded_additions": sorted(set(ungrounded_additions)),
    }


def validate_candidate_binding(
    coverage: Mapping[str, Any], model_state: Mapping[str, Any]
) -> dict[str, Any]:
    receipt = candidate_binding_receipt(coverage, model_state)
    failure_keys = (
        "missing_candidates",
        "unknown_dimensions",
        "duplicate_candidates",
        "invalid_origin_or_disagreement",
        "invalid_disposition",
        "ungrounded_additions",
    )
    if any(receipt[key] for key in failure_keys):
        raise CandidateSetContractError(receipt)
    return receipt


def candidate_exact_set_receipt(
    coverage: Mapping[str, Any], model_state: Mapping[str, Any]
) -> dict[str, Any]:
    """Compatibility alias for the replacement lower-bound binding receipt."""

    return candidate_binding_receipt(coverage, model_state)


def validate_candidate_exact_set(
    coverage: Mapping[str, Any], model_state: Mapping[str, Any]
) -> dict[str, Any]:
    """Compatibility alias; exact ceiling semantics were removed in 0.6.4."""

    return validate_candidate_binding(coverage, model_state)


def validate_coverage(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_json_schema_contract(
        value,
        COVERAGE_JSON_SCHEMA,
        contract_version=COVERAGE_CONTRACT_VERSION,
    )
    if value["coverage_contract_version"] != COVERAGE_CONTRACT_VERSION:
        raise HarnessContractError("coverage contract version mismatch")
    basis = value["whole_manuscript_basis"]
    reason_codes = value["basis_reason_codes"]
    explanation = sanitize_basis_explanation(value["basis_explanation"])
    if basis not in WHOLE_MANUSCRIPT_BASIS_STATES:
        raise HarnessContractError("coverage whole-manuscript basis state is invalid")
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or len(set(reason_codes)) != len(reason_codes)
        or any(code not in BASIS_REASON_CODES for code in reason_codes)
    ):
        raise HarnessContractError("coverage basis reason codes are invalid")
    if basis == "SUFFICIENT" and reason_codes != [BASIS_SUFFICIENT_CODE]:
        raise HarnessContractError("sufficient basis requires only the sufficient reason code")
    if basis == "INSUFFICIENT" and BASIS_SUFFICIENT_CODE in reason_codes:
        raise HarnessContractError("insufficient basis cannot use the sufficient reason code")
    for field in ("manuscript_identity_confirmed", "full_span_covered"):
        if not isinstance(value[field], bool):
            raise HarnessContractError(f"coverage {field} must be boolean")
    dimensions = value["dimensions"]
    if not isinstance(dimensions, list) or len(dimensions) != len(COVERAGE_DIMENSIONS):
        raise HarnessContractError("coverage dimensions must have the exact required cardinality")
    clean_dimensions: list[dict[str, Any]] = []
    observed_ids: list[str] = []
    for row in dimensions:
        if not isinstance(row, Mapping) or set(row) != DIMENSION_KEYS:
            raise HarnessContractError("coverage dimension row key set mismatch")
        dimension = row["dimension"]
        if dimension not in COVERAGE_DIMENSIONS:
            raise HarnessContractError("coverage contains an unknown dimension")
        if row["applicability"] not in APPLICABILITY_STATES:
            raise HarnessContractError("coverage applicability is invalid")
        if not isinstance(row["assessed"], bool) or row["status"] not in COVERAGE_STATUSES:
            raise HarnessContractError("coverage assessment state is invalid")
        if not isinstance(row["affirmative_sufficiency"], bool):
            raise HarnessContractError("coverage affirmative sufficiency must be boolean")
        reason = row["sufficiency_reason_code"]
        if reason not in SUFFICIENCY_REASON_CODES:
            raise HarnessContractError("coverage sufficiency reason code is invalid")
        if row["applicability"] == "NOT_APPLICABLE" and row["status"] not in {"CLEAR", "UNASSESSED"}:
            raise HarnessContractError("not-applicable coverage cannot claim a material concern")
        if row["applicability"] == "NOT_APPLICABLE" and (
            row["affirmative_sufficiency"] or reason != "NOT_APPLICABLE"
        ):
            raise HarnessContractError("not-applicable coverage cannot claim affirmative sufficiency")
        if dimension in AFFIRMATIVE_STOP_DIMENSIONS and row["applicability"] != "APPLICABLE":
            raise HarnessContractError("every affirmative STOP dimension must be applicable")
        if not row["assessed"] and row["status"] != "UNASSESSED":
            raise HarnessContractError("unassessed dimension must use UNASSESSED status")
        if row["assessed"] and row["status"] == "UNASSESSED":
            raise HarnessContractError("assessed dimension cannot use UNASSESSED status")
        if not row["assessed"] and (
            row["affirmative_sufficiency"] or reason != "UNASSESSED"
        ):
            raise HarnessContractError("unassessed coverage cannot claim affirmative sufficiency")
        if row["assessed"] and row["applicability"] == "APPLICABLE":
            if row["affirmative_sufficiency"] and reason not in {
                "AFFIRMATIVE_MANUSCRIPT_SUPPORT",
                "SUFFICIENT_WITH_NON_MATERIAL_LIMITS",
            }:
                raise HarnessContractError("affirmative coverage requires an affirmative reason code")
            if not row["affirmative_sufficiency"] and reason != "UNRESOLVED_MATERIAL_CONCERN":
                raise HarnessContractError("non-affirmative coverage requires an unresolved concern code")
            if row["status"] in {"CLEAR", "NON_MATERIAL_CONCERN"} and not row["affirmative_sufficiency"]:
                raise HarnessContractError("clear or non-material coverage must affirm sufficiency")
        observed_ids.append(dimension)
        clean_dimensions.append(dict(row))
    if set(observed_ids) != set(COVERAGE_DIMENSIONS) or len(set(observed_ids)) != len(observed_ids):
        raise HarnessContractError("coverage dimension set must match exactly without duplicates")
    candidate_dimensions = value["root_cause_candidate_dimensions"]
    if not isinstance(candidate_dimensions, list) or any(item not in COVERAGE_DIMENSIONS for item in candidate_dimensions):
        raise HarnessContractError("coverage root-cause candidate dimensions are invalid")
    if len(set(candidate_dimensions)) != len(candidate_dimensions):
        raise HarnessContractError("coverage root-cause candidate dimensions contain duplicates")
    expected_candidates = {
        row["dimension"] for row in clean_dimensions if row["status"] == "POTENTIAL_MATERIAL_ROOT_CAUSE"
    }
    if set(candidate_dimensions) != expected_candidates:
        raise HarnessContractError("coverage candidate list does not match dimension states")
    evidence = value["evidence_hold_codes"]
    submission = value["submission_hold_codes"]
    if not isinstance(evidence, list) or any(code not in EVIDENCE_HOLD_CODES for code in evidence):
        raise HarnessContractError("coverage evidence hold codes are invalid")
    if not isinstance(submission, list) or any(code not in SUBMISSION_HOLD_CODES for code in submission):
        raise HarnessContractError("coverage submission hold codes are invalid")
    invariants = value["protected_invariants"]
    if not isinstance(invariants, Mapping) or set(invariants) != PROTECTED_INVARIANT_KEYS:
        raise HarnessContractError("coverage protected invariant key set mismatch")
    if any(not isinstance(invariants[key], bool) for key in PROTECTED_INVARIANT_KEYS):
        raise HarnessContractError("coverage protected invariants must be boolean")
    if basis == "INSUFFICIENT":
        if value["full_span_covered"]:
            raise HarnessContractError("insufficient basis cannot claim full-span coverage")
        if candidate_dimensions or evidence or submission:
            raise HarnessContractError("insufficient basis cannot create candidates or hold codes")
        if any(
            row["assessed"]
            or row["status"] != "UNASSESSED"
            or row["affirmative_sufficiency"]
            or row["sufficiency_reason_code"] != "UNASSESSED"
            for row in clean_dimensions
        ):
            raise HarnessContractError("insufficient basis dimensions must remain unassessed")
        if any(invariants.values()):
            raise HarnessContractError("insufficient basis cannot claim protected invariants")
    return {
        "coverage_contract_version": COVERAGE_CONTRACT_VERSION,
        "whole_manuscript_basis": basis,
        "basis_reason_codes": list(reason_codes),
        "basis_explanation": explanation,
        "manuscript_identity_confirmed": value["manuscript_identity_confirmed"],
        "full_span_covered": value["full_span_covered"],
        "dimensions": clean_dimensions,
        "root_cause_candidate_dimensions": list(candidate_dimensions),
        "evidence_hold_codes": list(dict.fromkeys(evidence)),
        "submission_hold_codes": list(dict.fromkeys(submission)),
        "protected_invariants": dict(invariants),
    }


def coverage_is_complete(coverage: Mapping[str, Any]) -> bool:
    return bool(
        coverage["whole_manuscript_basis"] == "SUFFICIENT"
        and coverage["manuscript_identity_confirmed"]
        and coverage["full_span_covered"]
        and all(row["assessed"] and row["status"] != "UNASSESSED" for row in coverage["dimensions"])
    )


def sanitize_basis_explanation(value: Any) -> str:
    if not isinstance(value, str):
        raise HarnessContractError("coverage basis explanation must be text")
    candidate = " ".join(value.split())[:240]
    if not candidate:
        raise HarnessContractError("coverage basis explanation must be non-empty")
    suspicious = re.compile(
        r"(?:authorization\s*:|bearer\s+|api[_ -]?key|sk-[A-Za-z0-9]|"
        r"https?://|file://|[A-Za-z]:\\|(?:paragraph|section|line)\s+\d+)",
        re.I,
    )
    if suspicious.search(candidate):
        return "The supplied material does not provide a safe, sufficient whole-manuscript basis for revision closure."
    return candidate


def canonical_digest(value: Mapping[str, Any]) -> str:
    normalized: Mapping[str, Any] = value
    if value.get("coverage_contract_version") == COVERAGE_CONTRACT_VERSION:
        copy = deepcopy(dict(value))
        if isinstance(copy.get("root_cause_candidate_dimensions"), list):
            copy["root_cause_candidate_dimensions"] = _candidate_ids(copy)
        if isinstance(copy.get("dimensions"), list):
            order = {dimension: index for index, dimension in enumerate(COVERAGE_DIMENSIONS)}
            copy["dimensions"] = sorted(
                copy["dimensions"],
                key=lambda row: order.get(row.get("dimension"), len(order)) if isinstance(row, Mapping) else len(order),
            )
        for field in ("evidence_hold_codes", "submission_hold_codes"):
            if isinstance(copy.get(field), list):
                copy[field] = sorted(copy[field])
        normalized = copy
    payload = canonical_json_text(normalized)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


ADJUDICATION_BINDING_KEYS = frozenset({"coverage_digest_sha256"})


def validate_adjudication_binding(value: Mapping[str, Any], coverage: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HarnessContractError("adjudication output must be one object")
    expected_digest = canonical_digest(coverage)
    if value.get("coverage_digest_sha256") != expected_digest:
        raise HarnessContractError("adjudication coverage digest binding mismatch")
    return {key: value[key] for key in value if key not in ADJUDICATION_BINDING_KEYS}


def _validated_affirmative_sufficiency_rows(
    rows: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) != len(AFFIRMATIVE_STOP_DIMENSIONS):
        raise HarnessContractError("adjudication affirmative sufficiency has invalid cardinality")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != AFFIRMATIVE_SUFFICIENCY_KEYS:
            raise HarnessContractError("adjudication affirmative sufficiency row key set mismatch")
        dimension = row["dimension"]
        if dimension not in AFFIRMATIVE_STOP_DIMENSIONS or dimension in result:
            raise HarnessContractError("adjudication affirmative sufficiency dimension set mismatch")
        if any(
            not isinstance(row[field], bool)
            for field in ("assessed", "affirmative_sufficiency", "unresolved_material_concern")
        ):
            raise HarnessContractError("adjudication affirmative sufficiency flags must be boolean")
        reason = row["sufficiency_reason_code"]
        if reason not in SUFFICIENCY_REASON_CODES:
            raise HarnessContractError("adjudication sufficiency reason code is invalid")
        if not row["assessed"]:
            raise HarnessContractError("independent adjudication must assess every STOP dimension")
        if row["affirmative_sufficiency"]:
            if row["unresolved_material_concern"] or reason not in {
                "AFFIRMATIVE_MANUSCRIPT_SUPPORT",
                "SUFFICIENT_WITH_NON_MATERIAL_LIMITS",
            }:
                raise HarnessContractError("affirmative adjudication sufficiency is contradictory")
        elif not row["unresolved_material_concern"] or reason != "UNRESOLVED_MATERIAL_CONCERN":
            raise HarnessContractError("non-affirmative adjudication must expose an unresolved concern")
        result[str(dimension)] = dict(row)
    if set(result) != set(AFFIRMATIVE_STOP_DIMENSIONS):
        raise HarnessContractError("adjudication affirmative sufficiency dimension set mismatch")
    return result


def affirmative_stop_gate_receipt(
    coverage: Mapping[str, Any], model_state: Mapping[str, Any]
) -> dict[str, Any]:
    coverage_rows = {
        str(row["dimension"]): row
        for row in coverage.get("dimensions", [])
        if isinstance(row, Mapping) and row.get("dimension") in AFFIRMATIVE_STOP_DIMENSIONS
    }
    adjudication_rows = _validated_affirmative_sufficiency_rows(
        model_state.get("affirmative_sufficiency")
    )
    material_dimensions = sorted(
        dimension
        for cause in model_state.get("material_root_causes", [])
        if isinstance(cause, Mapping) and _cause_is_material(cause)
        for dimension in [_cause_dimension(cause)]
        if dimension is not None
    )
    coverage_not_affirmative = sorted(
        dimension
        for dimension in AFFIRMATIVE_STOP_DIMENSIONS
        if dimension not in coverage_rows
        or coverage_rows[dimension].get("assessed") is not True
        or coverage_rows[dimension].get("affirmative_sufficiency") is not True
    )
    adjudication_not_affirmative = sorted(
        dimension
        for dimension, row in adjudication_rows.items()
        if row["affirmative_sufficiency"] is not True
        or row["unresolved_material_concern"] is not False
    )
    return {
        "contract_version": AFFIRMATIVE_STOP_CONTRACT_VERSION,
        "required_dimensions": list(AFFIRMATIVE_STOP_DIMENSIONS),
        "coverage_not_affirmative": coverage_not_affirmative,
        "adjudication_not_affirmative": adjudication_not_affirmative,
        "material_dimensions": material_dimensions,
        "two_stage_complete": bool(
            coverage_is_complete(coverage)
            and len(adjudication_rows) == len(AFFIRMATIVE_STOP_DIMENSIONS)
        ),
        "stop_eligible": bool(
            coverage_is_complete(coverage)
            and not material_dimensions
            and not coverage_not_affirmative
            and not adjudication_not_affirmative
        ),
    }


def affirmative_stop_contract() -> dict[str, Any]:
    return {
        "version": AFFIRMATIVE_STOP_CONTRACT_VERSION,
        "required_dimensions": list(AFFIRMATIVE_STOP_DIMENSIONS),
        "coverage_and_adjudication_affirmative_required": True,
        "absence_of_root_causes_is_not_sufficient": True,
        "material_concern_requires_revision_verdict": True,
        "scope_or_evidence_caution_alone_is_not_a_material_cause": True,
    }


def validate_cross_stage_consistency(coverage: Mapping[str, Any], model_state: Mapping[str, Any]) -> None:
    candidates = list(coverage["root_cause_candidate_dimensions"])
    material_dimensions: set[str] = set()
    for cause in model_state["material_root_causes"]:
        affects = cause["affects"]
        if any(item not in COVERAGE_DIMENSIONS for item in affects):
            raise HarnessContractError("adjudication root cause references an unknown coverage dimension")
        dimension = _cause_dimension(cause)
        if dimension is not None and _cause_is_material(cause):
            material_dimensions.add(dimension)
    validate_candidate_binding(coverage, model_state)
    affirmative_rows = _validated_affirmative_sufficiency_rows(
        model_state.get("affirmative_sufficiency")
    )
    for dimension, row in affirmative_rows.items():
        has_material_cause = dimension in material_dimensions
        if has_material_cause and (
            row["affirmative_sufficiency"] or not row["unresolved_material_concern"]
        ):
            raise HarnessContractError("material cause contradicts affirmative sufficiency")
        if row["unresolved_material_concern"] and not has_material_cause:
            raise HarnessContractError("unresolved core concern lacks a material root-cause row")
    stop_gate = affirmative_stop_gate_receipt(coverage, model_state)
    if not material_dimensions and not stop_gate["stop_eligible"]:
        raise HarnessContractError("STOP requires two-stage affirmative sufficiency")
    if not set(coverage["evidence_hold_codes"]).issubset(model_state["evidence_hold_codes"]):
        raise HarnessContractError("adjudication silently dropped a coverage evidence hold")
    if not set(coverage["submission_hold_codes"]).issubset(model_state["submission_hold_codes"]):
        raise HarnessContractError("adjudication silently dropped a coverage submission hold")
    invariant_dimensions = {
        "claim_ceiling_preserved": "claim_ceiling_and_scope_conditions",
        "evidence_status_distinctions_preserved": "evidence_status_and_provenance",
        "rivals_and_negative_findings_preserved": "rivals_negative_findings_and_limitations",
    }
    for key, dimension in invariant_dimensions.items():
        if (
            not coverage["protected_invariants"][key]
            and dimension not in candidates
            and dimension not in material_dimensions
        ):
            raise HarnessContractError("coverage invariant failure lacks a root-cause candidate dimension")


def harness_receipt(
    intake: IntakeReceipt,
    budgets: Sequence[ContextBudgetReceipt],
    *,
    coverage: Mapping[str, Any] | None = None,
    adjudication_bound: bool = False,
    contradiction_gate_passed: bool = False,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "intake": intake.as_dict(),
        "context_budgets": [budget.as_dict() for budget in budgets],
        "coverage_contract_version": COVERAGE_CONTRACT_VERSION,
        "adjudication_contract_version": ADJUDICATION_CONTRACT_VERSION,
        "contradiction_gate_version": CONTRADICTION_GATE_VERSION,
        "schema_delivery_contract_version": SCHEMA_DELIVERY_CONTRACT_VERSION,
        "schema_delivery_contract_sha256": schema_sha256(schema_delivery_contract()),
        "dynamic_adjudication_schema_version": DYNAMIC_ADJUDICATION_SCHEMA_VERSION,
        "dynamic_adjudication_schema_contract_sha256": schema_sha256(
            dynamic_adjudication_schema_contract()
        ),
        "candidate_binding_contract_version": CANDIDATE_BINDING_CONTRACT_VERSION,
        "candidate_binding_contract_sha256": schema_sha256(candidate_binding_contract()),
        "affirmative_stop_contract_version": AFFIRMATIVE_STOP_CONTRACT_VERSION,
        "affirmative_stop_contract_sha256": schema_sha256(affirmative_stop_contract()),
        "schema_definition_lint_contract_version": SCHEMA_DEFINITION_LINT_VERSION,
        "schema_definition_lint_contract_sha256": schema_sha256(
            schema_definition_lint_contract()
        ),
        "manuscript_basis_contract_version": MANUSCRIPT_BASIS_CONTRACT_VERSION,
        "whole_manuscript_basis": None,
        "basis_reason_codes": [],
        "basis_explanation": None,
        "coverage_completed": False,
        "coverage_dimension_count": 0,
        "coverage_digest_sha256": None,
        "adjudication_coverage_binding": adjudication_bound,
        "contradiction_gate_passed": contradiction_gate_passed,
    }
    if coverage is not None:
        receipt.update(
            {
                "coverage_completed": coverage_is_complete(coverage),
                "coverage_dimension_count": len(coverage["dimensions"]),
                "coverage_digest_sha256": canonical_digest(coverage),
                "whole_manuscript_basis": coverage["whole_manuscript_basis"],
                "basis_reason_codes": list(coverage["basis_reason_codes"]),
                "basis_explanation": coverage["basis_explanation"],
            }
        )
    return receipt
