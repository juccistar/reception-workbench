# 工作台实施契约

本文维护当前源码的进程、接口和业务约束，0.4.1新增账号、侧栏及共享额度见下节；统一启动台、全局多API及同轮图文调用见[0.4.0方案](0.4.0开发方案.md)，实际实施、构建及公开状态见[0.4.0记录](verification/0.4.0.json)。真人聊天式自动分条、单条人工输入及独立发送记录沿用[0.3.21方案](0.3.21开发方案.md)和[发布说明](0.3.21发布说明.md)。沿用0.3.20分条同步等待、防重及登录恢复基础。需求增量、多事项及原图交互继续沿用0.3.16基础。0.3.13的需求、摘要、产品及采购契约继续沿用，见[基础开发说明](0.3.13开发说明.md)。精确字段同时核对[TypeScript契约](../apps/desktop/src/shared/api.ts)、[服务分派](../services/reception/src/reception_service/service.py)及对应测试。历史版本字段不能覆盖当前实现。

## 0.4.1开发新增契约

实际构建和发行状态见[0.4.1记录](verification/0.4.1.json)。本节约束新账号入口；下述旧版本内嵌WhatsApp条目仅用于原账本和历史行为说明，不能重新启用商家窗口的旧发送通道。

普通启动台首页不启动商家或WhatsApp后台。账号管理通过独立冻结的window.launcher和明确的launcher:wa:* IPC调用，公开类型见[whatsapp-account.ts](../apps/desktop/src/shared/whatsapp-account.ts)。`--whatsapp-account --workspace-id <UUID> --data-dir <账号根>`只创建独立账号服务，不伪造seller、不构造Accio商家执行器。真实系统Google Chrome以专用profile、可见窗口和私有pipe运行，不开放调试TCP端口；账号持有者核验完成之前不读取联系人和历史。商家工作台不再创建内嵌WA入口。

启动台以全部/国际站/WhatsApp三个分类并列账号卡片，所有WhatsApp详情和操作显式携带workspace_id，不用selected或全局单活动实例路由。runtime、启动、操作互斥、错误和退出任务按workspace隔离；不同真实账号可并行运行。改名按expected_revision核对，国际站本地显示名独立于已核验公司身份保存；复制授权口令使用宿主info.instructions完整三段。

账号与workspace的私有摘要映射及永久账本归属防止同号换环境绕过原账本。同一Windows用户按workspace和真实账号分别持有消费者锁；同账号第二workspace或不同账本根被拒绝。仅有确切LID/PN对应时才按PN归一所有权，未证明的别名不合并。关闭先撤新任务资格，保全在途结果并关闭明确自有Chrome，等待模型同步、上下文和后台实际结束后释放。后台错误使RPC立即不可用，子进程close才构成释放所有权的结束证据。异常关闭不伪造clean标记。

默认商家只在首条有效实时买家消息时冻结；历史、联系人metadata和人工外发不提前选择。显式绑定/解绑优先。切换原子生成新segment及binding_revision，清除旧段pending并设置独立reception_paused；恢复必须明确操作。旧prepared/sending/unknown不因切换清除，待切换意向在原操作结果明确后才能生效。

提交正文前，账号服务先持久写submission_started和unknown；宿主另在SDK调用前持久冻结意图。accepted需要确切平台消息编号和ACK≥1，unknown仅核查原操作。后台退出后的本地SDK结果可以沿原宿主账本保全，不伪称已写回账号库。真实商家背景经私有双向HMAC通道获得，绑定seller、实际根、实例、账号、聊天及绑定修订；发送前重新取得短租约并核对语义修订，不能用过期缓存继续发送。接待模型变化撤权，独立翻译配置变化不撤销无关接待。

