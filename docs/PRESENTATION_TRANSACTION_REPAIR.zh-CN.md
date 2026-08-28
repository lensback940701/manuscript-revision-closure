# Standalone 0.6.1：Codex Harness 上游对照、目标映射与 Presentation Transaction 修复审计

## 1. 审计锁定信息

- 目标仓库：`lensback940701/manuscript-revision-closure`
- 唯一实现基线：`ed679849762c11e3aabd848396c09b0adfdb6ab2`
- 基线 tree：`2770a92f47aab6d2ef931708f277773ca6d0996c`
- 修复分支：`repair/0.6.1-presentation-hold`
- Standalone 版本：`0.6.1`
- Skill 合同版本：`0.2.1`
- Codex 固定参考提交：`d5caceccb1ee5bf94c081b995575ce4860e0912b`
- 本轮最终上游检查锁定的 Codex `main`：`ec9620c231396895194329c410f3ec360b4cadef`

目标实现判断只以 exact target commit 为基线。未上传的 `release/`、`.build/`、`.venv/` 不在本轮源码审计对象中；构建脚本只作静态合同检查，不声称已经在本轮环境重新生成 Windows EXE。

## 2. Failure-first：真实反例及其事务边界

真实运行已经完成 Kimi `kimi-k2.6` 的两次核心调用：coverage 169.8 秒，`coverage_complete=true`，`dimension_count=10`；adjudication 约 150.4 秒；未超时、未重试。provider 返回 adjudication 后，本地抛出：

```text
requested Chinese output contains a non-Chinese public text value
```

exact target commit 的关键执行顺序为：

| 事实 | exact target commit 文件、符号和行号 | 基线语义 |
|---|---|---|
| provider completion 形成 | `standalone/providers.py:352-472`, `ChatCompletionClient.complete()` | `stream=False`；完整 HTTP JSON 返回后形成 `CompletionResult` |
| usage 取得 | `standalone/providers.py:473-517`, `_parse_payload()` | 从 provider JSON 的 `usage` 读取 token 字段 |
| coverage completion 记账 | `standalone/assessor.py:453-462` | coverage completion 返回后将 usage 加入 `usage_calls` |
| adjudication completion 记账 | `standalone/assessor.py:567-575` | adjudication completion 返回后将 usage 加入 `usage_calls` |
| coverage binding | `standalone/assessor.py:576-578`; `standalone/harness.py::validate_adjudication_binding()` | adjudication 已绑定 canonical coverage SHA-256 |
| machine schema validation | `standalone/assessor.py:578`; `validate_model_state()` 定义于 `168-232` | 已形成 exact finite model state |
| language validator | `standalone/assessor.py:579`; `_validate_model_output_language()` 定义于 `235-247` | 错误地位于 machine schema 与 contradiction gate 之间 |
| contradiction gate | `standalone/assessor.py:591-607`; `validate_cross_stage_consistency()` | 语言失败时不会运行 |
| reducer 与结果 | `standalone/assessor.py:608-627`; `_finish()` 定义于 `288-325` | 只有全部后处理通过才返回 `AnalysisResult` |
| 全局失败 | `standalone/assessor.py:628-638` | broad exception 调用 `sink.fail()` 后重新抛出 |
| GUI 重复失败 | `standalone/events.py::EventSink.fail()` 与 `standalone/web_gui.py::_analysis_worker()` 的 catch 路径 | event callback 已报告失败，worker 又调用 `state.fail()` |

因此，真实反例不是 provider timeout，也不是 adjudication 未返回，而是**已返回、已计费、已通过 binding/schema 的响应被一个后置 presentation 语言门回滚**。此外，语言门位于 contradiction gate 之前，使本应独立的本地 machine verifier 也没有机会完成。

## 3. Codex 上游能力对照六列表

