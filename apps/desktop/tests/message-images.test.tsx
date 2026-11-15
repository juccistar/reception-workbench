import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MessageImages } from '../src/renderer/components/MessageImages';
import { App } from '../src/renderer/App';
import type { MessageImage, ReceptionBridge } from '../src/shared/api';
import { createReplyActivityFixture } from './fixtures/replyActivity';

const preview = 'data:image/png;base64,aGVsbG8=';
const photo = { kind: 'image', attachment_id: 'opaque-id', source_version: 'v1', availability: 'not_loaded', url: 'https://example.invalid/photo.png' };
const image = (status: MessageImage['status'], data_url?: string): MessageImage => ({ status, source_version: 'v1', message: '', data_url });

function setup(initial = image('not_loaded')) {
  const fixture = createReplyActivityFixture();
  let current = initial;
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  const bridge: ReceptionBridge = { ...fixture.bridge, async invoke<T>(method, params = {}) {
    if (method === 'get_message_image' || method === 'load_message_image') {
      calls.push({ method, params });
      if (method === 'load_message_image') { current = image('loading'); return { job_id: 'image-job' } as T; }
      return current as T;
    }
    return fixture.bridge.invoke<T>(method, params);
  } };
  return { ...fixture, bridge, imageCalls: calls, setImage: (value: MessageImage) => { current = value; } };
}