原生客户侧栏由自有Chrome的私有pipe安装固定扩展，只向准确扩展origin、frame及默认执行上下文注入私有binding。随机能力令牌与workspace_id、runtime_instance_id、account_id、connection_epoch、chat_id及view_revision共同核验，普通WhatsApp网页没有业务binding。当前客户须为唯一active单聊、无活动频道，并经SDK及第二次当前ID读取复核；无法确定时清空。客户候选只读当前已核验商家已保存的名称和买家标识，最多25项，不读消息正文、不补造公司字段；来源或视图变化后拒绝迟到结果和操作。

同Windows用户全部国际站、WhatsApp账号与模型能力检查共用SQLite额度账本：接待1、翻译1、活跃HTTP合计2、带图1；有界队列按owner公平调度。宿主覆盖外部同名环境变量，目录由真实Windows KnownFolder确定。租约归属核对PID及进程创建时间，活进程不按TTL回收；取消后实际任务及输入清理完成才释放额度，不持业务锁等待模型。

消息图片与业务补充原图同轮提交。选图、准备和上下文使用同一当前事项/会话选源结果；有效定制等需求字段也可引用买家图，无事项只允许当前窗口无竞争的单张买家图。明确禁用、未被买家引用的商家图片、其他客户和独立商品展示图排除。原生P05-A只随实际编码的附件追加，正文沿用审核稿；聊天记录和附件按确切来源在单次请求去重。固定system提示词原样，user依次组装knowledge、guidance、style和context；客户context依次放补充、摘要、需求/供货、聊天及本轮目标。只改变最终JSON对象键顺序，保留正文、数组、角色、语义指纹、预算及原图，润色不因此新增历史图片或接待指引。

反写的版本化来源、目标、预览、提交及回执DTO已预留，能力固定available=false、enabled=false。私有预览/提交入口在关闭状态直接拒绝，不创建任务、不改库、不调用国际站。离线账号维护通过独立`--whatsapp-maintenance backup|restore`分派，在任何服务实例化前执行，保全unknown与源档案，恢复至空目标后自动接待关闭；目录和范围见[数据与运行](DATA_AND_RUNTIME.md)。

## 1. 进程与协议

Electron main管理窗口、受限IPC、原生确认、后台及离线存储维护；preload只暴露冻结window.reception。renderer没有Node、SQLite、shell、任意文件、令牌或通用URL打开接口。Python 3.12服务经自有stdin/stdout UTF-8 JSON Lines通信，不开本地HTTP业务端口，不在运行时导入旧Qt工程。

```text
请求：{id, method, params}
成功：{id, result}
失败：{id, error:{code, message, stage?, diagnostics?}}
事件：{event, data}
```

stdout仅输出协议。原生输出、完整prompt、凭据及环境不进入普通错误和事件。公开DTO使用snake_case、毫秒时间及null缺值。慢操作返回job_id，由job.updated更新；state.changed要求重读。排队、任务完成、本地保存、平台受理和飞书登记是不同状态。

每商家业务进程使用持续生命周期的模型事件循环，接待最多1个、翻译最多1个、活跃HTTP合计最多2个，带图生成最多1个。采购沿独立的Accio执行槽，业务协调和消息监测独立；队列优先人工发送、回执核查及已就绪自动发送，兼顾公平。同步业务回调和权威配置文件检查移出模型事件循环，HTTP前仍核配置与来源。保存、暂停及本地查询不排在长AI等待后。取消先撤结果和出站资格，工作线程或网络任务真正结束后才归还资源，不保证远端计费同时停止。

模型调用按商家、用途、通道和稳定来源持久限制为最多两次实际HTTP，不因新job、配置切换或重启重置。接待总期限180秒、翻译30秒，图片准备计入原期限；明确429可在同一期限内按Retry-After追加一次，网络未知及5xx不重放，SDK隐式重试关闭。持久领取由真实模型任务结束后收尾；确定没有HTTP出站的取消或配置变化记为unsubmitted并归还次数。清理超时先返回时，迟到回调仍负责完成原领取，不提前释放任务资源。此模型预算与平台发送的三次失败链分别保存。

