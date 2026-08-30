"""Direct frozen-EXE acceptance implementation for the bounded MRC 0.6.4 repair.

Uses only loopback mock providers and synthetic temporary manuscripts.  The
script intentionally imports no application modules, so every assertion is
against the frozen executable's public CLI/GUI behavior and receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "release" / "ManuscriptRevisionClosure.exe"
BUILD_RECEIPT = json.loads((ROOT / "release" / "BUILD_RECEIPT.json").read_text(encoding="utf-8"))

COVERAGE_CONTRACT_VERSION = "mrc-whole-manuscript-coverage-3.0"
COVERAGE_DIMENSIONS = (
    "contribution",
    "whole_paper_argument",
    "theory_and_concepts",
    "methods_and_research_design",
    "evidence_and_analysis",
    "rivals_negative_findings_and_limitations",
    "section_roles_and_coherence",
    "claim_ceiling_and_scope_conditions",
    "evidence_status_and_provenance",
    "revision_vs_submission_boundary",
)
AFFIRMATIVE_STOP_DIMENSIONS = (
    "contribution",
    "whole_paper_argument",
    "theory_and_concepts",
    "methods_and_research_design",
    "evidence_and_analysis",
    "section_roles_and_coherence",
)


def _canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assert_not_formed_value(receipt: dict[str, Any], field: str) -> None:
    """Accept only the contract's two no-value encodings: omission or explicit null."""

    assert field not in receipt or receipt[field] is None, (field, receipt.get(field))


def coverage_state(candidates: list[str]) -> dict[str, Any]:
    candidate_set = set(candidates)
    return {
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
                "status": (
                    "POTENTIAL_MATERIAL_ROOT_CAUSE" if dimension in candidate_set else "CLEAR"
                ),
                "affirmative_sufficiency": True,
                "sufficiency_reason_code": "AFFIRMATIVE_MANUSCRIPT_SUPPORT",
            }
            for dimension in COVERAGE_DIMENSIONS
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


def insufficient_coverage_state() -> dict[str, Any]:
    return {
        "coverage_contract_version": COVERAGE_CONTRACT_VERSION,
        "whole_manuscript_basis": "INSUFFICIENT",
        "basis_reason_codes": ["FRAGMENT_OR_EXCERPT_ONLY"],
        "basis_explanation": "The supplied material is substantively only a fragment or excerpt.",
        "manuscript_identity_confirmed": True,
        "full_span_covered": False,
        "dimensions": [
            {
                "dimension": dimension,
                "applicability": "APPLICABLE",
                "assessed": False,
                "status": "UNASSESSED",
                "affirmative_sufficiency": False,
                "sufficiency_reason_code": "UNASSESSED",
            }
            for dimension in COVERAGE_DIMENSIONS
        ],
        "root_cause_candidate_dimensions": [],
        "evidence_hold_codes": [],
        "submission_hold_codes": [],
        "protected_invariants": {
            "claim_ceiling_preserved": False,
            "evidence_status_distinctions_preserved": False,
            "rivals_and_negative_findings_preserved": False,
        },
    }