| Codex 上游能力 | Codex 文件/模块/commit 证据 | 当前 standalone 对应实现 | 是否完整保留必要语义 | 当前偏差或缺陷 | 是否必须修复 |
|---|---|---|---|---|---|
| 请求生命周期和阶段状态机 | `codex-rs/core/src/tasks/mod.rs::SessionTask`, `Session::start_task`, `Session::on_task_finished`; fixed `d5cace...`, checked main `ec9620c...` | `standalone/assessor.py::analyze_manuscript`; `standalone/events.py::RunPhase/EventSink` | 基线部分保留 | provider、machine validation、presentation 被归入同一全成或全败事务 | 是 |
| provider 请求封装 | `codex-rs/codex-api/src/common.rs::ResponseEvent`; `core/src/session/turn.rs` | `standalone/providers.py::ChatCompletionClient.complete/_parse_payload` | 对 non-streaming 调用基本足够 | completion 已发生后，基线没有不可逆的 bounded completion receipt | 是 |
| timeout、取消和重试边界 | `core/src/responses_retry.rs::ResponsesStreamRetryState`; `protocol/src/error.rs::CodexErr::is_retryable` | timeout/URLError 不自动重发；只重试 429/502/503/504 | 核心语义保留 | presentation repair 必须复用同一 unknown-server-state 边界，不得重发全文 | 保留并测试 |
| streaming 与 non-streaming 响应等待 | `codex-rs/codex-api/src/common.rs::ResponseEvent::Completed` | standalone 固定 `stream=False` | 完整保留本业务所需语义 | 不需要引入 streaming；但完整响应返回必须先于后处理记账 | 是 |
| structured output 与 schema validation | `core/src/client_common.rs::Prompt.output_schema/output_schema_strict`; `codex-api/src/common.rs::TextFormat` | coverage/adjudication schemas + 本地 exact validators | machine 合同较完整 | public language 不是 machine verdict schema，应成为独立 presentation transaction | 是 |
| 模型输出解析和 fail-closed | `protocol/src/error.rs::CodexErr`; current-main ancestry中的 `4f2a1d...` 对不可用 bounded context fail closed | `parse_model_json`, `validate_*`, contradiction gate | machine verifier 自身完整 | fail-closed 被错误实现为抹除既成 completion/usage，而不是产生分层 HOLD | 是 |
| usage 在成功、合同失败、超时和异常路径中保存 | `session/turn.rs::ResponseEvent::Completed` 先记录 usage；`tasks/mod.rs::on_task_finished` 统一收尾；`2c4a957...` 传播 per-response usage metadata | 基线仅在正常 `AnalysisResult` 中交付 `usage_calls` | 基线不完整 | language/verifier 后置失败会使已取得 usage 无公开载体；未知 usage 不能写成 0 | 是 |
| 事件流、状态收据和错误传播 | `protocol.rs::TurnCompleteEvent{error}`, `TurnAbortedEvent`, `ErrorEvent`; `tasks/mod.rs::on_task_finished` | `EventSink` + GUI worker | 基线不完整 | 一个异常可产生 terminal phase、terminal event、worker failure 三种可见记录 | 是 |
| 工具/模型调用完成后本地 verifier 独立 | fixed `d5cace...` 的 host-owned completeness 语义；`session/turn.rs` 与 `tasks/mod.rs` 分离 | binding、contradiction gate、deterministic reducer | verifier 逻辑存在 | language validator 截断了 machine verifier；completion 与 verifier 未分层 | 是 |
| 敏感信息与公开结果隔离 | `protocol.rs::ErrorEvent` 对敏感 details 跳过持久化；公开协议与内部状态分离 | environment-only key；事件排除稿件、prompt、credentials | 基线基本完整 | repair 不能把 raw response、全文、coverage rows、diagnostics、prompt、CoT、key 自动落盘 | 必须保持 |
| 任务中断、部分成功和可恢复 HOLD | `TurnAbortedEvent`; `TurnCompleteEvent` 可携带 error；`e8b938...::ClassificationOutcome` | 基线有领域 `UNASSESSED`，可选 interpretation 有独立 hold | 部分保留 | 核心 machine 成功但 presentation 失败没有可恢复状态 | 是 |
| typed usage/provider outcomes | Codex typed error/retry categories及 completion metadata | 基线主要靠异常字符串和空 dict | 基线不完整 | 必须区分 `SUCCEEDED/REJECTED/UNKNOWN` 和 `COMPLETE/PARTIAL/UNKNOWN` | 是 |
| `protected` 双层语义 | Codex 不定义本领域字段；参考其 host-owned identity 与 public projection 分离 | `closure_state.py` 同一 `protected` 字符串既进入 decision 又直接进入 Closure Card | 基线不完整 | 既不能任意翻译后覆盖 machine source，也不能把它视为不可本地化代码 | 是 |

## 4. 最小修复实现

### 4.1 变更路径

- `standalone/__init__.py`：安装一次性 runtime transaction repair；保持 Standalone `0.6.1`。
- `standalone/runtime_repair.py`：新增分层事务、completion/usage bounded receipts、machine freeze、verifier HOLD、单一 terminal owner、optional interpretation guard。
- `standalone/presentation_repair.py`：新增一次有限 presentation-only repair、immutable item identity、source/protection bindings、exact repair schema 与 hash parity。
- `tests/test_presentation_transaction.py`：新增 failure-first 与八项 focused regression。
- `build_exe.ps1`：在 build receipt 中登记新增合同版本，并复制本审计文档。
- `docs/PRESENTATION_TRANSACTION_REPAIR.zh-CN.md`：本文件。

没有修改 `scripts/closure_state.py` 的四 verdict reducer、hold code 分类、Skill `0.2.1` 合同，也没有加入 Shell、浏览器、搜索、文件修改、子代理、长期记忆、审批或通用工具系统。

