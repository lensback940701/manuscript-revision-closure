# MRC Standalone 0.6.4 Intake 与技术 HOLD 诊断修复

## 冻结范围

- 唯一基线 commit：`ed559d73193a59e475807fd9016a6b034ed906f0`
- 唯一基线 tree：`387d9274019c6096112af74c995772dd2d78cf6b`
- 候选分支：`repair/0.6.4-intake-technical-hold-diagnostics`
- Standalone：`0.6.4`
- Skill：`0.2.1`（未修改）
- 本轮不授权 commit、main fast-forward、push 或 Release。

## Bounded repair

1. `mrc-technical-hold-receipt-1.0` 把 intake PASS 后的 provider、schema、binding、candidate 与 contradiction failure 映射为 `TECHNICAL_EXECUTION_HOLD`。Closure Card、machine receipt、minimal receipt、GUI snapshot 与保存 JSON 共享 failed stage 和下一步；该 `UNASSESSED` receipt 不能作为 stable STOP 复用。
2. `mrc-provider-request-transaction-2.0` 与 `mrc-provider-error-detail-1.0` 保留有限 provider status/code/detail。detail 单行化、最长 240 字符并确定性脱敏；原始 response、请求体、全文、prompt、Authorization、key、hidden diagnostics 与 chain-of-thought 不持久化。
3. 架构迁移将本地门改为 `mrc-local-technical-preflight-1.0`：仅文件可处理、非空、未超限和配置可用可阻断；原 title/heading/front-matter parser 降级为 `mrc-format-advisory-1.0`，其结果在数据流上不能影响 coverage routing、machine verdict 或 hold。
4. `mrc-provider-transmission-consent-1.0` 要求每次运行重新显示并确认文件路径/名称/SHA-256、provider、model 和潜在计费说明；绑定变化、取消、重复消费或新运行均 API=0。
   取消在 API key/base URL 配置读取之前生效并返回用户未授权；只有用户确认后，缺失或非法 provider 配置才形成 `provider_configuration` technical HOLD，仍为 API=0。
5. `mrc-semantic-manuscript-basis-1.0` 合并进第一次 `mrc-whole-manuscript-coverage-3.0`。SUFFICIENT 继续 adjudication；INSUFFICIENT 精确形成 coverage=1/adjudication=0/no verdict/no presentation，usage/cost 不丢失；技术错误仍走独立 technical-HOLD。
4. 编号缺失、混用、不连续或 major 层级跳跃仅生成非阻断低风险 `HEADING_NUMBERING_STYLE_REVIEW`，不得形成 evidence/submission hold、material root cause 或 verdict 变化。
5. timeout、网络状态不明、429、502、503 与 504 仍只允许一次物理 attempt，不自动重发全文。

## Acceptance-harness closure

- 构建后的 basis-insufficient `KeyError` 被保留为 failure-first 历史；它来自验收脚本直接索引 NOT_FORMED 可选字段，不是冻结产品失败。
- `assert_not_formed_value` 仅允许字段缺失或显式 `null`，任何已形成值均失败；SUFFICIENT/STOP 的必需 machine/presentation 字段没有放宽。
- basis-insufficient 费用断言改在真实提供 `task_cost` 的冻结 GUI 表面完成；CLI 核心 receipt 继续验证一次 coverage、零 adjudication 和完整 usage。
- 本 acceptance-harness 续作未修改产品源码、未调用 builder、未改写 EXE/sidecar/BUILD receipt。

## Zero-candidate Moonshot schema source repair

