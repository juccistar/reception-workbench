export function companyName(value: unknown, sellerId: string): string | null {
  if (typeof value !== 'string' || [...value].length > 400 || /[\x00-\x1f\x7f-\x9f]/.test(value)) return null;
  const name = value.trim();
  if (!name || name === `商家 ${sellerId.slice(-4)}` || name === `商家${sellerId.slice(-4)}`) return null;
  return name;
}

export function launcherCompanyName(name: string, sellerId: string, verified: boolean): string {
  return verified && companyName(name, sellerId) || (verified ? '公司名称待读取' : '公司名称待授权');
}
