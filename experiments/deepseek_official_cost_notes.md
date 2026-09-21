# DeepSeek 官方接口与费用核对（2026-09-22）

用户更新 `.env` 授权使用新接口后，已实测官方 `/models` 和 `/user/balance`
均返回 HTTP 200，账户 `is_available=true`。余额明细仅保存在本地忽略目录。
两次小型非数据集请求成功完成工具调用与结果回传，共 382 个服务报告的 tokens；
这两次探针另记账，不计入任何题目成绩或实验调用数。

当前 API 名称 `deepseek-flash` 对应 DeepSeek‑V4.1‑Flash；旧服务使用的
`deepseek-v4.1-flash` 字符串不是此次模型列表返回的名称。
官方模型别名可能随服务更新变化，冻结本地名称与端点不等于冻结服务商权重。
[官方发布记录](https://api-docs.deepseek.com/updates/)

本系列显式发送 `thinking.type=disabled`，保留先前非思考模式的实验意图。
官方默认开启思考；旧服务的 `chat_template_kwargs.enable_thinking=false`
不能直接当作官方接口开关。工具探针已验证非思考回复。
[思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)

官方人民币标价如下，单位为每百万 tokens：

| 项目 | 空闲时段 | 高峰时段 |
|---|---:|---:|
| 缓存命中的输入 | ¥0.02 | ¥0.04 |
| 未命中缓存的输入 | ¥1 | ¥2 |
| 输出 | ¥4 | ¥8 |

高峰为北京时间周一至周五 09:00–12:00、14:00–18:00，其余为空闲时段。
表内已体现五折，不能再乘一次折扣。此规则只用于新官方系列，不能套回旧 Token Plan。
用户没有设置等待夜间的条件，仍按可用资源连续运行。
[官方人民币定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

每次调用保留输入、输出、缓存、未知用量和耗时。按公开价格推算的值须标为估算；
缺少用量或时段归属证据时保持未知。余额差额是账户级观测，可能包含其他客户端，
不能无条件冒充本实验的精确账单。不自动充值，实际余额/配额不足时保存断点并停止。
[官方余额接口](https://api-docs.deepseek.com/api/get-user-balance/)

本地证据目录：`.local-services/experiments/provider_transition_20260922/`。
新旧服务使用独立实验系列和 registry；不拼接剩余题目，不混算重复运行均值。
