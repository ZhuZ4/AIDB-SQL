# Token Plan 费用核对（2026-09-21）

范围：只查阿里云官方文档；未读取 `.env`、访问账户控制台、查询余额或发送模型 API 请求。研究对象为任务提供的 `token-plan.cn-beijing.maas.aliyuncs.com` 与 `deepseek-v4.1-flash`。

## 可确认的计量与优惠

该域名的 `/compatible-mode/v1` 属于 Token Plan 接入地址。个人版与团队版都使用它，账户具体套餐不能仅凭域名识别；官方接入样例均包含 `deepseek-v4.1-flash`。[官方 Qwen Code 接入文档](https://help.aliyun.com/zh/model-studio/qwen-code)

Token Plan 以 Credits 抵扣，个人版主要受每七天固定窗口限额约束，团队版主要受订阅月额度约束；用尽后暂停服务。它与 Coding Plan、普通按量 API 属于不同产品，不能把另一产品的单价或额度窗口套用到此任务。[Token Plan 总览](https://help.aliyun.com/zh/model-studio/token-plan-overview)

当前个人版公开档位为 Lite / Essential / Standard / Pro，每七天额度分别为 2,500 / 5,625 / 10,000 / 40,000 Credits。每个计量周期从该周期首次调用起计时七天，不能假定每周一重置。账户实际档位和剩余额度未知。[个人版官方说明](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview)

团队版标准 / 高级 / 尊享坐席每订阅月分别为 25,000 / 100,000 / 250,000 Credits；先抵扣坐席，再抵扣已有共享用量包。此处记录套餐机制，不代用户购买或充值。[团队版官方说明](https://help.aliyun.com/zh/model-studio/token-plan-team-overview)

夜间优惠已确认：当前**个人版和团队版**文档都将 `deepseek-v4.1-flash` 列为限时优惠模型；每天 **22:00 至次日 08:00，UTC+8（北京时间 / Asia/Shanghai）**，Credits 消耗五折。个人版另外包括 qwen3.8-max、deepseek-v4-pro-0813、deepseek-v4-flash-0731；团队版另外包括后两个 DeepSeek 型号。活动可调整，页面未给本次研究可确认的结束日。[个人版英文官方说明](https://help.aliyun.com/en/model-studio/token-plan-personal-overview)、[团队版英文官方说明](https://help.aliyun.com/en/model-studio/token-plan-team-overview)

核对时搜索摘要仍遗漏 deepseek-v4.1-flash，但实际打开的中英文正文已经包含该模型；以上以打开后的正文为准。用户未要求限定夜间运行，本次没有设置等待夜间的调度条件。

## 缓存与成本记录

缓存命中对应较低抵扣系数，具体 Credits 消耗还受模型、输入输出、思考和工具等因素影响，应以 Token Plan 订阅页的用量明细为准。本次未找到可据以准确计算该账户 `deepseek-v4.1-flash` 输入、缓存和输出消耗的公开完整抵扣系数表。官方其他模型的计算示例不能直接挪用。[个人版费用常见问题](https://help.aliyun.com/zh/model-studio/token-plan-personal-faq)

本地 q1515 探针记录了 115,241 输入 tokens，其中 97,408 为服务返回的缓存 tokens，未缓存输入为 17,833，输出为 1,129。输入总数来自九次独立响应，不是把流式累计值重复相加。缓存 tokens 是输入的子集，不能再额外加到输入总数上。

因此保留实际输入、输出、缓存和调用次数；没有账单或正确系数时，`api_cost` 与消耗 Credits 应保持未知，不能填 0 或套普通百炼的每百万 token 单价。上述夜间五折指 Credits 抵扣优惠，也不等于本次执行现金成本可直接算出。

可采用的记录字段：`billing_product="token_plan"`、`billing_unit="Credits"`、`credits_consumed=null`、`credit_rates=null`、`cash_cost=null`、`cash_currency=null`、`price_source=null`、`usage_source="provider_response"`。当前 `api_cost=null` 与 `api_cost_available=false` 应保留；套餐档位、剩余额度和真实消耗 Credits 未知，不用零值替代。

若以后获得**对应型号、模式、套餐且有效期匹配**的输入 / 缓存 / 输出系数（统一换算为 Credits/token），可按每次请求计算：`[(input_tokens-cached_tokens)*r_input + cached_tokens*r_cached + output_tokens*r_output] * applicable_discount + separately_metered_items`，再求和。该式只是估算框架：先确认系数本身是否已含夜间折扣，避免重复打折；额外计量项按实际账单口径处理；跨优惠时段的结算归属未核实，不擅自假设按开始或完成时刻判定。若有控制台消耗明细，以其作为真实 Credits，估算字段另列，不冒充账单。

## 故障识别

| 响应内容 | 官方含义 | 本任务处理原则 |
|---|---|---|
| `429 insufficient_quota` 且提示 `token-plan 1-week quota` 已耗尽 | 个人版七天额度触顶 | 落盘并停止派发，保留未开始题 |
| `429 insufficient_quota` 且提示 `token-plan quota` 已耗尽 | 团队坐席月额度耗尽 | 同上；不自动购买额度 |
| `429 Requests rate limit exceeded` | 请求频率或并发过高 | 有界退避，不能当作余额不足 |
| `401 InvalidApiKey` 等 | Key 不匹配、失效或订阅到期等 | 保存明确鉴权失败 |

以上依据 [个人版 FAQ](https://help.aliyun.com/zh/model-studio/token-plan-personal-faq) 与 [团队版 FAQ](https://help.aliyun.com/zh/model-studio/token-plan-team-faq)。通用百炼错误码页还把某些 `insufficient_quota` 用于 TPS/TPM 限流，因此诊断应保存并检查实际消息和 Token Plan 上下文，不能只看 HTTP 429 或把其他产品的配额错误机械套用。[通用错误码说明](https://help.aliyun.com/zh/model-studio/error-code)

## 已查到的服务适用范围

官方个人版说明将使用范围限定于兼容工具中的交互操作，并明确排除自动化脚本、后端和非交互批量调用；团队版也排除自动化脚本和后端。Python 子进程批量运行并不因最初在 Codex 中发起就自动获得该套餐的使用许可。这是官方正文中的实际限制；本研究未查账户协议、特殊授权或合同，不能判定账户是否另有许可。[个人版订阅前须知](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview)、[团队版订阅前须知](https://help.aliyun.com/zh/model-studio/token-plan-team-overview)

本任务用户已明确授权使用既有配置执行项目批量任务，主任务决定记录该公开使用范围，同时维持当前配置及进程，不擅自切换付费账号、端点或购买服务；实际服务拒绝时按真实返回的错误处理。本费用核对仅写本文档，未改动运行代码。
