"""Native presentation transaction for Manuscript Revision Closure 0.6.4.

The machine adjudication remains authoritative.  This module receives only the
finite model-authored fields that are already eligible for the public Closure
Card, binds every item to an immutable identity, applies deterministic language
validation, and permits at most one presentation-only provider request.

It never receives the manuscript, coverage rows, root-cause booleans, verdict,
hidden diagnostics, prompts from earlier stages, chain-of-thought, or API keys.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .harness import (
    canonical_digest,
    estimate_message_tokens,
    provider_context_limit,
    provider_output_ceiling,
    schema_sha256,
)
from .providers import (
    ChatCompletionClient,
    CompletionResult,
    ProviderRequestError,
    load_provider_config,
    provider_stage_timeout_seconds,
)


PRESENTATION_TRANSACTION_VERSION = "mrc-presentation-transaction-1.0"
PRESENTATION_SOURCE_CONTRACT_VERSION = "mrc-presentation-source-2.0"
PRESENTATION_REPAIR_CONTRACT_VERSION = "mrc-presentation-repair-2.0"
MACHINE_STATE_CONTRACT_VERSION = "mrc-machine-state-2.0"
LANGUAGE_CONTRACT_VERSION = "mrc-zh-display-language-1.0"

_DISPLAY_FIELDS = ("Direction", "Why it matters", "What to protect")
_MACHINE_KEYS = (
    "manuscript_complete",
    "current_identity_clear",
    "whole_manuscript_read",
    "critical_basis_available",
    "bounded_scope",
    "current_manuscript_identity",
    "current_artifact_sha256",
    "current_semantic_content_sha256",
    "material_root_causes",
    "affirmative_sufficiency",
    "affirmative_stop_gate_passed",
    "evidence_hold_codes",
    "submission_hold_codes",
    "protected",
    "parked_opportunities",
    "lite_suggestions",
    "invalidation_events",
    "artifact_only_drift_verified",
    "formal_tone",
    "rewrite_requested",
)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+:/-]*")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_DOI_RE = re.compile(r"(?i)\b10\.\d{4,9}/\S+")
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")


class PresentationContractError(ValueError):
    """Raised for an invalid bounded presentation source or repair object."""


@dataclass(frozen=True, slots=True)
class PresentationItem:
    item_id: str
    path: str
    index: int
    source_text: str
    source_sha256: str

    def request_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "path": self.path,
            "index": self.index,
            "source_text": self.source_text,
            "source_sha256": self.source_sha256,
        }

    def authoritative_receipt(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "path": self.path,
            "index": self.index,
            "source_text": self.source_text,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class PresentationSource:
    items: tuple[PresentationItem, ...]
    source_binding_digest_sha256: str
    protected_binding_digest_sha256: str
    protected_cardinality: int

    def bounded_receipt(self) -> dict[str, Any]:
        protected = [item for item in self.items if item.path.startswith("protected[")]
        return {
            "contract_version": PRESENTATION_SOURCE_CONTRACT_VERSION,
            "source_binding_digest_sha256": self.source_binding_digest_sha256,
            "protected_binding_digest_sha256": self.protected_binding_digest_sha256,
            "item_count": len(self.items),
            "item_ids": [item.item_id for item in self.items],
            "protected_cardinality": self.protected_cardinality,
            "protected_item_ids": [item.item_id for item in protected],
            "authoritative_items": [item.authoritative_receipt() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class PresentationBudgetReceipt:
    provider: str
    model: str
    provider_output_ceiling_tokens: int
    context_limit_tokens: int
    estimated_input_tokens: int
    safety_margin_tokens: int
    schema_output_ceiling_tokens: int
    requested_max_output_tokens: int
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "provider_output_ceiling_tokens": self.provider_output_ceiling_tokens,
            "context_limit_tokens": self.context_limit_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "schema_output_ceiling_tokens": self.schema_output_ceiling_tokens,
            "requested_max_output_tokens": self.requested_max_output_tokens,
            "passed": self.passed,
            "estimator": "mrc-schema-bounded-presentation-budget-1.0",
        }


@dataclass(frozen=True, slots=True)
class PresentationRepairResult:
    status: str
    display_state: dict[str, Any]
    receipt: dict[str, Any]
    usage: dict[str, int]
    model: str | None
    attempts: int
    provider_outcome: str
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class LanguageAssessment:
    passed: bool
    issues: tuple[dict[str, Any], ...]
    contract_version: str = LANGUAGE_CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "passed": self.passed,
            "issues": [dict(item) for item in self.issues],
        }


def usage_status(usage: Mapping[str, Any] | None) -> str:
    """Classify usage without using numerical zero as an unknown sentinel."""

    if not isinstance(usage, Mapping) or not usage:
        return "UNKNOWN"
    present = {
        key
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(usage.get(key), int)
        and not isinstance(usage.get(key), bool)
        and usage.get(key) >= 0
    }
    if present == {"prompt_tokens", "completion_tokens", "total_tokens"}:
        return "COMPLETE"
    return "PARTIAL"


def aggregate_usage_status(
    usages: Sequence[Mapping[str, Any]],
    *,
    attempted_call_count: int,
) -> str:
    if attempted_call_count <= 0:
        return "UNKNOWN"
    statuses = [usage_status(item) for item in usages]
    if len(statuses) < attempted_call_count:
        statuses.extend(["UNKNOWN"] * (attempted_call_count - len(statuses)))
    if statuses and all(status == "COMPLETE" for status in statuses):
        return "COMPLETE"
    if statuses and all(status == "UNKNOWN" for status in statuses):
        return "UNKNOWN"
    return "PARTIAL"


def provider_outcome_from_error(error: BaseException) -> str:
    receipts = getattr(error, "request_receipts", ())
    if receipts and isinstance(receipts[-1], Mapping):
        outcome = receipts[-1].get("provider_outcome")
        if outcome in {"SUCCEEDED", "REJECTED", "UNKNOWN", "NOT_CALLED"}:
            return str(outcome)
    text = str(error).casefold()
    ambiguous = (
        "server-side execution status is unknown",
        "timed out",
        "timeout",
        "network request failed",
        "connection reset",
        "connection aborted",
        "connection closed",
    )
    return "UNKNOWN" if any(token in text for token in ambiguous) else "REJECTED"


def _source_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _item_id(path: str, index: int, source_sha256: str) -> str:
    payload = json.dumps(
        {"path": path, "index": index, "source_sha256": source_sha256},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "presentation_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _clean_source_text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise PresentationContractError(f"presentation source {path} must be text")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 240:
        raise PresentationContractError(f"presentation source {path} must be concise non-empty text")
    return cleaned


def build_presentation_source(machine_state: Mapping[str, Any]) -> PresentationSource:
    protected = machine_state.get("protected", [])
    parked = machine_state.get("parked_opportunities", [])
    suggestions = machine_state.get("lite_suggestions", [])
    if not isinstance(protected, list) or not isinstance(parked, list) or not isinstance(suggestions, list):
        raise PresentationContractError("machine presentation fields must be finite lists")

    items: list[PresentationItem] = []

    def append(path: str, index: int, value: Any) -> None:
        text = _clean_source_text(value, path)
        source_sha = _source_sha256(text)
        items.append(
            PresentationItem(
                item_id=_item_id(path, index, source_sha),
                path=path,
                index=index,
                source_text=text,
                source_sha256=source_sha,
            )
        )

    for index, value in enumerate(protected):
        append(f"protected[{index}]", index, value)
    for index, value in enumerate(parked):
        append(f"parked_opportunities[{index}]", index, value)
    for suggestion_index, suggestion in enumerate(suggestions):
        if not isinstance(suggestion, Mapping) or set(suggestion) != set(_DISPLAY_FIELDS):
            raise PresentationContractError("Lite suggestion key set is not presentation-safe")
        for field_index, field in enumerate(_DISPLAY_FIELDS):
            append(
                f"lite_suggestions[{suggestion_index}].{field}",
                suggestion_index * len(_DISPLAY_FIELDS) + field_index,
                suggestion[field],
            )

    ids = [item.item_id for item in items]
    if len(ids) != len(set(ids)):
        raise PresentationContractError("presentation source identities collide")

    binding_payload = {
        "contract_version": PRESENTATION_SOURCE_CONTRACT_VERSION,
        "items": [item.authoritative_receipt() for item in items],
    }
    protected_payload = {
        "contract_version": PRESENTATION_SOURCE_CONTRACT_VERSION,
        "cardinality": len(protected),
        "items": [
            item.authoritative_receipt()
            for item in items
            if item.path.startswith("protected[")
        ],
    }
    return PresentationSource(
        items=tuple(items),
        source_binding_digest_sha256=canonical_digest(binding_payload),
        protected_binding_digest_sha256=canonical_digest(protected_payload),
        protected_cardinality=len(protected),
    )


def machine_state_payload(
    state: Mapping[str, Any],
    *,
    coverage_digest_sha256: str | None,
) -> dict[str, Any]:
    return {
        "contract_version": MACHINE_STATE_CONTRACT_VERSION,
        "coverage_digest_sha256": coverage_digest_sha256,
        "state": {key: deepcopy(state.get(key)) for key in _MACHINE_KEYS},
    }


def machine_state_digest(
    state: Mapping[str, Any],
    *,
    coverage_digest_sha256: str | None,
) -> str:
    return canonical_digest(
        machine_state_payload(state, coverage_digest_sha256=coverage_digest_sha256)
    )


def _latin_semantic_letter_count(text: str) -> int:
    scrubbed = _URL_RE.sub(" ", text)
    scrubbed = _DOI_RE.sub(" ", scrubbed)
    scrubbed = _EMAIL_RE.sub(" ", scrubbed)
    total = 0
    for token in _LATIN_TOKEN_RE.findall(scrubbed):
        letters = "".join(character for character in token if character.isalpha())
        if not letters:
            continue
        # Identifiers, model IDs, abbreviations, and code-like terms are allowed
        # inside Chinese syntax and do not count as English prose dominance.
        if any(character.isdigit() for character in token):
            continue
        if any(character in token for character in ("_", "/", ":", "+")):
            continue
        if letters.isupper() and len(letters) <= 12:
            continue
        total += len(letters)
    return total


def assess_chinese_text(text: str) -> dict[str, Any]:
    cleaned = " ".join(text.split())
    cjk_count = len(_CJK_RE.findall(cleaned))
    latin_letters = _latin_semantic_letter_count(cleaned)
    denominator = cjk_count + latin_letters
    cjk_share = (cjk_count / denominator) if denominator else 0.0
    latin_tokens = len(_LATIN_TOKEN_RE.findall(cleaned))
    passed = bool(
        cjk_count >= 4
        and cjk_share >= 0.18
        and not (latin_tokens >= 5 and cjk_count < 8)
    )
    return {
        "passed": passed,
        "cjk_count": cjk_count,
        "latin_semantic_letters": latin_letters,
        "cjk_semantic_share": round(cjk_share, 6),
        "latin_token_count": latin_tokens,
    }


def assess_presentation_language(
    machine_state: Mapping[str, Any],
    *,
    target_language: str,
) -> LanguageAssessment:
    if target_language != "zh":
        return LanguageAssessment(passed=True, issues=())
    source = build_presentation_source(machine_state)
    issues: list[dict[str, Any]] = []
    for item in source.items:
        assessment = assess_chinese_text(item.source_text)
        if not assessment["passed"]:
            issues.append(
                {
                    "id": item.item_id,
                    "path": item.path,
                    "source_sha256": item.source_sha256,
                    **assessment,
                }
            )
    return LanguageAssessment(passed=not issues, issues=tuple(issues))


def _repair_schema(source: PresentationSource) -> dict[str, Any]:
    ids = [item.item_id for item in source.items]
    hashes = [item.source_sha256 for item in source.items]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "contract_version",
            "source_binding_digest_sha256",
            "items",
        ],
        "properties": {
            "contract_version": {
                "type": "string",
                "enum": [PRESENTATION_REPAIR_CONTRACT_VERSION],
            },
            "source_binding_digest_sha256": {
                "type": "string",
                "enum": [source.source_binding_digest_sha256],
            },
            "items": {
                "type": "array",
                "minItems": len(ids),
                "maxItems": len(ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "source_sha256", "display_text"],
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "source_sha256": {"type": "string", "enum": hashes},
                        "display_text": {"type": "string", "maxLength": 240},
                    },
                },
            },
        },
    }


def build_presentation_repair_messages(
    source: PresentationSource,
    *,
    target_language: str,
) -> list[dict[str, str]]:
    if target_language != "zh":
        raise PresentationContractError("presentation repair is registered only for zh")
    request = {
        "contract_version": PRESENTATION_SOURCE_CONTRACT_VERSION,
        "target_language": "zh-CN",
        "source_binding_digest_sha256": source.source_binding_digest_sha256,
        "items": [item.request_dict() for item in source.items],
    }
    system = """You are the presentation-only localization stage of Manuscript Revision Closure.