正式入口只允许Live，身份核实后才打开对应卖家库；单个服务生命周期不跨卖家切换，同商家跨目录排他。合成适配器、种子和窗口夹具位于独立test_support/fixtures，不进入正式源码暂存和冻结归档。旧Demo数据不自动删除或迁入Live。启动台只提供独立冻结window.launcher桥，不开启业务Python或加载WhatsApp SDK。真实读取、AI、发送和外部写入分别服从当前明确授权。

## 0.3.20新增契约

SendAuthorization保持严格bool callable，内部SendGateDecision按本次检查固定原因；before_claim、after_token与before_submit拒绝由原生异常和operation立即保存gate_reason/gate_checkpoint/token_acquired等白名单事实，不事后猜测通用auth。communication_token_missing与source_sync_pending分别处理。

reply_batch与当前part增加waiting_sync及sync_wait_started_at/sync_wait_deadline_at/sync_target_version/sync_target_timestamp/wait_reason。等待不睡眠、不占执行线程、不重复生成或全量预读；同会话互斥，首次60秒截止不刷新。仅明确未提交且不足三次的原条目可等待后重校验；prepared/sending/unknown及原账本保护保留。自身回显投影使用锁内必回滚savepoint，不修改真实消息；只允许确切accepted ID的传输时间修正且顺序不变，不忽略其他正文、身份、附件或业务依据变化。

0.3.21移除recover_reply_batch_draft常规业务入口及ConversationDetail.recoverable_reply_batches投影，历史实体与原操作保留。ReplyBatchProgress保留等待及具体门控原因，增加origin/channel/phase、created_at/updated_at、会话或聊天标识、逐条part_index/status/message_id、message及存在时的retry_at。界面使用后台投影区分当前与历史，不根据当前输入框猜测原批次状态。

rebuildWhatsAppSession为专用宿主IPC，不加入API_METHODS、不开放路径/partition/商家参数。确认前后核对商家、连接及当前登录代次；WhatsAppCoordinator先撤权、等待在途结果和观察、销毁旧视图后原子保存whatsapp-session-generation.json，再建立新宿主。generation=0及v1已保存代次原样复用旧分区算法；0.3.21仅在确认新建时写v2配置与27字符partition_name，校验名称、重复和目录占用，旧程序不识别v2且不得静默回退。新代次不改变业务库和JSONL发送账本位置。配置损坏拒绝连接，提交前失败保留原配置，提交后重新启动沿新代次，均不恢复接待权限。旧账号未决通过other_account_pending_count及仅operation_id/status的受限列表提示，不暴露旧联系人；核查仍使用原账号、原操作epoch和确切平台ID。

## 2. AI需求、程序商品展示与会话摘要

Case.requirements_format为ai-v1，requirements只允许下列13键：

| 键 | 业务内容 | value |
| --- | --- | --- |
| product | 简短中文产品 | string或null |
| product_id | 有来源的数字平台ID或稳定本地定制-数字编号 | string或null |
| product_url | 程序由数字平台ID生成的展示/找货链接 | string或null |
| product_images | AI选择的可信图片媒体引用 | string[] |
| quantity | 数量与单位 | string或null |
| specifications | 关键规格 | string或null |
| customization | 客户定制要求 | string或null |
| destination | 收货地 | string或null |
| delivery | 客户期望交期 | string或null |
| budget | 客户预算 | string或null |
| packaging | 包装要求 | string或null |
| certifications | 认证要求 | string或null |
| contact | 联系方式 | string或null |

每值为{value,evidence_ids,source_excerpts?,evidence_versions?}。除派生商品链接外，业务正文由AI用中文归纳，原文另作证据，不强制中文归纳是外文连续子串。无依据留空，无display_value、提醒状态或人工复核表单。旧facts/翻译不自动升级成新需求。

