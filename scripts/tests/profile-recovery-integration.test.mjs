import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { launchAuthorized, projectRoot } from '../connect-live.mjs';
import { merchantRegistryDirectory, merchantSupportPaths, resolveMerchantProfile, workspacePathUnits } from '../merchant-profiles.mjs';

const python = path.join(projectRoot, 'services/reception/.venv/Scripts/python.exe');
const entry = path.join(projectRoot, 'services/reception/run.py');
const source = path.join(projectRoot, 'services/reception/src');
const seller = '12345';
const fixtureCode = `
import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from reception_service.repository import Repository
from reception_service.integrations.accio_native.vendor.adapters.common import Ledger
from reception_service.profile_recovery import io_path
from reception_service.workspace_lock import WorkspaceLock
base,registry,seller=map(str,sys.argv[2:5])
root=Path(base)/('x'*145)
key=hashlib.sha256(seller.encode()).hexdigest()
state=Path(registry)/'profiles'/key/'state'
state.mkdir(parents=True)
Repository(root/'live/control.sqlite3').close()
Ledger(root/'live/adapters-live.sqlite3').initialize()
workspace=root/'live/sellers'/key/'workspace.sqlite3'
io_path(workspace.parent).mkdir(parents=True)
io_path(workspace).touch()
(root/'live/workctl-state/chat-analysis/send-receipts').mkdir(parents=True)
with WorkspaceLock(root) as lock: lock.mark_clean_shutdown()
record={'sellerId':seller,'stateDirectory':str(state),'initialDataRoot':str(root),'status':'created'}
(Path(registry)/'merchants.json').write_text(json.dumps({'format':1,'profiles':{seller:record}}),encoding='utf-8')
(state/'storage.json').write_text(json.dumps({'version':1,'dataRoot':str(root)}),encoding='utf-8')
(state/'profile.json').write_text(json.dumps({'format':1,'sellerId':seller,'dataRoot':str(root),'stateDirectory':str(state)}),encoding='utf-8')
print(json.dumps({'root':str(root),'state':str(state)}))
`;
const verifyCode = `
import json,sqlite3,sys
from pathlib import Path
root=Path(sys.argv[1]); live=root/'live'
with sqlite3.connect(live/'control.sqlite3') as db:
 seller=db.execute("SELECT json_extract(data,'$.seller_id') FROM entities WHERE kind='meta' AND id='seller'").fetchall()
 business=db.execute("SELECT count(*) FROM entities WHERE kind IS NULL OR kind<>'meta'").fetchone()[0]
with sqlite3.connect(live/'adapters-live.sqlite3') as db:
 ledger={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('operations','attempt_history','retry_grants')}
print(json.dumps({'seller':seller,'business':business,'ledger':ledger,'sellers_exists':(live/'sellers').exists()}))
`;
const failCode = `
import sys
sys.path.insert(0,sys.argv[1])
from reception_service.profile_recovery import _recover
_recover(*sys.argv[2:],fail_after=2)
`;
function run(args, env) { return execFileSync(python, args, { cwd: projectRoot, env, encoding: 'utf8', timeout: 60000, windowsHide: true, stdio: 'pipe' }); }
function setup() {
  const base = mkdtempSync(path.join(os.tmpdir(), 'rpi-'));
  const program = path.join(base, 'program'), local = path.join(base, 'local'), account = path.join(base, 'accounts', 'synthetic');
  mkdirSync(path.join(program, 'resources'), { recursive: true }); mkdirSync(local); mkdirSync(account, { recursive: true });
  const executable = path.join(program, 'ReceptionWorkbench.exe'), cli = path.join(account, 'workctl.exe'), mcp = path.join(account, 'mcp.js');
  writeFileSync(executable, 'synthetic'); writeFileSync(cli, 'synthetic'); writeFileSync(mcp, 'synthetic');
  writeFileSync(path.join(program, 'resources/reception-launch.json'), JSON.stringify({format:1,app_id:'local.inquiry.reception.workbench',routing:'verified-seller-v1'}));
  const env = { SystemRoot: process.env.SystemRoot, WINDIR: process.env.WINDIR, TEMP: base, TMP: base, PATH: process.env.PATH, LOCALAPPDATA: local, USERPROFILE: base, ACCIO_GATEWAY_TOKEN:'synthetic', ACCIO_NODE_BIN:process.execPath, ACCIO_MCP_SCRIPT:mcp, PHOENIX_PLUGIN_CLI_WORKCTL_PATH:cli };
  const registry = merchantRegistryDirectory(env);
  const created = JSON.parse(run(['-I', '-c', fixtureCode, source, base, registry, seller], env));
  assert(workspacePathUnits(created.root) > 260);
  const backend = { executable: python, prefix: ['-I', entry], cwd: projectRoot, env };
  const options = {currentExecutable:executable, backend, probe:async () => seller};
  return {base,program,env,registry,...created,backend,options};
}
function verify(f, plan) {
  const storage = JSON.parse(readFileSync(path.join(f.state, 'storage.json'), 'utf8'));
  const registry = JSON.parse(readFileSync(path.join(f.registry, 'merchants.json'), 'utf8'));
  assert.equal(storage.previousRoot, f.root);
  assert.equal(storage.dataRoot, plan.dataRoot);
  assert.equal(registry.profiles[seller].initialDataRoot, plan.dataRoot);
  assert.equal(registry.profiles[seller].status, 'created');
  assert.equal(path.dirname(plan.dataRoot), merchantSupportPaths(f.registry).dataDirectory);
  assert.equal(JSON.parse(run(['-I',entry,'--maintenance','prepare','--source',plan.dataRoot],f.env)).status, 'completed');
  const state = JSON.parse(run(['-I','-c',verifyCode,plan.dataRoot],f.env));
  assert.deepEqual(state, {seller:[['']],business:0,ledger:{operations:0,attempt_history:0,retry_grants:0},sellers_exists:false});
  assert.deepEqual(JSON.parse(run(['-I',entry,'--inspect-launch-owner','--data-dir',plan.dataRoot],f.env)), {status:'empty'});
  assert(existsSync(f.root));
}
const windows = {skip:process.platform !== 'win32', timeout:180000};
test('actual backend recovers a long legacy created profile and prepares without binding', windows, async () => {
  const f = setup(); try {
    assert.deepEqual(JSON.parse(run(['-I',entry,'--inspect-launch-owner','--data-dir',f.root],f.env)), {status:'unknown',reason:'zero_byte_workspace'});
    const plan = await launchAuthorized(f.program, f.env, f.options);
    assert.deepEqual(await resolveMerchantProfile(seller, f.env, {backend:f.backend}), {sellerId:seller,dataRoot:plan.dataRoot,stateDirectory:f.state});
    verify(f, plan);
  } finally { rmSync(f.base, {recursive:true,force:true}); }
});
test('failed real staging remains intact and a fresh launch recovers to a different target', windows, async () => {
  const f = setup(); try {
    let firstTarget;
    await assert.rejects(launchAuthorized(f.program, f.env, {...f.options, recoverProfile: async args => {
      firstTarget = args[args.indexOf('--destination')+1];
      const backup = args[args.indexOf('--backup-dir')+1];
      assert.throws(() => run(['-I','-c',failCode,source,f.registry,seller,firstTarget,backup], f.env));
      throw new Error('synthetic_commit_failure');
    }}), /merchant_initialization_incomplete/);
    assert(existsSync(path.join(firstTarget,'live','control.sqlite3')));
    const firstControl = readFileSync(path.join(firstTarget,'live','control.sqlite3'));
    const plan = await launchAuthorized(f.program, f.env, f.options);
    assert.notEqual(plan.dataRoot, firstTarget);
    assert.deepEqual(readFileSync(path.join(firstTarget,'live','control.sqlite3')), firstControl);
    verify(f, plan);
  } finally { rmSync(f.base, {recursive:true,force:true}); }
});
