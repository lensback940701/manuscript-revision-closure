"""Privacy-bounded lifecycle events with one terminal owner per request.

Only lifecycle metadata is emitted. Manuscript text, model output, prompts,
private coverage rows, hidden diagnostics, credentials, and raw provider
responses are deliberately excluded from every event payload.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class RunPhase(str, Enum):
    CREATED = "created"
    READING = "reading"
    REQUESTING_MODEL = "requesting_model"
    VALIDATING = "validating"
    PRESENTING = "presenting"
    COMPLETED = "completed"
    FAILED = "failed"


LEGAL_TRANSITIONS = {
    RunPhase.CREATED: {RunPhase.READING, RunPhase.FAILED},
    RunPhase.READING: {
        RunPhase.REQUESTING_MODEL,
        RunPhase.VALIDATING,
        RunPhase.FAILED,
    },
    RunPhase.REQUESTING_MODEL: {RunPhase.VALIDATING, RunPhase.FAILED},
    RunPhase.VALIDATING: {
        RunPhase.PRESENTING,
        RunPhase.COMPLETED,
        RunPhase.FAILED,
    },
    RunPhase.PRESENTING: {RunPhase.COMPLETED, RunPhase.FAILED},
    RunPhase.COMPLETED: set(),
    RunPhase.FAILED: set(),
}


class InvalidTransition(RuntimeError):
    """Raised when the runtime tries to cross an undeclared phase boundary."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class EventSink:
    """Emit one canonical lifecycle stream and at most one terminal event."""

    callback: Callable[[dict[str, Any]], None] | None = None
    jsonl_path: Path | None = None
    thread_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    phase: RunPhase = RunPhase.CREATED
    events: list[dict[str, Any]] = field(default_factory=list)
    _item_counter: int = 0
    _started: bool = False
    _terminal_event: dict[str, Any] | None = None
    _terminal_event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    _lock: Any = field(default_factory=threading.RLock)

    @property
    def terminal_emitted(self) -> bool:
        return self._terminal_event is not None

    @property
    def terminal_event_id(self) -> str | None:
        return self._terminal_event_id if self._terminal_event is not None else None

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {
            "type": event_type,
            "thread_id": self.thread_id,
            "request_id": self.request_id,
            "timestamp": _now(),
            **payload,
        }
        with self._lock:
            self.events.append(event)
            if self.jsonl_path is not None:
                self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        if self.callback is not None:
            self.callback(dict(event))
        return event

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self.emit("thread.started", phase=self.phase.value)
        self.emit("turn.started", phase=self.phase.value)

    def transition(self, target: RunPhase) -> None:
        if target == self.phase:
            return
        if target not in LEGAL_TRANSITIONS[self.phase]:
            raise InvalidTransition(f"illegal runtime transition: {self.phase.value} -> {target.value}")
        self.phase = target
        self.emit("phase.changed", phase=target.value)

    def item_started(self, item_type: str, label: str, **metadata: Any) -> str:
        with self._lock:
            self._item_counter += 1
            item_id = f"item_{self._item_counter}"
        self.emit(
            "item.started",
            phase=self.phase.value,
            item={"id": item_id, "type": item_type, "label": label, **metadata},
        )
        return item_id

    def item_completed(
        self,
        item_id: str,
        item_type: str,
        status: str = "completed",
        **metadata: Any,
    ) -> None:
        self.emit(
            "item.completed",
            phase=self.phase.value,
            item={"id": item_id, "type": item_type, "status": status, **metadata},
        )

    def _terminal(self, event_type: str, **payload: Any) -> dict[str, Any]:
        # The re-entrant lock makes terminal ownership atomic even if two
        # callers race or a synchronous callback observes the terminal event.
        with self._lock:
            if self._terminal_event is not None:
                return dict(self._terminal_event)
            target = RunPhase.COMPLETED if event_type == "turn.completed" else RunPhase.FAILED
            if self.phase not in {RunPhase.COMPLETED, RunPhase.FAILED}:
                self.transition(target)
            event = self.emit(
                event_type,
                phase=self.phase.value,
                terminal_event_id=self._terminal_event_id,
                **payload,
            )
            self._terminal_event = dict(event)
            return dict(event)

    def complete(self, **metadata: Any) -> dict[str, Any]:
        return self._terminal("turn.completed", **metadata)

    def fail(self, error_code: str, message: str, **metadata: Any) -> dict[str, Any]:
        return self._terminal(
            "turn.failed",
            error={"code": error_code, "message": message},
            **metadata,
        )
