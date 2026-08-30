"""DeepSeek, Kimi, and Gemini clients using environment keys only."""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from . import __version__
from .harness import SCHEMA_DEFINITION_LINT_VERSION, validate_schema_definition

class ProviderConfigurationError(ValueError):
    """Raised when a provider lacks a safe, complete configuration."""


class ProviderRequestError(RuntimeError):
    """Raised when a provider request cannot produce one usable response."""

    def __init__(self, message: str, *, request_receipts: tuple[dict[str, Any], ...] = ()) -> None:
        super().__init__(message)
        self.request_receipts = tuple(dict(item) for item in request_receipts)


DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
_STAGE_TIMEOUT_SECONDS: dict[str, dict[str, float]] = {
    "deepseek": {
        "coverage": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "adjudication": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "presentation_repair": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "interpretation": DEFAULT_REQUEST_TIMEOUT_SECONDS,
    },
    "kimi": {
        "coverage": 300.0,
        "adjudication": 900.0,
        "presentation_repair": 900.0,
        "interpretation": 900.0,
    },
    "gemini": {
        "coverage": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "adjudication": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "presentation_repair": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        "interpretation": DEFAULT_REQUEST_TIMEOUT_SECONDS,
    },
}
_RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
PROVIDER_REQUEST_TRANSACTION_VERSION = "mrc-provider-request-transaction-2.0"
PROVIDER_ERROR_DETAIL_CONTRACT_VERSION = "mrc-provider-error-detail-1.0"
_PROVIDER_ERROR_DETAIL_MAX_LENGTH = 240
_SENSITIVE_DETAIL_CONTEXT_RE = re.compile(
    r"(?:authorization|api[_ -]?key|access[_ -]?token|request[_ -]?body|"
    r"full[_ -]?text|manuscript|prompt|hidden[_ -]?diagnostic|chain[_ -]?of[_ -]?thought|cot)\s*[:=]",
    re.I,
)
_SECRET_TOKEN_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{12,}|AKIA[0-9A-Z]{12,}|"
    r"Bearer\s+[A-Za-z0-9._~+/-]{8,})",
    re.I,
)
_OPAQUE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")


def sanitize_provider_error_detail(value: Any) -> str | None:
    """Return one bounded diagnostic line without request or secret-bearing content."""

    if not isinstance(value, str):
        return None
    detail = " ".join(value.split()).strip()
    if not detail:
        return None
    if _SENSITIVE_DETAIL_CONTEXT_RE.search(detail):
        return "[REDACTED_SENSITIVE_PROVIDER_DETAIL]"
    detail = _SECRET_TOKEN_RE.sub("[REDACTED]", detail)
    detail = _OPAQUE_TOKEN_RE.sub("[REDACTED]", detail)
    if len(detail) > _PROVIDER_ERROR_DETAIL_MAX_LENGTH:
        detail = detail[: _PROVIDER_ERROR_DETAIL_MAX_LENGTH - 3].rstrip() + "..."
    return detail or None


def _bounded_provider_error_scalar(value: Any) -> str | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split()).strip()
    if not clean or len(clean) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", clean):
        return None
    return clean


def provider_stage_timeout_seconds(
    provider: str,
    stage: str,
    *,
    override: float | None = None,
) -> float:
    """Return the finite wait window for one provider/stage request."""

    if override is not None:
        if not isinstance(override, (int, float)) or isinstance(override, bool) or override <= 0:
            raise ProviderConfigurationError("timeout override must be a positive number")
        return float(override)
    provider_name = provider.casefold().strip()
    stage_name = stage.casefold().strip()
    try:
        return _STAGE_TIMEOUT_SECONDS[provider_name][stage_name]
    except KeyError as exc:
        raise ProviderConfigurationError("timeout stage requires one registered provider and stage") from exc


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    default_model: str
    default_base_url: str
    key_variables: tuple[str, ...]
    model_variable: str
    base_url_variable: str
    default_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    supports_strict_json_schema: bool
    supports_json_object_mode: bool
    schema_delivery_mode: str


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    model: str
    base_url: str
    api_key: str
    key_variable: str


