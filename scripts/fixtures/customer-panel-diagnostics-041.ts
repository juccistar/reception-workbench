/** Observe synthetic side-panel RPCs without changing their authorization or results. */
import { CustomerPanelController as ProductionController } from '../../apps/desktop/src/main/whatsapp/customer-panel';
const reports = new Map<string, Record<string, any>>();
export class CustomerPanelController extends ProductionController {
  constructor(options: any) {
    const report: Record<string, any> = { searches: 0 };
    reports.set(options.workspaceId, report);
    super({ ...options, searchCustomers: async (input: any) => {
      report.searches++; report.query = input.query; const started = Date.now();
      try { const rows = await options.searchCustomers(input); report.results = rows; return rows; }
      catch (error) { report.search_error = String(error); throw error; }
      finally { report.search_elapsed_ms = Date.now() - started; }
    } });
    const self = this as any, handle = self.handleBinding.bind(this), check = self.assertCurrent.bind(this);
    self.handleBinding = async (event: any, generation: number) => {
      let packet: any; try { packet = JSON.parse(event.payload); } catch {}
      report.last_binding = { action: packet?.action, execution_context: event.executionContextId, expected_context: self.contextId, token_matches: packet?.token === self.token, view_present: !!self.view, identity_matches: JSON.stringify(packet?.identity) === JSON.stringify(self.view?.identity) };
      return handle(event, generation);
    };
    self.assertCurrent = async (...args: any[]) => { try { return await check(...args); } catch (error) { report.current_error = String(error); throw error; } };
  }
}
(globalThis as any).panelDiagnostics = () => [...reports].map(([workspace_id, data]) => ({ workspace_id, ...data }));
