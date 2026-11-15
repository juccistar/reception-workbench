# 开发与运行

以下命令从 Windows PowerShell 的 `E:\ReceptionWorkbench` 执行。使用 Node.js 22.12 或更新版本及 Python 3.12；使用正常 Node 可执行文件，不要把 Electron 当作 Node 运行。

```powershell
cd E:\ReceptionWorkbench
npm ci
py -3.12 -m venv services\reception\.venv
.\services\reception\.venv\Scripts\python.exe -m pip install -e .\services\reception
```

本机重新安装依赖时发现Electron包未带运行文件。若`node_modules/electron/dist/electron.exe`不存在，执行`node node_modules/electron/install.js`，由该锁定版本的安装程序下载并校验运行文件，然后再启动；不要复制旧工程的node_modules作为替代。

后端运行时依赖以 `services/reception/pyproject.toml` 为准。脚本优先使用 `RECEPTION_PYTHON` 指向的 Python，否则使用此独立虚拟环境；确保它指向所需环境。

按改动范围执行定向测试；只有用户明确指定时才运行全量测试。以下示例从仓库根目录执行：

```powershell
npm run typecheck
npm run test:backend -- test_service_integration_040.py
npm run test:backend -- test_manual_receipts.py -k test_missing_or_invalid_acceptance_evidence_stays_unknown
npm run test:ui -- apps/desktop/tests/launcher-activation.test.ts apps/desktop/tests/translation-activation-host.test.ts
node --test scripts/tests/release-paths.test.mjs
```

`npm run test:backend -- <文件名> [-k <名称>]`经 `scripts/test-backend.mjs`运行：040及cached_media测试直接发现，旧测试由`services/reception/test_support/run_legacy_tests.py`显式装配合成服务兼容层；每个文件使用独立解释器。测试支持仅用于隔离合成回归，生产CLI只接受live，不提供`--mode demo`。`npm test`串行执行三组全量测试，不包含类型检查和构建；发布本身不是全量测试指令。`test:ui`启动器统一Windows盘符形式。

`npm start`默认打开启动台，尚无桌面构建时先构建；桌面源码修改后先运行`npm run build`，启动脚本不会检测已有产物是否过期。正式工作台须从目标商家Accio Work授权会话启动；登记索引不构成授权，关闭Accio或重启后独立复用授权尚未验证。启动台可切回已核验、仍运行的商家窗口。模型统一在启动台配置、按用途主动检查，检查调用真实API且可能计费；商家翻译启用由宿主原生确认并绑定实际数据根。普通人工发送不依赖模型检查。

每次完成新版本构建后，按仓库用户规则立即检查变更范围、敏感文件和`git diff --check`，将源码、测试、文档与可入库验证记录本地提交；不强制加入产物、业务数据或凭据。提交不授权推送、发布、安装或迁移。

0.4.0 的定向整合回归使用 `node scripts/verify-backend-040.mjs --label <新标签>`，只运行本版040文件和列明的受影响旧用例，逐文件保存日志，不是全量测试。先完成 `npm run build` 及构建后本地提交，再依次运行以下窗口检查，全部截图须逐张查看：

```powershell
node scripts/smoke-launcher-entry-040.mjs --label <新标签>
node scripts/smoke-launcher-040.mjs --label <新标签>
node scripts/smoke-workbench-040.mjs --label <新标签>
```

`smoke-launcher-entry-040`使用正式主进程；发现已有全局模型配置或新启动台索引时停止，不覆盖真实配置。默认检查空列表；若本机已有旧版登记，可显式加`--expect-legacy 1`检查其可见性，该模式要求激活目录也不存在，只读旧索引与精确引用的storage.json并核对前后哈希，不读取业务库或已有激活凭据。另两组使用正式renderer/preload与独立合成宿主，启动台覆盖旧登记发现和保持打开时刷新，工作台通过真实ServiceClient执行隔离Python业务，不能当作真实商家验收。候选入口使用`--executable <候选EXE>`；另两组使用`--assets <候选resources/app.asar>`核对包内界面资源，但仍不是候选正式主进程。

候选冻结后台可用`node scripts/smoke-packaged-models-040.mjs --label <新标签> --executable <候选resources/backend/reception-backend.exe>`检查两种协议的真实请求编码；它只连接临时回环HTTP替身，不调用付费模型、不打开商家库。公共WhatsApp扫码仍用下述native脚本指定候选宿主和SDK，正常及受控故障各观察超过60秒并覆盖刷新，不登录实号。构建、闭包、窗口、签名与匿名完整下载结果分别记录。

