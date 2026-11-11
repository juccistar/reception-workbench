import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { launchAuthorized } from '../connect-live.mjs';
import { AUTO_LAUNCH_CONTEXT_ENV, consumeAutoLaunchContext, merchantRegistryDirectory, merchantSupportPaths } from '../merchant-profiles.mjs';
import { writeBootstrap } from '../data-location.mjs';
import { acquireMerchantRegistryMutex } from '../merchant-mutex.mjs';

function fixture() {
  const root = mkdtempSync(path.join(os.tmpdir(), 'authorized-launch-'));
  const program = path.join(root, 'program'), local = path.join(root, 'local');
  mkdirSync(path.join(program, 'resources'), { recursive: true }); mkdirSync(local);
  const executable = path.join(program, 'ReceptionWorkbench.exe');
  writeFileSync(executable, 'SYNTHETIC PLAN ONLY; NEVER EXECUTED');
  writeFileSync(path.join(program, 'resources/reception-launch.json'), JSON.stringify({ format: 1, app_id: 'local.inquiry.reception.workbench', routing: 'verified-seller-v1', version: '0.2.20' }));
  function environment(account) {
    const home = path.join(root, 'accounts', account); mkdirSync(home, { recursive: true });
    const cli = path.join(home, 'workctl.exe'), mcp = path.join(home, 'mcp.js');
    writeFileSync(cli, 'NOT EXECUTED'); writeFileSync(mcp, '// synthetic');
    const env = { ...process.env, LOCALAPPDATA: local, ACCIO_GATEWAY_TOKEN: 'SYNTHETIC_MEMORY_ONLY', ACCIO_NODE_BIN: process.execPath, ACCIO_MCP_SCRIPT: mcp, PHOENIX_PLUGIN_CLI_WORKCTL_PATH: cli };
    delete env.RECEPTION_MERCHANT_REGISTRY_DIR; delete env[AUTO_LAUNCH_CONTEXT_ENV];
    return env;
  }
  const options = { currentExecutable: executable, backend: { executable: process.execPath, prefix: [] }, inspectOwnership: async () => ({ status: 'empty' }) };
  return { root, program, local, executable, options, environment, cleanup: () => rmSync(root, { recursive: true, force: true }) };
}
const windows = { skip: process.platform !== 'win32', timeout: 60000 };

test('same launcher resolves the caller seller into isolated context and does not mutate caller authorization', windows, async () => {
  const f = fixture(); try {
    const envA = f.environment('A'), envB = f.environment('B');
    const a = await launchAuthorized(f.program, envA, { ...f.options, probe: async () => '12345' });
    const b = await launchAuthorized(f.program, envB, { ...f.options, probe: async () => '67890' });
    assert.equal(a.executable, f.executable); assert.equal(b.executable, f.executable);
    assert.notEqual(a.dataRoot, b.dataRoot); assert.notEqual(a.stateDirectory, b.stateDirectory);
    assert.equal(a.args[a.args.indexOf('--expect-seller') + 1], '12345');
    assert.equal(b.args[b.args.indexOf('--expect-seller') + 1], '67890');
    assert.equal(a.args[a.args.indexOf('--workctl') + 1], envA.PHOENIX_PLUGIN_CLI_WORKCTL_PATH);
    assert.equal(b.args[b.args.indexOf('--workctl') + 1], envB.PHOENIX_PLUGIN_CLI_WORKCTL_PATH);
    assert(!a.args.join(' ').includes('SYNTHETIC_MEMORY_ONLY'));
    assert.equal(a.env.ACCIO_GATEWAY_TOKEN, envA.ACCIO_GATEWAY_TOKEN);
    assert.equal(envA[AUTO_LAUNCH_CONTEXT_ENV], undefined);
    assert.deepEqual(consumeAutoLaunchContext(a.env, a.args), { sellerId: '12345', dataRoot: a.dataRoot, stateDirectory: a.stateDirectory });
    assert.equal(a.env[AUTO_LAUNCH_CONTEXT_ENV], undefined);
    assert.equal(consumeAutoLaunchContext(a.env, a.args), null);
  } finally { f.cleanup(); }
});

test('native failure or expected seller mismatch never allocates a business workspace', windows, async () => {
  const f = fixture(); try {
    const env = f.environment('A'), directory = merchantRegistryDirectory(env);
    await assert.rejects(launchAuthorized(f.program, env, { ...f.options, probe: async () => { throw new Error('identity_probe_failed'); } }), /identity_probe_failed/);
    assert.equal(existsSync(directory), false);
    await assert.rejects(launchAuthorized(f.program, env, { ...f.options, seller: '67890', probe: async () => '12345' }), /seller_mismatch/);
    assert.equal(existsSync(directory), false);
    const missing = { ...env }; delete missing.ACCIO_GATEWAY_TOKEN;
    await assert.rejects(launchAuthorized(f.program, missing, f.options), /authorized_environment_missing/);
    assert.equal(existsSync(directory), false);
  } finally { f.cleanup(); }
});

