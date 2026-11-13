# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目与维护入口

客户接待工作台是面向 Windows 的 Electron + React/TypeScript 桌面应用，Python 服务承载业务，SQLite 保存本地状态，需求采集、回复和图片 AI 经 Accio Work 授权环境调用；聊天中文翻译可独立配置第三方通用文本模型。自动接待与普通人工聊天并存；飞书是可选的异步登记渠道。

源码在独立私有仓库 `juccistar/reception-workbench` 维护，默认分支为 `main`。安装包与客户端更新继续使用公开仓库 `juccistar/reception-workbench-releases`；旧 Qt 工程及导出分支仅是历史来源，运行时不导入旧工程。仓库、应用标识、数据命名空间与固定签名公钥的约定见 [PROJECT.md](docs/PROJECT.md)。

常用资料：[开发说明](docs/DEVELOPMENT.md)、[现行规则](docs/CURRENT_RULES.md)、[实施契约](docs/IMPLEMENTATION_CONTRACT.md)、[架构](docs/项目开发/02-架构文档.md)。当前源码及公开发行为0.3.15，采用A版紧凑三栏，修复资料保存、扩展五张商品图并支持多选/粘贴/拖入补充附件，见[发布说明](docs/0.3.15发布说明.md)、[验证记录](docs/verification/0.3.15.json)及[提示词规则核对](docs/0.3.15提示词规则核对.md)。用户追加要求已落实：不显示重复日期/翻译行或常驻建议条，建议和润色直接在输入框上方展示待处理框，替换/弃用后关闭，同一条已处理结果重开不重复显示；处理偏好与发送回执独立。提示词只清理多余解释，必要规则和限制继续保留。0.3.13的中文需求、独立摘要、程序链接及产品/供应商采购基础继续沿用。本版已打包发布，最终EXE窗口与固定更新签名已核对；最终候选构建完成立即提交33da0a1。未执行真实安装、默认入口晋级或业务数据迁移，不以公开版本推断本机已升级。自动定向询价和报价回收仍blocked，其他未完成能力见[ROADMAP](docs/ROADMAP.md)，合成回归不替代真实模型语义、平台、图搜或供应商询价验收。

## 新版本构建后的提交

每次完成新版本构建后，立即在本仓库创建一次 Git commit，不再积累多个版本后统一提交。提交说明使用中文，并写明版本号及主要变更；同时保存该版本相关源码、测试、文档及可纳入仓库的验证记录。提交前检查变更范围、敏感文件及 `git diff --check`，构建产物、业务数据、凭据和私钥继续遵守 `.gitignore`，不得强制纳入提交。

本条为用户对后续新版本构建完成后本地提交的持续授权，无需逐次再次询问。它不自动授权推送源码、公开发布、安装或迁移真实数据，也不构成运行全量测试的指令；这些操作仍按当前明确授权执行。

## 环境与命令

所有命令从仓库根目录执行。以下初始化和 Python 示例使用 Windows PowerShell；开发环境为普通 Node.js 22.12+ 和 Python 3.12。

```powershell
npm ci
py -3.12 -m venv services\reception\.venv
.\services\reception\.venv\Scripts\python.exe -m pip install -e .\services\reception
```

Python 运行时依赖见 `services/reception/pyproject.toml`。Node 脚本优先使用 `RECEPTION_PYTHON` 指定的现有解释器，否则使用项目虚拟环境。若缺少 `node_modules/electron/dist/electron.exe`，执行 `node node_modules/electron/install.js` 下载对应版本运行文件。

| 命令 | 实际作用 |
| --- | --- |
| `npm run build` | esbuild 打包 main/preload，Vite 构建 renderer，输出到 `artifacts/app`；不打包 Python |
| `npm start` | 启动 Electron，默认 Demo；仅当 main 构建产物不存在时先构建 |
| `npm run typecheck` | 按桌面端 tsconfig 执行 `tsc --noEmit` |
| `npm run test:backend` | 使用项目 Python 执行 unittest discovery |
| `npm run test:ui` | 执行桌面/宿主 Vitest 测试 |
| `npm run test:tools` | 用 Node 内置测试运行器执行 `scripts/tests/*.test.mjs` |
| `npm test` | 串行运行后端、桌面/宿主、工具三组测试，不包含类型检查或构建 |

