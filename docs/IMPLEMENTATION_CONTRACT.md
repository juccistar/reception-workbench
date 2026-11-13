# 工作台实施契约

本文维护0.3.13源码及公开发行的进程、接口和业务约束；交付范围见[发布说明](0.3.13发布说明.md)。开发验证及限制见[本版开发说明](0.3.13开发说明.md)、[验证记录](verification/0.3.13.json)。精确字段同时核对[TypeScript契约](../apps/desktop/src/shared/api.ts)、[服务分派](../services/reception/src/reception_service/service.py)及对应测试。历史版本字段不能覆盖当前实现。

## 1. 进程与协议

Electron main管理窗口、受限IPC、原生确认、后台及离线存储维护；preload只暴露冻结window.reception。renderer没有Node、SQLite、shell、任意文件、令牌或通用URL打开接口。Python 3.12服务经自有stdin/stdout UTF-8 JSON Lines通信，不开本地HTTP业务端口，不在运行时导入旧Qt工程。

```text
请求：{id, method, params}
成功：{id, result}
失败：{id, error:{code, message, stage?, diagnostics?}}
事件：{event, data}
```

stdout仅输出协议。原生输出、完整prompt、凭据及环境不进入普通错误和事件。公开DTO使用snake_case、毫秒时间及null缺值。慢操作返回job_id，由job.updated更新；state.changed要求重读。排队、任务完成、本地保存、平台受理和飞书登记是不同状态。

每商家有一个可取消Accio AI执行槽，业务协调和消息监测独立；队列优先人工发送、回执核查及已就绪自动发送，兼顾公平。采购AI共用该槽，本轮已生成回复不等待采购结果，后续生成仍受队列与外部耗时影响，不承诺固定响应时限。保存、暂停及本地查询不排在长AI等待后。取消本地任务不保证远端物理调用或计费同时停止。

Demo/Live数据与适配器隔离，Live身份核实后才打开对应卖家库；单个服务生命周期不跨卖家切换，同商家跨目录排他。真实读取、AI、发送和外部写入分别服从当前明确授权。

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

新增Case/Product.product_main_image独立展示元数据：local_product_id、status、updated_at、message、width/height（可选），并标display_only，可保留primary_verified、selection_rule。get_product_main_image(local_product_id)仅本地返回安全data_url，列表/多事项投影不携带大图或原始source_url。后台调用icbu.product.list-information，JSON参数为productId（整数）、queryType=trunk、componentList=["images"]。仅解析已观察到的data（JSON字符串）→basicInfo.images[]，按最小imageIndex选originalImageUrl作为展示参考。响应不含商品ID，因此按已核身份及确切请求参数绑定，不声称响应回验；若显式返回异ID则拒绝。没有主图标志，primary_verified=false、selection_rule=lowest_image_index，页面注明“平台图组首图，仅供展示和找货参考”；不称SKU图。安全缓存放product_main_image及image_asset，不创建message_image关联，不替代或合并原客户product_images。

链接和主图不进入接待模型指定字段、photo_context/image_manifest或语义指纹；补图不增事项业务版本、不使旧回复过期。它们可以进入采购专属上下文，图搜参数只接受冻结的确切公共AliCDN主图URL。原消息或冻结润色正文中的链接原样保留。来源正文修改仍使实际需求失效，并解除旧供货关联；产品和原执行证据保全。

ConversationDetail.conversation_summary为{conversation_id,body,revision,updated_at}或null，独立商家及会话归属；ConversationDetail.case可为null。同会话多需求共用摘要，切换需求不换摘要，无需求仍支持聊天、补充和摘要。正常接待、人工建议经来源指纹复核后同轮保存中文摘要和有效需求；润色只处理冻结正文，不写入两者，不启动采购。

删除quality_notes、quality_warnings、语义validation.warnings、assistance_requests/suggestions、旧待核实/待协助及business_progress提醒生成与投影。摘要自然说明业务上仍需核实的内容，不强制每段添加提醒。真实任务错误、连接、接管、未读、unknown、报价有效期及操作证据继续保存；移除提醒不把语义问题升级为硬性issues或自动接管。

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

## 5. 图片、资料、翻译

真实消息协议、受控下载、安全解码、原图与预览缓存继续保留。图片归属和消息版本必须核实；需求product_images使用get_requirement_media的确切media_id，只显示匹配的本地安全预览。AI只有本轮实际读取才可标记已看过；预览不等于读取，商家图不作为买家确认。普通润色不准备历史图片。默认整批64 MiB、准备60秒、并行2项，单图6 MiB；workctl为同任务read_compat，无原生图片附件参数，p1/p2仅引用。真实SKU全链和图搜尚未验收。

政策按用途、启用、确认、有效期和产品范围选择。六份内置规则、三份模板默认关闭，采用后归当前商家；仅人工备注不进模型，公开口径与内部规则分开。适用风格选优先级最高的一份，同分按产品范围及稳定编号。公司名独立慢任务，快照不联网。

第三方翻译只处理可见聊天文字和有效人工建议，独立HTTP队列；持久缓存按商家、会话、原文/前文hash、端点、模型和提示词隔离。需求直接为AI中文正文，不走独立翻译。共享连接/Key由Windows CurrentUser DPAPI保存到真实KnownFolder，同机同Windows用户共享；启用仍绑定商家数据根，更换端点/恢复新根须确认。凭据不入SQLite、argv、环境、日志或业务备份。公网HTTPS和明确本机回环网关可用，禁远程HTTP、歧义IP、非回环私网和重定向。

## 6. 存储、关闭和升级

使用WAL与FULL同步，消息、需求、人工正文、草稿、job、operation、outbox分别保存。完整恢复集合包括控制库、卖家业务库、原生账本、回执、图片与有效WAL，不把回执当缓存。实际根以profile及设置页为准，顺序为本次参数、已保存选择、默认根；源代码复制不迁移业务数据。

maintenance prepare在服务实例化前运行。0.3.13内容清理只对正常关闭且checkpoint已核实的工作区执行：验证完整安全备份，重新独占并逐一核对源文件，再按reminder_cleanup.py白名单清理提醒专用entities/revisions和嵌入字段，checkpoint及完整性核对后记录正常关闭。备份失败、占用或源变化均拒绝删除；旧备份原样保留，不模糊删除job/draft/operation/outbox、消息、图片、回执或续答重试标识。无旧字段时不重复备份/清理。

正常关闭先保存编辑、撤权、取消并等待自有后台/原生子进程落库，再关闭数据库与释放锁；未完成不伪造clean。目录切换先完整备份和历史比较，恢复到空目标，不清unknown、不恢复授权。在线更新、存储、授权启动指引走专用宿主IPC，不混入业务RPC；固定更新公钥及发行路径约定见[PROJECT](PROJECT.md)与[在线升级](在线升级.md)。

所有验证使用新的隔离标签及临时Demo/替身；小版本仅定向测试，实际窗口需交互并查看截图。开发、候选打包、公开发布、实际安装、运行入口晋级与真实数据迁移各自核验。Demo、合成AI和只读schema不能替代真实平台发送或供应商报价验收。