@dataclass(frozen=True, slots=True)
class CompletionResult:
    content: str
    model: str
    usage: dict[str, int]
    finish_reason: str | None = None
    request_receipts: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ReasoningOption:
    value: str
    label: str


PROVIDERS = {
    "deepseek": ProviderSpec(
        name="deepseek",
        default_model="deepseek-v4-pro",
        default_base_url="https://api.deepseek.com",
        key_variables=("DEEPSEEK_API_KEY",),
        model_variable="DEEPSEEK_MODEL",
        base_url_variable="DEEPSEEK_BASE_URL",
        default_models=("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"),
    ),
    "kimi": ProviderSpec(
        name="kimi",
        default_model="kimi-k2.6",
        default_base_url="https://api.moonshot.cn/v1",
        key_variables=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        model_variable="KIMI_MODEL",
        base_url_variable="KIMI_BASE_URL",
        default_models=("kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"),
    ),
    "gemini": ProviderSpec(
        name="gemini",
        default_model="gemini-3.7-flash",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_variables=("GEMINI_API_KEY",),
        model_variable="GEMINI_MODEL",
        base_url_variable="GEMINI_BASE_URL",
        default_models=(
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-pro-latest",
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
        ),
    ),
}


PROVIDER_CAPABILITIES: dict[str, ProviderCapability] = {
    "deepseek": ProviderCapability(
        supports_strict_json_schema=False,
        supports_json_object_mode=True,
        schema_delivery_mode="prompt_canonical_schema_plus_json_object",
    ),
    "kimi": ProviderCapability(
        supports_strict_json_schema=True,
        supports_json_object_mode=True,
        schema_delivery_mode="prompt_canonical_schema_plus_api_strict_json_schema",
    ),
    "gemini": ProviderCapability(
        supports_strict_json_schema=True,
        supports_json_object_mode=True,
        schema_delivery_mode="prompt_canonical_schema_plus_api_strict_json_schema",
    ),
}


def provider_capability(provider: str) -> dict[str, Any]:
    name = provider.casefold().strip()
    try:
        capability = PROVIDER_CAPABILITIES[name]
    except KeyError as exc:
        raise ProviderConfigurationError("provider capability requires one registered provider") from exc
    return {
        "supports_strict_json_schema": capability.supports_strict_json_schema,
        "supports_json_object_mode": capability.supports_json_object_mode,
        "schema_delivery_mode": capability.schema_delivery_mode,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_retry_after(headers: Any) -> str | None:
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        value = None
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:120] or None


def _option(value: str, label: str) -> ReasoningOption:
    return ReasoningOption(value=value, label=label)


