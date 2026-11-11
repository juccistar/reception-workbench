import { createReadStream } from 'node:fs';
import { readFile, lstat, writeFile, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import path from 'node:path';
import assert from 'node:assert/strict';
import { root } from './paths.mjs';
import { assertReleasePath } from './release-paths.mjs';
const require=createRequire(import.meta.url);
const {listPackage}=require('@electron/asar');
function option(name){const index=process.argv.indexOf(name);if(index<0)return '';const value=process.argv[index+1];if(!value||value.startsWith('--'))throw new Error('Missing value for '+name);return value;}
const deliveryPath=path.resolve(root,option('--delivery')||'artifacts/delivery.json');
const outputPath=path.resolve(root,option('--output')||'artifacts/verification/release-integrity.json');
const delivery=JSON.parse(await readFile(deliveryPath,'utf8'));
const folder=assertReleasePath(root,path.dirname(delivery.executable));
assertReleasePath(root,delivery.executable);
assertReleasePath(root,delivery.archive);
const manifest=JSON.parse(await readFile(path.join(folder,'manifest.json'),'utf8'));
const expectedVersion=option('--expected-version');
if(expectedVersion) assert.equal(manifest.version, expectedVersion, 'Release version mismatch');
async function digest(file){const value=createHash('sha256');for await(const chunk of createReadStream(file))value.update(chunk);return value.digest('hex');}
for(const entry of manifest.files){
 const file=path.join(folder,entry.path);
 assert(file.startsWith(folder+path.sep));
 const stat=await lstat(file);assert(stat.isFile()&&!stat.isSymbolicLink());
 assert.equal(stat.size,entry.bytes,entry.path);
 assert.equal(await digest(file),entry.sha256,entry.path);
 assert(!/(^|\/)(accounts|\.runtime|\.venv)(\/|$)|\.(?:sqlite3?|db|log)(?:-(?:wal|shm))?$/i.test(entry.path),entry.path);
}
async function fileNames(directory, prefix='') {
 const names=[];
 for(const entry of await readdir(directory,{withFileTypes:true})) {
  assert(!entry.isSymbolicLink(),'Unexpected distribution link');
  const name=prefix+entry.name;
  if(entry.isDirectory())names.push(...await fileNames(path.join(directory,entry.name),name+'/'));
  else names.push(name);
 }
 return names;
}
const actualNames=await fileNames(folder);
assert.deepEqual(actualNames.sort(),[...manifest.files.map(entry=>entry.path),'manifest.json'].sort(),'Unexpected or missing files in the release directory');
const entries=listPackage(path.join(folder,'resources/app.asar')).map(name=>name.replaceAll('\\','/'));
const unexpected=entries.filter(name=>!/^\/(?:main|preload|renderer)(?:\/|$)/.test(name)&&name!=='/package.json');
assert.deepEqual(unexpected,[],'Unexpected input in compiled application archive');
assert.equal(await digest(delivery.archive),delivery.archive_sha256);
const result={passed:true,version:manifest.version,files:manifest.files.length,asar_entries:entries.length,archive_sha256:delivery.archive_sha256,customer_databases:0,development_dependencies:0,signed:delivery.signed};
await writeFile(outputPath,JSON.stringify(result,null,2));
console.log(JSON.stringify(result));
