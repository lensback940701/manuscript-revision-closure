# Standalone 0.6.4 多阶段 Harness 实现与边界审计

## 裁决

0.6.1 已实现上一轮建议的领域专用多阶段架构：确定性 intake、模型上下文预算、十维整稿
coverage、重新读取全文的 root-cause adjudication、coverage SHA-256 绑定、跨阶段矛盾门和
既有确定性 reducer。它不再是“一次模型调用 + JSON 合法性检查”的薄运行器。

它仍不是 Codex 通用 agent harness 的复刻：没有 Shell、文件编辑、浏览器、搜索、子代理或
长期记忆。这些能力被原 Skill 明确排除，不属于当前功能的缺失。

## 当前执行链

1. **Immutable document read**：读取 TXT/Markdown/HTML/DOCX/文本层 PDF，计算文件与
   语义文本 SHA-256，拒绝静默截断；
2. **Thin local technical preflight**：`mrc-local-technical-preflight-1.0` 仅验证文件读取/转换、非空文本、声明上限和 provider/model 配置；`mrc-format-advisory-1.0` 可 best-effort 识别标题、章节、顺序、ATX/Setext、编号与 front matter，但其任何结果都不能改变 coverage routing、machine verdict 或 hold。
3. **One-run transmission consent**：`mrc-provider-transmission-consent-1.0` 默认拒绝，绑定当前 artifact SHA-256、provider 和 model，并在消费后失效；取消为用户未授权状态，API=0。
4. **Semantic basis inside coverage**：第一次 `mrc-whole-manuscript-coverage-3.0` 同时返回 `mrc-semantic-manuscript-basis-1.0` 的 SUFFICIENT/INSUFFICIENT、有限 reason codes 与脱敏说明；不得因标题或章节格式判不足，且不增加第三次全文请求。
   Introduction、Methods、Results、Discussion、Conclusion 或 References 等章节重复消费，但不强制
   H1。结构不完整即 `UNASSESSED`，不调用模型。
   S 系列、数字、中文章标题和无编号结构均可成立；编号缺失、混用、跳号或 major 层级跳跃
   只生成非阻断 `HEADING_NUMBERING_STYLE_REVIEW`，不改变 verdict 或 hold；
3. **Model-specific context budget**：根据所选提供商/模型登记的上下文窗口，保守估算完整
   prompt、Skill 合同和输出余量；不足时不截断全文；
4. **Coverage pass**：模型读取完整全文，对以下十个维度各评估一次：
   contribution、whole-paper argument、theory/concepts、methods/design、evidence/analysis、
   rivals/negative findings/limitations、section roles/coherence、claim ceiling/scope、
   evidence status/provenance、revision/submission boundary；
5. **Coverage validator**：本地检查维度集合精确匹配、无重复、assessed/status 合法、每维肯定性充分性与有限 reason code 自洽、候选列表与 `POTENTIAL_MATERIAL_ROOT_CAUSE` 行完全一致、hold codes 和保护不变量有限合法；
6. **Canonical binding**：对验证后的 coverage 使用 UTF-8 canonical JSON 计算 SHA-256；
7. **Dynamic schema definition gate**：`mrc-dynamic-adjudication-schema-3.0` 将 coverage candidate 数量绑定为 `minItems`，以 canonical dimension 总数作为有限 `maxItems`，并始终使用非空 canonical enum；因此零候选可返回 0..N 个独立补充而不生成 `enum: []`。`mrc-schema-definition-lint-1.0` 在 dispatch 前递归拒绝空/重复 enum、required/properties 错配、非法 min/max 与不支持关键字；失败为本地 `SCHEMA_DEFINITION_INVALID`，该阶段物理请求为 0；
8. **Independent adjudication pass**：同一模型在独立第二次调用中重新读取全文，同时接收有限 coverage。每个 coverage candidate 必须由一个 row 处理；第二阶段还可添加 canonical、已观察、可定位且非臆测的 `INDEPENDENT_ADDITION`，并显式登记 `coverage_disagreement`。被驳回的 coverage candidate 必须给出有限 disposition reason，不能靠无解释的全 false 消失；
9. **Candidate lower-bound + contradiction gate**：本地独立重算 digest，拒绝 stale hash、漏候选、重复认领、未知维度、不可定位/臆测补充、origin/disagreement 自相矛盾、coverage hold 被丢弃或保护不变量失败却没有候选/补充维度；
10. **Affirmative STOP gate**：`mrc-affirmative-stop-gate-1.0` 要求 coverage 与 adjudication 对 contribution、whole-paper argument、theory/concepts、methods/design、evidence/analysis、section roles/coherence 均明确肯定充分，且没有未解决材料问题。真实 scope/evidence-status/rival/limitation 不是自动 revision，但谨慎或空候选也不是 STOP 证明；
11. **Deterministic reducer**：只有上述门全部通过，才把有限裁决交给既有
   `scripts/closure_state.py` 生成 Closure Card 和最小收据；
