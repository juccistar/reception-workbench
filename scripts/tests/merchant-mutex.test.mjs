import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { acquireMerchantRegistryMutex } from '../merchant-mutex.mjs';

test('Windows registry mutex serializes competing owners and releases on close', {skip:process.platform!=='win32', timeout:30000}, async()=>{
  const key=path.join(os.tmpdir(),`merchant-lock-test-${process.pid}`);
  const release=await acquireMerchantRegistryMutex(key,5000);
  try {
    await assert.rejects(acquireMerchantRegistryMutex(key,1000),/registry_lock_unavailable/);
  } finally { await release(); }
  const releaseNext=await acquireMerchantRegistryMutex(key,5000);
  await releaseNext();
});