def reasoning_profile(provider: str, model: str) -> dict[str, Any]:
    """Return the exact reasoning controls supported by one provider/model pair."""

    provider_name = provider.casefold().strip()
    model_id = model.casefold().strip()
    if provider_name not in PROVIDERS or not model_id:
        raise ProviderConfigurationError("reasoning profile requires one known provider and model")
    default = _option("default", "使用模型默认思考设置")
    options: tuple[ReasoningOption, ...]
    note: str
    if provider_name == "deepseek":
        options = (
            _option("default", "默认：开启思考（high）"),
            _option("disabled", "关闭思考"),
            _option("low", "低强度思考"),
            _option("high", "高强度思考"),
            _option("max", "最大强度思考"),
        )
        note = "DeepSeek OpenAI 接口：thinking 可开关；reasoning_effort 支持 low / high / max。"
    elif provider_name == "kimi" and model_id == "kimi-k3":
        options = (
            _option("default", "默认：最大强度思考（max）"),
            _option("low", "低强度思考"),
            _option("high", "高强度思考"),
            _option("max", "最大强度思考"),
        )
        note = "Kimi K3 始终思考；reasoning_effort 支持 low / high / max，不能关闭。"
    elif provider_name == "kimi" and model_id in {"kimi-k2.6", "kimi-k2.5"}:
        options = (
            _option("default", "默认：开启思考"),
            _option("enabled", "开启思考"),
            _option("disabled", "关闭思考"),
        )
        note = f"{model} 只支持思考开启/关闭，不支持 low / high / max 强度。"
    elif provider_name == "kimi" and model_id.startswith("kimi-k2.7-code"):
        options = (_option("default", "固定开启思考（不可调整）"),)
        note = "Kimi K2.7 Code 思考固定开启，reasoning_effort 与关闭思考均不受支持。"
    elif provider_name == "kimi":
        options = (default,)
        note = "该 Kimi 模型没有登记可安全发送的思考控制参数，保持提供商默认。"
    elif model_id == "gemini-3.7-flash":
        options = (
            _option("default", "默认：中等思考（medium）"),
            _option("low", "低强度思考"),
            _option("medium", "中等强度思考"),
            _option("high", "高强度思考"),
        )
        note = "Gemini 3.7 Flash 支持 low / medium / high，不能关闭思考。"
    elif model_id in {
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
    }:
        options = (
            default,
            _option("minimal", "最小思考"),
            _option("low", "低强度思考"),
            _option("medium", "中等强度思考"),
            _option("high", "高强度思考"),
        )
        note = f"{model} 支持 minimal / low / medium / high。"
    elif model_id.startswith("gemini-3.1-pro") or model_id == "gemini-2.5-pro":
        options = (
            default,
            _option("low", "低强度思考"),
            _option("medium", "中等强度思考"),
            _option("high", "高强度思考"),
        )
        note = f"{model} 支持 low / medium / high，不能关闭思考。"
    elif model_id in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
        options = (
            default,
            _option("none", "关闭思考"),
            _option("low", "低强度思考"),
            _option("medium", "中等强度思考"),
            _option("high", "高强度思考"),
        )
        note = f"{model} 兼容 none / low / medium / high。"
    elif provider_name == "gemini":
        options = (default,)
        note = "该 Gemini 别名或专用模型没有登记稳定的思考等级矩阵，保持模型默认。"
    else:
        raise ProviderConfigurationError("unknown provider reasoning profile")
    return {
        "provider": provider_name,
        "model": model,
        "default": "default",
        "options": [{"value": item.value, "label": item.label} for item in options],
        "note": note,
    }


def validate_reasoning_option(provider: str, model: str, selection: str | None) -> str:
    selected = (selection or "default").casefold().strip()
    allowed = {item["value"] for item in reasoning_profile(provider, model)["options"]}
    if selected not in allowed:
        raise ProviderConfigurationError(
            f"reasoning option {selected!r} is not supported by {provider}/{model}; allowed={sorted(allowed)}"
        )
    return selected


def _apply_reasoning_option(payload: dict[str, Any], provider: str, model: str, selection: str | None) -> str:
    selected = validate_reasoning_option(provider, model, selection)
    model_id = model.casefold()
    if selected == "default":
        return selected
    if provider == "deepseek":
        if selected == "disabled":
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = selected
    elif provider == "kimi" and model_id == "kimi-k3":
        payload["reasoning_effort"] = selected
    elif provider == "kimi" and model_id in {"kimi-k2.6", "kimi-k2.5"}:
        payload["thinking"] = {"type": selected}
    elif provider == "gemini":
        payload["reasoning_effort"] = selected
    else:
        raise ProviderConfigurationError("reasoning option cannot be represented for this provider/model")
    return selected


def resolve_provider_selection(
    provider: str,
    *,
    model: str | None = None,
) -> tuple[ProviderSpec, str]:
    """Resolve provider/model identity without reading an API key or endpoint."""

    key = provider.casefold().strip()
    spec = PROVIDERS.get(key)
    if spec is None:
        raise ProviderConfigurationError("provider must be deepseek, kimi, or gemini")
    selected_model = (model or os.environ.get(spec.model_variable) or spec.default_model).strip()
    if not selected_model or any(character.isspace() for character in selected_model):
        raise ProviderConfigurationError("model name must be one non-empty token")
    return spec, selected_model


