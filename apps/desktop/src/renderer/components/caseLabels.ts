import type { Case } from '../../shared/api';

/** Display metadata is independent of the product fact and never changes it. */
export function categoryLabel(item: Case): string {
  const label = item.category_label?.trim() ?? '';
  return label && Array.from(label).length <= 8 ? label : '待整理需求';
}

export function caseLabel(item: Case, cases: Case[]): string {
  const label = categoryLabel(item);
  const peers = cases.filter(candidate => categoryLabel(candidate) === label);
  return peers.length > 1 ? `${label} · 事项${peers.findIndex(candidate => candidate.case_id === item.case_id) + 1}` : label;
}
