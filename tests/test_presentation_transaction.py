from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from standalone import assessor as assessor_module
from standalone.assessor import RunOptions, analyze_manuscript
from standalone.events import EventSink
from standalone.harness import COVERAGE_CONTRACT_VERSION, COVERAGE_DIMENSIONS, canonical_digest
from standalone.interpretation import InterpretationContractError, generate_interpretation
from standalone.presentation_repair import (
    PRESENTATION_REPAIR_CONTRACT_VERSION,
    build_presentation_source,
)
from standalone.providers import ChatCompletionClient, CompletionResult
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


def repair_response() -> dict[str, object]:
    source = build_presentation_source(ENGLISH_MACHINE_STATE)
    translations = [
        "保护现有主张上限。",
        "保留负面案例。",
        "未来可开展比较研究。",
    ]
    return {
        "contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
        "source_digest_sha256": source.source_digest_sha256,
        "items": [
            {"id": entry.item_id, "text": translation}
            for entry, translation in zip(source.entries, translations, strict=True)
        ],
    }


class PresentationTransactionTests(unittest.TestCase):
    def _run(
        self,
        completions: list[CompletionResult],
        *,
        event_sink: EventSink | None = None,
    ) -> tuple[object, list[list[dict[str, str]]], int]:
        captured_messages: list[list[dict[str, str]]] = []
        queue = iter(completions)
        call_count = 0

        def mocked_complete(
            client: ChatCompletionClient,
            messages: list[dict[str, str]],
            **_kwargs: object,
        ) -> CompletionResult:
            nonlocal call_count
            call_count += 1
            captured_messages.append(messages)
            if client.on_attempt is not None:
                client.on_attempt(1)
            return next(queue)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "local-test-key"}, clear=True
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
                    output_language="zh",
                    manuscript_identity="paper-v1",
                    confirm_complete_current_manuscript=True,
                ),
                event_sink=event_sink,
            )
        return result, captured_messages, call_count

    def test_language_failure_runs_one_bounded_repair_and_freezes_machine_state(self) -> None:
        sink = EventSink()
        result, messages, call_count = self._run(
            [
                CompletionResult(
                    content=json.dumps(COVERAGE_STATE, ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                ),
                CompletionResult(
                    content=json.dumps(adjudication(ENGLISH_MACHINE_STATE), ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 120, "completion_tokens": 50, "total_tokens": 170},
                ),
                CompletionResult(
                    content=json.dumps(repair_response(), ensure_ascii=False),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
                ),
            ],
            event_sink=sink,
        )
        public = result.as_dict()
        runtime = public["runtime"]
        self.assertEqual(3, call_count)
        self.assertEqual("SUCCEEDED", runtime["machine_status"])
        self.assertEqual("PASS", runtime["presentation_status"])
        self.assertEqual("PASS", runtime["terminal_status"])
        self.assertEqual("NONE", runtime["recoverability"])
        self.assertEqual("COMPLETE", runtime["usage_status"])
        self.assertEqual(3, len(runtime["usage_calls"]))
        self.assertEqual(360, runtime["usage"]["total_tokens"])
        self.assertEqual(
            runtime["machine_receipt"]["machine_state_digest_sha256"],
            runtime["machine_receipt"]["machine_state_digest_after_presentation_sha256"],
        )
        self.assertTrue(runtime["machine_receipt"]["machine_state_parity"])
        self.assertEqual(2, runtime["machine_receipt"]["protected_item_count"])
        self.assertEqual(2, len(runtime["machine_receipt"]["protected_item_ids"]))
        self.assertEqual(
            ["保护现有主张上限。", "保留负面案例。"],
            public["closure_card"]["Protected / Do not disturb"],
        )
        repair_request = json.dumps(messages[2], ensure_ascii=False)
        self.assertNotIn("FULL_MANUSCRIPT_MARKER", repair_request)
        self.assertNotIn("dimensions", repair_request)
        self.assertNotIn("coverage_contract_version", repair_request)
        terminal_events = [
            event for event in sink.events if event["type"] in {"turn.completed", "turn.failed"}
        ]
        self.assertEqual(1, len(terminal_events))
        self.assertEqual("turn.completed", terminal_events[0]["type"])

    def test_failed_repair_returns_explicit_hold_and_keeps_usage_without_raw_output(self) -> None:
        marker = "RAW_PRESENTATION_RESPONSE_MUST_NOT_PERSIST"
        result, _messages, call_count = self._run(
            [
                CompletionResult(
                    content=json.dumps(COVERAGE_STATE),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ),
                CompletionResult(
                    content=json.dumps(adjudication(ENGLISH_MACHINE_STATE)),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                ),
                CompletionResult(
                    content=marker,
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                ),
            ]
        )
        public = result.as_dict()
        runtime = public["runtime"]
        self.assertEqual(3, call_count)
        self.assertEqual("SUCCEEDED", runtime["machine_status"])
        self.assertEqual("HOLD", runtime["presentation_status"])
        self.assertEqual("HOLD", runtime["terminal_status"])
        self.assertEqual("PRESENTATION_REPAIR", runtime["recoverability"])
        self.assertEqual("COMPLETE", runtime["usage_status"])
        self.assertEqual(38, runtime["usage"]["total_tokens"])
        self.assertEqual(
            "PRESENTATION_REPAIR_CONTRACT_FAILED",
            runtime["presentation_receipt"]["error_code"],
        )
        self.assertTrue(runtime["machine_receipt"]["machine_state_parity"])
        self.assertNotIn(marker, json.dumps(public, ensure_ascii=False))
        self.assertFalse(runtime["raw_provider_response_persisted"])
        self.assertFalse(runtime["automatic_result_file_written"])

    def test_optional_interpretation_is_blocked_before_read_or_provider_on_hold(self) -> None:
        public_result = {
            "runtime": {"terminal_status": "HOLD"},
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

    def test_contradiction_verifier_failure_returns_unassessed_hold_with_usage(self) -> None:
        sink = EventSink()
        with patch.object(
            assessor_module,
            "_mrc_original_validate_cross_stage_consistency",
            side_effect=assessor_module.HarnessContractError("forced verifier failure"),
        ):
            result, _messages, call_count = self._run(
                [
                    CompletionResult(
                        content=json.dumps(COVERAGE_STATE, ensure_ascii=False),
                        model="kimi-k2.6",
                        usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                    ),
                    CompletionResult(
                        content=json.dumps(adjudication(CHINESE_MACHINE_STATE), ensure_ascii=False),
                        model="kimi-k2.6",
                        usage={"prompt_tokens": 120, "completion_tokens": 50, "total_tokens": 170},
                    ),
                ],
                event_sink=sink,
            )
        public = result.as_dict()
        runtime = public["runtime"]
        self.assertEqual(2, call_count)
        self.assertEqual("UNASSESSED", public["closure_card"]["Verdict"])
        self.assertEqual("HOLD", runtime["machine_status"])
        self.assertEqual("HOLD", runtime["presentation_status"])
        self.assertEqual("HOLD", runtime["terminal_status"])
        self.assertEqual("NONE", runtime["recoverability"])
        self.assertEqual("SUCCEEDED", runtime["provider_outcome"])
        self.assertEqual("COMPLETE", runtime["usage_status"])
        self.assertEqual(310, runtime["usage"]["total_tokens"])
        self.assertEqual(2, len(runtime["usage_calls"]))
        self.assertEqual("contradiction_gate", runtime["machine_receipt"]["failed_stage"])
        self.assertIsNotNone(runtime["machine_receipt"]["candidate_state_digest_sha256"])
        terminal_events = [
            event for event in sink.events if event["type"] in {"turn.completed", "turn.failed"}
        ]
        self.assertEqual(1, len(terminal_events))
        self.assertEqual("turn.completed", terminal_events[0]["type"])

    def test_failure_first_exact_language_exception_remains_registered(self) -> None:
        with self.assertRaisesRegex(
            assessor_module.ModelContractError,
            "requested Chinese output contains a non-Chinese public text value",
        ):
            assessor_module._validate_model_output_language(ENGLISH_MACHINE_STATE, "zh")

    def test_protected_identity_uses_path_and_cannot_collapse_duplicate_text(self) -> None:
        duplicate_state = {
            **ENGLISH_MACHINE_STATE,
            "protected": ["Keep this boundary.", "Keep this boundary."],
            "parked_opportunities": [],
        }
        source = build_presentation_source(duplicate_state)
        protected_ids = [
            entry.item_id for entry in source.entries if entry.path.startswith("protected[")
        ]
        self.assertEqual(2, len(protected_ids))
        self.assertEqual(2, len(set(protected_ids)))
        self.assertEqual(2, source.protected_item_count)

    def test_unknown_repair_outcome_preserves_known_usage_without_zero_sentinel(self) -> None:
        from standalone.providers import ProviderRequestError

        queue: list[object] = [
            CompletionResult(
                content=json.dumps(COVERAGE_STATE),
                model="kimi-k2.6",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
            CompletionResult(
                content=json.dumps(adjudication(ENGLISH_MACHINE_STATE)),
                model="kimi-k2.6",
                usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
            ),
            ProviderRequestError(
                "provider response timed out after 900 seconds; the request was not automatically resent "
                "because server-side execution status is unknown"
            ),
        ]
        calls = 0

        def mocked_complete(client: ChatCompletionClient, _messages: object, **_kwargs: object) -> CompletionResult:
            nonlocal calls
            value = queue[calls]
            calls += 1
            if client.on_attempt is not None:
                client.on_attempt(1)
            if isinstance(value, Exception):
                raise value
            return value

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "local-test-key"}, clear=True
        ), patch.object(
            ChatCompletionClient, "complete", autospec=True, side_effect=mocked_complete
        ):
            path = Path(directory) / "paper.md"
            path.write_text(long_manuscript(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path, provider="kimi", model="kimi-k2.6",
                    reasoning_option="enabled", output_language="zh",
                    manuscript_identity="paper-v1", confirm_complete_current_manuscript=True,
                )
            )
        runtime = result.as_dict()["runtime"]
        self.assertEqual(3, calls)
        self.assertEqual("UNKNOWN", runtime["provider_outcome"])
        self.assertEqual("PARTIAL", runtime["usage_status"])
        self.assertEqual(33, runtime["usage"]["total_tokens"])
        self.assertEqual({}, runtime["usage_calls"][2])
        self.assertNotIn("total_tokens", runtime["usage_calls"][2])


    def test_gui_has_one_visible_hold_terminal_and_no_fourth_api_call(self) -> None:
        queue = iter(
            [
                CompletionResult(
                    content=json.dumps(COVERAGE_STATE),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ),
                CompletionResult(
                    content=json.dumps(adjudication(ENGLISH_MACHINE_STATE)),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                ),
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
            os.environ, {"MOONSHOT_API_KEY": "local-test-key"}, clear=True
        ), patch.object(
            ChatCompletionClient,
            "complete",
            autospec=True,
            side_effect=mocked_complete,
        ), patch(
            "standalone.web_gui.price_with_fallback", return_value=None
        ):
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
        self.assertEqual("completed_with_interpretation_hold", snapshot["phase"])
        self.assertEqual("HOLD", snapshot["result"]["runtime"]["terminal_status"])
        self.assertEqual(
            1,
            sum(item["phase"] == "completed_with_interpretation_hold" for item in snapshot["timeline"]),
        )
        self.assertEqual(0, sum(item["phase"] == "failed" for item in snapshot["timeline"]))
        self.assertIn("was not sent", snapshot["interpretation_error"])


if __name__ == "__main__":
    unittest.main()
