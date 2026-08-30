# Manuscript Revision Closure Standalone 0.6.4：原生事务与状态完整性审计

## 1. 锁定范围

- 目标仓库：`lensback940701/manuscript-revision-closure`
- 0.6.4 唯一施工基线：`ed559d73193a59e475807fd9016a6b034ed906f0`
- 0.6.4 修复分支：`repair/0.6.4-intake-technical-hold-diagnostics`
- Standalone：`0.6.4`
- Skill contract：`0.2.1`
- Codex 固定参考：`d5caceccb1ee5bf94c081b995575ce4860e0912b`
- 实现阶段检查的 Codex main：`6be2a6ca952ac9f70676ce4dd07fda27175aa9dd`

0.6.3 的 provider/schema/state transaction 保持为正向 donor；0.6.4 以 `mrc-local-technical-preflight-1.0`、`mrc-provider-transmission-consent-1.0` 和 `mrc-semantic-manuscript-basis-1.0` 收敛全文进入路径，同时保留 technical-HOLD、bounded provider error detail 与 Skill `0.2.1` 的业务裁决边界。

## 2. Failure-first

0.6.1 的真实失败链为：

```text
provider completion
→ adjudication usage 进入局部变量
→ coverage digest binding PASS
→ machine schema validation PASS
→ Chinese presentation validation FAIL
→ contradiction gate 未运行
→ AnalysisResult 未形成
→ bounded result 无法携带既成 machine fact 与 usage
```

根因不是 Kimi timeout，而是 machine adjudication、usage、presentation text 与语言验证被置于同一个全成或全败事务。0.6.2 将这些边界直接写入 `standalone/assessor.py` 主流程。

## 3. 原生执行顺序

```text
immutable manuscript read
→ thin local technical preflight（格式仅 advisory）
→ consume one-run file-hash/provider/model consent
→ coverage provider completion + semantic manuscript-basis decision
→ commit coverage bounded receipt and usage
→ coverage validation
→ if basis INSUFFICIENT: stop with coverage=1/adjudication=0/no machine verdict/no presentation source
→ if basis SUFFICIENT: continue
→ adjudication provider completion
→ commit adjudication bounded receipt and usage
→ coverage digest binding
→ machine schema validation
→ contradiction gate
→ freeze canonical machine state and SHA-256
→ deterministic reducer
→ presentation language validation
→ optional one-request presentation repair
→ presentation verifier and hash parity
→ exactly one terminal event
→ optional user-selected interpretation only when terminal_status=PASS
```

任何 presentation failure 都不会回滚 contradiction gate 前已经完成并验证的事实。任何 machine verifier failure 都返回 machine HOLD，不伪装为 PASS，同时保留已经取得的 provider usage。

## 4. 状态模型

```text
machine_status = SUCCEEDED | HOLD
presentation_status = NOT_STARTED | PASS | HOLD
terminal_status = PASS | HOLD
recoverability = NONE | PRESENTATION_REPAIR
machine_provider_outcome = NOT_CALLED | SUCCEEDED | REJECTED | UNKNOWN
presentation_provider_outcome = NOT_CALLED | SUCCEEDED | REJECTED | UNKNOWN
usage_status = COMPLETE | PARTIAL | UNKNOWN
```

`machine_provider_outcome` 与 `presentation_provider_outcome` 分开记账。没有 usage 时使用 `{}` 与 `UNKNOWN`，不以虚构数值 0 表示未知。

## 5. Provider completion 与 usage

`standalone/providers.py` 在每个物理 HTTP attempt 发出前建立 bounded receipt，并在结果明确后更新。`mrc-provider-request-transaction-2.0` 可包含 HTTP status 及经 `mrc-provider-error-detail-1.0` 处理的 provider status/code/单行限长 detail。确定性 sanitizer 遮蔽 secret、Authorization、请求体、稿件、prompt、hidden diagnostics 与 chain-of-thought 语境；原始 response 不持久化。