修改桌面源码后先 `npm run build` 再启动，`npm start` 不检查已有产物是否过期。当前没有独立的 lint 命令。Vitest 默认使用 jsdom、单个 fork worker；宿主测试可通过文件注释改用 Node 环境。

### 定向测试与窗口检查

小版本更新默认按改动范围运行相关文件或用例的定向测试，并按需执行类型检查、构建或窗口验证。**只有用户明确指定时，才运行全量测试**（如 `npm test` 或依次跑完全部测试分组）；版本号变更、打包或发布本身不构成全量测试指令。

```powershell
# 后端单文件
$env:PYTHONPATH = 'services/reception/src'
$env:PYTHONIOENCODING = 'utf-8'
.\services\reception\.venv\Scripts\python.exe -B -m unittest discover -s services/reception/tests -p 'test_manual_receipts.py' -v

# 桌面/宿主单文件
npm run test:ui -- apps/desktop/tests/chat-service-client.test.ts

# 工具单文件
node --test scripts/tests/release-paths.test.mjs
```

后端命令追加 `-k test_missing_or_invalid_acceptance_evidence_stays_unknown` 可运行单个用例；上述 Vitest 命令追加 `-t "preserves Unicode"` 可按用例名称筛选。

`node scripts/smoke-demo.mjs --label dev-check-001` 会使用临时 Demo 数据实际打开 Electron，检查翻译、主动建议和正常关闭，并在 `artifacts/verification/demo-<label>` 保存报告及截图；先构建，每次换一个未使用的 label。截图验收需要实际查看图片。普通浏览器没有 `window.reception` 桥，不能作为完整工作台运行环境。

真实连接需在目标商家的 Accio Work 授权会话执行 `授权启动接待.cmd`。启动仅建立当前身份及读取能力，AI 检查和自动发送授权有各自的操作，详见 [授权启动说明](docs/自动接待启动与授权.md)。离线回归使用临时目录、合成数据和替身；真实平台读取、AI、客户发送和飞书写入分别按当前明确授权的对象与范围执行。

### 候选打包

```powershell
.\services\reception\.venv\Scripts\python.exe -m pip install -r services\reception\requirements-build.lock

# 便携候选
npm run package -- --candidate --output relase/candidate-next

# 安装包候选，预置原公开发行仓库为更新源
npm run package:release -- --candidate --output relase/release-next
```

`package` 会重新构建桌面端，用 PyInstaller 打包 Python 后台，再用 electron-builder 生成 Windows 发行物。输出只允许仓库内 `artifacts/` 或 `relase/` 下的子目录，且必须为空；`relase` 是脚本使用的实际目录拼写。`--candidate` 避免改写 `artifacts/latest-release.path` 和默认交付记录。

安装包构建要求本机 `%LOCALAPPDATA%\AlibabaReceptionWorkbenchReleaseKeys` 已有匹配固定 Ed25519 公钥的密钥，不会自动创建或换钥；该更新元数据签名与 Windows 代码签名不同。上传发行物时须显式指定 `--repo juccistar/reception-workbench-releases`，流程见 [PROJECT.md](docs/PROJECT.md) 和 [在线升级](docs/在线升级.md)。构建、发布、安装、默认入口晋级与业务数据迁移分别核验。

## 跨进程架构

