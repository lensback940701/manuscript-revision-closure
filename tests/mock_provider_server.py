"""One-request local Chat Completions server for packaged EXE smoke tests."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from standalone.harness import (
    AFFIRMATIVE_STOP_DIMENSIONS,
    COVERAGE_CONTRACT_VERSION,
    COVERAGE_DIMENSIONS,
    canonical_digest,
)

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
    "protected": ["保持论点上限和可见的替代解释。"],
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
    "pre_submission_checklist": [
        "人工核对匿名化要求。",
        "人工核对作者信息与声明。",
        "人工核对图表和版权状态。",
    ],
    "optional_micro_adjustments": [],
    "report_limitations": ["没有执行外部事实核验。", "不能替代作者与同行评审判断。"],
    "boundary_note": "本解读不是事实认证、同行评审替代品或投稿授权。",
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        model = request.get("model")
        if self.path != "/chat/completions" or not isinstance(model, str) or not model:
            self.send_error(400)
            return
        messages = request.get("messages", [])
        system_text = messages[0].get("content", "") if messages and isinstance(messages[0], dict) else ""
        if "INTERPRETATION AGENT CONTRACT" in system_text:
            state = INTERPRETATION_STATE
        elif "whole-manuscript coverage stage" in system_text:
            state = COVERAGE_STATE
        else:
            state = ADJUDICATION_STATE
        response = json.dumps(
            {
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": json.dumps(state, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    request_count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    server = HTTPServer(("127.0.0.1", port), Handler)
    for _ in range(request_count):
        server.handle_request()
    server.server_close()


if __name__ == "__main__":
    main()
