import path from 'node:path';
import { mkdir, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { stageWhatsAppRuntime, verifyWhatsAppRuntime } from './whatsapp-runtime.mjs';
import { build as bundle } from 'esbuild';
import { build as viteBuild } from 'vite';
import { root } from './paths.mjs';
await mkdir(path.join(root,'artifacts/app'),{recursive:true});
for (const entry of ['main','preload','launcher-preload']) await bundle({entryPoints:[path.join(root,`apps/desktop/src/${entry}/index.ts`)],outfile:path.join(root,`artifacts/app/${entry}/index.js`),bundle:true,platform:'node',format:'cjs',target:'node22',external:['electron','original-fs','whatsapp-web.js','puppeteer'],banner:entry==='main'?{js:'const __hostRequire = require; const __waRequire = __hostRequire("node:module").createRequire(__hostRequire("electron").app.isPackaged ? __hostRequire("node:path").join(process.resourcesPath, "whatsapp-runtime.asar", "entry.cjs") : __filename); require = Object.assign(function(id) { return id === "puppeteer" || id === "whatsapp-web.js" ? __waRequire(id) : __hostRequire(id); }, __hostRequire);'}:undefined,sourcemap:false});
const whatsappRuntime = path.join(root, 'artifacts/app/main');
if (existsSync(path.join(whatsappRuntime, 'node_modules'))) {
  await verifyWhatsAppRuntime(whatsappRuntime);
  await rm(path.join(whatsappRuntime, 'node_modules'), { recursive: true });
}
await stageWhatsAppRuntime(whatsappRuntime);
await verifyWhatsAppRuntime(whatsappRuntime);
await viteBuild({configFile:path.join(root,'apps/desktop/vite.config.ts')});
console.log('Desktop UI and host built in artifacts/app.');
