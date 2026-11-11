# 独立项目与仓库约定

`E:\ReceptionWorkbench`作为单独的客户接待工作台项目维护，拥有自己的Git仓库和开发环境。

| 用途 | 仓库 | 规则 |
| --- | --- | --- |
| 源码、开发文档、提交与版本维护 | [juccistar/reception-workbench](https://github.com/juccistar/reception-workbench) | 私有独立仓库，默认分支main；本地origin仅指向此处 |
| 安装器、便携ZIP、blockmap和签名更新元数据 | [juccistar/reception-workbench-releases](https://github.com/juccistar/reception-workbench-releases) | 继续使用原公开发布仓库，客户端更新源保持一致 |

## 日常维护

在本目录开发并向独立源码仓库的main分支提交推送。原`alibaba-inquiry-quote`仓库和`maint/reception-workbench`导出分支只作为历史来源，不作为当前项目的提交目的地。当前源码仓库是独立创建的私有仓库，不是GitHub fork，也不是旧仓库中的工作分支。

原始来源提交、文件指纹和初次导出验证保留在[SOURCE_BASELINE.json](SOURCE_BASELINE.json)及[0.3.3验证记录](verification/0.3.3.json)。这些字段描述当时的导出过程；当前仓库关系以本文件及Git远端为准。项目独立后的检查见[仓库分离记录](verification/project-separation.json)。

## 安装包与更新

正式安装包候选优先使用固定发行目标的入口：

```powershell
npm run package:release -- --candidate --output relase/release-next
```

该入口预置`--installer --update-url https://github.com/juccistar/reception-workbench-releases`。普通`npm run package`保留通用打包功能；需要自定义构建时，须明确检查更新地址，不能从源码仓库origin推断发行仓库。

上传安装包时，GitHub CLI命令必须显式指定：

```text
--repo juccistar/reception-workbench-releases
```

先创建草稿、上传并核对附件和签名后再发布。不要把安装包Release创建在私有源码仓库，也不要改写已发布的0.3.3附件。构建、上传、安装、入口晋级及业务迁移分别记录。

项目独立不改变程序的package名称、版本语义、应用标识、数据命名空间或固定Ed25519公钥。既有客户端继续从原仓库更新，私钥仍留在用户本机，不复制进源码。已发布0.3.3及其验证范围见[版本说明](0.3.3发布说明.md)。

源码独立不迁移原业务数据或授权；旧目录清理仍须遵守[数据保全边界](MIGRATION.md)。
