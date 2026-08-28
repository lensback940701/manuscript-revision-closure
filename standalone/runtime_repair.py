"""Transactional repair layer for standalone 0.6.1.

The exact 0.6.1 assessor remains the semantic implementation.  This module
re-binds its runtime boundaries so a completed provider response, validated
machine adjudication, and usage receipt cannot be erased by a later
presentation failure.  It also provides one bounded presentation-only repair
without resending the manuscript.
"""

from __future__ import annotations

import contextvars
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from .harness import canonical_digest
from .presentation_repair import (
    PresentationRepairError,
    aggregate_usage_status,
    build_presentation_source,
    presentation_receipt_without_repair,
    repair_presentation,
)


RUNTIME_TRANSACTION_VERSION = "mrc-runtime-transaction-1.0"
MACHINE_RECEIPT_VERSION = "mrc-machine-receipt-1.0"


@dataclass(slots=True)
class _RunTrace:
    budgets: list[Any] = field(default_factory=list)
    coverage: dict[str, Any] | None = None
    machine_state: dict[str, Any] | None = None
    language_error: str | None = None
    contradiction_gate_passed: bool = False
    validation_stage: str = "created"
    calls: list[dict[str, Any]] = field(default_factory=list)
    attempt_count: int = 0
    pending_items: dict[str, str] = field(default_factory=dict)
    deferred_complete: dict[str, Any] | None = None
    deferred_failure: tuple[str, str] | None = None


_TRACE: contextvars.ContextVar[_RunTrace | None] = contextvars.ContextVar(
    "mrc_runtime_repair_trace",
    default=None,
)


@dataclass(frozen=True, slots=True)
class RepairedAnalysisResult:
    """Backward-compatible result with bounded transaction receipts."""

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
    run_status: dict[str, str] = field(default_factory=dict)
    machine_receipt: dict[str, Any] = field(default_factory=dict)
    presentation_receipt: dict[str, Any] = field(default_factory=dict)
    usage_call_stages: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        status = dict(self.run_status)
        runtime = {
            "provider": self.provider,
            "model": self.model,
            "reasoning_option": self.reasoning_option,
            "api_called": self.api_called,
            "usage": dict(self.usage),
            "usage_calls": [dict(item) for item in self.usage_calls],
            "usage_call_stages": list(self.usage_call_stages),
            "core_call_count": sum(
                stage in {"coverage", "adjudication"} for stage in self.usage_call_stages
            ),
            "presentation_repair_call_count": sum(
                stage == "presentation_repair" for stage in self.usage_call_stages
            ),
            "attempts": self.attempts,
            "thread_id": self.thread_id,
            "artifact_sha256": self.artifact_sha256,
            "semantic_content_sha256": self.semantic_content_sha256,
            "character_count": self.character_count,
            "standalone_version": "0.6.1",
            "skill_version": "0.2.1",
            "runtime_transaction_version": RUNTIME_TRANSACTION_VERSION,
            "status": status,
            "machine_status": status.get("machine_status"),
            "presentation_status": status.get("presentation_status"),
            "terminal_status": status.get("terminal_status"),
            "recoverability": status.get("recoverability"),
            "provider_outcome": status.get("provider_outcome"),
            "usage_status": status.get("usage_status"),
            "machine_receipt": deepcopy(self.machine_receipt),
            "presentation_receipt": deepcopy(self.presentation_receipt),
            "raw_provider_response_persisted": False,
            "automatic_result_file_written": False,
            "harness": deepcopy(self.harness),
        }
        return {
            "closure_card": deepcopy(self.closure_card),
            "minimal_receipt": deepcopy(self.minimal_receipt),
            "runtime": runtime,
        }


