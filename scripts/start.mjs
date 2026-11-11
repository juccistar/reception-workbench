import { spawnSync, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { root, cleanElectronEnv } from './paths.mjs';
const require=createRequire(import.meta.url);
if (!existsSync(path.join(root,'artifacts/app/main/index.js'))) {
 const build=spawnSync(process.execPath,[path.join(root,'scripts/build.mjs')],{cwd:root,stdio:'inherit'});
 if(build.status!==0) process.exit(build.status??2);
}
const electron=require('electron');
const child=spawn(electron,[path.join(root,'artifacts/app/main/index.js'),...process.argv.slice(2)],{cwd:root,env:cleanElectronEnv(),stdio:'inherit'});
child.on('error',()=>{console.error('Could not start Electron. Check local installation.');process.exitCode=2;});
child.on('exit',code=>{process.exitCode=code??2;});
