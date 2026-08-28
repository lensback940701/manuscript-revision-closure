from __future__ import annotations

import importlib
import inspect
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import standalone
from standalone import assessor as assessor_module
from standalone.assessor import RunOptions, analyze_manuscript
from standalone.events import EventSink
from standalone.harness import COVERAGE_CONTRACT_VERSION, COVERAGE_DIMENSIONS, canonical_digest
from standalone.interpretation import InterpretationContractError, generate_interpretation
from standalone.presentation_transaction import (
    PRESENTATION_REPAIR_CONTRACT_VERSION,
    aggregate_usage_status,
    assess_chinese_text,
    assess_presentation_language,
    build_presentation_repair_messages,
    build_presentation_source,
    machine_state_digest,
    presentation_budget,
    repair_presentation,
    usage_status,
    validate_presentation_repair,
)
from standalone.providers import ChatCompletionClient, CompletionResult, ProviderRequestError
from standalone.web_gui import GuiState, _analysis_worker


COVERAGE_STATE = {
    "coverage_contract_version": COVERAGE_CONTRACT_VERSION,
    "manuscript_identity_confirmed": True,
    "full_span_covered": True,
    "dimensions": [
        {
            "dimension": dimension,
            "applicability": "APPLICABLE",
            "assessed": True,
            "status": "CLEAR",
        }
        for dimension in COVERAGE_DIMENSIONS
    ],
    "root_cause_candidate_dimensions": [],
    "evidence_hold_codes": [],
    "submission_hold_codes": [],
    "protected_invariants": {
        "claim_ceiling_preserved": True,
        "evidence_status_distinctions_preserved": True,
        "rivals_and_negative_findings_preserved": True,
    },
}

ENGLISH_MACHINE_STATE = {
    "material_root_causes": [],
    "evidence_hold_codes": [],
    "submission_hold_codes": [],
    "protected": [
        "Protect the existing claim ceiling.",
        "Keep the negative case visible.",
    ],
    "parked_opportunities": ["Future comparative extension."],
    "lite_suggestions": [],
}

CHINESE_MACHINE_STATE = {
    **ENGLISH_MACHINE_STATE,
    "protected": ["保护现有主张上限。", "保留负面案例。"],
    "parked_opportunities": ["未来可开展比较研究。"],
}


def long_manuscript(marker: str = "FULL_MANUSCRIPT_MARKER") -> str:
    return (
        "Title\n\nAbstract\n"
        + ((marker + " evidence-bound argument.\n") * 90)
        + "\nConclusion\nThe argument remains bounded.\n\nReferences\nReference A."
    )


def adjudication(machine_state: dict[str, object]) -> dict[str, object]:
    return {
        "coverage_digest_sha256": canonical_digest(COVERAGE_STATE),
        **machine_state,
    }


def repair_object(
    machine_state: dict[str, object] = ENGLISH_MACHINE_STATE,
    translations: list[str] | None = None,
) -> dict[str, object]:
    source = build_presentation_source(machine_state)
    translated = translations or [
        "保护现有主张上限。",
        "保留负面案例。",
        "未来可开展比较研究。",
    ]
    return {
        "contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
        "source_binding_digest_sha256": source.source_binding_digest_sha256,
        "items": [
            {
                "id": item.item_id,
                "source_sha256": item.source_sha256,
                "display_text": text,
            }
            for item, text in zip(source.items, translated, strict=True)
        ],
    }