class _DeferredTerminalSink:
    """Delegate non-terminal events and let the transaction owner emit one terminal event."""

    def __init__(self, sink: Any, trace: _RunTrace) -> None:
        self._sink = sink
        self._trace = trace

    @property
    def thread_id(self) -> str:
        return self._sink.thread_id

    @property
    def phase(self) -> Any:
        return self._sink.phase

    @property
    def events(self) -> Any:
        return self._sink.events

    def start(self) -> None:
        self._sink.start()

    def transition(self, target: Any) -> None:
        self._sink.transition(target)

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        if event_type == "provider.attempt":
            self._trace.attempt_count += 1
        return self._sink.emit(event_type, **payload)

    def item_started(self, item_type: str, label: str, **metadata: Any) -> str:
        item_id = self._sink.item_started(item_type, label, **metadata)
        self._trace.pending_items[item_id] = item_type
        return item_id

    def item_completed(
        self,
        item_id: str,
        item_type: str,
        status: str = "completed",
        **metadata: Any,
    ) -> None:
        self._trace.pending_items.pop(item_id, None)
        self._sink.item_completed(item_id, item_type, status=status, **metadata)

    def complete(self, **metadata: Any) -> None:
        self._trace.deferred_complete = dict(metadata)

    def fail(self, error_code: str, message: str) -> None:
        self._trace.deferred_failure = (error_code, message)


def _trace() -> _RunTrace | None:
    return _TRACE.get()


def _call_stage(schema_name: str) -> str:
    if "coverage" in schema_name:
        return "coverage"
    if "adjudication" in schema_name:
        return "adjudication"
    return schema_name


def _usage_calls(trace: _RunTrace) -> tuple[dict[str, int], ...]:
    return tuple(dict(call["usage"]) for call in trace.calls)


def _usage_stages(trace: _RunTrace) -> tuple[str, ...]:
    return tuple(str(call["stage"]) for call in trace.calls)


def _aggregate_usage(assessor: Any, usages: Sequence[Mapping[str, int]]) -> dict[str, int]:
    return assessor._aggregate_usage(usages)


def _ensure_validating(sink: Any, run_phase: Any) -> None:
    if sink.phase == run_phase.REQUESTING_MODEL:
        sink.transition(run_phase.VALIDATING)
    elif sink.phase == run_phase.READING:
        sink.transition(run_phase.VALIDATING)


def _emit_terminal_complete(sink: Any, run_phase: Any, **metadata: Any) -> None:
    _ensure_validating(sink, run_phase)
    sink.complete(**metadata)


def _emit_terminal_failure_without_gui_duplicate(
    sink: Any,
    error_code: str,
    message: str,
) -> None:
    """Write one event-log terminal while leaving the GUI worker as the visible terminal owner."""

    callback = getattr(sink, "callback", None)
    can_swap = hasattr(sink, "callback")
    if can_swap:
        sink.callback = None
    try:
        sink.fail(error_code, message)
    finally:
        if can_swap:
            sink.callback = callback


def _status(
    *,
    machine: str,
    presentation: str,
    terminal: str,
    recoverability: str,
    provider_outcome: str,
    usage_status: str,
) -> dict[str, str]:
    return {
        "machine_status": machine,
        "presentation_status": presentation,
        "terminal_status": terminal,
        "recoverability": recoverability,
        "provider_outcome": provider_outcome,
        "usage_status": usage_status,
    }


def _machine_payload(assessor: Any, trace: _RunTrace) -> dict[str, Any]:
    if trace.machine_state is None:
        return {}
    coverage_digest = (
        canonical_digest(trace.coverage) if trace.coverage is not None else None
    )
    return {
        "contract_version": MACHINE_RECEIPT_VERSION,
        "coverage_digest_sha256": coverage_digest,
        "adjudication_contract_version": assessor.ADJUDICATION_CONTRACT_VERSION,
        "machine_state": deepcopy(trace.machine_state),
    }


