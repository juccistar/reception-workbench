const outlines: Record<string, string> = {
  home: 'm3 10 9-7 9 7v11h-7v-7h-4v7H3V10Z',
  reception: 'M20 11.5a8 8 0 0 1-8.5 8H4l1.3-4.1A8 8 0 1 1 20 11.5Z M8 10h8M8 14h5',
  products: 'm12 3 9 5-9 5-9-5 9-5ZM3 8v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5',
  suppliers: 'M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v3',
  customers: 'M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v3',
  procurement: 'm16 16 4.5 4.5',
  handoff: 'M5 3h14v18H5V3Zm3 5h8m-8 4h8m-8 4h5',
  policies: 'M12 5C9 3 6 3 3 4v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15',
  activity: 'M12 6v6l4 2',
  settings: 'm9 3-.6 2.6-2 1.2-2.6-.7-2 3.5L3.7 12l-1.9 2.4 2 3.5 2.6-.7 2 1.2L9 21h4l.6-2.6 2-1.2 2.6.7 2-3.5-1.9-2.4 1.9-2.4-2-3.5-2.6.7-2-1.2L13 3Z',
};

export function NavigationGlyph({ page }: { page: string }) {
  return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {outlines[page] && <path d={outlines[page]} />}
    {(page === 'suppliers' || page === 'customers') && <circle cx="9" cy="8" r="3" />}
    {page === 'procurement' && <circle cx="10.5" cy="10.5" r="6.5" />}
    {page === 'activity' && <circle cx="12" cy="12" r="9" />}
    {page === 'settings' && <circle cx="11" cy="12" r="3" />}
    {page === 'launcher' && <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>}
  </svg>;
}