12. **Optional interpretation**：核心裁决冻结后才进行第三次可选调用，生成中文公开解读，
    不参与重判。

## 请求等待与不可重发合同

Kimi 的 coverage 默认单次等待 300 秒，adjudication 与 interpretation 默认 900 秒；其他
已登记组合默认 180 秒。CLI 的显式 `--timeout` 可覆盖这些默认值。coverage、adjudication、
presentation repair 与 interpretation 均只允许一次物理 HTTP attempt；429、502、503、504、
socket/read timeout 与网络状态不明均不得自动重发全文。这一合同不改变输出 token 余量，
也不通过截短全文或压缩裁决来换取更短运行时间。

## 私有状态与公开收据

完整 coverage rows 仅存在于单次进程内，并只作为第二 pass 的有限输入；不会写入事件、公开
JSON、最小收据或解读文件。公开 runtime 只保留：

- local technical preflight、heading count、major level 与非阻断格式 advisories；
- 每次运行 consent 状态及其 hash/provider/model 绑定（不含 key 或全文）；
- coverage semantic basis 状态、有限 reason codes 与脱敏说明；

本地技术预检和逐次 consent 已通过后的 provider、schema、binding 或 contradiction 失败使用
`mrc-technical-hold-receipt-1.0`。Closure Card、machine receipt、minimal receipt、GUI 和保存 JSON
绑定同一 `TECHNICAL_EXECUTION_HOLD`、failed stage 与下一步；真正结构不完整仍使用
`INSUFFICIENT_WHOLE_MANUSCRIPT_BASIS`。技术 `UNASSESSED` 收据不具备稳定 STOP 复用资格。
- 每个阶段的估算输入、上下文上限和输出余量；
- coverage 合同版本、维度计数和 canonical digest；
- adjudication digest binding 与 contradiction gate 的 PASS/HOLD；
- 各次 API 的 token usage、模型和尝试次数。

因此，用户能验证流程确实走过各门，但不会获得详细私有审稿记录。

## 已闭合的旧缺口

- 不再只依赖“用户勾选完整 + 最小字符数”；
- 不再把完整稿件直接交给一次不可验证的 STOP/REOPEN 输出；
- 不再缺少模型级上下文预算；
- 不再允许空 root-cause 输出在 coverage 已发现候选时静默 STOP；
- 不再允许第二 pass 丢弃第一 pass 的 evidence/submission holds；
- 不再把可选中文解读误当作核心 verifier；
- 不再用输出 token 数或运行时间充当审阅充分性的代理指标。

## 仍然存在的诚实边界

1. **同模型独立 pass，而非异构双模型复核。** 第二 pass 使用同一用户选择的提供商与模型，
   以隔离 prompt、重新读取全文和 hash binding 获得过程独立性，但不是不同模型间的共识；
2. **token 预算是保守估算。** 未引入三家专有 tokenizer；估算不足会 fail-closed，但不是账单级
   精确 token 预测；
3. **结构识别是领域规则。** 标题、摘要、结论、参考文献使用中英文常见 heading typology；
   极不常见的标题写法可能被诚实判为 `UNASSESSED`；
4. **semantic truth 仍由 LLM 判断。** 本地 verifier 能验证完整性、绑定和矛盾，不能像事实核验
   Skill 那样证明论文事实为真；
5. **不替代同行评审或投稿授权。** 本程序只判断是否应停止通用 AI 改稿。

这些限制与当前功能边界一致，不需要通过加入搜索、改稿、第三方工具或自动投稿来“补齐”。