商品链接不再由AI填写。product_identity.product_link仅对数字平台ID生成https://www.alibaba.com/product-detail/_<ID>.html；定制-数字是本地编号，URL为null，不作平台商品查询。定制ID须有真实产品需求依据、同事项稳定复用，碰撞时原子分配不同本地编号，不能误合并客户。AI或找货结果不能替换派生链接。其他需求仍不由程序扫描历史链接或假定cardType/fields/skus/imageUrl代填，不另存平台标题。

新增Case/Product.product_main_image独立展示元数据：local_product_id、status、updated_at、message、width/height（可选），并标display_only，可保留primary_verified、selection_rule。get_product_main_image(local_product_id)仅本地返回安全data_url，列表/多事项投影不携带大图或原始source_url。后台调用icbu.product.list-information，JSON参数为productId（整数）、queryType=trunk、componentList=["images"]。仅解析已观察到的data（JSON字符串）→basicInfo.images[]，按imageIndex排序、对originalImageUrl去重并取最多五张，分项下载及安全缓存，部分失败不影响其余图片。images数组按原顺序投影status及可选尺寸，本地图片RPC可带各项data_url；顶层data_url兼容第一张有效缓存图，快照不含base64。独立展示预览将较大缓存预览转为不超过128 KiB的512像素缩略图，五图及兼容封面响应保持在预算内，原始资产和找货引用不改。旧单图记录持久标记image_group_version并安排一次补齐，loading/unknown及失败不反复查询。响应不含商品ID，因此按已核身份及确切请求参数绑定，不声称响应回验；若显式返回异ID则拒绝。没有主图标志，primary_verified=false、selection_rule=lowest_image_index，界面不再常驻显示来源解释，内部仍记录确切请求和排序证据；不称SKU图。安全缓存放product_main_image及image_asset，不创建message_image关联，不替代或合并原客户product_images。

链接和主图不进入接待模型指定字段、photo_context/image_manifest或语义指纹；补图不增事项业务版本、不使旧回复过期。它们可以进入采购专属上下文，图搜参数只接受冻结的确切公共AliCDN主图URL。原消息或冻结润色正文中的链接原样保留。来源正文修改仍使实际需求失效，并解除旧供货关联；产品和原执行证据保全。

ConversationDetail.conversation_summary为{conversation_id,body,revision,updated_at}或null，独立商家及会话归属；ConversationDetail.case可为null。同会话多需求共用摘要，切换需求不换摘要，无需求仍支持聊天、补充和摘要。正常接待、人工建议经来源指纹复核后同轮保存中文摘要和有效需求；润色只处理冻结正文，不写入两者，不启动采购。

删除quality_notes、quality_warnings、语义validation.warnings、assistance_requests/suggestions、旧待核实/待协助及business_progress提醒生成与投影。摘要自然说明业务上仍需核实的内容，不强制每段添加提醒。真实任务错误、连接、接管、未读、unknown、报价有效期及操作证据继续保存；移除提醒不把语义问题升级为硬性issues或自动接管。

## 2B. 需求增量与多事项（0.3.16）

自动及普通建议不再固定返回整份requirements/new_case。可选requirement_changes每轮最多32项，使用action=create/update、case_ref、trigger_evidence_ids及可选set_fields/clear_fields；省略或空数组保持原状，空值不能隐式清除。create须有可唯一定位的产品及买家原文，update只覆盖明确字段，clear须有撤回／更正依据。程序保存完整13键，product_url仍派生。模型不生成case_id、版本或幂等键。

context.case_catalog保存同会话的冻结别名、需求、版本及各自适用资料／供货；模型投影移除真实case_id、版本、派生链接及商品展示图。reply_case_refs可单独路由回复，不要求修改需求。来源与目录版本先整体复核，多项变更在一次事务中应用；requirement_application记录应用结果，case_origin.creation_spans绑定确切原文范围以防重复新建。首次真实采购可复用空case，相同商品的独立新订单不按商品ID误合并。

