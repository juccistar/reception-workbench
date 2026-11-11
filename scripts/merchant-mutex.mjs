import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import path from 'node:path';

// The child owns a Windows kernel mutex; its stdin closing releases it even if
// the Node parent dies. Abandoned ownership is deliberately recovered.
export async function acquireMerchantRegistryMutex(registryDirectory, timeoutMs = 30000) {
  if (process.platform !== 'win32' || typeof registryDirectory !== 'string' || !path.isAbsolute(registryDirectory) || !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000) throw new Error('registry_lock_invalid');
  const key = createHash('sha256').update(path.resolve(registryDirectory).toLowerCase()).digest('hex');
  const name = `Global\\ReceptionMerchantRegistry_${key}`;
  const program = `$ErrorActionPreference='Stop';$m=[Threading.Mutex]::new($false,'${name}');$owned=$false;try{try{$owned=$m.WaitOne(${timeoutMs})}catch [Threading.AbandonedMutexException]{$owned=$true};if(-not $owned){[Console]::Out.WriteLine('busy');exit 2};[Console]::Out.WriteLine('acquired');[Console]::Out.Flush();[void][Console]::ReadLine()}finally{if($owned){$m.ReleaseMutex()};$m.Dispose()}`;
  const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
  const child = spawn(powershell,['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(program,'utf16le').toString('base64')],{windowsHide:true,stdio:['pipe','pipe','pipe']});
  child.stdin.on('error',()=>{}); child.stderr.resume();
  let exited = false;
  const done = new Promise(resolve=>child.once('close',()=>{exited=true;resolve();}));
  try {
    await new Promise((resolve,reject)=>{
      let ready=false, output='';
      const fail=()=>{if(!ready){ready=true;clearTimeout(timer);reject(new Error('registry_lock_unavailable'));}};
      const timer=setTimeout(()=>{child.kill();fail();},timeoutMs+10000);
      child.once('error',fail);child.once('close',fail);
      child.stdout.on('data',chunk=>{if(ready)return;output+=chunk.toString();if(output.length>128){child.kill();fail();return;}if(output.includes('\n')){ready=true;clearTimeout(timer);if(output.trim()==='acquired')resolve();else reject(new Error('registry_lock_unavailable'));}});
    });
  } catch(error) { child.stdin.end(); await done; throw error; }
  let released=false;
  return async()=>{if(released)return;released=true;if(!exited)child.stdin.end('\n');await done;};
}
