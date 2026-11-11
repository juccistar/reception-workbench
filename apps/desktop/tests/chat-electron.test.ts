// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { buildSync } from 'esbuild';
import { execFile } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

const project = path.resolve(import.meta.dirname, '../../..');

describe('real sandboxed Electron chat bridge', () => {
  it('roundtrips exact chat requests through the production preload without exposing Node', async () => {
    const root = mkdtempSync(path.join(os.tmpdir(), 'reception-chat-electron-'));
    try {
      buildSync({ entryPoints: [path.join(project, 'apps/desktop/src/preload/index.ts')], outfile: path.join(root, 'preload.cjs'), bundle: true, platform: 'node', format: 'cjs', external: ['electron'], logLevel: 'silent' });
      // This test host has an in-memory synthetic service; it never loads the production backend or any database.
      const harness = `
        const {app,BrowserWindow,ipcMain} = require('electron');
        const fs = require('node:fs'); const path = require('node:path');
        const {validateChatParameters} = require(${JSON.stringify(path.join(project, 'apps/desktop/src/shared/chat-protocol.ts'))});
        const root = __dirname; let win; const calls = [];
        app.setPath('userData',path.join(root,'profile'));
        app.whenReady().then(async()=>{
          ipcMain.handle('reception:invoke',(event,method,params)=>{
            if(event.sender!==win.webContents || event.senderFrame!==win.webContents.mainFrame) throw Error('foreign sender');
            if(method==='inject_demo') {
              fs.writeFileSync(path.join(root,'result.json'),JSON.stringify({result:params.test_result,calls}));
              setImmediate(()=>{win.destroy();app.quit();}); return {};
            }
            validateChatParameters(method,params); calls.push({method,params});
            if(method==='begin_manual_edit') return {conversations:[{conversation_id:params.conversation_id,human_owned:true}]};
            if(method==='save_chat_draft') return {purpose:'chat',draft_id:'isolated-chat',revision:3,conversation_id:params.conversation_id,body:params.body};
            if(method==='send_chat') return {job_id:'isolated-job'};
            throw Error('unexpected test method');
          });
          ipcMain.handle('reception:storage:get',()=>({current_root:'synthetic'}));
          ipcMain.handle('reception:storage:choose',(_event,purpose)=>{calls.push({channel:'choose',purpose});return 'synthetic-target';});
          ipcMain.handle('reception:storage:open',(_event,which)=>{calls.push({channel:'open',which});});
          ipcMain.handle('reception:storage:manage',(_event,request)=>{calls.push({channel:'manage',request});return {status:'cancelled',message:'cancelled'};});
          win = new BrowserWindow({show:false,webPreferences:{preload:path.join(root,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true,webSecurity:true}});
          win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
          await win.loadFile(path.join(root,'index.html'));
        });
      `;
      buildSync({ stdin: { contents: harness, resolveDir: project, sourcefile: 'isolated-chat-host.cjs' }, outfile: path.join(root, 'host.cjs'), bundle: true, platform: 'node', format: 'cjs', external: ['electron'], logLevel: 'silent' });
      writeFileSync(path.join(root, 'index.html'), `<!doctype html><meta charset="utf-8"><script>
        addEventListener('DOMContentLoaded',async()=>{
          const bridge=window.reception;
          const result={nodeHidden:typeof require==='undefined' && typeof process==='undefined', methods:Object.keys(bridge)};
          try {
            const owner=await bridge.invoke('begin_manual_edit',{conversation_id:'isolated-customer'});
            const draft=await bridge.invoke('save_chat_draft',{conversation_id:'isolated-customer',body:'Literal 中文\\nsecond line',expected_revision:null,client_mutation_id:'isolated-save'});
            const params={conversation_id:'isolated-customer',draft_id:draft.draft_id,expected_revision:draft.revision,client_request_id:'isolated-original-request'};
            result.owner=owner.conversations[0].human_owned;
            result.original=await bridge.invoke('send_chat',params);
            result.retry=await bridge.invoke('send_chat',params);
            result.storage=await bridge.getStorageInfo();
            await bridge.chooseStorageDirectory('backup');
            await bridge.openStorageDirectory('current');
            result.storageResult=await bridge.manageStorage({action:'backup',target:'synthetic-target'});
            result.invalidRevision=false;result.shellDenied=false;
            try{await bridge.invoke('send_chat',{...params,expected_revision:null});}catch{result.invalidRevision=true;}
            try{await bridge.invoke('shell',{command:'never executed'});}catch{result.shellDenied=true;}
          } catch(error) {result.error=error.message;}
          await bridge.invoke('inject_demo',{test_result:result});
        });
      </script>`, 'utf8');
      const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
      await promisify(execFile)(path.join(project, 'node_modules/electron/dist', process.platform === 'win32' ? 'electron.exe' : 'electron'), [path.join(root, 'host.cjs')], { env, timeout: 25000, windowsHide: true, maxBuffer: 1024 * 1024 });
      const output = JSON.parse(readFileSync(path.join(root, 'result.json'), 'utf8'));
      expect(output.result.error).toBeUndefined(); expect(output.result.nodeHidden).toBe(true);
      expect(output.result.owner).toBe(true); expect(output.result.retry).toEqual(output.result.original);
      expect(output.result.invalidRevision).toBe(true); expect(output.result.shellDenied).toBe(true);
      expect(output.calls.filter((item: { method?: string }) => item.method).map((item: { method: string }) => item.method)).toEqual(['begin_manual_edit', 'save_chat_draft', 'send_chat', 'send_chat']);
      expect(output.result.storage.current_root).toBe('synthetic');
      expect(output.result.storageResult.status).toBe('cancelled');
      expect(output.calls.slice(4)).toEqual([{ channel: 'choose', purpose: 'backup' }, { channel: 'open', which: 'current' }, { channel: 'manage', request: { action: 'backup', target: 'synthetic-target' } }]);
      expect(output.calls[2].params).toEqual(output.calls[3].params);
      expect(output.calls[1].params.body).toBe('Literal 中文\nsecond line');
      expect(output.result.methods.sort()).toEqual(['chooseWorkctl', 'confirmSend', 'getPreferences', 'invoke', 'onEvent', 'savePreferences', 'getStorageInfo', 'chooseStorageDirectory', 'openStorageDirectory', 'manageStorage', 'getAccioLaunchInfo', 'copyAccioLaunchInstructions', 'getUpdateInfo', 'saveUpdateSettings', 'checkForUpdates', 'downloadUpdate', 'installUpdate'].sort());
    } finally { rmSync(root, { recursive: true, force: true }); }
  }, 35000);
});
