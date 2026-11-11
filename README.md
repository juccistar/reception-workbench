# 客户接待工作台

当前源码版本为0.3.3。工作台以Accio授权环境中的自动客户接待为主，客户页保留普通人工输入和发送；提供采购需求整理、当前会话业务补充、主动回复建议及只读中文翻译。

[0.3.3已公开发布](https://github.com/juccistar/reception-workbench-releases/releases/tag/v0.3.3)，四个附件的服务端摘要与匿名下载均校验通过。功能边界、安装包哈希及本版验收见[发布说明](docs/0.3.3发布说明.md)。发布不代表本机已安装。

这是位于`E:\ReceptionWorkbench`的独立源码项目，源码保存在私有仓库[juccistar/reception-workbench](https://github.com/juccistar/reception-workbench)，默认分支为`main`。本目录使用自己的`.git`，后续开发、提交和推送均在该独立仓库进行。原仓库和临时导出分支仅保留为来源历史，见[项目与仓库约定](docs/PROJECT.md)。安装包发布和客户端更新继续使用原公开发行仓库[juccistar/reception-workbench-releases](https://github.com/juccistar/reception-workbench-releases)。

初次复制只纳入247个必要文件，未复制旧Git对象、发行包、历史截图、旧依赖目录或业务数据。初次导出时237个非文档文件与原检查点逐字节一致；本次独立项目调整仅涉及仓库元数据、打包入口和文档。原始来源与验收记录保留在[来源清单](docs/SOURCE_BASELINE.json)，当前仓库关系见[项目约定](docs/PROJECT.md)。依赖在本目录重新安装；新环境108项后端定向检查、87项工具测试、类型检查、构建和实际Demo窗口均通过，见[验证记录](docs/verification/0.3.3.json)。未运行完整测试套件，真实平台两轮发送与原14字符Ready展示仍待验证。

从本目录执行`npm start`可运行演示工作台；环境准备、源码修改后的构建、定向测试与候选打包见[开发说明](docs/DEVELOPMENT.md)。真实连接仍须使用当前目标商家的Accio授权环境；启动不恢复自动接待权限。

源码迁移不迁移已安装程序、业务数据或授权。旧目录中的真实业务根和完整备份仍需保全，不能整目录删除，见[迁移与清理边界](docs/MIGRATION.md)。

- [文档入口](docs/README.md) · [开发代理指引](CLAUDE.md)
- [用户指南](docs/USER_GUIDE.md) · [人工聊天](docs/人工接待使用.md) · [授权启动](docs/自动接待启动与授权.md)
- [现行规则](docs/CURRENT_RULES.md) · [实施契约](docs/IMPLEMENTATION_CONTRACT.md) · [架构](docs/项目开发/02-架构文档.md)
- [后续待办](docs/ROADMAP.md)
