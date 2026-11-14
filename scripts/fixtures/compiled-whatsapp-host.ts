import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import Module from 'node:module';
import path from 'node:path';
import type { WhatsAppHost } from '../../apps/desktop/src/main/whatsapp/host';
import type { ViewTransport } from '../../apps/desktop/src/main/whatsapp/transport';

/** Only the isolated fixture replaces startup; production archives stay unchanged. */
export function compiledWhatsAppHost(filename: string) {
  const bytes = readFileSync(filename);
  let text = bytes.toString('utf8');
  const startup = /\nvoid start\(\)\.catch\(\(\) => [A-Za-z0-9_$]+\.app\.exit\(2\)\);\s*$/;
  if (!startup.test(text) || !text.includes('var WhatsAppHost = class') || !text.includes('var ViewTransport = class')) throw new Error('HOST_ENTRY_UNSUPPORTED');
  text = text.replace(startup, '\nmodule.exports = { WhatsAppHost, ViewTransport };\n');
  const route = ': __filename); require =';
  if (!text.includes(route)) throw new Error('HOST_RUNTIME_ROUTE_UNSUPPORTED');
  text = text.replace(route, ': process.env.RECEPTION_WA_RUNTIME_ENTRY); require =');
  const loaded = new (Module as any)(filename, module);
  loaded.filename = filename;
  loaded.paths = (Module as any)._nodeModulePaths(path.dirname(filename));
  loaded._compile(text, filename);
  return { ...(loaded.exports as { WhatsAppHost: typeof WhatsAppHost; ViewTransport: typeof ViewTransport }), host_sha256: createHash('sha256').update(bytes).digest('hex') };
}
