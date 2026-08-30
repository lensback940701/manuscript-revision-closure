from __future__ import annotations

import io
import hashlib
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from standalone.assessor import (
    RunOptions,
    analyze_manuscript,
    prepare_provider_transmission_consent,
    validate_model_state,
)
from standalone.cli import build_parser
from standalone.document_reader import read_document
from standalone.harness import (
    ADJUDICATION_CONTRACT_VERSION,
    AFFIRMATIVE_STOP_DIMENSIONS,
    AFFIRMATIVE_STOP_CONTRACT_VERSION,
    CANDIDATE_BINDING_CONTRACT_VERSION,
    CANDIDATE_EXACT_SET_CONTRACT_VERSION,
    COVERAGE_CONTRACT_VERSION,
    COVERAGE_DIMENSIONS,
    COVERAGE_JSON_SCHEMA,
    DYNAMIC_ADJUDICATION_SCHEMA_VERSION,
    PROTECTED_INVARIANT_KEYS,
    SCHEMA_DEFINITION_LINT_VERSION,
    SCHEMA_DELIVERY_CONTRACT_VERSION,
    SchemaContractError,
    SchemaDefinitionError,
    affirmative_stop_gate_receipt,
    analyze_intake_structure,
    build_adjudication_json_schema,
    canonical_digest,
    validate_adjudication_binding,
    validate_candidate_exact_set,
    validate_candidate_binding,
    validate_cross_stage_consistency,
    validate_json_schema_contract,
    validate_schema_definition,
)
from standalone.prompting import build_adjudication_messages, build_coverage_messages
from standalone.providers import (
    ChatCompletionClient,
    CompletionResult,
    ProviderConfig,
    ProviderRequestError,
)
from standalone.web_gui import GuiState
from scripts.closure_state import (
    ClosureStateError,
    TECHNICAL_FAILED_STAGES,
    decide_state,
    minimal_receipt,
    prior_receipt_status,
)


ROOT = Path(__file__).resolve().parents[1]


def _complete_markdown(*, numbered: bool = False) -> str:
    section = "1 Introduction" if numbered else "Introduction"
    return (
        "# Synthetic Complete Manuscript\n\n"
        "## Abstract\n\n"
        + ("Bounded synthetic argument, evidence, and scope.\n" * 80)
        + f"\n## {section}\n\nSynthetic section.\n\n"
        "## Conclusion\n\nSynthetic conclusion.\n\n"
        "## References\n\nSynthetic reference.\n"
    )


def _s1_s6_markdown() -> str:
    return (
        "# Synthetic S-Series Manuscript\n\n"
        "## Abstract\n\n"
        + ("Bounded synthetic argument, evidence, and scope.\n" * 80)
        + "\n## S1. Introduction\nText.\n"
        "## S2. Theory\nText.\n"
        "### Unnumbered child heading\nText.\n"
        "## S3. Methods\nText.\n"
        "## S4. Findings\nText.\n"
        "## S5. Discussion\nText.\n"
        "## S6. Conclusion\nText.\n"
        "## References\nReference.\n"
    )


def _config() -> ProviderConfig:
    return ProviderConfig(
        name="gemini",
        model="gemini-3.7-flash",
        base_url="http://127.0.0.1:8765",
        api_key="mock",
        key_variable="MRC_TEST_KEY",
    )


def _coverage_state(*, basis: str = "SUFFICIENT") -> dict[str, object]:
    sufficient = basis == "SUFFICIENT"
    return {
        "coverage_contract_version": COVERAGE_CONTRACT_VERSION,
        "whole_manuscript_basis": basis,
        "basis_reason_codes": (
            ["SUFFICIENT_SUBSTANTIVE_WHOLE_MANUSCRIPT"]
            if sufficient
            else ["FRAGMENT_OR_EXCERPT_ONLY"]
        ),
        "basis_explanation": (
            "The supplied text contains sufficient substantive whole-manuscript material."
            if sufficient
            else "The supplied material is substantively only a fragment or excerpt."
        ),
        "manuscript_identity_confirmed": True,
        "full_span_covered": sufficient,
        "dimensions": [
            {
                "dimension": dimension,
                "applicability": "APPLICABLE",
                "assessed": sufficient,
                "status": "CLEAR" if sufficient else "UNASSESSED",
                "affirmative_sufficiency": sufficient,
                "sufficiency_reason_code": (
                    "AFFIRMATIVE_MANUSCRIPT_SUPPORT" if sufficient else "UNASSESSED"
                ),
            }
            for dimension in COVERAGE_DIMENSIONS
        ],
        "root_cause_candidate_dimensions": [],
        "evidence_hold_codes": [],
        "submission_hold_codes": [],
        "protected_invariants": {
            key: sufficient for key in PROTECTED_INVARIANT_KEYS
        },
    }


def _adjudication_state(coverage: dict[str, object]) -> dict[str, object]:
    return {
        "coverage_digest_sha256": canonical_digest(coverage),
        "material_root_causes": [],
        "affirmative_sufficiency": [
            {
                "dimension": dimension,
                "assessed": True,
                "affirmative_sufficiency": True,
                "unresolved_material_concern": False,
                "sufficiency_reason_code": "AFFIRMATIVE_MANUSCRIPT_SUPPORT",
            }
            for dimension in AFFIRMATIVE_STOP_DIMENSIONS
        ],
        "evidence_hold_codes": [],
        "submission_hold_codes": [],
        "protected": [],
        "parked_opportunities": [],
        "lite_suggestions": [],
    }


