import { cp, access, readFile } from 'node:fs/promises';
import path from 'node:path';
import { root } from './paths.mjs';
import { assertReleasePath } from './release-paths.mjs';
const version=JSON.parse(await readFile(path.join(root,'package.json'),'utf8')).version;
const deliveryIndex=process.argv.indexOf('--delivery');
if(deliveryIndex>=0&&(!process.argv[deliveryIndex+1]||process.argv[deliveryIndex+1].startsWith('--')))throw new Error('delivery_path_missing');
const deliveryPath=path.resolve(root,deliveryIndex>=0?process.argv[deliveryIndex+1]:'artifacts/delivery.json');
const delivery=JSON.parse(await readFile(deliveryPath,'utf8'));
const source=assertReleasePath(root,path.dirname(delivery.executable));
assertReleasePath(root,delivery.executable);
const destination=path.join(root,`artifacts/runtime-${version}`);
if(JSON.parse(await readFile(path.join(source,'manifest.json'),'utf8')).version!==version)throw new Error('release_version_mismatch');
let exists=true;try{await access(destination);}catch{exists=false;}
if(exists)throw new Error('runtime_copy_already_exists');
await cp(source,destination,{recursive:true,errorOnExist:true,force:false});
console.log(JSON.stringify({runtime_copy_created:true,version}));