### 4.2 修复后的冻结顺序

`runtime_repair.install_runtime_repair()` 在 package 初始化时对 exact 0.6.1 assessor 的运行边界进行一次性绑定：

1. `_request_stage` 返回后只记录 bounded completion facts：stage、model、finish reason、usage；不记录 response content。
2. `validate_coverage` 保存经过验证的 in-memory coverage；不进入公开 receipt 的 rows。
3. `validate_adjudication_binding` 和 `validate_model_state` 正常 fail closed。
4. 原 `_validate_model_output_language` 仍执行；语言不合格被记录为 presentation defect，不再阻断 machine verifier。
5. `validate_cross_stage_consistency` 必须通过；此时 `machine_status=SUCCEEDED`，canonical machine-state SHA-256 冻结。
6. 只有 presentation 不合格时，调用一次 bounded presentation repair。
7. reducer 对 machine state 或其 display-only 深拷贝生成 Closure Card；machine hash 在 repair 前后必须相同。
8. transaction owner 最终只发布一个 `turn.completed` 或 `turn.failed` terminal event。

关键实现位置：

- `standalone/runtime_repair.py:32-51`：per-run in-memory trace。
- `53-117`：backward-compatible bounded result/status receipt。
- `119-169`：deferred terminal sink。
- `258-293`：machine receipt 与 protection binding。
- `338-475`：machine success、presentation PASS/HOLD、usage aggregation、hash parity。
- `478-562`：verifier failure 的 fail-closed `UNASSESSED` HOLD，保留 usage。
- `565-583`：terminal HOLD 时阻止可选 interpretation 在读取/发送全文前启动。
- `586-744`：一次性安装 wrappers 与唯一 terminal owner。

### 4.3 分层状态

公开 runtime 至少包含：

```text
machine_status = SUCCEEDED | HOLD
presentation_status = PASS | HOLD
terminal_status = PASS | HOLD
recoverability = NONE | PRESENTATION_REPAIR
provider_outcome = SUCCEEDED | REJECTED | UNKNOWN | NOT_CALLED
usage_status = COMPLETE | PARTIAL | UNKNOWN
```

`UNKNOWN` 是显式枚举。未知 usage 使用空对象加 `usage_status=UNKNOWN`；不得写入伪造的 `prompt_tokens=0`、`completion_tokens=0` 或 `total_tokens=0`。

### 4.4 三类持久化的严格区分

1. **in-memory committed state**：当前进程中不可逆记账 validated coverage、validated machine state、stage/model/finish reason/usage、machine hash 和 presentation receipt。原始 response content 仅作为局部解析输入，运行 receipt 不保留。
2. **bounded public receipt**：`AnalysisResult.as_dict()` 只公开 verdict/minimal receipt、status、usage、stage labels、coverage digest、machine/protection hashes、item IDs/count 和 persistence flags。CLI 只打印；GUI 只保存在内存。只有用户显式指定 `--output` 或点击保存时才写文件。
3. **raw provider response persistence**：始终为 false。程序不自动落盘完整模型响应、全文稿件、private coverage rows、hidden diagnostics、prompt、chain-of-thought 或 API key。

### 4.5 `protected` 的双层合同

`protected` 的 authoritative source 仍属于 validated machine state。`build_presentation_source()` 为每一项建立：

```text
item_id = SHA-256(canonical {path, source_text}) 的稳定前缀
path = protected[index]
source_digest_sha256 = 所有公开 source entries 的 canonical digest
protected_binding_digest_sha256 = protected entries 的 canonical digest
```

因此，重复文本位于不同 index 时仍拥有不同 identity；repair 不能合并或减少事项。repair response 只能返回 exact ID set 与 display text，本地 validator 检查同数、同 ID、无重复、目标语言合格。display 只写入 machine state 的深拷贝；authoritative source、数量、index、path、source digest 和 protection binding 均不变。

SHA-256 不能单独证明自然语言翻译在哲学意义上的完全等价。本修复采取更严格的权威分层：原 machine source 始终是语义权威，display translation 只是绑定到该 identity 的非权威呈现；任何无法通过 exact identity/cardinality/language/hash 合同的输出只能进入 HOLD，不能替换 machine source。

### 4.6 Presentation-only repair 输入边界

`standalone/presentation_repair.py:125-172` 仅提取：

- `protected[i]`；
- `parked_opportunities[i]`；
- `lite_suggestions[i].Direction`；
- `lite_suggestions[i].Why it matters`；
- `lite_suggestions[i].What to protect`。

repair request 不包含 manuscript、coverage rows、root-cause booleans、hold codes、verdict、evidence、hidden diagnostics、原 prompt、CoT 或 key。`repair_presentation()` 最多执行一个逻辑 repair transaction，使用 finite schema、8192-token ceiling 和既有 ambiguity-aware provider retry contract；不会触发 coverage/adjudication 重跑，也不会递归 self-repair。