Translate only the supplied bounded public text values into concise Simplified Chinese.
The immutable source text, item identity, source SHA-256, path, order, scope, and
cardinality remain authoritative. Do not add, delete, merge, split, reorder,
narrow, broaden, reinterpret, or replace any protected item, parked opportunity,
or Lite suggestion. Do not output a verdict, hold code, root-cause decision,
manuscript claim, diagnosis, explanation, or any key outside the exact schema.
Return one exact JSON object and nothing else."""
    user = (
        "Repair only the language of this bounded public projection.\n"
        "No manuscript, coverage rows, machine verdict, or private verifier material is supplied.\n\n"
        + json.dumps(request, ensure_ascii=False, sort_keys=True)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _schema_output_ceiling_tokens(source: PresentationSource) -> int:
    maximal = {
        "contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
        "source_binding_digest_sha256": source.source_binding_digest_sha256,
        "items": [
            {
                "id": item.item_id,
                "source_sha256": item.source_sha256,
                "display_text": "中" * 240,
            }
            for item in source.items
        ],
    }
    serialized = json.dumps(maximal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Two UTF-8 bytes per token is deliberately conservative for this bounded
    # mixed-script JSON.  The result is derived from the actual schema maximum,
    # not from a fixed small cross-provider cap.
    return max(1, math.ceil(len(serialized.encode("utf-8")) / 2))


def presentation_budget(
    messages: Sequence[Mapping[str, str]],
    source: PresentationSource,
    *,
    provider: str,
    model: str,
) -> PresentationBudgetReceipt:
    context_limit = provider_context_limit(provider, model)
    provider_ceiling = provider_output_ceiling(provider)
    estimated_input = estimate_message_tokens(messages)
    safety_margin = max(
        math.ceil(context_limit * 0.03),
        math.ceil(estimated_input * 0.10),
    )
    remaining = max(0, context_limit - estimated_input - safety_margin)
    requested = min(provider_ceiling, remaining)
    schema_ceiling = _schema_output_ceiling_tokens(source)
    return PresentationBudgetReceipt(
        provider=provider,
        model=model,
        provider_output_ceiling_tokens=provider_ceiling,
        context_limit_tokens=context_limit,
        estimated_input_tokens=estimated_input,
        safety_margin_tokens=safety_margin,
        schema_output_ceiling_tokens=schema_ceiling,
        requested_max_output_tokens=requested,
        passed=requested >= schema_ceiling,
    )


def _parse_repair_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if (
            len(lines) < 3
            or lines[0].strip() not in {"```", "```json", "```JSON"}
            or lines[-1].strip() != "```"
        ):
            raise PresentationContractError("presentation repair contains an invalid JSON fence")
        text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PresentationContractError("presentation repair is not one JSON object") from exc
    if not isinstance(value, dict):
        raise PresentationContractError("presentation repair must be one JSON object")
    return value


def validate_presentation_repair(
    value: Mapping[str, Any],
    source: PresentationSource,
    *,
    target_language: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    expected_top = {
        "contract_version",
        "source_binding_digest_sha256",
        "items",
    }
    if set(value) != expected_top:
        raise PresentationContractError("presentation repair key set mismatch")
    if value["contract_version"] != PRESENTATION_REPAIR_CONTRACT_VERSION:
        raise PresentationContractError("presentation repair contract version mismatch")
    if value["source_binding_digest_sha256"] != source.source_binding_digest_sha256:
        raise PresentationContractError("presentation repair source binding digest mismatch")
    rows = value["items"]
    if not isinstance(rows, list):
        raise PresentationContractError("presentation repair items must be a list")
    if len(rows) != len(source.items):
        raise PresentationContractError("presentation repair item cardinality mismatch")

    display_by_id: dict[str, str] = {}
    display_receipts: list[dict[str, Any]] = []
    for position, (row, expected) in enumerate(zip(rows, source.items, strict=True)):
        if not isinstance(row, Mapping) or set(row) != {"id", "source_sha256", "display_text"}:
            raise PresentationContractError("presentation repair item key set mismatch")
        if row["id"] != expected.item_id:
            raise PresentationContractError(
                f"presentation repair item order or identity mismatch at position {position}"
            )
        if row["source_sha256"] != expected.source_sha256:
            raise PresentationContractError(
                f"presentation repair source hash mismatch at position {position}"
            )
        if expected.item_id in display_by_id:
            raise PresentationContractError("presentation repair contains a duplicate item")
        display_text = row["display_text"]
        if not isinstance(display_text, str):
            raise PresentationContractError("presentation repair display text must be text")
        cleaned = " ".join(display_text.split())
        if not cleaned or len(cleaned) > 240:
            raise PresentationContractError("presentation repair display text must be concise non-empty text")
        if target_language == "zh" and not assess_chinese_text(cleaned)["passed"]:
            raise PresentationContractError("presentation repair still fails the Chinese display contract")
        display_by_id[expected.item_id] = cleaned
        display_receipts.append(
            {
                "id": expected.item_id,
                "path": expected.path,
                "index": expected.index,
                "source_sha256": expected.source_sha256,
                "display_text": cleaned,
            }
        )
    return display_by_id, display_receipts


def apply_display_attachment(
    machine_state: Mapping[str, Any],
    source: PresentationSource,
    display_by_id: Mapping[str, str],
) -> dict[str, Any]:
    display = deepcopy(dict(machine_state))
    for item in source.items:
        text = display_by_id[item.item_id]
        path = item.path
        if path.startswith("protected["):
            index = int(path[len("protected[") : path.index("]")])
            display["protected"][index] = text
        elif path.startswith("parked_opportunities["):
            index = int(path[len("parked_opportunities[") : path.index("]")])
            display["parked_opportunities"][index] = text
        elif path.startswith("lite_suggestions["):
            index = int(path[len("lite_suggestions[") : path.index("]")])
            field = path.split("].", 1)[1]
            display["lite_suggestions"][index][field] = text
        else:  # pragma: no cover
            raise PresentationContractError("unknown presentation path")
    return display


def _base_receipt(
    source: PresentationSource,
    *,
    target_language: str,
    machine_digest_before: str,
    timeout_seconds: float,
    budget: PresentationBudgetReceipt,
) -> dict[str, Any]:
    return {
        "transaction_version": PRESENTATION_TRANSACTION_VERSION,
        **source.bounded_receipt(),
        "repair_contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
        "language_contract_version": LANGUAGE_CONTRACT_VERSION,
        "target_language": target_language,
        "machine_state_digest_before_sha256": machine_digest_before,
        "stage_timeout_seconds": timeout_seconds,
        "max_transient_retries": 0,
        "budget": budget.as_dict(),
        "raw_provider_response_persisted": False,
        "manuscript_transmitted": False,
        "coverage_rows_transmitted": False,
        "machine_verdict_transmitted": False,
    }


def presentation_pass_without_repair(
    machine_state: Mapping[str, Any],
    *,
    target_language: str,
    coverage_digest_sha256: str | None,
    language_assessment: LanguageAssessment,
) -> PresentationRepairResult:
    source = build_presentation_source(machine_state)
    digest = machine_state_digest(
        machine_state,
        coverage_digest_sha256=coverage_digest_sha256,
    )
    display_receipts = [
        {
            "id": item.item_id,
            "path": item.path,
            "index": item.index,
            "source_sha256": item.source_sha256,
            "display_text": item.source_text,
        }
        for item in source.items
    ]
    display_binding = canonical_digest(
        {
            "contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
            "items": display_receipts,
        }
    )
    receipt = {
        "transaction_version": PRESENTATION_TRANSACTION_VERSION,
        **source.bounded_receipt(),
        "repair_contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
        "language_contract_version": LANGUAGE_CONTRACT_VERSION,
        "target_language": target_language,
        "status": "PASS",
        "repair_attempted": False,
        "provider_called": False,
        "presentation_provider_outcome": "NOT_CALLED",
        "usage": {},
        "usage_status": "UNKNOWN",
        "actual_attempt_count": 0,
        "max_transient_retries": 0,
        "stage_timeout_seconds": None,
        "budget": None,
        "language_assessment": language_assessment.as_dict(),
        "display_binding_digest_sha256": display_binding,
        "display_items": display_receipts,
        "machine_state_digest_before_sha256": digest,
        "machine_state_digest_after_sha256": digest,
        "machine_state_parity": True,
        "raw_provider_response_persisted": False,
        "manuscript_transmitted": False,
        "coverage_rows_transmitted": False,
        "machine_verdict_transmitted": False,
    }
    return PresentationRepairResult(
        status="PASS",
        display_state=deepcopy(dict(machine_state)),
        receipt=receipt,
        usage={},
        model=None,
        attempts=0,
        provider_outcome="NOT_CALLED",
    )


def repair_presentation(
    machine_state: Mapping[str, Any],
    *,
    provider: str,
    model: str | None,
    reasoning_option: str | None,
    target_language: str,
    coverage_digest_sha256: str | None,
    timeout_seconds: float | None = None,
    on_attempt: Callable[[int], None] | None = None,
) -> PresentationRepairResult:
    """Attempt exactly one presentation-only request and never resend it."""

    source = build_presentation_source(machine_state)
    machine_digest_before = machine_state_digest(
        machine_state,
        coverage_digest_sha256=coverage_digest_sha256,
    )
    config = load_provider_config(provider, model=model)
    messages = build_presentation_repair_messages(source, target_language=target_language)
    repair_schema = _repair_schema(source)
    budget = presentation_budget(
        messages,
        source,
        provider=config.name,
        model=config.model,
    )
    request_timeout = provider_stage_timeout_seconds(
        config.name,
        "presentation_repair",
        override=timeout_seconds,
    )
    base = _base_receipt(
        source,
        target_language=target_language,
        machine_digest_before=machine_digest_before,
        timeout_seconds=request_timeout,
        budget=budget,
    )
    if not budget.passed:
        receipt = {
            **base,
            "status": "HOLD",
            "error_code": "PRESENTATION_CONTEXT_BUDGET_HOLD",
            "repair_attempted": False,
            "provider_called": False,
            "presentation_provider_outcome": "NOT_CALLED",
            "usage": {},
            "usage_status": "UNKNOWN",
            "actual_attempt_count": 0,
            "machine_state_digest_after_sha256": machine_digest_before,
            "machine_state_parity": True,
        }
        return PresentationRepairResult(
            status="HOLD",
            display_state=deepcopy(dict(machine_state)),
            receipt=receipt,
            usage={},
            model=config.model,
            attempts=0,
            provider_outcome="NOT_CALLED",
            error_code="PRESENTATION_CONTEXT_BUDGET_HOLD",
            error_message="presentation context budget cannot hold the bounded schema output",
        )

    attempts: list[int] = []

    def attempt_callback(number: int) -> None:
        attempts.append(number)
        if on_attempt is not None:
            on_attempt(number)

    try:
        completion: CompletionResult = ChatCompletionClient(
            config,
            timeout_seconds=request_timeout,
            max_transient_retries=0,
            on_attempt=attempt_callback,
        ).complete(
            messages,
            reasoning_option=reasoning_option,
            json_mode=True,
            json_schema=repair_schema,
            json_schema_name="mrc_presentation_repair",
            max_output_tokens=budget.requested_max_output_tokens,
            stage="presentation_repair",
            schema_sha256=schema_sha256(repair_schema),
            coverage_digest_sha256=coverage_digest_sha256,
        )
    except ProviderRequestError as exc:
        outcome = provider_outcome_from_error(exc)
        receipt = {
            **base,
            "status": "HOLD",
            "error_code": "PRESENTATION_PROVIDER_FAILURE",
            "error_message": str(exc),
            "repair_attempted": True,
            "provider_called": True,
            "provider": config.name,
            "model": config.model,
            "presentation_provider_outcome": outcome,
            "usage": {},
            "usage_status": "UNKNOWN",
            "actual_attempt_count": len(attempts),
            "physical_request_receipts": [dict(item) for item in exc.request_receipts],
            "machine_state_digest_after_sha256": machine_digest_before,
            "machine_state_parity": True,
        }
        return PresentationRepairResult(
            status="HOLD",
            display_state=deepcopy(dict(machine_state)),
            receipt=receipt,
            usage={},
            model=config.model,
            attempts=len(attempts),
            provider_outcome=outcome,
            error_code="PRESENTATION_PROVIDER_FAILURE",
            error_message=str(exc),
        )

    runtime = {
        **base,
        "repair_attempted": True,
        "provider_called": True,
        "provider": config.name,
        "model": completion.model,
        "provider_transport_outcome": "SUCCEEDED",
        "presentation_provider_outcome": "SUCCEEDED",
        "usage": dict(completion.usage),
        "usage_status": usage_status(completion.usage),
        "actual_attempt_count": len(attempts),
        "physical_request_receipts": [dict(item) for item in completion.request_receipts],
        "finish_reason": completion.finish_reason,
    }
    if completion.finish_reason == "length":
        receipt = {
            **runtime,
            "status": "HOLD",
            "presentation_provider_outcome": "REJECTED",
            "error_code": "PRESENTATION_REPAIR_TRUNCATED",
            "machine_state_digest_after_sha256": machine_digest_before,
            "machine_state_parity": True,
        }
        return PresentationRepairResult(
            status="HOLD",
            display_state=deepcopy(dict(machine_state)),
            receipt=receipt,
            usage=dict(completion.usage),
            model=completion.model,
            attempts=len(attempts),
            provider_outcome="REJECTED",
            error_code="PRESENTATION_REPAIR_TRUNCATED",
            error_message="provider truncated the presentation repair response",
        )

    try:
        display_by_id, display_items = validate_presentation_repair(
            _parse_repair_json(completion.content),
            source,
            target_language=target_language,
        )
        display_state = apply_display_attachment(machine_state, source, display_by_id)
    except PresentationContractError as exc:
        receipt = {
            **runtime,
            "status": "HOLD",
            "presentation_provider_outcome": "REJECTED",
            "error_code": "PRESENTATION_REPAIR_CONTRACT_FAILED",
            "error_message": str(exc),
            "machine_state_digest_after_sha256": machine_digest_before,
            "machine_state_parity": True,
        }
        return PresentationRepairResult(
            status="HOLD",
            display_state=deepcopy(dict(machine_state)),
            receipt=receipt,
            usage=dict(completion.usage),
            model=completion.model,
            attempts=len(attempts),
            provider_outcome="REJECTED",
            error_code="PRESENTATION_REPAIR_CONTRACT_FAILED",
            error_message=str(exc),
        )

    machine_digest_after = machine_state_digest(
        machine_state,
        coverage_digest_sha256=coverage_digest_sha256,
    )
    if machine_digest_after != machine_digest_before:
        receipt = {
            **runtime,
            "status": "HOLD",
            "presentation_provider_outcome": "REJECTED",
            "error_code": "INTEGRITY_HOLD",
            "machine_state_digest_after_sha256": machine_digest_after,
            "machine_state_parity": False,
        }
        return PresentationRepairResult(
            status="HOLD",
            display_state=deepcopy(dict(machine_state)),
            receipt=receipt,
            usage=dict(completion.usage),
            model=completion.model,
            attempts=len(attempts),
            provider_outcome="REJECTED",
            error_code="INTEGRITY_HOLD",
            error_message="presentation repair changed the frozen machine state",
        )

    display_binding = canonical_digest(
        {
            "contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
            "items": display_items,
        }
    )
    receipt = {
        **runtime,
        "status": "PASS",
        "display_binding_digest_sha256": display_binding,
        "display_items": display_items,
        "machine_state_digest_after_sha256": machine_digest_after,
        "machine_state_parity": True,
    }
    return PresentationRepairResult(
        status="PASS",
        display_state=display_state,
        receipt=receipt,
        usage=dict(completion.usage),
        model=completion.model,
        attempts=len(attempts),
        provider_outcome="SUCCEEDED",
    )
