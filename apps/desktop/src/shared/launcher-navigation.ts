export type LauncherPage = 'home' | 'models';

/** Accept only the two application routes; never pass URLs or paths through. */
export function launcherPageFromArguments(arguments_: readonly string[]): LauncherPage {
  return arguments_.includes('--launcher-page=models') ? 'models' : 'home';
}

export function launcherPageRequest(value: unknown): LauncherPage | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const page = (value as { launcher_page?: unknown }).launcher_page;
  return page === 'home' || page === 'models' ? page : null;
}
