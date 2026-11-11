import test from 'node:test';
import assert from 'node:assert/strict';
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createHash, generateKeyPairSync } from 'node:crypto';
import { parsePackageArgs, updateConfig, createReleaseFeed } from '../release-feed.mjs';
import { loadOrCreateReleaseKeys, verifyReleaseMetadata } from '../update-signing.mjs';

test('package arguments reject missing output, unsafe paths and non-HTTPS sources', () => {
 const root = path.resolve('project');
 for (const args of [['--output'], ['--output','--candidate'], ['--output',path.resolve('elsewhere')], ['--update-url','http://example.com','--installer'], ['--update-url','https://user:pass@example.com','--installer'], ['--update-url','https://example.com'], ['--output','x','--output','y']]) assert.throws(() => parsePackageArgs(args,root));
 const parsed = parsePackageArgs(['--installer','--candidate','--output',path.join(root,'artifacts/new'),'--update-url','https://example.com/releases'],root);
 assert.equal(parsed.updateUrl,'https://example.com/releases/');
 assert.equal(parsed.installer,true);
 assert.equal(parsed.candidate,true);
});

test('updater config preserves cache and certificate publisher without inventing a server', () => {
 assert.deepEqual(JSON.parse(updateConfig(undefined, null)),{updaterCacheDirName:'inquiry-reception-workbench-updater'});
 assert.deepEqual(JSON.parse(updateConfig('https://example.com/', ['Publisher'], 'stable-cache')),{updaterCacheDirName:'stable-cache',provider:'generic',url:'https://example.com/',publisherName:['Publisher']});
 assert.deepEqual(JSON.parse(updateConfig('https://github.com/juccistar/reception-workbench-releases/', null)),{updaterCacheDirName:'inquiry-reception-workbench-updater',provider:'github',owner:'juccistar',repo:'reception-workbench-releases',private:false});
 const root=path.resolve('project');
 assert.equal(parsePackageArgs(['--installer','--output',path.join(root,'relase/test'),'--update-url','https://github.com/juccistar/reception-workbench-releases/releases'],root).updateUrl,'https://github.com/juccistar/reception-workbench-releases/');
 assert.throws(()=>parsePackageArgs(['--installer','--update-url','https://github.com/a/b/releases/download/v1.0.0/setup.exe'],root));
});

test('release keys remain stable and incomplete or corrupt keys are rejected', async () => {
 const dir=await mkdtemp(path.join(tmpdir(),'reception-keys-'));
 try {
  const keyDir=path.join(dir,'keys');
  const first=await loadOrCreateReleaseKeys(keyDir);
  assert.deepEqual(await loadOrCreateReleaseKeys(keyDir),first);
  await rm(path.join(keyDir,'public.pem'));
  await assert.rejects(loadOrCreateReleaseKeys(keyDir));
  await writeFile(path.join(keyDir,'public.pem'),'corrupt');
  await assert.rejects(loadOrCreateReleaseKeys(keyDir));
  const partial=path.join(dir,'partial');
  await mkdir(partial);
  await assert.rejects(loadOrCreateReleaseKeys(partial));
 } finally { await rm(dir,{recursive:true,force:true}); }
});

test('release build refuses missing or rotated published keys without generating replacements', async () => {
 const dir=await mkdtemp(path.join(tmpdir(),'reception-pinned-keys-'));
 try {
  const missing=path.join(dir,'missing');
  await assert.rejects(loadOrCreateReleaseKeys(missing,{allowCreate:false,expectedPublicKeyFingerprint:'0'.repeat(64)}));
  await assert.rejects(access(missing));
  const keyDir=path.join(dir,'existing');
  const original=await loadOrCreateReleaseKeys(keyDir);
  const pinned=createHash('sha256').update(original.publicKey).digest('hex');
  assert.deepEqual(await loadOrCreateReleaseKeys(keyDir,{allowCreate:false,expectedPublicKeyFingerprint:pinned}),original);
  const before=await readFile(path.join(keyDir,'public.pem'));
  await assert.rejects(loadOrCreateReleaseKeys(keyDir,{allowCreate:false,expectedPublicKeyFingerprint:'0'.repeat(64)}));
  assert.deepEqual(await readFile(path.join(keyDir,'public.pem')),before);
 } finally {await rm(dir,{recursive:true,force:true});}
});

test('feed metadata describes the actual installer and blockmap', async () => {
 const dir = await mkdtemp(path.join(tmpdir(),'reception-feed-'));
 try {
  const installer = path.join(dir,'Setup-0.2.16.exe');
  const bytes = Buffer.from('installer content');
  await writeFile(installer,bytes);
  await writeFile(installer+'.blockmap','blockmap');
  const keys = await loadOrCreateReleaseKeys(path.join(dir,'keys'));
  const result = await createReleaseFeed({output:dir,installer,version:'0.2.16',signed:false,privateKey:keys.privateKey});
  const metadata = JSON.parse(await readFile(path.join(result.feed,'latest.yml'),'utf8'));
  assert.equal(metadata.path,path.basename(installer));
  assert.equal(metadata.files[0].size,bytes.length);
  assert.equal(metadata.sha512,createHash('sha512').update(bytes).digest('base64'));
  assert.deepEqual(await readFile(path.join(result.feed,metadata.path)),bytes);
  assert.equal((await readFile(path.join(result.feed,path.basename(installer)+'.blockmap'))).toString(),'blockmap');
  assert.equal(result.manifest.signed,false);
  assert.equal(result.manifest.release_signature,'ed25519');
  assert.deepEqual(verifyReleaseMetadata(metadata,keys.publicKey),{version:'0.2.16',artifact:path.basename(installer),sha512:metadata.sha512,size:bytes.length});
  for (const mutate of [
   m=>m.version='0.2.17', m=>m.files[0].size++, m=>m.files[0].url='../evil.exe',
   m=>m.path='other.exe', m=>m.sha512='wrong', m=>m.files.push({...m.files[0]}),
   m=>m.packages={}, m=>m.reception_signature.signature='A'.repeat(86)+'==', m=>m.version='1.0.0-beta'
  ]) { const altered=structuredClone(metadata); mutate(altered); assert.equal(verifyReleaseMetadata(altered,keys.publicKey),null); }
  assert.equal(verifyReleaseMetadata(metadata,'invalid key'),null);
  const wrongKey=generateKeyPairSync('ed25519').publicKey.export({type:'spki',format:'pem'});
  const rsaKey=generateKeyPairSync('rsa',{modulusLength:2048}).publicKey.export({type:'spki',format:'pem'});
  assert.equal(verifyReleaseMetadata(metadata,wrongKey),null);
  assert.equal(verifyReleaseMetadata(metadata,rsaKey),null);
  await assert.rejects(createReleaseFeed({output:dir,installer,version:'0.2.16',signed:false,privateKey:keys.privateKey}));
 } finally { await rm(dir,{recursive:true,force:true}); }
});
