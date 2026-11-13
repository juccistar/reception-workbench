import type { ProductTextReference } from '../shared/media';

type Invoke = <T>(method: string, params: Record<string, unknown>) => Promise<T>;

export function validateProductTextReference(value: unknown): asserts value is ProductTextReference {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('复制引用无效。');
  const ref = value as Record<string, unknown>;
  const targets = ['case_id', 'local_product_id'].filter(key => Object.hasOwn(ref, key));
  if (Object.keys(ref).length !== 2 || targets.length !== 1 ||
      typeof ref.field !== 'string' || !['product_id', 'product_url'].includes(ref.field) ||
      typeof ref[targets[0]] !== 'string' || !(ref[targets[0]] as string).length ||
      (ref[targets[0]] as string).length > 500 || /[\u0000-\u001f]/.test(ref[targets[0]] as string)) {
    throw new Error('复制引用无效。');
  }
}

export async function copyProductText(invoke: Invoke, value: unknown, ready: () => void, writeText: (text: string) => Promise<void>): Promise<void> {
  validateProductTextReference(value);
  ready();
  const result = await invoke<{field: string; text: string}>('_copy_text_for_reference', {reference: value});
  ready();
  if (!result || result.field !== value.field || typeof result.text !== 'string' ||
      (value.field === 'product_id'
        ? !/^(?:[1-9][0-9]{0,24}|定制-[0-9]{1,24})$/.test(result.text)
        : !/^https:\/\/www\.alibaba\.com\/product-detail\/_[1-9][0-9]{0,24}\.html$/.test(result.text))) {
    throw new Error('复制内容无效或来源已变化。');
  }
  await writeText(result.text);
}
