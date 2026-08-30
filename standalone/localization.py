"""Deterministic language rendering for standalone public card constants."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


REASON_ZH = {
    "A valid prior closure decision exists for the same manuscript, and no legal invalidation event was supplied.":
        "同一稿件已有有效的修订截止裁决，且本次没有提供合法的失效事件。",
    "A reliable whole-manuscript cutoff cannot be made from the supplied current basis.":
        "依据当前提供的材料，无法可靠判断整稿是否应当停止通用改稿。",
    "Provider transmission was not authorized for this run; no manuscript text was sent and no manuscript or technical judgment was formed.":
        "本次运行未获得向提供商发送全文的授权；未发送稿件，也未形成稿件判断或技术 HOLD。",
    "The first coverage pass found that the supplied text is materially only a fragment or excerpt, so no whole-manuscript revision-closure verdict was formed.":
        "第一次 coverage 判断所提供文本在实质上仅为片段或节选，因此未形成整稿修订截止裁决。",
    "The first coverage pass found that substantive manuscript material needed for a whole-manuscript revision-closure judgment is missing.":
        "第一次 coverage 判断缺少整稿修订截止判断所需的实质稿件材料。",
    "The first coverage pass found too little substantive argument, method, evidence, or analysis for a whole-manuscript revision-closure judgment.":
        "第一次 coverage 判断当前文本的实质论证、方法、证据或分析不足以支持整稿修订截止判断。",
    "The first coverage pass found that the extracted text is too unreadable or corrupted for a whole-manuscript revision-closure judgment.":
        "第一次 coverage 判断提取文本过于不可读或损坏，无法支持整稿修订截止判断。",
    "The first coverage pass could not establish a sufficiently clear whole-manuscript identity or scope for revision closure.":
        "第一次 coverage 无法确认足够清晰的整稿身份或范围，因此未形成修订截止裁决。",
    "The first coverage pass found a substantive whole-manuscript basis gap, so no revision-closure verdict was formed.":
        "第一次 coverage 发现实质性的整稿基础缺口，因此未形成修订截止裁决。",
    "Local technical preflight passed, but the machine assessment did not form because a bounded technical execution stage failed.":
        "本地技术预检已通过，但限定技术执行阶段失败，机器裁决未形成。",
    "A central material root cause remains capable of affecting the manuscript's contribution, validity, or whole-paper coherence.":
        "稿件仍存在一个可能影响核心贡献、有效性或全文一致性的中心性实质根因。",
    "A local material problem remains, but its expected repair benefit supports one strictly bounded round.":
        "稿件仍有一个局部实质问题，预期修复收益足以支持一次严格限定的修订。",
    "No observed material root cause justifies reopening substantive revision; remaining holds are separate from the revision cutoff.":
        "未观察到足以重新启动实质修订的重大根因；其余 hold 与修订截止判断分开处理。",
}

NEXT_ZH = {
    "Keep the prior closure decision; do not start a new generic AI review without a legal invalidation event.":
        "保留既有截止裁决；没有合法失效事件时，不得启动新一轮通用 AI 审阅。",
    "Do not start another generic AI revision; address any listed evidence or submission hold separately if authorized.":
        "不要启动新一轮通用 AI 改稿；如获授权，仅分别处理已列明的证据或投稿 hold。",
    "Provide one complete, identifiable current manuscript and the basis needed for a whole-manuscript assessment; this lane does not rewrite bounded material.":
        "请提供一份身份明确、完整且为当前版本的稿件及整稿判断所需依据；本流程不改写局部材料。",
    "Resolve the reported technical execution failure, then explicitly start one new assessment; do not treat this receipt as a manuscript verdict.":
        "请先处理已报告的技术执行失败，再由用户明确发起一次新评估；不得把本收据当作稿件裁决。",
    "Explicitly confirm provider transmission for one new run after verifying the bound file hash, provider, and model; cancellation sends nothing.":
        "核对绑定的文件哈希、提供商和模型后，仅为一次新运行明确确认全文外发；取消不会发送任何内容。",
    "Authorize one bounded revision round only, then rerun closure; this lane does not execute the round.":
        "仅授权一次限定修订，完成后重新运行截止判断；本流程不执行该轮修订。",
    "Obtain a separately authorized substantive review/revision workflow; this closure lane does not edit the manuscript.":
        "另行授权并进入实质审阅或修订流程；本截止判断流程不修改稿件。",
}

REWRITE_SUFFIX_EN = " The request to rewrite is outside this read-only lane."
REWRITE_SUFFIX_ZH = " 改写请求超出本只读流程的权限边界。"
PARKED_NOTE_EN = (
    "These are not reasons to reopen the current manuscript. Reconsider them only if a new reviewer, "
    "journal requirement, evidence conflict, or author decision changes the task."
)
PARKED_NOTE_ZH = "这些不是重新打开当前稿件的理由。只有新的审稿人要求、期刊要求、证据冲突或作者决定改变任务时，才重新考虑它们。"


def localize_closure_card(card: Mapping[str, Any], language: str) -> dict[str, Any]:
    """Render only fixed public prose; model-authored fields are validated elsewhere."""

    result = deepcopy(dict(card))
    if language != "zh":
        return result
    reason = result.get("Reason")
    if reason not in REASON_ZH:
        raise ValueError("public card reason has no registered Chinese rendering")
    result["Reason"] = REASON_ZH[reason]
    next_action = result.get("Next permitted action")
    rewrite_suffix = isinstance(next_action, str) and next_action.endswith(REWRITE_SUFFIX_EN)
    base_action = next_action[: -len(REWRITE_SUFFIX_EN)] if rewrite_suffix else next_action
    if base_action not in NEXT_ZH:
        raise ValueError("public card next action has no registered Chinese rendering")
    result["Next permitted action"] = NEXT_ZH[base_action] + (REWRITE_SUFFIX_ZH if rewrite_suffix else "")
    if result.get("Parked opportunities note") == PARKED_NOTE_EN:
        result["Parked opportunities note"] = PARKED_NOTE_ZH
    return result