- **界面与编辑状态**：`renderer/App.tsx` 组合页面，`renderer/hooks/useWorkbench.ts` 维护快照、会话选择、任务事件和按会话保存的编辑器。`state.changed` / `job.updated` 驱动重读；请求序号防止迟到结果串会话。人工正文及发送时冻结的版本分开，发送回执不能清掉发送期间的新输入；阅读确认与翻译分别由专门 hook 管理。
- **桌面宿主与桥**：`apps/desktop/src/main/index.ts` 管理窗口、IPC 来源检查、原生确认、后台生命周期、存储和更新。`preload/index.ts` 只向 renderer 暴露冻结的 `window.reception`。页面没有 Node、SQLite、任意文件或 shell 接口；存储、更新和授权启动指引走专用 IPC，不混入业务 RPC。
- **业务传输**：`main/service-client.ts` 管理一个自有 Python 子进程，通过 stdin/stdout 的 UTF-8 JSON Lines 关联请求和事件，不开放本地 HTTP 业务端口。请求为 `{id, method, params}`，响应为 `{id, result}` 或 `{id, error}`，事件为 `{event, data}`。stdout 只能输出协议，不能混入普通日志。
- **跨语言契约**：`apps/desktop/src/shared/api.ts` 定义 DTO、`ReceptionBridge` 和 `API_METHODS`，`shared/chat-protocol.ts` 校验人工聊天参数。新增或修改业务接口时同时核对 preload、main 与 Python 分派；字段使用 snake_case，时间使用毫秒。`confirm_send` 由宿主经专用原生确认入口调用，`shutdown` 仅供宿主内部使用。
- **Python 业务层**：`services/reception/run.py` 引导包入口 `src/reception_service/__main__.py`；`service.py` 中的 `ReceptionService` 负责业务分派、任务、授权和状态协调。慢操作先返回 `{job_id}`，后续通过任务事件更新结果。业务协调和外部 AI 等待分离，每商家一个 Accio AI 执行槽；消息监测独立，队列优先处理人工发送、回执核查及已就绪自动发送，并保留公平调度。本地查询、保存和暂停不应排在长 AI 请求后；接管、暂停及来源变化可取消旧生成。聊天第三方翻译使用独立HTTP队列，原生业务不抢占其请求；译文按商家持久保存。0.3.6的服务地址、模型和Key由宿主用Windows用户级DPAPI在同Windows用户间共享，目录取真实KnownFolder而非Accio重定向的LOCALAPPDATA；商家启用绑定实际数据根，更换端点或恢复到新根须重新确认，业务数据仍隔离。
- **上下文与外部适配**：`context.py` 组合有预算的消息、采购事实、适用资料、会话补充及受限原图引用。`build_integrations` 为 Demo/Live 提供同一门面：Demo 使用合成数据，Live 经 `integrations/live.py` 和 `integrations/accio_native/` 调用 Accio。原生副本来源见 `vendor/SOURCE.txt`，可维护但变更需保留来源说明及回归；需求与回复继续使用Accio；第三方Provider用于聊天文字和有效人工建议的中文翻译，不翻译采购事项或自动内部草稿。

0.3.13的Case.requirements_format=ai-v1，requirements固定product、product_id、product_url、product_images、quantity、specifications、customization、destination、delivery、budget、packaging、certifications、contact共13键，值为{value,evidence_ids,source_excerpts?,evidence_versions?}；除图片为string[]，其余为中文业务正文或null，不写display_value或字段提醒状态。AI依据真实需求整理字段，product_url由程序根据数字ID派生；程序不扫描历史链接或解析假定卡片/SKU代填，不另存平台标题。conversation_summary按商家+conversation_id独立保存，同会话多需求共用，无Case仍可有摘要和人工聊天；正常接待/人工建议同轮更新，润色仅改冻结正文。

