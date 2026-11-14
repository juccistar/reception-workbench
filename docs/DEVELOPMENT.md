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

```powershell
npm run typecheck
npm run build
npm run test:backend
npm run test:ui
npm run test:tools
```

以上分别执行类型检查、桌面构建和后端/UI/工具测试；`npm test` 串行执行三组测试。小版本只运行与改动相关的定向测试，全量测试仅在用户明确指定时执行。`test:ui` 启动器会统一Windows盘符形式，避免Vite/Vitest因大小写产生重复运行时；直接调用Vitest时也须使用一致的工作目录。定向示例：

```powershell
$env:PYTHONPATH = 'services/reception/src'
.\services\reception\.venv\Scripts\python.exe -B -m unittest discover -s services/reception/tests -p 'test_manual_receipts.py' -v
npx vitest run --config apps/desktop/vitest.config.ts apps/desktop/tests/chat-service-client.test.ts
node --test scripts/tests/release-paths.test.mjs
```

`npm start` 启动演示工作台；若尚无桌面构建会先构建。真实连接须在目标商家允许使用工作台的 Accio Work 授权会话执行 `授权启动接待.cmd`，并在窗口核对商家和实际数据目录；启动本身不授权自动发送。按当前窗口检查 AI、选择范围并明确授权后，才可在授权范围操作。不要复制令牌；若宿主拒绝使用授权，停止并核实支持方式。普通人工聊天不依赖 AI 检查。

共享翻译配置的窗口回归先构建，再运行 `node scripts/smoke-shared-translation.mjs --label <未使用标签> --legacy-migration`；可加 `--executable` 指向候选EXE。它用两个临时Demo数据根及独立共享目录检查旧格式密钥迁移、跨窗口同步、开关隔离、冲突和重开。`smoke-demo.mjs` 继续检查缓存和原有回复建议。两者显式设置 `RECEPTION_SHARED_SETTINGS_DIR`，不得让自动测试写真实Windows共享配置；截图须实际查看，Demo不替代真实平台验收。

0.3.19窗口检查使用`node scripts/smoke-workbench-0319.mjs --label <未使用标签>`，检查实际renderer/preload和Demo后台上的宽窄窗口、页面可用但SDK失败、加载中与HTTP错误展示。宿主状态采用事件替身，公共页SDK故障另行检查。`node scripts/smoke-whatsapp-native.mjs --label <未使用标签>`使用实际`artifacts/app/main/index.js`中的宿主和锁定SDK进行无预热冷启动，等待inject结束后观察原始二维码至少60秒及一次刷新；添加`--fault-after-qr`检查首次二维码后的受控SDK异常与原生页面保留。`--host-entry`和`--runtime-entry`可分别指定包内编译宿主与SDK入口；夹具仅在内存替换业务启动，不代表真实商家启动。截图先遮去二维码等媒体，结果须逐张查看；这些脚本不登录实号或发送消息。窗口脚本串行执行。

0.3.18窗口检查使用`node scripts/smoke-workbench-0318.mjs --label <未使用标签>`，可加`--executable`指定候选EXE。脚本使用临时Demo库检查图片覆盖区删除、摘要、宽窄窗口，以及注入受限宿主健康事件后的停用发送、编辑保存和设置反馈；真实线程故障与撤权由后端定向用例验证，不把UI事件替身当作真实故障注入。`node scripts/smoke-whatsapp-native.mjs --label <未使用标签>`使用正式中文应用名与临时登录目录核对真实请求头、单页CDP、回调、预注入和公共扫码引导；`--reference-no-cdp`只作无SDK/调试协议的原始页面对照，不能替代正常模式验收。公共页未就绪时记录阻塞，不登录实号或保存二维码。截图须逐张查看。