def cause_row(
    dimension: str,
    *,
    coverage_candidate: bool = True,
    material: bool = False,
    scope: str = "local",
) -> dict[str, Any]:
    return {
        "observed": material,
        "locatable": material,
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


def adjudication_state(
    coverage: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    protected: list[str] | None = None,
) -> dict[str, Any]:
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
    return {
        "coverage_digest_sha256": _canonical_digest(coverage),
        "material_root_causes": deepcopy(rows),
        "affirmative_sufficiency": [
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
        ],
        "evidence_hold_codes": [],
        "submission_hold_codes": [],
        "protected": protected or ["保持论点上限和可见的替代解释。"],
        "parked_opportunities": [],
        "lite_suggestions": [],
    }


INTERPRETATION_STATE = {
    "status_explanation": "核心裁决已经完成，不应由一般性实质修改重新打开。",
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
    "what_is_stable": ["保持当前贡献层级与证据边界。"],
    "remaining_attention": [],
    "pre_submission_checklist": ["人工核对匿名化要求。", "人工核对作者信息与声明。", "人工核对图表和版权状态。"],
    "optional_micro_adjustments": [],
    "report_limitations": ["没有执行外部事实核验。", "不能替代作者与同行评审判断。"],
    "boundary_note": "本解读不是事实认证、同行评审替代品或投稿授权。",
}


def synthetic_manuscript() -> str:
    return (
        "Synthetic Frozen Acceptance Manuscript\n\nAbstract\n"
        + ("Bounded synthetic argument, evidence, and scope condition.\n" * 90)
        + "\nConclusion\nThe synthetic contribution remains bounded.\n\n"
        + "References\nSynthetic Reference A."
    )


class MockProvider:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.requests: list[dict[str, Any]] = []
        self.stages: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                stage = outer._stage(body)
                outer._validate_request(stage, body)
                outer.requests.append(body)
                outer.stages.append(stage)
                status, state = outer._response(stage)
                if status != 200:
                    payload = (
                        b'{"error":{"status":"UNAVAILABLE","code":"mock_overload",'
                        b'"message":"bounded mock failure"}}'
                    )
                    self.send_response(status)
                    self.send_header("Retry-After", "7")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = json.dumps(
                    {
                        "model": body.get("model", "mock-model"),
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(state, ensure_ascii=False),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def _stage(body: dict[str, Any]) -> str:
        messages = body.get("messages", [])
        system = messages[0].get("content", "") if messages and isinstance(messages[0], dict) else ""
        if "INTERPRETATION AGENT CONTRACT" in system:
            return "interpretation"
        if "whole-manuscript coverage stage" in system:
            return "coverage"
        if "root-cause adjudication stage" in system:
            return "adjudication"
        if "presentation-only localization stage" in system:
            return "presentation_repair"
        raise AssertionError("frozen EXE sent an unrecognized provider stage")

    @staticmethod
    def _validate_request(stage: str, body: dict[str, Any]) -> None:
        if stage != "adjudication":
            return
        messages = body["messages"]
        system = messages[0]["content"]
        assert "required lower bound" in system
        assert "you may add a dimension omitted by coverage" in system
        response_format = body["response_format"]
        if response_format["type"] == "json_schema":
            schema = response_format["json_schema"]["schema"]
            causes = schema["properties"]["material_root_causes"]
            assert causes["maxItems"] == len(COVERAGE_DIMENSIONS)
            assert causes["items"]["properties"]["dimension"]["enum"] == list(
                COVERAGE_DIMENSIONS
            )
            assert causes["items"]["properties"]["dimension"]["enum"]
        else:
            assert response_format == {"type": "json_object"}
            assert "Canonical JSON schema:" in system

    def _states(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.scenario.startswith("basis_insufficient"):
            coverage = insufficient_coverage_state()
        elif self.scenario in {"gemini_multi", "gemini_missing", "gemini_duplicate"}:
            coverage = coverage_state(["contribution", "methods_and_research_design"])
        elif self.scenario in {"gemini_extra", "gemini_ungrounded_addition"}:
            coverage = coverage_state(["contribution"])
        else:
            coverage = coverage_state([])

        if self.scenario == "gemini_missing":
            rows = [cause_row("contribution")]
        elif self.scenario == "gemini_extra":
            rows = [
                cause_row("contribution"),
                cause_row(
                    "theory_and_concepts",
                    coverage_candidate=False,
                    material=True,
                    scope="central",
                ),
            ]
        elif self.scenario == "gemini_ungrounded_addition":
            rows = [
                cause_row("contribution"),
                cause_row("theory_and_concepts", coverage_candidate=False),
            ]
        elif self.scenario == "gemini_duplicate":
            rows = [
                cause_row("contribution"),
                cause_row("contribution"),
                cause_row("methods_and_research_design"),
            ]
        elif self.scenario == "semantic_reopen_addition":
            rows = [
                cause_row(
                    "contribution",
                    coverage_candidate=False,
                    material=True,
                    scope="central",
                )
            ]
        elif self.scenario == "semantic_one_round_addition":
            rows = [
                cause_row(
                    "methods_and_research_design",
                    coverage_candidate=False,
                    material=True,
                    scope="local",
                )
            ]
        else:
            rows = [cause_row(item) for item in coverage["root_cause_candidate_dimensions"]]

        protected = (
            ["Preserve the bounded contribution and visible rival explanations."]
            if self.scenario == "presentation_hold_gui"
            else None
        )
        return coverage, adjudication_state(coverage, rows, protected=protected)

    def _response(self, stage: str) -> tuple[int, dict[str, Any]]:
        coverage, adjudication = self._states()
        if self.scenario in {"gemini_503", "machine_hold_gui"} and stage == "coverage":
            return 503, {}
        if self.scenario == "presentation_hold_gui" and stage == "presentation_repair":
            return 503, {}
        if stage == "coverage":
            if self.scenario == "deepseek_key_mismatch":
                broken = dict(coverage)
                broken["unexpected"] = True
                return 200, broken
            return 200, coverage
        if stage == "adjudication":
            return 200, adjudication
        if stage == "interpretation":
            return 200, INTERPRETATION_STATE
        raise AssertionError(f"unexpected stage {stage}")

    def __enter__(self) -> "MockProvider":
        self.thread.start()
        return self

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise AssertionError("mock provider server did not stop")


def provider_env(provider: str, port: int) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "MOONSHOT_API_KEY",
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
        "GEMINI_API_KEY",
        "GEMINI_BASE_URL",
    ):
        env.pop(key, None)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    base = f"http://127.0.0.1:{port}"
    if provider == "deepseek":
        env.update({"DEEPSEEK_API_KEY": "mock", "DEEPSEEK_BASE_URL": base})
    elif provider == "kimi":
        env.update({"MOONSHOT_API_KEY": "mock", "KIMI_BASE_URL": base})
    elif provider == "gemini":
        env.update({"GEMINI_API_KEY": "mock", "GEMINI_BASE_URL": base})
    else:
        raise AssertionError("unknown provider")
    return env


def validate_common_runtime(result: dict[str, Any], events: list[dict[str, Any]]) -> None:
    runtime = result["runtime"]
    attempts = [event for event in events if event.get("type") == "provider.attempt"]
    terminal = [event for event in events if event.get("type") in {"turn.completed", "turn.failed"}]
    assert len(terminal) == 1, terminal
    for event in attempts:
        assert event.get("provider")
        assert event.get("model")
        assert event.get("reasoning_option")
        assert event.get("max_transient_retries") == 0
        assert event.get("retry_decision") == "STOP_NO_AUTOMATIC_RETRY"
    assert runtime["provider_call_count"] == runtime["physical_request_attempt_count"]
    assert runtime["raw_provider_response_persisted"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert "Authorization" not in serialized
    assert '"api_key"' not in serialized
    assert '"raw_provider_response"' not in serialized


def run_cli_case(
    scenario: str,
    provider: str,
    model: str,
    manuscript_text: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    with MockProvider(scenario) as mock, tempfile.TemporaryDirectory(prefix="mrc064-frozen-cli-") as directory:
        temp = Path(directory)
        manuscript = temp / "synthetic.md"
        output = temp / "result.json"
        event_log = temp / "events.jsonl"
        manuscript.write_text(manuscript_text or synthetic_manuscript(), encoding="utf-8")
        completed = subprocess.run(
            [
                str(EXE),
                str(manuscript),
                "--provider",
                provider,
                "--model",
                model,
                "--reasoning",
                "default",
                "--language",
                "zh",
                "--identity",
                f"synthetic-{scenario}",
                "--confirm-complete",
                "--consent-to-provider-transmission",
                "--output",
                str(output),
                "--event-log",
                str(event_log),
            ],
            cwd=temp,
            env=provider_env(provider, mock.port),
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert completed.returncode == 0, (scenario, completed.stdout, completed.stderr)
        result = json.loads(output.read_text(encoding="utf-8"))
        events = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines() if line]
        validate_common_runtime(result, events)
        requests = deepcopy(mock.requests)
    return result, events, requests


def run_intake_only_case(name: str, manuscript_text: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mrc064-frozen-intake-") as directory:
        temp = Path(directory)
        manuscript = temp / "synthetic.md"
        output = temp / "result.json"
        manuscript.write_text(manuscript_text, encoding="utf-8")
        env = os.environ.copy()
        for key in (
            "DEEPSEEK_API_KEY",
            "MOONSHOT_API_KEY",
            "KIMI_API_KEY",
            "GEMINI_API_KEY",
        ):
            env.pop(key, None)
        env["GEMINI_API_KEY"] = "mock-consent-denied-no-network"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [
                str(EXE),
                str(manuscript),
                "--provider",
                "gemini",
                "--model",
                "gemini-3.7-flash",
                "--identity",
                f"synthetic-intake-{name}",
                "--output",
                str(output),
            ],
            cwd=temp,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert completed.returncode == 0, (name, completed.stdout, completed.stderr)
        result = json.loads(output.read_text(encoding="utf-8"))
        assert result["runtime"]["api_called"] is False
        return result


def _request_json(url: str, token: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="GET" if payload is None else "POST",
        headers={"X-MRC-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def run_gui_case(
    scenario: str,
    provider: str,
    model: str,
    *,
    interpretation: bool,
    consent_confirmed: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with MockProvider(scenario) as mock, tempfile.TemporaryDirectory(prefix="mrc064-frozen-gui-") as directory:
        temp = Path(directory)
        manuscript = temp / "synthetic.md"
        manuscript.write_text(synthetic_manuscript(), encoding="utf-8")
        process = subprocess.Popen(
            [str(EXE), "--gui-no-browser"],
            cwd=temp,
            env=provider_env(provider, mock.port),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            assert process.stdout is not None
            line = process.stdout.readline().strip()
            assert line.startswith("Local GUI URL: "), line
            gui_url = line.split(": ", 1)[1]
            parsed = urllib.parse.urlsplit(gui_url)
            token = urllib.parse.parse_qs(parsed.query)["token"][0]
            base = f"{parsed.scheme}://{parsed.netloc}"
            prepared = _request_json(
                base + "/api/prepare-consent",
                token,
                payload={
                    "manuscript_path": str(manuscript),
                    "provider": provider,
                    "model": model,
                },
            )
            accepted = _request_json(
                base + "/api/analyze",
                token,
                payload={
                    "manuscript_path": str(manuscript),
                    "provider": provider,
                    "model": model,
                    "reasoning_option": "default",
                    "language": "zh",
                    "identity": f"synthetic-{scenario}",
                    "confirmed_complete": True,
                    "prior_receipt_path": "",
                    "generate_interpretation": interpretation,
                    "consent_token": prepared["consent_token"],
                    "consent_confirmed": consent_confirmed,
                },
            )
            assert accepted["accepted"] is True
            deadline = time.monotonic() + 60
            snapshot: dict[str, Any] = {}
            while time.monotonic() < deadline:
                snapshot = _request_json(base + "/api/status", token)
                if not snapshot.get("busy"):
                    break
                time.sleep(0.05)
            assert snapshot and not snapshot.get("busy"), snapshot
            _request_json(base + "/api/close", token, payload={})
            process.wait(timeout=10)
            assert process.returncode == 0
            requests = deepcopy(mock.requests)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        assert not list(temp.iterdir()) or list(temp.iterdir()) == [manuscript]
    return snapshot, requests


def main() -> None:
    assert EXE.is_file()
    assert BUILD_RECEIPT["standalone_version"] == "0.6.4"
    assert BUILD_RECEIPT["intake_contract_version"] == "mrc-local-technical-preflight-1.0"
    assert BUILD_RECEIPT["title_evidence_contract_version"] == "mrc-format-advisory-1.0"
    assert len(BUILD_RECEIPT["title_evidence_contract_sha256"]) == 64
    assert BUILD_RECEIPT["manuscript_basis_contract_version"] == "mrc-semantic-manuscript-basis-1.0"
    assert len(BUILD_RECEIPT["manuscript_basis_contract_sha256"]) == 64
    assert (
        BUILD_RECEIPT["provider_transmission_consent_contract_version"]
        == "mrc-provider-transmission-consent-1.0"
    )
    assert len(BUILD_RECEIPT["provider_transmission_consent_contract_sha256"]) == 64
    assert BUILD_RECEIPT["coverage_contract_version"] == "mrc-whole-manuscript-coverage-3.0"
    assert BUILD_RECEIPT["adjudication_contract_version"] == "mrc-root-cause-adjudication-2.0"
    assert BUILD_RECEIPT["contradiction_gate_version"] == "mrc-cross-stage-contradiction-gate-2.0"
    assert BUILD_RECEIPT["candidate_binding_contract_version"] == (
        "mrc-candidate-lower-bound-independent-additions-1.0"
    )
    assert len(BUILD_RECEIPT["candidate_binding_contract_sha256"]) == 64
    assert BUILD_RECEIPT["affirmative_stop_contract_version"] == "mrc-affirmative-stop-gate-1.0"
    assert len(BUILD_RECEIPT["affirmative_stop_contract_sha256"]) == 64
    summary: dict[str, Any] = {"frozen_exe_sha256": hashlib.sha256(EXE.read_bytes()).hexdigest(), "cases": {}}

    kimi, kimi_requests = run_gui_case(
        "kimi_positive_gui", "kimi", "kimi-k2.6", interpretation=True
    )
    assert kimi["phase"] == "completed"
    kimi_runtime = kimi["result"]["runtime"]
    assert kimi_runtime["machine_status"] == "SUCCEEDED"
    assert kimi_runtime["presentation_status"] == "PASS"
    assert kimi_runtime["terminal_status"] == "PASS"
    assert [MockProvider._stage(request) for request in kimi_requests] == [
        "coverage",
        "adjudication",
        "interpretation",
    ]
    assert kimi["result"]["task_cost"]["physical_request_attempt_count"] == 3
    assert kimi["result"]["task_cost"]["usage_receipt_count"] == 3
    assert kimi["result"]["task_cost"]["unknown_potential_charge_attempt_count"] == 0
    assert sum("terminal_event_id" in item.get("details", {}) for item in kimi["timeline"]) == 1
    summary["cases"]["kimi_positive_gui"] = {"requests": 3, "phase": kimi["phase"]}

    transient, _events, transient_requests = run_cli_case(
        "gemini_503", "gemini", "gemini-3.7-flash"
    )
    transient_runtime = transient["runtime"]
    assert len(transient_requests) == 1
    assert transient_runtime["machine_status"] == "HOLD"
    assert transient_runtime["presentation_status"] == "NOT_STARTED"
    assert transient_runtime["physical_request_attempt_count"] == 1
    assert transient_runtime["unknown_potential_charge_attempt_count"] == 1
    physical = transient_runtime["physical_request_receipts"][0]
    assert physical["http_status"] == 503
    assert physical["provider_outcome"] == "UNKNOWN"
    assert physical["retry_after"] == "7"
    assert physical["provider_error_status"] == "UNAVAILABLE"
    assert physical["provider_error_code"] == "mock_overload"
    assert physical["provider_error_detail"] == "bounded mock failure"
    assert transient["minimal_receipt"]["reason_category"] == "TECHNICAL_EXECUTION_HOLD"
    assert transient["minimal_receipt"]["failed_stage"] == "coverage_provider"
    assert transient["minimal_receipt"]["technical_hold_contract_version"] == "mrc-technical-hold-receipt-1.0"
    assert transient["closure_card"]["Failed stage"] == "coverage_provider"
    assert transient_runtime["machine_receipt"]["reason_category"] == "TECHNICAL_EXECUTION_HOLD"
    assert "本地技术预检已通过" in transient["closure_card"]["Reason"]
    summary["cases"]["gemini_503"] = {"requests": 1, "machine_status": "HOLD"}

    multi, _events, multi_requests = run_cli_case(
        "gemini_multi", "gemini", "gemini-3.6-flash"
    )
    assert multi["runtime"]["machine_status"] == "SUCCEEDED"
    assert multi["runtime"]["presentation_status"] == "PASS"
    dynamic = multi_requests[1]["response_format"]["json_schema"]["schema"]
    cause_schema = dynamic["properties"]["material_root_causes"]
    assert cause_schema["minItems"] == 2
    assert cause_schema["maxItems"] == len(COVERAGE_DIMENSIONS)
    assert cause_schema["items"]["properties"]["dimension"]["enum"] == list(
        COVERAGE_DIMENSIONS
    )
    summary["cases"]["gemini_multi"] = {"requests": 2, "dynamic_candidates": 2}

    for scenario, field, expected in (
        ("gemini_missing", "missing_candidates", ["methods_and_research_design"]),
        ("gemini_ungrounded_addition", "ungrounded_additions", ["theory_and_concepts"]),
        ("gemini_duplicate", "duplicate_candidates", ["contribution"]),
    ):
        result, _events, requests = run_cli_case(scenario, "gemini", "gemini-3.6-flash")
        runtime = result["runtime"]
        assert len(requests) == 2
        assert runtime["machine_status"] == "HOLD"
        assert runtime["presentation_status"] == "NOT_STARTED"
        assert runtime["machine_receipt"]["authoritative_presentation_source"] is None
        assert runtime["machine_receipt"]["authoritative_candidate_state"] is False
        diagnostic = runtime["machine_receipt"]["bounded_contract_failure"]
        for required_field in (
            "required_candidates",
            "observed_candidates",
            "missing_candidates",
            "independent_additions",
            "duplicate_candidates",
            "invalid_origin_or_disagreement",
            "invalid_disposition",
            "ungrounded_additions",
        ):
            assert required_field in diagnostic
        assert diagnostic[field] == expected
        summary["cases"][scenario] = {"requests": 2, field: expected}

    recovered, _events, recovered_requests = run_cli_case(
        "gemini_extra", "gemini", "gemini-3.6-flash"
    )
    assert len(recovered_requests) == 2
    assert recovered["closure_card"]["Verdict"] == "REOPEN_SUBSTANTIVE_REVISION"
    recovered_binding = recovered["runtime"]["machine_receipt"]["candidate_binding"]
    assert recovered_binding["required_candidates"] == ["contribution"]
    assert recovered_binding["independent_additions"] == ["theory_and_concepts"]
    summary["cases"]["coverage_miss_recovered"] = {
        "requests": 2,
        "verdict": "REOPEN_SUBSTANTIVE_REVISION",
    }

    for scenario, expected_verdict in (
        ("semantic_one_round_addition", "ONE_BOUNDED_ROUND"),
        ("semantic_reopen_addition", "REOPEN_SUBSTANTIVE_REVISION"),
        ("semantic_affirmative_stop", "STOP_REVISING"),
    ):
        semantic, _events, semantic_requests = run_cli_case(
            scenario, "kimi", "kimi-k2.6"
        )
        assert len(semantic_requests) == 2
        assert semantic["closure_card"]["Verdict"] == expected_verdict
        gate = semantic["runtime"]["machine_receipt"]["affirmative_stop_gate"]
        assert gate["stop_eligible"] is (expected_verdict == "STOP_REVISING")
        summary["cases"][scenario] = {
            "requests": 2,
            "verdict": expected_verdict,
        }

    deepseek, _events, deepseek_requests = run_cli_case(
        "deepseek_valid", "deepseek", "deepseek-v4-pro"
    )
    assert deepseek["runtime"]["machine_status"] == "SUCCEEDED"
    assert len(deepseek_requests) == 2
    for request in deepseek_requests:
        assert request["response_format"] == {"type": "json_object"}
        system = request["messages"][0]["content"]
        assert "Canonical JSON schema:" in system
        assert "Canonical schema SHA-256:" in system
    assert BUILD_RECEIPT["coverage_schema_sha256"] in deepseek_requests[0]["messages"][0]["content"]
    summary["cases"]["deepseek_valid"] = {"requests": 2, "schema_in_prompt": True}

    mismatch, _events, mismatch_requests = run_cli_case(
        "deepseek_key_mismatch", "deepseek", "deepseek-v4-pro"
    )
    assert len(mismatch_requests) == 1
    mismatch_runtime = mismatch["runtime"]
    assert mismatch_runtime["machine_status"] == "HOLD"
    diagnostic = mismatch_runtime["machine_receipt"]["bounded_contract_failure"]
    assert diagnostic["failed_path"] == "$"
    assert diagnostic["extra_keys"] == ["unexpected"]
    assert diagnostic["schema_sha256"] == BUILD_RECEIPT["coverage_schema_sha256"]
    summary["cases"]["deepseek_key_mismatch"] = {"requests": 1, "extra_keys": ["unexpected"]}

    machine_gui, machine_gui_requests = run_gui_case(
        "machine_hold_gui", "gemini", "gemini-3.7-flash", interpretation=False
    )
    assert len(machine_gui_requests) == 1
    assert machine_gui["phase"] == "completed_with_machine_hold"
    assert machine_gui["result"]["runtime"]["presentation_status"] == "NOT_STARTED"
    assert "机器裁决未形成" in machine_gui["message"]
    summary["cases"]["machine_hold_gui"] = {"requests": 1, "phase": machine_gui["phase"]}

    presentation_gui, presentation_gui_requests = run_gui_case(
        "presentation_hold_gui", "gemini", "gemini-3.6-flash", interpretation=False
    )
    assert len(presentation_gui_requests) == 3
    assert presentation_gui["phase"] == "completed_with_presentation_hold"
    assert presentation_gui["result"]["runtime"]["machine_status"] == "SUCCEEDED"
    assert presentation_gui["result"]["runtime"]["presentation_status"] == "HOLD"
    assert "机器裁决已完成" in presentation_gui["message"]
    summary["cases"]["presentation_hold_gui"] = {
        "requests": 3,
        "phase": presentation_gui["phase"],
    }

    canceled_gui, canceled_gui_requests = run_gui_case(
        "consent_canceled_gui",
        "gemini",
        "gemini-3.7-flash",
        interpretation=False,
        consent_confirmed=False,
    )
    assert canceled_gui_requests == []
    assert canceled_gui["phase"] == "canceled"
    assert canceled_gui["result"]["runtime"]["api_called"] is False
    assert (
        canceled_gui["result"]["minimal_receipt"]["reason_category"]
        == "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED"
    )
    summary["cases"]["consent_canceled_gui"] = {"requests": 0, "phase": "canceled"}

    basis_gui, basis_requests = run_gui_case(
        "basis_insufficient_gui",
        "gemini",
        "gemini-3.7-flash",
        interpretation=False,
    )
    assert basis_gui["phase"] == "completed_with_machine_hold"
    basis_insufficient = basis_gui["result"]
    basis_runtime = basis_insufficient["runtime"]
    assert len(basis_requests) == 1
    assert basis_runtime["physical_request_attempt_count"] == 1
    assert basis_runtime["machine_status"] == "NOT_FORMED"
    assert basis_runtime["presentation_status"] == "NOT_STARTED"
    basis_machine_receipt = basis_runtime["machine_receipt"]
    assert basis_machine_receipt["status"] == "NOT_FORMED"
    assert_not_formed_value(basis_machine_receipt, "authoritative_machine_verdict")
    assert_not_formed_value(basis_machine_receipt, "deterministic_verdict")
    assert_not_formed_value(basis_machine_receipt, "authoritative_presentation_source")
    assert basis_machine_receipt["whole_manuscript_basis"] == "INSUFFICIENT"
    assert (
        basis_machine_receipt["reason_category"]
        == "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS"
    )
    assert basis_insufficient["minimal_receipt"]["whole_manuscript_basis"] == "INSUFFICIENT"
    assert (
        basis_insufficient["minimal_receipt"]["reason_category"]
        == "INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS"
    )
    assert [MockProvider._stage(item) for item in basis_requests] == ["coverage"]
    assert len(basis_runtime["provider_receipts"]) == 1
    assert basis_runtime["provider_receipts"][0]["stage"] == "coverage"
    basis_physical = basis_runtime["physical_request_receipts"]
    assert len(basis_physical) == 1
    assert basis_physical[0]["stage"] == "coverage"
    assert basis_physical[0]["usage_status"] == "COMPLETE"
    assert basis_insufficient["task_cost"]["physical_request_attempt_count"] == 1
    assert basis_insufficient["task_cost"]["usage_receipt_count"] == 1
    assert basis_insufficient["task_cost"]["unknown_potential_charge_attempt_count"] == 0
    summary["cases"]["basis_insufficient"] = {
        "coverage_requests": 1,
        "adjudication_requests": 0,
        "machine_status": "NOT_FORMED",
    }

    format_variants = {
        "h1_atx": "# Synthetic title\n\n## Abstract\n\nBody evidence.\n\n## Conclusion\nBounded.\n",
        "setext": "Synthetic title\n===============\n\nAbstract\n--------\nBody evidence.\n\nConclusion\n----------\nBounded.\n",
        "roman": "I. Introduction\n\nBody evidence.\n\nVI. Conclusion\nBounded.\n",
        "s_numbered": "S1 Introduction\n\nBody evidence.\n\nS6 Conclusion\nBounded.\n",
        "chinese": "第一章 引言\n\n实质材料。\n\n第六章 结论\n有限结论。\n",
        "front_matter": "---\nauthor: Synthetic\n---\n\nAbstract\n\nBody evidence.\n",
        "titleless": "Abstract\n\nBody evidence without a conventional title.\n",
    }
    for label, text in format_variants.items():
        variant, _events, requests = run_cli_case(
            f"basis_insufficient_format_{label}",
            "gemini",
            "gemini-3.7-flash",
            manuscript_text=text,
        )
        assert len(requests) == 1, label
        assert [MockProvider._stage(item) for item in requests] == ["coverage"], label
        assert variant["runtime"]["harness"]["intake"]["local_preflight_passed"] is True
        assert variant["minimal_receipt"]["whole_manuscript_basis"] == "INSUFFICIENT"
    summary["cases"]["format_invariant_coverage_routing"] = {
        "variants": len(format_variants),
        "coverage_requests_each": 1,
    }

    s_series = (
        "# Synthetic S Manuscript\n\n## Abstract\n\n"
        + ("Bounded structure.\n" * 100)
        + "\n## S1. Introduction\nText.\n## S2. Methods\nText.\n"
        "### Unnumbered child\nText.\n## S3. Findings\nText.\n"
        "## S4. Discussion\nText.\n## S5. Limits\nText.\n"
        "## S6. Conclusion\nText.\n## References\nRef.\n"
    )
    s_intake = run_intake_only_case("s-series", s_series)
    s_receipt = s_intake["runtime"]["harness"]["intake"]
    assert s_receipt["complete_structure"] is True
    assert s_receipt["advisory_codes"] == []
    summary["cases"]["intake_s_series"] = {"api_calls": 0, "complete": True}

    unnumbered = (
        "# Synthetic Unnumbered Manuscript\n\n## Abstract\n\n"
        + ("Bounded structure.\n" * 100)
        + "\n## Introduction\nText.\n## Methods\nText.\n"
        "## Conclusion\nText.\n## References\nRef.\n"
    )
    unnumbered_intake = run_intake_only_case("unnumbered", unnumbered)
    unnumbered_receipt = unnumbered_intake["runtime"]["harness"]["intake"]
    assert unnumbered_receipt["complete_structure"] is True
    assert unnumbered_receipt["advisory_codes"] == ["HEADING_NUMBERING_STYLE_REVIEW"]
    assert unnumbered_receipt["advisories"][0]["blocking"] is False
    summary["cases"]["intake_unnumbered_advisory"] = {"api_calls": 0, "complete": True}

    missing_conclusion = unnumbered.replace("## Conclusion\nText.\n", "")
    missing_intake = run_intake_only_case("missing-conclusion", missing_conclusion)
    assert missing_intake["runtime"]["harness"]["intake"]["local_preflight_passed"] is True
    assert (
        missing_intake["minimal_receipt"]["reason_category"]
        == "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED"
    )
    summary["cases"]["intake_missing_conclusion"] = {"api_calls": 0, "preflight": True}

    missing_title = unnumbered.replace("# Synthetic Unnumbered Manuscript\n\n", "", 1)
    missing_title_intake = run_intake_only_case("missing-title", missing_title)
    missing_title_receipt = missing_title_intake["runtime"]["harness"]["intake"]
    assert missing_title_receipt["title_present"] is False
    assert missing_title_receipt["local_preflight_passed"] is True
    assert (
        missing_title_intake["minimal_receipt"]["reason_category"]
        == "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED"
    )
    summary["cases"]["intake_missing_title"] = {"api_calls": 0, "preflight": True}

    yaml_missing_title = (
        "\ufeff---\nauthor: Synthetic Author\ndate: 2026-08-28\nkeywords: synthetic\n---\n\n"
        + missing_title
    )
    yaml_missing_title_intake = run_intake_only_case(
        "yaml-front-matter-missing-title", yaml_missing_title
    )
    yaml_missing_title_receipt = yaml_missing_title_intake["runtime"]["harness"]["intake"]
    assert yaml_missing_title_receipt["contract_version"] == "mrc-local-technical-preflight-1.0"
    assert yaml_missing_title_receipt["title_present"] is False
    assert yaml_missing_title_receipt["local_preflight_passed"] is True
    assert yaml_missing_title_intake["runtime"]["api_called"] is False
    assert (
        yaml_missing_title_intake["minimal_receipt"]["reason_category"]
        == "USER_PROVIDER_TRANSMISSION_NOT_AUTHORIZED"
    )
    summary["cases"]["intake_yaml_front_matter_missing_title"] = {
        "api_calls": 0,
        "preflight": True,
    }

    summary.update(
        {
            "status": "PASS_FROZEN_MRC_0_6_4_MOCK_ACCEPTANCE",
            "case_count": len(summary["cases"]),
            "real_api_calls": 0,
            "real_manuscripts_read": 0,
            "secret_values_persisted": 0,
            "raw_responses_persisted": 0,
        }
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