以下窗口脚本和验收是对应旧版本的历史记录，依赖当时的Demo入口或夹具，不能据此认定当前生产入口仍提供Demo或0.4.0已经验收。

0.3.21窗口检查使用`node scripts/smoke-auto-replies-0321.mjs --label <未使用标签>`，可加`--executable`指定最终候选EXE。脚本用临时Demo数据和预置合成AI回复，自动发送的原生确认仅在隔离夹具中返回允许，实际经业务IPC完成自动两条、三条独立受理、单条建议采用、整段润色、Ctrl+Enter及新输入保留；同步、核查和停止的中间状态截图明确采用投影夹具，不冒充真实平台发送。当前自动进度与折叠历史分别检查，截图须逐张查看。`smoke-whatsapp-native.mjs`使用当前编译的WhatsAppSessionHost提交新的合成登录代次，验证93字符宿主根与27字符分区，公共扫码稳定超过60秒并刷新；原生产资料不访问，意外认证立即停止且禁用历史读取。窗口脚本串行执行。

以下0.3.20与0.3.19旧分条/尾部恢复脚本只对应其原版本，0.3.21不执行已退役的人工分条或尾部提取入口。

0.3.20恢复窗口检查使用`node scripts/smoke-recovery-0320.mjs --label <未使用标签>`，可加`--executable`指定候选EXE。它在隔离Demo库核对历史尾部提取的真实业务接口、只提取未发送内容和人工草稿保护；同步等待投影、WhatsApp数据库错误及恢复进度采用受限合成夹具，不代表真实商家重建或发送。截图须实际查看，同一候选的窗口脚本串行运行。公共扫码及原生恢复协调器的实际范围见[0.3.20验证记录](verification/0.3.20.json)。

0.3.19分条窗口检查使用`node scripts/smoke-replies-0319.mjs --label <未使用标签>`，可加`--executable`指定候选EXE。它在独立临时Demo库预置合成两条建议，实际驱动建议采用、逐条编辑保存、当前条目Demo润色、两次独立Demo发送与回执、新输入保存及宽窄窗口；业务IPC不使用替身，预置建议不证明真实模型语义。诊断窗口检查使用`node scripts/smoke-workbench-0319.mjs --label <未使用标签>`，检查实际renderer/preload和Demo后台上的宽窄窗口、页面可用但SDK失败、加载中与HTTP错误展示。宿主状态采用事件替身，公共页SDK故障另行检查。`node scripts/smoke-whatsapp-native.mjs --label <未使用标签>`使用实际`artifacts/app/main/index.js`中的宿主和锁定SDK进行无预热冷启动，等待inject结束后观察原始二维码至少60秒及一次刷新；添加`--fault-after-qr`检查首次二维码后的受控SDK异常与原生页面保留。`--host-entry`和`--runtime-entry`可分别指定包内编译宿主与SDK入口；夹具仅在内存替换业务启动，不代表真实商家启动。截图先遮去二维码等媒体，结果须逐张查看；这些脚本不登录实号或发送消息。窗口脚本串行执行。

0.3.18窗口检查使用`node scripts/smoke-workbench-0318.mjs --label <未使用标签>`，可加`--executable`指定候选EXE。脚本使用临时Demo库检查图片覆盖区删除、摘要、宽窄窗口，以及注入受限宿主健康事件后的停用发送、编辑保存和设置反馈；真实线程故障与撤权由后端定向用例验证，不把UI事件替身当作真实故障注入。`node scripts/smoke-whatsapp-native.mjs --label <未使用标签>`使用正式中文应用名与临时登录目录核对真实请求头、单页CDP、回调、预注入和公共扫码引导；`--reference-no-cdp`只作无SDK/调试协议的原始页面对照，不能替代正常模式验收。公共页未就绪时记录阻塞，不登录实号或保存二维码。截图须逐张查看。

0.3.7相关定向验证已覆盖100项后端、113项桌面及宿主用例，typecheck和build通过；源码和最终EXE的隔离smoke-demo及smoke-reception-upgrade窗口均通过，截图已实际查看。严格发行清单、ZIP完整性、固定Ed25519更新签名和源码/构建输入匹配核验通过，结果见[验证记录](verification/0.3.7.json)。真实图文Accio本轮未进行付费实测，未执行NSIS安装或替换本机程序。

