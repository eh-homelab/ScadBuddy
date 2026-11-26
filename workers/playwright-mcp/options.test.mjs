// The wrapper's header and flag parsing (#7027). `node --test`; CI runs it inside the
// image (ci.yml `agent` job), so it imports nothing but options.mjs and node:*, and reads
// browser-vectors.json beside it (the Dockerfile copies both).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  HEADER,
  HEIGHT,
  LOCALE_MAX,
  LOCALE_PATTERN,
  UA_PATTERN,
  WIDTH,
  contextOptionsFor,
  isClientActivity,
  isResponse,
  parseArgs,
  parseBrowserHeader,
} from './options.mjs';

const BASE = { userAgent: 'DEFAULT', viewport: { width: 1280, height: 720 } };

function parsed(value) {
  return parseBrowserHeader(typeof value === 'string' ? value : JSON.stringify(value));
}

test('the header name is lower case, as node reports it', () => {
  assert.equal(HEADER, 'x-scadbuddy-browser');
});

test('header user agent reaches the context options', () => {
  const { options } = parseBrowserHeader('{"user_agent":"Mobile UA"}');
  assert.equal(contextOptionsFor({ userAgent: 'DEFAULT' }, options).userAgent, 'Mobile UA');
});

test('viewport and locale map, and the base keeps what the header leaves out', () => {
  const { options, warning } = parsed({ viewport: { width: 390, height: 844 }, locale: 'en-GB' });
  assert.equal(warning, undefined);
  assert.deepEqual(contextOptionsFor(BASE, options), {
    userAgent: 'DEFAULT',
    viewport: { width: 390, height: 844 },
    locale: 'en-GB',
  });
});

test('an absent header keeps the base', () => {
  const result = parseBrowserHeader(undefined);
  assert.deepEqual(result, { options: undefined });
  assert.deepEqual(contextOptionsFor(BASE, result.options), BASE);
});

test('an empty object is valid and keeps the base', () => {
  const { options, warning } = parsed({});
  assert.equal(warning, undefined);
  assert.deepEqual(contextOptionsFor(BASE, options), BASE);
});

test('contextOptionsFor does not change the base', () => {
  const base = { userAgent: 'DEFAULT' };
  contextOptionsFor(base, { user_agent: 'X', locale: 'de' });
  assert.deepEqual(base, { userAgent: 'DEFAULT' });
});

const valid = {
  'a user agent of 512 characters': { user_agent: 'a'.repeat(512) },
  'width 320': { viewport: { width: 320, height: 600 } },
  'width 3840': { viewport: { width: 3840, height: 600 } },
  'height 320': { viewport: { width: 800, height: 320 } },
  'height 2160': { viewport: { width: 800, height: 2160 } },
  'locale zh-Hant-TW': { locale: 'zh-Hant-TW' },
  'every key': { user_agent: 'Mozilla/5.0 (X11)', viewport: { width: 390, height: 844 }, locale: 'en' },
};

for (const [name, value] of Object.entries(valid)) {
  test(`valid: ${name}`, () => {
    const result = parsed(value);
    assert.equal(result.warning, undefined);
    assert.deepEqual(result.options, value);
  });
}

const invalid = {
  'an empty user agent': { user_agent: '' },
  'a user agent of 513 characters': { user_agent: 'x'.repeat(513) },
  'a non-ASCII user agent': { user_agent: 'é' },
  'a user agent with a newline': { user_agent: 'a\nb' },
  'a user agent that is not a string': { user_agent: 5 },
  'a null user agent': { user_agent: null },
  'width 319': { viewport: { width: 319, height: 600 } },
  'width 3841': { viewport: { width: 3841, height: 600 } },
  'height 319': { viewport: { width: 800, height: 319 } },
  'height 2161': { viewport: { width: 800, height: 2161 } },
  'width 390.5': { viewport: { width: 390.5, height: 844 } },
  'a string width': { viewport: { width: '390', height: 844 } },
  'a viewport without height': { viewport: { width: 390 } },
  'a viewport with another key': { viewport: { width: 390, height: 844, deviceScaleFactor: 3 } },
  'a viewport that is not an object': { viewport: '390x844' },
  'a viewport that is an array': { viewport: [390, 844] },
  'locale en_GB': { locale: 'en_GB' },
  'locale e': { locale: 'e' },
  'a locale of 36 characters': { locale: 'a'.repeat(36) },
  'locale en--GB': { locale: 'en--GB' },
  'an unknown key': { proxy: 'x' },
  'a JSON array': [],
  'a JSON string': 'just text',
  'JSON null': null,
};

for (const [name, value] of Object.entries(invalid)) {
  test(`invalid: ${name}`, () => {
    const result = parseBrowserHeader(JSON.stringify(value));
    assert.equal(result.options, undefined);
    assert.equal(typeof result.warning, 'string');
  });
}

test('invalid: a value that is not JSON', () => {
  const result = parseBrowserHeader('{user_agent: nope');
  assert.equal(result.options, undefined);
  assert.equal(typeof result.warning, 'string');
});

