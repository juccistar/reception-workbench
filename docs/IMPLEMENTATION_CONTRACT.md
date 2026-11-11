# 工作台实施契约

本文维护当前源码 0.3.3 的进程、接口和业务约束。发行及新目录验证正在进行，见[本版说明](0.3.3发布说明.md)；业务目标见[需求](项目开发/01-需求文档.md)。

精确参数、可选字段与错误码应同时核对[TypeScript契约](../apps/desktop/src/shared/api.ts)、[Python入口](../services/reception/src/reception_service/__main__.py)、[服务分派](../services/reception/src/reception_service/service.py)及对应回归。本文是稳定语义摘要，不用历史版本字段覆盖当前实现。

## 1. 系统边界

- Electron主进程负责窗口、受限IPC、原生确认、后台生命周期和离线数据管理；preload只暴露有限冻结桥。renderer没有Node、任意文件、数据库、shell或令牌接口。
- React/TypeScript负责界面和编辑状态；Python负责业务、权限/预算、版本、SQLite和外部适配。两者经UTF-8 JSON Lines通信，不开放本地HTTP业务端口。
- 包名reception_service，Python 3.12+；核心主要使用标准库，图片功能使用锁定的Pillow依赖，无Qt依赖。
- 新工程不在运行时导入旧Qt工程，不盲读旧业务库或账号目录。项目内vendor为可维护的适配副本，修改须有来源说明和回归，不是永久禁止修改的只读区域。
- `demo`与`live`隔离；真实卖家在核验身份后才打开相应业务库，一个服务生命周期不能跨卖家切换。Accio账号目录编号不直接当作卖家AliID。

[架构文档](项目开发/02-架构文档.md)维护模块分工；历史界面方案仅是设计来源，当前响应式、保存和可访问性行为以组件、样式和窗口验收为准。

## 2. 服务与协议

`ReceptionService(data_root:Path, mode='demo', seller_id='', emit=None)`提供公开业务调用和关闭；源码入口为`services/reception/run.py`及包`__main__.py`。源码进程使用隔离Python启动，发行版调用包内后台。业务运行参数包括`--mode demo|live --data-dir PATH --seller-id ID --workctl PATH`，可选项按当前解析器执行。

```text
请求：{id, method, params}
成功：{id, result}
失败：{id, error:{code, message, stage?, diagnostics?}}
事件：{event, data}
```

stdout只承载完整逐行JSON，正文换行经JSON转义；原生stdout/stderr、prompt、令牌及完整环境不进入用户错误或事件。DTO使用snake_case，时间为毫秒，缺失为null。

慢操作返回`{job_id}`并通过`job.updated`更新；`state.changed`通知页面重读。排队不等于执行，任务完成不等于平台受理或业务交接完成。外部业务worker串行执行，活动监测独立；本地查询、保存和暂停不应被长外部任务占住。线程/存储故障、编辑恢复和人工等待仍需完善，不能把设计要求写成已验收保证。

## 3. 数据与投影

| 对象 | 当前语义 |
| --- | --- |
| Snapshot | app、reception、checks、conversations、handoff_cases、policies、jobs、outbox、settings及message_sync；历史检查不代表实时授权 |
| ConversationSummary | 客户/会话归属、最新消息、事项状态、未读、reply_pending、human_owned及实际reception_status；reply_pending是尚未处理的真实客户回复义务，不由未读、clarifying或旧草稿推导，不能直接复制全局running |
| ConversationDetail | conversation、case、cases、messages、history、AI draft、独立chat_draft、reading及0.3.1的supplement和manual_suggestion |
| Message | 原平台标识、双方身份/CID、角色、正文、平台时间、附件元信息及own；原文与修订保留 |
| Case / Fact | 事项归属、版本、事实值、消息出处、缺失/冲突与采购状态；AI提取confirmed不等于业务员人工复核 |
| Draft / chat_draft | AI建议与会话人工正文分开；正文、来源、事项/资料/图片版本变化使旧确认失效 |
| Job | queued/running/succeeded/deferred/failed/cancelled/stale/unknown等实际阶段、时间及安全错误，归属不能串客户 |
| Operation / Outbox | 原操作与幂等、平台发送结果、异步登记及版本投影；发送、飞书登记和业务交接不是同一状态 |
| MessageImage / photo_observation | 安全预览、消息版本、原图哈希、逐图观察和不确定性；观察不直接升级为确认采购条件 |
| AssistanceTask | seller/buyer/conversation/case归属、kind、open/resolved/dismissed、revision、resolution、result_public及followup_status；协助状态不改变human_owned |
| 质量与恢复字段 | Draft.validation.warnings为稳定code/message数组；Job可带retry_at/retry_attempt/repair_count/last_error_code/retry_status，缺字段兼容旧记录 |