requirements_revision仅正式需求及有效依据变化时递增，缺失旧记录按有有效ai-v1需求为1、空壳为0读取。普通case.revision及会话版本保护继续执行；相关各事项及供货条件保存为draft.reply_case_ids/related_case_versions，预检及最终提交再次复核。手动查看事项不修改后台指针。正文更正使受影响依据失效，无关时间或附件元数据不清空文本需求。

## 2C. 原图及受限宿主能力（0.3.16）

各图片DTO可含image_ref，按消息／需求／产品／补充种类绑定真实归属、来源版本和SHA。getImageOriginal、showImageMenu及openProductLink为专用preload能力，main验证工作台主frame和生命周期；不接收任意文件路径、资源URL或菜单模板。

宿主私有_image_original_describe/_image_original_chunk读取本地原图，每块最多256 KiB原始字节，原图继续限制6 MiB、8192单边、2000万像素。复制专用_image_copy_describe/_image_copy_chunk用PIL生成校正EXIF方向、保留透明度的全尺寸PNG，最多80 MiB、两份会话和30秒有效期；逐块复核来源，最后一块释放会话。完整内容、块序号和SHA由宿主核对，不提高普通JSON行预算。这些方法均不进入普通API_METHODS。

主进程以Electron45异步ClipboardItem写入PNG，仅在明确选择固定菜单“复制图片”后执行。原图通过Uint8Array专用IPC返回，renderer以Blob URL显示并按生命周期释放；CSP仅img-src增加blob:。_product_link_for_reference根据当前商家真实case／产品取规范数字ID链接，main再次核对固定HTTPS格式后用系统浏览器打开，页面内导航和新窗口拦截继续执行。

## 2A. 业务补充附件（0.3.14）

Supplement新增attachments，按当前商家和conversation_id保存，字段含id、name、media_type、size、sha256、status、origin=business_supplement、created_at，可含pages及错误；界面只返回受限preview_data_url/text_preview，模型只得到元数据和实际提取的text，原件及预览不进入模型JSON。

renderer通过chooseSupplementAttachments(conversationId)多选、uploadSupplementAttachments(conversationId,files)上传用户提供的{name,content_b64}，或pasteSupplementAttachments(conversationId)显式请求原生粘贴，返回{supplement,errors}批量结果，保留部分成功。路径只来自宿主文件选择或当前用户粘贴操作；Windows本地文件读取使用固定只读剪贴板命令，截图读取使用Electron异步剪贴板接口，具有超时、输出和字节限制，不接受renderer路径。旧chooseSupplementAttachment保留兼容。宿主逐项调用私有upload_supplement_attachment，该方法不在普通API_METHODS中。公开移除仍只接受会话及附件编号。

支持PNG/JPEG/WebP、PDF、UTF-8 TXT/MD/CSV；单个6 MiB、最多5个、总计20 MiB。PDF最多10页、每附件提取16000字符，拒绝加密和无可提取文字的PDF；pypdf禁用外部jbig2dec并限制解压、解析工作量。专属展示缩略图最大256×256且JPEG不超过48 KiB，原始图片字节及哈希保持不变。上传JSON Lines仅为该私有方法放宽至8,400,000字符，其他方法沿用原预算。

正文CAS保持，附件操作在最新补充记录上更新版本，不覆盖期间新正文；变更进入补充指纹并使旧未提交结果失效。图片以origin和attachment_id独立于聊天消息，0.4.0通过所选接待API与文字同轮附图，确切输入证据映射为supplement_image_reads并复核版本、归属及原图哈希；预览不代表已读，不以附件编号伪装聊天消息。当前明确补充图片可用于润色理解，但不补读历史聊天图或程序商品主图。文本和图片都是商家来源，不作为买家确认，也不恢复旧质量/协助提醒。

## 3. 产品、供货方案和任务

product_library.py使用现有Repository JSON实体：library_product、supplier、supplier_offer、requirement_product_binding、procurement_task及library_mutation。仍为Repository schema v2、Ledger v1。

