"""Command-line interface for the standalone executable."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .assessor import RunOptions, analyze_manuscript
from .events import EventSink


def load_prior_receipt(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    source = Path(path).expanduser().resolve(strict=True)
    if source.stat().st_size > 64 * 1024:
        raise ValueError("prior receipt exceeds 64 KiB")
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("prior receipt must be one JSON object")
    if "minimal_receipt" in value:
        nested = value["minimal_receipt"]
        if not isinstance(nested, dict):
            raise ValueError("saved result minimal_receipt must be one JSON object")
        value = nested
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manuscript-revision-closure",
        description="Read-only whole-manuscript revision closure using DeepSeek, Kimi, or Gemini.",
    )
    parser.add_argument("manuscript", help="Complete current manuscript: TXT, Markdown, HTML, DOCX, or text-layer PDF")
    parser.add_argument("--provider", choices=("deepseek", "kimi", "gemini"), default="deepseek")
    parser.add_argument("--model", help="Override the provider model; environment model variables remain supported")
    parser.add_argument(
        "--reasoning",
        choices=("default", "none", "disabled", "enabled", "minimal", "low", "medium", "high", "max"),
        default="default",
        help="Provider/model-aware thinking control; unsupported combinations fail before the API request",
    )
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--identity", help="Stable manuscript identity; defaults to the input filename")
    parser.add_argument(
        "--confirm-complete",
        action="store_true",
        help="Legacy user statement retained for receipt compatibility; it does not gate provider routing",
    )
    parser.add_argument(
        "--consent-to-provider-transmission",
        action="store_true",
        help=(
            "Explicitly authorize this invocation to send the selected full text to the bound provider/model; "
            "default is refusal and every new invocation must confirm again"
        ),
    )
    parser.add_argument("--prior-receipt", help="Optional prior minimal receipt JSON")
    parser.add_argument("--output", help="Explicitly write the public card and minimal receipt JSON to this path")
    parser.add_argument("--event-log", help="Explicitly write privacy-bounded lifecycle events as JSONL")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Override every stage timeout in seconds; otherwise provider/stage defaults are used",
    )
    parser.add_argument(
        "--transient-retries",
        type=int,
        choices=(0,),
        default=0,
        help="Compatibility flag; 0 is the only accepted value because full requests are never auto-retried",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def run_cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        prior = load_prior_receipt(args.prior_receipt)
        event_path = Path(args.event_log).expanduser().resolve() if args.event_log else None
        sink = EventSink(jsonl_path=event_path)
        result = analyze_manuscript(
            RunOptions(
                manuscript_path=Path(args.manuscript),
                provider=args.provider,
                model=args.model,
                reasoning_option=args.reasoning,
                output_language=args.language,
                manuscript_identity=args.identity,
                confirm_complete_current_manuscript=args.confirm_complete,
                prior_receipt=prior,
                timeout_seconds=args.timeout,
                transient_retries=args.transient_retries,
                provider_transmission_consent=args.consent_to_provider_transmission,
            ),
            event_sink=sink,
        )
        rendered = json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        if args.output:
            output = Path(args.output).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
        print(rendered)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
