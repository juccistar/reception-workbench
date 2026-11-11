import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { AUTO_LAUNCH_CONTEXT_ENV, consumeAutoLaunchContext } from '../merchant-profiles.mjs';

function fixture() {
  const root = mkdtempSync(path.join(os.tmpdir(), 'merchant-context-'));
  const dataRoot = path.join(root, 'data'), stateDirectory = path.join(root, 'state');
  mkdirSync(dataRoot); mkdirSync(stateDirectory);
  const value = {sellerId:'12345',dataRoot,stateDirectory};
  const env = {[AUTO_LAUNCH_CONTEXT_ENV]:JSON.stringify(value), RECEPTION_BOOTSTRAP_DIR:stateDirectory};
  const args = ['--connect-from-accio','--expect-seller','12345','--data-dir',dataRoot];
  return {root,value,env,args,cleanup:()=>rmSync(root,{recursive:true,force:true})};
}
test('consumes routing context exactly once',()=>{const f=fixture();try{assert.deepEqual(consumeAutoLaunchContext(f.env,f.args),f.value);assert.equal(consumeAutoLaunchContext(f.env,f.args),null);}finally{f.cleanup();}});
test('rejects mismatched seller, data, state, duplicates and extra keys and deletes context on failure',()=>{
  for (const alter of [f=>f.args[2]='999', f=>f.args[4]=f.root, f=>f.env.RECEPTION_BOOTSTRAP_DIR=f.root, f=>f.args.push('--expect-seller','12345'), f=>f.env[AUTO_LAUNCH_CONTEXT_ENV]=JSON.stringify({...f.value,token:'no'})]) {
    const f=fixture();try{alter(f);assert.throws(()=>consumeAutoLaunchContext(f.env,f.args),/auto_launch_context_invalid/);assert.equal(f.env[AUTO_LAUNCH_CONTEXT_ENV],undefined);}finally{f.cleanup();}
  }
});
test('rejects malformed and oversized context',()=>{for(const raw of ['{','x'.repeat(100001),JSON.stringify({sellerId:'01'})]){const f=fixture();try{f.env[AUTO_LAUNCH_CONTEXT_ENV]=raw;assert.throws(()=>consumeAutoLaunchContext(f.env,f.args),/auto_launch_context_invalid/);assert.equal(f.env[AUTO_LAUNCH_CONTEXT_ENV],undefined);}finally{f.cleanup();}}});
test('rejects linked directories',()=>{const f=fixture();try{const alias=path.join(f.root,'alias');symlinkSync(f.value.dataRoot,alias,process.platform==='win32'?'junction':'dir');f.value.dataRoot=alias;f.args[4]=alias;f.env[AUTO_LAUNCH_CONTEXT_ENV]=JSON.stringify(f.value);assert.throws(()=>consumeAutoLaunchContext(f.env,f.args),/auto_launch_context_invalid/);}finally{f.cleanup();}});