- failure-first 用 synthetic zero-candidate coverage 证明旧 builder 生成 `enum: []`，Kimi-shaped local mock 返回与历史 live HTTP 400 相同的 Moonshot schema 错误；没有调用真实 API。
- 历史 `mrc-dynamic-adjudication-schema-2.0` 曾对零候选生成 `minItems=0`、`maxItems=0`；该状态作为已完成的 Moonshot `enum: []` 修复证据保留，不再代表当前语义上限。
- `mrc-schema-definition-lint-1.0` 在 dispatch 前递归检查有限 schema subset；失败使用 `SCHEMA_DEFINITION_INVALID`、bounded path/error 与 `request_dispatched=false`。
- 独立预构建曾发现动态 adjudication schema 构造阶段的 lint 异常会在结构化 HOLD 形成前冒泡；该 failure-first 保留。本轮窄修在构造边界捕获异常，保留已完成 coverage 的一次 attempt 与 usage、保持 adjudication=0，并返回 `adjudication_schema_definition` 技术 HOLD。修后 focused 为 49/49，full source 为 350/350。
- 修后独立只读预构建 PASS：0/1/N truth table、DeepSeek/Kimi/Gemini 共 9 个 mock 请求、7 类无效 schema 的 API=0、consent、basis、usage 与 privacy 均通过；验收者未编辑仓库、未构建、未联网或访问真实稿件/key。
- 上轮独立 live semantic false-STOP evidence 当时属于未授权主题，因此准确停在构建前；本轮获得单独授权后才进入以下窄修。

## Semantic false-STOP bounded repair

- failure-first 以完全 synthetic state 复现三条单向通道：零候选 `maxItems=0`、coverage miss 被 exact ceiling 拒绝、STOP 没有双阶段肯定性充分性字段；修前 3 tests 得到 2 failures + 1 error，真实 API=0。
- `mrc-whole-manuscript-coverage-3.0` 为每维增加肯定性充分性与有限 reason code。谨慎、保护 claim ceiling、scope、source status、rivals、negative findings 和真实 limitations 不是材料性失败；但它们也不能代替对贡献、方法可评估性与论证闭合的肯定性证明。
- `mrc-root-cause-adjudication-2.0` 与 `mrc-candidate-lower-bound-independent-additions-1.0` 把 coverage candidates 改为必须处理的下限。第二阶段可增加 canonical、已观察、可定位、非臆测且非重复的 `INDEPENDENT_ADDITION`；每项登记 origin、coverage disagreement 与有限 disposition reason。漏候选、未知/重复/不可定位/臆测补充、hash 错配、自相矛盾或无解释全 false 均 fail closed。
- `mrc-dynamic-adjudication-schema-3.0` 使用 candidate count 作为 `minItems`、canonical dimension count 作为有限 `maxItems`，enum 始终为非空 canonical set；保留 `mrc-schema-definition-lint-1.0` 的 API=0 invalid-schema gate。
- `mrc-affirmative-stop-gate-1.0` 要求 coverage 与 adjudication 对 contribution、whole-paper argument、theory/concepts、methods/design、evidence/analysis、section roles/coherence 均明确肯定充分且没有材料问题。local cause 导向 ONE_BOUNDED_ROUND，central cause 导向 REOPEN_SUBSTANTIVE_REVISION；空 candidates 不再机械导向 STOP，也不会被机械改成 REVISE。

## Declared candidate paths

```text
CHANGELOG.md
CHANGELOG.zh-CN.md
README.md
README.zh-CN.md
SKILL.md
STANDALONE.zh-CN.md
build_exe.ps1
docs/HARNESS_EQUIVALENCE_AUDIT.zh-CN.md
docs/MRC_0.6.4_FAILURE_FIRST_RECEIPT.json
docs/MRC_0.6.4_INTAKE_TECHNICAL_HOLD_DIAGNOSTICS_REPAIR.zh-CN.md
docs/NATIVE_PRESENTATION_TRANSACTION_AUDIT.zh-CN.md
scripts/closure_state.py
standalone/__init__.py
standalone/assessor.py
standalone/cli.py
standalone/harness.py
standalone/localization.py
standalone/presentation_transaction.py
standalone/pricing.py
standalone/prompting.py
standalone/providers.py
standalone/web_gui.py
tests/mock_provider_server.py
tests/run_frozen_acceptance_0_6_3.py
tests/run_frozen_acceptance_0_6_4.py
tests/test_closure_state.py
tests/test_harness.py
tests/test_mrc_0_6_3_provider_contract_state.py
tests/test_mrc_0_6_4_intake_technical_hold_diagnostics.py
tests/test_presentation_transaction_native.py
tests/test_pricing.py
tests/test_rc2_contract.py
tests/test_standalone_runtime.py
tests/test_web_gui.py
```

0.6.3 Goal 授权载体保持字节不变，明确排除在本轮候选路径之外。
