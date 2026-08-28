from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]

from scripts.closure_state import decide_state, minimal_receipt  # noqa: E402
from standalone.assessor import (  # noqa: E402
    ModelContractError,
    RunOptions,
    analyze_manuscript,
    parse_model_json,
    validate_model_state,
)
from standalone.document_reader import normalize_semantic_text, read_document  # noqa: E402
from standalone.events import EventSink  # noqa: E402
from standalone.harness import (  # noqa: E402
    COVERAGE_CONTRACT_VERSION,
    COVERAGE_DIMENSIONS,
    canonical_digest,
)
from standalone.localization import localize_closure_card  # noqa: E402
from standalone.providers import (  # noqa: E402
    ChatCompletionClient,
    CompletionResult,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderRequestError,
    list_provider_models,
    load_provider_config,
    reasoning_profile,
)
from standalone import providers as providers_module  # noqa: E402


VALID_MODEL_STATE = {
    "material_root_causes": [],
    "evidence_hold_codes": [],
    "submission_hold_codes": [],
    "protected": ["保持论点上限和可见的替代解释。"],
    "parked_opportunities": [],
    "lite_suggestions": [],
}

COVERAGE_STATE = {
    "coverage_contract_version": COVERAGE_CONTRACT_VERSION,
    "manuscript_identity_confirmed": True,
    "full_span_covered": True,
    "dimensions": [
        {"dimension": dimension, "applicability": "APPLICABLE", "assessed": True, "status": "CLEAR"}
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

ADJUDICATION_STATE = {
    "coverage_digest_sha256": canonical_digest(COVERAGE_STATE),
    **VALID_MODEL_STATE,
}


def long_manuscript(marker: str = "stable manuscript marker") -> str:
    return (
        "Title\n\nAbstract\n"
        + ((marker + " evidence-bound argument.\n") * 80)
        + "\nConclusion\nThe argument remains bounded.\n\nReferences\nReference A."
    )


class StandaloneRuntimeTests(unittest.TestCase):
    def test_provider_stage_timeouts_give_kimi_long_adjudication_window(self) -> None:
        stage_timeout = getattr(providers_module, "provider_stage_timeout_seconds")
        self.assertEqual(300.0, stage_timeout("kimi", "coverage"))
        self.assertEqual(900.0, stage_timeout("kimi", "adjudication"))
        self.assertEqual(900.0, stage_timeout("kimi", "interpretation"))
        self.assertEqual(180.0, stage_timeout("deepseek", "adjudication"))
        self.assertEqual(77.0, stage_timeout("kimi", "adjudication", override=77.0))

    def test_socket_timeout_is_not_automatically_retried(self) -> None:
        config = ProviderConfig(
            name="kimi",
            model="kimi-k2.6",
            base_url="http://127.0.0.1:8765",
            api_key="local",
            key_variable="TEST_KEY",
        )
        with patch(
            "standalone.providers.urllib.request.urlopen",
            side_effect=socket.timeout("read timed out"),
        ) as mocked, patch("standalone.providers.time.sleep"):
            with self.assertRaisesRegex(ProviderRequestError, "not automatically resent"):
                ChatCompletionClient(config, timeout_seconds=900, max_transient_retries=2).complete(
                    [{"role": "user", "content": "test"}]
                )
        self.assertEqual(1, mocked.call_count)

    def test_only_explicit_http_overload_statuses_are_retried(self) -> None:
        config = ProviderConfig(
            name="kimi",
            model="kimi-k2.6",
            base_url="http://127.0.0.1:8765",
            api_key="local",
            key_variable="TEST_KEY",
        )

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                        "usage": {},
                        "model": "kimi-k2.6",
                    }
                ).encode("utf-8")

        overload = urllib.error.HTTPError(
            "https://api.moonshot.cn/v1/chat/completions",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"error":{"message":"temporary overload"}}'),
        )
        with patch(
            "standalone.providers.urllib.request.urlopen",
            side_effect=[overload, FakeResponse()],
        ) as mocked, patch("standalone.providers.time.sleep"):
            ChatCompletionClient(config, max_transient_retries=2).complete(
                [{"role": "user", "content": "test"}]
            )
        self.assertEqual(2, mocked.call_count)

        unregistered = urllib.error.HTTPError(
            "https://api.moonshot.cn/v1/chat/completions",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b'{"error":{"message":"internal error"}}'),
        )
        with patch(
            "standalone.providers.urllib.request.urlopen",
            side_effect=unregistered,
        ) as mocked, patch("standalone.providers.time.sleep"):
            with self.assertRaisesRegex(ProviderRequestError, "status 500"):
                ChatCompletionClient(config, max_transient_retries=2).complete(
                    [{"role": "user", "content": "test"}]
                )
        self.assertEqual(1, mocked.call_count)

    def test_text_reader_hashes_immutable_bytes_and_semantic_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            data = long_manuscript().replace("\n", "\r\n").encode("utf-8")
            path.write_bytes(data)
            before = path.read_bytes()
            document = read_document(path)
            self.assertEqual(hashlib.sha256(data).hexdigest(), document.artifact_sha256)
            expected = normalize_semantic_text(data.decode("utf-8"))
            self.assertEqual(hashlib.sha256(expected.encode("utf-8")).hexdigest(), document.semantic_content_sha256)
            self.assertEqual(before, path.read_bytes())
            self.assertTrue(document.critical_basis_available)

    def test_docx_reader_includes_notes_and_detects_comments(self) -> None:
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        main = f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>{long_manuscript()}</w:t></w:r></w:p></w:body></w:document>'
        footnotes = f'<w:footnotes xmlns:w="{ns}"><w:footnote><w:p><w:r><w:t>Footnote evidence</w:t></w:r></w:p></w:footnote></w:footnotes>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", main)
                archive.writestr("word/footnotes.xml", footnotes)
                archive.writestr("word/comments.xml", f'<w:comments xmlns:w="{ns}"/>')
            document = read_document(path)
            self.assertIn("Footnote evidence", document.text)
            self.assertEqual(("COMMENTS_OR_TRACKING_REMAIN",), document.submission_hold_codes)

    def test_model_json_accepts_one_optional_json_fence_only(self) -> None:
        raw = json.dumps(VALID_MODEL_STATE)
        self.assertEqual(VALID_MODEL_STATE, parse_model_json(raw))
        self.assertEqual(VALID_MODEL_STATE, parse_model_json("```json\n" + raw + "\n```"))
        with self.assertRaises(ModelContractError):
            parse_model_json("prose\n" + raw)

    def test_model_state_is_exact_set_and_finite(self) -> None:
        self.assertEqual(VALID_MODEL_STATE, validate_model_state(VALID_MODEL_STATE))
        with self.assertRaises(ModelContractError):
            validate_model_state(VALID_MODEL_STATE | {"hidden_reasoning": "not allowed"})
        with self.assertRaises(ModelContractError):
            validate_model_state(VALID_MODEL_STATE | {"evidence_hold_codes": ["UNKNOWN"]})

    def test_provider_reads_only_environment_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderConfigurationError):
                load_provider_config("deepseek")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-secret"}, clear=True):
            config = load_provider_config("deepseek")
            self.assertEqual("test-secret", config.api_key)
            self.assertEqual("DEEPSEEK_API_KEY", config.key_variable)
            self.assertEqual("deepseek-v4-pro", config.model)
        with patch.dict(os.environ, {"KIMI_API_KEY": "kimi-secret"}, clear=True):
            config = load_provider_config("kimi")
            self.assertEqual("KIMI_API_KEY", config.key_variable)
            self.assertEqual("kimi-k2.6", config.model)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-value"}, clear=True):
            config = load_provider_config("gemini")
            self.assertEqual("GEMINI_API_KEY", config.key_variable)
            self.assertEqual("gemini-3.7-flash", config.model)

    def test_gemini_model_catalog_uses_provider_api_and_filters_non_text_models(self) -> None:
        payload = json.dumps(
            {
                "models": [
                    {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-embedding-2", "supportedGenerationMethods": ["embedContent"]},
                    {"name": "models/gemini-3.1-flash-image", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
                ]
            }
        ).encode("utf-8")

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return payload

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-value"}, clear=True), patch(
            "standalone.providers.urllib.request.urlopen", return_value=FakeResponse()
        ):
            models = list_provider_models("gemini")
        self.assertEqual(["gemini-3.7-flash", "gemini-3.6-flash"], models)

    def test_gemini_current_request_omits_deprecated_temperature(self) -> None:
        response_payload = json.dumps(
            {
                "choices": [{"message": {"content": "{}"}}],
                "model": "gemini-3.7-flash",
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }
        ).encode("utf-8")
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return response_payload

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            captured["body"] = json.loads(getattr(request, "data").decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-value"}, clear=True), patch(
            "standalone.providers.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            result = ChatCompletionClient(load_provider_config("gemini")).complete(
                [{"role": "user", "content": "test"}]
            )
        self.assertEqual("gemini-3.7-flash", result.model)
        self.assertNotIn("temperature", captured["body"])

    def test_reasoning_profiles_are_provider_and_model_specific(self) -> None:
        deepseek = reasoning_profile("deepseek", "deepseek-v4-pro")
        self.assertEqual(
            ["default", "disabled", "low", "high", "max"],
            [item["value"] for item in deepseek["options"]],
        )
        kimi_k3 = reasoning_profile("kimi", "kimi-k3")
        self.assertEqual(["default", "low", "high", "max"], [item["value"] for item in kimi_k3["options"]])
        kimi_k26 = reasoning_profile("kimi", "kimi-k2.6")
        self.assertEqual(["default", "enabled", "disabled"], [item["value"] for item in kimi_k26["options"]])
        kimi_code = reasoning_profile("kimi", "kimi-k2.7-code")
        self.assertEqual(["default"], [item["value"] for item in kimi_code["options"]])
        gemini = reasoning_profile("gemini", "gemini-3.7-flash")
        self.assertEqual(["default", "low", "medium", "high"], [item["value"] for item in gemini["options"]])

    def test_reasoning_options_serialize_to_each_official_request_shape(self) -> None:
        def capture(
            provider: str,
            model: str,
            option: str,
            schema: dict[str, object] | None = None,
        ) -> dict[str, object]:
            captured: dict[str, object] = {}
            response_payload = json.dumps(
                {
                    "choices": [{"message": {"content": "{}"}}],
                    "model": model,
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            ).encode("utf-8")

            class FakeResponse:
                def __enter__(self) -> "FakeResponse":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def read(self) -> bytes:
                    return response_payload

            def fake_urlopen(request: object, timeout: float) -> FakeResponse:
                self.assertGreater(timeout, 0)
                captured.update(json.loads(getattr(request, "data").decode("utf-8")))
                return FakeResponse()

            config = ProviderConfig(
                name=provider,
                model=model,
                base_url="http://127.0.0.1:8765",
                api_key="local",
                key_variable="TEST_KEY",
            )
            with patch("standalone.providers.urllib.request.urlopen", side_effect=fake_urlopen):
                ChatCompletionClient(config, max_transient_retries=0).complete(
                    [{"role": "user", "content": "test"}],
                    reasoning_option=option,
                    json_mode=True,
                    json_schema=schema,
                    json_schema_name="test_schema",
                )
            return captured

        deepseek = capture("deepseek", "deepseek-v4-pro", "max")
        self.assertEqual({"type": "enabled"}, deepseek["thinking"])
        self.assertEqual("max", deepseek["reasoning_effort"])
        kimi_k3 = capture("kimi", "kimi-k3", "high")
        self.assertEqual("high", kimi_k3["reasoning_effort"])
        self.assertNotIn("thinking", kimi_k3)
        kimi_k26 = capture("kimi", "kimi-k2.6", "disabled")
        self.assertEqual({"type": "disabled"}, kimi_k26["thinking"])
        gemini = capture("gemini", "gemini-3.7-flash", "medium")
        self.assertEqual("medium", gemini["reasoning_effort"])
        for body in (deepseek, kimi_k3, kimi_k26, gemini):
            self.assertNotIn("temperature", body)
            self.assertEqual({"type": "json_object"}, body["response_format"])
        self.assertEqual(393216, deepseek["max_tokens"])
        self.assertEqual(65536, gemini["max_tokens"])
        self.assertEqual(131072, kimi_k26["max_completion_tokens"])
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        gemini_strict = capture("gemini", "gemini-3.6-flash", "low", schema)
        self.assertEqual(
            {"type": "json_schema", "json_schema": {"name": "test_schema", "strict": True, "schema": schema}},
            gemini_strict["response_format"],
        )
        kimi_strict = capture("kimi", "kimi-k2.6", "disabled", schema)
        self.assertEqual(
            {"type": "json_schema", "json_schema": {"name": "test_schema", "strict": True, "schema": schema}},
            kimi_strict["response_format"],
        )

        fixed_config = ProviderConfig(
            name="kimi",
            model="kimi-k2.7-code",
            base_url="http://127.0.0.1:8765",
            api_key="local",
            key_variable="TEST_KEY",
        )
        with self.assertRaises(ProviderConfigurationError):
            ChatCompletionClient(fixed_config, max_transient_retries=0).complete(
                [{"role": "user", "content": "test"}], reasoning_option="disabled"
            )

    def test_unconfirmed_input_fails_closed_without_api_or_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "paper.md"
            path.write_text(long_manuscript(), encoding="utf-8")
            result = analyze_manuscript(RunOptions(manuscript_path=path))
            self.assertEqual("UNASSESSED", result.closure_card["Verdict"])
            self.assertFalse(result.api_called)
            self.assertIsNone(result.provider)

    def test_kimi_analysis_uses_separate_coverage_and_adjudication_timeouts(self) -> None:
        completions = iter(
            (
                CompletionResult(content=json.dumps(COVERAGE_STATE), model="kimi-k2.6", usage={}),
                CompletionResult(content=json.dumps(ADJUDICATION_STATE), model="kimi-k2.6", usage={}),
            )
        )
        observed_timeouts: list[float] = []

        def mocked_complete(client: ChatCompletionClient, _messages: object, **_kwargs: object) -> CompletionResult:
            observed_timeouts.append(client.timeout_seconds)
            if client.on_attempt is not None:
                client.on_attempt(1)
            return next(completions)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "local"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete", new=mocked_complete):
            path = Path(directory) / "paper.md"
            path.write_text(long_manuscript(), encoding="utf-8")
            event_path = Path(directory) / "events.jsonl"
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="kimi",
                    model="kimi-k2.6",
                    confirm_complete_current_manuscript=True,
                    manuscript_identity="paper-v1",
                ),
                event_sink=EventSink(jsonl_path=event_path),
            )
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("STOP_REVISING", result.closure_card["Verdict"])
        self.assertEqual([300.0, 900.0], observed_timeouts)
        attempt_timeouts = [event["timeout_seconds"] for event in events if event["type"] == "provider.attempt"]
        self.assertEqual([300.0, 900.0], attempt_timeouts)

    def test_valid_model_state_is_deterministically_closed(self) -> None:
        completions = iter(
            (
                CompletionResult(
                    content=json.dumps(COVERAGE_STATE),
                    model="deepseek-v4-pro",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                ),
                CompletionResult(
                    content=json.dumps(ADJUDICATION_STATE),
                    model="deepseek-v4-pro",
                    usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "secret-that-must-not-leak"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete", side_effect=completions):
            path = Path(directory) / "paper.md"
            path.write_text(long_manuscript(), encoding="utf-8")
            event_path = Path(directory) / "events.jsonl"
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    confirm_complete_current_manuscript=True,
                    manuscript_identity="paper-v1",
                ),
                event_sink=EventSink(jsonl_path=event_path),
            )
            self.assertEqual("STOP_REVISING", result.closure_card["Verdict"])
            self.assertIn("未观察到", result.closure_card["Reason"])
            self.assertIn("不要启动", result.closure_card["Next permitted action"])
            self.assertTrue(result.api_called)
            self.assertEqual(2, len(result.usage_calls))
            self.assertTrue(result.harness["contradiction_gate_passed"])
            self.assertEqual("paper-v1", result.minimal_receipt["manuscript_identity"])
            log = event_path.read_text(encoding="utf-8")
            self.assertNotIn("secret-that-must-not-leak", log)
            self.assertNotIn("stable manuscript marker", log)
            event_types = [json.loads(line)["type"] for line in log.splitlines()]
            self.assertEqual("thread.started", event_types[0])
            self.assertEqual("turn.completed", event_types[-1])

    def test_fixed_card_prose_is_deterministically_localized(self) -> None:
        english = {
            "Reason": "No observed material root cause justifies reopening substantive revision; remaining holds are separate from the revision cutoff.",
            "Next permitted action": "Do not start another generic AI revision; address any listed evidence or submission hold separately if authorized.",
        }
        chinese = localize_closure_card(english, "zh")
        self.assertIn("未观察到", chinese["Reason"])
        self.assertIn("不要启动", chinese["Next permitted action"])
        self.assertEqual(english, localize_closure_card(english, "en"))

    def test_invalid_model_state_has_no_semantic_repair_loop(self) -> None:
        completions = iter(
            (
                CompletionResult(content=json.dumps(COVERAGE_STATE), model="kimi-k2.6", usage={}),
                CompletionResult(
                    content=json.dumps(ADJUDICATION_STATE | {"extra": True}),
                    model="kimi-k2.6",
                    usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "secret"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete", side_effect=completions) as mocked:
            path = Path(directory) / "paper.md"
            path.write_text(long_manuscript(), encoding="utf-8")
            result = analyze_manuscript(
                RunOptions(
                    manuscript_path=path,
                    provider="kimi",
                    confirm_complete_current_manuscript=True,
                )
            )
        self.assertEqual(2, mocked.call_count)
        runtime = result.as_dict()["runtime"]
        self.assertEqual("UNASSESSED", result.closure_card["Verdict"])
        self.assertEqual("HOLD", runtime["machine_status"])
        self.assertEqual("SUCCEEDED", runtime["machine_provider_outcome"])
        self.assertEqual(7, runtime["usage_calls"][1]["total_tokens"])
        self.assertEqual(0, runtime["presentation_repair_call_count"])

    def test_stable_prior_stop_receipt_skips_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text(long_manuscript(), encoding="utf-8")
            document = read_document(path)
            base_state = {
                "manuscript_complete": True,
                "current_identity_clear": True,
                "whole_manuscript_read": True,
                "critical_basis_available": True,
                "bounded_scope": False,
                "current_manuscript_identity": "paper-v1",
                "current_artifact_sha256": document.artifact_sha256,
                "current_semantic_content_sha256": document.semantic_content_sha256,
                "material_root_causes": [],
                "evidence_hold_codes": [],
                "submission_hold_codes": [],
                "protected": [],
                "lite_suggestions": [],
                "invalidation_events": [],
                "artifact_only_drift_verified": False,
                "formal_tone": False,
                "rewrite_requested": False,
            }
            receipt = minimal_receipt(
                decide_state(base_state),
                "paper-v1",
                artifact_sha256=document.artifact_sha256,
                semantic_content_sha256=document.semantic_content_sha256,
            )
            with patch("standalone.assessor.ChatCompletionClient.complete") as mocked:
                result = analyze_manuscript(
                    RunOptions(
                        manuscript_path=path,
                        confirm_complete_current_manuscript=True,
                        manuscript_identity="paper-v1",
                        prior_receipt=receipt,
                    )
                )
            mocked.assert_not_called()
            self.assertFalse(result.api_called)
            self.assertEqual("STOP_REVISING", result.closure_card["Verdict"])


if __name__ == "__main__":
    unittest.main()