新AI产品ID按商家+平台+ID稳定键建立一份产品和首次任务，并关联需求。事务内只持久任务，由调度器后续入队；队列满不能回滚已接受的AI需求。需求私有条件不进入公共产品；客户私图不复制为跨客户公共图片。产品可有多个供货方案，供应商可供多个产品；缺可靠平台供应商ID时不凭公司名推断同一平台身份。

| RPC | 参数及结果 |
| --- | --- |
| list_products / get_product | 列表items；详情product、offers、suppliers、tasks；详情参数local_product_id |
| list_suppliers / get_supplier | 列表items；详情supplier、offers、products；详情参数supplier_id |
| save_supplier | 名称、平台、可空平台ID/店铺链接、contact、notes、client_mutation_id；编辑另需supplier_id、expected_revision |
| save_product_offer | local_product_id、supplier_id、data、client_mutation_id；编辑另需offer_id、expected_revision |
| bind_requirement_product | case_id、local_product_id、expected_case_revision、client_mutation_id |
| select_supplier_offer | case_id、可空offer_id、expected_case_revision、client_mutation_id |
| set_product_default_offer | local_product_id、可空offer_id、expected_revision、client_mutation_id |
| get_procurement_settings | auto_find、auto_inquiry、supplier_limit、authorized、inquiry_available、inquiry_reason |
| save_procurement_settings | auto_find、auto_inquiry、supplier_limit；保存后采购grant撤销 |
| set_procurement_enabled | enabled；只授权当前进程，重启关闭 |
| list_procurement_tasks | 可选local_product_id，返回items |
| request_procurement | local_product_id、client_request_id，返回task_id；已执行/未知任务不重新搜索 |
| reconcile_procurement | task_id，返回job_id；仅查询原请求 |

供货方案data限定supplier_product_id、product_url、specifications、moq、unit、unit_price、currency、lead_time、packaging、freight、tax_included、valid_until、source_type、source_note、customer_wording、enabled、reply_enabled。价格保存十进制字符串；每次修订保留历史。source_type为page_reference、supplier_reply或manual_verified，页面参考不能直接启用为已核实回复承诺。

选择只影响指定需求，DTO投影local_product_id和selected_offer_id。默认只给新绑定继承；修改默认不修改已有选择。选中方案的selection_revision、offer_id、offer_revision、product_revision进入自动与人工上下文指纹；修改使旧未提交结果失效。禁用、过期、MOQ不适用或尚不明确的方案usable=false，不能作为回复依据。内部进价不是销售价；仅明确customer_wording作为对客口径，禁止泄露内部成本或自行按利润/汇率算售价。

采购任务状态为waiting_authorization、waiting_input、queued、running、waiting_reply、succeeded、failed、unknown、blocked。初始供应商上限3，可配置1至3。source_product仅内部AI job，不开放任意执行RPC。产品任务conversation_id为空，不因无关客户编辑取消；采购暂停和关闭可以取消，接待grant不能代替采购grant。

integrations/sourcing.py使用同一desktop query入口。会话名由原request_id决定，提交前持久化确切prompt、UTF-8 SHA256、agent、会话名及开始时间，实际调用前再核采购grant。调用失败或超时不能证明未执行；核查只恢复唯一匹配的原会话、确切请求、工具调用/结果和终态答案，不新发搜索，不读取无关会话正文。候选只取真实搜索结果并复核模型选择；原商品链接由程序生成，采购AI不再输出。主图准备在媒体池中进行，等待时不占接待AI槽；有图可作为采购专用reference_image，必须与冻结URL一致。当前只支持已观察到的返回格式。事后工具记录检查不构成提交前工具沙箱，不传绕过宿主审批的参数。

