const stages: Record<string, string> = { identity: '身份核验', native: '平台读取', source: '来源校验', network: '网络读取', http: '下载响应', parse: '资料解析', validation: '内容校验', decode: '图片解码', read: '读取', unknown: '尚未分类' };
const categories: Record<string, string> = { size_limit: '内容超过大小限制', unsupported_type: '内容类型不支持', type_mismatch: '声明类型与实际格式不符', dimensions: '图片尺寸不支持', animation: '暂不支持动态图片', preview: '预览生成失败', invalid_image: '图片内容无法解码', timeout: '读取超时', resolution: '域名解析失败', redirect: '跳转无法核实', rejected: '下载被拒绝', download: '下载失败', invalid_url: '资料地址不符合要求', host_not_allowed: '资料主机尚不支持', unsafe_address: '资料地址无法核实', retrieval: '读取未完成', status: '资料服务未返回成功响应', content_encoding: '响应编码不支持', invalid_json: '资料不是有效 JSON', invalid_document: '资料结构无效', invalid_seller: '商家身份无效', verification: '身份核验未通过', read_failed: '平台读取未成功', business_failure: '平台未确认业务成功', invalid_response: '平台返回结构不支持', invalid_name: '公司名称字段无效', association: '图片无法关联到确切消息', unknown: '尚未取得具体类别' };

export function ReadDiagnostics({ diagnostics }: { diagnostics?: Record<string, unknown> }) {
  const stage = typeof diagnostics?.read_stage === 'string' ? diagnostics.read_stage : '';
  const category = typeof diagnostics?.read_category === 'string' ? diagnostics.read_category : '';
  const visualStages: Record<string, string> = { preflight: '图片输入预检', workspace: '本轮图片工作区', process: '图片查询执行', read_trace: '本轮原图读取回执', image_changed: '原图一致性' };
  const visualStatuses: Record<string, string> = { invalid: '输入无法核实', unavailable: '工作区尚未就绪', not_found: '未找到本轮确切回执', rejected: '回执未通过核验', budget_exhausted: '读取资源预算已用尽', process_error: '查询执行未完成', cancelled: '请求已取消', completed: '已取得完整读取证据' };
  const visualStage = typeof diagnostics?.visual_stage === 'string' ? diagnostics.visual_stage : '';
  const visualStatus = typeof diagnostics?.visual_status === 'string' ? diagnostics.visual_status : '';
  const hasRead = stage in stages && category in categories;
  const hasVisual = visualStage in visualStages && visualStatus in visualStatuses;
  if (!hasRead && !hasVisual) return null;
  return <details className="check-details"><summary>读取详情</summary>{hasRead && <><p>阶段：{stages[stage]}</p><p>原因：{categories[category]}</p></>}{hasVisual && <><p>图片阶段：{visualStages[visualStage]}</p><p>结果：{visualStatuses[visualStatus]}</p></>}</details>;
}
