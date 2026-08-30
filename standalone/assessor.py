"""Native multi-stage closure orchestration with transactional presentation repair."""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.closure_state import (
    ClosureStateError,
    EVIDENCE_HOLD_CODES,
    SUBMISSION_HOLD_CODES,
    TECHNICAL_HOLD_CONTRACT_VERSION,
    decide_state,
    minimal_receipt,
    public_card,
)

from . import __version__
from .document_reader import DocumentContent, DocumentReadError, read_document
from .events import EventSink, RunPhase
from .harness import (
    ADJUDICATION_CONTRACT_VERSION,
    AFFIRMATIVE_STOP_DIMENSIONS,
    AFFIRMATIVE_STOP_CONTRACT_VERSION,
    AFFIRMATIVE_SUFFICIENCY_KEYS,
    CANDIDATE_BINDING_CONTRACT_VERSION,
    COVERAGE_CONTRACT_VERSION,
    COVERAGE_DIMENSIONS,
    COVERAGE_JSON_SCHEMA,
    MANUSCRIPT_BASIS_CONTRACT_VERSION,
    ROOT_CAUSE_DISPOSITION_CODES,
    ROOT_CAUSE_ORIGINS,
    SCHEMA_DEFINITION_LINT_VERSION,
    SUFFICIENCY_REASON_CODES,
    ContextBudgetReceipt,
    HarnessContractError,
    IntakeReceipt,
    SchemaDefinitionError,
    affirmative_stop_gate_receipt,
    analyze_intake_structure,
    canonical_digest,
    build_adjudication_json_schema,
    candidate_binding_receipt,
    context_budget,
    coverage_is_complete,
    harness_receipt,
    validate_adjudication_binding,
    validate_candidate_binding,
    validate_coverage,
    validate_cross_stage_consistency,
    validate_json_schema_contract,
    validate_schema_definition,
    schema_sha256,
)
from .localization import localize_closure_card
from .presentation_transaction import (
    PRESENTATION_TRANSACTION_VERSION,
    PresentationRepairResult,
    aggregate_usage_status,
    assess_presentation_language,
    build_presentation_source,
    machine_state_digest,
    machine_state_payload,
    presentation_pass_without_repair,
    provider_outcome_from_error,
    repair_presentation,
    usage_status,
)
from .prompting import build_adjudication_messages, build_coverage_messages
from .providers import (
    ChatCompletionClient,
    CompletionResult,
    ProviderConfigurationError,
    ProviderRequestError,
    load_provider_config,
    provider_capability,
    provider_stage_timeout_seconds,
    resolve_provider_selection,
    validate_reasoning_option,
)


MODEL_KEYS = frozenset(
    {
        "material_root_causes",
        "affirmative_sufficiency",
        "evidence_hold_codes",
        "submission_hold_codes",
        "protected",
        "parked_opportunities",
        "lite_suggestions",
    }
)
PROVIDER_TRANSMISSION_CONSENT_VERSION = "mrc-provider-transmission-consent-1.0"


@dataclass(slots=True)
class ProviderTransmissionConsent:
    artifact_sha256: str
    provider: str
    model: str
    confirmed_at: str
    confirmed: bool = False
    contract_version: str = PROVIDER_TRANSMISSION_CONSENT_VERSION
    _consumed: bool = False

    def set_decision(self, confirmed: bool) -> None:
        if self._consumed:
            raise ValueError("provider transmission consent was already consumed")
        self.confirmed = confirmed

    def consume(self, *, artifact_sha256: str, provider: str, model: str) -> bool:
        if self._consumed:
            return False
        self._consumed = True
        return bool(
            self.confirmed
            and self.contract_version == PROVIDER_TRANSMISSION_CONSENT_VERSION
            and self.artifact_sha256 == artifact_sha256
            and self.provider == provider
            and self.model == model
            and self.confirmed_at.strip()
        )


def prepare_provider_transmission_consent(
    *, artifact_sha256: str, provider: str, model: str, confirmed: bool = False
) -> ProviderTransmissionConsent:
    return ProviderTransmissionConsent(
        artifact_sha256=artifact_sha256,
        provider=provider,
        model=model,
        confirmed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        confirmed=confirmed,
    )
ROOT_CAUSE_KEYS = frozenset(
    {
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
    }
)


class ModelContractError(ValueError):
    """Raised when model output is not exactly one finite stage contract."""


@dataclass(frozen=True, slots=True)
class RunOptions:
    manuscript_path: Path
    provider: str = "deepseek"
    model: str | None = None
    reasoning_option: str | None = None
    output_language: str = "zh"
    manuscript_identity: str | None = None
    confirm_complete_current_manuscript: bool = False
    prior_receipt: Mapping[str, Any] | None = None
    timeout_seconds: float | None = None
    transient_retries: int = 0
    enable_presentation_repair: bool = True
    provider_transmission_consent: bool | ProviderTransmissionConsent = False


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    closure_card: dict[str, Any]
    minimal_receipt: dict[str, Any]
    provider: str | None
    model: str | None
    reasoning_option: str | None
    api_called: bool
    usage: dict[str, int]
    usage_calls: tuple[dict[str, int], ...]
    attempts: int
    artifact_sha256: str
    semantic_content_sha256: str
    character_count: int
    thread_id: str
    harness: dict[str, Any]
    provider_receipts: tuple[dict[str, Any], ...] = ()
    usage_call_stages: tuple[str, ...] = ()
    run_status: dict[str, str] = field(default_factory=dict)
    machine_receipt: dict[str, Any] = field(default_factory=dict)
    presentation_receipt: dict[str, Any] = field(default_factory=dict)
    consent_receipt: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        status = dict(self.run_status)
        physical_receipts = [
            deepcopy(item)
            for stage_receipt in self.provider_receipts
            for item in stage_receipt.get("physical_request_receipts", [])
            if isinstance(item, Mapping)
        ]
        successful_completions = sum(
            item.get("provider_outcome") == "SUCCEEDED" for item in physical_receipts
        )
        usage_receipts = sum(
            item.get("usage_status") in {"COMPLETE", "PARTIAL"} for item in physical_receipts
        )
        unknown_potential_charge = sum(
            bool(item.get("request_dispatched"))
            and item.get("usage_status") == "UNKNOWN"
            for item in physical_receipts
        )
        return {
            "closure_card": deepcopy(self.closure_card),
            "minimal_receipt": deepcopy(self.minimal_receipt),
            "runtime": {
                "provider": self.provider,
                "model": self.model,
                "reasoning_option": self.reasoning_option,
                "api_called": self.api_called,
                "usage": dict(self.usage),
                "usage_calls": [dict(item) for item in self.usage_calls],
                "usage_call_stages": list(self.usage_call_stages),
                "provider_receipts": [deepcopy(item) for item in self.provider_receipts],
                "physical_request_receipts": physical_receipts,
                "provider_call_count": len(physical_receipts),
                "physical_request_attempt_count": len(physical_receipts),
                "successful_completion_count": successful_completions,
                "usage_receipt_count": usage_receipts,
                "logical_stage_count": len(self.provider_receipts),
                "unknown_potential_charge_attempt_count": unknown_potential_charge,
                "core_call_count": sum(
                    stage in {"coverage", "adjudication"}
                    for stage in self.usage_call_stages
                ),
                "presentation_repair_call_count": sum(
                    stage == "presentation_repair"
                    for stage in self.usage_call_stages
                ),
                "attempts": self.attempts,
                "thread_id": self.thread_id,
                "artifact_sha256": self.artifact_sha256,
                "semantic_content_sha256": self.semantic_content_sha256,
                "character_count": self.character_count,
                "standalone_version": __version__,
                "skill_version": "0.2.1",
                "presentation_transaction_version": PRESENTATION_TRANSACTION_VERSION,
                "status": status,
                "machine_status": status.get("machine_status"),
                "presentation_status": status.get("presentation_status"),
                "terminal_status": status.get("terminal_status"),
                "recoverability": status.get("recoverability"),
                "machine_provider_outcome": status.get("machine_provider_outcome"),
                "presentation_provider_outcome": status.get("presentation_provider_outcome"),
                "usage_status": status.get("usage_status"),
                "machine_receipt": deepcopy(self.machine_receipt),
                "presentation_receipt": deepcopy(self.presentation_receipt),
                "provider_transmission_consent": deepcopy(self.consent_receipt),
                "raw_provider_response_persisted": False,
                "automatic_result_file_written": False,
                "harness": deepcopy(self.harness),
            },
        }


