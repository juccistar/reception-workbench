# 客户接待工作台

源码版本 **0.4.0**，正在完成最终整合与候选验收。当前公开发行基线为 [0.3.21](https://github.com/juccistar/reception-workbench-releases/releases/tag/v0.3.21)；0.4.0 的构建、窗口和发行状态以 [发布说明](docs/0.4.0发布说明.md)及[验证记录](docs/verification/0.4.0.json)为准。

普通双击进入极简启动台，同一 Windows 用户统一管理多个 API，为接待和翻译分别选择配置。接待模型处理中文需求、会话摘要、自动回复、人工建议及润色，有相关图片时同轮提交文字与受控原图。支持 Chat Completions 和 Anthropic 协议；翻译可以使用独立文字模型。正式版不再提供 Demo，开发用合成数据和替身独立于发行包。

Accio Work 继续负责商家身份、国际站读取与发送及采购工具。首次或重新建立业务窗口仍须从目标商家的 Accio 授权环境启动；启动台可以切回已经核验的运行窗口。商家登记不保存 Accio 令牌，不承诺完全退出或重启后的永久授权复用。模型 Key 使用当前 Windows 用户的 DPAPI 加密，业务库和发送账本按商家隔离，自动接待重启后不恢复授权。

工作台保留普通单正文人工聊天、整段建议及润色，自动接待自然生成一至三条并逐条核对平台受理。中文需求与独立会话摘要、多事项、商品与供应商档案、业务补充附件继续沿用；原图可预览和复制。WhatsApp 使用原始网页及同页面 SDK，媒体下载和图文来源受控。发送结果未知时沿原操作核查，不能换任务重发。自动定向询价、报价回收、实号登录收发和真实模型质量仍须各自验收。

从仓库根目录运行 `npm run build` 后再运行 `npm start` 打开启动台。环境准备、定向测试、合成窗口夹具和候选打包见[开发说明](docs/DEVELOPMENT.md)。默认按改动范围测试，只有明确要求才运行全量测试。源码开发、构建、公开发布、本机安装和真实数据迁移分别记录，公开版本不代表本机已经升级。

源码维护于私有仓库 [juccistar/reception-workbench](https://github.com/juccistar/reception-workbench)，默认分支为 main；安装器、便携 ZIP 及客户端更新继续使用公开仓库 [juccistar/reception-workbench-releases](https://github.com/juccistar/reception-workbench-releases)。原工程仅作为历史来源，运行时不导入旧 Qt 工程。详见[项目约定](docs/PROJECT.md)、[来源清单](docs/SOURCE_BASELINE.json)和[迁移边界](docs/MIGRATION.md)。

- [用户指南](docs/USER_GUIDE.md) · [人工聊天](docs/人工接待使用.md) · [授权启动](docs/自动接待启动与授权.md)
- [文档入口](docs/README.md) · [开发代理指引](CLAUDE.md)
- [现行规则](docs/CURRENT_RULES.md) · [实施契约](docs/IMPLEMENTATION_CONTRACT.md) · [架构](docs/项目开发/02-架构文档.md)
- [后续待办](docs/ROADMAP.md)
