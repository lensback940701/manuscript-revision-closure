from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from standalone import harness
from standalone.assessor import RunOptions, analyze_manuscript
from standalone.events import EventSink
from standalone.prompting import build_coverage_messages
from standalone.providers import (
    ChatCompletionClient,
    CompletionResult,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderRequestError,
    provider_capability,
)
from standalone.web_gui import GuiState, reduce_gui_terminal_state


def _config(provider: str = "gemini", model: str = "gemini-3.7-flash") -> ProviderConfig:
    return ProviderConfig(
        name=provider,
        model=model,
        base_url="http://127.0.0.1:8765",
        api_key="mock",
        key_variable="MRC_TEST_KEY",
    )


def _coverage(candidates: list[str]) -> dict:
    candidate_set = set(candidates)
    return {
        "coverage_contract_version": harness.COVERAGE_CONTRACT_VERSION,
        "whole_manuscript_basis": "SUFFICIENT",
        "basis_reason_codes": ["SUFFICIENT_SUBSTANTIVE_WHOLE_MANUSCRIPT"],
        "basis_explanation": "The supplied text contains sufficient substantive whole-manuscript material.",
        "manuscript_identity_confirmed": True,
        "full_span_covered": True,
        "dimensions": [
            {
                "dimension": dimension,
                "applicability": "APPLICABLE",
                "assessed": True,
                "status": (
                    "POTENTIAL_MATERIAL_ROOT_CAUSE" if dimension in candidate_set else "CLEAR"
                ),
                "affirmative_sufficiency": True,
                "sufficiency_reason_code": "AFFIRMATIVE_MANUSCRIPT_SUPPORT",
            }
            for dimension in harness.COVERAGE_DIMENSIONS
        ],
        "root_cause_candidate_dimensions": list(candidates),
        "evidence_hold_codes": [],
        "submission_hold_codes": [],
        "protected_invariants": {
            "claim_ceiling_preserved": True,
            "evidence_status_distinctions_preserved": True,
            "rivals_and_negative_findings_preserved": True,
        },
    }


def _stop_sufficiency(*, unresolved: str | None = None) -> list[dict[str, object]]:
    return [
        {
            "dimension": dimension,
            "assessed": True,
            "affirmative_sufficiency": dimension != unresolved,
            "unresolved_material_concern": dimension == unresolved,
            "sufficiency_reason_code": (
                "UNRESOLVED_MATERIAL_CONCERN"
                if dimension == unresolved
                else "AFFIRMATIVE_MANUSCRIPT_SUPPORT"
            ),
        }
        for dimension in harness.AFFIRMATIVE_STOP_DIMENSIONS
    ]


def _cause(
    dimension: str,
    *,
    required: bool = True,
    material: bool = True,
    scope: str = "local",
) -> dict[str, object]:
    return {
        "observed": material,
        "locatable": material,
        "dimension": dimension,
        "origin": "COVERAGE_CANDIDATE" if required else "INDEPENDENT_ADDITION",
        "coverage_disagreement": not required,
        "disposition_reason_code": (
            "MATERIAL_CONCERN_CONFIRMED" if material else "NOT_OBSERVED"
        ),
        "author_decision_required": False,
        "style_only": False,
        "hold_only": False,
        "verification_only": False,
        "expected_benefit_exceeds_risk": material,
        "scope": scope,
    }