0.3.7相关定向验证已覆盖100项后端、113项桌面及宿主用例，typecheck和build通过；源码和最终EXE的隔离smoke-demo及smoke-reception-upgrade窗口均通过，截图已实际查看。严格发行清单、ZIP完整性、固定Ed25519更新签名和源码/构建输入匹配核验通过，结果见[验证记录](verification/0.3.7.json)。真实图文Accio本轮未进行付费实测，未执行NSIS安装或替换本机程序。

当前源码开发版本为0.3.19，公开发行仍为0.3.18。0.3.19增加WhatsApp分阶段脱敏诊断、SDK故障后保留可信原始页面，以及独立于慢业务查询的宿主状态通知；保留需求增量规则，不加入旧数据清理。源码构建后立即提交f7f3c51；57项桌面、32项后端定向用例、1项SDK资源用例及类型检查通过，实际Demo窗口6张截图和两组公共页遮媒体截图已逐张查看。编译宿主无预热冷启动及受控SDK失败后均持续观察二维码超过60秒并覆盖刷新。结果见[本版说明](0.3.19发布说明.md)和[记录](verification/0.3.19.json)；本次未打包发布，真实商家连接故障尚未复现，实号收发未验收。0.3.18最终A候选已完成EXE窗口、包内SDK公共扫码、固定更新签名及四附件匿名完整下载核验，未执行安装替换或真实数据迁移。内嵌WhatsApp连接修复、执行器健康与队列保护、图片覆盖展示删除及实际验证限制见[0.3.18说明](0.3.18发布说明.md)和[记录](verification/0.3.18.json)。0.3.16的需求增量、多事项归属、原图预览／复制及接待交互见[发布说明](0.3.16发布说明.md)和[验证记录](verification/0.3.16.json)，紧凑布局、建议处理框、五图和附件输入继续沿用。原需求、摘要、产品及采购基础见[0.3.13开发说明](0.3.13开发说明.md)。接待页中间显示宽聊天区、独立会话摘要及编辑器，右侧为中文需求和业务补充。有效新产品ID原子建立公共产品、需求绑定及一次采购任务；采购授权独立，unknown只核查原记录。数字平台ID的商品链接由程序派生，本地“定制-数字”编号不生成平台链接。独立商品主图使用list-information的trunk/images组件，由受限媒体池读取及缓存；按imageIndex排序、URL去重后最多缓存五张，展示预览受独立传输预算约束，原图和找货引用不变；内部保留请求证据，界面不常驻解释来源。链接及主图不进入接待指定上下文或指纹，客户图片仍独立；采购等待主图时不占接待AI槽。旧提醒及解析RPC退役，需核实内容进入会话摘要，真实回执与续答防重关联保留。新建默认检测2秒、合并1秒、最近50条及必要依据，已有配置不改。

0.3.16窗口检查使用 `node scripts/smoke-workbench-0316.mjs --label <未使用标签>`，可加 `--executable relase/<候选目录>/installer-build/win-unpacked/ReceptionWorkbench.exe`。临时Demo库中实际验证原图分块、哈希、全尺寸预览及缩略卡／大图复制链路，完整链接复制与独立箭头、顶部历史与接待切换、弹层、固定底栏，以及真实Demo任务的需求增量、无变化不增需求修订、固定查看另一事项及重开。原生菜单选择、文件选择来源、剪贴板写入目标和外浏览器目标使用替身，不读写真实系统剪贴板；业务IPC与随包后台实际执行。`smoke-demo.mjs`另检翻译缓存和配置重开、建议及正常退出。多个窗口脚本串行运行，截图须实际查看；合成模型和媒体不替代真实平台／AI或操作系统剪贴板的端到端验收。0.3.15旧驱动及 `docs/design/0.3.15` 原型保留历史用途。

0.3.14历史附件窗口检查可运行 `node scripts/smoke-supplement-attachments.mjs --label <未使用标签> --executable relase/<候选目录>/installer-build/win-unpacked/ReceptionWorkbench.exe`；仅替换原生文件选择框以选中临时合成PNG、PDF及文本，上传、提取、持久化和业务IPC均实际运行。按同一EXE的启动互斥要求串行执行窗口脚本，正常关闭后再运行smoke-demo，避免同时冷启动相同安装路径。检查生成模块可用 `node scripts/sync-reception-prompts.mjs --check`，它不改写文件。

