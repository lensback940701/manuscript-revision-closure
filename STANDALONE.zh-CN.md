# Manuscript Revision Closure Standalone 0.6.4

这是 `manuscript-revision-closure` Skill 的独立 Windows 多阶段合同运行器。它执行
Skill 0.2.1 的只读截止判断输出合同，通过 DeepSeek、Kimi 或 Gemini API 完成不落盘的
整稿覆盖与根因裁决，再由原确定性 helper 生成 Closure Card 和最小收据。它不复制 Codex
的通用工具系统，但已加入与本 Skill 直接相关的 intake、coverage、adjudication 和 contradiction gates；架构审计见
[`docs/HARNESS_EQUIVALENCE_AUDIT.zh-CN.md`](docs/HARNESS_EQUIVALENCE_AUDIT.zh-CN.md)。

0.6.4 继续将 provider completion、usage、machine adjudication 和公开展示拆分为独立事务，并收敛为薄本地技术预检、逐次全文外发确认和 coverage 内语义 basis gate。`mrc-local-technical-preflight-1.0` 仅阻断不可读/不支持/提取失败、零有效文本、明确超限或配置失败；任何标题、章节、顺序、编号、ATX/Setext/plain、YAML/TOML 解析结果都只进入 `mrc-format-advisory-1.0`，不能改变 coverage routing。GUI 每次运行通过 `mrc-provider-transmission-consent-1.0` 显示并绑定文件路径/名称/SHA-256、provider、model 和可能计费说明；取消 API=0，确认不可复用。第一次 coverage 同时执行 `mrc-semantic-manuscript-basis-1.0`，不增加全文请求；basis 不足为 coverage=1/adjudication=0/no verdict/no presentation，技术错误仍使用 `mrc-technical-hold-receipt-1.0`。所有核心和公开调用固定 `max_transient_retries=0`。

`mrc-canonical-schema-delivery-3.0`、`mrc-dynamic-adjudication-schema-3.0`、`mrc-candidate-lower-bound-independent-additions-1.0` 与 `mrc-affirmative-stop-gate-1.0` 支持 0/1/N coverage candidates。候选是第二阶段必须逐项处理的下限；第二阶段可补充 canonical、已观察、可定位、非臆测且非重复的 coverage disagreement。零候选使用 `minItems=0`、有限 canonical 上限与非空 dimension enum，不再机械导向 STOP。STOP 只有在 coverage 与 adjudication 对六个核心维度均形成肯定性充分判断且无材料问题时才成立。`mrc-schema-definition-lint-1.0` 在任何 schema provider dispatch 前递归检查 enum、array bounds、required/properties 与本项目支持的有限关键字。失败阶段不发送请求，并返回带 bounded path/error 的 `SCHEMA_DEFINITION_INVALID` technical HOLD。

## 运行方式

双击：

```text
release\ManuscriptRevisionClosure.exe
```

无参数时会启动一个只监听 `127.0.0.1` 随机端口的本地 GUI，并自动在默认浏览器
打开。它不是云端网页，不加载远程脚本或图片；关闭时请点击页面中的“关闭本地程序”。

没有稳定 STOP receipt 时，核心判断固定进行两次同模型 API 调用：第一次形成十维有限覆盖
状态，第二次重新读取全文、逐项处理 coverage candidates，并可在 canonical 维度内恢复第一阶段漏报。两阶段都必须区分真实 evidence-bound caution 与遮蔽贡献/方法可评估性/论证闭合的防御性结构；“不要过度修改”既不能自动触发 revision，也不能代替肯定性 STOP 证明。GUI 默认在核心裁决后再额外调用一次同一模型，生成中文结果解读。该解读受
[`standalone/AGENT.md`](standalone/AGENT.md) 的精确十一个键合同约束，包含判断依据、
判断原则、重点维度、选择性公开观察、应保护内容、投稿前人工核对清单、简要局限和
最多三项低风险可选微调。可取消勾选以免除这次额外 API 调用。

核心判断与中文解读请求均启用提供商兼容的结构化输出；Gemini 与 Kimi 请求还会把精确
JSON Schema 交给服务端约束。模型返回后仍须通过独立的本地精确字段合同。
解析器允许一个完整 JSON 对象、只包裹该对象的单层 JSON Markdown fence，或说明文字中
唯一可确定提取的完整对象；多个顶层对象、缺键、额外键和字段类型错误均 fail-closed。
若解读格式失败，该次 API 返回的 token usage 仍会进入任务费用估算。
程序不再使用会同时截断 reasoning 与可见 JSON 的 5000 token 小上限；请求按提供商使用
DeepSeek 384K、Kimi 128K、Gemini 64K 的高余量。若提供商仍以 `finish_reason=length` 截断，程序会明确报告截断，
不把它伪装成普通 JSON 解析错误。

