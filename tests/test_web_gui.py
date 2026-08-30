from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch


from standalone.harness import (
    AFFIRMATIVE_STOP_DIMENSIONS,
    COVERAGE_CONTRACT_VERSION,
    COVERAGE_DIMENSIONS,
    canonical_digest,
)
from standalone.providers import ChatCompletionClient, CompletionResult
from standalone.web_gui import create_gui_server


CORE_MODEL_STATE = {
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
    "protected": ["保持现有论点上限。"],
    "parked_opportunities": [],
    "lite_suggestions": [],
}

COVERAGE_STATE = {
    "coverage_contract_version": COVERAGE_CONTRACT_VERSION,
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
            "status": "CLEAR",
            "affirmative_sufficiency": True,
            "sufficiency_reason_code": "AFFIRMATIVE_MANUSCRIPT_SUPPORT",
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

ADJUDICATION_STATE = {
    "coverage_digest_sha256": canonical_digest(COVERAGE_STATE),
    **CORE_MODEL_STATE,
}

INTERPRETATION_STATE = {
    "status_explanation": "核心裁决已经完成，不应由一般性修改重新打开。",
    "judgment_basis": ["本次判断使用完整当前稿件。", "确定性 Closure Card 提供核心状态。"],
    "judgment_principles": ["材料性根因门槛优先。", "保护论点与证据边界。", "修改截止与投稿准备分轴判断。"],
    "assessment_dimensions": [
        {"dimension": "稿件身份", "finding": "当前身份明确。", "implication": "可以形成整稿判断。"},
        {"dimension": "贡献层级", "finding": "主要贡献可辨认。", "implication": "无需中心重写。"},
        {"dimension": "证据边界", "finding": "现有边界应保护。", "implication": "不得增强主张。"},
        {"dimension": "章节结构", "finding": "章节角色互补。", "implication": "不建议结构重做。"},
        {"dimension": "双轴状态", "finding": "投稿事项独立判断。", "implication": "不能据此重开改稿。"},
    ],
    "selective_findings": [],
    "what_is_stable": ["保持当前贡献层级和证据边界。"],
    "remaining_attention": [],
    "pre_submission_checklist": [
        "人工核对匿名化要求。",
        "人工核对作者信息和声明。",
        "人工核对图表与版权状态。",
    ],
    "optional_micro_adjustments": [],
    "report_limitations": ["没有执行外部事实核验。", "不能替代作者与同行评审判断。"],
    "boundary_note": "本解读不是事实认证、同行评审替代品或投稿授权。",
}


def _long_manuscript() -> str:
    return (
        "Title\n\nAbstract\n"
        + ("bounded argument and evidence.\n" * 90)
        + "\nConclusion\nThe argument remains bounded.\n\nReferences\nReference A."
    )


class WebGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server, self.state, self.url = create_gui_server()
        self.base_url = self.url.split("/?", 1)[0]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(
        self,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        headers = dict(extra_headers or {})
        if token is not None:
            headers["X-MRC-Token"] = token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def _prepare_consent(self, manuscript: Path, provider: str, model: str) -> dict[str, object]:
        status, body, _headers = self._request(
            "/api/prepare-consent",
            token=self.state.token,
            payload={
                "manuscript_path": str(manuscript),
                "provider": provider,
                "model": model,
            },
        )
        self.assertEqual(200, status, body)
        return json.loads(body)

    def test_page_and_api_require_random_token_and_local_host(self) -> None:
        status, _body, _headers = self._request("/api/status")
        self.assertEqual(403, status)
        status, _body, _headers = self._request("/api/status", token="wrong")
        self.assertEqual(403, status)
        status, _body, _headers = self._request(
            "/api/status",
            token=self.state.token,
            extra_headers={"Host": "attacker.invalid"},
        )
        self.assertEqual(403, status)
        status, body, headers = self._request("/api/status", token=self.state.token)
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual("ready", json.loads(body)["phase"])

    def test_gui_has_no_remote_assets_and_never_exposes_key_value(self) -> None:
        opaque_value = "GUI-" + "CREDENTIAL-MUST-NOT-LEAK"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": opaque_value}, clear=True):
            status, body, _headers = self._request("/", token=self.state.token)
            self.assertEqual(200, status)
            html = body.decode("utf-8")
            self.assertNotIn("https://", html)
            self.assertNotIn("<script src=", html)
            self.assertIn('<select id="model"></select>', html)
            self.assertIn('<select id="reasoning"></select>', html)
            self.assertNotIn("<datalist", html)
            self.assertNotIn("它不会改变核心裁决，也不会公开思维链或原始隐藏审阅记录", html)
            self.assertNotIn("const document=bundle", html)
            self.assertIn("const doc=bundle&&bundle.document", html)
            self.assertIn("每次运行都必须重新明确确认", html)
            self.assertIn("/api/prepare-consent", html)
            self.assertNotIn(opaque_value, html)
            status, body, _headers = self._request("/api/status", token=self.state.token)
            self.assertEqual(200, status)
            text = body.decode("utf-8")
            self.assertNotIn(opaque_value, text)
            provider = json.loads(text)["providers"]["deepseek"]
            self.assertTrue(provider["key_present"])
            self.assertEqual("DEEPSEEK_API_KEY", provider["key_variable"])

    def test_model_catalog_endpoint_returns_live_list_or_explicit_fallback(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "opaque-gemini-value"}, clear=True), patch(
            "standalone.web_gui.list_provider_models",
            return_value=["gemini-3.7-flash", "gemini-3.6-flash"],
        ):
            status, body, _headers = self._request(
                "/api/models", token=self.state.token, payload={"provider": "gemini"}
            )
        self.assertEqual(200, status)
        result = json.loads(body)
        self.assertEqual("live_provider_api", result["source"])
        self.assertEqual("gemini-3.7-flash", result["models"][0])
        with patch.dict(os.environ, {}, clear=True):
            status, body, _headers = self._request(
                "/api/models", token=self.state.token, payload={"provider": "gemini"}
            )
        self.assertEqual(200, status)
        result = json.loads(body)
        self.assertEqual("bundled_fallback", result["source"])
        self.assertIn("gemini-3.7-flash", result["models"])
        self.assertGreater(len(result["models"]), 5)

    def test_reasoning_options_follow_provider_and_model_contract(self) -> None:
        status, body, _headers = self._request(
            "/api/reasoning-options",
            token=self.state.token,
            payload={"provider": "gemini", "model": "gemini-3.7-flash"},
        )
        self.assertEqual(200, status)
        profile = json.loads(body)
        self.assertEqual(["default", "low", "medium", "high"], [item["value"] for item in profile["options"]])
        status, body, _headers = self._request(
            "/api/reasoning-options",
            token=self.state.token,
            payload={"provider": "kimi", "model": "kimi-k2.7-code"},
        )
        self.assertEqual(200, status)
        profile = json.loads(body)
        self.assertEqual(["default"], [item["value"] for item in profile["options"]])

    def test_analysis_api_records_user_cancellation_without_api(self) -> None:
        marker = "GUI-MANUSCRIPT-CONTENT-MUST-NOT-LEAK"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "mock"}, clear=True
        ):
            manuscript = Path(directory) / "paper.md"
            manuscript.write_text(_long_manuscript() + marker, encoding="utf-8")
            prepared = self._prepare_consent(manuscript, "deepseek", "deepseek-v4-pro")
            status, body, _headers = self._request(
                "/api/analyze",
                token=self.state.token,
                payload={
                    "manuscript_path": str(manuscript),
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "language": "zh",
                    "identity": "paper-gui-v1",
                    "confirmed_complete": False,
                    "prior_receipt_path": "",
                    "consent_token": prepared["consent_token"],
                    "consent_confirmed": False,
                },
            )
            self.assertEqual(202, status, body)
            deadline = time.monotonic() + 3
            snapshot = self.state.snapshot()
            while snapshot["busy"] and time.monotonic() < deadline:
                time.sleep(0.02)
                snapshot = self.state.snapshot()
            self.assertFalse(snapshot["busy"])
            self.assertIsNone(snapshot["error"])
            self.assertEqual("UNASSESSED", snapshot["result"]["closure_card"]["Verdict"])
            self.assertFalse(snapshot["result"]["runtime"]["api_called"])
            self.assertEqual("canceled", snapshot["phase"])
            self.assertEqual(
                "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED",
                snapshot["result"]["minimal_receipt"]["reason_category"],
            )
            self.assertNotIn(marker, json.dumps(snapshot, ensure_ascii=False))

    def test_gui_consent_is_hash_bound_and_token_is_one_use(self) -> None:
        marker = "SYNTHETIC-CONSENT-MARKER"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "mock"}, clear=True
        ), patch("standalone.assessor.ChatCompletionClient.complete") as provider_call:
            manuscript = Path(directory) / "paper.md"
            manuscript.write_text(_long_manuscript(), encoding="utf-8")
            prepared = self._prepare_consent(manuscript, "deepseek", "deepseek-v4-pro")
            self.assertEqual(str(manuscript.resolve()), prepared["path"])
            self.assertEqual(64, len(prepared["artifact_sha256"]))
            self.assertNotIn(_long_manuscript(), json.dumps(prepared, ensure_ascii=False))
            manuscript.write_text(_long_manuscript() + marker, encoding="utf-8")
            status, _body, _headers = self._request(
                "/api/analyze",
                token=self.state.token,
                payload={
                    "manuscript_path": str(manuscript),
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "language": "en",
                    "identity": "paper-gui-hash-change",
                    "confirmed_complete": True,
                    "prior_receipt_path": "",
                    "generate_interpretation": False,
                    "consent_token": prepared["consent_token"],
                    "consent_confirmed": True,
                },
            )
            self.assertEqual(202, status)
            deadline = time.monotonic() + 3
            snapshot = self.state.snapshot()
            while snapshot["busy"] and time.monotonic() < deadline:
                time.sleep(0.02)
                snapshot = self.state.snapshot()
        provider_call.assert_not_called()
        self.assertEqual("canceled", snapshot["phase"])
        self.assertFalse(snapshot["result"]["runtime"]["api_called"])
        reused = self.state.consume_consent(
            prepared["consent_token"],
            True,
            manuscript=str(manuscript),
            provider="deepseek",
            model="deepseek-v4-pro",
        )
        self.assertFalse(reused)

    def test_unknown_analysis_field_fails_closed(self) -> None:
        status, _body, _headers = self._request(
            "/api/analyze",
            token=self.state.token,
            payload={"manuscript_path": "unused.md", "unexpected": True},
        )
        self.assertEqual(202, status)
        deadline = time.monotonic() + 3
        snapshot = self.state.snapshot()
        while snapshot["busy"] and time.monotonic() < deadline:
            time.sleep(0.02)
            snapshot = self.state.snapshot()
        self.assertEqual("failed", snapshot["phase"])
        self.assertIn("unknown fields", snapshot["error"])

    def test_interpretation_failure_cannot_erase_completed_core_result(self) -> None:
        self.assertTrue(self.state.start())
        core = {"closure_card": {"Verdict": "STOP_REVISING"}, "minimal_receipt": {}, "runtime": {}}
        self.state.core_ready(core)
        self.state.interpretation_fail("bounded interpretation contract mismatch")
        snapshot = self.state.snapshot()
        self.assertEqual("completed_with_interpretation_hold", snapshot["phase"])
        self.assertEqual("STOP_REVISING", snapshot["result"]["closure_card"]["Verdict"])
        self.assertIn("contract mismatch", snapshot["interpretation_error"])

    def test_confirmed_gui_run_generates_bounded_chinese_interpretation_and_timeline(self) -> None:
        completions = iter(
            (
                CompletionResult(
                    content=json.dumps(COVERAGE_STATE, ensure_ascii=False),
                    model="deepseek-v4-pro",
                    usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                ),
                CompletionResult(
                    content=json.dumps(ADJUDICATION_STATE, ensure_ascii=False),
                    model="deepseek-v4-pro",
                    usage={"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
                ),
                CompletionResult(
                    content=json.dumps(INTERPRETATION_STATE, ensure_ascii=False),
                    model="deepseek-v4-pro",
                    usage={"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
                ),
            )
        )

        reasoning_seen: list[str] = []
        json_mode_seen: list[bool] = []

        def mocked_complete(
            client: ChatCompletionClient,
            _messages: object,
            *,
            reasoning_option: str | None = None,
            json_mode: bool = False,
            **_kwargs: object,
        ) -> CompletionResult:
            if client.on_attempt is not None:
                client.on_attempt(1)
            reasoning_seen.append(reasoning_option or "default")
            json_mode_seen.append(json_mode)
            return next(completions)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "local-gui-test-value"}, clear=True
        ), patch.object(
            ChatCompletionClient, "complete", autospec=True, side_effect=mocked_complete
        ) as mocked, patch(
            "standalone.web_gui.price_with_fallback", return_value=None
        ):
            manuscript = Path(directory) / "paper.md"
            manuscript.write_text(_long_manuscript(), encoding="utf-8")
            prepared = self._prepare_consent(manuscript, "deepseek", "deepseek-v4-pro")
            status, _body, _headers = self._request(
                "/api/analyze",
                token=self.state.token,
                payload={
                    "manuscript_path": str(manuscript),
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "reasoning_option": "high",
                    "language": "zh",
                    "identity": "paper-gui-v2",
                    "confirmed_complete": True,
                    "prior_receipt_path": "",
                    "generate_interpretation": True,
                    "consent_token": prepared["consent_token"],
                    "consent_confirmed": True,
                },
            )
            self.assertEqual(202, status)
            deadline = time.monotonic() + 3
            snapshot = self.state.snapshot()
            while snapshot["busy"] and time.monotonic() < deadline:
                time.sleep(0.02)
                snapshot = self.state.snapshot()
            self.assertEqual(3, mocked.call_count)
            self.assertEqual(["high", "high", "high"], reasoning_seen)
            self.assertEqual([True, True, True], json_mode_seen)
            self.assertEqual("completed", snapshot["phase"])
            self.assertEqual("STOP_REVISING", snapshot["result"]["closure_card"]["Verdict"])
            self.assertEqual("high", snapshot["result"]["runtime"]["reasoning_option"])
            self.assertEqual(
                INTERPRETATION_STATE,
                snapshot["result"]["interpretation"]["document"],
            )
            messages = [item["message"] for item in snapshot["timeline"]]
            self.assertTrue(any("第 1 次请求" in message for message in messages))
            self.assertTrue(any("十一键合同校验" in message for message in messages))

    def test_failed_interpretation_usage_is_still_billed_and_raw_output_is_not_exposed(self) -> None:
        completions = iter(
            (
                CompletionResult(
                    content=json.dumps(COVERAGE_STATE, ensure_ascii=False),
                    model="gemini-3.6-flash",
                    usage={"prompt_tokens": 1000, "completion_tokens": 60, "total_tokens": 1060},
                ),
                CompletionResult(
                    content=json.dumps(ADJUDICATION_STATE, ensure_ascii=False),
                    model="gemini-3.6-flash",
                    usage={"prompt_tokens": 1100, "completion_tokens": 70, "total_tokens": 1170},
                ),
                CompletionResult(
                    content="invalid public interpretation output that must not be retained",
                    model="gemini-3.6-flash",
                    usage={"prompt_tokens": 1400, "completion_tokens": 90, "total_tokens": 1490},
                ),
            )
        )

        def mocked_complete(client: ChatCompletionClient, _messages: object, **_kwargs: object) -> CompletionResult:
            if client.on_attempt is not None:
                client.on_attempt(1)
            return next(completions)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"GEMINI_API_KEY": "local"}, clear=True
        ), patch.object(
            ChatCompletionClient, "complete", autospec=True, side_effect=mocked_complete
        ), patch("standalone.web_gui.price_with_fallback", return_value=None):
            manuscript = Path(directory) / "paper.md"
            manuscript.write_text(_long_manuscript(), encoding="utf-8")
            prepared = self._prepare_consent(manuscript, "gemini", "gemini-3.6-flash")
            status, _body, _headers = self._request(
                "/api/analyze",
                token=self.state.token,
                payload={
                    "manuscript_path": str(manuscript),
                    "provider": "gemini",
                    "model": "gemini-3.6-flash",
                    "reasoning_option": "low",
                    "language": "zh",
                    "identity": "paper-gui-failed-interpretation",
                    "confirmed_complete": True,
                    "prior_receipt_path": "",
                    "generate_interpretation": True,
                    "consent_token": prepared["consent_token"],
                    "consent_confirmed": True,
                },
            )
            self.assertEqual(202, status)
            deadline = time.monotonic() + 3
            snapshot = self.state.snapshot()
            while snapshot["busy"] and time.monotonic() < deadline:
                time.sleep(0.02)
                snapshot = self.state.snapshot()
        self.assertEqual("completed_with_interpretation_hold", snapshot["phase"])
        self.assertEqual("模型未按要求返回单一 JSON 解读对象", snapshot["interpretation_error"])
        self.assertEqual(3, len(snapshot["result"]["task_cost"]["calls"]))
        self.assertEqual(1490, snapshot["result"]["failed_interpretation_runtime"]["usage"]["total_tokens"])
        self.assertNotIn("invalid public interpretation", json.dumps(snapshot, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
