"""Localhost-only browser GUI for the standalone executable."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .assessor import RunOptions, analyze_manuscript
from .cli import load_prior_receipt
from .events import EventSink
from .interpretation import (
    InterpretationContractError,
    generate_interpretation,
    render_interpretation_markdown,
)
from .native_dialogs import (
    hide_console_window,
    pick_interpretation_destination,
    pick_manuscript,
    pick_prior_receipt,
    pick_result_destination,
)
from .pricing import calculate_task_cost, exchange_rate_or_none, price_with_fallback
from .providers import (
    PROVIDERS,
    ProviderRequestError,
    list_provider_models,
    provider_stage_timeout_seconds,
    reasoning_profile,
    validate_reasoning_option,
)


APP_TITLE = "Manuscript Revision Closure"

PHASE_MESSAGES = {
    "created": "建立本地任务",
    "reading": "读取并核对不可变稿件",
    "requesting_model": "等待模型 API 返回受限分类",
    "validating": "执行本地确定性合同校验",
    "presenting": "执行受限公开展示校验或修复",
    "completed": "核心裁决事务完成",
    "failed": "运行失败",
}
ITEM_MESSAGES = {
    "document_read": "读取稿件并检查完整结构",
    "intake_gate": "执行稿件 intake 门",
    "coverage_request": "执行整稿十维覆盖 pass",
    "adjudication_request": "执行独立 root-cause adjudication pass",
    "contradiction_gate": "执行跨阶段矛盾与哈希绑定门",
    "presentation_repair": "执行一次受限公开文本修复",
    "receipt_reuse": "核验并复用既有最小收据",
}


def _provider_public_status() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, spec in PROVIDERS.items():
        detected = next((variable for variable in spec.key_variables if os.environ.get(variable, "").strip()), None)
        result[name] = {
            "default_model": os.environ.get(spec.model_variable, spec.default_model),
            "key_present": detected is not None,
            "key_variable": detected or spec.key_variables[0],
            "models": list(spec.default_models),
        }
    return result


@dataclass(slots=True)
class GuiState:
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    busy: bool = False
    phase: str = "ready"
    message: str = "就绪 / Ready"
    result: dict[str, Any] | None = None
    error: str | None = None
    interpretation_error: str | None = None
    presentation_error: str | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    selected_manuscript: str = ""
    selected_prior_receipt: str = ""
    timeline: list[dict[str, Any]] = field(default_factory=list)
    _started_at: float | None = None
    _seen_terminal_keys: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "busy": self.busy,
                "phase": self.phase,
                "message": self.message,
                "elapsed_seconds": round(time.monotonic() - self._started_at, 1) if self._started_at else 0.0,
                "result": deepcopy(self.result),
                "error": self.error,
                "interpretation_error": self.interpretation_error,
                "presentation_error": self.presentation_error,
                "request_id": self.request_id,
                "timeline": deepcopy(self.timeline),
                "selected_manuscript": self.selected_manuscript,
                "selected_prior_receipt": self.selected_prior_receipt,
                "providers": _provider_public_status(),
            }

    def select_path(self, kind: str, path: Path) -> None:
        with self._lock:
            if kind == "manuscript":
                self.selected_manuscript = str(path)
            elif kind == "prior_receipt":
                self.selected_prior_receipt = str(path)
            else:
                raise ValueError("unknown GUI path kind")

    def start(self) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            self.phase = "starting"
            self.message = "正在启动只读判断 / Starting read-only assessment"
            self.result = None
            self.error = None
            self.interpretation_error = None
            self.presentation_error = None
            self.request_id = str(uuid.uuid4())
            self._seen_terminal_keys.clear()
            self.timeline = []
            self._started_at = time.monotonic()
            self._append_locked("starting", "已接收请求，开始只读处理", {})
            return True

    def _append_locked(self, phase: str, message: str, details: dict[str, Any]) -> None:
        self.timeline.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "elapsed_seconds": round(time.monotonic() - self._started_at, 1) if self._started_at else 0.0,
                "phase": phase,
                "message": message,
                "details": details,
            }
        )
        if len(self.timeline) > 100:
            self.timeline = self.timeline[-100:]

    def add_status(self, phase: str, message: str, **details: Any) -> None:
        with self._lock:
            self.phase = phase
            self.message = message
            self._append_locked(phase, message, details)

    def on_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            event_type = str(event.get("type", "running"))
            request_id = str(event.get("request_id") or self.request_id)
            terminal_event_id = str(event.get("terminal_event_id") or "")
            if event_type in {"turn.completed", "turn.failed"}:
                terminal_key = f"{request_id}:{terminal_event_id}"
                if terminal_key in self._seen_terminal_keys:
                    return
                self._seen_terminal_keys.add(terminal_key)

            event_phase = str(event.get("phase") or event_type or "running")
            item = event.get("item")
            details: dict[str, Any] = {}
            if isinstance(item, dict):
                self.phase = event_phase
                item_type = str(item.get("type", ""))
                base = ITEM_MESSAGES.get(item_type, str(item.get("label") or item_type or event_type))
                suffix = "开始" if event_type == "item.started" else "完成"
                message = f"{base}：{suffix}"
                for key in (
                    "provider", "model", "reasoning_option", "attempts", "usage",
                    "character_count", "verdict", "status", "complete_structure",
                    "heading_count", "coverage_complete", "dimension_count",
                    "coverage_binding", "contradiction_gate_passed", "timeout_seconds",
                    "provider_outcome", "machine_state_parity",
                ):
                    if key in item:
                        details[key] = item[key]
            elif event_type == "phase.changed":
                self.phase = event_phase
                message = PHASE_MESSAGES.get(self.phase, self.phase)
            elif event_type == "provider.attempt":
                self.phase = event_phase
                stage_labels = {
                    "coverage": "整稿覆盖",
                    "adjudication": "根因裁决",
                    "presentation_repair": "公开文本修复",
                }
                stage = str(event.get("stage") or "model")
                message = (
                    f"已向 {event.get('provider', 'provider')} API 发出"
                    f"{stage_labels.get(stage, stage)}第 {event.get('attempt', 1)} 次请求"
                )
                details = {
                    "stage": stage,
                    "provider": event.get("provider"),
                    "model": event.get("model"),
                    "reasoning_option": event.get("reasoning_option"),
                    "attempt": event.get("attempt"),
                    "timeout_seconds": event.get("timeout_seconds"),
                    "max_transient_retries": event.get("max_transient_retries"),
                }
            elif event_type == "turn.completed":
                self.phase = "core_completed"
                message = "核心裁决与公开展示事务已形成唯一终态"
                details = {
                    key: event[key]
                    for key in (
                        "verdict", "usage", "machine_status", "presentation_status",
                        "terminal_status", "recoverability",
                    )
                    if key in event
                }
                details["terminal_event_id"] = terminal_event_id
                details["request_id"] = request_id
            elif event_type == "turn.failed":
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                public_error = str(error.get("message") or "运行失败 / Run failed")
                self.busy = False
                self.phase = "failed"
                self.error = public_error
                self.result = None
                message = "运行失败 / Run failed"
                details = {
                    "error": public_error,
                    "error_code": error.get("code"),
                    "terminal_event_id": terminal_event_id,
                    "request_id": request_id,
                }
            else:
                self.phase = event_phase
                message = PHASE_MESSAGES.get(self.phase, event_type)
            self.message = message
            self._append_locked(self.phase, message, details)

    def core_ready(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.result = deepcopy(result)
            self._append_locked(
                "core_completed",
                "核心 Closure Card 已生成，正在处理后续公开交付状态",
                {"verdict": result.get("closure_card", {}).get("Verdict")},
            )

    def attach_result_field(self, key: str, value: Any) -> None:
        with self._lock:
            if self.result is None:
                raise RuntimeError("core result is unavailable")
            self.result[key] = deepcopy(value)

    def complete(self, interpretation: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self.result is not None and interpretation is not None:
                self.result["interpretation"] = interpretation
            self.busy = False
            self.phase = "completed"
            self.message = "核心判断与中文解读均已完成" if interpretation else "核心判断已完成"
            self.error = None
            self.presentation_error = None
            self._append_locked(self.phase, self.message, {})

    def presentation_hold(self, message: str) -> None:
        with self._lock:
            self.busy = False
            self.phase = "completed_with_presentation_hold"
            self.message = "机器裁决已完成；公开展示处于可恢复 HOLD"
            self.error = None
            self.presentation_error = message
            self._append_locked(
                self.phase,
                self.message,
                {"presentation_hold": True, "error": message},
            )

    def interpretation_fail(self, message: str) -> None:
        with self._lock:
            self.busy = False
            self.phase = "completed_with_interpretation_hold"
            self.message = "核心判断已完成；中文解读生成失败"
            self.interpretation_error = message
            self._append_locked(self.phase, self.message, {"error": message})

    def fail(self, message: str) -> None:
        with self._lock:
            self.busy = False
            self.phase = "failed"
            self.message = "运行失败 / Run failed"
            self.result = None
            self.error = message
            self._append_locked(self.phase, self.message, {"error": message})


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: GuiState) -> None:
        self.gui_state = state
        super().__init__(address, GuiRequestHandler)


class GuiRequestHandler(BaseHTTPRequestHandler):
    server: GuiServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected_port = self.server.server_address[1]
        host = self.headers.get("Host", "").casefold()
        allowed_hosts = {f"127.0.0.1:{expected_port}", f"localhost:{expected_port}"}
        if host not in allowed_hosts:
            return False
        origin = self.headers.get("Origin", "")
        if origin and origin.casefold() not in {f"http://{item}" for item in allowed_hosts}:
            return False
        header_token = self.headers.get("X-MRC-Token", "")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        query_token = query.get("token", [""])[0]
        supplied = header_token or query_token
        return bool(supplied) and secrets.compare_digest(supplied, self.server.gui_state.token)

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("application/json"):
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > 64 * 1024:
            raise ValueError("request body exceeds 64 KiB")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be one JSON object")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self._security_headers("image/x-icon")
            self.end_headers()
            return
        if path == "/":
            data = render_html(self.server.gui_state.token).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self._security_headers("text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/status":
            self._json(HTTPStatus.OK, self.server.gui_state.snapshot())
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/pick-manuscript":
                selected = pick_manuscript()
                if selected is not None:
                    self.server.gui_state.select_path("manuscript", selected)
                self._json(HTTPStatus.OK, {"path": str(selected) if selected else ""})
                return
            if path == "/api/pick-prior":
                selected = pick_prior_receipt()
                if selected is not None:
                    self.server.gui_state.select_path("prior_receipt", selected)
                self._json(HTTPStatus.OK, {"path": str(selected) if selected else ""})
                return
            if path == "/api/models":
                payload = self._read_json()
                if set(payload) != {"provider"} or payload.get("provider") not in PROVIDERS:
                    raise ValueError("model request requires one known provider")
                provider_name = str(payload["provider"])
                try:
                    models = list_provider_models(provider_name)
                    self._json(HTTPStatus.OK, {"models": models, "source": "live_provider_api", "warning": ""})
                except (ValueError, RuntimeError) as exc:
                    self._json(
                        HTTPStatus.OK,
                        {
                            "models": list(PROVIDERS[provider_name].default_models),
                            "source": "bundled_fallback",
                            "warning": str(exc),
                        },
                    )
                return
            if path == "/api/reasoning-options":
                payload = self._read_json()
                if set(payload) != {"provider", "model"} or payload.get("provider") not in PROVIDERS:
                    raise ValueError("reasoning request requires one known provider and model")
                model = payload.get("model")
                if not isinstance(model, str) or not model.strip() or len(model) > 120:
                    raise ValueError("reasoning request model must be concise text")
                self._json(HTTPStatus.OK, reasoning_profile(str(payload["provider"]), model.strip()))
                return
            if path == "/api/analyze":
                payload = self._read_json()
                if not self.server.gui_state.start():
                    self._json(HTTPStatus.CONFLICT, {"error": "an analysis is already running"})
                    return
                threading.Thread(
                    target=_analysis_worker,
                    args=(self.server.gui_state, payload),
                    daemon=True,
                ).start()
                self._json(HTTPStatus.ACCEPTED, {"accepted": True})
                return
            if path == "/api/save":
                snapshot = self.server.gui_state.snapshot()
                if snapshot["result"] is None:
                    self._json(HTTPStatus.CONFLICT, {"error": "no completed result to save"})
                    return
                destination = pick_result_destination()
                if destination is None:
                    self._json(HTTPStatus.OK, {"saved": False, "path": ""})
                    return
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    json.dumps(snapshot["result"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                self._json(HTTPStatus.OK, {"saved": True, "path": str(destination)})
                return
            if path == "/api/save-interpretation":
                snapshot = self.server.gui_state.snapshot()
                interpretation = (snapshot.get("result") or {}).get("interpretation")
                document = interpretation.get("document") if isinstance(interpretation, dict) else None
                if not isinstance(document, dict):
                    self._json(HTTPStatus.CONFLICT, {"error": "no completed interpretation to save"})
                    return
                destination = pick_interpretation_destination()
                if destination is None:
                    self._json(HTTPStatus.OK, {"saved": False, "path": ""})
                    return
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    render_interpretation_markdown(document, (snapshot.get("result") or {}).get("task_cost")),
                    encoding="utf-8",
                    newline="\n",
                )
                self._json(HTTPStatus.OK, {"saved": True, "path": str(destination)})
                return
            if path == "/api/close":
                self._json(HTTPStatus.OK, {"closing": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def _analysis_worker(state: GuiState, payload: dict[str, Any]) -> None:
    sink = EventSink(callback=state.on_event, request_id=state.request_id)
    sink.start()
    try:
        allowed = {
            "manuscript_path", "provider", "model", "reasoning_option", "language",
            "identity", "confirmed_complete", "prior_receipt_path", "generate_interpretation",
        }
        if set(payload).difference(allowed):
            raise ValueError("analysis request contains unknown fields")
        manuscript = payload.get("manuscript_path")
        if not isinstance(manuscript, str) or not manuscript.strip():
            raise ValueError("请选择稿件文件 / Select a manuscript file")
        provider = payload.get("provider", "deepseek")
        if provider not in PROVIDERS:
            raise ValueError("provider must be deepseek, kimi, or gemini")
        model = payload.get("model")
        if model is not None and (not isinstance(model, str) or len(model) > 120):
            raise ValueError("model must be concise text")
        language = payload.get("language", "zh")
        if language not in {"zh", "en"}:
            raise ValueError("language must be zh or en")
        identity = payload.get("identity")
        if identity is not None and (not isinstance(identity, str) or len(identity) > 240):
            raise ValueError("identity must be concise text")
        confirmed = payload.get("confirmed_complete", False)
        if not isinstance(confirmed, bool):
            raise ValueError("confirmed_complete must be boolean")
        interpret = payload.get("generate_interpretation", True)
        if not isinstance(interpret, bool):
            raise ValueError("generate_interpretation must be boolean")
        prior_path = payload.get("prior_receipt_path")
        if prior_path is not None and not isinstance(prior_path, str):
            raise ValueError("prior_receipt_path must be text")
        prior = load_prior_receipt(prior_path.strip() or None) if prior_path else None
        selected_identity = identity.strip() if isinstance(identity, str) and identity.strip() else None
        selected_model = model.strip() if isinstance(model, str) and model.strip() else None
        selected_model_for_contract = selected_model or PROVIDERS[provider].default_model
        reasoning = payload.get("reasoning_option", "default")
        if not isinstance(reasoning, str) or len(reasoning) > 20:
            raise ValueError("reasoning_option must be concise text")
        selected_reasoning = validate_reasoning_option(provider, selected_model_for_contract, reasoning)
        result = analyze_manuscript(
            RunOptions(
                manuscript_path=Path(manuscript), provider=provider, model=selected_model,
                reasoning_option=selected_reasoning, output_language=language,
                manuscript_identity=selected_identity,
                confirm_complete_current_manuscript=confirmed, prior_receipt=prior,
            ),
            event_sink=sink,
        )
        public_result = result.as_dict()
        state.core_ready(public_result)
    except Exception as exc:
        if not sink.terminal_emitted:
            sink.fail(type(exc).__name__, str(exc))
        return

    core_runtime = public_result.get("runtime", {})
    if core_runtime.get("terminal_status") == "HOLD" or core_runtime.get("presentation_status") == "HOLD":
        _attach_task_cost(state, public_result, provider, selected_model)
        presentation_receipt = core_runtime.get("presentation_receipt", {})
        error_code = presentation_receipt.get("error_code") if isinstance(presentation_receipt, dict) else None
        message = str(
            (presentation_receipt.get("error_message") if isinstance(presentation_receipt, dict) else None)
            or error_code or "公开展示合同未通过"
        )
        state.presentation_hold(message)
        return

    verdict = public_result.get("closure_card", {}).get("Verdict")
    if not interpret or not confirmed or verdict == "UNASSESSED":
        _attach_task_cost(state, public_result, provider, selected_model)
        state.complete()
        return
    interpretation_timeout = provider_stage_timeout_seconds(provider, "interpretation")
    state.add_status(
        "interpreting", "正在调用同一模型生成受约束的中文解读（额外一次 API 调用）",
        provider=provider, model=selected_model or PROVIDERS[provider].default_model,
        reasoning_option=selected_reasoning, timeout_seconds=interpretation_timeout,
    )

    def on_interpretation_attempt(number: int) -> None:
        state.add_status(
            "interpreting",
            f"中文解读 API 第 {number} 次请求已发出，单次等待上限 {interpretation_timeout:g} 秒",
            provider=provider, attempt=number, reasoning_option=selected_reasoning,
            timeout_seconds=interpretation_timeout,
        )

    try:
        interpretation = generate_interpretation(
            Path(manuscript), expected_artifact_sha256=result.artifact_sha256,
            manuscript_identity=selected_identity or Path(manuscript).name,
            public_result=public_result, provider=provider, model=selected_model,
            reasoning_option=selected_reasoning, on_attempt=on_interpretation_attempt,
        )
        state.add_status(
            "interpretation_validated", "中文解读已返回并通过十一键合同校验",
            provider=interpretation.provider, model=interpretation.model,
            reasoning_option=interpretation.reasoning_option, attempts=interpretation.attempts,
            usage=interpretation.usage,
        )
        public_result["interpretation"] = interpretation.as_dict()
        state.attach_result_field("interpretation", public_result["interpretation"])
        _attach_task_cost(state, public_result, provider, selected_model)
        state.complete()
    except Exception as exc:
        public_error = str(exc)
        if isinstance(exc, InterpretationContractError) and exc.runtime:
            public_result["failed_interpretation_runtime"] = exc.runtime
            state.attach_result_field("failed_interpretation_runtime", exc.runtime)
            if public_error == "interpretation is not one JSON object":
                public_error = "模型未按要求返回单一 JSON 解读对象"
        _attach_task_cost(state, public_result, provider, selected_model)
        state.interpretation_fail(public_error)


def _attach_task_cost(
    state: GuiState,
    public_result: dict[str, Any],
    provider: str,
    selected_model: str | None,
) -> None:
    usages: list[dict[str, int]] = []
    core_runtime = public_result.get("runtime", {})
    core_calls = core_runtime.get("usage_calls")
    if core_runtime.get("api_called") and isinstance(core_calls, list):
        usages.extend(item for item in core_calls if isinstance(item, dict))
    elif core_runtime.get("api_called") and isinstance(core_runtime.get("usage"), dict):
        usages.append(core_runtime["usage"])
    interpretation = public_result.get("interpretation")
    if isinstance(interpretation, dict):
        interpretation_runtime = interpretation.get("runtime", {})
        if isinstance(interpretation_runtime, dict) and isinstance(interpretation_runtime.get("usage"), dict):
            usages.append(interpretation_runtime["usage"])
    failed_interpretation = public_result.get("failed_interpretation_runtime")
    if isinstance(failed_interpretation, dict) and isinstance(failed_interpretation.get("usage"), dict):
        usages.append(failed_interpretation["usage"])
    if not usages:
        public_result["task_cost"] = {
            "status": "no_api_calls",
            "pricing": None,
            "calls": [],
            "total_estimated_cost": 0.0,
            "total_estimated_cost_usd": 0.0,
            "total_estimated_cost_cny": 0.0,
            "currency": "USD",
            "exchange_rate": None,
            "billing_limitations": ["本次没有调用模型 API，因此 token 计价为 0。"],
        }
        state.attach_result_field("task_cost", public_result["task_cost"])
        return
    actual_model = str(
        core_runtime.get("model")
        or (interpretation.get("runtime", {}).get("model") if isinstance(interpretation, dict) else "")
        or selected_model
        or PROVIDERS[provider].default_model
    )
    state.add_status("pricing", "正在刷新官方定价并按实际 token usage 估算本次费用", provider=provider, model=actual_model)
    try:
        quote = price_with_fallback(provider, actual_model)
    except (OSError, ValueError, RuntimeError):
        quote = None
    exchange_rate = exchange_rate_or_none() if quote is not None else None
    public_result["task_cost"] = calculate_task_cost(quote, usages, exchange_rate)
    state.attach_result_field("task_cost", public_result["task_cost"])
    pricing_status = public_result["task_cost"].get("pricing", {}) or {}
    state.add_status(
        "pricing_completed",
        "任务计价完成" if quote is not None else "未找到该模型的可验证价格，费用保持未估算",
        provider=provider,
        model=actual_model,
        source_status=pricing_status.get("source_status"),
        total_estimated_cost=public_result["task_cost"].get("total_estimated_cost"),
        total_estimated_cost_usd=public_result["task_cost"].get("total_estimated_cost_usd"),
        total_estimated_cost_cny=public_result["task_cost"].get("total_estimated_cost_cny"),
        currency=public_result["task_cost"].get("currency"),
    )


def create_gui_server() -> tuple[GuiServer, GuiState, str]:
    state = GuiState()
    server = GuiServer(("127.0.0.1", 0), state)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={urllib.parse.quote(state.token)}"
    return server, state, url


def run_web_gui(*, open_browser: bool = True, hide_console: bool = True) -> int:
    server, _state, url = create_gui_server()
    if open_browser:
        opened = webbrowser.open(url, new=1, autoraise=True)
        if opened and hide_console:
            hide_console_window()
        elif not opened:
            print("Open this local GUI URL in your browser:", url, flush=True)
    else:
        print("Local GUI URL:", url, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


def render_html(token: str) -> str:
    token_json = json.dumps(token)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_TITLE}</title>
<style>
:root{{--bg:#f4f6fb;--panel:#fff;--ink:#182033;--muted:#667085;--line:#d9dfeb;--brand:#3157d5;--brand2:#2443a8;--ok:#137a50;--bad:#b42318;--shadow:0 16px 50px rgba(27,39,78,.12)}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(135deg,#eef2ff 0,#f8fafc 45%,#eef9f4 100%);color:var(--ink);font-family:"Segoe UI","Microsoft YaHei",sans-serif;min-height:100vh}}
.wrap{{max-width:1080px;margin:0 auto;padding:34px 22px 60px}}header{{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:22px}}
h1{{font-size:30px;margin:0 0 7px;letter-spacing:-.4px}}.subtitle{{color:var(--muted);line-height:1.6}}.badge{{padding:8px 12px;border-radius:999px;background:#e9edff;color:#2949b7;font-size:13px;font-weight:650;white-space:nowrap}}
.panel{{background:rgba(255,255,255,.94);border:1px solid rgba(217,223,235,.9);border-radius:18px;box-shadow:var(--shadow);padding:24px;margin-bottom:18px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.full{{grid-column:1/-1}}label{{display:block;font-weight:650;margin-bottom:7px;font-size:14px}}.hint{{font-weight:400;color:var(--muted);font-size:12px;margin-left:6px}}
input,select{{width:100%;height:42px;border:1px solid var(--line);border-radius:10px;padding:0 12px;background:#fff;color:var(--ink);font-size:14px;outline:none}}input:focus,select:focus{{border-color:#7790e9;box-shadow:0 0 0 3px rgba(49,87,213,.11)}}
.pathrow{{display:grid;grid-template-columns:1fr auto;gap:9px}}button{{border:0;border-radius:10px;padding:11px 16px;font-weight:650;cursor:pointer;background:#eef1f7;color:#253047}}button:hover{{filter:brightness(.97)}}button.primary{{background:var(--brand);color:#fff}}button.primary:hover{{background:var(--brand2)}}button:disabled{{opacity:.5;cursor:not-allowed}}
.check{{display:flex;gap:10px;align-items:flex-start;padding:13px;border:1px solid var(--line);border-radius:11px;background:#fafbfe}}.check input{{width:18px;height:18px;margin-top:1px}}.check label{{margin:0;font-weight:500;line-height:1.5}}
.explain{{margin-top:9px;border-left:3px solid #91a4eb;padding:9px 12px;background:#f7f8ff;border-radius:0 9px 9px 0;color:#475467;font-size:13px;line-height:1.65}}.explain summary{{cursor:pointer;font-weight:650;color:#344054}}
.key{{font-size:12px;margin-top:6px}}.key.ok{{color:var(--ok)}}.key.bad{{color:var(--bad)}}.actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}}
.status{{display:flex;align-items:center;gap:11px;padding:14px 16px;border-radius:12px;background:#f5f7fb;border:1px solid var(--line)}}.statushead{{display:flex;justify-content:space-between;gap:12px;align-items:center;width:100%}}.elapsed{{font-variant-numeric:tabular-nums;color:#475467;font-size:13px}}.dot{{width:10px;height:10px;border-radius:50%;background:#98a2b3;flex:0 0 auto}}.dot.busy{{background:#3157d5;box-shadow:0 0 0 5px rgba(49,87,213,.12);animation:pulse 1.2s infinite}}.dot.ok{{background:var(--ok)}}.dot.bad{{background:var(--bad)}}@keyframes pulse{{50%{{opacity:.45}}}}
.timeline{{list-style:none;margin:14px 0 0;padding:0;max-height:300px;overflow:auto;border-top:1px solid var(--line)}}.timeline li{{display:grid;grid-template-columns:70px 1fr;gap:12px;padding:10px 3px;border-bottom:1px solid #edf0f5;font-size:13px;line-height:1.45}}.timeline .time{{color:#667085;font-variant-numeric:tabular-nums}}.timeline .detail{{color:#667085;font-size:12px;margin-top:3px;word-break:break-word}}.statusnote{{margin-top:10px;color:#667085;font-size:12px}}
.result{{display:none}}.verdict{{font-size:23px;font-weight:750;margin-bottom:8px}}.reason{{color:#344054;line-height:1.65}}.cols{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}}.box{{background:#f8fafc;border:1px solid var(--line);border-radius:11px;padding:14px}}.box h3{{font-size:14px;margin:0 0 8px}}.box ul{{margin:0;padding-left:20px;line-height:1.65}}pre{{white-space:pre-wrap;word-break:break-word;max-height:380px;overflow:auto;background:#101828;color:#e5e7eb;border-radius:12px;padding:16px;font:12px/1.55 Consolas,monospace}}
.interpretation{{display:none;margin-top:18px;border-top:1px solid var(--line);padding-top:20px}}.interpretation h2{{font-size:21px;margin:0 0 8px}}.finding{{padding:12px 14px;border:1px solid var(--line);border-radius:10px;margin-top:9px;background:#fff}}.finding b{{display:block;margin-bottom:5px}}.finding .why{{color:#667085;margin-top:5px;font-size:13px}}.interp-error{{display:none;color:#9a6700;background:#fff8dd;border:1px solid #ead58f;border-radius:10px;padding:12px;margin-top:14px}}
.error{{display:none;color:var(--bad);background:#fff1f0;border:1px solid #f4b8b2;border-radius:10px;padding:12px;margin-top:12px}}footer{{color:var(--muted);font-size:12px;text-align:center;margin-top:20px;line-height:1.6}}
@media(max-width:760px){{.grid,.cols{{grid-template-columns:1fr}}.full{{grid-column:auto}}header{{display:block}}.badge{{display:inline-block;margin-top:12px}}}}
</style></head><body><div class="wrap">
<header><div><h1>Manuscript Revision Closure</h1><div class="subtitle">只读整稿修订截止判断 · DeepSeek / Kimi / Gemini · Standalone {__version__}</div></div><div class="badge">本地 GUI · 127.0.0.1</div></header>
<section class="panel"><div class="grid">
<div class="full"><label>完整当前稿件 <span class="hint">TXT / Markdown / HTML / DOCX / text-layer PDF</span></label><div class="pathrow"><input id="manuscript" readonly placeholder="请选择稿件文件"><button id="pickManuscript">选择文件…</button></div></div>
<div><label>模型提供商</label><select id="provider"><option value="deepseek">DeepSeek</option><option value="kimi">Kimi</option><option value="gemini">Gemini</option></select><div id="keyStatus" class="key"></div></div>
<div><label>模型 <span class="hint">从当前可用目录选择</span></label><div class="pathrow"><select id="model"></select><button id="refreshModels" type="button">刷新列表</button></div><div id="modelSource" class="key"></div></div>
<div><label>思考设置 <span class="hint">随提供商和模型动态变化</span></label><select id="reasoning"></select><div id="reasoningSource" class="key"></div></div>
<div><label>输出语言</label><select id="language"><option value="zh">中文</option><option value="en">English</option></select></div>
<div><label>稳定稿件身份</label><input id="identity" placeholder="例如 manuscript-v12"></div>
<div class="full"><label>既有最小收据 <span class="hint">可选，仅稳定 STOP receipt 可免两次核心判断 API 调用</span></label><div class="pathrow"><input id="prior" readonly placeholder="首次使用请留空"><button id="pickPrior">选择收据…</button></div><details class="explain"><summary>这是什么？什么时候需要选择？</summary>这是同一稿件上一次判断保存的精简 JSON 凭证，记录稿件身份、文件与语义哈希、裁决、hold codes 和失效条件。只有稿件身份及哈希完全一致、且旧收据仍是合法稳定的 STOP 收据时，核心判断才能复用它而免除整稿覆盖与根因裁决两次核心 API 调用。首次使用或稿件发生实质修改时请留空。若勾选下面的中文解读，解读仍会单独调用一次 API。</details></div>
<div class="full check"><input id="confirmed" type="checkbox"><label for="confirmed">我确认所选文件是身份明确的完整当前稿件。未确认时将直接返回 <b>UNASSESSED</b>，不会调用 API。</label></div>
<div class="full check"><input id="interpret" type="checkbox" checked><label for="interpret">核心裁决完成后，使用同一提供商额外调用一次 API，生成受 <code>standalone/AGENT.md</code> 约束的中文解读文档。</label></div>
</div><div class="actions"><button class="primary" id="run">开始只读判断</button><button id="save" disabled>保存完整公开结果…</button><button id="saveInterpretation" disabled>保存中文解读…</button><button id="copy" disabled>复制 JSON</button><button id="close">关闭本地程序</button></div><div id="error" class="error"></div></section>
<section class="panel"><div class="status"><div id="dot" class="dot"></div><div class="statushead"><div><b id="phase">ready</b><div id="message" class="subtitle">就绪 / Ready</div></div><div id="elapsed" class="elapsed">0.0 秒</div></div></div><ol id="timeline" class="timeline"></ol><div class="statusnote">三家提供商均使用非流式兼容接口；这里实时显示真实阶段、请求尝试、等待用时、token usage、定价刷新与本地合同校验，不显示虚构百分比。</div></section>
<section id="result" class="panel result"><div id="verdict" class="verdict"></div><div id="reason" class="reason"></div><div class="cols"><div class="box"><h3>证据层 hold</h3><ul id="evidence"></ul></div><div class="box"><h3>投稿／外部 hold</h3><ul id="submission"></ul></div></div><div class="cols"><div class="box"><h3>应保护、不应扰动</h3><ul id="protected"></ul></div><div class="box"><h3>核心卡片允许的轻量方向</h3><div id="suggestions" class="reason"></div></div></div><div class="box" style="margin-top:14px"><h3>下一步允许做什么</h3><div id="next" class="reason"></div></div><div id="harnessBox" class="box" style="margin-top:14px;display:none"><h3>Harness 门禁收据</h3><ul id="harnessChecks"></ul></div><div id="costBox" class="box" style="margin-top:14px;display:none"><h3>本次任务计费估算</h3><div id="costTotal" class="verdict" style="font-size:19px"></div><div id="costSource" class="reason"></div><ul id="costCalls"></ul><ul id="costLimits"></ul></div><div id="interpError" class="interp-error"></div><div id="interpretation" class="interpretation"><h2>中文结果解读</h2><div id="statusExplanation" class="reason"></div><div class="cols"><div class="box"><h3>判断依据</h3><ul id="judgmentBasis"></ul></div><div class="box"><h3>判断原则</h3><ul id="judgmentPrinciples"></ul></div></div><div class="box" style="margin-top:14px"><h3>重点考察维度</h3><div id="dimensions"></div></div><div class="cols"><div class="box"><h3>当前稳定且应保护</h3><ul id="stable"></ul></div><div class="box"><h3>仍需注意</h3><ul id="attention"></ul></div></div><div class="box" style="margin-top:14px"><h3>选择性公开观察</h3><div id="findings"></div></div><div class="box" style="margin-top:14px"><h3>投稿前人工核对清单</h3><ul id="checklist"></ul></div><div class="box" style="margin-top:14px"><h3>可选低风险微调</h3><div id="adjustments"></div></div><div class="box" style="margin-top:14px"><h3>报告局限性</h3><ul id="limitations"></ul></div><div class="box" style="margin-top:14px"><h3>使用边界</h3><div id="boundary" class="reason"></div></div></div><details style="margin-top:16px"><summary>完整公开 JSON</summary><pre id="json"></pre></details></section>
<footer>稿件按不可变、不可信输入处理。API key 只从环境变量读取，不会显示或写入结果。页面不加载任何远程脚本或资源。</footer>
</div><script>
const TOKEN={token_json};let providers={{}},poller=null,lastResult=null,lastProvider='';
async function api(path,body){{const opt={{method:body===undefined?'GET':'POST',headers:{{'X-MRC-Token':TOKEN}}}};if(body!==undefined){{opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(body)}}const r=await fetch(path,opt);const data=await r.json();if(!r.ok)throw new Error(data.error||('HTTP '+r.status));return data}}
const $=id=>document.getElementById(id);function showError(message){{$('error').textContent=message;$('error').style.display=message?'block':'none'}}
function populateModels(models,preferred){{const el=$('model'),wanted=preferred||el.value;el.textContent='';[...new Set(models||[])].forEach(model=>{{const option=document.createElement('option');option.value=model;option.textContent=model;el.appendChild(option)}});if([...el.options].some(option=>option.value===wanted))el.value=wanted;else if(el.options.length)el.selectedIndex=0}}
function updateProvider(force=false){{const selected=$('provider').value,p=providers[selected];if(!p)return;const changed=force||lastProvider!==selected;if(changed)populateModels(p.models,p.default_model);lastProvider=selected;$('keyStatus').textContent=p.key_present?('✓ 已检测 '+p.key_variable):('✕ 缺少 '+p.key_variable);$('keyStatus').className='key '+(p.key_present?'ok':'bad');if(!$('modelSource').textContent)$('modelSource').textContent='内置回退目录包含多个兼容模型；可刷新提供商当前目录'}}
async function refreshReasoningOptions(){{const provider=$('provider').value,model=$('model').value;if(!model)return;try{{const r=await api('/api/reasoning-options',{{provider,model}}),el=$('reasoning'),wanted=el.value;el.textContent='';r.options.forEach(item=>{{const option=document.createElement('option');option.value=item.value;option.textContent=item.label;el.appendChild(option)}});if([...el.options].some(option=>option.value===wanted))el.value=wanted;else el.value=r.default;$('reasoningSource').textContent=r.note}}catch(e){{$('reasoning').textContent='';$('reasoningSource').textContent='思考设置读取失败：'+e.message}}}}
async function refreshModelCatalog(){{const provider=$('provider').value,previous=$('model').value;$('modelSource').textContent='正在读取当前模型目录…';try{{const r=await api('/api/models',{{provider}});populateModels(r.models,previous);$('modelSource').textContent=r.source==='live_provider_api'?('✓ 已从提供商 API 刷新 '+r.models.length+' 个兼容模型'):('使用 '+r.models.length+' 个内置回退模型：'+r.warning);await refreshReasoningOptions()}}catch(e){{$('modelSource').textContent='模型目录刷新失败：'+e.message}}}}
function list(id,items){{const el=$(id);el.textContent='';(items&&items.length?items:['None / 无']).forEach(x=>{{const li=document.createElement('li');li.textContent=x;el.appendChild(li)}})}}
function cards(id,items,type){{const el=$(id);el.textContent='';if(!items||!items.length){{el.textContent='无。';return}}items.forEach(item=>{{const box=document.createElement('div');box.className='finding';const title=document.createElement('b');title.textContent=item.area;box.appendChild(title);const main=document.createElement('div');main.textContent=type==='finding'?item.observation:item.suggestion;box.appendChild(main);const why=document.createElement('div');why.className='why';why.textContent=type==='finding'?item.significance:('需保护：'+item.protect);box.appendChild(why);el.appendChild(box)}})}}
function renderSuggestions(items){{const el=$('suggestions');el.textContent='';if(!items||!items.length){{el.textContent='无。';return}}items.forEach(item=>{{const box=document.createElement('div');box.className='finding';const title=document.createElement('b');title.textContent=item.Direction;box.appendChild(title);const why=document.createElement('div');why.textContent=item['Why it matters'];box.appendChild(why);const protect=document.createElement('div');protect.className='why';protect.textContent='需保护：'+item['What to protect'];box.appendChild(protect);el.appendChild(box)}})}}
function renderHarness(runtime){{const h=runtime&&runtime.harness;$('harnessBox').style.display=h?'block':'none';if(!h)return;const intake=h.intake||{{}},items=['Intake 完整结构：'+(intake.complete_structure?'PASS':'HOLD')+'（标题/摘要/结论/参考文献）','整稿覆盖：'+(h.coverage_completed?'PASS':'HOLD')+'；维度 '+(h.coverage_dimension_count||0)+'/10','Adjudication coverage hash 绑定：'+(h.adjudication_coverage_binding?'PASS':'HOLD'),'跨阶段矛盾门：'+(h.contradiction_gate_passed?'PASS':'HOLD')];(h.context_budgets||[]).forEach((b,i)=>items.push('上下文预算 '+(i+1)+'：'+(b.passed?'PASS':'HOLD')+'；估算输入 '+b.estimated_input_tokens+' / 上限 '+b.context_limit_tokens+' tokens'));list('harnessChecks',items)}}
function renderInterpretation(bundle){{const doc=bundle&&bundle.document;$('interpretation').style.display=doc?'block':'none';if(!doc)return;$('statusExplanation').textContent=doc.status_explanation;list('judgmentBasis',doc.judgment_basis);list('judgmentPrinciples',doc.judgment_principles);list('stable',doc.what_is_stable);list('attention',doc.remaining_attention);list('checklist',doc.pre_submission_checklist);list('limitations',doc.report_limitations);cards('findings',doc.selective_findings,'finding');cards('adjustments',doc.optional_micro_adjustments,'adjustment');const dims=$('dimensions');dims.textContent='';doc.assessment_dimensions.forEach(item=>{{const box=document.createElement('div');box.className='finding';const title=document.createElement('b');title.textContent=item.dimension;const finding=document.createElement('div');finding.textContent=item.finding;const implication=document.createElement('div');implication.className='why';implication.textContent='裁决含义：'+item.implication;box.append(title,finding,implication);dims.appendChild(box)}});$('boundary').textContent=doc.boundary_note}}
function moneyPair(usd,cny){{const parts=[];if(cny!==null&&cny!==undefined)parts.push('CNY ¥'+Number(cny).toFixed(6));if(usd!==null&&usd!==undefined)parts.push('USD $'+Number(usd).toFixed(6));return parts.length?parts.join(' / '):'不可用'}}
function renderCost(cost){{$('costBox').style.display=cost?'block':'none';if(!cost)return;$('costTotal').textContent=cost.status==='no_api_calls'?'CNY ¥0.000000 / USD $0.000000（未调用 API）':(cost.status==='usage_unavailable'?'API 未返回完整 token usage，无法估算':('约 '+moneyPair(cost.total_estimated_cost_usd,cost.total_estimated_cost_cny)));const pricing=cost.pricing,fx=cost.exchange_rate;$('costSource').textContent=pricing?('价格来源：'+pricing.source_status+'；原币 '+pricing.currency+'；'+pricing.price_as_of+'；'+pricing.source_url+'；'+pricing.note+(fx?(' 汇率来源：'+fx.source_status+'；'+fx.rate_date+'；1 USD = '+Number(fx.usd_to_cny).toFixed(6)+' CNY；'+fx.source_url+'。'):'')):'没有可验证的该模型价格。';const callItems=(cost.calls||[]).map(item=>'API '+item.call_index+'：'+(item.usage_complete===false?'token usage 不完整，费用不可用':('输入 '+item.prompt_tokens+'，缓存命中 '+item.cache_hit_tokens+'，输出 '+item.completion_tokens+' tokens，估算 '+moneyPair(item.estimated_cost_usd,item.estimated_cost_cny))));list('costCalls',callItems);list('costLimits',cost.billing_limitations)}}
function renderTimeline(items){{const el=$('timeline');el.textContent='';(items||[]).forEach(item=>{{const li=document.createElement('li');const clock=document.createElement('div');clock.className='time';clock.textContent=Number(item.elapsed_seconds).toFixed(1)+' 秒';const body=document.createElement('div');const message=document.createElement('div');message.textContent=item.message;body.appendChild(message);if(item.details&&Object.keys(item.details).length){{const detail=document.createElement('div');detail.className='detail';detail.textContent=JSON.stringify(item.details);body.appendChild(detail)}}li.append(clock,body);el.appendChild(li)}});el.scrollTop=el.scrollHeight}}
function renderResult(result,busy,interpretationError,presentationError){{lastResult=result;const c=result.closure_card;$('result').style.display='block';$('verdict').textContent=c.Verdict;$('reason').textContent=c.Reason;list('evidence',c['Evidence holds']);list('submission',c['Submission / external holds']);list('protected',c['Protected / Do not disturb']);renderSuggestions(c['Lite directional suggestions']);$('next').textContent=c['Next permitted action'];renderHarness(result.runtime);renderCost(result.task_cost);renderInterpretation(result.interpretation);$('json').textContent=JSON.stringify(result,null,2);$('save').disabled=busy;$('copy').disabled=busy;$('saveInterpretation').disabled=busy||!(result.interpretation&&result.interpretation.document);const holdText=presentationError?('机器裁决保持有效，但公开展示处于 HOLD：'+presentationError):(interpretationError?('核心裁决保持有效，但中文解读未生成：'+interpretationError):'');$('interpError').textContent=holdText;$('interpError').style.display=holdText?'block':'none'}}
function renderStatus(s){{providers=s.providers;updateProvider();$('phase').textContent=s.phase;$('message').textContent=s.message;$('elapsed').textContent=Number(s.elapsed_seconds).toFixed(1)+' 秒';$('run').disabled=s.busy;$('dot').className='dot '+(s.busy?'busy':(s.error||s.presentation_error)?'bad':s.result?'ok':'');renderTimeline(s.timeline);if(s.error)showError(s.error);if(s.result)renderResult(s.result,s.busy,s.interpretation_error,s.presentation_error);if(!s.busy&&poller){{clearInterval(poller);poller=null}}}}
async function refresh(){{try{{renderStatus(await api('/api/status'))}}catch(e){{showError(e.message)}}}}
$('provider').onchange=async()=>{{$('modelSource').textContent='';updateProvider(true);await refreshModelCatalog()}};$('model').onchange=refreshReasoningOptions;$('refreshModels').onclick=refreshModelCatalog;$('pickManuscript').onclick=async()=>{{try{{showError('');const r=await api('/api/pick-manuscript',{{}});if(r.path){{$('manuscript').value=r.path;$('identity').value=r.path.split(/[\\/]/).pop()}}}}catch(e){{showError(e.message)}}}};
$('pickPrior').onclick=async()=>{{try{{showError('');const r=await api('/api/pick-prior',{{}});if(r.path)$('prior').value=r.path}}catch(e){{showError(e.message)}}}};
$('run').onclick=async()=>{{try{{showError('');$('result').style.display='none';$('save').disabled=true;$('saveInterpretation').disabled=true;$('copy').disabled=true;lastResult=null;await api('/api/analyze',{{manuscript_path:$('manuscript').value,provider:$('provider').value,model:$('model').value,reasoning_option:$('reasoning').value,language:$('language').value,identity:$('identity').value,confirmed_complete:$('confirmed').checked,prior_receipt_path:$('prior').value,generate_interpretation:$('interpret').checked}});await refresh();poller=setInterval(refresh,400)}}catch(e){{showError(e.message);await refresh()}}}};
$('save').onclick=async()=>{{try{{const r=await api('/api/save',{{}});if(r.saved)$('message').textContent='已保存：'+r.path}}catch(e){{showError(e.message)}}}};
$('saveInterpretation').onclick=async()=>{{try{{const r=await api('/api/save-interpretation',{{}});if(r.saved)$('message').textContent='中文解读已保存：'+r.path}}catch(e){{showError(e.message)}}}};
$('copy').onclick=async()=>{{if(!lastResult)return;const text=JSON.stringify(lastResult,null,2);try{{await navigator.clipboard.writeText(text)}}catch(_e){{const area=document.createElement('textarea');area.value=text;area.style.position='fixed';area.style.opacity='0';document.body.appendChild(area);area.select();document.execCommand('copy');area.remove()}}$('message').textContent='JSON 已复制'}};
$('close').onclick=async()=>{{try{{await api('/api/close',{{}});document.body.innerHTML='<div style="font:18px Segoe UI;padding:40px">本地程序已关闭，可以关闭此页面。<br>Local program closed; you may close this tab.</div>'}}catch(e){{showError(e.message)}}}};
refresh().then(refreshReasoningOptions);
</script></body></html>'''
