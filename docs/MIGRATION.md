# 源码迁移与旧目录清理边界

当前`E:\ReceptionWorkbench`作为独立项目维护，源码仓库为私有的`juccistar/reception-workbench`，默认分支为`main`。本目录拥有自己的`.git`，没有链接旧仓库的Git目录或对象库，`origin`只指向该独立源码仓库；安装包和客户端更新继续使用`juccistar/reception-workbench-releases`。

来源记录保留原时点：工作台最初从`juccistar/alibaba-inquiry-quote`的`bc8661283f1c1cd0d75bcf3a1a04db8391af7a67`精简导出，并临时推送到该仓库的`maint/reception-workbench`分支。该分支及`feat/desktop-reception`只作为历史存档，不再作为本项目的维护入口。当前仓库关系见[项目约定](PROJECT.md)。

初次复制247个必要源码、配置、测试和相关文档，237个非文档文件逐字节一致。未复制旧Git对象、旧发行包、历史截图、基线ZIP、旧node_modules/虚拟环境、真实业务数据库、备份或凭据。当前文档已整理，另提供可复用的独立Demo窗口检查脚本。完整来源见[基线清单](SOURCE_BASELINE.json)，新环境验证见[记录](verification/0.3.3.json)。依赖在本目录重新安装，不依赖旧源码目录；通用Node、Python和发布签名密钥仍按开发要求安装或保存在用户本机。

发布附件另保存在本机`E:\ReceptionWorkbench-Releases\v0.3.3`，没有混入源码目录。它们也可从[GitHub Release](https://github.com/juccistar/reception-workbench-releases/releases/tag/v0.3.3)重新下载。

## 业务数据尚未迁移

本次没有安装程序、替换真实窗口、迁移业务库或恢复自动接待授权。旧工程内确有真实业务根、草稿、发送账本、回执和完整备份，**当前不能整目录删除**。本机`.local/CLEANUP.md`保存了确切路径与清理前检查单；该文件不提交Git。

删除旧目录前，先在现有程序设置中核对每个商家的实际数据位置，保存编辑、正常退出相应窗口并等待后台及原生子进程收尾，然后通过完整备份或迁移流程把业务及恢复集合保存到旧目录之外，并核验恢复结果。业务库、控制库、原生账本、回执、图片、来源元数据与有效WAL须整体保全；只复制单个SQLite文件不够。不要伪造clean标记、清unknown或让两个在线窗口写同一份恢复副本。

最后确认快捷方式、商家profile、运行副本和维护命令已不引用拟删除路径，再按具体清单处理旧源码和可重建缓存。父仓库其他工程的原有未提交修改不在本次工作台检查点内，清理父目录前须另外保全。发布私钥留在本机专用位置，不能移入源码或上传。
