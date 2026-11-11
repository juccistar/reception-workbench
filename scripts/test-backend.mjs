import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { root, pythonPath } from './paths.mjs';
const tests=path.join(root,'services/reception/tests');
const env={...process.env,PYTHONPATH:path.join(root,'services/reception/src'),PYTHONIOENCODING:'utf-8'};
const result=spawnSync(pythonPath(),['-B','-m','unittest','discover','-s',tests,'-v'],{cwd:root,env,stdio:'inherit'});
process.exitCode=result.status??2;