0.3.21当时的源码及公开发行为0.3.21，最终A候选构建307018f；源码4770407和候选均构建完成后立即本地提交。200项后端、191项桌面/宿主和1项SDK资源工具定向用例、类型及提示词一致性检查通过。源码与最终EXE的自动两条/三条Demo回执、单正文建议/润色、Ctrl+Enter和新输入窗口流程均通过，各8张截图已查看；中间等待/核查/停止采用投影夹具。包内宿主及SDK使用93字符临时根和27字符新分区，正常扫码与受控SDK故障后原页保留各超过60秒并刷新，两张遮媒体截图已查看。最终A候选已正式发布，公开latest、四附件匿名完整下载、源码/冻结后台/SDK闭包和固定Ed25519更新签名均核对通过，Windows Authenticode为NotSigned。相关说明及完整验收范围见[0.3.21发布说明](0.3.21发布说明.md)和[记录](verification/0.3.21.json)；未安装替换本机程序、迁移真实数据或推送私有源码，实际模型真人聊天效果及WhatsApp实号收发未验收。

以下为历史记录。0.3.20最终B候选构建225364c，相关说明及当时附件核验见[0.3.20发布说明](0.3.20发布说明.md)和[记录](verification/0.3.20.json)；发布时未执行安装替换。0.3.19历史记录：当时公开latest及四附件匿名完整下载已核对。0.3.19已增补需求补录、自然1至3条回复、两渠道逐条回执、人工分条编辑与当前条目润色；源码85a91ce及A候选3735711均构建后立即提交，后端200项、桌面142项定向用例及类型/提示词检查通过，源码和候选EXE各12张窗口图已逐张查看，最终A候选已正式发布，包内公共扫码正常及受控故障保留均超过60秒并刷新，两张遮媒体截图已查看，四附件匿名完整下载及固定更新签名已核对；旧需求保留、部分成功后只保留未发送内容及unknown不重发继续受保护。商品段落严格只删除指定前缀。以下f7f3c51和71c3fb7为此前WhatsApp诊断验收基线，新构建和发行状态见本版记录。WhatsApp分阶段脱敏诊断、SDK故障后保留可信原始页面及即时宿主状态继续保留，不加入旧数据清理。源码构建后立即提交f7f3c51；57项桌面、32项后端定向用例、1项SDK资源用例及类型检查通过，实际Demo窗口6张截图和两组公共页遮媒体截图已逐张查看。编译宿主无预热冷启动及受控SDK失败后均持续观察二维码超过60秒并覆盖刷新。结果见[本版说明](0.3.19发布说明.md)和[记录](verification/0.3.19.json)；本轮实际发行状态见验证记录，真实商家连接故障尚未复现，实号收发未验收。0.3.18最终A候选已完成EXE窗口、包内SDK公共扫码、固定更新签名及四附件匿名完整下载核验，未执行安装替换或真实数据迁移。内嵌WhatsApp连接修复、执行器健康与队列保护、图片覆盖展示删除及实际验证限制见[0.3.18说明](0.3.18发布说明.md)和[记录](verification/0.3.18.json)。0.3.16的需求增量、多事项归属、原图预览／复制及接待交互见[发布说明](0.3.16发布说明.md)和[验证记录](verification/0.3.16.json)，紧凑布局、建议处理框、五图和附件输入继续沿用。原需求、摘要、产品及采购基础见[0.3.13开发说明](0.3.13开发说明.md)。接待页中间显示宽聊天区、独立会话摘要及编辑器，右侧为中文需求和业务补充。有效新产品ID原子建立公共产品、需求绑定及一次采购任务；采购授权独立，unknown只核查原记录。数字平台ID的商品链接由程序派生，本地“定制-数字”编号不生成平台链接。独立商品主图使用list-information的trunk/images组件，由受限媒体池读取及缓存；按imageIndex排序、URL去重后最多缓存五张，展示预览受独立传输预算约束，原图和找货引用不变；内部保留请求证据，界面不常驻解释来源。链接及主图不进入接待指定上下文或指纹，客户图片仍独立；采购等待主图时不占接待AI槽。旧提醒及解析RPC退役，需核实内容进入会话摘要，真实回执与续答防重关联保留。新建默认检测2秒、合并1秒、最近50条及必要依据，已有配置不改。

0.3.16窗口检查使用 `node scripts/smoke-workbench-0316.mjs --label <未使用标签>`，可加 `--executable relase/<候选目录>/installer-build/win-unpacked/ReceptionWorkbench.exe`。临时Demo库中实际验证原图分块、哈希、全尺寸预览及缩略卡／大图复制链路，完整链接复制与独立箭头、顶部历史与接待切换、弹层、固定底栏，以及真实Demo任务的需求增量、无变化不增需求修订、固定查看另一事项及重开。原生菜单选择、文件选择来源、剪贴板写入目标和外浏览器目标使用替身，不读写真实系统剪贴板；业务IPC与随包后台实际执行。`smoke-demo.mjs`另检翻译缓存和配置重开、建议及正常退出。多个窗口脚本串行运行，截图须实际查看；合成模型和媒体不替代真实平台／AI或操作系统剪贴板的端到端验收。0.3.15旧驱动及 `docs/design/0.3.15` 原型保留历史用途。

