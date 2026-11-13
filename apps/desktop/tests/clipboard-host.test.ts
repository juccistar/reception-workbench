// @vitest-environment node
import {describe, expect, it, vi} from 'vitest';
import {copyProductText, validateProductTextReference} from '../src/main/clipboard-host';

const ref = {case_id: 'case-1', field: 'product_id' as const};
const reply = <T>(field = 'product_id', text = '123456789') => Promise.resolve({field, text} as T);

describe('restricted product text clipboard host', () => {
  it('resolves the owned reference and waits for the native write to finish', async () => {
    let release!: () => void;
    const write = vi.fn(() => new Promise<void>(resolve => {release = resolve;}));
    const invoke = vi.fn(<T>() => reply<T>());
    let finished = false;
    const task = copyProductText(invoke, ref, () => {}, write).then(() => {finished = true;});
    await Promise.resolve(); await Promise.resolve();
    expect(write).toHaveBeenCalledWith('123456789');
    expect(finished).toBe(false);
    expect(invoke).toHaveBeenCalledWith('_copy_text_for_reference', {reference: ref});
    release(); await task; expect(finished).toBe(true);
  });
  it('accepts canonical links and custom product identifiers', async () => {
    const write = vi.fn(async () => {});
    await copyProductText(<T>() => reply<T>('product_url', 'https://www.alibaba.com/product-detail/_123.html'), {local_product_id: 'p', field: 'product_url'}, () => {}, write);
    await copyProductText(<T>() => reply<T>('product_id', '定制-1234'), ref, () => {}, write);
    expect(write).toHaveBeenLastCalledWith('定制-1234');
  });
  it('rejects arbitrary text, destinations, ambiguous references and extra properties', () => {
    for (const value of [{...ref, text: 'untrusted'}, {...ref, local_product_id: 'p'}, {field: 'product_id'}, {...ref, field: 'contact'}, {...ref, field: {toString: () => 'product_id'}}, {...ref, case_id: '\u0000'}, null]) {
      expect(() => validateProductTextReference(value)).toThrow();
    }
  });
  it('does not write invalid or mismatched backend results', async () => {
    const write = vi.fn(async () => {});
    for (const [field, text] of [['product_url', 'https://example.com/'], ['product_id', '123?'], ['product_url', 'https://www.alibaba.com/product-detail/_123.html?tracking=x']]) {
      await expect(copyProductText(<T>() => reply<T>(field, text), ref, () => {}, write)).rejects.toThrow();
    }
    expect(write).not.toHaveBeenCalled();
  });
  it('propagates rejected system writes and blocks writes after shutdown', async () => {
    await expect(copyProductText(<T>() => reply<T>(), ref, () => {}, async () => {throw new Error('native write failed');})).rejects.toThrow('native write failed');
    const write = vi.fn(async () => {});
    let calls = 0;
    await expect(copyProductText(<T>() => reply<T>(), ref, () => {if (++calls === 2) throw new Error('closing');}, write)).rejects.toThrow('closing');
    expect(write).not.toHaveBeenCalled();
  });
});