SQLite使用WAL及FULL同步，entities逐实体存JSON，revisions保留修订；当前Repository v2、Ledger v1。原图与预览保存在卖家业务库，其他未支持附件只保留元信息，不执行附件。完整目录、工作区锁、关闭、备份和恢复契约见[数据与运行结构](DATA_AND_RUNTIME.md)。

阅读水位按实际可见消息边界与observed_sequence确认；旧页面不能借用新序号清除未读。历史覆盖不完整必须明确显示，补历史不冒充新入站，不调用未核实的平台标已读接口。

## 4. 公开业务方法

精确白名单为`shared/api.ts`中的API_METHODS；renderer不能传任意方法、源目录或原生命令。以下按用途整理，字段校验仍由服务完成。

| 方法 | 参数及结果要点 |
| --- | --- |
| get_snapshot、list_jobs、list_outbox | 读取当前投影，不产生新授权或外部任务 |
| get_conversation | conversation_id及可选case_id；事项必须属于该会话，不改变当前事项 |
| get_messages | conversation_id、before_id、limit；返回分页和对应阅读观察信息 |
| mark_messages_read | conversation_id、through_message_id、observed_sequence；只确认本地已观察边界 |
| get_context | case_id；受限公开上下文，不含令牌、原图字节或任意本地路径 |
| get_message_image、load_message_image | conversation_id、message_id、source_version；只读状态或按明确版本排队加载，不取得发送授权 |
| begin_manual_edit | conversation_id；接管当前客户、使未提交旧自动意图失效 |
| save_chat_draft | conversation_id、body、expected_revision、client_mutation_id；保存独立人工正文 |
| save_conversation_supplement | conversation_id、body、expected_revision、client_mutation_id；会话补充CAS与幂等保存，空正文有效，不接管 |
| generate_manual_suggestion | conversation_id、可空case_id、client_request_id；返回job_id，独立于自动接待的来源条件，只展示最新成功建议 |
| request_translations、get_translations | 按会话及受限消息/事项来源请求或读取只读译文；服务校验归属及原文hash，读取不排队，失败可显式重试 |
| send_chat | conversation_id、draft_id、expected_revision、client_request_id；提交确切已保存版本，返回原请求关联的JobAck |
| save_draft | case_id、body、expected_revision、client_mutation_id；AI建议兼容编辑路径，不与普通手写发送混用 |
| save_policy、save_settings | 仅接受公开字段；资料修订使旧建议失效，设置保存撤销自动授权；不能保存凭据或任意文件 |
| set_scope | selected/all/incoming、conversation_ids；max_auto_replies兼容旧数字输入，0.2.22规范输出null、不参与限制；保存范围仍撤权 |
| set_reception | enabled、auto_send及兼容manual_only；本轮检查、范围及系统确认必须满足；某条旧草稿预演不是全局前置 |
| takeover | conversation_id、enabled；恢复自动需要明确操作，仍需全局授权有效 |
| prepare_send、confirm_send | 旧AI草稿兼容接口，保留确切版本、短期confirmation_id及最终校验；不是0.3.1主动建议采用或普通人工发送的前置 |
| reconcile_operation | operation_id；排队核查原操作，不重新提交或用核查时间改写原平台时间 |
| inject_demo | 仅demo可用，不伪造live买家入站 |
| sync_messages、fill_history | 增量/历史后台读取并保存覆盖与游标 |
| generate_draft | case_id、expected_case_revision；上下文、资料和图片观察按预算处理，生成自然正文与有来源事实 |
| check_connection、check_ai | 分别核验当前身份读取和一次明确合成AI；AI检查先暂停，不取客户业务上下文，不自动重试 |
| preview_send | draft_id、expected_revision；仅原生预演，无发送提交 |
| run_outbox、check_feishu | 本地队列后续同步与结构检查；飞书失败不阻断本地接待 |
| list_assistance_tasks | 可选conversation_id/status，返回items；任务归属于当前已核验商家 |
| save_assistance_result | task_id、expected_revision、result、result_public、schedule_followup、client_mutation_id；返回task、followup_id、followup_status；同mutation与内容幂等，内部结果不安排对客跟进 |

`shutdown`是宿主内部使用的关闭协议，不在renderer的`API_METHODS`白名单中；它撤权并等待在途结果保存，不强行把未决判为未提交。

`manual_only`只保留旧调用兼容，不是普通人工聊天的页面前置。保存期间的新输入和迟到任务结果必须保持正确版本与归属。