### 4.7 GUI 单一终态

基线的重复失败来自两条并行终态路径：EventSink callback 与 worker catch。`_DeferredTerminalSink` 只转发 progress/item events，并拦截原 assessor 的 `complete/fail`；外层 transaction owner 在状态冻结后发布唯一 canonical terminal event。对不可恢复异常，event-log terminal 在临时屏蔽 GUI callback 的情况下写入，随后 GUI worker 只产生一次 visible failure。对 presentation HOLD，核心返回 bounded result，GUI 形成一次 `completed_with_interpretation_hold`；guard 保证不会出现第四次、携带全文的 interpretation API 调用。

## 5. Focused regression

`tests/test_presentation_transaction.py` 覆盖：

1. 原 language validator 在 transaction 外仍稳定抛出 exact failure-first message，证明没有删除中文校验。
2. English machine public fields 触发且只触发一次 bounded repair；总 API 调用为 coverage、adjudication、repair 三次。
3. repair request 不含全文 marker、coverage dimensions 或 coverage contract。
4. repair 前后 machine-state hash 完全相同；protected item count、IDs 和 binding digest 保留。
5. repair contract failure 返回 `machine=SUCCEEDED / presentation=HOLD / terminal=HOLD / recoverability=PRESENTATION_REPAIR`，并保留 repair usage；raw invalid response marker不进入 public result。
6. provider outcome unknown 时已知两次核心 usage 保留，第三次 unknown usage为空对象，global `usage_status=PARTIAL`，不使用 0 sentinel。
7. contradiction verifier failure返回 `UNASSESSED` machine HOLD，coverage 与 adjudication usage 均保留。
8. optional interpretation 在 terminal HOLD 时于 `read_document` 和 provider call 之前停止。
9. GUI 只有一次 visible hold terminal，零 `failed` 重复记录，并且没有第四次 API 调用。
10. 两个相同 protected 文本因 path/index 不同而具有不同 identity，不能被翻译结果碰撞合并。

完整回归继续使用仓库既有 `.github/workflows/tests.yml`：Python 3.11/3.12 的 unittest discovery，以及 retained/current adversarial probes。CI 必须绑定最终 repair commit SHA，而不是浮动分支名称。

## 6. Codex 来源、实现方式与许可证

- 来源仓库：`openai/codex`
- 固定参考：`d5caceccb1ee5bf94c081b995575ce4860e0912b`
- 最终检查 main：`ec9620c231396895194329c410f3ec360b4cadef`
- 重点差异提交：`2c4a95736bea64256a50f7b8506bd33c181cc85a`、`e8b938b02eb7e202675f33bae4a3ba82f084d5b9`、`4f2a1d866697144c519c6ee135a114dcd46afc95`
- 吸收的仅是语义：completion/usage 先记账、typed partial outcomes、ambiguity-aware retry、单一 terminal owner、fail-closed 不等于删除既成事实。
- 本地实现方式：Python 语义重写，没有复制 Codex Rust 代码。
- Codex 与本仓库均为 Apache License 2.0。由于没有代码复制，不新增逐段版权头；本文件记录来源仓库、commit、文件和符号作为 attribution。

## 7. 未处理观察项

1. 本修复不提供跨进程或跨重启自动恢复队列；符合“不自动落盘原始响应”的边界。用户可显式保存 bounded HOLD receipt。
2. presentation repair 仅为当前 `zh` 公共字段合同服务，不扩展为通用翻译功能。
3. provider payload 在缺少 `choices[0].message.content` 时，基线 `_parse_payload()` 仍可能在构造 `CompletionResult` 之前失败；本轮真实反例不涉及该路径，未扩展 provider wire parser。
4. Windows EXE 需要在维护者既有 `.venv`/PyInstaller 环境按更新后的 `build_exe.ps1` 重新构建；本轮 GitHub CI 不构建或签署 EXE。
5. source-level runtime patch 是对 exact 0.6.1 事务边界的有限绑定，不改变领域 reducer。后续大版本可以把这些边界原生折叠回 assessor，但不应在本轮继续重构。

## 8. 合并门槛

仅在以下条件全部满足时建议合并：

- repair commit 的唯一 parent 精确为 `ed679849762c11e3aabd848396c09b0adfdb6ab2`；
- focused regression 全部通过；
- Python 3.11/3.12 full regression 与两组 adversarial probes 全部通过；
- language failure 与 contradiction verifier failure usage retention 通过；
- machine-state before/after hash parity 为 true；
- canonical event stream 每次运行只有一个 terminal event；
- diff 不含 raw response persistence、全文 presentation resend、coverage/adjudication 重跑或业务扩展。