def parse_model_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ModelContractError("model response contains an incomplete Markdown fence")
        if lines[0].strip() not in {"```", "```json", "```JSON"}:
            raise ModelContractError("model response uses an unsupported Markdown fence")
        text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelContractError("model response is not one JSON object") from exc
    if not isinstance(value, dict):
        raise ModelContractError("model response must be a JSON object")
    return value


def _string_list(value: Any, field: str, *, maximum: int, max_length: int = 240) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ModelContractError(f"{field} must be a list with at most {maximum} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > max_length:
            raise ModelContractError(f"{field} must contain concise non-empty strings")
        result.append(" ".join(item.split()))
    return result


def validate_model_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != MODEL_KEYS:
        missing = sorted(MODEL_KEYS.difference(value))
        extra = sorted(set(value).difference(MODEL_KEYS))
        raise ModelContractError(f"model state key set mismatch; missing={missing}; extra={extra}")
    causes = value["material_root_causes"]
    if not isinstance(causes, list) or len(causes) > len(COVERAGE_DIMENSIONS):
        raise ModelContractError("material_root_causes exceeds the finite coverage dimension set")
    clean_causes: list[dict[str, Any]] = []
    for cause in causes:
        if not isinstance(cause, dict) or set(cause) != ROOT_CAUSE_KEYS:
            raise ModelContractError("each material root cause must match the exact finite schema")
        boolean_fields = ROOT_CAUSE_KEYS - {
            "dimension",
            "scope",
            "origin",
            "disposition_reason_code",
        }
        for name in boolean_fields:
            if not isinstance(cause[name], bool):
                raise ModelContractError(f"material root cause {name} must be boolean")
        dimension = cause["dimension"]
        if dimension not in COVERAGE_DIMENSIONS:
            raise ModelContractError("material root cause dimension must use the registered coverage set")
        if cause["origin"] not in ROOT_CAUSE_ORIGINS:
            raise ModelContractError("material root cause origin is invalid")
        if cause["disposition_reason_code"] not in ROOT_CAUSE_DISPOSITION_CODES:
            raise ModelContractError("material root cause disposition reason is invalid")
        if cause["scope"] not in {"local", "central"}:
            raise ModelContractError("material root cause scope must be local or central")
        clean_cause = {key: item for key, item in cause.items() if key != "dimension"}
        clean_causes.append({**clean_cause, "affects": [dimension]})
    sufficiency_rows = value["affirmative_sufficiency"]
    if not isinstance(sufficiency_rows, list) or len(sufficiency_rows) != len(
        AFFIRMATIVE_STOP_DIMENSIONS
    ):
        raise ModelContractError("affirmative_sufficiency must cover every STOP dimension")
    clean_sufficiency: list[dict[str, Any]] = []
    observed_sufficiency_dimensions: list[str] = []
    for row in sufficiency_rows:
        if not isinstance(row, dict) or set(row) != AFFIRMATIVE_SUFFICIENCY_KEYS:
            raise ModelContractError("affirmative sufficiency row key set mismatch")
        dimension = row["dimension"]
        if dimension not in AFFIRMATIVE_STOP_DIMENSIONS:
            raise ModelContractError("affirmative sufficiency dimension is invalid")
        for name in ("assessed", "affirmative_sufficiency", "unresolved_material_concern"):
            if not isinstance(row[name], bool):
                raise ModelContractError(f"affirmative sufficiency {name} must be boolean")
        if row["sufficiency_reason_code"] not in SUFFICIENCY_REASON_CODES:
            raise ModelContractError("affirmative sufficiency reason code is invalid")
        observed_sufficiency_dimensions.append(dimension)
        clean_sufficiency.append(dict(row))
    if set(observed_sufficiency_dimensions) != set(AFFIRMATIVE_STOP_DIMENSIONS) or len(
        set(observed_sufficiency_dimensions)
    ) != len(observed_sufficiency_dimensions):
        raise ModelContractError("affirmative sufficiency dimension set must be exact")
    evidence = _string_list(
        value["evidence_hold_codes"],
        "evidence_hold_codes",
        maximum=len(EVIDENCE_HOLD_CODES),
        max_length=80,
    )
    submission = _string_list(
        value["submission_hold_codes"],
        "submission_hold_codes",
        maximum=len(SUBMISSION_HOLD_CODES),
        max_length=80,
    )
    if any(code not in EVIDENCE_HOLD_CODES for code in evidence):
        raise ModelContractError("evidence_hold_codes contains an unknown code")
    if any(code not in SUBMISSION_HOLD_CODES for code in submission):
        raise ModelContractError("submission_hold_codes contains an unknown code")
    protected = _string_list(value["protected"], "protected", maximum=5)
    parked = _string_list(value["parked_opportunities"], "parked_opportunities", maximum=2)
    suggestions = value["lite_suggestions"]
    if not isinstance(suggestions, list) or len(suggestions) > 3:
        raise ModelContractError("lite_suggestions must contain at most three items")
    clean_suggestions: list[dict[str, str]] = []
    for suggestion in suggestions:
        if not isinstance(suggestion, dict) or set(suggestion) != set(
            ("Direction", "Why it matters", "What to protect")
        ):
            raise ModelContractError("each Lite suggestion must use the exact three-field schema")
        clean: dict[str, str] = {}
        for field_name, text in suggestion.items():
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > 240:
                raise ModelContractError(f"Lite suggestion {field_name} must be concise non-empty text")
            clean[field_name] = " ".join(text.split())
        clean_suggestions.append(clean)
    return {
        "material_root_causes": clean_causes,
        "affirmative_sufficiency": clean_sufficiency,
        "evidence_hold_codes": list(dict.fromkeys(evidence)),
        "submission_hold_codes": list(dict.fromkeys(submission)),
        "protected": protected,
        "parked_opportunities": parked,
        "lite_suggestions": clean_suggestions,
    }


def _validate_model_output_language(value: Mapping[str, Any], language: str) -> None:
    """Retain the historical error contract while using the 0.6.2 heuristic."""

    assessment = assess_presentation_language(value, target_language=language)
    if not assessment.passed:
        raise ModelContractError("requested Chinese output contains a non-Chinese public text value")


def _aggregate_usage(usages: Sequence[Mapping[str, int]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for usage in usages:
        for key, value in usage.items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = result.get(key, 0) + value
    return result


def _base_state(document: DocumentContent, options: RunOptions, intake: IntakeReceipt) -> dict[str, Any]:
    identity = (options.manuscript_identity or document.path.name).strip()
    locally_processable = bool(document.text.strip() and intake.local_preflight_passed)
    state: dict[str, Any] = {
        "manuscript_complete": locally_processable,
        "current_identity_clear": bool(identity),
        "whole_manuscript_read": locally_processable,
        "critical_basis_available": locally_processable,
        "bounded_scope": False,
        "current_manuscript_identity": identity,
        "current_artifact_sha256": document.artifact_sha256,
        "current_semantic_content_sha256": document.semantic_content_sha256,
        "material_root_causes": [],
        "affirmative_sufficiency": [],
        "affirmative_stop_gate_passed": False,
        "evidence_hold_codes": [],
        "submission_hold_codes": list(document.submission_hold_codes),
        "protected": [],
        "parked_opportunities": [],
        "lite_suggestions": [],
        "invalidation_events": [],
        "artifact_only_drift_verified": False,
        "formal_tone": False,
        "rewrite_requested": False,
        "output_language": options.output_language,
    }
    if options.prior_receipt is not None:
        state["prior_receipt"] = dict(options.prior_receipt)
        for field_name in ("evidence_hold_codes", "submission_hold_codes"):
            previous = options.prior_receipt.get(field_name, [])
            if isinstance(previous, list):
                state[field_name] = list(dict.fromkeys([*state[field_name], *previous]))
    return state


def _provider_transmission_consent_receipt(
    raw: bool | ProviderTransmissionConsent,
    *,
    artifact_sha256: str,
    provider: str,
    model: str,
) -> tuple[bool, dict[str, Any]]:
    recorded_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    binding_match = raw is True
    authorized = raw is True
    if isinstance(raw, ProviderTransmissionConsent):
        binding_match = bool(
            raw.contract_version == PROVIDER_TRANSMISSION_CONSENT_VERSION
            and raw.artifact_sha256 == artifact_sha256
            and raw.provider == provider
            and raw.model == model
        )
        authorized = raw.consume(
            artifact_sha256=artifact_sha256,
            provider=provider,
            model=model,
        )
    return authorized, {
        "contract_version": PROVIDER_TRANSMISSION_CONSENT_VERSION,
        "status": "CONFIRMED" if authorized else "NOT_AUTHORIZED",
        "binding_match": binding_match,
        "artifact_sha256": artifact_sha256,
        "provider": provider,
        "model": model,
        "recorded_at": recorded_at,
    }


def _status(
    *,
    machine_status: str,
    presentation_status: str,
    terminal_status: str,
    recoverability: str,
    machine_provider_outcome: str,
    presentation_provider_outcome: str,
    usage_status_value: str,
) -> dict[str, str]:
    return {
        "machine_status": machine_status,
        "presentation_status": presentation_status,
        "terminal_status": terminal_status,
        "recoverability": recoverability,
        "machine_provider_outcome": machine_provider_outcome,
        "presentation_provider_outcome": presentation_provider_outcome,
        "usage_status": usage_status_value,
    }


def _receipt_usage(receipt: Mapping[str, Any]) -> dict[str, int]:
    value = receipt.get("usage")
    return dict(value) if isinstance(value, Mapping) else {}


def _synthetic_physical_receipts(
    *,
    stage: str,
    provider: str,
    model: str,
    reasoning_option: str,
    attempt_count: int,
    timeout_seconds: float,
    provider_outcome: str,
    usage: Mapping[str, int],
    schema_digest: str | None,
    coverage_digest: str | None,
    error: BaseException | None = None,
) -> list[dict[str, Any]]:
    """Represent local mocks that call on_attempt but bypass the real HTTP client."""

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    count = max(1, attempt_count)
    result: list[dict[str, Any]] = []
    for number in range(1, count + 1):
        item_usage = dict(usage) if number == count else {}
        outcome = provider_outcome if number == count else "UNKNOWN"
        result.append(
            {
                "contract_version": "mrc-provider-request-transaction-2.0",
                "request_id": str(uuid.uuid4()),
                "stage": stage,
                "provider": provider,
                "model": model,
                "reasoning_option": reasoning_option,
                "physical_attempt_number": number,
                "timeout_seconds": timeout_seconds,
                "max_transient_retries": 0,
                "retry_decision": "STOP_NO_AUTOMATIC_RETRY",
                "request_dispatched": True,
                "provider_outcome": outcome,
                "http_status": 200 if outcome == "SUCCEEDED" else None,
                "retry_after": None,
                "error_class": type(error).__name__ if error is not None else None,
                "error_summary": str(error)[:500] if error is not None else None,
                "provider_error_detail_contract_version": "mrc-provider-error-detail-1.0",
                "provider_error_status": None,
                "provider_error_code": None,
                "provider_error_detail": None,
                "usage_status": usage_status(item_usage),
                "usage": item_usage,
                "schema_sha256": schema_digest,
                "coverage_digest_sha256": coverage_digest,
                "schema_delivery_mode": provider_capability(provider)["schema_delivery_mode"],
                "schema_definition_lint_contract_version": (
                    SCHEMA_DEFINITION_LINT_VERSION if schema_digest else None
                ),
                "schema_definition_lint_status": "PASS" if schema_digest else None,
                "started_at": now,
                "finished_at": now,
                "test_transport": "mocked_completion",
            }
        )
    return result


def _provider_receipt_completed(
    stage: str,
    completion: CompletionResult,
    *,
    provider: str,
    requested_model: str,
    reasoning_option: str,
    attempt_count: int,
    timeout_seconds: float,
    budget: Mapping[str, Any],
    schema_digest: str | None,
    coverage_digest: str | None,
) -> dict[str, Any]:
    physical = [deepcopy(dict(item)) for item in completion.request_receipts]
    if not physical:
        physical = _synthetic_physical_receipts(
            stage=stage,
            provider=provider,
            model=completion.model or requested_model,
            reasoning_option=reasoning_option,
            attempt_count=attempt_count,
            timeout_seconds=timeout_seconds,
            provider_outcome="SUCCEEDED",
            usage=completion.usage,
            schema_digest=schema_digest,
            coverage_digest=coverage_digest,
        )
    return {
        "stage": stage,
        "provider_called": True,
        "provider_outcome": "SUCCEEDED",
        "provider": provider,
        "model": completion.model,
        "reasoning_option": reasoning_option,
        "finish_reason": completion.finish_reason,
        "usage": dict(completion.usage),
        "usage_status": usage_status(completion.usage),
        "actual_attempt_count": len(physical),
        "stage_timeout_seconds": timeout_seconds,
        "max_transient_retries": 0,
        "schema_sha256": schema_digest,
        "coverage_digest_sha256": coverage_digest,
        "provider_capability": provider_capability(provider),
        "schema_definition_lint_contract_version": SCHEMA_DEFINITION_LINT_VERSION,
        "schema_definition_lint_status": "PASS",
        "physical_request_receipts": physical,
        "budget": dict(budget),
        "committed_in_memory": True,
        "raw_provider_response_persisted": False,
    }


def _provider_receipt_failed(
    stage: str,
    error: ProviderRequestError,
    *,
    provider: str,
    requested_model: str,
    reasoning_option: str,
    attempt_count: int,
    timeout_seconds: float,
    budget: Mapping[str, Any],
    schema_digest: str | None,
    coverage_digest: str | None,
) -> dict[str, Any]:
    physical = [deepcopy(dict(item)) for item in error.request_receipts]
    if not physical:
        physical = _synthetic_physical_receipts(
            stage=stage,
            provider=provider,
            model=requested_model,
            reasoning_option=reasoning_option,
            attempt_count=attempt_count,
            timeout_seconds=timeout_seconds,
            provider_outcome=provider_outcome_from_error(error),
            usage={},
            schema_digest=schema_digest,
            coverage_digest=coverage_digest,
            error=error,
        )
    latest = physical[-1] if physical else {}
    return {
        "stage": stage,
        "provider_called": True,
        "provider_outcome": provider_outcome_from_error(error),
        "provider": provider,
        "model": requested_model,
        "reasoning_option": reasoning_option,
        "finish_reason": None,
        "usage": {},
        "usage_status": "UNKNOWN",
        "actual_attempt_count": len(physical),
        "stage_timeout_seconds": timeout_seconds,
        "max_transient_retries": 0,
        "schema_sha256": schema_digest,
        "coverage_digest_sha256": coverage_digest,
        "provider_capability": provider_capability(provider),
        "schema_definition_lint_contract_version": SCHEMA_DEFINITION_LINT_VERSION,
        "schema_definition_lint_status": (
            latest.get("schema_definition_lint_status")
        ),
        "physical_request_receipts": physical,
        "budget": dict(budget),
        "error_code": "PROVIDER_REQUEST_FAILED",
        "error_message": str(error),
        "provider_error_detail_contract_version": latest.get(
            "provider_error_detail_contract_version", "mrc-provider-error-detail-1.0"
        ),
        "provider_error_status": latest.get("provider_error_status"),
        "provider_error_code": latest.get("provider_error_code"),
        "provider_error_detail": latest.get("provider_error_detail"),
        "committed_in_memory": True,
        "raw_provider_response_persisted": False,
    }


def _presentation_provider_receipt(
    result: PresentationRepairResult,
    *,
    provider: str,
    requested_model: str,
    reasoning_option: str,
) -> dict[str, Any] | None:
    receipt = result.receipt
    if not receipt.get("provider_called"):
        return None
    physical = [
        deepcopy(dict(item))
        for item in receipt.get("physical_request_receipts", [])
        if isinstance(item, Mapping)
    ]
    if not physical:
        physical = _synthetic_physical_receipts(
            stage="presentation_repair",
            provider=provider,
            model=result.model or requested_model,
            reasoning_option=reasoning_option,
            attempt_count=result.attempts,
            timeout_seconds=float(receipt.get("stage_timeout_seconds") or 0.0),
            provider_outcome=result.provider_outcome,
            usage=result.usage,
            schema_digest=receipt.get("schema_sha256"),
            coverage_digest=receipt.get("coverage_digest_sha256"),
            error=RuntimeError(result.error_message) if result.error_message else None,
        )
    return {
        "stage": "presentation_repair",
        "provider_called": True,
        "provider_outcome": result.provider_outcome,
        "provider": provider,
        "model": result.model,
        "reasoning_option": reasoning_option,
        "finish_reason": receipt.get("finish_reason"),
        "usage": dict(result.usage),
        "usage_status": usage_status(result.usage),
        "actual_attempt_count": len(physical),
        "stage_timeout_seconds": receipt.get("stage_timeout_seconds"),
        "max_transient_retries": 0,
        "physical_request_receipts": physical,
        "budget": deepcopy(receipt.get("budget")),
        "error_code": result.error_code,
        "committed_in_memory": True,
        "raw_provider_response_persisted": False,
    }


def _provider_outcome_for_machine(receipts: Sequence[Mapping[str, Any]]) -> str:
    machine = [item for item in receipts if item.get("stage") in {"coverage", "adjudication"}]
    if not machine:
        return "NOT_CALLED"
    for item in reversed(machine):
        outcome = str(item.get("provider_outcome", "UNKNOWN"))
        if outcome != "SUCCEEDED":
            return outcome
    return "SUCCEEDED"


def _flatten_physical_receipts(receipts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        deepcopy(dict(item))
        for receipt in receipts
        for item in receipt.get("physical_request_receipts", [])
        if isinstance(item, Mapping)
    ]


def _usage_status_for_receipts(receipts: Sequence[Mapping[str, Any]]) -> str:
    physical = _flatten_physical_receipts(receipts)
    attempted = sum(bool(item.get("request_dispatched")) for item in physical)
    usages = [_receipt_usage(item) for item in physical if item.get("usage_status") != "UNKNOWN"]
    return aggregate_usage_status(usages, attempted_call_count=attempted)


def _finish(
    document: DocumentContent,
    state: dict[str, Any],
    sink: EventSink,
    *,
    provider: str | None,
    model: str | None,
    reasoning_option: str | None,
    provider_receipts: Sequence[Mapping[str, Any]] = (),
    attempts: int = 0,
    harness_state: Mapping[str, Any],
    display_state: Mapping[str, Any] | None = None,
    run_status: Mapping[str, str] | None = None,
    machine_receipt: Mapping[str, Any] | None = None,
    presentation_receipt: Mapping[str, Any] | None = None,
) -> AnalysisResult:
    machine_decision = decide_state(state)
    visible_state = deepcopy(dict(display_state or state))
    display_decision = decide_state(visible_state)
    parity_keys = (
        "verdict",
        "reason_category",
        "evidence_hold_codes",
        "submission_hold_codes",
        "next_permitted_action",
    )
    if any(machine_decision[key] != display_decision[key] for key in parity_keys):
        raise ClosureStateError("presentation state changed deterministic verdict inputs")
    card = localize_closure_card(public_card(visible_state), state["output_language"])
    basis_receipt = None
    if state.get("whole_manuscript_basis") in {"SUFFICIENT", "INSUFFICIENT"}:
        basis_receipt = {
            "whole_manuscript_basis": state["whole_manuscript_basis"],
            "basis_reason_codes": list(state.get("whole_manuscript_basis_reason_codes", [])),
            "basis_explanation": str(state.get("whole_manuscript_basis_explanation", "")),
            "basis_contract_version": MANUSCRIPT_BASIS_CONTRACT_VERSION,
        }
    consent_receipt = state.get("provider_transmission_consent_receipt")
    receipt = minimal_receipt(
        machine_decision,
        state["current_manuscript_identity"],
        artifact_sha256=document.artifact_sha256,
        semantic_content_sha256=document.semantic_content_sha256,
        failed_stage=(
            state.get("technical_failed_stage")
            if state.get("technical_execution_hold") is True
            else None
        ),
        basis_receipt=basis_receipt,
        consent_receipt=(
            consent_receipt if isinstance(consent_receipt, Mapping) else None
        ),
    )
    safe_provider_receipts = tuple(deepcopy(dict(item)) for item in provider_receipts)
    physical_receipts = _flatten_physical_receipts(safe_provider_receipts)
    usage_calls = tuple(
        _receipt_usage(item)
        for item in physical_receipts
        if item.get("usage_status") != "UNKNOWN"
    )
    usage_stages = tuple(
        str(item.get("stage"))
        for item in physical_receipts
        if item.get("usage_status") != "UNKNOWN"
    )
    api_called = any(bool(item.get("request_dispatched")) for item in physical_receipts)
    status = dict(run_status or {})
    if not status:
        status = _status(
            machine_status="HOLD" if card["Verdict"] == "UNASSESSED" else "SUCCEEDED",
            presentation_status="NOT_STARTED" if card["Verdict"] == "UNASSESSED" else "PASS",
            terminal_status="HOLD" if card["Verdict"] == "UNASSESSED" else "PASS",
            recoverability="NONE",
            machine_provider_outcome=_provider_outcome_for_machine(safe_provider_receipts),
            presentation_provider_outcome="NOT_CALLED",
            usage_status_value=_usage_status_for_receipts(safe_provider_receipts),
        )
    return AnalysisResult(
        closure_card=card,
        minimal_receipt=receipt,
        provider=provider,
        model=model,
        reasoning_option=reasoning_option,
        api_called=api_called,
        usage=_aggregate_usage(usage_calls),
        usage_calls=usage_calls,
        provider_receipts=safe_provider_receipts,
        usage_call_stages=usage_stages,
        attempts=attempts,
        artifact_sha256=document.artifact_sha256,
        semantic_content_sha256=document.semantic_content_sha256,
        character_count=document.character_count,
        thread_id=sink.thread_id,
        harness=dict(harness_state),
        run_status=status,
        machine_receipt=deepcopy(dict(machine_receipt or {})),
        presentation_receipt=deepcopy(dict(presentation_receipt or {})),
        consent_receipt=deepcopy(
            dict(consent_receipt) if isinstance(consent_receipt, Mapping) else {}
        ),
    )


def _request_stage(
    client: ChatCompletionClient,
    messages: list[dict[str, str]],
    *,
    reasoning_option: str,
    schema: Mapping[str, Any],
    schema_name: str,
    budget: ContextBudgetReceipt,
    stage: str,
    coverage_digest: str | None = None,
) -> CompletionResult:
    if not budget.passed:
        raise HarnessContractError("model context budget cannot hold the complete stage input and output reserve")
    validate_schema_definition(schema)
    completion = client.complete(
        messages,
        reasoning_option=reasoning_option,
        json_mode=True,
        json_schema=schema,
        json_schema_name=schema_name,
        max_output_tokens=budget.requested_max_output_tokens,
        stage=stage,
        schema_sha256=schema_sha256(schema),
        coverage_digest_sha256=coverage_digest,
    )
    if completion.finish_reason == "length":
        raise ModelContractError(f"provider truncated {schema_name} at its output limit")
    return completion


def _machine_receipt_success(
    state: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    coverage_digest = canonical_digest(coverage)
    digest = machine_state_digest(state, coverage_digest_sha256=coverage_digest)
    source = build_presentation_source(state)
    decision = decide_state(state)
    adjudication_schema = build_adjudication_json_schema(coverage)
    return {
        "contract_version": "mrc-machine-receipt-3.0",
        "status": "SUCCEEDED",
        "coverage_digest_sha256": coverage_digest,
        "adjudication_contract_version": ADJUDICATION_CONTRACT_VERSION,
        "adjudication_schema_sha256": schema_sha256(adjudication_schema),
        "candidate_binding_contract_version": CANDIDATE_BINDING_CONTRACT_VERSION,
        "candidate_binding": candidate_binding_receipt(coverage, state),
        "affirmative_stop_contract_version": AFFIRMATIVE_STOP_CONTRACT_VERSION,
        "affirmative_stop_gate": affirmative_stop_gate_receipt(coverage, state),
        "contradiction_gate_passed": True,
        "machine_state_digest_sha256": digest,
        "machine_state_digest_after_presentation_sha256": digest,
        "machine_state_parity": True,
        "deterministic_verdict": decision["verdict"],
        "reason_category": decision["reason_category"],
        "whole_manuscript_basis": coverage["whole_manuscript_basis"],
        "basis_reason_codes": list(coverage["basis_reason_codes"]),
        "basis_explanation": coverage["basis_explanation"],
        "basis_contract_version": MANUSCRIPT_BASIS_CONTRACT_VERSION,
        "provider_transmission_consent": deepcopy(
            state.get("provider_transmission_consent_receipt", {})
        ),
        "evidence_hold_codes": list(decision["evidence_hold_codes"]),
        "submission_hold_codes": list(decision["submission_hold_codes"]),
        "machine_state_contract_version": machine_state_payload(
            state,
            coverage_digest_sha256=coverage_digest,
        )["contract_version"],
        "authoritative_presentation_source": source.bounded_receipt(),
    }


def _machine_receipt_hold(
    state: Mapping[str, Any],
    coverage: Mapping[str, Any] | None,
    *,
    failed_stage: str,
    error_code: str,
    error_message: str,
    candidate_state: Mapping[str, Any] | None = None,
    diagnostic_receipt: Mapping[str, Any] | None = None,
    provider_error_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    coverage_digest = canonical_digest(coverage) if coverage is not None else None
    candidate_digest = None
    if candidate_state is not None:
        candidate_full = deepcopy(dict(state))
        candidate_full.update(deepcopy(dict(candidate_state)))
        candidate_digest = machine_state_digest(
            candidate_full,
            coverage_digest_sha256=coverage_digest,
        )
    decision = decide_state(state)
    result = {
        "contract_version": "mrc-machine-receipt-3.0",
        "status": "HOLD",
        "failed_stage": failed_stage,
        "error_code": error_code,
        "error_message": error_message,
        "reason_category": decision["reason_category"],
        "provider_transmission_consent": deepcopy(
            state.get("provider_transmission_consent_receipt", {})
        ),
        "next_permitted_action": decision["next_permitted_action"],
        "coverage_digest_sha256": coverage_digest,
        "candidate_state_digest_sha256": candidate_digest,
        "contradiction_gate_passed": False,
        "authoritative_presentation_source": None,
        "authoritative_candidate_state": False,
    }
    if isinstance(diagnostic_receipt, Mapping):
        result["bounded_contract_failure"] = deepcopy(dict(diagnostic_receipt))
    if coverage is not None:
        result.update(
            {
                "whole_manuscript_basis": coverage["whole_manuscript_basis"],
                "basis_reason_codes": list(coverage["basis_reason_codes"]),
                "basis_explanation": coverage["basis_explanation"],
                "basis_contract_version": MANUSCRIPT_BASIS_CONTRACT_VERSION,
            }
        )
    if decision["reason_category"] == "TECHNICAL_EXECUTION_HOLD":
        result["technical_hold_contract_version"] = TECHNICAL_HOLD_CONTRACT_VERSION
    if isinstance(provider_error_fields, Mapping):
        for key in (
            "provider_error_detail_contract_version",
            "provider_error_status",
            "provider_error_code",
            "provider_error_detail",
        ):
            if key in provider_error_fields:
                result[key] = deepcopy(provider_error_fields[key])
    return result


def _hold_result(
    document: DocumentContent,
    state: dict[str, Any],
    sink: EventSink,
    *,
    options: RunOptions,
    provider_receipts: Sequence[Mapping[str, Any]],
    attempts: int,
    intake: IntakeReceipt,
    budgets: Sequence[ContextBudgetReceipt],
    coverage: Mapping[str, Any] | None,
    failed_stage: str,
    error: BaseException,
    candidate_state: Mapping[str, Any] | None = None,
) -> AnalysisResult:
    state["whole_manuscript_read"] = False
    state["technical_execution_hold"] = True
    state["technical_failed_stage"] = failed_stage
    if coverage is not None:
        state["evidence_hold_codes"] = list(coverage.get("evidence_hold_codes", []))
        state["submission_hold_codes"] = list(coverage.get("submission_hold_codes", []))
    physical_receipts = _flatten_physical_receipts(provider_receipts)
    provider_error_fields = physical_receipts[-1] if physical_receipts else {}
    machine_receipt = _machine_receipt_hold(
        state,
        coverage,
        failed_stage=failed_stage,
        error_code=str(getattr(error, "error_code", type(error).__name__)),
        error_message=str(error),
        candidate_state=candidate_state,
        diagnostic_receipt=getattr(error, "contract_receipt", None),
        provider_error_fields=provider_error_fields,
    )
    status = _status(
        machine_status="HOLD",
        presentation_status="NOT_STARTED",
        terminal_status="HOLD",
        recoverability="NONE",
        machine_provider_outcome=_provider_outcome_for_machine(provider_receipts),
        presentation_provider_outcome="NOT_CALLED",
        usage_status_value=_usage_status_for_receipts(provider_receipts),
    )
    result = _finish(
        document,
        state,
        sink,
        provider=options.provider if provider_receipts else None,
        model=(provider_receipts[-1].get("model") if provider_receipts else options.model),
        reasoning_option=options.reasoning_option,
        provider_receipts=provider_receipts,
        attempts=attempts,
        harness_state=harness_receipt(
            intake,
            budgets,
            coverage=coverage,
            adjudication_bound=candidate_state is not None,
            contradiction_gate_passed=False,
        ),
        run_status=status,
        machine_receipt=machine_receipt,
        presentation_receipt={
            "transaction_version": PRESENTATION_TRANSACTION_VERSION,
            "status": "NOT_STARTED",
            "error_code": "MACHINE_STATE_NOT_COMMITTED",
            "repair_attempted": False,
            "presentation_provider_outcome": "NOT_CALLED",
            "usage": {},
            "usage_status": "UNKNOWN",
        },
    )
    return result


def analyze_manuscript(options: RunOptions, *, event_sink: EventSink | None = None) -> AnalysisResult:
    sink = event_sink or EventSink()
    sink.start()
    intake: IntakeReceipt | None = None
    document: DocumentContent | None = None
    budgets: list[ContextBudgetReceipt] = []
    coverage: dict[str, Any] | None = None
    provider_receipts: list[dict[str, Any]] = []
    attempts: list[tuple[str, int]] = []
    current_stage = "created"
    current_timeout = 0.0
    current_provider = "NOT_CONFIGURED"
    current_model = "NOT_CONFIGURED"
    current_reasoning = "default"

    def on_attempt(number: int) -> None:
        attempts.append((current_stage, number))
        sink.emit(
            "provider.attempt",
            phase=sink.phase.value,
            stage=current_stage,
            provider=current_provider,
            model=current_model,
            reasoning_option=current_reasoning,
            attempt=number,
            timeout_seconds=current_timeout,
            max_transient_retries=0,
            retry_decision="STOP_NO_AUTOMATIC_RETRY",
        )

    def emit_provider_result(stage_receipt: Mapping[str, Any]) -> None:
        physical = stage_receipt.get("physical_request_receipts", [])
        final = physical[-1] if isinstance(physical, list) and physical else {}
        sink.emit(
            "provider.result",
            phase=sink.phase.value,
            stage=stage_receipt.get("stage"),
            provider=stage_receipt.get("provider"),
            model=stage_receipt.get("model"),
            reasoning_option=stage_receipt.get("reasoning_option"),
            attempt=final.get("physical_attempt_number"),
            timeout_seconds=stage_receipt.get("stage_timeout_seconds"),
            max_transient_retries=0,
            retry_decision=final.get("retry_decision", "STOP_NO_AUTOMATIC_RETRY"),
            provider_outcome=stage_receipt.get("provider_outcome"),
            http_status=final.get("http_status"),
            error_class=final.get("error_class"),
            provider_error_status=final.get("provider_error_status"),
            provider_error_code=final.get("provider_error_code"),
            provider_error_detail=final.get("provider_error_detail"),
            usage_status=final.get("usage_status"),
        )

    try:
        sink.transition(RunPhase.READING)
        item_id = sink.item_started("document_read", "Read immutable manuscript and validate structure")
        document = read_document(options.manuscript_path)
        intake = analyze_intake_structure(document.text)
        sink.item_completed(
            item_id,
            "document_read",
            character_count=document.character_count,
            artifact_sha256=document.artifact_sha256,
            semantic_content_sha256=document.semantic_content_sha256,
            intake_contract_version=intake.contract_version,
            complete_structure=intake.complete_structure,
            heading_count=intake.heading_count,
            advisory_codes=list(intake.advisory_codes),
        )
        state = _base_state(document, options, intake)
        initial_harness = harness_receipt(intake, budgets)

        if not state["critical_basis_available"]:
            sink.transition(RunPhase.VALIDATING)
            gate_item = sink.item_started("intake_gate", "Block locally unusable empty text before provider routing")
            error = HarnessContractError("local technical preflight found no effective text")
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=None,
                failed_stage="local_preflight",
                error=error,
            )
            sink.item_completed(gate_item, "intake_gate", verdict="UNASSESSED", api_called=False)
            sink.complete(
                verdict="UNASSESSED",
                machine_status="HOLD",
                presentation_status="NOT_STARTED",
                terminal_status="HOLD",
                usage={},
            )
            return result

        if options.prior_receipt is not None:
            prior_decision = decide_state(state)
            if prior_decision["prior_receipt_valid"]:
                sink.transition(RunPhase.VALIDATING)
                reuse_item = sink.item_started("receipt_reuse", "Reuse stable prior closure receipt")
                result = _finish(
                    document,
                    state,
                    sink,
                    provider=None,
                    model=None,
                    reasoning_option=None,
                    harness_state=initial_harness,
                    run_status=_status(
                        machine_status="SUCCEEDED",
                        presentation_status="PASS",
                        terminal_status="PASS",
                        recoverability="NONE",
                        machine_provider_outcome="NOT_CALLED",
                        presentation_provider_outcome="NOT_CALLED",
                        usage_status_value="UNKNOWN",
                    ),
                )
                sink.item_completed(reuse_item, "receipt_reuse", verdict=result.closure_card["Verdict"])
                sink.complete(
                    verdict=result.closure_card["Verdict"],
                    machine_status="SUCCEEDED",
                    presentation_status="PASS",
                    terminal_status="PASS",
                    usage={},
                )
                return result

        try:
            if options.transient_retries != 0:
                raise ProviderConfigurationError(
                    "automatic full-request retries are disabled; transient_retries must be zero"
                )
            provider_spec, selected_model = resolve_provider_selection(
                options.provider,
                model=options.model,
            )
            reasoning_option = validate_reasoning_option(
                provider_spec.name,
                selected_model,
                options.reasoning_option,
            )
        except ProviderConfigurationError as exc:
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=None,
                failed_stage="provider_configuration",
                error=exc,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage={})
            return result
        current_provider = provider_spec.name
        current_model = selected_model
        current_reasoning = reasoning_option
        consent_authorized, consent_receipt = _provider_transmission_consent_receipt(
            options.provider_transmission_consent,
            artifact_sha256=document.artifact_sha256,
            provider=provider_spec.name,
            model=selected_model,
        )
        state["provider_transmission_consent_receipt"] = consent_receipt
        state["provider_transmission_authorized"] = consent_authorized
        if not consent_authorized:
            sink.transition(RunPhase.VALIDATING)
            consent_item = sink.item_started(
                "provider_consent",
                "Stop because provider transmission was not authorized for this run",
            )
            result = _finish(
                document,
                state,
                sink,
                provider=provider_spec.name,
                model=selected_model,
                reasoning_option=reasoning_option,
                harness_state=initial_harness,
                run_status=_status(
                    machine_status="NOT_STARTED",
                    presentation_status="NOT_STARTED",
                    terminal_status="CANCELED",
                    recoverability="USER_CONFIRMATION_REQUIRED",
                    machine_provider_outcome="NOT_CALLED",
                    presentation_provider_outcome="NOT_CALLED",
                    usage_status_value="UNKNOWN",
                ),
                machine_receipt={
                    "contract_version": "mrc-machine-receipt-3.0",
                    "status": "NOT_STARTED",
                    "reason_category": "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED",
                    "error_code": "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED",
                    "contradiction_gate_passed": False,
                    "authoritative_presentation_source": None,
                    "provider_transmission_consent": consent_receipt,
                },
                presentation_receipt={
                    "transaction_version": PRESENTATION_TRANSACTION_VERSION,
                    "status": "NOT_STARTED",
                    "repair_attempted": False,
                    "presentation_provider_outcome": "NOT_CALLED",
                },
            )
            sink.item_completed(consent_item, "provider_consent", status="not_authorized")
            sink.complete(
                verdict="UNASSESSED",
                machine_status="NOT_STARTED",
                presentation_status="NOT_STARTED",
                terminal_status="CANCELED",
                usage={},
            )
            return result
        try:
            config = load_provider_config(options.provider, model=selected_model)
        except ProviderConfigurationError as exc:
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=None,
                failed_stage="provider_configuration",
                error=exc,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage={})
            return result
        sink.transition(RunPhase.REQUESTING_MODEL)

        current_stage = "coverage"
        current_timeout = provider_stage_timeout_seconds(
            config.name,
            current_stage,
            override=options.timeout_seconds,
        )
        coverage_messages = build_coverage_messages(
            document.text,
            manuscript_identity=state["current_manuscript_identity"],
        )
        coverage_budget = context_budget(
            coverage_messages,
            provider=config.name,
            model=config.model,
        )
        budgets.append(coverage_budget)
        if not coverage_budget.passed:
            sink.transition(RunPhase.VALIDATING)
            error = HarnessContractError("coverage context budget cannot hold the complete input")
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=None,
                failed_stage="coverage_context_budget",
                error=error,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage=result.usage)
            return result

        coverage_item = sink.item_started(
            "coverage_request",
            "Run whole-manuscript ten-dimension coverage pass",
            provider=config.name,
            model=config.model,
            reasoning_option=reasoning_option,
            estimated_input_tokens=coverage_budget.estimated_input_tokens,
            max_output_tokens=coverage_budget.requested_max_output_tokens,
            timeout_seconds=current_timeout,
        )
        coverage_client = ChatCompletionClient(
            config,
            timeout_seconds=current_timeout,
            max_transient_retries=0,
            on_attempt=on_attempt,
        )
        try:
            coverage_completion = _request_stage(
                coverage_client,
                coverage_messages,
                reasoning_option=reasoning_option,
                schema=COVERAGE_JSON_SCHEMA,
                schema_name="mrc_whole_manuscript_coverage",
                budget=coverage_budget,
                stage="coverage",
            )
        except SchemaDefinitionError as exc:
            sink.item_completed(
                coverage_item,
                "coverage_request",
                status="hold",
                error_code="SCHEMA_DEFINITION_INVALID",
                request_dispatched=False,
            )
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=None,
                failed_stage="coverage_schema_definition",
                error=exc,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage={})
            return result
        except ProviderRequestError as exc:
            stage_receipt = _provider_receipt_failed(
                    "coverage",
                    exc,
                    provider=config.name,
                    requested_model=config.model,
                    reasoning_option=reasoning_option,
                    attempt_count=sum(stage == "coverage" for stage, _number in attempts),
                    timeout_seconds=current_timeout,
                    budget=coverage_budget.as_dict(),
                    schema_digest=schema_sha256(COVERAGE_JSON_SCHEMA),
                    coverage_digest=None,
                )
            provider_receipts.append(stage_receipt)
            emit_provider_result(stage_receipt)
            sink.item_completed(coverage_item, "coverage_request", status="hold", error_code="PROVIDER_REQUEST_FAILED")
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=None,
                failed_stage="coverage_provider",
                error=exc,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage=result.usage)
            return result

        stage_receipt = _provider_receipt_completed(
                "coverage",
                coverage_completion,
                provider=config.name,
                requested_model=config.model,
                reasoning_option=reasoning_option,
                attempt_count=sum(stage == "coverage" for stage, _number in attempts),
                timeout_seconds=current_timeout,
                budget=coverage_budget.as_dict(),
                schema_digest=schema_sha256(COVERAGE_JSON_SCHEMA),
                coverage_digest=None,
            )
        provider_receipts.append(stage_receipt)
        emit_provider_result(stage_receipt)
        try:
            coverage = validate_coverage(parse_model_json(coverage_completion.content))
        except (ModelContractError, HarnessContractError) as exc:
            sink.item_completed(coverage_item, "coverage_request", status="hold", error_code=type(exc).__name__)
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=None,
                failed_stage="coverage_contract",
                error=exc,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage=result.usage)
            return result
        basis_receipt = {
            "whole_manuscript_basis": coverage["whole_manuscript_basis"],
            "basis_reason_codes": list(coverage["basis_reason_codes"]),
            "basis_explanation": coverage["basis_explanation"],
            "basis_contract_version": MANUSCRIPT_BASIS_CONTRACT_VERSION,
        }
        state["whole_manuscript_basis"] = coverage["whole_manuscript_basis"]
        state["whole_manuscript_basis_reason_codes"] = list(coverage["basis_reason_codes"])
        state["whole_manuscript_basis_explanation"] = coverage["basis_explanation"]
        stage_receipt.update(deepcopy(basis_receipt))
        for physical_receipt in stage_receipt.get("physical_request_receipts", []):
            if isinstance(physical_receipt, dict):
                physical_receipt.update(deepcopy(basis_receipt))
        if coverage["whole_manuscript_basis"] == "INSUFFICIENT":
            state["manuscript_complete"] = False
            state["whole_manuscript_read"] = False
            state["critical_basis_available"] = False
            state["bounded_scope"] = True
            sink.item_completed(
                coverage_item,
                "coverage_request",
                provider=config.name,
                model=coverage_completion.model,
                attempts=sum(stage == "coverage" for stage, _number in attempts),
                usage=coverage_completion.usage,
                coverage_contract_version=COVERAGE_CONTRACT_VERSION,
                whole_manuscript_basis="INSUFFICIENT",
                adjudication_requests=0,
            )
            sink.transition(RunPhase.VALIDATING)
            machine_receipt = {
                "contract_version": "mrc-machine-receipt-3.0",
                "status": "NOT_FORMED",
                "reason_category": "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS",
                "coverage_digest_sha256": canonical_digest(coverage),
                "contradiction_gate_passed": False,
                "authoritative_presentation_source": None,
                "authoritative_candidate_state": False,
                "provider_transmission_consent": deepcopy(consent_receipt),
                **deepcopy(basis_receipt),
            }
            run_status = _status(
                machine_status="NOT_FORMED",
                presentation_status="NOT_STARTED",
                terminal_status="HOLD",
                recoverability="NEW_WHOLE_MANUSCRIPT_BASIS_REQUIRED",
                machine_provider_outcome="SUCCEEDED",
                presentation_provider_outcome="NOT_CALLED",
                usage_status_value=_usage_status_for_receipts(provider_receipts),
            )
            result = _finish(
                document,
                state,
                sink,
                provider=config.name,
                model=coverage_completion.model,
                reasoning_option=reasoning_option,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                harness_state=harness_receipt(
                    intake,
                    budgets,
                    coverage=coverage,
                    adjudication_bound=False,
                    contradiction_gate_passed=False,
                ),
                run_status=run_status,
                machine_receipt=machine_receipt,
                presentation_receipt={
                    "transaction_version": PRESENTATION_TRANSACTION_VERSION,
                    "status": "NOT_STARTED",
                    "repair_attempted": False,
                    "presentation_provider_outcome": "NOT_CALLED",
                    "usage": {},
                    "usage_status": "UNKNOWN",
                },
            )
            sink.complete(
                verdict="UNASSESSED",
                machine_status="NOT_FORMED",
                presentation_status="NOT_STARTED",
                terminal_status="HOLD",
                usage=result.usage,
            )
            return result
        coverage["submission_hold_codes"] = list(
            dict.fromkeys([*coverage["submission_hold_codes"], *document.submission_hold_codes])
        )
        sink.item_completed(
            coverage_item,
            "coverage_request",
            provider=config.name,
            model=coverage_completion.model,
            attempts=sum(stage == "coverage" for stage, _number in attempts),
            usage=coverage_completion.usage,
            coverage_contract_version=COVERAGE_CONTRACT_VERSION,
            dimension_count=len(coverage["dimensions"]),
            coverage_complete=coverage_is_complete(coverage),
        )
        if not coverage_is_complete(coverage):
            sink.transition(RunPhase.VALIDATING)
            error = HarnessContractError("coverage did not assess every required dimension")
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=coverage,
                failed_stage="coverage_incomplete",
                error=error,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage=result.usage)
            return result

        schema_item = sink.item_started(
            "adjudication_schema_definition",
            "Build and lint the coverage-bound adjudication schema before dispatch",
            provider=config.name,
            model=config.model,
            request_dispatched=False,
        )
        try:
            adjudication_schema = build_adjudication_json_schema(coverage)
        except SchemaDefinitionError as exc:
            sink.item_completed(
                schema_item,
                "adjudication_schema_definition",
                status="hold",
                error_code="SCHEMA_DEFINITION_INVALID",
                request_dispatched=False,
            )
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=coverage,
                failed_stage="adjudication_schema_definition",
                error=exc,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage=result.usage)
            return result
        sink.item_completed(
            schema_item,
            "adjudication_schema_definition",
            status="pass",
            schema_sha256=schema_sha256(adjudication_schema),
            request_dispatched=False,
        )
        frozen_coverage_digest = canonical_digest(coverage)
        adjudication_messages = build_adjudication_messages(
            document.text,
            manuscript_identity=state["current_manuscript_identity"],
            output_language=options.output_language,
            coverage=coverage,
        )
        adjudication_budget = context_budget(
            adjudication_messages,
            provider=config.name,
            model=config.model,
        )
        budgets.append(adjudication_budget)
        if not adjudication_budget.passed:
            sink.transition(RunPhase.VALIDATING)
            error = HarnessContractError("adjudication context budget cannot hold the complete input")
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=coverage,
                failed_stage="adjudication_context_budget",
                error=error,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage=result.usage)
            return result

        current_stage = "adjudication"
        current_timeout = provider_stage_timeout_seconds(
            config.name,
            current_stage,
            override=options.timeout_seconds,
        )
        adjudication_item = sink.item_started(
            "adjudication_request",
            "Re-read manuscript and adjudicate bound root-cause candidates",
            provider=config.name,
            model=config.model,
            reasoning_option=reasoning_option,
            estimated_input_tokens=adjudication_budget.estimated_input_tokens,
            max_output_tokens=adjudication_budget.requested_max_output_tokens,
            timeout_seconds=current_timeout,
        )
        adjudication_client = ChatCompletionClient(
            config,
            timeout_seconds=current_timeout,
            max_transient_retries=0,
            on_attempt=on_attempt,
        )
        try:
            adjudication_completion = _request_stage(
                adjudication_client,
                adjudication_messages,
                reasoning_option=reasoning_option,
                schema=adjudication_schema,
                schema_name="mrc_root_cause_adjudication",
                budget=adjudication_budget,
                stage="adjudication",
                coverage_digest=frozen_coverage_digest,
            )
        except SchemaDefinitionError as exc:
            sink.item_completed(
                adjudication_item,
                "adjudication_request",
                status="hold",
                error_code="SCHEMA_DEFINITION_INVALID",
                request_dispatched=False,
            )
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=coverage,
                failed_stage="adjudication_schema_definition",
                error=exc,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage=result.usage)
            return result
        except ProviderRequestError as exc:
            stage_receipt = _provider_receipt_failed(
                    "adjudication",
                    exc,
                    provider=config.name,
                    requested_model=config.model,
                    reasoning_option=reasoning_option,
                    attempt_count=sum(stage == "adjudication" for stage, _number in attempts),
                    timeout_seconds=current_timeout,
                    budget=adjudication_budget.as_dict(),
                    schema_digest=schema_sha256(adjudication_schema),
                    coverage_digest=frozen_coverage_digest,
                )
            provider_receipts.append(stage_receipt)
            emit_provider_result(stage_receipt)
            sink.item_completed(adjudication_item, "adjudication_request", status="hold", error_code="PROVIDER_REQUEST_FAILED")
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=coverage,
                failed_stage="adjudication_provider",
                error=exc,
            )
            sink.complete(verdict="UNASSESSED", terminal_status="HOLD", usage=result.usage)
            return result

        stage_receipt = _provider_receipt_completed(
                "adjudication",
                adjudication_completion,
                provider=config.name,
                requested_model=config.model,
                reasoning_option=reasoning_option,
                attempt_count=sum(stage == "adjudication" for stage, _number in attempts),
                timeout_seconds=current_timeout,
                budget=adjudication_budget.as_dict(),
                schema_digest=schema_sha256(adjudication_schema),
                coverage_digest=frozen_coverage_digest,
            )
        provider_receipts.append(stage_receipt)
        emit_provider_result(stage_receipt)
        try:
            envelope = parse_model_json(adjudication_completion.content)
            try:
                validate_json_schema_contract(
                    envelope,
                    adjudication_schema,
                    contract_version=ADJUDICATION_CONTRACT_VERSION,
                )
            except HarnessContractError as schema_error:
                diagnostic = getattr(schema_error, "contract_receipt", None)
                if isinstance(diagnostic, dict) and isinstance(envelope.get("material_root_causes"), list):
                    candidate_diagnostic = candidate_binding_receipt(coverage, envelope)
                    diagnostic["candidate_binding_contract_version"] = candidate_diagnostic.pop(
                        "contract_version"
                    )
                    diagnostic.update(candidate_diagnostic)
                raise
            validate_candidate_binding(coverage, envelope)
            finite_state = validate_adjudication_binding(envelope, coverage)
            model_state = validate_model_state(finite_state)
        except (ModelContractError, HarnessContractError) as exc:
            sink.item_completed(
                adjudication_item,
                "adjudication_request",
                status="hold",
                error_code=type(exc).__name__,
            )
            sink.transition(RunPhase.VALIDATING)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=coverage,
                failed_stage="adjudication_contract",
                error=exc,
            )
            sink.complete(
                verdict="UNASSESSED",
                machine_status="HOLD",
                presentation_status="NOT_STARTED",
                terminal_status="HOLD",
                usage=result.usage,
            )
            return result
        sink.item_completed(
            adjudication_item,
            "adjudication_request",
            provider=config.name,
            model=adjudication_completion.model,
            attempts=sum(stage == "adjudication" for stage, _number in attempts),
            usage=adjudication_completion.usage,
            adjudication_contract_version=ADJUDICATION_CONTRACT_VERSION,
            coverage_binding=True,
        )

        sink.transition(RunPhase.VALIDATING)
        contradiction_item = sink.item_started(
            "contradiction_gate",
            "Independently verify cross-stage finite-state consistency",
        )
        try:
            validate_cross_stage_consistency(coverage, model_state)
            stop_gate_receipt = affirmative_stop_gate_receipt(coverage, model_state)
        except HarnessContractError as exc:
            sink.item_completed(contradiction_item, "contradiction_gate", status="hold", error_code=type(exc).__name__)
            result = _hold_result(
                document,
                state,
                sink,
                options=options,
                provider_receipts=provider_receipts,
                attempts=len(attempts),
                intake=intake,
                budgets=budgets,
                coverage=coverage,
                failed_stage="contradiction_gate",
                error=exc,
                candidate_state=model_state,
            )
            sink.complete(
                verdict="UNASSESSED",
                machine_status="HOLD",
                presentation_status="NOT_STARTED",
                terminal_status="HOLD",
                usage=result.usage,
            )
            return result

        state.update(deepcopy(model_state))
        state["affirmative_stop_gate_passed"] = bool(stop_gate_receipt["stop_eligible"])
        state["submission_hold_codes"] = list(
            dict.fromkeys([*model_state["submission_hold_codes"], *document.submission_hold_codes])
        )
        machine_receipt = _machine_receipt_success(state, coverage)
        # The deterministic reducer executes before language validation.
        initial_decision = decide_state(state)
        _ = localize_closure_card(public_card(state), state["output_language"])
        sink.item_completed(
            contradiction_item,
            "contradiction_gate",
            verdict=initial_decision["verdict"],
            coverage_binding=True,
            contradiction_gate_passed=True,
            machine_state_digest_sha256=machine_receipt["machine_state_digest_sha256"],
        )

        language_assessment = assess_presentation_language(
            state,
            target_language=options.output_language,
        )
        presentation_result: PresentationRepairResult
        if language_assessment.passed or options.output_language != "zh":
            presentation_result = presentation_pass_without_repair(
                state,
                target_language=options.output_language,
                coverage_digest_sha256=machine_receipt["coverage_digest_sha256"],
                language_assessment=language_assessment,
            )
        elif options.enable_presentation_repair:
            sink.transition(RunPhase.PRESENTING)
            presentation_item = sink.item_started(
                "presentation_repair",
                "Repair only bounded public presentation text",
                provider=config.name,
                model=config.model,
                max_transient_retries=0,
            )
            current_stage = "presentation_repair"
            current_timeout = provider_stage_timeout_seconds(
                config.name,
                current_stage,
                override=options.timeout_seconds,
            )

            def on_presentation_attempt(number: int) -> None:
                attempts.append(("presentation_repair", number))
                sink.emit(
                    "provider.attempt",
                    phase=sink.phase.value,
                    stage="presentation_repair",
                    provider=config.name,
                    model=config.model,
                    reasoning_option=reasoning_option,
                    attempt=number,
                    timeout_seconds=current_timeout,
                    max_transient_retries=0,
                    retry_decision="STOP_NO_AUTOMATIC_RETRY",
                )

            presentation_result = repair_presentation(
                state,
                provider=config.name,
                model=config.model,
                reasoning_option=reasoning_option,
                target_language=options.output_language,
                coverage_digest_sha256=machine_receipt["coverage_digest_sha256"],
                timeout_seconds=options.timeout_seconds,
                on_attempt=on_presentation_attempt,
            )
            presentation_provider_receipt = _presentation_provider_receipt(
                presentation_result,
                provider=config.name,
                requested_model=config.model,
                reasoning_option=reasoning_option,
            )
            if presentation_provider_receipt is not None:
                provider_receipts.append(presentation_provider_receipt)
                emit_provider_result(presentation_provider_receipt)
            sink.item_completed(
                presentation_item,
                "presentation_repair",
                status="completed" if presentation_result.status == "PASS" else "hold",
                provider_outcome=presentation_result.provider_outcome,
                attempts=presentation_result.attempts,
                usage=presentation_result.usage,
                error_code=presentation_result.error_code,
                machine_state_parity=presentation_result.receipt.get("machine_state_parity"),
            )
        else:
            source = build_presentation_source(state)
            digest = machine_receipt["machine_state_digest_sha256"]
            presentation_result = PresentationRepairResult(
                status="HOLD",
                display_state=deepcopy(state),
                receipt={
                    "transaction_version": PRESENTATION_TRANSACTION_VERSION,
                    **source.bounded_receipt(),
                    "status": "HOLD",
                    "error_code": "PRESENTATION_LANGUAGE_HOLD",
                    "repair_attempted": False,
                    "presentation_provider_outcome": "NOT_CALLED",
                    "usage": {},
                    "usage_status": "UNKNOWN",
                    "language_assessment": language_assessment.as_dict(),
                    "machine_state_digest_before_sha256": digest,
                    "machine_state_digest_after_sha256": digest,
                    "machine_state_parity": True,
                },
                usage={},
                model=None,
                attempts=0,
                provider_outcome="NOT_CALLED",
                error_code="PRESENTATION_LANGUAGE_HOLD",
                error_message="requested Chinese output contains a non-Chinese public text value",
            )

        digest_after = machine_state_digest(
            state,
            coverage_digest_sha256=machine_receipt["coverage_digest_sha256"],
        )
        machine_receipt["machine_state_digest_after_presentation_sha256"] = digest_after
        machine_receipt["machine_state_parity"] = (
            digest_after == machine_receipt["machine_state_digest_sha256"]
        )
        if not machine_receipt["machine_state_parity"]:
            presentation_result = PresentationRepairResult(
                status="HOLD",
                display_state=deepcopy(state),
                receipt={
                    **presentation_result.receipt,
                    "status": "HOLD",
                    "error_code": "INTEGRITY_HOLD",
                    "machine_state_digest_after_sha256": digest_after,
                    "machine_state_parity": False,
                },
                usage=presentation_result.usage,
                model=presentation_result.model,
                attempts=presentation_result.attempts,
                provider_outcome=presentation_result.provider_outcome,
                error_code="INTEGRITY_HOLD",
                error_message="presentation processing changed the frozen machine state",
            )

        presentation_pass = presentation_result.status == "PASS"
        run_status = _status(
            machine_status="SUCCEEDED",
            presentation_status="PASS" if presentation_pass else "HOLD",
            terminal_status="PASS" if presentation_pass else "HOLD",
            recoverability="NONE" if presentation_pass else "PRESENTATION_REPAIR",
            machine_provider_outcome="SUCCEEDED",
            presentation_provider_outcome=presentation_result.provider_outcome,
            usage_status_value=_usage_status_for_receipts(provider_receipts),
        )
        final_harness = harness_receipt(
            intake,
            budgets,
            coverage=coverage,
            adjudication_bound=True,
            contradiction_gate_passed=True,
        )
        result = _finish(
            document,
            state,
            sink,
            provider=config.name,
            model=adjudication_completion.model,
            reasoning_option=reasoning_option,
            provider_receipts=provider_receipts,
            attempts=len(attempts),
            harness_state=final_harness,
            display_state=presentation_result.display_state,
            run_status=run_status,
            machine_receipt=machine_receipt,
            presentation_receipt=presentation_result.receipt,
        )
        sink.complete(
            verdict=result.closure_card["Verdict"],
            machine_status=run_status["machine_status"],
            presentation_status=run_status["presentation_status"],
            terminal_status=run_status["terminal_status"],
            recoverability=run_status["recoverability"],
            machine_provider_outcome=run_status["machine_provider_outcome"],
            presentation_provider_outcome=run_status["presentation_provider_outcome"],
            usage_status=run_status["usage_status"],
            usage=result.usage,
        )
        return result
    except (
        DocumentReadError,
        ProviderConfigurationError,
        ModelContractError,
        HarnessContractError,
        ClosureStateError,
        RuntimeError,
    ) as exc:
        sink.fail(type(exc).__name__, str(exc))
        raise