def _machine_receipt(
    assessor: Any,
    trace: _RunTrace,
    result: RepairedAnalysisResult,
) -> dict[str, Any]:
    payload = _machine_payload(assessor, trace)
    if not payload:
        verdict = result.minimal_receipt.get("verdict")
        return {
            "contract_version": MACHINE_RECEIPT_VERSION,
            "status": "SUCCEEDED" if verdict != "UNASSESSED" else "HOLD",
            "source": "prior_receipt_or_deterministic_intake",
            "verdict": verdict,
            "machine_state_digest_sha256": None,
        }
    source = build_presentation_source(trace.machine_state or {})
    digest = canonical_digest(trace.machine_state or {})
    return {
        "contract_version": MACHINE_RECEIPT_VERSION,
        "status": "SUCCEEDED",
        "coverage_digest_sha256": payload["coverage_digest_sha256"],
        "adjudication_contract_version": assessor.ADJUDICATION_CONTRACT_VERSION,
        "contradiction_gate_passed": trace.contradiction_gate_passed,
        "machine_state_digest_sha256": digest,
        "machine_state_digest_after_presentation_sha256": digest,
        "machine_state_parity": True,
        "verdict": result.minimal_receipt.get("verdict"),
        "reason_category": result.minimal_receipt.get("reason_category"),
        "evidence_hold_codes": list(result.minimal_receipt.get("evidence_hold_codes", [])),
        "submission_hold_codes": list(result.minimal_receipt.get("submission_hold_codes", [])),
        "protected_binding_digest_sha256": source.protected_binding_digest_sha256,
        "protected_item_count": source.protected_item_count,
        "protected_item_ids": [
            entry.item_id for entry in source.entries if entry.path.startswith("protected[")
        ],
    }


def _rebuild_card(
    assessor: Any,
    options: Any,
    display_state: Mapping[str, Any],
) -> dict[str, Any]:
    document = assessor.read_document(options.manuscript_path)
    intake = assessor.analyze_intake_structure(document.text)
    state = assessor._base_state(document, options, intake)
    state.update(deepcopy(dict(display_state)))
    state["submission_hold_codes"] = list(
        dict.fromkeys(
            [
                *display_state.get("submission_hold_codes", []),
                *document.submission_hold_codes,
            ]
        )
    )
    card = assessor.public_card(state)
    return assessor.localize_closure_card(card, options.output_language)


def _coerce_result(result: Any) -> RepairedAnalysisResult:
    if isinstance(result, RepairedAnalysisResult):
        return result
    return RepairedAnalysisResult(
        closure_card=deepcopy(result.closure_card),
        minimal_receipt=deepcopy(result.minimal_receipt),
        provider=result.provider,
        model=result.model,
        reasoning_option=result.reasoning_option,
        api_called=result.api_called,
        usage=dict(result.usage),
        usage_calls=tuple(dict(item) for item in result.usage_calls),
        attempts=result.attempts,
        artifact_sha256=result.artifact_sha256,
        semantic_content_sha256=result.semantic_content_sha256,
        character_count=result.character_count,
        thread_id=result.thread_id,
        harness=deepcopy(result.harness),
    )