product_library.py按商家+平台+产品ID原子建library_product、requirement_product_binding和一次procurement_task，供应商及报价用supplier/supplier_offer保存。数字商品ID的链接由代码生成 `https://www.alibaba.com/product-detail/_<ID>.html`，仅供展示和复制，不由AI查询或填写，也不进入接待指定上下文或语义指纹；原聊天及冻结润色正文中的链接保持原样。无平台ID的真实自定义需求可使用稳定复用的`定制-数字`本地编号，偶然碰撞时原子换号，不生成平台链接或读取平台图片。独立`product_main_image`由后台读取并安全本地缓存，仅供展示及采购找货，不进入接待photo_context、manifest或指纹，也不替代客户product_images；媒体池不占AI槽，采购可等待图片终态后采用图或文字找货。采购grant与接待独立，保存设置撤权、重启不恢复；单产品默认最多3家、可配置1至3。sourcing.py使用确定会话名，提交前持久确切prompt/hash/agent及任务编号；unknown只核查原记录，不因空值、客户切换或新job重搜。当前定向询价及回复回收未验收，inquiry_available=false，阶段明确blocked。所选有效供货方案及selection/offer/product版本进入生成和发送指纹，默认仅新绑定继承，进价不是销售价。客户预算、地址、聊天、补充和私图不共享。详见[实施方案](docs/0.3.13中文需求与产品供应商开发方案.md)。

0.3.9 包含[商家图片修复](docs/卖家图片与上下文.md)，0.3.10 取消固定每轮四张限制，默认整批 64 MiB、准备 60 秒、并行 2 项。仅当前事项明确关联的聊天产品来源图片或无竞争来源的单张买家图候选可进入相关接待任务；商家图片须有买家明确引用，普通正文润色不准备历史图。0.3.13已停用历史链接及cardType/fields/skus/imageUrl业务解析，真实消息/图片协议和受控缓存继续保留。需求图使用AI明确选择的本地media_id及本轮读取证据。独立商品展示图的只读证据见[报告](artifacts/verification/reception-0.3.13/product-image-component-accio.json)：正式list-information以数字productId、queryType=trunk及componentList=[images]请求，data为JSON字符串，图片位于basicInfo.images。响应未回传商品ID或主图标记，按确切请求绑定并取最小imageIndex作为展示参考，不称平台已证主图或SKU图；样本商品1601902417143在售状态未知。真实SKU及图搜链尚未验收。聊天图片按时间用p1/p2关联，明确标记role并匹配实际发送方，商家图不作为买家确认。workctl v0.1.65外部接口没有原生图片附件参数，当前使用同任务read_compat；仅有本轮确切原生读取证据才标记已看过，不保证Accio内部只有一次物理模型调用。每次请求独立组装本地上下文，不新增长期客户记忆。公司名由独立慢任务读取，快照不联网。

## 数据与业务语义

`repository.py` 使用 SQLite WAL 和 FULL 同步，以 `entities(kind,id,data)` 保存 JSON 实体、`revisions` 保存修订。消息、采购事项、AI 草稿、人工 `chat_draft`、任务、发送 operation 和 outbox 是独立对象；一个会话可有多个采购事项，编辑、接管和未决发送保护按会话执行。当前 Repository schema 为 v2，适配器 Ledger 为 v1。

一轮自动接待大致为：活动摘要发现变化 → 同步并持久化正文 → 组装上下文并生成草稿/事实 → 复核身份、来源版本及当前授权 → 原生提交 → 保存受理证据；飞书登记另经 outbox 后续处理。任务完成、本地保存、平台受理和飞书登记是不同状态。

新建或缺少配置时默认活动检测2秒、合并窗口1秒，现存明确配置不改；文字上下文取最近50条及窗口外必要依据。中文需求由AI直接整理，缺项留空，待报价读取ai-v1字段，不依赖旧facts或逐项复核表单。高级设置保留图片和翻译资源、提问次数及按品类的待报价字段；风格按优先级和适用范围选择。

维护时保留这些现行语义，详细条件以实施契约和源码为准：

