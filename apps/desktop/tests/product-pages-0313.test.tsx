import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ProductsPage, SuppliersPage, ProcurementPage } from '../src/renderer/components/ProductProcurementPages';
import { ProductMainImage } from '../src/renderer/components/ProductMainImage';
import { messageDateTime } from '../src/renderer/components/ui';

it('formats complete local message times and rejects missing or invalid values', () => {
  expect(messageDateTime(new Date(2026, 0, 2, 3, 4, 5).getTime())).toBe('2026-01-02 03:04:05');
  expect(messageDateTime(null)).toBe('时间未知');
  expect(messageDateTime(Number.NaN)).toBe('时间未知');
  expect(messageDateTime(0)).toBe('时间未知');
});

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
  fireEvent.click(screen.getByText('查看商品资料和完整规格'));
  expect(await screen.findByRole('button', { name: '放大查看商品图片 1' })).toBeInTheDocument();
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

it('copies the product library ID and link through the host', async () => {
  const invoke = setup(); const original = invoke.getMockImplementation()!;
  invoke.mockImplementation(async (method: string, params: unknown = {}) => method === 'get_product' ? { product: { ...product, product_url: 'https://example.com/product' }, offers: [], suppliers: [], tasks: [] } : original(method, params));
  const copyProductText = vi.fn(async () => {});
  Object.assign(window.reception!, { copyProductText });
  render(<ProductsPage initialProductId="p1" />);
  fireEvent.click(await screen.findByText('查看商品资料和完整规格'));
  fireEvent.click(await screen.findByRole('button', { name: '复制产品ID' }));
  await screen.findByText('产品ID已复制');
  fireEvent.click(screen.getByRole('button', { name: 'https://example.com/product' }));
  await screen.findByText('产品链接已复制');
  expect(copyProductText).toHaveBeenCalledWith({ field: 'product_id', local_product_id: 'p1' });
  expect(copyProductText).toHaveBeenCalledWith({ field: 'product_url', local_product_id: 'p1' });
});

it('does not show a late product image after switching products', async () => {
  let resolveOld!: (value: unknown) => void;
  const invoke = vi.fn((method: string, params: Record<string, unknown>) => params.local_product_id === 'old' ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve({ status: 'ready', data_url: 'data:image/png;base64,BBBB' }));
  const view = render(<ProductMainImage localProductId="old" metadata={{ status: 'loading' }} invoke={invoke as never} />);
  view.rerender(<ProductMainImage localProductId="new" metadata={{ status: 'ready' }} invoke={invoke as never} />);
  const image = await screen.findByRole('img', { name: '商品图片 1' });
  expect(image).toHaveAttribute('src', 'data:image/png;base64,BBBB');
  await act(async () => resolveOld({ status: 'ready', data_url: 'data:image/png;base64,AAAA' }));
  expect(screen.getByRole('img', { name: '商品图片 1' })).toHaveAttribute('src', 'data:image/png;base64,BBBB');
});

it('rejects a remote URL returned by the local image RPC', async () => {
  const invoke = vi.fn(async () => ({ status: 'ready', data_url: 'https://example.com/image.png' }));
  render(<ProductMainImage localProductId="p1" metadata={{ status: 'ready' }} invoke={invoke as never} />);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('get_product_main_image', { local_product_id: 'p1' }));
  expect(screen.getByText('暂无商品图片')).toBeInTheDocument();
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
});

it('clears the old gallery and dialog for a case image refresh and shows the current failed result', async () => {
  const invoke = vi.fn(async (method: string) => method === 'request_product_images' ? { job_id: 'job1' } : { status: 'ready', data_url: 'data:image/png;base64,AAAA' });
  const view = render(<ProductMainImage caseId="case1" localProductId="p1" metadata={{ status: 'ready', updated_at: 1 }} invoke={invoke as never} />);
  fireEvent.click(await screen.findByRole('button', { name: '放大查看商品图片 1' }));
  expect(screen.getByRole('dialog', { name: '商品图片' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '重新采集' }));
  expect(screen.queryByRole('dialog', { name: '商品图片' })).not.toBeInTheDocument();
  expect(screen.queryByRole('img', { name: '商品图片 1' })).not.toBeInTheDocument();
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('request_product_images', { case_id: 'case1', client_request_id: expect.any(String) }));
  invoke.mockImplementation(async () => ({ status: 'failed' }));
  view.rerender(<ProductMainImage caseId="case1" localProductId="p1" metadata={{ status: 'failed', updated_at: 2, job_id: 'job1' }} invoke={invoke as never} />);
  expect(await screen.findByText('商品图片读取失败')).toBeInTheDocument();
  expect(screen.queryByRole('img', { name: '商品图片 1' })).not.toBeInTheDocument();
});