def _commit_success(
    assessor: Any,
    options: Any,
    result: Any,
    trace: _RunTrace,
) -> RepairedAnalysisResult:
    current = _coerce_result(result)
    usages = list(current.usage_calls)
    stages = list(_usage_stages(trace))
    if len(stages) != len(usages):
        stages = ["coverage", "adjudication"][: len(usages)]

    if trace.machine_state is None:
        verdict = current.closure_card.get("Verdict")
        run_status = _status(
            machine="HOLD" if verdict == "UNASSESSED" else "SUCCEEDED",
            presentation="PASS",
            terminal="HOLD" if verdict == "UNASSESSED" else "PASS",
            recoverability="NONE",
            provider_outcome="NOT_CALLED" if not stages else "SUCCEEDED",
            usage_status=aggregate_usage_status(usages),
        )
        return replace(
            current,
            run_status=run_status,
            machine_receipt=_machine_receipt(assessor, trace, current),
            presentation_receipt={
                "status": "PASS",
                "target_language": options.output_language,
                "repair_attempted": False,
                "repair_call_count": 0,
            },
            usage_call_stages=tuple(stages),
        )

    machine_receipt = _machine_receipt(assessor, trace, current)
    machine_digest = machine_receipt["machine_state_digest_sha256"]
    if trace.language_error is None:
        presentation_receipt = presentation_receipt_without_repair(
            trace.machine_state,
            target_language=options.output_language,
            machine_state_digest_sha256=machine_digest,
        )
        run_status = _status(
            machine="SUCCEEDED",
            presentation="PASS",
            terminal="PASS",
            recoverability="NONE",
            provider_outcome="SUCCEEDED",
            usage_status=aggregate_usage_status(usages),
        )
        return replace(
            current,
            run_status=run_status,
            machine_receipt=machine_receipt,
            presentation_receipt=presentation_receipt,
            usage_call_stages=tuple(stages),
        )

    try:
        repair = repair_presentation(
            trace.machine_state,
            provider=options.provider,
            model=current.model or options.model,
            reasoning_option=current.reasoning_option or options.reasoning_option,
            target_language=options.output_language,
            timeout_seconds=options.timeout_seconds,
            transient_retries=options.transient_retries,
        )
    except PresentationRepairError as exc:
        receipt = dict(exc.runtime)
        receipt.setdefault("status", "HOLD")
        receipt["initial_language_error"] = trace.language_error
        receipt["recoverability"] = "PRESENTATION_REPAIR"
        if receipt.get("repair_attempted"):
            usage = receipt.get("usage")
            usages.append(dict(usage) if isinstance(usage, Mapping) else {})
            stages.append("presentation_repair")
        run_status = _status(
            machine="SUCCEEDED",
            presentation="HOLD",
            terminal="HOLD",
            recoverability="PRESENTATION_REPAIR",
            provider_outcome=str(receipt.get("provider_outcome") or "SUCCEEDED"),
            usage_status=aggregate_usage_status(usages),
        )
        machine_receipt["machine_state_digest_after_presentation_sha256"] = machine_digest
        machine_receipt["machine_state_parity"] = True
        return replace(
            current,
            api_called=bool(stages),
            usage=_aggregate_usage(assessor, usages),
            usage_calls=tuple(usages),
            attempts=current.attempts + int(receipt.get("attempts", 0) or 0),
            run_status=run_status,
            machine_receipt=machine_receipt,
            presentation_receipt=receipt,
            usage_call_stages=tuple(stages),
        )

    # Commit the bounded repair call receipt before local card rendering so a
    # subsequent deterministic verifier failure cannot erase its usage.
    trace.calls.append(
        {
            "stage": "presentation_repair",
            "model": repair.model,
            "usage": dict(repair.usage),
            "finish_reason": None,
        }
    )
    usages.append(dict(repair.usage))
    stages.append("presentation_repair")
    repaired_card = _rebuild_card(assessor, options, repair.display_state)
    run_status = _status(
        machine="SUCCEEDED",
        presentation="PASS",
        terminal="PASS",
        recoverability="NONE",
        provider_outcome="SUCCEEDED",
        usage_status=aggregate_usage_status(usages),
    )
    machine_receipt["machine_state_digest_after_presentation_sha256"] = repair.receipt[
        "machine_state_digest_after_sha256"
    ]
    machine_receipt["machine_state_parity"] = repair.receipt["machine_state_parity"]
    return replace(
        current,
        closure_card=repaired_card,
        model=repair.model or current.model,
        api_called=True,
        usage=_aggregate_usage(assessor, usages),
        usage_calls=tuple(usages),
        attempts=current.attempts + repair.attempts,
        run_status=run_status,
        machine_receipt=machine_receipt,
        presentation_receipt=repair.receipt,
        usage_call_stages=tuple(stages),
    )


