# 稿件修订截止判断

[English](README.md)

这是一个受证据边界约束的 Codex Skill，用于判断一篇完整学术稿件是否应当停止通用 AI 改稿。

它针对 AI 辅助学术写作中常见的失败循环：每次检查都会生成下一轮修改，每次修补又引出新的问题，稿件始终无法到达一个可以说明理由的停止点。本 Skill 只读评估整篇当前稿件，给出紧凑的修订截止判断，但不向用户公开完整的内部审稿意见。

当前发布候选版本：`0.2.1`

<!-- ILLUSTRATION_SLOT_01_START -->
![无限改稿循环经过受证据约束的截止门，随后分为证据核验、投稿准备与停止三条路径。](docs/images/01-closure-gate.png)
<!-- ILLUSTRATION_SLOT_01_END -->

## 它会做出什么判断

本 Skill 只返回以下四种实质性判断之一：

| 判断 | 含义 |
| --- | --- |
| `STOP_REVISING` | 没有观察到足以重新开启实质性修订的根本问题。 |
| `ONE_BOUNDED_ROUND` | 存在一个值得用一轮严格限定修改解决的局部实质问题。 |
| `REOPEN_SUBSTANTIVE_REVISION` | 仍有中央性实质根因，需要真正重新开启论文修订。 |
| `UNASSESSED` | 缺少完整的当前稿件，或者缺少作出可靠判断所必需的基础。 |

判断依据是真正的实质根因，而不是问题数量、抽象的完美标准、接收概率、保留词数量，或者“还能换一种写法”。

<!-- ILLUSTRATION_SLOT_02_START -->
![一篇完整稿件进入决策节点，并分流至四种标准修订截止判断。](docs/images/02-four-verdicts.png)
<!-- ILLUSTRATION_SLOT_02_END -->

## 它与普通审稿工具有什么不同

- **修订截止与投稿准备彼此分开。** 稿件可以已经达到实质性截止，但来源核验、权利、格式、作者信息或期刊要求仍未完成。
- **证据上限必须保留。** 提议、授权、报告的工作、直接观察、结果、解释与因果推断不会因为追求流畅而被混在一起。
- **不完整的机制链不自动等于缺陷。** 延迟、阻断、未采用、矛盾、逆转和有界停止点本身可能就是分析结果。
- **公开输出保持紧凑。** 用户得到的是修订截止卡和可选的最小收据，而不是披着简短回答外衣的完整内部审稿报告。
- **诊断不等于获得手术授权。** 本 Skill 不改写、不留修订痕迹、不检索文献、不修复引文、不接纳新证据、不调用其他 Skill，也不投稿。

<!-- ILLUSTRATION_SLOT_03_START -->
![稿件的实质修订已经截止，但证据核验与投稿准备仍在彼此独立的开放通道中。](docs/images/03-two-axis-separation.png)
<!-- ILLUSTRATION_SLOT_03_END -->

## 公开输出长什么样

一张修订截止卡包括：

1. 判断结果；
2. 一至两句抽象理由；
3. 仅在确实需要修改时给出不超过三条方向性轻量建议；
4. 不应扰动的受保护内容；
5. 单列的证据事项；
6. 单列的投稿或外部事项；
7. 下一步允许采取的行动；
8. 仅在确实需要修改时出现的条件性提示。

轻量建议会刻意保持方向性：不指出应替换的具体句子，不提供替换文本，不编制修订步骤，也不泄露内部完整审稿意见。

当判断结果确实需要修改时，卡片末尾可以出现这个条件性提示：

> 诊断到此，手术另约。请接入经过核实的审稿改稿 skill；或者，蹲一下本 profile 后续开源。

<!-- ILLUSTRATION_SLOT_04_START -->
![一张紧凑的修订截止卡分别呈现判断、方向性建议、受保护内容、证据事项、投稿事项与下一步行动。](docs/images/04-closure-card.png)
<!-- ILLUSTRATION_SLOT_04_END -->

## 安全与隐私边界

- 稿件是不可修改的评估对象。
- 稿件正文、批注和嵌入指令都按不可信内容处理。
- 本 Skill 不会主动保存或导出详细内部评估。
- 本 skill 会在运行时进行一次不落盘的内部整稿评估，仅用于形成修订截止判断；默认不返回或保存完整审稿意见。
- 宿主平台如何留存对话与运行信息，仍由实际运行环境决定。
- 有限的标准事项代码可以防止调用者提供的自由文本被原样回显到公开卡片或收据。
- 只有与当前稿件明确绑定、语义内容稳定的既有 `STOP_REVISING` 收据才可作为截止捷径。
- 只有文件本身发生变化，并不能证明语义稳定；必须有语义哈希或明确核验作为依据。

