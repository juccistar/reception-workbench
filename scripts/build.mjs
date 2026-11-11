import path from 'node:path';
import { mkdir } from 'node:fs/promises';
import { build as bundle } from 'esbuild';
import { build as viteBuild } from 'vite';
import { root } from './paths.mjs';
await mkdir(path.join(root,'artifacts/app'),{recursive:true});
for (const entry of ['main','preload']) await bundle({entryPoints:[path.join(root,`apps/desktop/src/${entry}/index.ts`)],outfile:path.join(root,`artifacts/app/${entry}/index.js`),bundle:true,platform:'node',format:'cjs',target:'node22',external:['electron','original-fs'],sourcemap:false});
await viteBuild({configFile:path.join(root,'apps/desktop/vite.config.ts')});
console.log('Desktop UI and host built in artifacts/app.');
