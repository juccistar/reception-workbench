import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ReceptionBridge } from '../src/shared/api';
import { OriginalImageDialog, copyImageMenu } from '../src/renderer/components/OriginalImageDialog';

const first = { kind: 'product' as const, local_product_id: 'p', sha256: 'a', version: 1 };
const second = { kind: 'product' as const, local_product_id: 'p', sha256: 'b', version: 1 };
afterEach(() => vi.restoreAllMocks());

it('discards late originals, revokes blob URLs, navigates and restores focus', async () => {
  let resolveFirst!: (value: any) => void;
  const pending = new Promise<any>(resolve => { resolveFirst = resolve; });
  const image = { bytes: new Uint8Array([1]), media_type: 'image/png', width: 1, height: 1, sha256: 'b' };
  const getImageOriginal = vi.fn().mockReturnValueOnce(pending).mockResolvedValueOnce(image);
  const cancelImageOriginal = vi.fn().mockResolvedValue(undefined);
  const bridge = { getImageOriginal, cancelImageOriginal } as unknown as ReceptionBridge;
  const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:second');
  const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
  const trigger = document.createElement('button'); document.body.appendChild(trigger); trigger.focus();
  const close = vi.fn();
  const view = render(<OriginalImageDialog bridge={bridge} images={[{ ref: first, label: '第一张' }, { ref: second, label: '第二张' }]} initial={0} onClose={close} />);
  fireEvent.keyDown(document, { key: 'ArrowRight' });
  expect(cancelImageOriginal).toHaveBeenCalledWith(getImageOriginal.mock.calls[0][1]);
  expect(getImageOriginal.mock.calls[1][1]).not.toBe(getImageOriginal.mock.calls[0][1]);
  await waitFor(() => expect(screen.getByAltText('第二张')).toHaveAttribute('src', 'blob:second'));
  resolveFirst(image);
  await Promise.resolve();
  expect(create).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(document, { key: 'Escape' }); expect(close).toHaveBeenCalledOnce();
  view.unmount(); expect(cancelImageOriginal).toHaveBeenCalledWith(getImageOriginal.mock.calls[1][1]); expect(revoke).toHaveBeenCalledWith('blob:second'); expect(trigger).toHaveFocus(); trigger.remove();
});

it('copies only through the fixed host menu and reports missing original', async () => {
  const feedback = vi.fn();
  copyImageMenu(undefined, first, feedback); expect(feedback).toHaveBeenCalledWith('原图不可用，无法复制图片。');
  const showImageMenu = vi.fn().mockResolvedValue('copied');
  copyImageMenu({ showImageMenu } as unknown as ReceptionBridge, second, feedback);
  await waitFor(() => expect(feedback).toHaveBeenCalledWith('图片已复制'));
  expect(showImageMenu).toHaveBeenCalledWith(second);
});