请求等待采用 provider/stage 合同：Kimi 整稿覆盖为 300 秒，根因裁决、presentation repair 和中文解读为 900 秒；其余已登记组合默认 180 秒。coverage、adjudication、presentation repair 与 interpretation 均固定 `max_transient_retries=0`；timeout、socket/read error、连接中断以及 HTTP 429、502、503、504 或其他 provider error 都不会自动重发。CLI `--timeout` 可显式覆盖等待时间；兼容参数 `--transient-retries` 只接受 `0`。

HTTP error 可在 `mrc-provider-error-detail-1.0` 下公开有限的 provider status/code 和最长 240 字符单行 detail。detail 先经过确定性脱敏；Authorization、key/token、请求体、稿件、prompt、hidden diagnostics 或 chain-of-thought 语境会遮蔽。原始 response body 始终不进入事件、receipt、GUI 或保存文件。

如需保留原来的终端向导：

```powershell
.\release\ManuscriptRevisionClosure.exe --console
```

也可以直接使用命令行参数：

```powershell
$env:DEEPSEEK_API_KEY = 'your-key'
.\release\ManuscriptRevisionClosure.exe .\paper.docx `
  --provider deepseek `
  --reasoning high `
  --confirm-complete `
  --identity paper-v12 `
  --language zh
```

Kimi：

```powershell
$env:MOONSHOT_API_KEY = 'your-key'
.\release\ManuscriptRevisionClosure.exe .\paper.pdf `
  --provider kimi `
  --reasoning enabled `
  --confirm-complete
```

Gemini：

```powershell
$env:GEMINI_API_KEY = 'your-key'
.\release\ManuscriptRevisionClosure.exe .\paper.docx `
  --provider gemini `
  --model gemini-3.7-flash `
  --reasoning medium `
  --confirm-complete
```

API key 只从环境变量读取：