def _semantic_cause(
    dimension: str,
    *,
    coverage_candidate: bool = False,
    scope: str = "local",
    material: bool = True,
    observed: bool | None = None,
    locatable: bool | None = None,
) -> dict[str, object]:
    observed_value = material if observed is None else observed
    locatable_value = material if locatable is None else locatable
    return {
        "observed": observed_value,
        "locatable": locatable_value,
        "dimension": dimension,
        "origin": "COVERAGE_CANDIDATE" if coverage_candidate else "INDEPENDENT_ADDITION",
        "coverage_disagreement": not coverage_candidate,
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


def _semantic_adjudication(
    coverage: dict[str, object],
    *,
    causes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    rows = list(causes or [])
    unresolved = {
        str(row["dimension"])
        for row in rows
        if row.get("observed") is True
        and row.get("locatable") is True
        and row.get("style_only") is False
        and row.get("hold_only") is False
        and row.get("verification_only") is False
        and row.get("expected_benefit_exceeds_risk") is True
    }
    state = _adjudication_state(coverage)
    state["material_root_causes"] = rows
    state["affirmative_sufficiency"] = [
        {
            "dimension": dimension,
            "assessed": True,
            "affirmative_sufficiency": dimension not in unresolved,
            "unresolved_material_concern": dimension in unresolved,
            "sufficiency_reason_code": (
                "UNRESOLVED_MATERIAL_CONCERN"
                if dimension in unresolved
                else "AFFIRMATIVE_MANUSCRIPT_SUPPORT"
            ),
        }
        for dimension in AFFIRMATIVE_STOP_DIMENSIONS
    ]
    return state


def _semantic_decision(
    coverage: dict[str, object], envelope: dict[str, object]
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    finite = validate_adjudication_binding(envelope, coverage)
    model_state = validate_model_state(finite)
    validate_cross_stage_consistency(coverage, model_state)
    stop_gate = affirmative_stop_gate_receipt(coverage, model_state)
    state: dict[str, object] = {
        "manuscript_complete": True,
        "current_identity_clear": True,
        "whole_manuscript_read": True,
        "critical_basis_available": True,
        "bounded_scope": False,
        "current_manuscript_identity": "synthetic-semantic-control",
        "evidence_hold_codes": [],
        "submission_hold_codes": [],
        "protected": [],
        "parked_opportunities": [],
        "lite_suggestions": [],
        "invalidation_events": [],
        "artifact_only_drift_verified": False,
        "formal_tone": False,
        "rewrite_requested": False,
        **model_state,
        "affirmative_stop_gate_passed": stop_gate["stop_eligible"],
    }
    return decide_state(state), model_state, stop_gate


def _technical_result(*, body: bytes | None = None, output_language: str = "en"):
    payload = body or (
        b'{"error":{"status":"UNAVAILABLE","code":"backend_unavailable",'
        b'"message":"capacity temporarily unavailable"}}'
    )
    failure = urllib.error.HTTPError(
        "http://127.0.0.1:8765/chat/completions",
        503,
        "Service Unavailable",
        {"Retry-After": "11"},
        io.BytesIO(payload),
    )
    with tempfile.TemporaryDirectory() as directory, patch.dict(
        os.environ, {"GEMINI_API_KEY": "mock"}, clear=True
    ), patch("standalone.providers.urllib.request.urlopen", side_effect=failure):
        path = Path(directory) / "synthetic.md"
        path.write_text(_complete_markdown(numbered=True), encoding="utf-8")
        return analyze_manuscript(
            RunOptions(
                manuscript_path=path,
                provider="gemini",
                model="gemini-3.7-flash",
                output_language=output_language,
                confirm_complete_current_manuscript=True,
                manuscript_identity="synthetic-technical-hold",
                provider_transmission_consent=True,
            )
        )


def _numbered_markdown(headings: list[tuple[int, str]]) -> str:
    rows = ["# Synthetic Numbered Manuscript", "", "## Abstract", ""]
    rows.extend(["Bounded synthetic argument and evidence."] * 80)
    rows.append("")
    for level, heading in headings:
        rows.extend(["#" * level + " " + heading, "Text.", ""])
    rows.extend(["## References", "Reference."])
    return "\n".join(rows)


def _moonshot_schema_definition_errors(
    schema: object,
    *,
    path: str = "$",
) -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors
    enum = schema.get("enum")
    if isinstance(enum, list) and not enum:
        errors.append(path + ".enum: enum array cannot be empty")
    for key, value in schema.items():
        if isinstance(value, dict):
            errors.extend(_moonshot_schema_definition_errors(value, path=f"{path}.{key}"))
        elif isinstance(value, list) and key != "enum":
            for index, item in enumerate(value):
                errors.extend(
                    _moonshot_schema_definition_errors(item, path=f"{path}.{key}[{index}]")
                )
    return errors


class _KimiSchemaResponse:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_KimiSchemaResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _kimi_schema_checking_urlopen(request: object, **_kwargs: object) -> _KimiSchemaResponse:
    body = json.loads(getattr(request, "data").decode("utf-8"))
    response_format = body["response_format"]
    schema = (
        response_format["json_schema"]["schema"]
        if response_format.get("type") == "json_schema"
        else None
    )
    errors = _moonshot_schema_definition_errors(schema) if schema is not None else []
    if errors:
        detail = (
            "Invalid request: response_format.json_schema is not a valid moonshot flavored "
            "json schema, details: <At path "
            "'properties.material_root_causes.items.properties.dimension.enum': "
            "enum array cannot be empty>"
        )
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8765/chat/completions",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({"error": {"message": detail}}).encode("utf-8")),
        )
    return _KimiSchemaResponse(
        {
            "model": "kimi-k2.6",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "coverage_digest_sha256": "0" * 64,
                                "material_root_causes": [],
                                "evidence_hold_codes": [],
                                "submission_hold_codes": [],
                                "protected": [],
                                "parked_opportunities": [],
                                "lite_suggestions": [],
                            }
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


class _KimiZeroCandidateScenario:
    def __init__(self) -> None:
        self.coverage = _coverage_state(basis="SUFFICIENT")
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: object, **_kwargs: object) -> _KimiSchemaResponse:
        body = json.loads(getattr(request, "data").decode("utf-8"))
        self.requests.append(body)
        schema = body["response_format"]["json_schema"]["schema"]
        errors = _moonshot_schema_definition_errors(schema)
        if errors:
            detail = (
                "Invalid request: response_format.json_schema is not a valid moonshot flavored "
                "json schema, details: <At path "
                "'properties.material_root_causes.items.properties.dimension.enum': "
                "enum array cannot be empty>"
            )
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8765/chat/completions",
                400,
                "Bad Request",
                {},
                io.BytesIO(json.dumps({"error": {"message": detail}}).encode("utf-8")),
            )
        properties = schema.get("properties", {})
        if "coverage_contract_version" in properties:
            state: dict[str, object] = self.coverage
        else:
            state = _adjudication_state(self.coverage)
        return _KimiSchemaResponse(
            {
                "model": "kimi-k2.6",
                "choices": [
                    {
                        "message": {"content": json.dumps(state)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                },
            }
        )


def _missing_title_markdown(first_heading: str) -> str:
    return (
        f"{first_heading}\n\n"
        + ("Bounded synthetic section content.\n" * 80)
        + "\n## Abstract\n\nSynthetic abstract.\n\n"
        "## Introduction\n\nSynthetic introduction.\n\n"
        "## Conclusion\n\nSynthetic conclusion.\n\n"
        "## References\n\nSynthetic reference.\n"
    )


def _front_matter_markdown(front_matter: str, *, body_title: str | None = None) -> str:
    title = f"{body_title}\n\n" if body_title is not None else ""
    return (
        f"\n---\n{front_matter}\n---\n\n{title}"
        "## Abstract\n\n"
        + ("Bounded synthetic section content.\n" * 80)
        + "\n## Introduction\n\nSynthetic introduction.\n\n"
        "## Conclusion\n\nSynthetic conclusion.\n\n"
        "## References\n\nSynthetic reference.\n"
    )


def _assert_intake_hold_without_provider(test_case: unittest.TestCase, text: str, identity: str) -> None:
    receipt = analyze_intake_structure(text)
    test_case.assertFalse(receipt.title_present)
    test_case.assertFalse(receipt.complete_structure)
    with tempfile.TemporaryDirectory() as directory, patch.dict(
        os.environ, {"GEMINI_API_KEY": "mock"}, clear=True
    ), patch("standalone.assessor.ChatCompletionClient.complete") as provider_call:
        path = Path(directory) / "synthetic-front-matter.md"
        path.write_text(text, encoding="utf-8")
        result = analyze_manuscript(
            RunOptions(
                manuscript_path=path,
                provider="gemini",
                model="gemini-3.7-flash",
                confirm_complete_current_manuscript=True,
                manuscript_identity=identity,
                provider_transmission_consent=True,
                output_language="en",
            )
        ).as_dict()
    provider_call.assert_not_called()
    test_case.assertFalse(result["runtime"]["api_called"])
    test_case.assertEqual(
        "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS",
        result["minimal_receipt"]["reason_category"],
    )


class Mrc064FailureFirstTests(unittest.TestCase):
    def test_01_technical_hold_minimal_receipt_matches_machine_surface(self) -> None:
        public = _technical_result().as_dict()
        receipt = public["minimal_receipt"]
        machine = public["runtime"]["machine_receipt"]
        self.assertEqual("TECHNICAL_EXECUTION_HOLD", receipt["reason_category"])
        self.assertEqual(machine["failed_stage"], receipt["failed_stage"])
        self.assertEqual(
            "mrc-technical-hold-receipt-1.0",
            receipt["technical_hold_contract_version"],
        )
        self.assertNotIn("Provide one complete", receipt["next_permitted_action"])
        card = public["closure_card"]
        self.assertEqual(machine["failed_stage"], card["Failed stage"])
        self.assertEqual(receipt["next_permitted_action"], machine["next_permitted_action"])
        self.assertEqual(receipt["next_permitted_action"], card["Next permitted action"])
        self.assertEqual(receipt["technical_hold_contract_version"], card["Technical hold contract version"])
        self.assertEqual("HOLD", public["runtime"]["machine_status"])
        self.assertEqual("NOT_STARTED", public["runtime"]["presentation_status"])
        self.assertEqual([], receipt["evidence_hold_codes"])
        self.assertEqual([], receipt["submission_hold_codes"])

    def test_02_provider_safe_error_detail_is_retained(self) -> None:
        failure = urllib.error.HTTPError(
            "http://127.0.0.1:8765/chat/completions",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"error":{"status":"UNAVAILABLE","code":"backend_unavailable","message":"capacity temporarily unavailable"}}'),
        )
        with patch("standalone.providers.urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(ProviderRequestError) as raised:
                ChatCompletionClient(_config()).complete(
                    [{"role": "user", "content": "synthetic"}], stage="coverage"
                )
        receipt = raised.exception.request_receipts[0]
        self.assertEqual("UNAVAILABLE", receipt["provider_error_status"])
        self.assertEqual("backend_unavailable", receipt["provider_error_code"])
        self.assertEqual("capacity temporarily unavailable", receipt["provider_error_detail"])

    def test_03_s1_s6_atx_structure_is_complete(self) -> None:
        receipt = analyze_intake_structure(_s1_s6_markdown())
        self.assertTrue(receipt.complete_structure)
        self.assertTrue(receipt.conclusion_present)
        self.assertGreaterEqual(receipt.heading_count, 9)

    def test_04_unnumbered_atx_is_pass_with_nonblocking_advisory(self) -> None:
        receipt = analyze_intake_structure(_complete_markdown())
        self.assertTrue(receipt.complete_structure)
        self.assertIn("HEADING_NUMBERING_STYLE_REVIEW", receipt.advisory_codes)
        serialized = json.dumps(receipt.as_dict(), ensure_ascii=False)
        self.assertIn("当前稿件章节结构完整，但标题编号或层级形式可能需要统一", serialized)

    def test_05_readme_does_not_claim_bounded_retry_for_overload(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("responses receive bounded retries", readme)
        self.assertIn("never trigger an automatic full-request resend", readme)
        for relative in (
            "README.zh-CN.md",
            "STANDALONE.zh-CN.md",
            "docs/HARNESS_EQUIVALENCE_AUDIT.zh-CN.md",
            "docs/NATIVE_PRESENTATION_TRANSACTION_AUDIT.zh-CN.md",
        ):
            content = (ROOT / relative).read_text(encoding="utf-8")
            self.assertRegex(content, r"不(?:会|得)自动重发")

    def test_06_semantic_basis_insufficient_uses_one_coverage_and_no_adjudication(self) -> None:
        incomplete = _complete_markdown().replace("## Conclusion\n\nSynthetic conclusion.\n\n", "")
        calls: list[str] = []

        def mocked_complete(client: ChatCompletionClient, _messages: object, **kwargs: object) -> CompletionResult:
            calls.append(str(kwargs.get("stage")))
            if client.on_attempt is not None:
                client.on_attempt(1)
            return CompletionResult(
                content=json.dumps(_coverage_state(basis="INSUFFICIENT")),
                model="deepseek-v4-pro",
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            )

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "mock"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete", new=mocked_complete):
            path = Path(directory) / "incomplete.md"
            path.write_text(incomplete, encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    confirm_complete_current_manuscript=True,
                    manuscript_identity="synthetic-incomplete",
                    provider_transmission_consent=True,
                    output_language="en",
                )
            ).as_dict()
        self.assertEqual(
            "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS",
            result["minimal_receipt"]["reason_category"],
        )
        self.assertNotIn("failed_stage", result["minimal_receipt"])
        self.assertEqual(["coverage"], calls)
        self.assertTrue(result["runtime"]["api_called"])
        self.assertEqual(1, result["runtime"]["physical_request_attempt_count"])
        self.assertEqual("NOT_FORMED", result["runtime"]["machine_status"])
        self.assertEqual("NOT_STARTED", result["runtime"]["presentation_status"])
        self.assertIsNone(result["runtime"]["machine_receipt"]["authoritative_presentation_source"])
        self.assertEqual(25, result["runtime"]["usage"]["total_tokens"])
        self.assertEqual("INSUFFICIENT", result["minimal_receipt"]["whole_manuscript_basis"])
        self.assertNotIn("Failed stage", result["closure_card"])
        self.assertIn("fragment or excerpt", result["closure_card"]["Reason"])
        provider_receipt = result["runtime"]["provider_receipts"][0]
        physical_receipt = provider_receipt["physical_request_receipts"][0]
        surfaces = (
            result["minimal_receipt"],
            result["runtime"]["machine_receipt"],
            provider_receipt,
            physical_receipt,
        )
        for field in (
            "whole_manuscript_basis",
            "basis_reason_codes",
            "basis_explanation",
            "basis_contract_version",
        ):
            self.assertTrue(all(surface[field] == surfaces[0][field] for surface in surfaces))
        gui_state = GuiState()
        self.assertTrue(gui_state.start())
        gui_state.core_ready(result)
        snapshot = gui_state.snapshot()
        self.assertEqual(result, snapshot["result"])

    def test_07_technical_receipt_is_never_reusable_as_stable_stop(self) -> None:
        result = _technical_result().as_dict()
        receipt = result["minimal_receipt"]
        state = {
            "manuscript_complete": True,
            "current_identity_clear": True,
            "whole_manuscript_read": True,
            "critical_basis_available": True,
            "bounded_scope": False,
            "current_manuscript_identity": receipt["manuscript_identity"],
            "current_artifact_sha256": receipt["artifact_sha256"],
            "current_semantic_content_sha256": receipt["semantic_content_sha256"],
            "prior_receipt": receipt,
        }
        self.assertEqual((False, True), prior_receipt_status(state))
        self.assertEqual("UNASSESSED", receipt["verdict"])

    def test_08_gui_snapshot_and_serialized_result_keep_technical_surface(self) -> None:
        public = _technical_result().as_dict()
        state = GuiState()
        self.assertTrue(state.start())
        state.core_ready(public)
        card = public["closure_card"]
        message = (
            f"{card['Reason']}; failed stage: {card['Failed stage']}; "
            f"next: {card['Next permitted action']}"
        )
        state.machine_hold(message, manuscript_complete=True)
        snapshot = state.snapshot()
        self.assertEqual("completed_with_machine_hold", snapshot["phase"])
        self.assertEqual(public, snapshot["result"])
        serialized = json.dumps(snapshot["result"], ensure_ascii=False)
        for marker in (
            "TECHNICAL_EXECUTION_HOLD",
            "coverage_provider",
            "mrc-technical-hold-receipt-1.0",
        ):
            self.assertIn(marker, serialized)

    def test_09_provider_detail_privacy_and_shape_matrix(self) -> None:
        synthetic_secret_pattern = "sk-" + "abcdefghijklmnop"
        cases = (
            (b'{"error":{"message":"line one\\nline two"}}', "line one line two"),
            (json.dumps({"error": {"message": "bounded detail " * 40}}).encode(), "TRUNCATED"),
            (b'{"error":{"message":"Authorization: Bearer abcdefghijklmnop"}}', "[REDACTED_SENSITIVE_PROVIDER_DETAIL]"),
            (
                json.dumps(
                    {"error": {"message": f"token {synthetic_secret_pattern}"}}
                ).encode(),
                "token [REDACTED]",
            ),
            (b'{"error":{"status":"UNAVAILABLE"}}', None),
            (b'not-json-provider-body', None),
        )
        for body, expected in cases:
            with self.subTest(body=body[:30]):
                failure = urllib.error.HTTPError(
                    "http://127.0.0.1:8765/chat/completions",
                    503,
                    "Service Unavailable",
                    {},
                    io.BytesIO(body),
                )
                with patch("standalone.providers.urllib.request.urlopen", side_effect=failure):
                    with self.assertRaises(ProviderRequestError) as raised:
                        ChatCompletionClient(_config()).complete(
                            [{"role": "user", "content": "synthetic"}], stage="coverage"
                        )
                receipt = raised.exception.request_receipts[0]
                detail = receipt["provider_error_detail"]
                if expected == "TRUNCATED":
                    self.assertEqual(240, len(detail))
                    self.assertTrue(detail.endswith("..."))
                else:
                    self.assertEqual(expected, detail)
                serialized = json.dumps(receipt, ensure_ascii=False)
                self.assertNotIn("abcdefghijklmnop", serialized)
                self.assertNotIn("not-json-provider-body", serialized)

    def test_10_provider_detail_reaches_stage_machine_and_human_error_only_safely(self) -> None:
        public = _technical_result().as_dict()
        runtime = public["runtime"]
        physical = runtime["physical_request_receipts"][0]
        stage = runtime["provider_receipts"][0]
        machine = runtime["machine_receipt"]
        for surface in (physical, stage, machine):
            self.assertEqual("UNAVAILABLE", surface["provider_error_status"])
            self.assertEqual("backend_unavailable", surface["provider_error_code"])
            self.assertEqual("capacity temporarily unavailable", surface["provider_error_detail"])
        self.assertIn("capacity temporarily unavailable", machine["error_message"])
        serialized = json.dumps(public, ensure_ascii=False).casefold()
        for prohibited in ("authorization", "request_body", "chain_of_thought", "raw response"):
            self.assertNotIn(prohibited, serialized)
        self.assertEqual("UNKNOWN", runtime["usage_status"])
        self.assertEqual(1, runtime["unknown_potential_charge_attempt_count"])

    def test_11_overload_matrix_remains_one_physical_attempt(self) -> None:
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                failure = urllib.error.HTTPError(
                    "http://127.0.0.1:8765/chat/completions",
                    status,
                    "bounded mock",
                    {"Retry-After": "9"},
                    io.BytesIO(b'{"error":{"message":"bounded overload"}}'),
                )
                with patch("standalone.providers.urllib.request.urlopen", side_effect=failure) as mocked:
                    with self.assertRaises(ProviderRequestError) as raised:
                        ChatCompletionClient(_config()).complete(
                            [{"role": "user", "content": "synthetic"}], stage="coverage"
                        )
                self.assertEqual(1, mocked.call_count)
                receipt = raised.exception.request_receipts[0]
                self.assertEqual("STOP_NO_AUTOMATIC_RETRY", receipt["retry_decision"])
                self.assertEqual("UNKNOWN", receipt["usage_status"])

    def test_12_heading_positive_matrix(self) -> None:
        cases = {
            "pure_atx": _complete_markdown(),
            "s_series": _s1_s6_markdown(),
            "numeric": _numbered_markdown(
                [(2, "1 Introduction"), (2, "2 Methods"), (2, "3 Conclusion")]
            ),
            "chinese_chapters": _numbered_markdown(
                [
                    (2, "第一章 引言"),
                    (2, "第二章 理论"),
                    (2, "第三章 方法"),
                    (2, "第四章 发现"),
                    (2, "第五章 讨论"),
                    (2, "第六章 结论"),
                ]
            ),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.assertTrue(analyze_intake_structure(text).complete_structure)
        self.assertNotIn(
            "HEADING_NUMBERING_STYLE_REVIEW",
            analyze_intake_structure(_s1_s6_markdown()).advisory_codes,
        )

    def test_13_heading_advisory_truth_table_is_nonblocking(self) -> None:
        cases = {
            "unnumbered": _complete_markdown(),
            "mixed": _numbered_markdown(
                [(2, "S1 Introduction"), (2, "2 Methods"), (2, "S3 Conclusion")]
            ),
            "gap": _numbered_markdown([(2, "S1 Introduction"), (2, "S3 Conclusion")]),
            "major_level_jump": _numbered_markdown(
                [(2, "S1 Introduction"), (3, "S2 Methods"), (2, "S3 Conclusion")]
            ),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                receipt = analyze_intake_structure(text)
                self.assertTrue(receipt.complete_structure)
                self.assertEqual(("HEADING_NUMBERING_STYLE_REVIEW",), receipt.advisory_codes)
                advisory = receipt.advisories[0]
                self.assertFalse(advisory["blocking"])
                self.assertEqual("LOW", advisory["severity"])

    def test_14_structural_gaps_are_nonblocking_format_advisories(self) -> None:
        complete = _complete_markdown()
        cases = {
            "missing_conclusion": complete.replace("## Conclusion\n\nSynthetic conclusion.\n\n", ""),
            "missing_references": complete.replace("## References\n\nSynthetic reference.\n", ""),
            "reversed": complete.replace(
                "## Conclusion\n\nSynthetic conclusion.\n\n## References\n\nSynthetic reference.",
                "## References\n\nSynthetic reference.\n\n## Conclusion\n\nSynthetic conclusion.",
            ),
            "not_complete": "# Title\n\n## Abstract\nShort.\n## Conclusion\nShort.\n## References\nRef.",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                receipt = analyze_intake_structure(text)
                self.assertTrue(receipt.complete_structure)
                self.assertTrue(receipt.format_advisory_only)
                self.assertTrue(all(not item["blocking"] for item in receipt.advisories))

    def test_15_baoshan_fixture_is_read_only_parser_acceptance(self) -> None:
        fixture_drive = "".join(("G", ":"))
        path = (
            Path(fixture_drive + chr(92))
            / "Agents Projects"
            / "8 云南咖啡转型"
            / "writing_startup_2026-08-09"
            / "outputs"
            / "qa"
            / "71_Q_b_corrected_package_v8.md"
        )
        before = path.read_bytes()
        before_hash = hashlib.sha256(before).hexdigest()
        with patch("standalone.providers.urllib.request.urlopen") as network:
            receipt = analyze_intake_structure(before.decode("utf-8"))
        after = path.read_bytes()
        self.assertEqual(88539, len(before))
        self.assertEqual(
            "b0ebd8e1f17fd8a20c9a8f0f7179579e8cfd63f4aeead8831339187ac21e2732",
            before_hash,
        )
        self.assertEqual(before_hash, hashlib.sha256(after).hexdigest())
        self.assertTrue(receipt.complete_structure)
        self.assertEqual(23, receipt.heading_count)
        self.assertNotIn("HEADING_NUMBERING_STYLE_REVIEW", receipt.advisory_codes)
        network.assert_not_called()

    def test_16_technical_receipt_allowed_fields_and_cross_field_truth_table(self) -> None:
        base = {
            "manuscript_complete": True,
            "current_identity_clear": True,
            "whole_manuscript_read": False,
            "critical_basis_available": True,
            "bounded_scope": False,
            "current_manuscript_identity": "synthetic-technical-matrix",
            "technical_execution_hold": True,
            "evidence_hold_codes": [],
            "submission_hold_codes": [],
        }
        for stage in sorted(TECHNICAL_FAILED_STAGES):
            with self.subTest(stage=stage):
                state = dict(base, technical_failed_stage=stage)
                decision = decide_state(state)
                receipt = minimal_receipt(
                    decision,
                    state["current_manuscript_identity"],
                    failed_stage=stage,
                )
                self.assertEqual("TECHNICAL_EXECUTION_HOLD", receipt["reason_category"])
                self.assertEqual(stage, receipt["failed_stage"])
                self.assertEqual([], receipt["evidence_hold_codes"])
                self.assertEqual([], receipt["submission_hold_codes"])
        technical_decision = decide_state(dict(base, technical_failed_stage="coverage_provider"))
        with self.assertRaises(ClosureStateError):
            minimal_receipt(technical_decision, "synthetic-technical-matrix")
        ordinary_state = dict(base)
        ordinary_state.pop("technical_execution_hold")
        ordinary_state["whole_manuscript_read"] = False
        ordinary_decision = decide_state(ordinary_state)
        with self.assertRaises(ClosureStateError):
            minimal_receipt(
                ordinary_decision,
                "synthetic-technical-matrix",
                failed_stage="coverage_provider",
            )

    def test_17_versioned_build_receipt_contract_is_declared(self) -> None:
        build = (ROOT / "build_exe.ps1").read_text(encoding="utf-8")
        self.assertIn("standalone_version = '0.6.4'", build)
        self.assertIn("skill_version = '0.2.1'", build)
        for field in (
            "intake_contract_version",
            "intake_contract_sha256",
            "title_evidence_contract_version",
            "title_evidence_contract_sha256",
            "manuscript_basis_contract_version",
            "manuscript_basis_contract_sha256",
            "provider_transmission_consent_contract_version",
            "provider_transmission_consent_contract_sha256",
            "schema_delivery_contract_sha256",
            "dynamic_adjudication_schema_contract_sha256",
            "candidate_binding_contract_version",
            "candidate_binding_contract_sha256",
            "affirmative_stop_contract_version",
            "affirmative_stop_contract_sha256",
            "machine_state_contract_version",
            "machine_receipt_contract_version",
            "schema_definition_lint_contract_version",
            "schema_definition_lint_contract_sha256",
            "technical_hold_receipt_contract_version",
            "technical_hold_receipt_contract_sha256",
            "provider_error_detail_contract_version",
            "provider_error_detail_contract_sha256",
        ):
            self.assertIn(field, build)

    def test_18_missing_title_before_english_abstract_is_nonblocking_advisory(self) -> None:
        text = _missing_title_markdown("## Abstract")
        receipt = analyze_intake_structure(text)
        self.assertFalse(receipt.title_present)
        self.assertTrue(receipt.complete_structure)
        self.assertIn("STRUCTURE_FORMAT_REVIEW", receipt.advisory_codes)

    def test_19_missing_title_before_chinese_abstract_is_nonblocking(self) -> None:
        receipt = analyze_intake_structure(_missing_title_markdown("## 摘要"))
        self.assertFalse(receipt.title_present)
        self.assertTrue(receipt.complete_structure)

    def test_20_semantic_section_heading_is_never_reused_as_title_fallback(self) -> None:
        semantic_headings = (
            "## Abstract",
            "## 摘要",
            "## Keywords",
            "## 关键词",
            "## Introduction",
            "## 引言",
            "## Methods",
            "## 方法",
            "## Results",
            "## 结果",
            "## Discussion",
            "## 讨论",
            "## Conclusion",
            "## 结论",
            "## References",
            "## 参考文献",
        )
        for heading in semantic_headings:
            with self.subTest(heading=heading):
                receipt = analyze_intake_structure(_missing_title_markdown(heading))
                self.assertFalse(receipt.title_present)
                self.assertTrue(receipt.complete_structure)

    def test_21_legal_h1_and_plain_titles_remain_complete(self) -> None:
        h1 = analyze_intake_structure(_complete_markdown())
        plain = analyze_intake_structure(_complete_markdown().replace("# Synthetic Complete Manuscript", "Synthetic Complete Manuscript", 1))
        self.assertTrue(h1.title_present)
        self.assertTrue(h1.complete_structure)
        self.assertTrue(plain.title_present)
        self.assertTrue(plain.complete_structure)

    def test_22_front_matter_without_title_before_english_abstract_is_nonblocking(self) -> None:
        text = "\ufeff" + _front_matter_markdown(
            "author: Synthetic Author\ndate: 2026-08-28"
        ).lstrip("\n")
        receipt = analyze_intake_structure(text)
        self.assertTrue(receipt.complete_structure)
        self.assertFalse(receipt.title_present)

    def test_23_front_matter_without_title_before_chinese_abstract_is_nonblocking(self) -> None:
        text = _front_matter_markdown("作者: 合成作者\n日期: 2026-08-28").replace(
            "## Abstract", "## 摘要", 1
        )
        receipt = analyze_intake_structure(text)
        self.assertTrue(receipt.complete_structure)
        self.assertFalse(receipt.title_present)

    def test_24_empty_or_null_top_level_yaml_title_is_not_title_evidence(self) -> None:
        invalid_titles = (
            "title:",
            'title: ""',
            "title: '   '",
            "title: null",
            "title: ~",
            "title: |",
            "title: >-",
        )
        for title_row in invalid_titles:
            with self.subTest(title_row=title_row):
                receipt = analyze_intake_structure(
                    _front_matter_markdown(f"{title_row}\nauthor: Synthetic Author")
                )
                self.assertFalse(receipt.title_present)
                self.assertTrue(receipt.complete_structure)

    def test_25_nested_yaml_title_is_not_top_level_title_evidence(self) -> None:
        invalid_front_matter = (
            "author:\n  name: Synthetic Author\n  title: Professor\ndate: 2026-08-28",
            "author title: Professor\ndate: 2026-08-28",
            "title: First\ntitle: Second\ndate: 2026-08-28",
        )
        for index, front_matter in enumerate(invalid_front_matter):
            with self.subTest(front_matter_index=index):
                text = _front_matter_markdown(front_matter)
                receipt = analyze_intake_structure(text)
                self.assertTrue(receipt.complete_structure)
                self.assertFalse(receipt.title_present)

    def test_26_nonempty_top_level_yaml_title_is_independent_title_evidence(self) -> None:
        valid_front_matter = (
            "title: Synthetic Front Matter Title\nauthor: Synthetic Author",
            "title: |\n  Synthetic Block Title\nauthor: Synthetic Author",
            "title: >-\n  Synthetic Folded Title\nauthor: Synthetic Author",
        )
        for front_matter in valid_front_matter:
            with self.subTest(front_matter=front_matter.splitlines()[0]):
                receipt = analyze_intake_structure(_front_matter_markdown(front_matter))
                self.assertTrue(receipt.title_present)
                self.assertTrue(receipt.complete_structure)

    def test_27_front_matter_without_title_allows_body_h1_title(self) -> None:
        text = _front_matter_markdown(
            "author: Synthetic Author", body_title="# Synthetic Body Title"
        ).replace("\n---\n\n# Synthetic Body Title", "\n...\n\n# Synthetic Body Title", 1)
        receipt = analyze_intake_structure(text)
        self.assertTrue(receipt.title_present)
        self.assertTrue(receipt.complete_structure)

    def test_28_front_matter_without_title_allows_plain_body_title(self) -> None:
        receipt = analyze_intake_structure(
            _front_matter_markdown("author: Synthetic Author", body_title="Synthetic Plain Body Title")
        )
        self.assertTrue(receipt.title_present)
        self.assertTrue(receipt.complete_structure)

    def test_29_unclosed_front_matter_is_unknown_but_nonblocking(self) -> None:
        text = _front_matter_markdown("author: Synthetic Author\ndate: 2026-08-28")
        text = text.replace("\n---\n\n## Abstract", "\n\n## Abstract", 1)
        receipt = analyze_intake_structure(text)
        self.assertTrue(receipt.complete_structure)
        self.assertIn("STRUCTURE_FORMAT_REVIEW", receipt.advisory_codes)

    def test_30_failure_first_equivalent_format_variants_change_coverage_routing(self) -> None:
        formatted = _complete_markdown()
        titleless = formatted.replace("# Synthetic Complete Manuscript\n\n", "", 1)
        variants = (("formatted", formatted, True), ("titleless", titleless, True))
        for name, text, expected_provider_call in variants:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"GEMINI_API_KEY": "mock"}, clear=True
            ), patch(
                "standalone.assessor.ChatCompletionClient.complete",
                side_effect=ProviderRequestError("synthetic failure-first stop"),
            ) as provider_call:
                path = Path(directory) / f"{name}.md"
                path.write_text(text, encoding="utf-8")
                analyze_manuscript(
                    RunOptions(
                        manuscript_path=path,
                        provider="gemini",
                        model="gemini-3.7-flash",
                        confirm_complete_current_manuscript=True,
                        manuscript_identity=f"synthetic-routing-{name}",
                        provider_transmission_consent=True,
                    )
                )
            self.assertEqual(expected_provider_call, provider_call.called)

    def test_31_equivalent_format_variants_must_have_equal_local_routing_eligibility(self) -> None:
        formatted = analyze_intake_structure(_complete_markdown())
        titleless = analyze_intake_structure(
            _complete_markdown().replace("# Synthetic Complete Manuscript\n\n", "", 1)
        )
        self.assertEqual(formatted.complete_structure, titleless.complete_structure)

    def test_32_metamorphic_format_matrix_routes_exactly_one_coverage(self) -> None:
        substance = "Bounded synthetic argument, method, evidence, and analysis.\n" * 80
        variants = {
            "h1_atx": f"# Title\n\n## Abstract\n{substance}\n## Conclusion\nText\n## References\nRef",
            "no_h1": f"Abstract\n{substance}\nConclusion\nText\nReferences\nRef",
            "setext": f"Title\n=====\nAbstract\n--------\n{substance}\nConclusion\nReferences",
            "plain": f"A plain opening line\n{substance}\nClosing discussion\nSources",
            "roman": f"## I. Introduction\n{substance}\n## VI. Closing\nWorks cited",
            "arabic": f"1 Introduction\n{substance}\n6 Conclusion\nReferences",
            "letter": f"A. Introduction\n{substance}\nF. Conclusion\nReferences",
            "s_series": f"## S1 Introduction\n{substance}\n## S6 Conclusion\nReferences",
            "chinese": f"第一章 引言\n{substance}\n第六章 结论\n参考文献",
            "yaml": f"---\nauthor: Synthetic\ndate: 2026-08-28\n---\n{substance}",
            "toml": f"+++\nauthor = 'Synthetic'\ndate = '2026-08-28'\n+++\n{substance}",
            "mixed": f"# Title\nA Introduction\n{substance}\n## 6 Conclusion\n参考文献",
            "title_missing": substance,
            "synonyms": f"Overview\n{substance}\nClosing observations\nSources cited",
            "bom_crlf": ("\ufeffTitle\r\nAbstract\r\n" + substance + "\r\nConclusion\r\nReferences"),
        }

        def mocked_complete(client: ChatCompletionClient, _messages: object, **kwargs: object) -> CompletionResult:
            if client.on_attempt is not None:
                client.on_attempt(1)
            self.assertEqual("coverage", kwargs.get("stage"))
            return CompletionResult(
                content=json.dumps(_coverage_state(basis="INSUFFICIENT")),
                model="gemini-3.7-flash",
                usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            )

        for label, text in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"GEMINI_API_KEY": "mock"}, clear=True
            ), patch("standalone.assessor.ChatCompletionClient.complete", new=mocked_complete):
                path = Path(directory) / f"{label}.md"
                path.write_text(text, encoding="utf-8")
                result = analyze_manuscript(
                    RunOptions(
                        manuscript_path=path,
                        provider="gemini",
                        model="gemini-3.7-flash",
                        output_language="en",
                        provider_transmission_consent=True,
                    )
                ).as_dict()
            self.assertTrue(result["runtime"]["harness"]["intake"]["local_preflight_passed"])
            self.assertEqual(1, result["runtime"]["physical_request_attempt_count"])
            self.assertEqual("INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS", result["minimal_receipt"]["reason_category"])

    def test_33_consent_binding_change_cancel_default_and_one_use_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"GEMINI_API_KEY": "mock"}, clear=True
        ):
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            document = read_document(path)
            cases = (
                ("cancel", False, document.artifact_sha256, "gemini", "gemini-3.7-flash"),
                ("hash", True, "0" * 64, "gemini", "gemini-3.7-flash"),
                ("provider", True, document.artifact_sha256, "kimi", "gemini-3.7-flash"),
                ("model", True, document.artifact_sha256, "gemini", "gemini-3.6-flash"),
            )
            for label, confirmed, artifact_hash, provider, model in cases:
                with self.subTest(label=label), patch(
                    "standalone.assessor.ChatCompletionClient.complete"
                ) as provider_call:
                    consent = prepare_provider_transmission_consent(
                        artifact_sha256=artifact_hash,
                        provider=provider,
                        model=model,
                        confirmed=confirmed,
                    )
                    result = analyze_manuscript(
                        RunOptions(
                            manuscript_path=path,
                            provider="gemini",
                            model="gemini-3.7-flash",
                            output_language="en",
                            provider_transmission_consent=consent,
                        )
                    ).as_dict()
                provider_call.assert_not_called()
                self.assertEqual("CANCELED", result["runtime"]["terminal_status"])
                self.assertEqual("NOT_AUTHORIZED", result["runtime"]["provider_transmission_consent"]["status"])

            calls: list[str] = []

            def insufficient(client: ChatCompletionClient, _messages: object, **kwargs: object) -> CompletionResult:
                calls.append(str(kwargs.get("stage")))
                if client.on_attempt is not None:
                    client.on_attempt(1)
                return CompletionResult(
                    content=json.dumps(_coverage_state(basis="INSUFFICIENT")),
                    model="gemini-3.7-flash",
                    usage={},
                )

            one_use = prepare_provider_transmission_consent(
                artifact_sha256=document.artifact_sha256,
                provider="gemini",
                model="gemini-3.7-flash",
                confirmed=True,
            )
            options = RunOptions(
                manuscript_path=path,
                provider="gemini",
                model="gemini-3.7-flash",
                output_language="en",
                provider_transmission_consent=one_use,
            )
            with patch("standalone.assessor.ChatCompletionClient.complete", new=insufficient):
                first = analyze_manuscript(options).as_dict()
                second = analyze_manuscript(options).as_dict()
            self.assertEqual(["coverage"], calls)
            self.assertEqual("CONFIRMED", first["runtime"]["provider_transmission_consent"]["status"])
            self.assertEqual("NOT_AUTHORIZED", second["runtime"]["provider_transmission_consent"]["status"])
            self.assertFalse(second["runtime"]["api_called"])

    def test_34_cli_consent_flag_defaults_to_refusal_per_invocation(self) -> None:
        parser = build_parser()
        default = parser.parse_args(["synthetic.md"])
        confirmed = parser.parse_args(["synthetic.md", "--consent-to-provider-transmission"])
        self.assertFalse(default.consent_to_provider_transmission)
        self.assertTrue(confirmed.consent_to_provider_transmission)

    def test_35_semantic_basis_sufficient_continues_to_adjudication(self) -> None:
        coverage = _coverage_state(basis="SUFFICIENT")
        queue = iter(
            (
                CompletionResult(
                    content=json.dumps(coverage),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                ),
                CompletionResult(
                    content=json.dumps(_adjudication_state(coverage)),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 15, "completion_tokens": 4, "total_tokens": 19},
                ),
            )
        )
        stages: list[str] = []

        def mocked_complete(client: ChatCompletionClient, _messages: object, **kwargs: object) -> CompletionResult:
            stages.append(str(kwargs.get("stage")))
            if client.on_attempt is not None:
                client.on_attempt(1)
            return next(queue)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "mock"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete", new=mocked_complete):
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="kimi",
                    model="kimi-k2.6",
                    output_language="en",
                    provider_transmission_consent=True,
                )
            ).as_dict()
        self.assertEqual(["coverage", "adjudication"], stages)
        self.assertEqual("STOP_REVISING", result["closure_card"]["Verdict"])
        self.assertEqual("SUFFICIENT", result["minimal_receipt"]["whole_manuscript_basis"])
        self.assertEqual("SUFFICIENT", result["runtime"]["machine_receipt"]["whole_manuscript_basis"])
        self.assertEqual(2, result["runtime"]["physical_request_attempt_count"])

    def test_36_coverage_schema_failure_remains_technical_not_basis_hold(self) -> None:
        def malformed(client: ChatCompletionClient, _messages: object, **_kwargs: object) -> CompletionResult:
            if client.on_attempt is not None:
                client.on_attempt(1)
            return CompletionResult(
                content="{}",
                model="deepseek-v4-pro",
                usage={"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
            )

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "mock"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete", new=malformed):
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    output_language="en",
                    provider_transmission_consent=True,
                )
            ).as_dict()
        self.assertEqual("TECHNICAL_EXECUTION_HOLD", result["minimal_receipt"]["reason_category"])
        self.assertEqual("coverage_contract", result["minimal_receipt"]["failed_stage"])
        self.assertNotIn("whole_manuscript_basis", result["minimal_receipt"])
        self.assertEqual(1, result["runtime"]["physical_request_attempt_count"])

    def test_37_zero_effective_text_is_local_technical_hold_without_consent_or_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "standalone.assessor.ChatCompletionClient.complete"
        ) as provider_call:
            path = Path(directory) / "empty.md"
            path.write_text("\ufeff\n\n", encoding="utf-8")
            result = analyze_manuscript(RunOptions(manuscript_path=path, output_language="en")).as_dict()
        provider_call.assert_not_called()
        self.assertFalse(result["runtime"]["api_called"])
        self.assertEqual("TECHNICAL_EXECUTION_HOLD", result["minimal_receipt"]["reason_category"])
        self.assertEqual("local_preflight", result["minimal_receipt"]["failed_stage"])

    def test_38_user_cancellation_precedes_key_loading(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ), patch("standalone.assessor.load_provider_config") as config_loader, patch(
            "standalone.assessor.ChatCompletionClient.complete"
        ) as provider_call:
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="gemini",
                    model="gemini-3.7-flash",
                    output_language="en",
                    provider_transmission_consent=False,
                )
            ).as_dict()
        config_loader.assert_not_called()
        provider_call.assert_not_called()
        self.assertEqual("CANCELED", result["runtime"]["terminal_status"])
        self.assertEqual(
            "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED",
            result["minimal_receipt"]["reason_category"],
        )
        self.assertFalse(result["runtime"]["api_called"])

    def test_39_confirmed_run_with_missing_key_is_structured_configuration_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete") as provider_call:
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="gemini",
                    model="gemini-3.7-flash",
                    output_language="en",
                    provider_transmission_consent=True,
                )
            ).as_dict()
        provider_call.assert_not_called()
        self.assertEqual("TECHNICAL_EXECUTION_HOLD", result["minimal_receipt"]["reason_category"])
        self.assertEqual("provider_configuration", result["minimal_receipt"]["failed_stage"])
        self.assertEqual("CONFIRMED", result["runtime"]["provider_transmission_consent"]["status"])
        self.assertFalse(result["runtime"]["api_called"])

    def test_40_failure_first_zero_candidate_schema_has_no_empty_enum(self) -> None:
        coverage = _coverage_state(basis="SUFFICIENT")
        schema = build_adjudication_json_schema(coverage)
        causes = schema["properties"]["material_root_causes"]
        self.assertEqual(0, causes["minItems"])
        self.assertEqual(len(COVERAGE_DIMENSIONS), causes["maxItems"])
        self.assertEqual([], _moonshot_schema_definition_errors(schema))

    def test_41_failure_first_kimi_strict_mock_accepts_zero_candidate_schema(self) -> None:
        coverage = _coverage_state(basis="SUFFICIENT")
        schema = build_adjudication_json_schema(coverage)
        config = ProviderConfig(
            name="kimi",
            model="kimi-k2.6",
            base_url="http://127.0.0.1:8765",
            api_key="mock",
            key_variable="MRC_TEST_KEY",
        )
        with patch(
            "standalone.providers.urllib.request.urlopen",
            new=_kimi_schema_checking_urlopen,
        ):
            completion = ChatCompletionClient(config).complete(
                [{"role": "user", "content": "synthetic"}],
                json_mode=True,
                json_schema=schema,
                json_schema_name="mrc_root_cause_adjudication",
                stage="adjudication",
            )
        self.assertEqual("kimi-k2.6", completion.model)

    def test_42_zero_one_many_candidate_schema_truth_table(self) -> None:
        self.assertEqual("mrc-canonical-schema-delivery-3.0", SCHEMA_DELIVERY_CONTRACT_VERSION)
        self.assertEqual("mrc-dynamic-adjudication-schema-3.0", DYNAMIC_ADJUDICATION_SCHEMA_VERSION)
        self.assertEqual(
            "mrc-candidate-lower-bound-independent-additions-1.0",
            CANDIDATE_EXACT_SET_CONTRACT_VERSION,
        )
        self.assertEqual("mrc-schema-definition-lint-1.0", SCHEMA_DEFINITION_LINT_VERSION)
        cases = (
            ([], list(COVERAGE_DIMENSIONS)),
            (["contribution"], list(COVERAGE_DIMENSIONS)),
            (
                ["contribution", "methods_and_research_design"],
                list(COVERAGE_DIMENSIONS),
            ),
        )
        for candidates, expected_enum in cases:
            with self.subTest(candidates=candidates):
                coverage = _coverage_state(basis="SUFFICIENT")
                candidate_set = set(candidates)
                coverage["root_cause_candidate_dimensions"] = list(candidates)
                for row in coverage["dimensions"]:
                    row["status"] = (
                        "POTENTIAL_MATERIAL_ROOT_CAUSE"
                        if row["dimension"] in candidate_set
                        else "CLEAR"
                    )
                schema = build_adjudication_json_schema(coverage)
                causes = schema["properties"]["material_root_causes"]
                self.assertEqual(len(candidates), causes["minItems"])
                self.assertEqual(len(COVERAGE_DIMENSIONS), causes["maxItems"])
                self.assertEqual(expected_enum, causes["items"]["properties"]["dimension"]["enum"])
                self.assertEqual("PASS", validate_schema_definition(schema)["status"])
                self.assertEqual([], _moonshot_schema_definition_errors(schema))

    def test_43_zero_candidate_stop_and_grounded_addition_binding(self) -> None:
        coverage = _coverage_state(basis="SUFFICIENT")
        schema = build_adjudication_json_schema(coverage)
        valid = _adjudication_state(coverage)
        validate_json_schema_contract(
            valid,
            schema,
            contract_version=ADJUDICATION_CONTRACT_VERSION,
        )
        validate_candidate_exact_set(coverage, valid)
        self.assertEqual([], validate_adjudication_binding(valid, coverage)["material_root_causes"])

        addition = json.loads(json.dumps(valid))
        addition["material_root_causes"] = [
            {
                "observed": True,
                "locatable": True,
                "dimension": "contribution",
                "origin": "INDEPENDENT_ADDITION",
                "coverage_disagreement": True,
                "disposition_reason_code": "MATERIAL_CONCERN_CONFIRMED",
                "author_decision_required": False,
                "style_only": False,
                "hold_only": False,
                "verification_only": False,
                "expected_benefit_exceeds_risk": True,
                "scope": "central",
            }
        ]
        validate_json_schema_contract(
            addition,
            schema,
            contract_version=ADJUDICATION_CONTRACT_VERSION,
        )
        validate_candidate_exact_set(coverage, addition)
        ungrounded = json.loads(json.dumps(addition))
        ungrounded["material_root_causes"][0].update(
            {
                "observed": False,
                "disposition_reason_code": "NOT_OBSERVED",
                "expected_benefit_exceeds_risk": False,
            }
        )
        with self.assertRaises(Exception) as binding_error:
            validate_candidate_exact_set(coverage, ungrounded)
        self.assertIn("grounded canonical additions", str(binding_error.exception))

    def test_44_schema_definition_lint_matrix_stops_before_dispatch(self) -> None:
        base = {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string", "enum": ["ok"]}},
        }
        cases: dict[str, dict[str, object]] = {}
        empty_enum = json.loads(json.dumps(base))
        empty_enum["properties"]["value"]["enum"] = []
        cases["empty_enum"] = empty_enum
        duplicate_enum = json.loads(json.dumps(base))
        duplicate_enum["properties"]["value"]["enum"] = ["same", "same"]
        cases["duplicate_enum"] = duplicate_enum
        required_mismatch = json.loads(json.dumps(base))
        required_mismatch["required"] = ["missing"]
        cases["required_mismatch"] = required_mismatch
        duplicate_required = json.loads(json.dumps(base))
        duplicate_required["required"] = ["value", "value"]
        cases["duplicate_required"] = duplicate_required
        bad_bounds = {
            "type": "array",
            "minItems": 2,
            "maxItems": 1,
            "items": {"type": "string"},
        }
        cases["min_greater_than_max"] = bad_bounds

        config = ProviderConfig(
            name="kimi",
            model="kimi-k2.6",
            base_url="http://127.0.0.1:8765",
            api_key="mock",
            key_variable="MRC_TEST_KEY",
        )
        for label, schema in cases.items():
            attempts: list[int] = []
            with self.subTest(label=label), patch(
                "standalone.providers.urllib.request.urlopen"
            ) as network, self.assertRaises(SchemaDefinitionError) as raised:
                ChatCompletionClient(config, on_attempt=attempts.append).complete(
                    [{"role": "user", "content": "synthetic"}],
                    json_mode=True,
                    json_schema=schema,
                    json_schema_name="invalid_schema",
                    stage="adjudication",
                )
            network.assert_not_called()
            self.assertEqual([], attempts)
            receipt = raised.exception.contract_receipt
            self.assertEqual("SCHEMA_DEFINITION_INVALID", receipt["error_code"])
            self.assertEqual(SCHEMA_DEFINITION_LINT_VERSION, receipt["contract_version"])
            self.assertFalse(receipt["request_dispatched"])
            self.assertTrue(str(receipt["failed_path"]).startswith("$"))

    def test_45_invalid_coverage_schema_is_structured_local_hold_with_api_zero(self) -> None:
        invalid_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string", "enum": []}},
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"GEMINI_API_KEY": "mock"}, clear=True
        ), patch("standalone.assessor.COVERAGE_JSON_SCHEMA", invalid_schema), patch(
            "standalone.assessor.ChatCompletionClient.complete"
        ) as provider_call:
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="gemini",
                    model="gemini-3.7-flash",
                    output_language="en",
                    provider_transmission_consent=True,
                )
            ).as_dict()
        provider_call.assert_not_called()
        self.assertFalse(result["runtime"]["api_called"])
        self.assertEqual(0, result["runtime"]["physical_request_attempt_count"])
        self.assertEqual("TECHNICAL_EXECUTION_HOLD", result["minimal_receipt"]["reason_category"])
        self.assertEqual("coverage_schema_definition", result["minimal_receipt"]["failed_stage"])
        machine = result["runtime"]["machine_receipt"]
        self.assertEqual("SCHEMA_DEFINITION_INVALID", machine["error_code"])
        self.assertEqual("SCHEMA_DEFINITION_INVALID", machine["bounded_contract_failure"]["error_code"])

    def test_46_invalid_adjudication_schema_stops_before_second_dispatch(self) -> None:
        coverage = _coverage_state(basis="SUFFICIENT")
        invalid_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["material_root_causes"],
            "properties": {
                "material_root_causes": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 0,
                    "items": {"type": "string", "enum": []},
                }
            },
        }

        def coverage_only(
            client: ChatCompletionClient,
            _messages: object,
            **kwargs: object,
        ) -> CompletionResult:
            self.assertEqual("coverage", kwargs.get("stage"))
            if client.on_attempt is not None:
                client.on_attempt(1)
            return CompletionResult(
                content=json.dumps(coverage),
                model="kimi-k2.6",
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            )

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "mock"}, clear=True
        ), patch(
            "standalone.assessor.build_adjudication_json_schema",
            return_value=invalid_schema,
        ), patch(
            "standalone.assessor.ChatCompletionClient.complete",
            new=coverage_only,
        ):
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="kimi",
                    model="kimi-k2.6",
                    output_language="en",
                    provider_transmission_consent=True,
                )
            ).as_dict()
        self.assertEqual(1, result["runtime"]["physical_request_attempt_count"])
        self.assertEqual(["coverage"], result["runtime"]["usage_call_stages"])
        self.assertEqual("adjudication_schema_definition", result["minimal_receipt"]["failed_stage"])
        self.assertEqual(
            "SCHEMA_DEFINITION_INVALID",
            result["runtime"]["machine_receipt"]["error_code"],
        )

    def test_47_adjudication_schema_builder_error_is_structured_before_second_dispatch(self) -> None:
        coverage = _coverage_state(basis="SUFFICIENT")
        invalid_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["material_root_causes"],
            "properties": {
                "material_root_causes": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 0,
                    "items": {"type": "string", "enum": []},
                }
            },
        }

        def coverage_only(
            client: ChatCompletionClient,
            _messages: object,
            **kwargs: object,
        ) -> CompletionResult:
            self.assertEqual("coverage", kwargs.get("stage"))
            if client.on_attempt is not None:
                client.on_attempt(1)
            return CompletionResult(
                content=json.dumps(coverage),
                model="kimi-k2.6",
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            )

        def failing_builder(_coverage: object) -> dict[str, object]:
            validate_schema_definition(invalid_schema)
            self.fail("invalid schema unexpectedly passed definition lint")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "mock"}, clear=True
        ), patch(
            "standalone.assessor.build_adjudication_json_schema",
            new=failing_builder,
        ), patch(
            "standalone.assessor.ChatCompletionClient.complete",
            new=coverage_only,
        ):
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="kimi",
                    model="kimi-k2.6",
                    output_language="en",
                    provider_transmission_consent=True,
                )
            ).as_dict()
        self.assertEqual(1, result["runtime"]["physical_request_attempt_count"])
        self.assertEqual(["coverage"], result["runtime"]["usage_call_stages"])
        self.assertEqual("TECHNICAL_EXECUTION_HOLD", result["minimal_receipt"]["reason_category"])
        self.assertEqual("adjudication_schema_definition", result["minimal_receipt"]["failed_stage"])
        machine = result["runtime"]["machine_receipt"]
        self.assertEqual("SCHEMA_DEFINITION_INVALID", machine["error_code"])
        self.assertFalse(machine["bounded_contract_failure"]["request_dispatched"])
        self.assertEqual(1, result["runtime"]["usage_receipt_count"])

    def test_48_kimi_zero_candidate_end_to_end_strict_mock_forms_stop(self) -> None:
        scenario = _KimiZeroCandidateScenario()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MOONSHOT_API_KEY": "mock",
                "KIMI_BASE_URL": "http://127.0.0.1:8765",
            },
            clear=True,
        ), patch(
            "standalone.providers.urllib.request.urlopen",
            new=scenario,
        ):
            path = Path(directory) / "paper.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="kimi",
                    model="kimi-k2.6",
                    output_language="en",
                    provider_transmission_consent=True,
                )
            ).as_dict()
        self.assertEqual(2, len(scenario.requests))
        self.assertEqual("STOP_REVISING", result["closure_card"]["Verdict"])
        self.assertEqual("SUCCEEDED", result["runtime"]["machine_status"])
        self.assertEqual("PASS", result["runtime"]["presentation_status"])
        binding = result["runtime"]["machine_receipt"]["candidate_binding"]
        self.assertEqual([], binding["required_candidates"])
        self.assertEqual([], binding["observed_candidates"])
        self.assertEqual(2, result["runtime"]["physical_request_attempt_count"])
        self.assertEqual(2, result["runtime"]["usage_receipt_count"])
        adjudication_schema = scenario.requests[1]["response_format"]["json_schema"]["schema"]
        self.assertEqual([], _moonshot_schema_definition_errors(adjudication_schema))
        self.assertEqual(
            len(COVERAGE_DIMENSIONS),
            adjudication_schema["properties"]["material_root_causes"]["maxItems"],
        )

    def test_49_three_provider_schema_delivery_uses_valid_zero_candidate_schema(self) -> None:
        schema = build_adjudication_json_schema(_coverage_state(basis="SUFFICIENT"))
        cases = (
            ("deepseek", "deepseek-v4-pro"),
            ("kimi", "kimi-k2.6"),
            ("gemini", "gemini-3.7-flash"),
        )
        for provider, model in cases:
            captured: list[dict[str, object]] = []

            def transport(request: object, **kwargs: object) -> _KimiSchemaResponse:
                captured.append(json.loads(getattr(request, "data").decode("utf-8")))
                return _kimi_schema_checking_urlopen(request, **kwargs)

            config = ProviderConfig(
                name=provider,
                model=model,
                base_url="http://127.0.0.1:8765",
                api_key="mock",
                key_variable="MRC_TEST_KEY",
            )
            with self.subTest(provider=provider), patch(
                "standalone.providers.urllib.request.urlopen",
                new=transport,
            ):
                completion = ChatCompletionClient(config).complete(
                    [{"role": "user", "content": "synthetic"}],
                    json_mode=True,
                    json_schema=schema,
                    json_schema_name="mrc_root_cause_adjudication",
                    stage="adjudication",
                )
            response_format = captured[0]["response_format"]
            if provider == "deepseek":
                self.assertEqual({"type": "json_object"}, response_format)
            else:
                self.assertEqual("json_schema", response_format["type"])
                self.assertEqual([], _moonshot_schema_definition_errors(response_format["json_schema"]["schema"]))
            physical = completion.request_receipts[0]
            self.assertEqual(SCHEMA_DEFINITION_LINT_VERSION, physical["schema_definition_lint_contract_version"])
            self.assertEqual("PASS", physical["schema_definition_lint_status"])

    def test_50_failure_first_zero_candidates_allow_independent_canonical_causes(self) -> None:
        coverage = _coverage_state(basis="SUFFICIENT")
        schema = build_adjudication_json_schema(coverage)
        causes = schema["properties"]["material_root_causes"]
        self.assertEqual(0, causes["minItems"])
        self.assertEqual(len(COVERAGE_DIMENSIONS), causes["maxItems"])
        self.assertEqual(
            list(COVERAGE_DIMENSIONS),
            causes["items"]["properties"]["dimension"]["enum"],
        )

    def test_51_failure_first_coverage_miss_accepts_grounded_independent_addition(self) -> None:
        coverage = _coverage_state(basis="SUFFICIENT")
        state = _adjudication_state(coverage)
        state["material_root_causes"] = [
            {
                "observed": True,
                "locatable": True,
                "dimension": "methods_and_research_design",
                "origin": "INDEPENDENT_ADDITION",
                "coverage_disagreement": True,
                "disposition_reason_code": "MATERIAL_CONCERN_CONFIRMED",
                "author_decision_required": False,
                "style_only": False,
                "hold_only": False,
                "verification_only": False,
                "expected_benefit_exceeds_risk": True,
                "scope": "central",
            }
        ]
        validate_candidate_exact_set(coverage, state)

    def test_52_failure_first_stop_requires_explicit_two_stage_affirmative_sufficiency(self) -> None:
        coverage_row_required = set(
            COVERAGE_JSON_SCHEMA["properties"]["dimensions"]["items"]["required"]
        )
        adjudication_required = set(build_adjudication_json_schema(_coverage_state())["required"])
        self.assertIn("affirmative_sufficiency", coverage_row_required)
        self.assertIn("affirmative_sufficiency", adjudication_required)

    def test_53_bidirectional_semantic_verdict_truth_table(self) -> None:
        cases = (
            (
                "central_independent_addition",
                [_semantic_cause("contribution", scope="central")],
                "REOPEN_SUBSTANTIVE_REVISION",
            ),
            (
                "local_independent_addition",
                [_semantic_cause("methods_and_research_design", scope="local")],
                "ONE_BOUNDED_ROUND",
            ),
            ("two_stage_affirmative", [], "STOP_REVISING"),
        )
        for label, causes, expected in cases:
            with self.subTest(label=label):
                coverage = _coverage_state()
                decision, _state, gate = _semantic_decision(
                    coverage,
                    _semantic_adjudication(coverage, causes=causes),
                )
                self.assertEqual(expected, decision["verdict"])
                self.assertEqual(expected == "STOP_REVISING", gate["stop_eligible"])

    def test_54_coverage_candidate_lower_bound_and_independent_addition_both_bind(self) -> None:
        coverage = _coverage_state()
        coverage["root_cause_candidate_dimensions"] = ["contribution"]
        contribution = next(
            row for row in coverage["dimensions"] if row["dimension"] == "contribution"
        )
        contribution["status"] = "POTENTIAL_MATERIAL_ROOT_CAUSE"
        causes = [
            _semantic_cause("contribution", coverage_candidate=True, material=False),
            _semantic_cause("theory_and_concepts", scope="central"),
        ]
        envelope = _semantic_adjudication(coverage, causes=causes)
        decision, state, _gate = _semantic_decision(coverage, envelope)
        binding = validate_candidate_binding(coverage, state)
        self.assertEqual([], binding["missing_candidates"])
        self.assertEqual(["theory_and_concepts"], binding["independent_additions"])
        self.assertEqual("REOPEN_SUBSTANTIVE_REVISION", decision["verdict"])

    def test_55_binding_rejects_unknown_duplicate_unlocatable_and_speculative_additions(self) -> None:
        coverage = _coverage_state()
        valid = _semantic_cause("contribution")
        unknown = dict(valid, dimension="unknown_dimension")
        duplicate = {"material_root_causes": [valid, dict(valid)]}
        unlocatable = _semantic_cause(
            "methods_and_research_design",
            material=False,
            observed=True,
            locatable=False,
        )
        unlocatable["disposition_reason_code"] = "NOT_LOCATABLE"
        speculative = _semantic_cause(
            "theory_and_concepts",
            material=False,
            observed=False,
            locatable=False,
        )
        cases = {
            "unknown": {"material_root_causes": [unknown]},
            "duplicate": duplicate,
            "unlocatable": {"material_root_causes": [unlocatable]},
            "speculative": {"material_root_causes": [speculative]},
        }
        for label, state in cases.items():
            with self.subTest(label=label), self.assertRaises(Exception):
                validate_candidate_binding(coverage, state)

    def test_56_coverage_candidate_cannot_be_dropped_or_silently_all_false(self) -> None:
        coverage = _coverage_state()
        coverage["root_cause_candidate_dimensions"] = ["contribution"]
        next(row for row in coverage["dimensions"] if row["dimension"] == "contribution")[
            "status"
        ] = "POTENTIAL_MATERIAL_ROOT_CAUSE"
        with self.assertRaises(Exception):
            validate_candidate_binding(coverage, {"material_root_causes": []})
        unexplained = _semantic_cause("contribution", coverage_candidate=True, material=False)
        unexplained["disposition_reason_code"] = "MATERIAL_CONCERN_CONFIRMED"
        with self.assertRaises(Exception):
            validate_candidate_binding(coverage, {"material_root_causes": [unexplained]})
        explained = _semantic_cause("contribution", coverage_candidate=True, material=False)
        receipt = validate_candidate_binding(coverage, {"material_root_causes": [explained]})
        self.assertEqual([], receipt["missing_candidates"])
        self.assertEqual([], receipt["invalid_disposition"])

    def test_57_methods_reporting_is_contextual_not_keyword_deterministic(self) -> None:
        sparse = (
            "Synthetic manuscript: interviews are cited, but participant count, sampling basis, "
            "material processing, and analytic procedure are not supplied; the main inference "
            "cannot yet be evaluated from the visible design."
        )
        sufficient = (
            "Synthetic manuscript: participant count, purposive sampling basis, source processing, "
            "coding and analytic comparison are supplied in equivalent prose, so the bounded main "
            "inference is assessable without a conventional methods checklist."
        )
        coverage = _coverage_state()
        sparse_messages = build_adjudication_messages(
            sparse,
            manuscript_identity="synthetic-sparse-methods",
            output_language="en",
            coverage=coverage,
        )
        sufficient_messages = build_adjudication_messages(
            sufficient,
            manuscript_identity="synthetic-sufficient-methods",
            output_language="en",
            coverage=coverage,
        )
        self.assertIn(sparse, sparse_messages[1]["content"])
        self.assertIn(sufficient, sufficient_messages[1]["content"])
        self.assertEqual(sparse_messages[0]["content"], sufficient_messages[0]["content"])
        sparse_decision, _state, _gate = _semantic_decision(
            coverage,
            _semantic_adjudication(
                coverage,
                causes=[_semantic_cause("methods_and_research_design", scope="local")],
            ),
        )
        sufficient_decision, _state, _gate = _semantic_decision(
            coverage,
            _semantic_adjudication(coverage),
        )
        self.assertEqual("ONE_BOUNDED_ROUND", sparse_decision["verdict"])
        self.assertEqual("STOP_REVISING", sufficient_decision["verdict"])

    def test_58_defensive_writing_and_load_bearing_caution_are_bidirectionally_calibrated(self) -> None:
        coverage = _coverage_state()
        preserved_scope_decision, _state, _gate = _semantic_decision(
            coverage,
            _semantic_adjudication(coverage),
        )
        obscured_contribution_decision, _state, _gate = _semantic_decision(
            coverage,
            _semantic_adjudication(
                coverage,
                causes=[_semantic_cause("contribution", scope="central")],
            ),
        )
        coverage_prompt = build_coverage_messages(
            "Synthetic scope conditions and rival explanations remain visible.",
            manuscript_identity="synthetic-defensive-calibration",
        )[0]["content"]
        self.assertIn("Do not treat careful scope", coverage_prompt)
        self.assertIn("defensive caveats", coverage_prompt)
        self.assertEqual("STOP_REVISING", preserved_scope_decision["verdict"])
        self.assertEqual("REOPEN_SUBSTANTIVE_REVISION", obscured_contribution_decision["verdict"])

    def test_59_mutations_restore_exact_ceiling_or_remove_additions_and_are_detected(self) -> None:
        coverage = _coverage_state()
        messages = build_adjudication_messages(
            "Synthetic complete manuscript.",
            manuscript_identity="synthetic-mutation",
            output_language="en",
            coverage=coverage,
        )
        system = messages[0]["content"]
        schema = build_adjudication_json_schema(coverage)

        def independent_additions_enabled(text: str, candidate_schema: dict[str, object]) -> bool:
            causes = candidate_schema["properties"]["material_root_causes"]
            return bool(
                "required lower bound" in text
                and "you may add a dimension omitted by coverage" in text
                and causes["maxItems"] == len(COVERAGE_DIMENSIONS)
                and causes["items"]["properties"]["dimension"]["enum"]
                == list(COVERAGE_DIMENSIONS)
            )

        self.assertTrue(independent_additions_enabled(system, schema))
        removed_permission = system.replace("you may add a dimension omitted by coverage", "")
        self.assertFalse(independent_additions_enabled(removed_permission, schema))
        exact_ceiling = json.loads(json.dumps(schema))
        exact_ceiling["properties"]["material_root_causes"]["maxItems"] = 0
        self.assertFalse(independent_additions_enabled(system, exact_ceiling))
        decision, _state, gate = _semantic_decision(coverage, _semantic_adjudication(coverage))
        self.assertTrue(gate["stop_eligible"])
        self.assertEqual("STOP_REVISING", decision["verdict"])

    def test_60_author_decision_with_substantive_text_effect_cannot_be_hidden_as_hold(self) -> None:
        coverage = _coverage_state()
        material = _semantic_cause("whole_paper_argument", scope="local")
        material["author_decision_required"] = True
        decision, _state, _gate = _semantic_decision(
            coverage,
            _semantic_adjudication(coverage, causes=[material]),
        )
        self.assertEqual("ONE_BOUNDED_ROUND", decision["verdict"])
        disguised = dict(material)
        disguised.update(
            {
                "hold_only": True,
                "expected_benefit_exceeds_risk": False,
                "disposition_reason_code": "HOLD_ONLY",
            }
        )
        with self.assertRaises(Exception):
            validate_candidate_binding(coverage, {"material_root_causes": [disguised]})

    def test_61_provider_matrix_checks_real_prompt_schema_and_binding_for_zero_one_many(self) -> None:
        request_count = 0
        for provider, model in (
            ("deepseek", "deepseek-v4-pro"),
            ("kimi", "kimi-k2.6"),
            ("gemini", "gemini-3.7-flash"),
        ):
            for candidates in (
                [],
                ["contribution"],
                ["contribution", "methods_and_research_design"],
            ):
                coverage = _coverage_state()
                coverage["root_cause_candidate_dimensions"] = list(candidates)
                for row in coverage["dimensions"]:
                    row["status"] = (
                        "POTENTIAL_MATERIAL_ROOT_CAUSE"
                        if row["dimension"] in candidates
                        else "CLEAR"
                    )
                schema = build_adjudication_json_schema(coverage)
                messages = build_adjudication_messages(
                    "Synthetic provider-neutral complete manuscript.",
                    manuscript_identity="synthetic-provider-matrix",
                    output_language="en",
                    coverage=coverage,
                )
                captured: list[dict[str, object]] = []

                def transport(request: object, **_kwargs: object) -> _KimiSchemaResponse:
                    body = json.loads(getattr(request, "data").decode("utf-8"))
                    captured.append(body)
                    return _kimi_schema_checking_urlopen(request)

                config = ProviderConfig(
                    name=provider,
                    model=model,
                    base_url="http://127.0.0.1:8765",
                    api_key="mock",
                    key_variable="MRC_TEST_KEY",
                )
                with patch("standalone.providers.urllib.request.urlopen", new=transport):
                    ChatCompletionClient(config).complete(
                        messages,
                        json_mode=True,
                        json_schema=schema,
                        json_schema_name="mrc_root_cause_adjudication",
                        stage="adjudication",
                        coverage_digest_sha256=canonical_digest(coverage),
                    )
                request_count += 1
                self.assertIn("required lower bound", captured[0]["messages"][0]["content"])
                response_format = captured[0]["response_format"]
                if provider == "deepseek":
                    self.assertEqual({"type": "json_object"}, response_format)
                    self.assertIn(
                        json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        captured[0]["messages"][0]["content"],
                    )
                else:
                    self.assertEqual(schema, response_format["json_schema"]["schema"])
        self.assertEqual(9, request_count)

    def test_62_end_to_end_coverage_miss_independent_addition_reopens(self) -> None:
        coverage = _coverage_state()
        adjudication = _semantic_adjudication(
            coverage,
            causes=[_semantic_cause("contribution", scope="central")],
        )
        completions = iter(
            (
                CompletionResult(
                    content=json.dumps(coverage),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                ),
                CompletionResult(
                    content=json.dumps(adjudication),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 22, "completion_tokens": 8, "total_tokens": 30},
                ),
            )
        )

        def complete(client: ChatCompletionClient, _messages: object, **_kwargs: object) -> CompletionResult:
            if client.on_attempt is not None:
                client.on_attempt(1)
            return next(completions)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "mock"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete", new=complete):
            path = Path(directory) / "synthetic.md"
            path.write_text(_complete_markdown(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="kimi",
                    model="kimi-k2.6",
                    output_language="en",
                    provider_transmission_consent=True,
                )
            ).as_dict()
        self.assertEqual("REOPEN_SUBSTANTIVE_REVISION", result["closure_card"]["Verdict"])
        binding = result["runtime"]["machine_receipt"]["candidate_binding"]
        self.assertEqual(["contribution"], binding["independent_additions"])
        self.assertFalse(result["runtime"]["machine_receipt"]["affirmative_stop_gate"]["stop_eligible"])
        self.assertEqual(2, result["runtime"]["physical_request_attempt_count"])


if __name__ == "__main__":
    unittest.main()
