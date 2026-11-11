import test from 'node:test';
import assert from 'node:assert/strict';
import { parseUpdateSource, updateProvider, updateSourceFromConfig, isUpdateRequestAllowed } from '../update-source.mjs';

const repository = 'https://github.com/juccistar/reception-workbench-releases/';

test('GitHub repository and Releases page resolve to the same public provider', () => {
  const expected = { kind: 'github', owner: 'juccistar', repo: 'reception-workbench-releases', url: repository };
  for (const url of [repository, repository.slice(0, -1), repository + 'releases', repository + 'releases/']) {
    assert.deepEqual(parseUpdateSource(url), expected);
  }
  const provider = updateProvider(expected);
  assert.deepEqual(provider, { provider: 'github', owner: 'juccistar', repo: 'reception-workbench-releases', private: false });
  assert.equal(updateSourceFromConfig(provider), repository);
});

test('update sources reject credentials, tokens, invalid repositories and download URLs', () => {
  for (const value of [undefined, null, {}, 'http://github.com/a/b', 'https://user:secret@github.com/a/b',
    'https://@github.com/a/b', repository + '?token=secret', repository + '#x', repository + '?',
    'https://github.com/a', 'https://github.com/a/b/releases/latest', 'https://github.com/a/b.git',
    'https://github.com/a/b/tree/main', 'https://github.com:8443/a/b', 'https://github.com/a/%62',
    'https://github.com/a\\b', 'https://github.com/a/b/releases/download/v1.0.0/setup.exe']) {
    assert.throws(() => parseUpdateSource(value), String(value));
  }
  for (const config of [{ provider: 'github', owner: 'a', repo: 'b', private: true },
    { provider: 'github', owner: 'a', repo: 'b', token: 'do-not-persist' },
    { provider: 'github', owner: 'a/b', repo: 'c' }]) assert.throws(() => updateSourceFromConfig(config));
});

test('GitHub allows only its repository release paths and explicit HTTPS asset origins', () => {
  const source = parseUpdateSource(repository);
  for (const url of [repository + 'releases.atom', repository + 'releases/latest',
    repository + 'releases/download/v0.2.17/ReceptionWorkbench-Setup-0.2.17.exe',
    'https://release-assets.githubusercontent.com/github-production-release-asset/fixture?sig=synthetic',
    'https://objects.githubusercontent.com/github-production-release-asset/fixture',
    'https://github-releases.githubusercontent.com/fixture']) assert.equal(isUpdateRequestAllowed(source, url), true, url);
  for (const url of ['http://github.com/juccistar/reception-workbench-releases/releases',
    'http://release-assets.githubusercontent.com/file', 'https://github.com/other/repo/releases.atom',
    repository + 'releases-evil/file', 'https://github.com/login', 'https://github.com.evil.example/file',
    'https://evil.githubusercontent.com/file', 'https://release-assets.githubusercontent.com.evil.example/file',
    'https://release-assets.githubusercontent.com:444/file', 'https://user:secret@release-assets.githubusercontent.com/file',
    'https://@release-assets.githubusercontent.com/file', 'file:///tmp/setup.exe']) {
    assert.equal(isUpdateRequestAllowed(source, url), false, url);
  }
});

test('static HTTPS sources retain the same-origin rule and can be disabled', () => {
  const source = parseUpdateSource('https://EXAMPLE.com:443/releases');
  assert.deepEqual(source, { kind: 'generic', url: 'https://example.com/releases/' });
  assert.deepEqual(updateProvider(source), { provider: 'generic', url: source.url });
  assert.equal(updateSourceFromConfig(updateProvider(source)), source.url);
  assert.equal(isUpdateRequestAllowed(source, 'https://example.com/files/setup.exe?cache=1'), true);
  assert.equal(isUpdateRequestAllowed(source, 'https://release-assets.githubusercontent.com/file'), false);
  assert.equal(isUpdateRequestAllowed(source, 'http://example.com/setup.exe'), false);
  const disabled = parseUpdateSource('  ');
  assert.deepEqual(disabled, { kind: 'disabled', url: '' });
  assert.equal(isUpdateRequestAllowed(disabled, 'https://github.com/a/b'), false);
  assert.throws(() => updateProvider(disabled));
});