所有传给 provider 的 JSON schema 还先通过 `mrc-schema-definition-lint-1.0`。lint receipt 只保留版本、schema hash、bounded failed path/error 与 `request_dispatched=false`，不保留 schema 原文、prompt 或稿件。零候选 adjudication 使用 `minItems=0`、有限 canonical 上限与非空 canonical dimension enum，兼容 Moonshot strict schema，同时允许真正独立的第二阶段恢复 grounded coverage miss。candidate binding 与 affirmative STOP gate 均在冻结 machine state 前完成；presentation transaction 不能改变这些裁决字段。

- coverage、adjudication、presentation repair 与 interpretation 均强制 `max_transient_retries=0`；
- Kimi presentation repair timeout 为 900 秒；
- timeout、socket/read error、连接中断、429/502/503/504 或其他 provider error 均不会自动重发全文请求；
- presentation output budget由 provider ceiling、context limit、估算输入、safety margin及有限 schema 最大输出共同形成，不使用固定 4096/5000/8192 cap。

## 6. Machine state 冻结

contradiction gate 通过后冻结：

- canonical coverage digest；
- material root causes；
- evidence/submission hold codes；
- authoritative protected、parked opportunities 与 Lite suggestions；
- deterministic verdict inputs；
- machine-state canonical SHA-256。

presentation 前后独立重算 machine digest。仅当 before 与 after 完全相等时，display attachment 才可交付；否则返回 `INTEGRITY_HOLD`，不交付变化后的对象。

## 7. Protected 双层结构

每个 authoritative public item绑定：

- canonical path；
- 原始 index；
- stable item ID；
- source text；
- source SHA-256；
- source binding digest；
- protected cardinality 与 protected binding digest。

repair response 只能返回相同顺序的 `id + source_sha256 + display_text`。本地验证 exact cardinality、order、ID、duplicate、missing、extra、source hash 与 machine hash。hash 证明来源、身份、数量、顺序和 machine state 不变；它不宣称数学证明自然语言翻译完全等价，因此 bounded receipt 同时保留 authoritative source binding 与 display binding。

## 8. Presentation-only request 边界

request 只包含已经属于公开投影的：

- protected；
- parked opportunities；
- Lite suggestion 三个公开字段；
- canonical path；
- stable item ID；
- source SHA-256；
- source binding digest；
- target language。

request 不包含稿件、摘要、coverage rows、root-cause booleans、hold 私有判断过程、verdict、hidden diagnostics、原始 provider response、早期 prompt、chain-of-thought、API key 或 verifier 私有材料。

## 9. 中文展示合同

语言判断是确定性的，不调用另一个 LLM。对每个公开文本：

1. 至少需要四个 CJK 字符；
2. 排除 URL、DOI、email、模型编号、全大写缩写和 code-like identifier 后，CJK 在中文/拉丁语义字符中的比例不得低于 18%；
3. 五个以上拉丁 token 而 CJK 少于八个时 fail closed。

因此完整英文句子只夹一两个汉字会 HOLD；中文句法中保留 DOI、Kimi K2.6、英文缩写、期刊名、理论术语和专有名词可以 PASS。该 heuristic 只适用于 bounded public natural-language fields，不适用于 schema keys 或 hold codes。

## 10. 单一 terminal event

`EventSink` 为每个 request生成稳定 `request_id` 与 `terminal_event_id`，使用原子、幂等 terminal ownership。orchestrator 是唯一发出 `turn.completed` 或 `turn.failed` 的组件；GUI 按 `request_id + terminal_event_id` 去重，只消费该终态，不再从 worker 重复生成第二个 terminal。

## 11. 持久化与隐私

`committed_in_memory` 不等于自动落盘。当前运行状态与 bounded public receipt 可保存状态、outcome、usage、machine/presentation digest、binding、HOLD code、call count、timeout/retry/budget receipt。程序不会自动落盘稿件、完整模型响应、private coverage rows、prompt、hidden diagnostics、chain-of-thought 或 API key。只有 CLI `--output` 或 GUI 保存操作会写出 bounded public result。

## 12. 上游语义提取边界

Codex 仅作为 provider completion、usage 不可逆记账、timeout ambiguity、structured output、postprocessing 分层、单一 terminal event 与敏感信息隔离的语义参考。本实现是 Python 语义重写，没有复制 Codex Rust 源码，也没有引入 Shell、浏览器、搜索、文件修改工具、审批、多代理、长期记忆或通用 agent runtime。