定向询价与回复读取已有只读schema报告，但本卖家权限、回复域、未知提交恢复未实测，且工具无远端幂等键。当前inquiry_available=false，相关阶段blocked，未实现自动发送或真实报价回收；不以页面价格冒充供应商回复。RFQ发布、订单、付款和自动谈判不属于默认采购范围。

## 4. 人工聊天、接待和发送保护

save_chat_draft/send_chat按会话运行，不以case、AI检查、采购齐全或最后买家发言为前置。首次手写、采用建议及人工发送接管当前客户；建议查看、生成及补充保存不接管。人工正文、AI draft、manual_suggestion和冻结的发送版本独立，迟到结果不得覆盖新输入或串客户。

补充CAS保存，默认进入自动、建议及润色，每会话只保留最新正文及revision/hash。续答由有效买家来源、最新本机accepted卖家消息及最新补充组成，不再依赖业务提醒实体。稳定内部问题/来源标识、supplement_followup、continuity_plan和原操作关联继续承担幂等与重试，不当作另一套业务提醒；有确切草稿accepted回执才表示已答复。

自动接待要求当前身份、读取/AI检查、范围、人工接管、来源及资料/需求版本、退出与最终授权复核。selected/all冻结相应范围，incoming受授权起点约束。不限AI/发送总次数，保留统计；保存接待范围或业务设置撤权，重启不恢复，普通人工编辑只影响当前客户。

正常自动回复及续答保留本地业务检查后直接聊天提交，不做发送前Ready、Seal或整批平台历史重读。prepared/sending/unknown保护整会话，accepted需要确切平台消息编号和提交证据，未知只核查原operation，不换事项/键或凭同文猜测重发。单条自动失败链最多3次总尝试，仅明确未提交的暂时错误按5秒/15秒退避，新job、草稿及重启不重置。旧显式预演/原生确认仅兼容，不作为普通发送前置；旧未知回执保留原通道核查。

公开业务方法以API_METHODS和Python FAST/SLOW双重白名单为准。confirm_send必须走宿主专用原生确认，shutdown仅供宿主关闭。退役的旧协助、候选整理和商品解析查询方法不作为新界面接口。

## 4A. 自动自然分条、单条人工正文与回执（0.3.21）

国际站与WhatsApp自动接待使用reply_messages:[{body,reply_sources}]，按真人即时聊天节奏生成1至3条，正文合计最多1200字符；不以压成一条为目标，不固定条数、顺序模板或每条一句。国际站skip允许空数组并仍可保存需求增量。reply及整轮来源并集由程序派生用于展示；自动执行只用冻结的有序列表。普通人工建议严格只含一个条目，旧多项建议不兼容、不拼接转换。POLISH保持单个reply/reply_sources，冻结整份正文。一次生成只应用一次需求与摘要，形式校验不代替真实模型语义验收。

新人工chat_draft仅保存单份body，正文预算50000字符，不提供message_parts/part_index/remaining_batch_id的日常编辑与润色参数。采用建议只替换单份人工正文。旧草稿保留其已有body，旧进行中/unknown继续原operation核查和整会话围栏；部分受理的旧人工批次只有确切停止且无未决操作后才能归档并保存独立新正文，不重发受理前缀，不清原账本。

reply_batch及独立wa_reply_batch冻结稳定part_id、顺序、正文hash、来源和授权归属，当前条目独立operation；前一条确切accepted后，后续才进原队列。国际站首条保留原来源逻辑键，后续核对冻结清单及全部前序确切受理证据；ordinary/final/supplement沿原通道及重试规则，按part_id限次，旧账本accepted/unknown不能换批次绕过。活动批次在条目间、重试和核查期间保持同会话互斥。新消息、接管、暂停或依据变化停止未提交尾部，重启不续发。平台先同步自身回显时，仅按确切受理ID、正文、角色与目标，并核实其余依据未变后继续；不能按同文泛化放行。