## 4.1 0.3.1主动建议、翻译及需求展示

右侧需求与补充分区，窄屏共用抽屉；顶部单一采购需求入口支持多事项、来源定位及从待报价进入指定事项。发送左侧单一主动生成按钮，建议在正文上方预览。会话补充只用于主动生成，不写入买家事实、政策或自动接待上下文；保存、生成、查看不接管、不改正文。无新消息、卖家最后发言、人工接管、自动暂停、无事项或缺项均不阻止主动生成，身份与AI能力仍须正常。采用无需确认，完整覆盖编辑框并接管；手写及人工发送同样接管。新结果、过期来源和迟到请求不得覆盖编辑正文或串会话。固定发送及unknown独立于编辑器，用户可继续准备下一条。

事实保留原始value与evidence_ids；可选display_value只作展示，缺失时按当前原文hash取历史翻译缓存，再回退原值，不用于事实确认或报价判断。保留缺项、冲突和来源，预算与卖价分开；字段齐全或逐项人工确认不是回复前置。内部摘要尽量简中，对客建议使用客户语言；不做语言比例、检测或外语重试。

消息翻译只读，真实模式须AI检查通过，原文和译文并列；按商家、会话、来源、原文版本、目标语言和提示词版本缓存，不写入事实、正文或发送队列、不接管。仅文字及图片附带说明，不做OCR；失败保留原文并可重试。独立辅助lane在主业务开始前取消或让出自有调用，关闭时等待其收尾；假Native测试不证明真实Native并发能力。

全局状态、客户接管、会话回复进度及采购进度各用相应投影；未读、reply_pending和clarifying彼此独立。发送成功在编辑区简短显示4秒；发送失败、连接/后台异常和unknown持续显示并提供处理入口，窄窗口保留生成与发送控件，本地保存、平台受理和飞书同步分别记录。

## 5. 人工与自动发送

普通人工发送不要求AI检查、采购齐全、商业资料依据、最后一条来自买家或全局running；保留身份/CID、正文格式与版本、退出撤权、幂等和全会话未决保护。人工消息进入真实历史与后续上下文，不自行修改采购事实或创建飞书登记。

自动接待要求当前检查和本轮授权；selected/all冻结相应名单，incoming以授权起点处理新消息。0.2.22取消AI与发送次数门槛，budget继续保存used/ai_used，limit/remaining/ai_remaining为null并标记unlimited。暂停不清零统计，设置或范围保存仍撤权，重启不恢复授权。人工编辑只暂停该客户；其他已授权客户不受影响，明确恢复也不能绕过全局范围。

正文校验保留ready/issues/sources并增加warnings；issues仅含硬错误，ready由其决定，商业条件/数字/重复追问等语义质量问题只进入warnings。事实逐项核验、非法提取丢弃并留提醒，真实字段冲突与正文质量分开；模型human动作产生协助信号，不自动设置human_owned。旧needs_human不等于人工接管，兼容流程保留历史且不能在只读投影中偷写。

生成失败的可恢复任务保存重试时间和次数，由后台到期重新调度，不在业务worker中sleep退避。每次尝试重新检查epoch、范围、客户接管、来源及未决操作。新消息取消旧来源任务后以最新上下文规划；暂停或重启不会恢复旧权限。一次简短确认与最终答复是不同阶段，协助回填的对客结果也须经过当前权限、确切版本和幂等链，accepted之前不能标已跟进。

0.3.1发送失败链按商家、会话、来源消息和阶段持久计数，协助阶段再区分结果版本；最多三次自动尝试（首次加两次），仅明确暂时性且提交未开始的失败按5秒、15秒退避。确定性或未分类拒绝停止本条，保留原因；新job、草稿和重启不重置计数。普通回复、确认、最终答复和协助跟进均适用，正常使用总量不限。可能已提交或unknown只核查原操作。

发送绑定确切正文、最新来源、需求/资料及图片版本，准备和原生提交前均复核。整会话prepared/sending/unknown阻止新增发送，不能换事项或请求键绕过。accepted需要平台消息编号与提交证据，明确未提交才可rejected；证据不足保留unknown并核查原操作。

快照规则兼容from及旧senderRole，冲突/未知角色拒绝；已核实的同身份、CID、messageId及角色下，Ready精确`[Card]`与平台精确`[image]`可在内存投影。对于已验证当前来源以外的历史消息，仅对代码识别的原生商品卡片描述和对应展示，匹配权威记录、角色及时间后投影，并在封存后重新核验；当前来源及其他正文仍严格核对，封存Ready不改。差异诊断只保留严格ID、枚举、类型/有界长度和比较布尔，不输出原文、URL或令牌。原始14字符Ready正文具体内容未知，真实Accio两轮发送尚未执行。