it('allows explicit recovery of a persisted loading image record', async () => {
  const invoke = vi.fn(async (method: string) => method === 'request_product_images' ? { job_id: 'recovery' } : { status: 'loading' });
  render(<ProductMainImage caseId="case1" localProductId="p1" metadata={{ status: 'loading', job_id: 'old-job' }} invoke={invoke as never} />);
  const retry = screen.getByRole('button', { name: '读取图片' });
  expect(retry).toBeEnabled();
  fireEvent.click(retry);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('request_product_images', { case_id: 'case1', client_request_id: expect.any(String) }));
});

it('requests an unbound case without a product reference', async () => {
  const invoke = vi.fn(async () => ({ job_id: 'job1' }));
  render(<ProductMainImage caseId="case1" invoke={invoke as never} />);
  fireEvent.click(screen.getByRole('button', { name: '读取图片' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('request_product_images', { case_id: 'case1', client_request_id: expect.any(String) }));
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
  fireEvent.change(screen.getByLabelText('平台'), { target: { value: 'alibaba' } });
  fireEvent.click(screen.getByRole('button', { name: '保存供应商' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('供应商保存失败');
  expect(screen.getByLabelText('名称')).toHaveValue('新供应商');
  expect(screen.getByLabelText('平台')).toHaveValue('alibaba');
  expect(invoke).toHaveBeenCalledWith('save_supplier', expect.objectContaining({ platform: 'alibaba' }));
});

it('filters the compact product list without inventing customer associations', async () => {
  setup(); render(<ProductsPage />);
  const list = await screen.findByRole('region', { name: '产品目录' });
  expect(within(list).getByText('棉帽')).toBeInTheDocument();
  expect(within(list).getByText('关联客户')).toBeInTheDocument();
  expect(within(list).getAllByText('—').length).toBeGreaterThan(0);
  fireEvent.change(screen.getByLabelText('搜索产品'), { target: { value: '找不到' } });
  expect(within(list).getByText('没有匹配的产品')).toBeInTheDocument();
});

it('shows customer counts and readable platforms while preserving supplier enum values', async () => {
  const invoke = setup(); const original = invoke.getMockImplementation()!;
  invoke.mockImplementation(async (method: string, params: unknown = {}) => method === 'list_products'
    ? { items: [{ ...product, platform: 'alibaba', customer_count: 2 }, { ...product, local_product_id: 'p2', name: '定制产品', platform: 'custom', customer_count: 0 }] }
    : original(method, params));
  const view = render(<ProductsPage />);
  const list = await screen.findByRole('region', { name: '产品目录' });
  expect(await within(list).findByText('国际站 · 123')).toBeInTheDocument();
  expect(within(list).getByText('本地定制 · 123')).toBeInTheDocument();
  expect(within(list).getByText('2')).toBeInTheDocument();
  expect(within(list).getByText('0')).toBeInTheDocument();
  view.unmount();
  render(<SuppliersPage />);
  fireEvent.click(screen.getByRole('button', { name: '新增供应商' }));
  const select = screen.getByLabelText('平台');
  expect(within(select).getByRole('option', { name: '国际站' })).toHaveValue('alibaba');
  expect(within(select).getByRole('option', { name: '1688' })).toHaveValue('1688');
  expect(within(select).getByRole('option', { name: 'AliExpress' })).toHaveValue('aliexpress');
  expect(within(select).getByRole('option', { name: '其他' })).toHaveValue('other');
});

it('uses projected product specifications and offer counts without inventing supplier totals', async () => {
  const invoke = setup(); const original = invoke.getMockImplementation()!;
  invoke.mockImplementation(async (method: string, params: unknown = {}) => method === 'list_products'
    ? { items: [{ ...product, specifications_summary: '蓝色 / 500ml', offer_count: 3, customer_count: 2 }] }
    : original(method, params));
  render(<ProductsPage />);
  const list = await screen.findByRole('region', { name: '产品目录' });
  expect(await within(list).findByText('蓝色 / 500ml')).toBeInTheDocument();
  expect(within(list).getByText('3 条供货方案')).toBeInTheDocument();
  expect(within(list).getByText('2')).toBeInTheDocument();
});

it('retains the complete long specification in expandable product details', async () => {
  const invoke = setup(); const original = invoke.getMockImplementation()!;
  const specification = '完整规格内容'.repeat(500);
  invoke.mockImplementation(async (method: string, params: unknown = {}) => method === 'list_products'
    ? { items: [{ ...product, specifications_summary: specification }] }
    : method === 'get_product' ? { product: { ...product, specifications_summary: specification }, offers: [offer], suppliers: [supplier], tasks: [], customers: [] }
    : original(method, params));
  render(<ProductsPage initialProductId="p1" />);
  const summary = await screen.findByText('查看商品资料和完整规格');
  const details = summary.closest('details')!;
  expect(details).not.toHaveAttribute('open');
  fireEvent.click(summary);
  expect(within(details).getByText(specification)).toBeVisible();
  expect(screen.getByRole('heading', { name: '供货方案' })).toBeVisible();
  expect(screen.getByRole('heading', { name: '关联客户' })).toBeVisible();
  expect(screen.getByText('暂无关联客户')).toBeVisible();
});

it('keeps inquiry unavailable and reconciles a selected unknown procurement task', async () => {
  const invoke = setup(); render(<ProcurementPage />);
  fireEvent.click(screen.getByText('找货设置'));
  expect(await screen.findByLabelText('供应商上限')).toHaveValue('3');
  expect(screen.getByRole('switch', { name: '自动找货' })).toHaveAttribute('aria-checked', 'false');
  expect(screen.queryByLabelText('自动询价')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '关闭自动找货' }));
  fireEvent.click(await screen.findByRole('button', { name: /123.*结果未知/ }));
  fireEvent.click(screen.getByRole('button', { name: '核查结果' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('reconcile_procurement', { task_id: 't1' }));
  expect(invoke).not.toHaveBeenCalledWith('request_procurement', expect.anything());
});

it('saves the sourcing switch and requires saved settings before authorization', async () => {
  const invoke = setup(); render(<ProcurementPage />);
  fireEvent.click(screen.getByRole('button', { name: '找货设置' }));
  const toggle = await screen.findByRole('switch', { name: '自动找货' });
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-checked', 'true');
  expect(screen.getByRole('button', { name: '本次授权' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('供应商上限'), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('save_procurement_settings', expect.objectContaining({ auto_find: true, supplier_limit: 2 })));
});

it('opens the associated customer using the projected conversation id', async () => {
  const invoke = setup(); const original = invoke.getMockImplementation()!;
  invoke.mockImplementation(async (method: string, params: unknown = {}) => method === 'get_product'
    ? { product, offers: [], suppliers: [], tasks: [], customers: [{ buyer_id: 'buyer1', customer_name: 'Emma', conversation_id: 'conversation1', case_id: 'case1', case_ids: ['case1'] }] }
    : original(method, params));
  const open = vi.fn(); render(<ProductsPage initialProductId="p1" onOpenConversation={open} />);
  expect(await screen.findByText('Emma')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '查看会话' }));
  expect(open).toHaveBeenCalledWith('conversation1');
});

it('saves the supplier region with the existing revision', async () => {
  const invoke = setup(); render(<SuppliersPage />);
  fireEvent.click(await screen.findByRole('button', { name: /供应商甲/ }));
  fireEvent.click(await screen.findByRole('button', { name: '编辑资料' }));
  fireEvent.change(screen.getByLabelText('平台'), { target: { value: 'alibaba' } });
  fireEvent.change(screen.getByLabelText('地区'), { target: { value: '浙江' } });
  fireEvent.click(screen.getByRole('button', { name: '保存供应商' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('save_supplier', expect.objectContaining({ supplier_id: 's1', expected_revision: 2, region: '浙江' })));
});
