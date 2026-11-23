// The wrapper's pure parts (#7027): the X-ScadBuddy-Browser header and the server's flags.
// No Playwright import, so `node --test options.test.mjs` runs anywhere.
//
// The bounds are R-7027-2's, the same as the API's `SessionBrowser` and `Viewport`
// (backend/scadbuddy/api/crud_agent_sessions.py, BROWSER_UA_PATTERN, BROWSER_LOCALE_PATTERN,
// BROWSER_LOCALE_MAX, VIEWPORT_WIDTH, VIEWPORT_HEIGHT). Change both together. The API
// already refused anything outside them, so a value that fails here is a bug or a
// forged header: the session gets the server's defaults, and the warning says why
// without the value.

export const HEADER = 'x-scadbuddy-browser';

const HEADER_BYTES_MAX = 2048;
const UA_PATTERN = /^[\x20-\x7E]{1,512}$/;
const LOCALE_PATTERN = /^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$/;
const LOCALE_MAX = 35;
const WIDTH = [320, 3840];
const HEIGHT = [320, 2160];
const KEYS = new Set(['user_agent', 'viewport', 'locale']);
const VIEWPORT_KEYS = new Set(['width', 'height']);

function isObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function inRange(value, [min, max]) {
  return Number.isInteger(value) && value >= min && value <= max;
}

// The reason a parsed value is refused, or undefined when it is valid. Never the value.
function problem(value) {
  if (!isObject(value)) return 'is not a JSON object';
  for (const key of Object.keys(value)) {
    if (!KEYS.has(key)) return 'has a key other than user_agent, viewport and locale';
  }
  if ('user_agent' in value) {
    if (typeof value.user_agent !== 'string' || !UA_PATTERN.test(value.user_agent)) {
      return 'has a user_agent that is not 1 to 512 printable ASCII characters';
    }
  }
  if ('viewport' in value) {
    const viewport = value.viewport;
    if (!isObject(viewport) || Object.keys(viewport).some((key) => !VIEWPORT_KEYS.has(key))) {
      return 'has a viewport that is not an object of width and height';
    }
    if (!inRange(viewport.width, WIDTH)) return `has a viewport width that is not an integer ${WIDTH[0]}..${WIDTH[1]}`;
    if (!inRange(viewport.height, HEIGHT)) return `has a viewport height that is not an integer ${HEIGHT[0]}..${HEIGHT[1]}`;
  }
  if ('locale' in value) {
    if (typeof value.locale !== 'string' || value.locale.length > LOCALE_MAX || !LOCALE_PATTERN.test(value.locale)) {
      return 'has a locale that is not a BCP 47 tag of at most 35 characters';
    }
  }
  return undefined;
}

/**
 * The browser options of the header's value (node gives a repeated header as an array).
 * Absent: `{options: undefined}`. Invalid: `{options: undefined, warning}`.
 */
export function parseBrowserHeader(value) {
  if (value === undefined) return { options: undefined };
  const refuse = (why) => ({ options: undefined, warning: `ignored the ${HEADER} header: it ${why}` });
  if (Array.isArray(value)) return refuse('was sent more than once');
  if (Buffer.byteLength(value, 'utf8') > HEADER_BYTES_MAX) return refuse(`is over ${HEADER_BYTES_MAX} bytes`);
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    return refuse('is not JSON');
  }
  const why = problem(parsed);
  if (why) return refuse(why);
  return { options: parsed };
}

/** The server's context options with the keys `options` sets put over them. */
export function contextOptionsFor(base, options) {
  const merged = { ...base };
  if (options?.user_agent !== undefined) merged.userAgent = options.user_agent;
  if (options?.viewport !== undefined) merged.viewport = { width: options.viewport.width, height: options.viewport.height };
  if (options?.locale !== undefined) merged.locale = options.locale;
  return merged;
}

const DEFAULT_IDLE_TIMEOUT = 3_600_000;

function nonNegativeInteger(flag, text) {
  if (!/^\d+$/.test(text)) throw new Error(`${flag} wants a whole number, got "${text}"`);
  return Number(text);
}

/**
 * The flags the CLI's Deployment passes (#7027 keeps its args): --port, --host,
 * --allowed-hosts (comma list), --isolated (accepted: every session is isolated),
 * --output-dir, --user-agent, --viewport-size WxH, --idle-timeout ms. Anything else throws.
 */
export function parseArgs(argv) {
  const options = {
    port: 8931,
    host: 'localhost',
    allowedHosts: undefined,
    outputDir: undefined,
    userAgent: undefined,
    viewport: undefined,
    idleTimeout: DEFAULT_IDLE_TIMEOUT,
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (!arg.startsWith('--')) throw new Error(`unexpected argument "${arg}"`);
    const eq = arg.indexOf('=');
    const flag = eq === -1 ? arg : arg.slice(0, eq);
    if (flag === '--isolated') {
      if (eq !== -1) throw new Error('--isolated takes no value');
      continue;
    }
    const value = () => {
      if (eq !== -1) return arg.slice(eq + 1);
      if (i + 1 >= argv.length || argv[i + 1].startsWith('--')) throw new Error(`${flag} needs a value`);
      return argv[++i];
    };
    switch (flag) {
      case '--port': {
        const port = nonNegativeInteger(flag, value());
        if (port < 1 || port > 65535) throw new Error(`--port must be 1..65535, got ${port}`);
        options.port = port;
        break;
      }
      case '--host':
        options.host = value();
        break;
      case '--allowed-hosts':
        options.allowedHosts = value().split(',').map((h) => h.trim().toLowerCase()).filter(Boolean);
        break;
      case '--output-dir':
        options.outputDir = value();
        break;
      case '--user-agent':
        options.userAgent = value();
        break;
      case '--viewport-size': {
        const size = value();
        const match = /^(\d+)x(\d+)$/.exec(size);
        if (!match) throw new Error(`--viewport-size wants WIDTHxHEIGHT, got "${size}"`);
        options.viewport = { width: Number(match[1]), height: Number(match[2]) };
        break;
      }
      case '--idle-timeout':
        options.idleTimeout = nonNegativeInteger(flag, value());
        break;
      default:
        throw new Error(`unknown flag ${flag}: this wrapper takes only --port, --host, --allowed-hosts, --isolated, --output-dir, --user-agent, --viewport-size and --idle-timeout`);
    }
  }
  return options;
}