| Provider | Key 变量 | 默认模型 | 可选覆盖 |
| --- | --- | --- | --- |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-v4-pro` | `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL` |
| Kimi | `MOONSHOT_API_KEY`；兼容 `KIMI_API_KEY` | `kimi-k2.6` | `KIMI_MODEL`、`KIMI_BASE_URL` |
| Gemini | `GEMINI_API_KEY` | `gemini-3.7-flash` | `GEMINI_MODEL`、`GEMINI_BASE_URL` |

程序不会显示 key，也不会把 key 写入事件、结果或收据。

GUI 使用真正的模型下拉框，并可从提供商官方模型 API 刷新全部兼容模型；没有 key、
断网或接口失败时，会明确显示带版本的多模型回退目录。Gemini 目录只保留官方声明支持
`generateContent`、且适合文本判断的模型，排除 embedding、image、live、transcribe、
computer-use 等专用端点。

思考设置会随模型动态变化，不把三家的不同能力伪装成同一套参数：

- DeepSeek：默认开启 high；可关闭，或选择 `low`、`high`、`max`；
- Kimi K3：固定开启，支持 `low`、`high`、`max`；K2.6 仅支持开启/关闭；
  K2.7 Code 固定开启且不可调强度；
- Gemini：按具体型号只显示其支持的 `none`、`minimal`、`low`、`medium`、`high`
  子集；例如 Gemini 3.7 Flash 支持 `low / medium / high`，不能关闭。

GUI 与 CLI 都会在发出 API 请求前验证组合。若手工提交模型不支持的思考选项，会
fail-closed，而不是静默忽略。CLI 参数为 `--reasoning`。

## 本次任务计价

完成核心判断及可选中文解读后，程序会读取各次 API 响应里的实际 usage token，刷新
所选模型的官方定价页，并在 GUI、公开 JSON 和保存的中文解读中显示逐次与合计 CNY/USD
估算。Kimi 与 DeepSeek 使用中国站人民币价格作为原币主账单；Gemini 使用美元官方价作为
原币主账单。程序再读取欧洲中央银行最新工作日 EUR 基准下的 USD 与 CNY 参考汇率，计算
`CNY per USD` 并显示另一币种。DeepSeek 会按当前 UTC 对应的北京时间峰/谷时段选择价格；
Gemini 会按日期选择仍然有效的标准付费层档位；Kimi 从中国站官方 Markdown 价格表读取
精确模型行。

若官方页面暂时不可达，只能使用标有快照日期的内置参考价，并显示
`bundled_snapshot_fallback`，绝不把它冒充“实时价格”。该金额是根据 provider 返回的
token usage 计算的标准价估算，不是平台最终账单；税费、免费额度、赠送余额、账户折扣、
批量/优先级服务和缓存结算差异仍可能改变实际扣费。实时价格不可用时，程序仍保留每次完整 usage 回执及其 token 计数；价格状态只决定能否估算金额，不能把已知 usage 改写为缺失或零。

## 输入与输出

支持：TXT、Markdown、RST、HTML、DOCX、带文本层 PDF。扫描 PDF 不做 OCR；
文本不可读时失败或返回 `UNASSESSED`，不会猜测。程序不会静默截断超长输入。
默认提取文本上限为 300,000 字符，可通过 `MRC_MAX_TEXT_CHARS` 显式调整；超过
上限会停止而不是只送出稿件前半部分。

没有 `--confirm-complete` 时，程序不会调用 API，而是稳定返回 `UNASSESSED`。
这用于防止把节选误判为整稿。

本地只验证文件可处理、非空且未超过声明限制；标题、摘要、结论、参考文献、顺序和 heading
样式仅形成非阻断 advisory。只有本次文件 SHA-256、provider、model 的明确外发确认通过后，
全文才进入第一次 coverage；该 coverage 自身判断实质整稿基础并决定是否继续 adjudication。
程序按所选模型登记的上下文上限保守估算每个阶段的输入和输出余量；预算不足时不截断全文，
而是在该门稳定停止。

默认只把公开结果显示在本地 GUI、交互式控制台或 stdout。只有以下显式操作会写文件：

- GUI 中点击“保存公开结果…”并确认目标路径；
- GUI 中点击“保存中文解读…”并确认 Markdown 目标路径；
- 交互向导中主动填写结果保存路径；
- CLI 使用 `--output result.json`；
- CLI 使用 `--event-log events.jsonl`。

事件日志不包含稿件正文、prompt、模型原始输出或 API key。

## 安全边界

- 稿件按不可变、不可信输入处理；
- GUI 只绑定 `127.0.0.1`，每次启动生成随机访问 token，并校验本机 Host/Origin；
- GUI 页面使用 CSP，不加载远程代码或资源，API key 只显示环境变量是否存在；
- 状态时间线仅显示真实阶段、API 尝试、等待用时、token usage 与合同校验结果；
- 定价刷新只访问三家官方文档域名及 ECB 汇率 XML，不发送稿件或 API key；
- 中文解读失败不会覆盖或降级已经完成的核心 Closure Card；
- coverage 模型只返回十维有限状态；adjudication 只返回 digest 绑定和有限裁决对象；
- coverage 私有状态只在本次进程内传递给 adjudication，不写入公开结果或事件；
- 本地 contradiction gate 独立复算 coverage digest，并检查候选维度、hold 与保护不变量没有被遗漏；
- 未知键、未知 hold code、详细位置、改写指令或不合法卡片全部 fail-closed；
- 不存在 Shell、文件编辑、MCP、浏览器、搜索或多代理入口；
- 不对网络错误、timeout、429、502、503、504 或其他失败自动重发全文；
- 模型输出合同错误不会触发第二轮“自我修复”；
- 详细内部评估、原始模型输出和 chain-of-thought 不落盘。

## 从源码运行和测试

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m unittest discover -s tests -p 'test_*.py'
python -B -m standalone --help
```

构建：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build_exe.ps1
```

最小 harness 的推导、Codex 参考 commit 和明确排除项见
[`docs/HARNESS_EXTRACTION.zh-CN.md`](docs/HARNESS_EXTRACTION.zh-CN.md)。
换电脑运行、loopback 安全边界和已验证/未保证的平台范围见
[`docs/PORTABILITY.zh-CN.md`](docs/PORTABILITY.zh-CN.md)。

## 限制

这是实验性第一版，不是事实认证、同行评审替代品、投稿授权或稿件修改器。
DeepSeek/Kimi/Gemini 的模型可用性、API 合同与价格可能变化，因此 GUI 支持刷新目录，
模型名与 base URL 也可通过
环境变量覆盖，但最终输出仍必须通过本地固定合同。