test('original positively owned root survives and a foreign current workctl is rejected before probe', windows, async () => {
  const f = fixture(); try {
    const envA = f.environment('A'), envB = f.environment('B');
    const original = path.join(f.root, 'original'); mkdirSync(original); writeFileSync(path.join(original, 'keep.txt'), 'KEEP');
    const config = path.join(merchantRegistryDirectory(envA), 'storage.json');
    writeBootstrap(config, { version: 1, dataRoot: original }); const before = readFileSync(config);
    const plan = await launchAuthorized(f.program, envA, { ...f.options, probe: async () => '12345', inspectOwnership: async root => root === original ? { status: 'owned', sellerId: '12345' } : { status: 'empty' } });
    assert.equal(plan.dataRoot, original); assert.deepEqual(readFileSync(config), before);
    let called = false;
    await assert.rejects(launchAuthorized(f.program, envA, { ...f.options, workctl: envB.PHOENIX_PLUGIN_CLI_WORKCTL_PATH, probe: async () => { called = true; return '12345'; } }), /workctl_invalid/);
    assert.equal(called, false); assert.equal(readFileSync(path.join(original, 'keep.txt'), 'utf8'), 'KEEP');
  } finally { f.cleanup(); }
});

test('recovery runs once after releasing the registry mutex', windows, async () => {
  const f = fixture(); try {
    const env = f.environment('A'), oldRoot = path.join(f.root, 'old');
    const first = await launchAuthorized(f.program, env, { ...f.options, dataRoot: oldRoot, probe: async () => '12345' });
    let calls = 0, recovered;
    const plan = await launchAuthorized(f.program, env, { ...f.options, probe: async () => '12345',
      inspectOwnership: async root => root === oldRoot ? { status: 'unknown', reason: 'zero_byte_workspace' } : { status: 'empty' },
      recoverProfile: async args => {
        calls++;
        const release = await acquireMerchantRegistryMutex(merchantRegistryDirectory(env));
        try {
          const destination = args[args.indexOf('--destination') + 1];
          const support = merchantSupportPaths(merchantRegistryDirectory(env));
          assert.equal(path.dirname(destination), support.dataDirectory);
          assert.match(path.basename(destination), /^12345-recovery-[0-9a-f]{8}$/);
          assert.equal(args[args.indexOf('--backup-dir') + 1], support.backupDirectory);
          recovered = destination;
          mkdirSync(destination, { recursive: true });
          const registryFile = path.join(merchantRegistryDirectory(env), 'merchants.json');
          const registry = JSON.parse(readFileSync(registryFile));
          registry.profiles['12345'].initialDataRoot = destination;
          writeFileSync(registryFile, JSON.stringify(registry));
          writeFileSync(path.join(first.stateDirectory, 'storage.json'), JSON.stringify({ version: 1, dataRoot: destination, previousRoot: oldRoot }));
          return { status: 'completed', action: 'recover-profile', data_root: destination };
        } finally { await release(); }
      }
    });
    assert.equal(calls, 1);
    assert.equal(plan.dataRoot, recovered);
  } finally { f.cleanup(); }
});

test('failed recovery preserves staging and retry uses a fresh destination', windows, async () => {
  const f = fixture(); try {
    const env = f.environment('A');
    const first = await launchAuthorized(f.program, env, { ...f.options, probe: async () => '12345' });
    const targets = [];
    const options = { ...f.options, probe: async () => '12345',
      inspectOwnership: async root => root === first.dataRoot ? { status: 'unknown', reason: 'zero_byte_workspace' } : { status: 'empty' },
      recoverProfile: async args => {
        const destination = args[args.indexOf('--destination') + 1];
        targets.push(destination);
        mkdirSync(destination, { recursive: true });
        writeFileSync(path.join(destination, 'keep.txt'), `attempt-${targets.length}`);
        throw new Error('synthetic failure');
      }
    };
    await assert.rejects(launchAuthorized(f.program, env, options), /merchant_initialization_incomplete/);
    await assert.rejects(launchAuthorized(f.program, env, options), /merchant_initialization_incomplete/);
    assert.equal(targets.length, 2);
    assert.notEqual(targets[0], targets[1]);
    assert.equal(readFileSync(path.join(targets[0], 'keep.txt'), 'utf8'), 'attempt-1');
  } finally { f.cleanup(); }
});

test('failed initialization writes only controlled diagnostics', windows, async () => {
  const f = fixture(); try {
    const env = f.environment('A');
    await launchAuthorized(f.program, env, { ...f.options, probe: async () => '12345' });
    await assert.rejects(launchAuthorized(f.program, env, { ...f.options, probe: async () => '12345', inspectOwnership: async () => ({ status: 'unknown', reason: 'zero_byte_workspace', token: 'NEVER_WRITE' }) }), error => {
      assert.equal(error.message, 'merchant_initialization_incomplete');
      const content = readFileSync(error.diagnostic, 'utf8');
      assert(!content.includes('SYNTHETIC_MEMORY_ONLY'));
      assert(!content.includes('NEVER_WRITE'));
      const diagnostic = JSON.parse(content);
      assert.equal(diagnostic.seller, '12345');
      assert.equal(diagnostic.ownerStatus, 'unknown');
      assert.equal(diagnostic.code, 'merchant_initialization_incomplete');
      return true;
    });
  } finally { f.cleanup(); }
});

test('cannot pair a new launcher with a missing policy or an unrelated executable', windows, async () => {
  const f = fixture(); try {
    const env = f.environment('A');
    rmSync(path.join(f.program, 'resources/reception-launch.json'));
    await assert.rejects(launchAuthorized(f.program, env, { ...f.options, probe: async () => '12345' }), /launch_components_missing/);
    const other = path.join(f.root, 'ReceptionWorkbench.exe'); writeFileSync(other, 'NOT EXECUTED');
    await assert.rejects(launchAuthorized(f.program, env, { ...f.options, currentExecutable: other, probe: async () => '12345' }), /release_invalid/);
  } finally { f.cleanup(); }
});
