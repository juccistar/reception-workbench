# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 当前维护入口

本目录是单独维护的客户接待工作台项目，源码仓库为私有的`juccistar/reception-workbench`，默认分支为`main`；Git的`origin`只指向该独立仓库。后续开发、提交和推送在本项目进行。原`alibaba-inquiry-quote`仓库与当时的导出分支只作为历史来源，详见[基线清单](docs/SOURCE_BASELINE.json)，不再作为本项目的提交目的地。

安装包、便携ZIP及客户端更新继续使用公开仓库`juccistar/reception-workbench-releases`。源码仓库与发行仓库职责分开，发布命令须显式指定原发行仓库。推荐安装包构建入口为`npm run package:release -- --candidate --output relase/release-next`，固定预置原更新源；普通`package`入口保留通用打包能力。不要更换应用标识、数据命名空间或固定签名公钥，详见[项目约定](docs/PROJECT.md)。

先读[开发说明](docs/DEVELOPMENT.md)、[现行规则](docs/CURRENT_RULES.md)、[实施契约](docs/IMPLEMENTATION_CONTRACT.md)和[架构](docs/项目开发/02-架构文档.md)。当前发布与验证范围见[0.3.3说明](docs/0.3.3发布说明.md)，待办见[ROADMAP](docs/ROADMAP.md)。

## 构建与验证

使用普通Node22.12+和Python3.12，不把Electron当Node。首次执行`npm ci`，独立建立`services/reception/.venv`；打包依赖按`services/reception/requirements-build.lock`安装。常用命令为`npm run typecheck`、`npm run build`、`npm start`；完整验证入口为`npm test`，按改动优先选择有意义的单项。后台单项示例和桌面/工具筛选见开发说明。

构建输出在`artifacts/app`；源码修改后先构建再运行。`node scripts/smoke-demo.mjs --label <新标签>`使用独立Demo数据，实际打开窗口、检查翻译/建议并正常关闭。截图必须实际查看。打包使用全新候选目录及`--candidate`，不自动晋级本机入口。发行沿用固定Ed25519公钥；私钥仅留在用户本机，不能复制、提交或静默换钥。

## 架构与产品约束

- Electron main管理窗口、有限IPC、后台生命周期、存储与升级；preload暴露冻结桥，React renderer不直接访问数据库、文件系统或凭据。共享DTO、IPC白名单和Python分派须同步。
- Python/SQLite通过stdin/stdout UTF-8 JSON Lines服务，不开放业务HTTP。业务外部worker串行，消息监测独立，翻译辅助可为主任务取消让位。stdout仅用于协议。
- AI和平台继续经Accio适配，vendored原生源码来源在`services/reception/src/reception_service/integrations/accio_native/vendor/SOURCE.txt`；不导入其他旧工程或另换模型SDK。
- 自动接待为主，客户页始终保留普通人工输入发送。生成或查看建议不接管、不改正文；采用建议整段覆盖并沿人工编辑路径接管。会话业务补充仅供主动建议，实际发送后才作为真实历史。
- 翻译和中文展示只作辅助，不改变原文、事实或发送内容；不加语言检测或中文比例门槛。
- 身份、归属、来源/版本、授权、epoch、人工接管与防重是硬保护。普通人工聊天不以AI检查或采购条件齐全为前置。prepared/sending/unknown阻止整会话新增发送，unknown只核查原操作。
- 自动发送失败链首次加两次最多三次，仅明确未提交的暂时错误按5秒/15秒重试；不恢复全局AI或发送次数上限。重启不恢复自动接待授权。
- 历史卡片兼容只对已识别平台描述与明确原生标记/一致描述做内存投影，核实时钟并复用最终平台历史复核；当前来源和普通文字仍严格。原14字符Ready内容与真实平台两轮发送未验证，不把合成通过写成真实故障已彻底消除。

## 授权、数据和交付

源码复制、提交及普通离线测试不读取或复制真实客户库、凭据或发布私钥。真实平台读取、AI、客户发送、飞书业务写分别按当前会话明确对象与范围执行；外部环境拒绝后不换身份或通道绕过。

实际数据根以商家profile与设置页为准，源码复制不迁移程序、业务数据或授权。旧业务根、完整备份、账本、回执、图片和有效WAL必须保护，不能当构建缓存删除，也不能伪造clean或清unknown。参见[迁移与清理边界](docs/MIGRATION.md)。

版本、提交、发布、已安装程序、默认运行副本和真实窗口分别核实。测试和公开发布都须如实记录范围；源码推送、Release上传、安装及业务迁移是不同操作。
