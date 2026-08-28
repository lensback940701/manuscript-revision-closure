"""One-shot presentation repair over bounded public fields only.

This module never receives the manuscript, coverage rows, prompts, hidden
review diagnostics, credentials, or raw provider payloads. The validated
machine adjudication remains authoritative; repaired text is an attached
presentation layer bound to immutable source identities.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .harness import canonical_digest
from .providers import (
    ChatCompletionClient,
    ProviderRequestError,
    load_provider_config,
    provider_stage_timeout_seconds,
)


PRESENTATION_SOURCE_CONTRACT_VERSION = "mrc-presentation-source-1.0"
PRESENTATION_REPAIR_CONTRACT_VERSION = "mrc-presentation-repair-1.0"
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_DISPLAY_FIELDS = ("Direction", "Why it matters", "What to protect")


class PresentationRepairError(ValueError):
    """Raised when the single bounded presentation repair cannot be accepted."""

    def __init__(self, message: str, *, runtime: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.runtime = dict(runtime or {})


@dataclass(frozen=True, slots=True)
class PresentationEntry:
    item_id: str
    path: str
    source_text: str

    def as_private_request_dict(self) -> dict[str, str]:
        return {"id": self.item_id, "path": self.path, "source_text": self.source_text}


@dataclass(frozen=True, slots=True)
class PresentationSource:
    entries: tuple[PresentationEntry, ...]
    source_digest_sha256: str
    protected_binding_digest_sha256: str
    protected_item_count: int

    def bounded_receipt(self) -> dict[str, Any]:
        return {
            "contract_version": PRESENTATION_SOURCE_CONTRACT_VERSION,
            "source_digest_sha256": self.source_digest_sha256,
            "protected_binding_digest_sha256": self.protected_binding_digest_sha256,
            "item_count": len(self.entries),
            "item_ids": [entry.item_id for entry in self.entries],
            "protected_item_count": self.protected_item_count,
            "protected_item_ids": [
                entry.item_id for entry in self.entries if entry.path.startswith("protected[")
            ],
        }


@dataclass(frozen=True, slots=True)
class PresentationRepairResult:
    display_state: dict[str, Any]
    receipt: dict[str, Any]
    usage: dict[str, int]
    model: str
    attempts: int


def usage_status(usage: Mapping[str, Any]) -> str:
    """Classify provider usage without representing unknown values as zero."""

    present = {
        key
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(usage.get(key), int)
        and not isinstance(usage.get(key), bool)
        and usage.get(key) >= 0
    }
    if present == {"prompt_tokens", "completion_tokens", "total_tokens"}:
        return "COMPLETE"
    if usage:
        return "PARTIAL"
    return "UNKNOWN"


def aggregate_usage_status(usages: Sequence[Mapping[str, Any]]) -> str:
    if not usages:
        return "UNKNOWN"
    statuses = [usage_status(item) for item in usages]
    if all(status == "COMPLETE" for status in statuses):
        return "COMPLETE"
    if all(status == "UNKNOWN" for status in statuses):
        return "UNKNOWN"
    return "PARTIAL"


def _item_id(path: str, source_text: str) -> str:
    payload = json.dumps(
        {"path": path, "source_text": source_text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "p_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 240:
        raise PresentationRepairError(f"presentation source {path} is not concise non-empty text")
    return value.strip()


def build_presentation_source(machine_state: Mapping[str, Any]) -> PresentationSource:
    """Create immutable identities for every model-authored public text value."""

    entries: list[PresentationEntry] = []
    protected = machine_state.get("protected", [])
    parked = machine_state.get("parked_opportunities", [])
    suggestions = machine_state.get("lite_suggestions", [])
    if not isinstance(protected, list) or not isinstance(parked, list) or not isinstance(suggestions, list):
        raise PresentationRepairError("machine presentation fields must be finite lists")

    for index, value in enumerate(protected):
        path = f"protected[{index}]"
        text = _string(value, path)
        entries.append(PresentationEntry(_item_id(path, text), path, text))
    for index, value in enumerate(parked):
        path = f"parked_opportunities[{index}]"
        text = _string(value, path)
        entries.append(PresentationEntry(_item_id(path, text), path, text))
    for index, suggestion in enumerate(suggestions):
        if not isinstance(suggestion, Mapping) or set(suggestion) != set(_DISPLAY_FIELDS):
            raise PresentationRepairError("machine Lite suggestion key set is not presentation-safe")
        for field in _DISPLAY_FIELDS:
            path = f"lite_suggestions[{index}].{field}"
            text = _string(suggestion[field], path)
            entries.append(PresentationEntry(_item_id(path, text), path, text))

    ids = [entry.item_id for entry in entries]
    if len(ids) != len(set(ids)):
        raise PresentationRepairError("presentation source identities collide")

    private_payload = {
        "contract_version": PRESENTATION_SOURCE_CONTRACT_VERSION,
        "items": [entry.as_private_request_dict() for entry in entries],
    }
    protected_payload = {
        "contract_version": PRESENTATION_SOURCE_CONTRACT_VERSION,
        "items": [
            entry.as_private_request_dict()
            for entry in entries
            if entry.path.startswith("protected[")
        ],
    }
    return PresentationSource(
        entries=tuple(entries),
        source_digest_sha256=canonical_digest(private_payload),
        protected_binding_digest_sha256=canonical_digest(protected_payload),
        protected_item_count=len(protected),
    )


def _repair_schema(source: PresentationSource) -> dict[str, Any]:
    ids = [entry.item_id for entry in source.entries]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["contract_version", "source_digest_sha256", "items"],
        "properties": {
            "contract_version": {
                "type": "string",
                "enum": [PRESENTATION_REPAIR_CONTRACT_VERSION],
            },
            "source_digest_sha256": {
                "type": "string",
                "enum": [source.source_digest_sha256],
            },
            "items": {
                "type": "array",
                "minItems": len(ids),
                "maxItems": len(ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "text"],
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "text": {"type": "string", "maxLength": 240},
                    },
                },
            },
        },
    }


def build_presentation_repair_messages(
    source: PresentationSource,
    *,
    target_language: str,
) -> list[dict[str, str]]:
    """Build a request that contains no manuscript or private adjudication data."""

    if target_language != "zh":
        raise PresentationRepairError("presentation repair is currently registered only for zh")
    payload = {
        "contract_version": PRESENTATION_SOURCE_CONTRACT_VERSION,
        "target_language": "zh-CN",
        "source_digest_sha256": source.source_digest_sha256,
        "items": [entry.as_private_request_dict() for entry in source.entries],
    }
    system = """You are a presentation-only localization repair stage.