## 6. 原生适配与图片

`build_integrations(mode, data_dir, settings)`返回相同业务门面的实现，提供身份、近期摘要、分页消息、generate、图片加载/观察、preview_send、send、send_chat、register和revoke等能力。真实调用前核验当前运行环境与卖家归属；内部intent和authorization回调由服务生成，不能由renderer提供或持久化授权回调。

自动发送保留run→read-ready→seal→最新来源/最终权限→send-message受控链。普通人工路径的chatToken仅在内存/stdin，不进入argv、文件或账本。未知结果沿原操作和确切平台消息核查，不以相同正文猜消息编号，不重发试探。

图片源与原消息唯一关联；受控下载、完整解码及安全预览后，视觉观察绑定消息版本、图像哈希及任务。视觉和文字分别计真实调用，不再有次数预留或耗尽；普通视觉失败不阻断可用文字回复，未观察图片不能当作已识别。图片自身的资源及重试间隔限制继续保留。最近20条最多4张、静态PNG/JPEG/WebP、6 MiB等现行参数没有取消。

文字AI只能使用提供的上下文和安全图片观察，不自行抓客户数据或执行返回的工具。普通寒暄、规格偏好和待核实表达不强制问卷；具体价格、库存、交期和认证承诺需要适用依据。AI继续通过Accio，不因工程整理更换供应商。

## 7. Electron本机接口

布局get/savePreferences、confirmSend及chooseWorkctl经有限preload桥处理。存储getStorageInfo、chooseStorageDirectory、openStorageDirectory、manageStorage通过专门IPC；source由宿主选择，renderer不能传任意shell、文件读取或生产目录命令。

目录优先级：明确本次参数→已保存选择→默认根；保存根缺失不创建空库冒充恢复。数据管理先处理编辑、撤权和在途结果，完整备份、校验、比较发送历史及恢复到空目标后才切换；失败保留原根和选择，重启暂停。需要升级但缺正常退出证据时拒绝，未来/未知格式拒绝。

在线升级经独立的getUpdateInfo、saveUpdateSettings、checkForUpdates、downloadUpdate、installUpdate桥接，不进入Python业务方法白名单。页面只能提交公开GitHub仓库或静态HTTPS目录地址，不提交Token。共享解析器统一界面、宿主及构建规则；GitHub模式使用公开provider并仅允许该仓库Release路径及固定附件HTTPS域名，静态目录仍限制同源。发布公钥、安装路径、原业务根、重启参数及备份由宿主决定。只有安装版、固定公钥与已验证版本/下载候选允许安装，Windows助手持数据管理锁并正常关闭/备份，按作业对象等待安装器进程树结束，重启不恢复自动权限。详见[在线升级](在线升级.md)。

0.2.20的演示页通过getAccioLaunchInfo与copyAccioLaunchInstructions两个无参数专用IPC读取宿主生成的信息或复制到剪贴板。当前EXE的`--connect-from-accio`指令不绑定seller、dataRoot或workctl；先以当前授权Native resolve-id取得CURRENT_SELF_ALIID，选择registry中的seller profile，再由后台重新核验。页面不能提交路径、命令或环境，读取/复制不执行启动、不改配置、不调用AI或发送，仍校验窗口和主frame来源。

同seller跨数据目录全局排他，Native子进程继承占用；父进程异常退出但子进程存活时仍拒绝新owner。profile的state/storage.json对迁移后数据根权威；旧全局storage保留，旧根只在唯一匹配seller的正面证据下收编，未知、多重匹配或缺失拒绝。安装共享gate要求其他实例和孤儿后台退出，旧版本不参与该gate。重启保留原窗口seller约束并重验，自动接待暂停。真实双Accio并行接待闭环待验收。旧版本不参加新安装gate，首次手动升级仍须正常退出全部旧窗口。

新live首次建立空账本与空回执目录，未发送也可备份重开；已有账本丢失时底层Ledger.get拒绝重建，缺失历史不能当作空。

## 8. 验证与变更

自动化回归使用临时目录、合成数据和受控替身，不默认访问生产数据、其他任务记录或凭据。真实接口、AI、发送及飞书验证按用户已明确授权的对象和范围独立执行并留证；某轮禁止真实调用的协作指令不是永久禁用产品能力，也不授权后续扩大测试。

实际窗口需启动并交互，最终包独立验证，历史成功不代替新范围或持续运行验收。界面保存、原生提交和数据迁移等重要行为变化同步需求、架构、契约及测试。Git提交/推送依用户明确要求执行。