def _verifier_hold_result(
    assessor: Any,
    options: Any,
    sink: _DeferredTerminalSink,
    trace: _RunTrace,
    exc: Exception,
) -> RepairedAnalysisResult:
    document = assessor.read_document(options.manuscript_path)
    intake = assessor.analyze_intake_structure(document.text)
    state = assessor._base_state(document, options, intake)
    state["whole_manuscript_read"] = False
    if trace.coverage is not None:
        state["evidence_hold_codes"] = list(trace.coverage["evidence_hold_codes"])
        state["submission_hold_codes"] = list(
            dict.fromkeys(
                [
                    *trace.coverage["submission_hold_codes"],
                    *document.submission_hold_codes,
                ]
            )
        )
    harness_state = assessor.harness_receipt(
        intake,
        trace.budgets,
        coverage=trace.coverage,
        adjudication_bound=trace.machine_state is not None,
        contradiction_gate_passed=False,
    )
    usages = _usage_calls(trace)
    provider = options.provider if trace.calls else None
    model = trace.calls[-1]["model"] if trace.calls else options.model
    result = assessor._finish(
        document,
        state,
        sink,
        provider=provider,
        model=model,
        reasoning_option=options.reasoning_option,
        usage_calls=usages,
        attempts=trace.attempt_count,
        harness_state=harness_state,
    )
    current = _coerce_result(result)
    candidate_digest = None
    protected_digest = None
    protected_count = 0
    if trace.machine_state is not None:
        candidate_digest = canonical_digest(trace.machine_state)
        source = build_presentation_source(trace.machine_state)
        protected_digest = source.protected_binding_digest_sha256
        protected_count = source.protected_item_count
    machine_receipt = {
        "contract_version": MACHINE_RECEIPT_VERSION,
        "status": "HOLD",
        "failed_stage": trace.validation_stage,
        "error_code": type(exc).__name__,
        "coverage_digest_sha256": (
            canonical_digest(trace.coverage) if trace.coverage is not None else None
        ),
        "candidate_state_digest_sha256": candidate_digest,
        "protected_binding_digest_sha256": protected_digest,
        "protected_item_count": protected_count,
        "contradiction_gate_passed": False,
    }
    run_status = _status(
        machine="HOLD",
        presentation="HOLD",
        terminal="HOLD",
        recoverability="NONE",
        provider_outcome="SUCCEEDED",
        usage_status=aggregate_usage_status(usages),
    )
    return replace(
        current,
        run_status=run_status,
        machine_receipt=machine_receipt,
        presentation_receipt={
            "status": "HOLD",
            "error_code": "MACHINE_STATE_NOT_COMMITTED",
            "repair_attempted": False,
            "repair_call_count": 0,
            "target_language": options.output_language,
        },
        usage_call_stages=_usage_stages(trace),
    )


def _install_interpretation_guard() -> None:
    from . import interpretation

    if getattr(interpretation, "_mrc_presentation_guard_installed", False):
        return
    original = interpretation.generate_interpretation

    def guarded_generate_interpretation(*args: Any, **kwargs: Any) -> Any:
        public_result = kwargs.get("public_result")
        if isinstance(public_result, Mapping):
            runtime = public_result.get("runtime")
            if isinstance(runtime, Mapping) and runtime.get("terminal_status") == "HOLD":
                raise interpretation.InterpretationContractError(
                    "core result is on presentation HOLD; optional interpretation was not sent"
                )
        return original(*args, **kwargs)

    interpretation.generate_interpretation = guarded_generate_interpretation
    interpretation._mrc_presentation_guard_installed = True