0.3.14历史附件窗口检查可运行 `node scripts/smoke-supplement-attachments.mjs --label <未使用标签> --executable relase/<候选目录>/installer-build/win-unpacked/ReceptionWorkbench.exe`；仅替换原生文件选择框以选中临时合成PNG、PDF及文本，上传、提取、持久化和业务IPC均实际运行。按同一EXE的启动互斥要求串行执行窗口脚本，正常关闭后再运行smoke-demo，避免同时冷启动相同安装路径。检查生成模块可用 `node scripts/sync-reception-prompts.mjs --check`，它不改写文件。

0.3.13窗口检查可运行 `node scripts/smoke-workbench-0313.mjs --label <未使用标签>`，添加 `--executable relase/<候选目录>/installer-build/win-unpacked/ReceptionWorkbench.exe` 可实际驱动安装包输入目录中的最终EXE，使用随包Python后台，不执行NSIS安装。该脚本验证独立摘要、无需求、派生链接、独立参考图、供应商/方案、窄窗及保存重开；`smoke-demo.mjs`另检翻译和建议。截图必须实际查看；夹具仅为隔离合成数据，不能替代真实业务验收。

0.3.12历史商品窗口检查可运行 `node scripts/smoke-products-0312.mjs --label <未使用标签>`，支持相同的`--executable`候选路径参数；实际执行商品字段、图片、移除表单、跨客户独立数量、正文/补充保存、窄窗和重开。跨客户商品ID为窗口夹具预置，后端关联另由服务回归证明。0.3.11需求窗口检查仍可运行 `node scripts/smoke-requirements-0311.mjs --label <未使用标签>`，或添加 `--executable` 指定候选运行目录中的EXE。脚本隔离数据、bootstrap和共享翻译设置，使用本地合成图片缓存及预置进度记录，验证两个内容区、像素展示、封面重开、补充、正文保存和窄窗。须实际查看截图；预置状态不证明模型语义正确，合成商品卡不证明任意平台商品链接支持SKU取图。

0.3.9源码已实现默认关闭的内置资料目录、用途隔离、共用接待上下文、协助任务明确开始及安全读取诊断；源码窗口与相关定向验证通过，见[本版说明](0.3.9发布说明.md)和[验证记录](verification/0.3.9.json)。本版已完成候选核验并公开发布；231项后端与80项桌面/宿主定向用例通过，最终EXE窗口、签名、源码资源及四个附件的匿名完整下载已核对。安装替换和真实发送仍需分别验收。资料页窗口回归可在构建后运行 `node scripts/smoke-policy-library.mjs --label <未使用标签>`，使用临时Demo数据和独立共享配置；`--executable` 可指定候选EXE，截图需实际查看。

0.3.21时workctl v0.1.65外部接口没有原生图片附件，当时以P05-B read_compat读取受限原图；这一历史描述不代表0.4.0的验收结果。

## 候选打包

打包另需在该虚拟环境安装 `services/reception/requirements-build.lock` 中的构建依赖：

```powershell
.\services\reception\.venv\Scripts\python.exe -m pip install -r services\reception\requirements-build.lock
npm run package -- --candidate --output E:\ReceptionWorkbench\relase\candidate-example
```

输出必须是符合脚本限制的全新空目录；`--candidate` 避免更新默认发行入口。添加 `--installer` 可构建安装包；可选 `--update-url` 仅与安装包一起使用，接受受支持的公开 GitHub 仓库或 HTTPS 目录。安装包构建要求 `%LOCALAPPDATA%\AlibabaReceptionWorkbenchReleaseKeys` 内已有与源码固定公钥指纹匹配的 Ed25519 密钥，脚本不会为该发行自动创建或轮换密钥。私钥留在用户本机，绝不复制进源码、包或记录。更新元数据签名与 Windows 安装包代码签名是不同事项。

本项目安装包候选优先使用`npm run package:release -- --candidate --output relase/release-next`，该入口固定预置原公开更新仓库。源码提交推送到私有的`juccistar/reception-workbench`，默认分支main；上传安装包时GitHub CLI必须显式指定`--repo juccistar/reception-workbench-releases`。两仓库职责见[项目约定](PROJECT.md)。

打包、检查、安装、发布均须分别取得实际结果；源码版本及上述命令不代表这些步骤已完成。
