import { createHash, createPrivateKey, createPublicKey, generateKeyPairSync, sign, verify } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

export const UPDATE_APP_ID = 'local.inquiry.reception.workbench';
// Published clients pin this key. A release build must never silently rotate it.
export const RELEASE_PUBLIC_KEY_SHA256 = 'af96939c43f836d2f4d20e9d86985256513987e22903936dd5d9e7d7028ed078';
const payload = ({ version, artifact, sha512, size }) => ({ format: 1, app_id: UPDATE_APP_ID, version, artifact, sha512, size });
const bytes = value => Buffer.from(JSON.stringify(payload(value)), 'utf8');
const validFields = ({version, artifact, sha512, size}) => typeof version === 'string' && /^\d+\.\d+\.\d+$/.test(version) && typeof artifact === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]*\.exe$/i.test(artifact) && !artifact.includes('..') && typeof sha512 === 'string' && /^[A-Za-z0-9+/]{86}==$/.test(sha512) && Buffer.from(sha512, 'base64').length === 64 && Number.isSafeInteger(size) && size > 0;

export function signReleaseMetadata(fields, privateKey) {
  if (!validFields(fields)) throw new Error('Invalid release metadata');
  const key = createPrivateKey(privateKey);
  if (key.asymmetricKeyType !== 'ed25519') throw new Error('Release signing key must be Ed25519');
  return { format: 1, app_id: UPDATE_APP_ID, signature: sign(null, bytes(fields), key).toString('base64') };
}

export function verifyReleaseMetadata(metadata, publicKey) {
  try {
    if (!metadata || typeof metadata !== 'object' || Object.hasOwn(metadata, 'packages') || !Array.isArray(metadata.files) || metadata.files.length !== 1) return null;
    const file = metadata.files[0];
    if (!file || typeof file !== 'object') return null;
    const fields = { version: metadata.version, artifact: file.url, sha512: file.sha512, size: file.size };
    if (!validFields(fields) || metadata.path !== fields.artifact || metadata.sha512 !== fields.sha512) return null;
    const signature = metadata.reception_signature;
    if (!signature || signature.format !== 1 || signature.app_id !== UPDATE_APP_ID || typeof signature.signature !== 'string' || !/^[A-Za-z0-9+/]{86}==$/.test(signature.signature)) return null;
    const key = createPublicKey(publicKey);
    if (key.asymmetricKeyType !== 'ed25519' || !verify(null, bytes(fields), key, Buffer.from(signature.signature, 'base64'))) return null;
    return fields;
  } catch { return null; }
}

export async function loadOrCreateReleaseKeys(directory = process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'AlibabaReceptionWorkbenchReleaseKeys'), { allowCreate = true, expectedPublicKeyFingerprint } = {}) {
  if (!directory || !path.isAbsolute(directory)) throw new Error('An absolute LOCALAPPDATA release key directory is required');
  const privatePath = path.join(directory, 'private.pem');
  const publicPath = path.join(directory, 'public.pem');
  let created = false;
  if (allowCreate) {
    await mkdir(path.dirname(directory), { recursive: true });
    try { await mkdir(directory, { mode: 0o700 }); created = true; } catch (error) { if (error.code !== 'EEXIST') throw error; }
  }
  if (created) {
    const pair = generateKeyPairSync('ed25519');
    await writeFile(privatePath, pair.privateKey.export({ type: 'pkcs8', format: 'pem' }), { flag: 'wx', mode: 0o600 });
    await writeFile(publicPath, pair.publicKey.export({ type: 'spki', format: 'pem' }), { flag: 'wx', mode: 0o600 });
  }
  try {
    const privateKey = await readFile(privatePath, 'utf8');
    const publicKey = await readFile(publicPath, 'utf8');
    const priv = createPrivateKey(privateKey), pub = createPublicKey(publicKey);
    if (priv.asymmetricKeyType !== 'ed25519' || pub.asymmetricKeyType !== 'ed25519' || !createPublicKey(priv).export({ type: 'spki', format: 'der' }).equals(pub.export({ type: 'spki', format: 'der' }))) throw new Error('Key mismatch');
    if (expectedPublicKeyFingerprint !== undefined && createHash('sha256').update(publicKey).digest('hex') !== expectedPublicKeyFingerprint) throw new Error('Published key mismatch');
    return { privateKey, publicKey, directory };
  } catch { throw new Error(`Release key directory is incomplete or invalid: ${directory}`); }
}