Translate only the supplied bounded public text values into concise Simplified
Chinese. The source text attached to each immutable id remains authoritative.
Do not add, delete, merge, split, narrow, broaden, reinterpret, or reorder any
protection, opportunity, or directional suggestion. Do not output a verdict,
hold code, manuscript claim, diagnosis, explanation, or any field not present in
the required schema. Return one exact JSON object and nothing else."""
    user = (
        "Repair the presentation language for this bounded public projection.\n"
        "The request contains no manuscript and no private coverage rows.\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if (
            len(lines) < 3
            or lines[0].strip() not in {"```", "```json", "```JSON"}
            or lines[-1].strip() != "```"
        ):
            raise PresentationRepairError("presentation repair contains an invalid JSON fence")
        text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PresentationRepairError("presentation repair is not one JSON object") from exc
    if not isinstance(value, dict):
        raise PresentationRepairError("presentation repair must be one JSON object")
    return value


def validate_presentation_repair(
    value: Mapping[str, Any],
    source: PresentationSource,
    *,
    target_language: str,
) -> dict[str, str]:
    if set(value) != {"contract_version", "source_digest_sha256", "items"}:
        raise PresentationRepairError("presentation repair key set mismatch")
    if value["contract_version"] != PRESENTATION_REPAIR_CONTRACT_VERSION:
        raise PresentationRepairError("presentation repair contract version mismatch")
    if value["source_digest_sha256"] != source.source_digest_sha256:
        raise PresentationRepairError("presentation repair source digest mismatch")
    rows = value["items"]
    if not isinstance(rows, list) or len(rows) != len(source.entries):
        raise PresentationRepairError("presentation repair item cardinality mismatch")

    expected_ids = {entry.item_id for entry in source.entries}
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"id", "text"}:
            raise PresentationRepairError("presentation repair item key set mismatch")
        item_id = row["id"]
        text = row["text"]
        if item_id not in expected_ids or item_id in result:
            raise PresentationRepairError("presentation repair item identity mismatch")
        if not isinstance(text, str):
            raise PresentationRepairError("presentation repair text must be a string")
        cleaned = " ".join(text.split())
        if not cleaned or len(cleaned) > 240:
            raise PresentationRepairError("presentation repair text must be concise and non-empty")
        if target_language == "zh" and not _CJK_RE.search(cleaned):
            raise PresentationRepairError("presentation repair still contains non-Chinese public text")
        result[item_id] = cleaned
    if set(result) != expected_ids:
        raise PresentationRepairError("presentation repair did not preserve the exact identity set")
    return result


def _apply_display_text(
    machine_state: Mapping[str, Any],
    source: PresentationSource,
    repaired: Mapping[str, str],
) -> dict[str, Any]:
    display = deepcopy(dict(machine_state))
    for entry in source.entries:
        text = repaired[entry.item_id]
        path = entry.path
        if path.startswith("protected["):
            index = int(path[len("protected[") : path.index("]")])
            display["protected"][index] = text
        elif path.startswith("parked_opportunities["):
            index = int(path[len("parked_opportunities[") : path.index("]")])
            display["parked_opportunities"][index] = text
        elif path.startswith("lite_suggestions["):
            index = int(path[len("lite_suggestions[") : path.index("]")])
            field = path.split("].", 1)[1]
            display["lite_suggestions"][index][field] = text
        else:  # pragma: no cover - source builder makes this unreachable
            raise PresentationRepairError("unknown presentation path")
    return display


def presentation_receipt_without_repair(
    machine_state: Mapping[str, Any],
    *,
    target_language: str,
    machine_state_digest_sha256: str,
) -> dict[str, Any]:
    source = build_presentation_source(machine_state)
    return {
        **source.bounded_receipt(),
        "repair_contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
        "target_language": target_language,
        "status": "PASS",
        "repair_attempted": False,
        "repair_call_count": 0,
        "provider_outcome": "NOT_CALLED",
        "usage_status": "UNKNOWN",
        "machine_state_digest_before_sha256": machine_state_digest_sha256,
        "machine_state_digest_after_sha256": machine_state_digest_sha256,
        "machine_state_parity": True,
    }


def repair_presentation(
    machine_state: Mapping[str, Any],
    *,
    provider: str,
    model: str | None,
    reasoning_option: str | None,
    target_language: str,
    timeout_seconds: float | None = None,
    transient_retries: int = 2,
) -> PresentationRepairResult:
    """Perform at most one provider call over the bounded public projection."""

    source = build_presentation_source(machine_state)
    machine_digest_before = canonical_digest(dict(machine_state))
    if not source.entries:
        receipt = presentation_receipt_without_repair(
            machine_state,
            target_language=target_language,
            machine_state_digest_sha256=machine_digest_before,
        )
        return PresentationRepairResult(
            display_state=deepcopy(dict(machine_state)),
            receipt=receipt,
            usage={},
            model=model or "",
            attempts=0,
        )

    config = load_provider_config(provider, model=model)
    request_timeout = provider_stage_timeout_seconds(
        config.name,
        "interpretation",
        override=timeout_seconds,
    )
    messages = build_presentation_repair_messages(source, target_language=target_language)
    attempts: list[int] = []

    def on_attempt(number: int) -> None:
        attempts.append(number)

    try:
        completion = ChatCompletionClient(
            config,
            timeout_seconds=request_timeout,
            max_transient_retries=transient_retries,
            on_attempt=on_attempt,
        ).complete(
            messages,
            reasoning_option=reasoning_option,
            json_mode=True,
            json_schema=_repair_schema(source),
            json_schema_name="mrc_presentation_repair",
            max_output_tokens=8192,
        )
    except ProviderRequestError as exc:
        text = str(exc)
        outcome = "UNKNOWN" if "unknown" in text or "timed out" in text else "REJECTED"
        raise PresentationRepairError(
            text,
            runtime={
                **source.bounded_receipt(),
                "repair_contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
                "target_language": target_language,
                "status": "HOLD",
                "error_code": "PRESENTATION_PROVIDER_FAILURE",
                "repair_attempted": True,
                "repair_call_count": 1,
                "provider": config.name,
                "model": config.model,
                "attempts": len(attempts),
                "provider_outcome": outcome,
                "usage": {},
                "usage_status": "UNKNOWN",
                "machine_state_digest_before_sha256": machine_digest_before,
                "machine_state_digest_after_sha256": machine_digest_before,
                "machine_state_parity": True,
            },
        ) from exc

    runtime_base = {
        **source.bounded_receipt(),
        "repair_contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
        "target_language": target_language,
        "repair_attempted": True,
        "repair_call_count": 1,
        "provider": config.name,
        "model": completion.model,
        "attempts": len(attempts),
        "provider_outcome": "SUCCEEDED",
        "usage": dict(completion.usage),
        "usage_status": usage_status(completion.usage),
        "machine_state_digest_before_sha256": machine_digest_before,
    }
    if completion.finish_reason == "length":
        raise PresentationRepairError(
            "provider truncated the presentation repair response",
            runtime={
                **runtime_base,
                "status": "HOLD",
                "error_code": "PRESENTATION_REPAIR_TRUNCATED",
                "machine_state_digest_after_sha256": machine_digest_before,
                "machine_state_parity": True,
            },
        )
    try:
        repaired = validate_presentation_repair(
            _parse_json(completion.content),
            source,
            target_language=target_language,
        )
        display_state = _apply_display_text(machine_state, source, repaired)
    except PresentationRepairError as exc:
        raise PresentationRepairError(
            str(exc),
            runtime={
                **runtime_base,
                "status": "HOLD",
                "error_code": "PRESENTATION_REPAIR_CONTRACT_FAILED",
                "machine_state_digest_after_sha256": machine_digest_before,
                "machine_state_parity": True,
            },
        ) from exc

    display_binding_digest = canonical_digest(
        {
            "contract_version": PRESENTATION_REPAIR_CONTRACT_VERSION,
            "items": [
                {"id": entry.item_id, "path": entry.path, "display_text": repaired[entry.item_id]}
                for entry in source.entries
            ],
        }
    )
    machine_digest_after = canonical_digest(dict(machine_state))
    if machine_digest_after != machine_digest_before:
        raise PresentationRepairError(
            "presentation repair changed the frozen machine state",
            runtime={
                **runtime_base,
                "status": "HOLD",
                "error_code": "MACHINE_STATE_PARITY_FAILED",
                "machine_state_digest_after_sha256": machine_digest_after,
                "machine_state_parity": False,
            },
        )
    receipt = {
        **runtime_base,
        "status": "PASS",
        "display_binding_digest_sha256": display_binding_digest,
        "machine_state_digest_after_sha256": machine_digest_after,
        "machine_state_parity": True,
    }
    return PresentationRepairResult(
        display_state=display_state,
        receipt=receipt,
        usage=dict(completion.usage),
        model=completion.model,
        attempts=len(attempts),
    )