class Mrc063FailureFirstTests(unittest.TestCase):
    def test_transient_then_timeout_is_one_physical_attempt_by_default(self) -> None:
        overload = urllib.error.HTTPError(
            "http://127.0.0.1:8765/chat/completions",
            503,
            "Service Unavailable",
            {"Retry-After": "12"},
            io.BytesIO(b'{"error":{"message":"temporary overload"}}'),
        )
        with patch(
            "standalone.providers.urllib.request.urlopen",
            side_effect=[overload, socket.timeout("second request must not happen")],
        ) as mocked, patch("standalone.providers.time.sleep"):
            with self.assertRaises(ProviderRequestError):
                ChatCompletionClient(_config()).complete([{"role": "user", "content": "mock"}])
        self.assertEqual(1, mocked.call_count)

    def test_nonzero_core_retry_configuration_fails_closed(self) -> None:
        self.assertEqual(0, ChatCompletionClient(_config()).max_transient_retries)
        with self.assertRaises(ProviderConfigurationError):
            ChatCompletionClient(_config(), max_transient_retries=1)

    def test_deepseek_prompt_contains_complete_canonical_coverage_schema_and_hash(self) -> None:
        messages = build_coverage_messages(
            "synthetic manuscript marker only",
            manuscript_identity="synthetic-v1",
        )
        system = messages[0]["content"]
        canonical = json.dumps(
            harness.COVERAGE_JSON_SCHEMA,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertIn(canonical, system)
        self.assertIn(digest, system)
        self.assertIn('"additionalProperties":false', system)

    def test_adjudication_schema_is_dynamic_and_order_stable(self) -> None:
        builder = getattr(harness, "build_adjudication_json_schema", None)
        self.assertTrue(callable(builder))
        first = harness.validate_coverage(_coverage(["contribution", "methods_and_research_design"]))
        second = harness.validate_coverage(_coverage(["methods_and_research_design", "contribution"]))
        first_schema = builder(first)
        second_schema = builder(second)
        rows = first_schema["properties"]["material_root_causes"]
        self.assertEqual(2, rows["minItems"])
        self.assertEqual(len(harness.COVERAGE_DIMENSIONS), rows["maxItems"])
        self.assertEqual(
            list(harness.COVERAGE_DIMENSIONS),
            rows["items"]["properties"]["dimension"]["enum"],
        )
        first_hash = hashlib.sha256(
            json.dumps(first_schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        second_hash = hashlib.sha256(
            json.dumps(second_schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(first_hash, second_hash)

    def test_machine_hold_has_distinct_gui_terminal_state(self) -> None:
        state = GuiState()
        self.assertTrue(state.start())
        result = {
            "closure_card": {"Verdict": "UNASSESSED"},
            "runtime": {
                "machine_status": "HOLD",
                "presentation_status": "NOT_STARTED",
                "terminal_status": "HOLD",
                "machine_receipt": {
                    "failed_stage": "coverage_provider",
                    "error_message": "provider outcome unknown",
                },
            },
        }
        state.core_ready(result)
        machine_hold = getattr(state, "machine_hold", None)
        self.assertTrue(callable(machine_hold))
        machine_hold("coverage_provider: provider outcome unknown")
        snapshot = state.snapshot()
        self.assertEqual("completed_with_machine_hold", snapshot["phase"])
        self.assertNotIn("机器裁决已完成", snapshot["message"])
        self.assertIsNone(snapshot["presentation_error"])

    def test_429_502_503_504_matrix_stops_once_with_bounded_receipt(self) -> None:
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                failure = urllib.error.HTTPError(
                    "http://127.0.0.1:8765/chat/completions",
                    status,
                    "bounded mock",
                    {"Retry-After": "9"},
                    io.BytesIO(
                        b'{"error":{"message":"Authorization: Bearer must-not-persist-opaque"}}'
                    ),
                )
                with patch("standalone.providers.urllib.request.urlopen", side_effect=failure) as mocked:
                    with self.assertRaises(ProviderRequestError) as raised:
                        ChatCompletionClient(_config()).complete(
                            [{"role": "user", "content": "synthetic"}],
                            stage="coverage",
                        )
                self.assertEqual(1, mocked.call_count)
                receipt = raised.exception.request_receipts[0]
                self.assertEqual(status, receipt["http_status"])
                self.assertEqual("9", receipt["retry_after"])
                self.assertEqual("REJECTED" if status == 429 else "UNKNOWN", receipt["provider_outcome"])
                self.assertEqual("UNKNOWN", receipt["usage_status"])
                self.assertEqual({}, receipt["usage"])
                self.assertNotIn("must-not-persist", json.dumps(receipt))
                self.assertEqual(
                    "[REDACTED_SENSITIVE_PROVIDER_DETAIL]",
                    receipt["provider_error_detail"],
                )

    def test_socket_timeout_and_url_error_are_unknown_single_attempts(self) -> None:
        failures = (socket.timeout("mock timeout"), urllib.error.URLError("mock disconnect"))
        for failure in failures:
            with self.subTest(error=type(failure).__name__), patch(
                "standalone.providers.urllib.request.urlopen", side_effect=failure
            ) as mocked:
                with self.assertRaises(ProviderRequestError) as raised:
                    ChatCompletionClient(_config()).complete(
                        [{"role": "user", "content": "synthetic"}], stage="adjudication"
                    )
            self.assertEqual(1, mocked.call_count)
            receipt = raised.exception.request_receipts[0]
            self.assertEqual("UNKNOWN", receipt["provider_outcome"])
            self.assertEqual("UNKNOWN", receipt["usage_status"])
            self.assertTrue(receipt["request_dispatched"])

    def test_provider_capability_registry_distinguishes_schema_delivery(self) -> None:
        deepseek = provider_capability("deepseek")
        self.assertFalse(deepseek["supports_strict_json_schema"])
        self.assertTrue(deepseek["supports_json_object_mode"])
        self.assertEqual("prompt_canonical_schema_plus_json_object", deepseek["schema_delivery_mode"])
        for provider in ("kimi", "gemini"):
            capability = provider_capability(provider)
            self.assertTrue(capability["supports_strict_json_schema"])
            self.assertIn("api_strict_json_schema", capability["schema_delivery_mode"])

    def test_schema_failures_have_top_level_and_nested_bounded_receipts(self) -> None:
        coverage = harness.validate_coverage(_coverage(["contribution"]))
        schema = harness.build_adjudication_json_schema(coverage)
        valid = {
            "coverage_digest_sha256": harness.canonical_digest(coverage),
            "material_root_causes": [_cause("contribution", material=False)],
            "affirmative_sufficiency": _stop_sufficiency(),
            "evidence_hold_codes": [],
            "submission_hold_codes": [],
            "protected": ["保持当前论点上限。"],
            "parked_opportunities": [],
            "lite_suggestions": [],
        }
        cases = []
        missing = dict(valid)
        missing.pop("protected")
        cases.append((missing, "$", "key_set"))
        extra = dict(valid, unexpected=True)
        cases.append((extra, "$", "key_set"))
        nested = json.loads(json.dumps(valid))
        nested["material_root_causes"][0].pop("scope")
        cases.append((nested, "$.material_root_causes[0]", "key_set"))
        wrong_type = json.loads(json.dumps(valid))
        wrong_type["material_root_causes"][0]["observed"] = "false"
        cases.append((wrong_type, "$.material_root_causes[0].observed", "type"))
        wrong_enum = json.loads(json.dumps(valid))
        wrong_enum["material_root_causes"][0]["scope"] = "approximate"
        cases.append((wrong_enum, "$.material_root_causes[0].scope", "enum"))
        for value, path, error_kind in cases:
            with self.subTest(path=path, error_kind=error_kind), self.assertRaises(
                harness.SchemaContractError
            ) as raised:
                harness.validate_json_schema_contract(
                    value,
                    schema,
                    contract_version=harness.ADJUDICATION_CONTRACT_VERSION,
                )
            receipt = raised.exception.contract_receipt
            self.assertEqual(path, receipt["failed_path"])
            self.assertEqual(error_kind, receipt["error_kind"])
            self.assertEqual(harness.schema_sha256(schema), receipt["schema_sha256"])
            self.assertEqual(
                {"required_keys", "observed_keys", "missing_keys", "extra_keys"},
                {key for key in receipt if key.endswith("keys")},
            )

    def test_candidate_lower_bound_receipts_cover_missing_addition_duplicate_and_positive_matrix(self) -> None:
        coverage = harness.validate_coverage(
            _coverage(["contribution", "methods_and_research_design"])
        )
        positive = {
            "material_root_causes": [
                _cause("methods_and_research_design"),
                _cause("contribution"),
                _cause("theory_and_concepts", required=False),
            ]
        }
        receipt = harness.validate_candidate_binding(coverage, positive)
        self.assertEqual([], receipt["missing_candidates"])
        self.assertEqual(["theory_and_concepts"], receipt["independent_additions"])
        self.assertEqual([], receipt["duplicate_candidates"])
        cases = {
            "missing": {"material_root_causes": [_cause("contribution")]},
            "ungrounded": {
                "material_root_causes": [
                    _cause("contribution"),
                    _cause("methods_and_research_design"),
                    _cause("theory_and_concepts", required=False, material=False),
                ]
            },
            "duplicate": {
                "material_root_causes": [
                    _cause("contribution"),
                    _cause("contribution"),
                    _cause("methods_and_research_design"),
                ]
            },
        }
        for label, state in cases.items():
            with self.subTest(label=label), self.assertRaises(
                harness.CandidateSetContractError
            ) as raised:
                harness.validate_candidate_binding(coverage, state)
            failed = raised.exception.contract_receipt
            failed_key = {
                "missing": "missing_candidates",
                "ungrounded": "ungrounded_additions",
                "duplicate": "duplicate_candidates",
            }[label]
            self.assertTrue(failed[failed_key])

        for candidates in ([], ["contribution"], ["contribution", "methods_and_research_design"]):
            with self.subTest(positive_count=len(candidates)):
                current = harness.validate_coverage(_coverage(list(candidates)))
                state = {
                    "material_root_causes": [_cause(item) for item in reversed(candidates)]
                }
                harness.validate_candidate_binding(current, state)

    def test_gui_truth_table_and_configuration_failure(self) -> None:
        self.assertEqual("completed", reduce_gui_terminal_state("SUCCEEDED", "PASS"))
        self.assertEqual(
            "completed_with_presentation_hold",
            reduce_gui_terminal_state("SUCCEEDED", "HOLD"),
        )
        self.assertEqual(
            "completed_with_machine_hold",
            reduce_gui_terminal_state("HOLD", "NOT_STARTED"),
        )
        self.assertEqual(
            "failed",
            reduce_gui_terminal_state(None, "NOT_STARTED", configuration_failed=True),
        )

    def test_candidate_failure_is_machine_hold_with_no_authoritative_presentation_source(self) -> None:
        coverage = harness.validate_coverage(_coverage(["contribution"]))
        adjudication = {
            "coverage_digest_sha256": harness.canonical_digest(coverage),
            "material_root_causes": [],
            "affirmative_sufficiency": _stop_sufficiency(),
            "evidence_hold_codes": [],
            "submission_hold_codes": [],
            "protected": ["UNPUBLISHED-CANDIDATE-NATURAL-LANGUAGE"],
            "parked_opportunities": [],
            "lite_suggestions": [],
        }
        completions = iter(
            (
                CompletionResult(
                    content=json.dumps(coverage),
                    model="gemini-3.6-flash",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ),
                CompletionResult(
                    content=json.dumps(adjudication),
                    model="gemini-3.6-flash",
                    usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                ),
            )
        )

        def mocked_complete(client: ChatCompletionClient, _messages: object, **_kwargs: object) -> CompletionResult:
            if client.on_attempt is not None:
                client.on_attempt(1)
            return next(completions)

        manuscript = (
            "Synthetic Contract Fixture\n\nAbstract\n"
            + ("Bounded synthetic argument and evidence.\n" * 80)
            + "\nConclusion\nSynthetic conclusion.\n\nReferences\nSynthetic reference."
        )
        sink = EventSink()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"GEMINI_API_KEY": "mock"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete", new=mocked_complete):
            path = Path(directory) / "synthetic.md"
            path.write_text(manuscript, encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="gemini",
                    model="gemini-3.6-flash",
                    confirm_complete_current_manuscript=True,
                    manuscript_identity="synthetic-candidate-mismatch",
                    provider_transmission_consent=True,
                ),
                event_sink=sink,
            )
        runtime = result.as_dict()["runtime"]
        minimal = result.as_dict()["minimal_receipt"]
        self.assertEqual("HOLD", runtime["machine_status"])
        self.assertEqual("NOT_STARTED", runtime["presentation_status"])
        self.assertIsNone(runtime["machine_receipt"]["authoritative_presentation_source"])
        self.assertFalse(runtime["machine_receipt"]["authoritative_candidate_state"])
        self.assertEqual("TECHNICAL_EXECUTION_HOLD", minimal["reason_category"])
        self.assertEqual("adjudication_contract", minimal["failed_stage"])
        self.assertEqual([], minimal["evidence_hold_codes"])
        self.assertEqual([], minimal["submission_hold_codes"])
        diagnostic = runtime["machine_receipt"]["bounded_contract_failure"]
        self.assertEqual(["contribution"], diagnostic["missing_candidates"])
        self.assertEqual(2, runtime["physical_request_attempt_count"])
        self.assertEqual(2, runtime["successful_completion_count"])
        self.assertEqual(2, runtime["usage_receipt_count"])
        public_json = json.dumps(result.as_dict(), ensure_ascii=False)
        self.assertNotIn("UNPUBLISHED-CANDIDATE-NATURAL-LANGUAGE", public_json)
        attempts = [event for event in sink.events if event["type"] == "provider.attempt"]
        self.assertEqual(2, len(attempts))
        self.assertTrue(
            all(event.get("provider") == "gemini" and event.get("model") == "gemini-3.6-flash" for event in attempts)
        )
        self.assertEqual(1, sum(event["type"] == "turn.completed" for event in sink.events))


if __name__ == "__main__":
    unittest.main()