def install_runtime_repair(assessor: Any) -> None:
    """Install the bounded transaction repair exactly once."""

    if getattr(assessor, "_mrc_runtime_repair_installed", False):
        return

    assessor._mrc_original_analysis_result = assessor.AnalysisResult
    assessor.AnalysisResult = RepairedAnalysisResult

    assessor._mrc_original_request_stage = assessor._request_stage
    assessor._mrc_original_context_budget = assessor.context_budget
    assessor._mrc_original_validate_coverage = assessor.validate_coverage
    assessor._mrc_original_validate_adjudication_binding = assessor.validate_adjudication_binding
    assessor._mrc_original_validate_model_state = assessor.validate_model_state
    assessor._mrc_original_validate_output_language = assessor._validate_model_output_language
    assessor._mrc_original_validate_cross_stage_consistency = assessor.validate_cross_stage_consistency
    assessor._mrc_original_analyze_manuscript = assessor.analyze_manuscript

    def request_stage_wrapper(*args: Any, **kwargs: Any) -> Any:
        completion = assessor._mrc_original_request_stage(*args, **kwargs)
        trace = _trace()
        if trace is not None:
            stage = _call_stage(str(kwargs.get("schema_name", "model")))
            trace.calls.append(
                {
                    "stage": stage,
                    "model": completion.model,
                    "usage": dict(completion.usage),
                    "finish_reason": completion.finish_reason,
                }
            )
        return completion

    def context_budget_wrapper(*args: Any, **kwargs: Any) -> Any:
        receipt = assessor._mrc_original_context_budget(*args, **kwargs)
        trace = _trace()
        if trace is not None:
            trace.budgets.append(receipt)
        return receipt

    def validate_coverage_wrapper(value: Mapping[str, Any]) -> dict[str, Any]:
        trace = _trace()
        if trace is not None:
            trace.validation_stage = "coverage_contract"
        clean = assessor._mrc_original_validate_coverage(value)
        if trace is not None:
            trace.coverage = deepcopy(clean)
        return clean

    def validate_binding_wrapper(
        value: Mapping[str, Any],
        coverage: Mapping[str, Any],
    ) -> dict[str, Any]:
        trace = _trace()
        if trace is not None:
            trace.validation_stage = "adjudication_binding"
        return assessor._mrc_original_validate_adjudication_binding(value, coverage)

    def validate_model_state_wrapper(value: Mapping[str, Any]) -> dict[str, Any]:
        trace = _trace()
        if trace is not None:
            trace.validation_stage = "adjudication_schema"
        clean = assessor._mrc_original_validate_model_state(value)
        if trace is not None:
            trace.machine_state = deepcopy(clean)
        return clean

    def validate_language_wrapper(value: Mapping[str, Any], language: str) -> None:
        trace = _trace()
        try:
            assessor._mrc_original_validate_output_language(value, language)
        except assessor.ModelContractError as exc:
            if trace is None:
                raise
            trace.language_error = str(exc)
            trace.validation_stage = "presentation_language"

    def validate_cross_stage_wrapper(
        coverage: Mapping[str, Any],
        model_state: Mapping[str, Any],
    ) -> None:
        trace = _trace()
        if trace is not None:
            trace.validation_stage = "contradiction_gate"
        assessor._mrc_original_validate_cross_stage_consistency(coverage, model_state)
        if trace is not None:
            trace.contradiction_gate_passed = True
            trace.validation_stage = "machine_committed"

    def analyze_wrapper(options: Any, *, event_sink: Any | None = None) -> RepairedAnalysisResult:
        trace = _RunTrace()
        token = _TRACE.set(trace)
        actual_sink = event_sink or assessor.EventSink()
        deferred_sink = _DeferredTerminalSink(actual_sink, trace)
        try:
            try:
                raw_result = assessor._mrc_original_analyze_manuscript(
                    options,
                    event_sink=deferred_sink,
                )
                result = _commit_success(assessor, options, raw_result, trace)
                _emit_terminal_complete(
                    actual_sink,
                    assessor.RunPhase,
                    verdict=result.closure_card.get("Verdict"),
                    terminal_status=result.run_status.get("terminal_status"),
                    machine_status=result.run_status.get("machine_status"),
                    presentation_status=result.run_status.get("presentation_status"),
                    usage=result.usage,
                )
                return result
            except (assessor.HarnessContractError, assessor.ClosureStateError) as exc:
                if trace.validation_stage not in {"contradiction_gate", "machine_committed"}:
                    raise
                result = _verifier_hold_result(
                    assessor,
                    options,
                    deferred_sink,
                    trace,
                    exc,
                )
                for item_id, item_type in list(trace.pending_items.items()):
                    actual_sink.item_completed(
                        item_id,
                        item_type,
                        status="hold",
                        error_code=type(exc).__name__,
                    )
                    trace.pending_items.pop(item_id, None)
                _emit_terminal_complete(
                    actual_sink,
                    assessor.RunPhase,
                    verdict="UNASSESSED",
                    terminal_status="HOLD",
                    machine_status="HOLD",
                    presentation_status="HOLD",
                    usage=result.usage,
                )
                return result
        except Exception as exc:
            _emit_terminal_failure_without_gui_duplicate(
                actual_sink,
                type(exc).__name__,
                str(exc),
            )
            raise
        finally:
            _TRACE.reset(token)

    assessor._request_stage = request_stage_wrapper
    assessor.context_budget = context_budget_wrapper
    assessor.validate_coverage = validate_coverage_wrapper
    assessor.validate_adjudication_binding = validate_binding_wrapper
    assessor.validate_model_state = validate_model_state_wrapper
    assessor._validate_model_output_language = validate_language_wrapper
    assessor.validate_cross_stage_consistency = validate_cross_stage_wrapper
    assessor.analyze_manuscript = analyze_wrapper
    assessor._mrc_runtime_repair_installed = True
    _install_interpretation_guard()