- 普通人工发送不要求AI检查、已有采购需求、条件齐全或最后一条消息来自买家。手写、采用建议和人工发送接管当前客户；生成或查看建议不接管、不改正文。会话业务补充默认用于自动接待、人工建议及润色；第三方译文只供聊天展示，不改原始消息、正式需求或发送内容。
- 全局接待状态、human_owned、reply_pending、未读、采购进度及发送回执各自保存，不互相推导。0.3.13删除quality_notes/quality_warnings/validation.warnings及旧业务提醒、待协助实体与日常RPC，需核实内容写会话摘要；不把AI归纳当人工核实。业务补充续答继续保留稳定来源/问题ID、原operation及accepted证据和三次失败预算，不恢复逐项协助表单。
- 0.3.9 内置六份规则和三份待填写模板全部默认关闭，明确采用及启用后才进入当前商家上下文；旧非公开资料不自动进入 AI。公开事实、内部接待规则、聊天风格与仅人工备注分开；自动、建议及润色共用适用输入及版本。固定提示词由审核稿生成到静态模块，运行时不读取 docs，经营事实不写入固定提示词。
- 自动接待在当前授权范围内不限 AI/发送总次数，但继续保存统计。范围或设置保存撤权，重启不恢复权限；人工编辑只影响当前客户，恢复须明确操作。不再保存独立正文语义质量提醒，也不能将其升级为硬性issues或自动接管条件。
- 发送绑定确切正文及来源版本。整会话的 prepared/sending/unknown 阻止新增发送；unknown 沿原 operation 核查。accepted 需要平台消息编号和提交证据，不能用新请求键、换事项或同文猜测绕过防重。
- 单条自动发送失败链最多三次总尝试，仅明确未提交的暂时错误按 5 秒、15 秒退避；新 job、草稿或重启不重置计数。已提交或结果未知的操作只核查，不重发。
- 0.3.9 包含已在 0.3.8 候选核验的[发送通道简化](docs/自动发送通道简化.md)：正常自动回复和续答保留完整本地业务检查，直接使用聊天提交，不做发送前平台历史重读、账号Ready或Seal。提交后按确切消息编号有限读取确认；新适配器请求 purpose=reception 沿用来源防重键。旧快照 unknown 保留原回执核查，不能改通道重发；显式旧预演/确认只作兼容，不作为正常自动发送前置。

## 数据位置与恢复边界

实际业务根以商家 profile 的 `state/storage.json` 及设置页为准；选择优先级为本次明确 `--data-dir` → 已保存根 → 默认根。商家 registry 默认位于 `%LOCALAPPDATA%\AlibabaReceptionWorkbenchHost\merchants.json`，新商家默认根位于同级 `ARWData/<namespace>/<sellerId>`。身份核验后才打开相应卖家库，一个服务生命周期不跨卖家切换；同商家跨目录全局排他。

`demo/` 与 `live/` 数据隔离。真实数据集合包含控制库、`live/sellers/<seller哈希>/workspace.sqlite3`、`live/adapters-live.sqlite3`、`live/workctl-state` 中的回执，以及锁、退出和来源元数据；图片原图及预览保存在业务 SQLite 中。完整备份与恢复须整体保全账本、回执及有效 WAL，不能只复制单个数据库或把回执当缓存删除。

`main/storage-host.ts`限定维护参数，Python的`--maintenance`入口在接待服务实例化之前执行离线维护。0.3.13的prepare只在正常关闭、完整备份验证及当前源文件逐一hash匹配后按白名单清理旧提醒字段/实体和独立标题；entities与revisions一并处理，原始消息、图片、job/draft、operation/outbox、原生账本/回执及续答幂等标识保全，旧备份不改写。保存编辑、撤权、正常关闭、完整备份及历史比较先于切换数据根；恢复到空目标，不伪造 clean 标记，不清 unknown。源码复制不迁移业务数据，旧目录仍可能包含真实数据；详细位置和清理边界见 [DATA_AND_RUNTIME.md](docs/DATA_AND_RUNTIME.md) 与 [MIGRATION.md](docs/MIGRATION.md)。
