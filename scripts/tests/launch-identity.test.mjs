import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, readdirSync, existsSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { probeNativeSeller } from '../launch-identity.mjs';

test('native probe executes the expected workflow, validates identity and removes temporary files', {skip:process.platform !== 'win32', timeout:90000}, async()=>{
  const root=mkdtempSync(path.join(os.tmpdir(),'probe-test-'));
  try {
    const source=path.join(root,'fake.cs'), exe=path.join(root,'fake.exe');
    writeFileSync(source, `using System; using System.IO; class Fake { static int Main(string[] a) { if(a.Length!=7 || a[0]!="workflow" || a[1]!="chat-analysis" || a[2]!="resolve-id" || a[3]!="--output-file" || a[5]!="--format" || a[6]!="json") return 3; File.WriteAllText(a[4], Environment.GetEnvironmentVariable("FAKE_IDENTITY")); return 0; } }`);
    const program='Add-Type -Path $env:FAKE_SOURCE -OutputAssembly $env:FAKE_EXE -OutputType ConsoleApplication';
    execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(program,'utf16le').toString('base64')],{windowsHide:true,env:{...process.env,FAKE_SOURCE:source,FAKE_EXE:exe}});
    const before=readdirSync(root).sort();
    for(const [identity,expected] of [['{"CURRENT_SELF_ALIID":"12345","other":"ignored"}','12345'],['{"CURRENT_SELF_ALIID":12345}','12345'],['{"CURRENT_SELF_ALIID":"01"}',null],['{"CURRENT_SELF_ALIID":9007199254740992}',null],['{}',null],['bad',null]]) {
      const attempt=probeNativeSeller({cli:exe,env:{...process.env,FAKE_IDENTITY:identity},tempRoot:root});
      if(expected) assert.equal(await attempt,expected); else await assert.rejects(attempt);
      assert.deepEqual(readdirSync(root).sort(),before);
    }
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('probe timeout terminates a descendant even after the native parent has exited', {skip:process.platform !== 'win32', timeout:90000}, async()=>{
  const root=mkdtempSync(path.join(os.tmpdir(),'probe-tree-'));
  try {
    const source=path.join(root,'fake.cs'),exe=path.join(root,'fake.exe'),child=path.join(root,'child.cjs');
    const childPid=path.join(root,'child.pid'),marker=path.join(root,'survived.txt');
    writeFileSync(child,'setTimeout(()=>require("node:fs").writeFileSync(process.env.FAKE_MARKER,"survived"),20000);');
    writeFileSync(source,String.raw`using System;using System.IO;using System.Diagnostics;class Fake{static int Main(string[] a){var start=new ProcessStartInfo(Environment.GetEnvironmentVariable("FAKE_NODE"),"\""+Environment.GetEnvironmentVariable("FAKE_CHILD")+"\""){UseShellExecute=false,CreateNoWindow=true};var child=Process.Start(start);File.WriteAllText(Environment.GetEnvironmentVariable("FAKE_PID"),child.Id.ToString());File.WriteAllText(a[4],Environment.GetEnvironmentVariable("FAKE_IDENTITY"));return 0;}}`);
    const compile='Add-Type -Path $env:FAKE_SOURCE -OutputAssembly $env:FAKE_EXE -OutputType ConsoleApplication';
    execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(compile,'utf16le').toString('base64')],{windowsHide:true,env:{...process.env,FAKE_SOURCE:source,FAKE_EXE:exe}});
    await assert.rejects(probeNativeSeller({cli:exe,tempRoot:root,timeoutMs:5000,env:{...process.env,FAKE_IDENTITY:'{"CURRENT_SELF_ALIID":"12345"}',FAKE_NODE:process.execPath,FAKE_CHILD:child,FAKE_PID:childPid,FAKE_MARKER:marker}}),/identity_probe_timeout/);
    assert(existsSync(childPid));const pid=Number(readFileSync(childPid,'utf8'));
    let stopped=false;const deadline=Date.now()+3000;
    while(Date.now()<deadline){try{process.kill(pid,0);}catch{stopped=true;break;}await new Promise(resolve=>setTimeout(resolve,20));}
    assert(stopped,'Owned descendant must be stopped with its job');assert.equal(existsSync(marker),false);
    assert(!readdirSync(root).some(name=>name.startsWith('reception-identity-')));
  } finally {rmSync(root,{recursive:true,force:true});}
});