def load_provider_config(provider: str, *, model: str | None = None) -> ProviderConfig:
    spec, selected_model = resolve_provider_selection(provider, model=model)
    api_key = ""
    key_variable = spec.key_variables[0]
    for variable in spec.key_variables:
        value = os.environ.get(variable, "").strip()
        if value:
            api_key = value
            key_variable = variable
            break
    if not api_key:
        variables = " or ".join(spec.key_variables)
        raise ProviderConfigurationError(f"missing API key environment variable: {variables}")
    base_url = (os.environ.get(spec.base_url_variable) or spec.default_base_url).strip().rstrip("/")
    if not base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise ProviderConfigurationError("provider base URL must use HTTPS, except localhost test endpoints")
    return ProviderConfig(
        name=spec.name,
        model=selected_model,
        base_url=base_url,
        api_key=api_key,
        key_variable=key_variable,
    )


def list_provider_models(provider: str, *, timeout_seconds: float = 15.0) -> list[str]:
    """Read the authenticated provider model catalog without exposing the key."""

    config = load_provider_config(provider)
    spec = PROVIDERS[config.name]
    if config.name == "gemini" and "generativelanguage.googleapis.com" in config.base_url:
        endpoint = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
        headers = {"x-goog-api-key": config.api_key, "Accept": "application/json"}
    else:
        endpoint = config.base_url + "/models"
        headers = {"Authorization": "Bearer " + config.api_key, "Accept": "application/json"}
    headers["User-Agent"] = f"manuscript-revision-closure-standalone/{__version__}"
    request = urllib.request.Request(endpoint, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise ProviderRequestError("provider model list request failed") from exc
    if len(raw) > 2 * 1024 * 1024:
        raise ProviderRequestError("provider model list response exceeds 2 MiB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderRequestError("provider model list is invalid JSON") from exc
    models: list[str] = []
    if config.name == "gemini":
        rows = payload.get("models", []) if isinstance(payload, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            methods = row.get("supportedGenerationMethods", row.get("supportedActions", []))
            name = row.get("name")
            if not isinstance(name, str) or not isinstance(methods, list) or "generateContent" not in methods:
                continue
            model_id = name.removeprefix("models/")
            excluded = (
                "embedding",
                "image",
                "tts",
                "live",
                "robotics",
                "aqa",
                "computer-use",
                "transcribe",
                "omni",
                "customtools",
            )
            if model_id.startswith("gemini-") and not any(token in model_id.casefold() for token in excluded):
                models.append(model_id)
    else:
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        for row in rows if isinstance(rows, list) else []:
            model_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(model_id, str) and model_id.strip() and not any(character.isspace() for character in model_id):
                models.append(model_id.strip())
    unique = list(dict.fromkeys(models))
    if not unique:
        raise ProviderRequestError("provider returned no compatible text-generation models")
    preferred = [model for model in spec.default_models if model in unique]
    remainder = sorted(model for model in unique if model not in preferred)
    return [*preferred, *remainder]


class ChatCompletionClient:
    """One-call client whose logical request is exactly one physical HTTP attempt."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_transient_retries: int = 0,
        on_attempt: Callable[[int], None] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ProviderConfigurationError("timeout must be positive")
        if max_transient_retries != 0:
            raise ProviderConfigurationError("automatic full-request retries are disabled; max_transient_retries must be zero")
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.max_transient_retries = max_transient_retries
        self.on_attempt = on_attempt

    @property
    def endpoint(self) -> str:
        return self.config.base_url + "/chat/completions"

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        reasoning_option: str | None = None,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
        json_schema_name: str = "response",
        max_output_tokens: int | None = None,
        stage: str = "unspecified",
        schema_sha256: str | None = None,
        coverage_digest_sha256: str | None = None,
    ) -> CompletionResult:
        schema_lint_receipt = (
            validate_schema_definition(json_schema)
            if json_schema is not None
            else None
        )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
        }
        # The former 5,000-token application cap could truncate reasoning plus
        # the final structured object.  APIs still require a finite ceiling
        # (and Kimi defaults much lower if omitted), so use provider-scale
        # headroom rather than one small cross-provider cap.
        provider_ceiling = {"kimi": 131072, "deepseek": 393216, "gemini": 65536}[self.config.name]
        selected_ceiling = provider_ceiling if max_output_tokens is None else max_output_tokens
        if (
            not isinstance(selected_ceiling, int)
            or isinstance(selected_ceiling, bool)
            or selected_ceiling <= 0
            or selected_ceiling > provider_ceiling
        ):
            raise ProviderConfigurationError("max_output_tokens exceeds the registered provider ceiling")
        if self.config.name == "kimi":
            payload["max_completion_tokens"] = selected_ceiling
        else:
            payload["max_tokens"] = selected_ceiling
        capability = PROVIDER_CAPABILITIES[self.config.name]
        if json_schema is not None and capability.supports_strict_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema_name,
                    "strict": True,
                    "schema": dict(json_schema),
                },
            }
        elif json_mode and capability.supports_json_object_mode:
            payload["response_format"] = {"type": "json_object"}
        selected_reasoning = _apply_reasoning_option(
            payload, self.config.name, self.config.model, reasoning_option
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"manuscript-revision-closure-standalone/{__version__}",
            },
        )
        receipt: dict[str, Any] = {
            "contract_version": PROVIDER_REQUEST_TRANSACTION_VERSION,
            "request_id": str(uuid.uuid4()),
            "stage": stage,
            "provider": self.config.name,
            "model": self.config.model,
            "reasoning_option": selected_reasoning,
            "physical_attempt_number": 1,
            "timeout_seconds": self.timeout_seconds,
            "max_transient_retries": 0,
            "retry_decision": "STOP_NO_AUTOMATIC_RETRY",
            "request_dispatched": False,
            "provider_outcome": "NOT_CALLED",
            "http_status": None,
            "retry_after": None,
            "error_class": None,
            "error_summary": None,
            "provider_error_detail_contract_version": PROVIDER_ERROR_DETAIL_CONTRACT_VERSION,
            "provider_error_status": None,
            "provider_error_code": None,
            "provider_error_detail": None,
            "usage_status": "UNKNOWN",
            "usage": {},
            "schema_sha256": schema_sha256,
            "coverage_digest_sha256": coverage_digest_sha256,
            "schema_delivery_mode": capability.schema_delivery_mode,
            "schema_definition_lint_contract_version": (
                SCHEMA_DEFINITION_LINT_VERSION if schema_lint_receipt is not None else None
            ),
            "schema_definition_lint_status": (
                schema_lint_receipt.get("status") if schema_lint_receipt is not None else None
            ),
            "started_at": _utc_now(),
            "finished_at": None,
        }
        if self.on_attempt is not None:
            self.on_attempt(1)
        receipt["request_dispatched"] = True
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                receipt["http_status"] = int(getattr(response, "status", 200))
                response_payload = json.loads(response.read().decode("utf-8"))
            completion = self._parse_payload(response_payload)
            receipt["provider_outcome"] = "SUCCEEDED"
            receipt["usage"] = dict(completion.usage)
            receipt["usage_status"] = (
                "COMPLETE"
                if {"prompt_tokens", "completion_tokens"}.issubset(completion.usage)
                else "PARTIAL" if completion.usage else "UNKNOWN"
            )
            receipt["finished_at"] = _utc_now()
            return CompletionResult(
                content=completion.content,
                model=completion.model,
                usage=dict(completion.usage),
                finish_reason=completion.finish_reason,
                request_receipts=(dict(receipt),),
            )
        except urllib.error.HTTPError as exc:
            detail = None
            provider_error_status = None
            provider_error_code = None
            try:
                error_payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
                error_object = error_payload.get("error", {})
                if isinstance(error_object, Mapping):
                    provider_error_status = _bounded_provider_error_scalar(error_object.get("status"))
                    provider_error_code = _bounded_provider_error_scalar(error_object.get("code"))
                    detail = sanitize_provider_error_detail(error_object.get("message"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                detail = None
            receipt.update(
                {
                    "provider_outcome": (
                        "REJECTED" if 400 <= exc.code < 500 else "UNKNOWN"
                    ),
                    "http_status": exc.code,
                    "retry_after": _bounded_retry_after(exc.headers),
                    "error_class": "HTTPError",
                    "error_summary": f"HTTP status {exc.code}",
                    "provider_error_status": provider_error_status,
                    "provider_error_code": provider_error_code,
                    "provider_error_detail": detail,
                    "finished_at": _utc_now(),
                }
            )
            safe_suffix = f"; provider detail: {detail}" if detail else ""
            raise ProviderRequestError(
                f"provider HTTP request failed with status {exc.code}{safe_suffix}; automatic resend is disabled",
                request_receipts=(receipt,),
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            receipt.update(
                {
                    "provider_outcome": "UNKNOWN",
                    "error_class": type(exc).__name__,
                    "error_summary": "provider response timeout; server-side execution status is unknown",
                    "finished_at": _utc_now(),
                }
            )
            raise ProviderRequestError(
                f"provider response timed out after {self.timeout_seconds:g} seconds; "
                "the request was not automatically resent because server-side execution status is unknown",
                request_receipts=(receipt,),
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            summary = (
                "provider response timeout; server-side execution status is unknown"
                if isinstance(reason, (TimeoutError, socket.timeout))
                else "provider network failure; server-side execution status is unknown"
            )
            receipt.update(
                {
                    "provider_outcome": "UNKNOWN",
                    "error_class": "URLError",
                    "error_summary": summary,
                    "finished_at": _utc_now(),
                }
            )
            raise ProviderRequestError(
                summary + "; the request was not automatically resent",
                request_receipts=(receipt,),
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ProviderRequestError) as exc:
            receipt.update(
                {
                    "provider_outcome": "UNKNOWN",
                    "error_class": type(exc).__name__,
                    "error_summary": "provider completion failed the bounded response envelope",
                    "finished_at": _utc_now(),
                }
            )
            raise ProviderRequestError(
                "provider returned an unusable completion envelope",
                request_receipts=(receipt,),
            ) from exc

    def _parse_payload(self, payload: Any) -> CompletionResult:
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderRequestError("provider response is missing choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderRequestError("provider returned an empty model response")
        usage_raw = payload.get("usage", {}) if isinstance(payload, dict) else {}
        usage: dict[str, int] = {}
        if isinstance(usage_raw, dict):
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
                "cached_tokens",
                "reasoning_tokens",
            ):
                value = usage_raw.get(key)
                if isinstance(value, int) and value >= 0:
                    usage[key] = value
            details = usage_raw.get("prompt_tokens_details")
            if isinstance(details, dict):
                cached = details.get("cached_tokens")
                if isinstance(cached, int) and cached >= 0:
                    usage["cached_tokens"] = cached
            completion_details = usage_raw.get("completion_tokens_details")
            if isinstance(completion_details, dict):
                reasoning = completion_details.get("reasoning_tokens")
                if isinstance(reasoning, int) and reasoning >= 0:
                    usage["reasoning_tokens"] = reasoning
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        if not isinstance(finish_reason, str) or not finish_reason.strip():
            finish_reason = None
        model = payload.get("model", self.config.model) if isinstance(payload, dict) else self.config.model
        if not isinstance(model, str) or not model.strip():
            model = self.config.model
        return CompletionResult(
            content=content.strip(),
            model=model.strip(),
            usage=usage,
            finish_reason=finish_reason.strip() if finish_reason else None,
        )