describe('消息图片', () => {
  it('自动排队并在相关事件后显示缩略图和可关闭的大图，保留普通附件', async () => {
    const fixture = setup();
    fixture.details.emma.messages[0].text = ' [image] ';
    fixture.details.emma.messages[0].attachments = [photo, { kind: 'file', name: '报价资料.pdf', url: 'https://example.invalid/file' }];
    render(<App bridge={fixture.bridge} />);
    expect(await screen.findByText('图片加载中…')).toBeInTheDocument();
    expect(fixture.imageCalls.filter(call => call.method === 'load_message_image')).toHaveLength(1);
    fixture.setImage(image('ready', preview));
    act(() => fixture.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    const thumbnail = await screen.findByRole('button', { name: '放大查看客户发送的照片' });
    const article = thumbnail.closest('article')!;
    expect(within(article).queryByText('[image]')).not.toBeInTheDocument();
    expect(within(article).getByText('图片已加载')).toBeInTheDocument();
    expect(within(article).getByText('报价资料.pdf')).toBeInTheDocument();
    expect(article.querySelector('a')).toBeNull();
    fireEvent.click(thumbnail);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(thumbnail).toHaveFocus();
  });

  it('显示商家图片和直接用于生成回复的提示，可放大并关闭', async () => {
    const fixture = setup({ ...image('ready', preview), input_mode: 'direct' });
    fixture.details.emma.messages[0].role = 'seller';
    fixture.details.emma.messages[0].attachments = [photo];
    render(<App bridge={fixture.bridge} />);
    const thumbnail = await screen.findByRole('button', { name: '放大查看商家发送的照片' });
    expect(screen.getByText('生成回复时结合此图片')).toBeInTheDocument();
    expect(screen.queryByText('客户发送的照片')).not.toBeInTheDocument();
    fireEvent.click(thumbnail);
    expect(screen.getByRole('dialog', { name: '商家发送的照片' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '关闭图片' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(thumbnail).toHaveFocus();
  });

  it('区分预览、识别中和待确认观察，失败后不保留旧观察', async () => {
    const fixture = setup({ ...image('ready', preview), vision_status: 'not_started' });
    fixture.details.emma.messages[0].attachments = [photo];
    render(<App bridge={fixture.bridge} />);
    expect(await screen.findByText('尚未识别')).toBeInTheDocument();
    fixture.setImage({ ...image('ready', preview), vision_status: 'processing' });
    act(() => fixture.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    expect(await screen.findByText('AI 正在识别照片…')).toBeInTheDocument();
    fixture.setImage({ ...image('ready', preview), vision_status: 'ready', vision_description: '绿色手提袋示意图。' });
    act(() => fixture.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    expect(await screen.findByText('图片观察 · 待客户确认')).toBeInTheDocument();
    expect(screen.getByText('绿色手提袋示意图。')).toBeInTheDocument();
    fixture.setImage({ ...image('ready', preview), vision_status: 'failed', vision_description: '绿色手提袋示意图。' });
    act(() => fixture.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    expect(await screen.findByText('暂未识别，可结合文字或人工确认')).toBeInTheDocument();
    expect(screen.queryByText('绿色手提袋示意图。')).not.toBeInTheDocument();
    expect(fixture.imageCalls.filter(call => call.method === 'load_message_image')).toHaveLength(0);
  });

  it('拒绝不安全预览并保留图片说明和普通消息占位符', async () => {
    const fixture = setup(image('ready', 'https://example.invalid/photo.png'));
    const original = fixture.details.emma.messages[0];
    original.text = '请看这张照片'; original.attachments = [photo];
    fixture.details.emma.messages.push({ ...original, message_id: 'plain', text: '[image]', attachments: [], timestamp_ms: original.timestamp_ms + 1 });
    render(<App bridge={fixture.bridge} />);
    expect(await screen.findByText('图片预览不可用')).toBeInTheDocument();
    expect(screen.getByText('请看这张照片')).toBeInTheDocument();
    expect(screen.getByText('[image]')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  it('版本不一致时不显示过期图片', async () => {
    const fixture = setup({ ...image('ready', preview), source_version: 'old' });
    fixture.details.emma.messages[0].attachments = [photo];
    render(<App bridge={fixture.bridge} />);
    expect(await screen.findByText('图片暂不可用')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
    expect(fixture.imageCalls.filter(call => call.method === 'load_message_image')).toHaveLength(0);
  });

  it('失败后只在用户请求时重试，商家图片使用正确称呼', async () => {
    const fixture = setup(image('failed'));
    const message = fixture.details.emma.messages[0];
    message.role = 'seller'; message.attachments = [photo];
    render(<App bridge={fixture.bridge} />);
    expect(await screen.findByText('商家发送的照片')).toBeInTheDocument();
    const button = await screen.findByRole('button', { name: '重试加载图片' });
    expect(fixture.imageCalls.filter(call => call.method === 'load_message_image')).toHaveLength(0);
    fireEvent.click(button);
    await waitFor(() => expect(fixture.imageCalls.find(call => call.method === 'load_message_image')?.params.retry).toBe(true));
  });

  it('同一消息有多张图片时按图片数量限制自动加载', async () => {
    const fixture = setup();
    const original = fixture.details.emma.messages[0];
    fixture.details.emma.messages = [
      { ...original, message_id: 'older', attachments: [photo, { ...photo, attachment_id: 'older-2' }] },
      { ...original, role: 'seller', message_id: 'newer', timestamp_ms: original.timestamp_ms + 1, attachments: [0, 1, 2].map(index => ({ ...photo, attachment_id: `newer-${index}` })) },
    ];
    render(<App bridge={fixture.bridge} />);
    await waitFor(() => expect(fixture.imageCalls.filter(call => call.method === 'load_message_image')).toHaveLength(4));
    expect(fixture.imageCalls.filter(call => call.method === 'load_message_image' && call.params.message_id === 'older')).toHaveLength(1);
    expect(fixture.imageCalls.filter(call => call.method === 'load_message_image' && call.params.message_id === 'newer')).toHaveLength(3);
  });

  it('切换会话后丢弃旧图片响应', async () => {
    const fixture = setup();
    fixture.details.emma.messages[0].attachments = [photo];
    let resolve!: (value: MessageImage) => void;
    const original = fixture.bridge.invoke.bind(fixture.bridge);
    fixture.bridge.invoke = <T,>(method: Parameters<ReceptionBridge['invoke']>[0], params = {}) => method === 'get_message_image' ? new Promise<T>(done => { resolve = done as (value: MessageImage) => void; }) : original<T>(method, params);
    render(<App bridge={fixture.bridge} />);
    await waitFor(() => expect(resolve).toBeDefined());
    fireEvent.click(screen.getByRole('button', { name: /Sofia Martin/ }));
    await screen.findByRole('heading', { name: 'Sofia Martin' });
    await act(async () => resolve(image('ready', preview)));
    expect(document.querySelector('img')).toBeNull();
  });

  it('只自动加载买卖双方合计最近四张图片', async () => {
    const fixture = setup();
    const original = fixture.details.emma.messages[0];
    fixture.details.emma.messages = Array.from({ length: 5 }, (_, index) => ({ ...original, role: index % 2 ? 'seller' : 'buyer', message_id: `photo-${index}`, timestamp_ms: original.timestamp_ms + index, attachments: [photo] }));
    render(<App bridge={fixture.bridge} />);
    await waitFor(() => expect(fixture.imageCalls.filter(call => call.method === 'load_message_image')).toHaveLength(4));
    expect(fixture.imageCalls.filter(call => call.method === 'load_message_image').map(call => call.params.message_id).sort()).toEqual(['photo-1', 'photo-2', 'photo-3', 'photo-4']);
  });
});

it('按原图资产去重，并以真实引用在同一消息的图片间导航', async () => {
  const refs = ['a', 'b', 'a'].map((sha256, index) => ({ kind: 'message' as const, conversation_id: 'c', message_id: 'm', source_version: 'v' + index, sha256 }));
  const getImageOriginal = vi.fn().mockRejectedValue(new Error('synthetic'));
  const bridge = { invoke: vi.fn(async (_method, params) => { const index = Number(String(params.source_version).slice(1)); return { status: 'ready', source_version: 'v' + index, message: '', data_url: preview, image_ref: refs[index] }; }), getImageOriginal } as unknown as ReceptionBridge;
  render(<MessageImages attachments={refs.map((ref, index) => ({ kind: 'image', attachment_id: String(index), source_version: ref.source_version }))} conversationId="c" messageId="m" role="buyer" autoLoadIndices={[]} revision={0} bridge={bridge} />);
  const cards = await screen.findAllByRole('button', { name: '放大查看客户发送的照片' });
  await waitFor(() => expect(cards).toHaveLength(3));
  fireEvent.click(cards[2]);
  expect(await screen.findByText('1 / 2')).toBeInTheDocument();
  expect(getImageOriginal).toHaveBeenCalledWith(refs[0], expect.any(String));
  fireEvent.keyDown(document, { key: 'ArrowRight' });
  await waitFor(() => expect(getImageOriginal).toHaveBeenCalledWith(refs[1], expect.any(String)));
  expect(screen.getByText('2 / 2')).toBeInTheDocument();
});
