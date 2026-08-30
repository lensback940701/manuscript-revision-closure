# 更新记录

[English](CHANGELOG.md)

## Standalone 0.6.4——Intake 与技术 HOLD 诊断修复

- 增加版本化 technical-HOLD receipt，使 intake PASS 后的技术失败在 Closure Card、machine receipt、minimal receipt、GUI 与保存 JSON 中使用一致的原因、动作和失败阶段。
- 保留经过限长、单行化和脱敏的 provider status/code/detail；仍不持久化原始响应、请求体、prompt、稿件全文、凭据或 hidden diagnostics。
- 用 `mrc-local-technical-preflight-1.0` 取代格式敏感的 intake 阻断；标题、章节名/顺序、编号和 front matter 仅作 advisory，不能改变 coverage routing。
- 新增逐次 `mrc-provider-transmission-consent-1.0`，绑定文件 SHA-256、provider、model，默认拒绝且取消形成独立用户状态。
- 将 `mrc-semantic-manuscript-basis-1.0` 合并进第一次 `mrc-whole-manuscript-coverage-3.0`，不新增全文请求；basis 不足准确记录一次 coverage usage/cost、跳过 adjudication，且不得冒充技术失败。
- 完整 usage 回执与实时价格可用性分开计数；价格源不可用时不得抹除已知 token 记账。
- 用 `mrc-candidate-lower-bound-independent-additions-1.0` 替换 candidate exact ceiling：coverage candidates 仍逐项必需，真正独立的第二阶段可恢复已观察、可定位的 canonical 漏报。零候选使用有限合法 schema 与非空 canonical enum；未知、重复、不可定位、臆测或无解释补充均 fail closed。
- 新增 `mrc-affirmative-stop-gate-1.0`：STOP 必须由两阶段对贡献、全稿论证、理论、方法、证据和连贯性作出肯定性充分判断；谨慎措辞或空候选本身不能证明停止。真实 claim ceiling、scope、source status、rivals 与 limitations 继续受保护，但不再形成默认 STOP 偏向。
- `mrc-schema-definition-lint-1.0` 继续在 provider dispatch 前递归拒绝空/重复 enum、非法边界、required/properties 错配和不支持结构。
- 增加非阻断低风险标题样式建议；编号样式不再决定稿件完整性。
- timeout、网络状态不明以及 429、502、503、504 仍只允许一次物理 attempt。

## Standalone 0.6.3——Provider 合同与状态完整性修复

- coverage、adjudication、presentation repair 与 interpretation 的每个逻辑调用只允许一次物理 HTTP 请求，不再自动重发全文。
- 增加 bounded 物理请求收据、provider capability 元数据、canonical schema 哈希与未知潜在计费表达。
- 在模型可见 prompt 中嵌入 canonical coverage schema 和本轮动态 adjudication schema；支持 strict schema 的 provider 继续使用 API 级交付。
- 增加动态 candidate cardinality/enum 绑定、独立 exact-set verifier，以及 missing/extra/duplicate bounded 诊断。
- 在 runtime 与 GUI 中分离 machine HOLD 和 presentation HOLD，不改变 Skill `0.2.1` 的学术判断合同。

## 0.2.1——公开发布候选

- 为无版本、`0.1.x`、`0.2.0` 和 `0.2.1` 收据增加统一的版本族验证。
- 对不支持的收据版本直接拒绝，不再猜测其结构。
- 加强方向性轻量建议的边界检查，防止修改命令借助已声明的标点和包裹符号泄漏。
- 保留标准事项代码、固定标签、精确旧版迁移、非回显、四种实质判断、双哈希收据语义与只读路由。
- 将英文和简体中文公开说明拆分为独立页面。
- 在中英文首页中加入四张由作者提供的说明插图。