class NativePresentationTransactionTests(unittest.TestCase):
    def _run_analysis(
        self,
        completions: list[object],
        *,
        machine_state: dict[str, object] = ENGLISH_MACHINE_STATE,
        output_language: str = "zh",
        event_sink: EventSink | None = None,
        enable_presentation_repair: bool = True,
    ) -> tuple[object, list[list[dict[str, str]]], list[dict[str, object]], int]:
        queue = list(completions)
        captured_messages: list[list[dict[str, str]]] = []
        captured_clients: list[dict[str, object]] = []
        calls = 0

        def mocked_complete(
            client: ChatCompletionClient,
            messages: list[dict[str, str]],
            **kwargs: object,
        ) -> CompletionResult:
            nonlocal calls
            captured_messages.append(deepcopy(messages))
            captured_clients.append(
                {
                    "timeout_seconds": client.timeout_seconds,
                    "max_transient_retries": client.max_transient_retries,
                    "kwargs": deepcopy(kwargs),
                }
            )
            value = queue[calls]
            calls += 1
            if client.on_attempt is not None:
                client.on_attempt(1)
            if isinstance(value, BaseException):
                raise value
            assert isinstance(value, CompletionResult)
            return value

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "synthetic-test-key"}, clear=True
        ), patch.object(
            ChatCompletionClient,
            "complete",
            autospec=True,
            side_effect=mocked_complete,
        ):
            path = Path(directory) / "paper.md"
            path.write_text(long_manuscript(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="kimi",
                    model="kimi-k2.6",
                    reasoning_option="enabled",
                    output_language=output_language,
                    manuscript_identity="paper-v1",
                    confirm_complete_current_manuscript=True,
                    enable_presentation_repair=enable_presentation_repair,
                ),
                event_sink=event_sink,
            )
        return result, captured_messages, captured_clients, calls

    def _core_completions(self, machine_state: dict[str, object]) -> list[CompletionResult]:
        return [
            CompletionResult(
                content=json.dumps(COVERAGE_STATE, ensure_ascii=False),
                model="kimi-k2.6",
                usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
            ),
            CompletionResult(
                content=json.dumps(adjudication(machine_state), ensure_ascii=False),
                model="kimi-k2.6",
                usage={"prompt_tokens": 120, "completion_tokens": 50, "total_tokens": 170},
            ),
        ]

    def test_01_exact_061_language_failure_is_retained(self) -> None:
        with self.assertRaisesRegex(
            assessor_module.ModelContractError,
            "requested Chinese output contains a non-Chinese public text value",
        ):
            assessor_module._validate_model_output_language(ENGLISH_MACHINE_STATE, "zh")

    def test_02_language_failure_does_not_erase_machine_state(self) -> None:
        result, _messages, _clients, calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content=json.dumps(repair_object(), ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
                ),
            ]
        )
        runtime = result.as_dict()["runtime"]
        self.assertEqual(3, calls)
        self.assertEqual("SUCCEEDED", runtime["machine_status"])
        self.assertEqual("PASS", runtime["presentation_status"])
        self.assertEqual("STOP_REVISING", result.closure_card["Verdict"])
        self.assertTrue(runtime["machine_receipt"]["machine_state_parity"])

    def test_03_adjudication_usage_is_retained_after_language_failure(self) -> None:
        result, _messages, _clients, _calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content="invalid presentation response",
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                ),
            ]
        )
        runtime = result.as_dict()["runtime"]
        self.assertEqual([140, 170, 5], [item.get("total_tokens") for item in runtime["usage_calls"]])
        self.assertEqual(315, runtime["usage"]["total_tokens"])
        self.assertEqual("SUCCEEDED", runtime["machine_provider_outcome"])

    def test_04_presentation_repair_has_one_actual_attempt(self) -> None:
        result, _messages, clients, calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content=json.dumps(repair_object(), ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
                ),
            ]
        )
        self.assertEqual(3, calls)
        self.assertEqual(0, clients[2]["max_transient_retries"])
        receipt = result.as_dict()["runtime"]["presentation_receipt"]
        self.assertEqual(1, receipt["actual_attempt_count"])
        self.assertEqual(0, receipt["max_transient_retries"])

    def test_05_presentation_http_503_is_not_retried(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.moonshot.cn/v1/chat/completions",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"error":{"message":"overloaded"}}'),
        )
        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "synthetic"}, clear=True), patch(
            "standalone.providers.urllib.request.urlopen", side_effect=error
        ) as mocked:
            result = repair_presentation(
                ENGLISH_MACHINE_STATE,
                provider="kimi",
                model="kimi-k2.6",
                reasoning_option="enabled",
                target_language="zh",
                coverage_digest_sha256=canonical_digest(COVERAGE_STATE),
            )
        self.assertEqual(1, mocked.call_count)
        self.assertEqual("HOLD", result.status)
        self.assertEqual("REJECTED", result.provider_outcome)
        self.assertEqual(1, result.attempts)

    def test_06_presentation_timeout_is_not_retried(self) -> None:
        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "synthetic"}, clear=True), patch(
            "standalone.providers.urllib.request.urlopen", side_effect=socket.timeout("read timed out")
        ) as mocked:
            result = repair_presentation(
                ENGLISH_MACHINE_STATE,
                provider="kimi",
                model="kimi-k2.6",
                reasoning_option="enabled",
                target_language="zh",
                coverage_digest_sha256=canonical_digest(COVERAGE_STATE),
            )
        self.assertEqual(1, mocked.call_count)
        self.assertEqual("UNKNOWN", result.provider_outcome)
        self.assertEqual(1, result.attempts)

    def test_07_kimi_presentation_timeout_is_900_seconds(self) -> None:
        result, _messages, clients, _calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content=json.dumps(repair_object(), ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={},
                ),
            ]
        )
        self.assertEqual(900.0, clients[2]["timeout_seconds"])
        self.assertEqual(900.0, result.as_dict()["runtime"]["presentation_receipt"]["stage_timeout_seconds"])

    def test_08_presentation_budget_has_no_small_magic_cap(self) -> None:
        source = build_presentation_source(ENGLISH_MACHINE_STATE)
        messages = build_presentation_repair_messages(source, target_language="zh")
        budget = presentation_budget(messages, source, provider="kimi", model="kimi-k2.6")
        source_code = inspect.getsource(importlib.import_module("standalone.presentation_transaction"))
        for forbidden in ("max_output_tokens=4096", "max_output_tokens=5000", "max_output_tokens=8192"):
            self.assertNotIn(forbidden, source_code)
        self.assertGreaterEqual(budget.requested_max_output_tokens, budget.schema_output_ceiling_tokens)
        self.assertLessEqual(budget.requested_max_output_tokens, budget.provider_output_ceiling_tokens)

    def test_09_provider_scale_context_budget_is_recorded(self) -> None:
        result, _messages, clients, _calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content=json.dumps(repair_object(), ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={},
                ),
            ]
        )
        receipt = result.as_dict()["runtime"]["presentation_receipt"]
        budget = receipt["budget"]
        self.assertEqual(clients[2]["kwargs"]["max_output_tokens"], budget["requested_max_output_tokens"])
        self.assertEqual(131072, budget["provider_output_ceiling_tokens"])
        self.assertGreater(budget["estimated_input_tokens"], 0)
        self.assertGreater(budget["safety_margin_tokens"], 0)

    def test_10_english_sentence_with_one_chinese_character_holds(self) -> None:
        self.assertFalse(assess_chinese_text("This remains almost entirely English 仅")["passed"])
        self.assertFalse(assess_chinese_text("The argument should remain unchanged 中文")["passed"])

    def test_11_chinese_with_identifiers_and_english_terms_passes(self) -> None:
        examples = (
            "保持 DOI、Kimi K2.6 与 state entrepreneurship 等专有术语的原有含义。",
            "保留原有 evidence boundary，不提高主张层级。",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertTrue(assess_chinese_text(text)["passed"])

    def test_12_duplicate_protected_text_keeps_distinct_identity(self) -> None:
        state = {**ENGLISH_MACHINE_STATE, "protected": ["Keep this boundary.", "Keep this boundary."], "parked_opportunities": []}
        source = build_presentation_source(state)
        ids = [item.item_id for item in source.items if item.path.startswith("protected[")]
        self.assertEqual(2, len(ids))
        self.assertEqual(2, len(set(ids)))
        self.assertEqual(2, source.protected_cardinality)

    def test_13_missing_item_holds(self) -> None:
        source = build_presentation_source(ENGLISH_MACHINE_STATE)
        value = repair_object()
        value["items"] = value["items"][:-1]
        with self.assertRaisesRegex(ValueError, "cardinality"):
            validate_presentation_repair(value, source, target_language="zh")

    def test_14_extra_item_holds(self) -> None:
        source = build_presentation_source(ENGLISH_MACHINE_STATE)
        value = repair_object()
        value["items"] = [*value["items"], deepcopy(value["items"][-1])]
        with self.assertRaisesRegex(ValueError, "cardinality"):
            validate_presentation_repair(value, source, target_language="zh")

    def test_15_duplicate_item_holds(self) -> None:
        source = build_presentation_source(ENGLISH_MACHINE_STATE)
        value = repair_object()
        value["items"][1] = deepcopy(value["items"][0])
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_presentation_repair(value, source, target_language="zh")

    def test_16_reordered_item_holds(self) -> None:
        source = build_presentation_source(ENGLISH_MACHINE_STATE)
        value = repair_object()
        value["items"][0], value["items"][1] = value["items"][1], value["items"][0]
        with self.assertRaisesRegex(ValueError, "order or identity"):
            validate_presentation_repair(value, source, target_language="zh")

    def test_17_stale_source_hash_holds(self) -> None:
        source = build_presentation_source(ENGLISH_MACHINE_STATE)
        value = repair_object()
        value["items"][0]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source hash"):
            validate_presentation_repair(value, source, target_language="zh")

    def test_18_machine_hash_change_is_integrity_hold(self) -> None:
        completion = CompletionResult(
            content=json.dumps(repair_object(), ensure_ascii=False),
            model="kimi-k2.6",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )
        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "synthetic"}, clear=True), patch.object(
            ChatCompletionClient, "complete", return_value=completion
        ), patch(
            "standalone.presentation_transaction.machine_state_digest",
            side_effect=["a" * 64, "b" * 64],
        ):
            result = repair_presentation(
                ENGLISH_MACHINE_STATE,
                provider="kimi",
                model="kimi-k2.6",
                reasoning_option="enabled",
                target_language="zh",
                coverage_digest_sha256=canonical_digest(COVERAGE_STATE),
            )
        self.assertEqual("HOLD", result.status)
        self.assertEqual("INTEGRITY_HOLD", result.error_code)
        self.assertFalse(result.receipt["machine_state_parity"])

    def test_19_20_21_forbidden_machine_keys_in_repair_schema_hold(self) -> None:
        source = build_presentation_source(ENGLISH_MACHINE_STATE)
        for extra in (
            {"verdict": "STOP_REVISING"},
            {"evidence_hold_codes": []},
            {"material_root_causes": []},
        ):
            with self.subTest(extra=next(iter(extra))):
                value = {**repair_object(), **extra}
                with self.assertRaisesRegex(ValueError, "key set"):
                    validate_presentation_repair(value, source, target_language="zh")

    def test_22_repair_failure_keeps_machine_provider_success(self) -> None:
        result, _messages, _clients, _calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content="invalid repair",
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                ),
            ]
        )
        runtime = result.as_dict()["runtime"]
        self.assertEqual("SUCCEEDED", runtime["machine_provider_outcome"])
        self.assertEqual("SUCCEEDED", runtime["machine_status"])
        self.assertEqual("STOP_REVISING", result.closure_card["Verdict"])

    def test_23_presentation_provider_outcome_is_independent(self) -> None:
        result, _messages, _clients, _calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(content="invalid repair", model="kimi-k2.6", usage={}),
            ]
        )
        runtime = result.as_dict()["runtime"]
        self.assertEqual("SUCCEEDED", runtime["machine_provider_outcome"])
        self.assertEqual("REJECTED", runtime["presentation_provider_outcome"])
        self.assertEqual("HOLD", runtime["presentation_status"])

    def test_24_known_partial_unknown_usage_paths(self) -> None:
        self.assertEqual("COMPLETE", usage_status({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}))
        self.assertEqual("PARTIAL", usage_status({"prompt_tokens": 1}))
        self.assertEqual("UNKNOWN", usage_status({}))
        self.assertEqual(
            "PARTIAL",
            aggregate_usage_status(
                [{"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, {}],
                attempted_call_count=2,
            ),
        )

    def test_25_contradiction_failure_retains_usage(self) -> None:
        sink = EventSink()
        with patch(
            "standalone.assessor.validate_cross_stage_consistency",
            side_effect=assessor_module.HarnessContractError("forced contradiction failure"),
        ):
            result, _messages, _clients, calls = self._run_analysis(
                self._core_completions(CHINESE_MACHINE_STATE),
                machine_state=CHINESE_MACHINE_STATE,
                event_sink=sink,
            )
        runtime = result.as_dict()["runtime"]
        self.assertEqual(2, calls)
        self.assertEqual("HOLD", runtime["machine_status"])
        self.assertEqual("SUCCEEDED", runtime["machine_provider_outcome"])
        self.assertEqual("COMPLETE", runtime["usage_status"])
        self.assertEqual(310, runtime["usage"]["total_tokens"])
        self.assertEqual("UNASSESSED", result.closure_card["Verdict"])

    def test_26_gui_observes_one_terminal_event(self) -> None:
        queue = iter(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content="invalid repair",
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                ),
            ]
        )
        calls = 0

        def mocked_complete(client: ChatCompletionClient, _messages: object, **_kwargs: object) -> CompletionResult:
            nonlocal calls
            calls += 1
            if client.on_attempt is not None:
                client.on_attempt(1)
            return next(queue)

        state = GuiState()
        self.assertTrue(state.start())
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "synthetic"}, clear=True
        ), patch.object(
            ChatCompletionClient, "complete", autospec=True, side_effect=mocked_complete
        ), patch("standalone.web_gui.price_with_fallback", return_value=None):
            path = Path(directory) / "paper.md"
            path.write_text(long_manuscript(), encoding="utf-8")
            _analysis_worker(
                state,
                {
                    "manuscript_path": str(path),
                    "provider": "kimi",
                    "model": "kimi-k2.6",
                    "reasoning_option": "enabled",
                    "language": "zh",
                    "identity": "paper-v1",
                    "confirmed_complete": True,
                    "prior_receipt_path": "",
                    "generate_interpretation": True,
                },
            )
        snapshot = state.snapshot()
        self.assertEqual(3, calls)
        terminal_rows = [row for row in snapshot["timeline"] if row["details"].get("terminal_event_id")]
        self.assertEqual(1, len(terminal_rows))
        self.assertEqual("completed_with_presentation_hold", snapshot["phase"])

    def test_27_package_import_does_not_replace_assessor_functions(self) -> None:
        before = assessor_module.analyze_manuscript
        importlib.reload(standalone)
        self.assertIs(before, assessor_module.analyze_manuscript)
        source = inspect.getsource(standalone)
        self.assertNotIn("install_runtime_repair", source)
        self.assertNotIn("_mrc_original_", inspect.getsource(assessor_module))

    def test_28_import_reload_is_idempotent(self) -> None:
        first = assessor_module.analyze_manuscript
        importlib.reload(standalone)
        importlib.reload(standalone)
        self.assertIs(first, assessor_module.analyze_manuscript)

    def test_29_chinese_without_defect_does_not_call_repair(self) -> None:
        result, _messages, _clients, calls = self._run_analysis(
            self._core_completions(CHINESE_MACHINE_STATE),
            machine_state=CHINESE_MACHINE_STATE,
        )
        runtime = result.as_dict()["runtime"]
        self.assertEqual(2, calls)
        self.assertEqual("NOT_CALLED", runtime["presentation_provider_outcome"])
        self.assertFalse(runtime["presentation_receipt"]["repair_attempted"])

    def test_30_english_output_mode_does_not_call_chinese_repair(self) -> None:
        result, _messages, _clients, calls = self._run_analysis(
            self._core_completions(ENGLISH_MACHINE_STATE),
            output_language="en",
        )
        self.assertEqual(2, calls)
        self.assertEqual("NOT_CALLED", result.as_dict()["runtime"]["presentation_provider_outcome"])

    def test_31_presentation_hold_blocks_optional_interpretation_before_read(self) -> None:
        public_result = {
            "runtime": {"terminal_status": "HOLD", "presentation_status": "HOLD"},
            "closure_card": {"Verdict": "STOP_REVISING"},
        }
        with patch("standalone.interpretation.read_document") as read_document, patch.object(
            ChatCompletionClient, "complete"
        ) as complete:
            with self.assertRaisesRegex(InterpretationContractError, "was not sent"):
                generate_interpretation(
                    Path("unused.md"),
                    expected_artifact_sha256="a" * 64,
                    manuscript_identity="paper-v1",
                    public_result=public_result,
                    provider="kimi",
                    model="kimi-k2.6",
                )
        read_document.assert_not_called()
        complete.assert_not_called()

    def test_32_repair_request_excludes_manuscript_and_machine_decisions(self) -> None:
        _result, messages, _clients, calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content=json.dumps(repair_object(), ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={},
                ),
            ]
        )
        self.assertEqual(3, calls)
        serialized = json.dumps(messages[2], ensure_ascii=False)
        for forbidden in (
            "FULL_MANUSCRIPT_MARKER",
            "coverage_contract_version",
            "dimensions",
            "material_root_causes",
            "evidence_hold_codes",
            "submission_hold_codes",
            "STOP_REVISING",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_33_version_is_062_and_skill_remains_021(self) -> None:
        self.assertEqual("0.6.2", standalone.__version__)
        result, _messages, _clients, _calls = self._run_analysis(
            self._core_completions(CHINESE_MACHINE_STATE),
            machine_state=CHINESE_MACHINE_STATE,
        )
        runtime = result.as_dict()["runtime"]
        self.assertEqual("0.6.2", runtime["standalone_version"])
        self.assertEqual("0.2.1", runtime["skill_version"])

    def test_machine_hash_parity_is_exact_after_successful_repair(self) -> None:
        result, _messages, _clients, _calls = self._run_analysis(
            [
                *self._core_completions(ENGLISH_MACHINE_STATE),
                CompletionResult(
                    content=json.dumps(repair_object(), ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={},
                ),
            ]
        )
        receipt = result.as_dict()["runtime"]["machine_receipt"]
        self.assertEqual(
            receipt["machine_state_digest_sha256"],
            receipt["machine_state_digest_after_presentation_sha256"],
        )
        self.assertTrue(receipt["machine_state_parity"])


if __name__ == "__main__":
    unittest.main()
