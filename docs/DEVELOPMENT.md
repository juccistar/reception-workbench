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

以上分别执行类型检查、桌面构建和后端/UI/工具测试；`npm test` 串行执行三组测试。按改动选择有意义的定向测试，例如：

```powershell
$env:PYTHONPATH = 'services/reception/src'
.\services\reception\.venv\Scripts\python.exe -B -m unittest discover -s services/reception/tests -p 'test_manual_receipts.py' -v
npx vitest run --config apps/desktop/vitest.config.ts apps/desktop/tests/chat-service-client.test.ts
node --test scripts/tests/release-paths.test.mjs
```

`npm start` 启动演示工作台；若尚无桌面构建会先构建。真实连接须在目标商家允许使用工作台的 Accio Work 授权会话执行 `授权启动接待.cmd`，并在窗口核对商家和实际数据目录；启动本身不授权自动发送。按当前窗口检查 AI、选择范围并明确授权后，才可在授权范围操作。不要复制令牌；若宿主拒绝使用授权，停止并核实支持方式。普通人工聊天不依赖 AI 检查。

## 候选打包

打包另需在该虚拟环境安装 `services/reception/requirements-build.lock` 中的构建依赖：

```powershell
.\services\reception\.venv\Scripts\python.exe -m pip install -r services\reception\requirements-build.lock
npm run package -- --candidate --output E:\ReceptionWorkbench\relase\candidate-example
```

输出必须是符合脚本限制的全新空目录；`--candidate` 避免更新默认发行入口。添加 `--installer` 可构建安装包；可选 `--update-url` 仅与安装包一起使用，接受受支持的公开 GitHub 仓库或 HTTPS 目录。安装包构建要求 `%LOCALAPPDATA%\AlibabaReceptionWorkbenchReleaseKeys` 内已有与源码固定公钥指纹匹配的 Ed25519 密钥，脚本不会为该发行自动创建或轮换密钥。私钥留在用户本机，绝不复制进源码、包或记录。更新元数据签名与 Windows 安装包代码签名是不同事项。

本项目安装包候选优先使用`npm run package:release -- --candidate --output relase/release-next`，该入口固定预置原公开更新仓库。源码提交推送到私有的`juccistar/reception-workbench`，默认分支main；上传安装包时GitHub CLI必须显式指定`--repo juccistar/reception-workbench-releases`。两仓库职责见[项目约定](PROJECT.md)。

打包、检查、安装、发布均须分别取得实际结果；源码版本及上述命令不代表这些步骤已完成。