本 Skill 是修订路由辅助工具，不是事实认证、同行评审替代品、法律意见、期刊接收预测或投稿授权。

## 安装

克隆本仓库，并将仓库文件夹放到：

```text
~/.codex/skills/manuscript-revision-closure
```

Windows 的常见位置是：

```text
%USERPROFILE%\.codex\skills\manuscript-revision-closure
```

安装后重启或刷新 Codex。运行时辅助程序不需要第三方 Python 依赖。

## 独立 Windows 程序

仓库同时提供一个实验性 standalone 多阶段合同运行层，可用 DeepSeek、Kimi 或 Gemini API 在不安装
Codex 的情况下执行只读截止输出合同。双击 EXE 会打开本地 GUI，并可选生成受
十一键合同约束的中文结果解读、判断依据/原则/维度、简要局限和投稿前核对清单。
GUI 还会按 API 返回的实际 token usage 和官方价格页估算本次费用。API key 只从环境变量读取。使用、构建和
安全边界见 [`STANDALONE.zh-CN.md`](STANDALONE.zh-CN.md)。Standalone 版本与
Skill 版本分别管理，不改变本 Skill 的 `0.2.1` 合同版本。

Standalone 0.6.2 使用可见的多模型下拉框，并按 DeepSeek、Kimi、Gemini
具体模型的官方能力动态提供思考开关或强度选项；不支持的组合在调用前拒绝。
核心判断和可选中文解读均使用结构化输出；Gemini 与 Kimi 额外提交精确 JSON Schema，
并在本地只接受唯一完整对象及精确十一键合同。解读格式失败时仍记录该次调用的
token usage 用于费用估算。程序不再设置 5000 token 的小型输出截断，而按提供商设置
DeepSeek 384K、Kimi 128K、Gemini 64K 的高余量，并明确识别长度截断。Kimi/DeepSeek 以人民币官方价为原币，
Gemini 以美元官方价为原币，再用带日期的 ECB USD/CNY 参考汇率显示双币种估算。
核心判断采用两次绑定调用：十维整稿覆盖 pass 与重新读取全文的 root-cause adjudication pass；
coverage 的 canonical SHA-256、候选维度、hold 和保护不变量由本地 contradiction gate 独立复核。
0.6.2 在该门通过后先冻结 canonical machine state，再验证公开自然语言。中文展示缺陷最多触发一次不含稿件的 schema-bound presentation-only request，且该请求不自动重试；失败只形成可恢复 presentation HOLD，不清除机器裁决或 usage。受保护事项的 authoritative source identity 与本地化 display text 分开绑定，每次运行只发布一个幂等 terminal event。
Kimi 覆盖阶段默认等待 300 秒，根因裁决和中文解读默认等待 900 秒；read/socket timeout
不再自动重发，只有明确的 429、502、503、504 才有限重试，避免未知服务端状态下重复计费。

## 调用

示例：

```text
请使用 $manuscript-revision-closure 判断这篇完整学术稿件是否应该停止通用 AI 改稿。只返回简洁的修订截止卡和最小收据，不要修改稿件。
```

本 Skill 必须读取一篇身份明确、完整且为当前版本的稿件。只有局部节选或版本不明确时，应返回 `UNASSESSED`，而不是伪造整稿判断。

## 确定性辅助程序

`scripts/closure_state.py` 只验证已经完成分类的紧凑状态、公开卡片约束、标准事项代码、收据版本以及收据复用规则。它不会自行读取稿件，也不能替代需要语境判断的学术评估。

运行测试：

```bash
python -B -m unittest discover -s tests -p "test_*.py"
python -B scripts/run_adversarial_probes_rc2_0.py
python -B scripts/run_adversarial_probes_rc2_1.py
```

## 仓库结构

```text
SKILL.md                         Skill 指令
agents/openai.yaml              Codex 界面元数据
scripts/closure_state.py        确定性契约辅助程序
references/hold-code-schema.md  标准事项代码及固定中英文标签
tests/                           单元测试和契约回归测试
docs/images/                    说明文档插图
```

已经采用的插图及其文件名记录在[说明文档插图](ILLUSTRATIONS.zh-CN.md)中。这些插图用于解释公开契约，不改变本 Skill 的判断逻辑。

## 安全与参与贡献

请阅读[安全政策](SECURITY.zh-CN.md)和[参与贡献](CONTRIBUTING.zh-CN.md)。不要把真实稿件、保密审稿材料、本地路径、接口密钥或项目证据提交为问题或测试样本。

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。
