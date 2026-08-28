"""Native multi-stage closure orchestration with transactional presentation repair."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.closure_state import (
    ClosureStateError,
    EVIDENCE_HOLD_CODES,
    SUBMISSION_HOLD_CODES,
    decide_state,
    minimal_receipt,
    public_card,
)

from . import __version__
from .document_reader import DocumentContent, DocumentReadError, read_document
from .events import EventSink, RunPhase
from .harness import (
    ADJUDICATION_CONTRACT_VERSION,
    COVERAGE_CONTRACT_VERSION,
    COVERAGE_DIMENSIONS,
    COVERAGE_JSON_SCHEMA,
    ContextBudgetReceipt,
    HarnessContractError,
    IntakeReceipt,
    analyze_intake_structure,
    canonical_digest,
    context_budget,
    coverage_is_complete,
    harness_receipt,
    validate_adjudication_binding,
    validate_coverage,
    validate_cross_stage_consistency,
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
from .prompting import (
    ADJUDICATION_JSON_SCHEMA,
    build_adjudication_messages,
    build_coverage_messages,
)
from .providers import (
    ChatCompletionClient,
    CompletionResult,
    ProviderConfigurationError,
    ProviderRequestError,
    load_provider_config,
    provider_stage_timeout_seconds,
    validate_reasoning_option,
)


MODEL_KEYS = frozenset(
    {
        "material_root_causes",
        "evidence_hold_codes",
        "submission_hold_codes",
        "protected",
        "parked_opportunities",
        "lite_suggestions",
    }
)
ROOT_CAUSE_KEYS = frozenset(
    {
        "observed",
        "locatable",
        "dimension",
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
    transient_retries: int = 2
    enable_presentation_repair: bool = True


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

    def as_dict(self) -> dict[str, Any]:
        status = dict(self.run_status)
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
                "provider_call_count": len(self.provider_receipts),
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
        for name in ROOT_CAUSE_KEYS - {"dimension", "scope"}:
            if not isinstance(cause[name], bool):
                raise ModelContractError(f"material root cause {name} must be boolean")
        dimension = cause["dimension"]
        if dimension not in COVERAGE_DIMENSIONS:
            raise ModelContractError("material root cause dimension must use the registered coverage set")
        if cause["scope"] not in {"local", "central"}:
            raise ModelContractError("material root cause scope must be local or central")
        clean_cause = {key: item for key, item in cause.items() if key != "dimension"}
        clean_causes.append({**clean_cause, "affects": [dimension]})
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
    complete = bool(options.confirm_complete_current_manuscript and intake.complete_structure)
    state: dict[str, Any] = {
        "manuscript_complete": complete,
        "current_identity_clear": bool(identity),
        "whole_manuscript_read": complete and document.critical_basis_available,
        "critical_basis_available": document.critical_basis_available and intake.complete_structure,
        "bounded_scope": not options.confirm_complete_current_manuscript,
        "current_manuscript_identity": identity,
        "current_artifact_sha256": document.artifact_sha256,
        "current_semantic_content_sha256": document.semantic_content_sha256,
        "material_root_causes": [],
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


def _provider_receipt_completed(
    stage: str,
    completion: CompletionResult,
    *,
    attempt_count: int,
    timeout_seconds: float,
    max_transient_retries: int,
    budget: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "stage": stage,
        "provider_called": True,
        "provider_outcome": "SUCCEEDED",
        "model": completion.model,
        "finish_reason": completion.finish_reason,
        "usage": dict(completion.usage),
        "usage_status": usage_status(completion.usage),
        "actual_attempt_count": attempt_count,
        "stage_timeout_seconds": timeout_seconds,
        "max_transient_retries": max_transient_retries,
        "budget": dict(budget),
        "committed_in_memory": True,
        "raw_provider_response_persisted": False,
    }


def _provider_receipt_failed(
    stage: str,
    error: ProviderRequestError,
    *,
    attempt_count: int,
    timeout_seconds: float,
    max_transient_retries: int,
    budget: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "stage": stage,
        "provider_called": True,
        "provider_outcome": provider_outcome_from_error(error),
        "model": None,
        "finish_reason": None,
        "usage": {},
        "usage_status": "UNKNOWN",
        "actual_attempt_count": attempt_count,
        "stage_timeout_seconds": timeout_seconds,
        "max_transient_retries": max_transient_retries,
        "budget": dict(budget),
        "error_code": "PROVIDER_REQUEST_FAILED",
        "error_message": str(error),
        "committed_in_memory": True,
        "raw_provider_response_persisted": False,
    }


def _presentation_provider_receipt(result: PresentationRepairResult) -> dict[str, Any] | None:
    receipt = result.receipt
    if not receipt.get("provider_called"):
        return None
    return {
        "stage": "presentation_repair",
        "provider_called": True,
        "provider_outcome": result.provider_outcome,
        "model": result.model,
        "finish_reason": receipt.get("finish_reason"),
        "usage": dict(result.usage),
        "usage_status": usage_status(result.usage),
        "actual_attempt_count": result.attempts,
        "stage_timeout_seconds": receipt.get("stage_timeout_seconds"),
        "max_transient_retries": 0,
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


def _usage_status_for_receipts(receipts: Sequence[Mapping[str, Any]]) -> str:
    attempted = sum(bool(item.get("provider_called")) for item in receipts)
    usages = [_receipt_usage(item) for item in receipts if item.get("provider_called")]
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
    receipt = minimal_receipt(
        machine_decision,
        state["current_manuscript_identity"],
        artifact_sha256=document.artifact_sha256,
        semantic_content_sha256=document.semantic_content_sha256,
    )
    safe_provider_receipts = tuple(deepcopy(dict(item)) for item in provider_receipts)
    usage_calls = tuple(_receipt_usage(item) for item in safe_provider_receipts if item.get("provider_called"))
    usage_stages = tuple(
        str(item.get("stage")) for item in safe_provider_receipts if item.get("provider_called")
    )
    api_called = any(bool(item.get("provider_called")) for item in safe_provider_receipts)
    status = dict(run_status or {})
    if not status:
        status = _status(
            machine_status="HOLD" if card["Verdict"] == "UNASSESSED" else "SUCCEEDED",
            presentation_status="HOLD" if card["Verdict"] == "UNASSESSED" else "PASS",
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
    )


def _request_stage(
    client: ChatCompletionClient,
    messages: list[dict[str, str]],
    *,
    reasoning_option: str,
    schema: Mapping[str, Any],
    schema_name: str,
    budget: ContextBudgetReceipt,
) -> CompletionResult:
    if not budget.passed:
        raise HarnessContractError("model context budget cannot hold the complete stage input and output reserve")
    completion = client.complete(
        messages,
        reasoning_option=reasoning_option,
        json_mode=True,
        json_schema=schema,
        json_schema_name=schema_name,
        max_output_tokens=budget.requested_max_output_tokens,
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
    return {
        "contract_version": "mrc-machine-receipt-2.0",
        "status": "SUCCEEDED",
        "coverage_digest_sha256": coverage_digest,
        "adjudication_contract_version": ADJUDICATION_CONTRACT_VERSION,
        "contradiction_gate_passed": True,
        "machine_state_digest_sha256": digest,
        "machine_state_digest_after_presentation_sha256": digest,
        "machine_state_parity": True,
        "deterministic_verdict": decision["verdict"],
        "reason_category": decision["reason_category"],
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
) -> dict[str, Any]:
    coverage_digest = canonical_digest(coverage) if coverage is not None else None
    candidate_digest = None
    source_receipt = None
    if candidate_state is not None:
        candidate_full = deepcopy(dict(state))
        candidate_full.update(deepcopy(dict(candidate_state)))
        candidate_digest = machine_state_digest(
            candidate_full,
            coverage_digest_sha256=coverage_digest,
        )
        source_receipt = build_presentation_source(candidate_full).bounded_receipt()
    return {
        "contract_version": "mrc-machine-receipt-2.0",
        "status": "HOLD",
        "failed_stage": failed_stage,
        "error_code": error_code,
        "error_message": error_message,
        "coverage_digest_sha256": coverage_digest,
        "candidate_state_digest_sha256": candidate_digest,
        "contradiction_gate_passed": False,
        "authoritative_presentation_source": source_receipt,
    }


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
    if coverage is not None:
        state["evidence_hold_codes"] = list(coverage.get("evidence_hold_codes", []))
        state["submission_hold_codes"] = list(coverage.get("submission_hold_codes", []))
    machine_receipt = _machine_receipt_hold(
        state,
        coverage,
        failed_stage=failed_stage,
        error_code=type(error).__name__,
        error_message=str(error),
        candidate_state=candidate_state,
    )
    status = _status(
        machine_status="HOLD",
        presentation_status="HOLD",
        terminal_status="HOLD",
        recoverability="NONE",
        machine_provider_outcome=_provider_outcome_for_machine(provider_receipts),
        presentation_provider_outcome="NOT_CALLED",
        usage_status_value=_usage_status_for_receipts(provider_receipts),
    )
    return _finish(
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
            "status": "HOLD",
            "error_code": "MACHINE_STATE_NOT_COMMITTED",
            "repair_attempted": False,
            "presentation_provider_outcome": "NOT_CALLED",
            "usage": {},
            "usage_status": "UNKNOWN",
        },
    )


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

    def on_attempt(number: int) -> None:
        attempts.append((current_stage, number))
        sink.emit(
            "provider.attempt",
            phase=sink.phase.value,
            stage=current_stage,
            attempt=number,
            timeout_seconds=current_timeout,
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
        )
        state = _base_state(document, options, intake)
        initial_harness = harness_receipt(intake, budgets)

        if not options.confirm_complete_current_manuscript or not state["critical_basis_available"]:
            sink.transition(RunPhase.VALIDATING)
            gate_item = sink.item_started("intake_gate", "Return fail-closed UNASSESSED intake state")
            result = _finish(
                document,
                state,
                sink,
                provider=None,
                model=None,
                reasoning_option=None,
                harness_state=initial_harness,
                run_status=_status(
                    machine_status="HOLD",
                    presentation_status="HOLD",
                    terminal_status="HOLD",
                    recoverability="NONE",
                    machine_provider_outcome="NOT_CALLED",
                    presentation_provider_outcome="NOT_CALLED",
                    usage_status_value="UNKNOWN",
                ),
                machine_receipt={
                    "contract_version": "mrc-machine-receipt-2.0",
                    "status": "HOLD",
                    "failed_stage": "intake_gate",
                    "error_code": "UNASSESSED_INTAKE",
                    "contradiction_gate_passed": False,
                },
                presentation_receipt={
                    "transaction_version": PRESENTATION_TRANSACTION_VERSION,
                    "status": "HOLD",
                    "repair_attempted": False,
                    "presentation_provider_outcome": "NOT_CALLED",
                },
            )
            sink.item_completed(gate_item, "intake_gate", verdict="UNASSESSED")
            sink.complete(
                verdict="UNASSESSED",
                machine_status="HOLD",
                presentation_status="HOLD",
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

        config = load_provider_config(options.provider, model=options.model)
        reasoning_option = validate_reasoning_option(config.name, config.model, options.reasoning_option)
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
            max_transient_retries=options.transient_retries,
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
            )
        except ProviderRequestError as exc:
            provider_receipts.append(
                _provider_receipt_failed(
                    "coverage",
                    exc,
                    attempt_count=sum(stage == "coverage" for stage, _number in attempts),
                    timeout_seconds=current_timeout,
                    max_transient_retries=options.transient_retries,
                    budget=coverage_budget.as_dict(),
                )
            )
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

        provider_receipts.append(
            _provider_receipt_completed(
                "coverage",
                coverage_completion,
                attempt_count=sum(stage == "coverage" for stage, _number in attempts),
                timeout_seconds=current_timeout,
                max_transient_retries=options.transient_retries,
                budget=coverage_budget.as_dict(),
            )
        )
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
            max_transient_retries=options.transient_retries,
            on_attempt=on_attempt,
        )
        try:
            adjudication_completion = _request_stage(
                adjudication_client,
                adjudication_messages,
                reasoning_option=reasoning_option,
                schema=ADJUDICATION_JSON_SCHEMA,
                schema_name="mrc_root_cause_adjudication",
                budget=adjudication_budget,
            )
        except ProviderRequestError as exc:
            provider_receipts.append(
                _provider_receipt_failed(
                    "adjudication",
                    exc,
                    attempt_count=sum(stage == "adjudication" for stage, _number in attempts),
                    timeout_seconds=current_timeout,
                    max_transient_retries=options.transient_retries,
                    budget=adjudication_budget.as_dict(),
                )
            )
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

        provider_receipts.append(
            _provider_receipt_completed(
                "adjudication",
                adjudication_completion,
                attempt_count=sum(stage == "adjudication" for stage, _number in attempts),
                timeout_seconds=current_timeout,
                max_transient_retries=options.transient_retries,
                budget=adjudication_budget.as_dict(),
            )
        )
        try:
            envelope = parse_model_json(adjudication_completion.content)
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
                presentation_status="HOLD",
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
                presentation_status="HOLD",
                terminal_status="HOLD",
                usage=result.usage,
            )
            return result

        state.update(deepcopy(model_state))
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
            presentation_provider_receipt = _presentation_provider_receipt(presentation_result)
            if presentation_provider_receipt is not None:
                provider_receipts.append(presentation_provider_receipt)
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
