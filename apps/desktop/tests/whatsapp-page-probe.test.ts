import { afterEach, describe, expect, it } from 'vitest';
import { whatsappPageProbe } from '../src/main/whatsapp/connection';

afterEach(() => { document.body.innerHTML = ''; delete (window as any).Debug; });
describe('WhatsApp login page storage error probe', () => {
  it.each(['Database error. Please log out and try again.', '数据库发生错误，请重试。'])('finds the database banner without a QR or landing test id: %s', text => {
    document.body.innerHTML = '<div id="app"><section></section></div>';
    document.querySelector('section')!.textContent = text;
    expect(whatsappPageProbe()).toMatchObject({ databaseError: true, available: false });
  });
  it('gives the database banner precedence over a visible QR placeholder', () => {
    document.body.innerHTML = '<div id="app"><p>Database error</p><canvas data-ref="never-return-this-value"></canvas></div>';
    document.querySelector('canvas')!.getBoundingClientRect = () => ({ width: 100, height: 100 } as DOMRect);
    const result = whatsappPageProbe();
    expect(result).toMatchObject({ databaseError: true, available: false });
    expect(JSON.stringify(result)).not.toContain('never-return-this-value');
  });
  it('does not classify customer text inside an authenticated chat as a login error', () => {
    document.body.innerHTML = '<div id="app"><aside id="pane-side">Database error in customer project</aside></div>';
    document.querySelector('aside')!.getBoundingClientRect = () => ({ width: 100, height: 100 } as DOMRect);
    (window as any).Debug = { VERSION: '2.3000.1054634694' };
    expect(whatsappPageProbe()).toEqual({ databaseError: false, available: true, webVersion: '2.3000.1054634694' });
  });
});
