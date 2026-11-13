import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ProductsPage, SuppliersPage, ProcurementPage } from '../src/renderer/components/ProductProcurementPages';
import { ProductMainImage } from '../src/renderer/components/ProductMainImage';

const product = { local_product_id: 'p1', platform: 'demo', product_id: '123', name: '棉帽', product_url: null, product_images: [], default_offer_id: null, revision: 3, supplier_count: 1 };
const supplier = { supplier_id: 's1', name: '供应商甲', platform: 'demo', platform_supplier_id: null, product_url: null, contact: '', notes: '', revision: 2 };
const offer = { offer_id: 'o1', local_product_id: 'p1', supplier_id: 's1', supplier_name: '供应商甲', supplier_product_id: null, product_url: null, specifications: '蓝色', moq: '100', unit: '件', unit_price: '2', currency: 'USD', lead_time: '30天', packaging: '', freight: '', tax_included: null, valid_until: null, source_type: 'manual_verified', source_note: '', customer_wording: '', enabled: true, reply_enabled: true, revision: 2 };
function setup() {
  const invoke = vi.fn(async (method: string, _params: unknown = {}) => {
    if (method === 'list_products') return { items: [product] };
    if (method === 'list_suppliers') return { items: [supplier] };
    if (method === 'get_product') return { product: { ...product, product_main_image: { status: 'ready', updated_at: 1 } }, offers: [offer], suppliers: [supplier], tasks: [] };
    if (method === 'get_product_main_image') return { status: 'ready', data_url: 'data:image/png;base64,AAAA' };
    if (method === 'get_supplier') return { supplier, offers: [offer], products: [product] };
    if (method === 'get_procurement_settings') return { auto_find: false, auto_inquiry: false, supplier_limit: 3, authorized: false, inquiry_available: false, inquiry_reason: '真实询价接口未验证' };
    if (method === 'list_procurement_tasks') return { items: [{ task_id: 't1', local_product_id: 'p1', product_id: '123', status: 'unknown', phase: 'search', message: '结果未知', updated_at: 1, conversation_name: null }] };
    return {};
  });
  Object.defineProperty(window, 'reception', { configurable: true, value: { invoke, onEvent: () => () => {} } });
  return invoke;
}
afterEach(() => { Reflect.deleteProperty(window, 'reception'); });

it('selects and clears a current case offer with the projected revision and sets a separate product default', async () => {
  const invoke = setup(); const changed = vi.fn();
  const view = render(<ProductsPage initialProductId="p1" caseSelection={{ case_id: 'case1', revision: 7, customer_name: 'Emma', product_name: '棉帽', local_product_id: 'p1', offer_id: null }} onCaseChanged={changed} />);
  fireEvent.click(await screen.findByRole('button', { name: '选用于当前事项' }));
  expect(screen.getByText('当前客户：Emma · 棉帽')).toBeInTheDocument();
  expect(await screen.findByRole('button', { name: '放大查看商品主图' })).toBeInTheDocument();
  expect(invoke).toHaveBeenCalledWith('get_product_main_image', { local_product_id: 'p1' });
  expect(screen.queryByText(/当前事项：case1/)).not.toBeInTheDocument();
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('select_supplier_offer', expect.objectContaining({ case_id: 'case1', offer_id: 'o1', expected_case_revision: 7, client_mutation_id: expect.any(String) })));
  expect(changed).toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '设为默认' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('set_product_default_offer', expect.objectContaining({ local_product_id: 'p1', offer_id: 'o1', expected_revision: 3 })));
  view.rerender(<ProductsPage initialProductId="p1" caseSelection={{ case_id: 'case1', revision: 8, local_product_id: 'p1', offer_id: 'o1' }} onCaseChanged={changed} />);
  fireEvent.click(await screen.findByRole('button', { name: '清除当前事项选择' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('select_supplier_offer', expect.objectContaining({ case_id: 'case1', offer_id: null, expected_case_revision: 8 })));
});

it('does not show a late product image after switching products', async () => {
  let resolveOld!: (value: unknown) => void;
  const invoke = vi.fn((method: string, params: Record<string, unknown>) => params.local_product_id === 'old' ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve({ status: 'ready', data_url: 'data:image/png;base64,BBBB' }));
  const view = render(<ProductMainImage localProductId="old" metadata={{ status: 'loading' }} invoke={invoke as never} />);
  view.rerender(<ProductMainImage localProductId="new" metadata={{ status: 'ready' }} invoke={invoke as never} />);
  const image = await screen.findByRole('img', { name: '商品主图' });
  expect(image).toHaveAttribute('src', 'data:image/png;base64,BBBB');
  await act(async () => resolveOld({ status: 'ready', data_url: 'data:image/png;base64,AAAA' }));
  expect(screen.getByRole('img', { name: '商品主图' })).toHaveAttribute('src', 'data:image/png;base64,BBBB');
});

it('rejects a remote URL returned by the local image RPC', async () => {
  const invoke = vi.fn(async () => ({ status: 'ready', data_url: 'https://example.com/image.png' }));
  render(<ProductMainImage localProductId="p1" metadata={{ status: 'ready' }} invoke={invoke as never} />);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('get_product_main_image', { local_product_id: 'p1' }));
  expect(screen.getByText('暂无可用商品主图')).toBeInTheDocument();
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
});

it('retains the offer form when a real save error occurs', async () => {
  const invoke = setup(); const original = invoke.getMockImplementation()!;
  invoke.mockImplementation(async (method: string, params: unknown = {}) => { if (method === 'save_product_offer') throw new Error('供货方案已变化，请刷新'); return original(method, params); });
  render(<ProductsPage initialProductId="p1" />);
  fireEvent.click(await screen.findByRole('button', { name: '新增供货方案' }));
  fireEvent.change(screen.getByLabelText('规格', { exact: true }), { target: { value: '手工录入规格' } });
  fireEvent.click(screen.getByRole('button', { name: '保存供货方案' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('供货方案已变化，请刷新');
  expect(screen.getByLabelText('规格', { exact: true })).toHaveValue('手工录入规格');
});

it('retains supplier form values and displays the actual save failure', async () => {
  const invoke = setup(); invoke.mockImplementation(async (method: string) => { if (method === 'list_suppliers') return { items: [] }; if (method === 'save_supplier') throw new Error('供应商保存失败'); return {}; });
  render(<SuppliersPage />);
  fireEvent.click(screen.getByRole('button', { name: '新增供应商' }));
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '新供应商' } });
  fireEvent.change(screen.getByLabelText('平台'), { target: { value: 'demo' } });
  fireEvent.click(screen.getByRole('button', { name: '保存供应商' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('供应商保存失败');
  expect(screen.getByLabelText('名称')).toHaveValue('新供应商');
  expect(screen.getByLabelText('平台')).toHaveValue('demo');
});

it('exposes the blocked inquiry capability and reconciles an unknown task without a new request', async () => {
  const invoke = setup(); render(<ProcurementPage />);
  expect(await screen.findByText(/真实询价接口未验证/)).toBeInTheDocument();
  expect(screen.getByLabelText('供应商上限')).toHaveAttribute('max', '3');
  const task = screen.getByText('结果未知', { selector: 'strong' }).closest('article')!;
  fireEvent.click(within(task).getByRole('button', { name: '核查原任务' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('reconcile_procurement', { task_id: 't1' }));
  expect(invoke).not.toHaveBeenCalledWith('request_procurement', expect.anything());
});