ConversationDetail.reply_batches投影批次状态、条目状态、来源类型、时间、原因及待核查操作；当前自动进度在聊天区域展示，完成约3秒收起，历史折叠，不进入人工编辑区。WhatsApp保持宿主单次sendMessage及单份单条JSONL账本，确切ID且ACK≥1才受理，Python管理整轮进度；历史及联系人metadata不触发回复，绑定国际站仅作只读背景。SDK阶段故障退役本代程序接待，可信原始页面保留；即时宿主状态独立于慢业务查询。诊断按固定阶段/错误码和受限相对堆栈保存，最多2 MiB×3份，不包含原始异常、聊天、账号、二维码或凭据。私有_wa_*入口不加入renderer API。

## 5. 图片、资料、翻译

真实消息协议、受控下载、安全解码、原图与预览缓存继续保留。图片归属和消息版本必须核实；需求product_images使用get_requirement_media的确切media_id，只显示匹配的本地安全预览。AI只有本轮实际读取才可标记已看过；预览不等于读取，商家图不作为买家确认。普通润色不准备历史图片。默认整批64 MiB、准备最多60秒、下载并行2项，单图6 MiB；相关原图由当前接待API同轮附加，p1/p2与实际字节、角色、来源版本绑定。请求编码还受保守内存预留限制，64 MiB不是任意图片批次均能成功的承诺。模型编码与重像素处理共用512 MiB工作量预算，重像素单并发；它不包含解释器、SQLite及SDK隐藏分配，不是RSS硬上限。真实SKU全链和图搜尚未验收。

政策按用途、启用、确认、有效期和产品范围选择。六份内置规则、三份模板默认关闭，采用后归当前商家；仅人工备注不进模型，公开口径与内部规则分开。适用风格选优先级最高的一份，同分按产品范围及稳定编号。公司名独立慢任务，快照不联网。

第三方翻译只处理可见聊天文字和有效人工建议，独立HTTP队列；持久缓存按商家、会话、原文/前文hash、端点、模型和提示词隔离。需求直接为AI中文正文，不走独立翻译。共享连接/Key由Windows CurrentUser DPAPI保存到真实KnownFolder，同机同Windows用户共享；启用仍绑定商家数据根，更换端点/恢复新根须确认。凭据不入SQLite、argv、环境、日志或业务备份。公网HTTPS和明确本机回环网关可用，禁远程HTTP、歧义IP、非回环私网和重定向。

## 6. 存储、关闭和升级

使用WAL与FULL同步，消息、需求、人工正文、草稿、job、operation、outbox分别保存。完整恢复集合包括控制库、卖家业务库、原生账本、回执、图片与有效WAL，不把回执当缓存。实际根以profile及设置页为准，顺序为本次参数、已保存选择、默认根；源代码复制不迁移业务数据。

maintenance prepare在服务实例化前运行。0.3.13内容清理只对正常关闭且checkpoint已核实的工作区执行：验证完整安全备份，重新独占并逐一核对源文件，再按reminder_cleanup.py白名单清理提醒专用entities/revisions和嵌入字段，checkpoint及完整性核对后记录正常关闭。备份失败、占用或源变化均拒绝删除；旧备份原样保留，不模糊删除job/draft/operation/outbox、消息、图片、回执或续答重试标识。无旧字段时不重复备份/清理。

正常关闭先保存编辑、撤权、取消并等待自有后台/原生子进程落库，再关闭数据库与释放锁；未完成不伪造clean。目录切换先完整备份和历史比较，恢复到空目标，不清unknown、不恢复授权。在线更新、存储、授权启动指引走专用宿主IPC，不混入业务RPC；固定更新公钥及发行路径约定见[PROJECT](PROJECT.md)与[在线升级](在线升级.md)。

所有验证使用新的隔离标签及临时Demo/替身；小版本仅定向测试，实际窗口需交互并查看截图。开发、候选打包、公开发布、实际安装、运行入口晋级与真实数据迁移各自核验。Demo、合成AI和只读schema不能替代真实平台发送或供应商报价验收。
