const roles: Record<string, string> = { buyer: '买家', seller: '卖家', unknown: '未知', missing: '未提供', invalid: '格式无效' };
const differences: Record<string, string> = { role: '发言角色', role_conflict: '两个角色字段互相冲突', content: '消息正文', role_and_content: '发言角色和消息正文', sender_identity: '发送者身份', time: '消息时间', time_and_content: '消息时间和正文' };
const contentTypes: Record<string, string> = { string: '文字', null: '空值', object: '结构化对象', array: '结构化数组', other: '其他类型' };

// Deliberately render only known, bounded metadata; never stringify diagnostics.
export function SnapshotDiagnostics({ diagnostics }: { diagnostics?: Record<string, unknown> }) {
  if (!diagnostics) return null;
  const difference = typeof diagnostics.snapshot_difference === 'string' && Object.hasOwn(differences, diagnostics.snapshot_difference) ? differences[diagnostics.snapshot_difference] : null;
  if (!difference) return null;
  const items: [string, string][] = [['不一致字段', difference]];
  const id = diagnostics.snapshot_message_id;
  if (typeof id === 'string' && /^[1-9][0-9]{0,31}(?:\.PNM)?$/.test(id)) items.push(['消息编号', id]);
  else if (id === 'redacted') items.push(['消息编号', '编号格式未识别，已隐藏']);
  for (const [key, label] of [['snapshot_from', '快照 from 角色'], ['snapshot_sender_role', '快照 senderRole 角色']] as const) {
    const value = diagnostics[key];
    if (typeof value === 'string' && Object.hasOwn(roles, value)) items.push([label, roles[value]]);
  }
  const type = diagnostics.snapshot_content_type;
  if (typeof type === 'string' && Object.hasOwn(contentTypes, type)) items.push(['快照正文类型', contentTypes[type]]);
  for (const [key, label] of [['snapshot_content_length', '快照正文长度'], ['snapshot_platform_content_length', '平台正文长度']] as const) {
    const value = diagnostics[key];
    if (typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 1000000) items.push([label, String(value) + ' 字符']);
  }
  for (const [key, label] of [['snapshot_role_equal', '角色核对'], ['snapshot_content_equal', '正文核对']] as const) {
    if (typeof diagnostics[key] === 'boolean') items.push([label, diagnostics[key] ? '一致' : '不一致']);
  }
  return <section aria-label="快照核验差异"><h3>快照核验差异</h3><dl>{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl><p>这里只显示核验字段，不显示消息正文或授权信息。旧失败记录可能没有这些详情。</p></section>;
}