test('invalid: a header over 2048 bytes', () => {
  // Valid JSON with a valid user agent, padded with spaces past the limit.
  const value = JSON.stringify({ user_agent: 'a' }) + ' '.repeat(2048);
  const result = parseBrowserHeader(value);
  assert.equal(result.options, undefined);
  assert.equal(typeof result.warning, 'string');
});

test('invalid: a repeated header', () => {
  const result = parseBrowserHeader(['{"locale":"en"}', '{"locale":"de"}']);
  assert.equal(result.options, undefined);
  assert.equal(typeof result.warning, 'string');
});

test('a warning never holds the header value', () => {
  const secret = 'SECRET-UA-é';
  const result = parseBrowserHeader(JSON.stringify({ user_agent: secret }));
  assert.ok(!result.warning.includes(secret));
  assert.ok(!result.warning.includes('SECRET'));
});

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36';

test("parseArgs reads the Deployment's args", () => {
  const options = parseArgs([
    '--port', '8931', '--host', '0.0.0.0', '--isolated', '--allowed-hosts', 'a:1,b:2',
    '--output-dir', '/tmp/output', '--user-agent', UA,
  ]);
  assert.equal(options.port, 8931);
  assert.equal(options.host, '0.0.0.0');
  assert.deepEqual(options.allowedHosts, ['a:1', 'b:2']);
  assert.equal(options.outputDir, '/tmp/output');
  assert.equal(options.userAgent, UA);
  assert.equal(options.viewport, undefined);
  assert.equal(options.idleTimeout, 3_600_000);
});

test('parseArgs reads --viewport-size, --idle-timeout and the = form', () => {
  const options = parseArgs(['--port=9000', '--viewport-size', '1280x720', '--idle-timeout', '60000']);
  assert.equal(options.port, 9000);
  assert.deepEqual(options.viewport, { width: 1280, height: 720 });
  assert.equal(options.idleTimeout, 60000);
});

test('parseArgs lower-cases the allowed hosts', () => {
  assert.deepEqual(parseArgs(['--allowed-hosts', 'Agent.Example:8931']).allowedHosts, ['agent.example:8931']);
});

test('parseArgs throws on a flag it does not know', () => {
  assert.throws(() => parseArgs(['--caps', 'vision']), /--caps/);
});

test('parseArgs throws on a flag with no value, a bad port or a bad size', () => {
  assert.throws(() => parseArgs(['--port']), /--port/);
  assert.throws(() => parseArgs(['--port', 'abc']), /--port/);
  assert.throws(() => parseArgs(['--viewport-size', '1280']), /--viewport-size/);
  assert.throws(() => parseArgs(['--idle-timeout', '-1']), /--idle-timeout/);
  assert.throws(() => parseArgs(['stray']), /stray/);
});

test('a request or notification from the client is activity; an answer to a ping is not', () => {
  assert.equal(isClientActivity({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: {} }), true);
  assert.equal(isClientActivity({ jsonrpc: '2.0', method: 'notifications/initialized' }), true);
  assert.equal(isClientActivity({ jsonrpc: '2.0', id: 0, result: {} }), false);
  assert.equal(isClientActivity({ jsonrpc: '2.0', id: 0, error: { code: -1, message: 'x' } }), false);
  assert.equal(isClientActivity(undefined), false);
});

test('a response the server sends answers a request; its ping and notifications do not', () => {
  assert.equal(isResponse({ jsonrpc: '2.0', id: 3, result: { content: [] } }), true);
  assert.equal(isResponse({ jsonrpc: '2.0', id: 3, error: { code: -1, message: 'x' } }), true);
  assert.equal(isResponse({ jsonrpc: '2.0', id: 0, method: 'ping' }), false);
  assert.equal(isResponse({ jsonrpc: '2.0', method: 'notifications/tools/list_changed' }), false);
});

// The bounds the API holds too (the Go tests in tools/go/core reads the same file).
const vectors = JSON.parse(readFileSync(new URL('./browser-vectors.json', import.meta.url), 'utf8'));

test("the bounds are the shared vectors' (the API's)", () => {
  assert.deepEqual(
    {
      user_agent_pattern: UA_PATTERN.source,
      locale_pattern: LOCALE_PATTERN.source,
      locale_max: LOCALE_MAX,
      viewport_width: WIDTH,
      viewport_height: HEIGHT,
    },
    vectors.bounds,
  );
});

for (const { name, value } of vectors.valid) {
  test(`shared vector, valid: ${name}`, () => {
    const result = parsed(value);
    assert.equal(result.warning, undefined);
    assert.deepEqual(result.options, value);
  });
}

for (const { name, value } of vectors.invalid) {
  test(`shared vector, invalid: ${name}`, () => {
    const result = parsed(value);
    assert.equal(result.options, undefined);
    assert.equal(typeof result.warning, 'string');
  });
}