0.3.13窗口检查可运行 `node scripts/smoke-workbench-0313.mjs --label <未使用标签>`，添加 `--executable relase/<候选目录>/installer-build/win-unpacked/ReceptionWorkbench.exe` 可实际驱动安装包输入目录中的最终EXE，使用随包Python后台，不执行NSIS安装。该脚本验证独立摘要、无需求、派生链接、独立参考图、供应商/方案、窄窗及保存重开；`smoke-demo.mjs`另检翻译和建议。截图必须实际查看；夹具仅为隔离合成数据，不能替代真实业务验收。

0.3.12历史商品窗口检查可运行 `node scripts/smoke-products-0312.mjs --label <未使用标签>`，支持相同的`--executable`候选路径参数；实际执行商品字段、图片、移除表单、跨客户独立数量、正文/补充保存、窄窗和重开。跨客户商品ID为窗口夹具预置，后端关联另由服务回归证明。0.3.11需求窗口检查仍可运行 `node scripts/smoke-requirements-0311.mjs --label <未使用标签>`，或添加 `--executable` 指定候选运行目录中的EXE。脚本隔离数据、bootstrap和共享翻译设置，使用本地合成图片缓存及预置进度记录，验证两个内容区、像素展示、封面重开、补充、正文保存和窄窗。须实际查看截图；预置状态不证明模型语义正确，合成商品卡不证明任意平台商品链接支持SKU取图。

0.3.9源码已实现默认关闭的内置资料目录、用途隔离、共用接待上下文、协助任务明确开始及安全读取诊断；源码窗口与相关定向验证通过，见[本版说明](0.3.9发布说明.md)和[验证记录](verification/0.3.9.json)。本版已完成候选核验并公开发布；231项后端与80项桌面/宿主定向用例通过，最终EXE窗口、签名、源码资源及四个附件的匿名完整下载已核对。安装替换和真实发送仍需分别验收。资料页窗口回归可在构建后运行 `node scripts/smoke-policy-library.mjs --label <未使用标签>`，使用临时Demo数据和独立共享配置；`--executable` 可指定候选EXE，截图需实际查看。

workctl v0.1.65外部接口没有原生图片附件，当前以P05-B read_compat在同一业务任务中读取受限原图，p1/p2按双方消息时间顺序关联；不能将其描述为已实现原生附件直传。

## 候选打包

打包另需在该虚拟环境安装 `services/reception/requirements-build.lock` 中的构建依赖：

```powershell
.\services\reception\.venv\Scripts\python.exe -m pip install -r services\reception\requirements-build.lock
npm run package -- --candidate --output E:\ReceptionWorkbench\relase\candidate-example
```

输出必须是符合脚本限制的全新空目录；`--candidate` 避免更新默认发行入口。添加 `--installer` 可构建安装包；可选 `--update-url` 仅与安装包一起使用，接受受支持的公开 GitHub 仓库或 HTTPS 目录。安装包构建要求 `%LOCALAPPDATA%\AlibabaReceptionWorkbenchReleaseKeys` 内已有与源码固定公钥指纹匹配的 Ed25519 密钥，脚本不会为该发行自动创建或轮换密钥。私钥留在用户本机，绝不复制进源码、包或记录。更新元数据签名与 Windows 安装包代码签名是不同事项。

本项目安装包候选优先使用`npm run package:release -- --candidate --output relase/release-next`，该入口固定预置原公开更新仓库。源码提交推送到私有的`juccistar/reception-workbench`，默认分支main；上传安装包时GitHub CLI必须显式指定`--repo juccistar/reception-workbench-releases`。两仓库职责见[项目约定](PROJECT.md)。

打包、检查、安装、发布均须分别取得实际结果；源码版本及上述命令不代表这些步骤已完成。
