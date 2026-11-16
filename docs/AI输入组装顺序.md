# AI输入组装顺序

2026-11-16，按用户确认的顺序调整0.4.1源码中的实际接待模型输入。固定提示词正文、资料选择、来源约束、任务输出契约与发送门控沿用现有实现。

## 请求结构

`system`放当前任务的固定接待规则；`user`文字块是一个JSON对象，依次放商家公开资料、内部接待策略、聊天风格和当前客户`context`。实际原图继续作为同一条`user`消息中的图片块提交。

```text
system：固定接待规则＋当前任务模块＋适用的需求变更/图片规则

user：
{
  "knowledge": [商家公开资料],
  "guidance": [内部接待策略],
  "style": [采用的聊天风格],
  "context": {
    业务补充,
    会话摘要,
    采购需求与供货背景,
    当前聊天,
    本轮目标
  }
}

同一条user消息的后续内容块：本轮确切附带的原图。
```

上面是结构示意。只有当前任务实际具备的字段才会输出，不为缺失资料补造内容。

## context字段顺序

| 顺序 | 内容 | 对应字段 |
| --- | --- | --- |
| 1 | 业务补充 | `supplement`，包含正文和允许附件 |
| 2 | 会话摘要 | `conversation_summary`、WhatsApp的`wa_summary` |
| 3 | 采购需求与供货背景 | `case`、`requirements`、`product_context`、`supplier_context`、`case_catalog`，以及WhatsApp可选的`international_background` |
| 4 | 当前聊天 | `message_view`、`whatsapp_messages`、`image_manifest`、`image_transport`、`message_semantics` |
| 5 | 本轮目标及执行所需信息 | `payload_version`、`task_mode`、`seller_id`、`binding_revision`、`collection_rules`、`required_fields`、`conversation_intent`、`current_case_ref`、`selected_case_ref`、`continuation`、`followup_context`、`reply_target`、`unsent_reply` |

新增或渠道专用的其他已投影字段保留在`context`中，位于最终目标字段之前。WhatsApp的国际站只读背景内部依次为`supplement`、`summary`、`requirements`、`messages`。

## 保留的业务关联

- 顶层三类资料是当前请求原本选中的资料。各采购事项自己的`knowledge`、`guidance`、`style`与供货背景继续保存在对应`case_catalog`条目中，保留`case_ref`和`product_scope`，不合并为全局通用规则。
- 消息数组及资料数组不因本次调整重新排序；消息角色、来源编号、买卖双方图片来源和真实附图清单继续保留。
- 人工建议仍为单份正文；自动接待及WhatsApp仍按现有规则生成1至3条。润色仍只接收原本允许的输入，不加入普通`guidance`、采购事项或会话摘要。
- `project_model_payload`继续生成原有语义投影，供本地来源校验及指纹计算使用；`model_json`负责最终外层封装和顺序，不改写本地权威上下文。
- 最终JSON仍按实际编码内容执行120000字符预算，包含新增的`context`容器开销；超限明确拒绝，不截断证据。
- 稳定的商家资料位于客户数据之前。摘要更新会影响后续聊天部分的公共前缀，本次未验证真实模型的缓存命中率或费用变化。

## 定向验证

以下用例使用合成内容、临时目录和替身HTTP或模型传输，不调用真实付费模型、不读取真实客户库。

| 测试文件 | 通过项数 | 覆盖内容 |
| --- | --- | --- |
| `test_model_context_order.py` | 10 | 五类任务的材料/客户字段顺序、封装前后内容等价、事项范围、本地指纹、实际输入预算、两协议固定提示词及图片块等价 |
| `test_native_image_requests_041.py` | 5 | 真实组装入口与router/transport下，两协议固定提示词全文、PNG原始字节、同轮图文、人工润色与独立WhatsApp路径 |
| `test_context_deduplication_041.py` | 5 | 历史消息与图片去重、同文不同ID保留、同像素不同买卖双方来源保留 |
| `test_model_inputs_040.py` | 10 | 图片来源绑定、真实附图清单、图片证据、输入预算和原有错误边界 |
| `test_whatsapp_account_generation_041.py` | 10 | 新封装下账号接待、1至3条逐条回执、来源/商家修订变化保护、接管取消与同轮原图 |
| `test_reception_prompts_0314.py` | 11 | 实际接待任务入口、原问题续答、业务补充续答、人工单条、自动多条、需求变更与冻结润色边界 |

合计51项通过。两协议分别为`chat-completions`与`anthropic`；固定提示词审核稿与静态模块一致性检查、相关改动的`git diff --check`通过。未运行全量测试。本次修改未构建或安装应用，已安装程序是否采用该顺序须以之后的构建和安装为准。

实现入口：[model_payload.py](../services/reception/src/reception_service/model_payload.py)。
