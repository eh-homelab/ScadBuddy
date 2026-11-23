// ScadBuddy's Playwright MCP server (#7027): @playwright/mcp's tools, with each MCP
// session's browser context built from the X-ScadBuddy-Browser header that the agent
// sends on the POST that creates the session, merged over this server's own options.
//
// Why a wrapper: the CLI (`cli.js`) takes one user agent and one viewport for every
// client (`--user-agent`, `--viewport-size`); a session that asks for a phone's
// browser had no way to get one. The package's library entry,
// `createConnection(config, contextGetter)`, builds a server per connection, so this
// file runs the CLI's HTTP layer itself and gives each session its own context options.
//
// Measured on 2026-11-23 in `@playwright/mcp@0.0.85` (npm pack) and its dependency
// `playwright-core@1.66.0-alpha-2026-11-09` (lib/coreBundle.js; the sources are
// https://github.com/microsoft/playwright/tree/main/packages/playwright-core/src/tools):
// - `index.js` is `require('playwright-core/lib/coreBundle').tools.createConnection`;
//   `index.d.ts`: `createConnection(config?, contextGetter?: () => Promise<BrowserContext>)`.
// - `createConnection` (tools/mcp/index.ts) creates the backend on the session's first
//   tool call: with a getter, `config.browser.isolated ? browser.newContext(...) :
//   browser.contexts()[0]`, where `browser` wraps the getter's one context and its
//   `newContext` throws. So `isolated: false` here, and the getter returns a NEW context
//   of one shared Chromium: each session still has its own in-memory context, which is
//   what `--isolated` gives the CLI (`decorateMCPCommand` shares one browser and calls
//   `browser.newContext(contextOptions)` per client).
// - The backend's `dispose` does not close that context, so this file closes it when
//   the session's transport closes. When a context closes, the backend reports
//   "disconnected" and the session's next tool call calls the getter again
//   (tools/utils/mcp/server.ts `createServer`): the idle timer below relies on that.
// - `createConnection` returns an MCP `Server` with no transport. The CLI's HTTP layer
//   (tools/utils/mcp/http.ts) uses `StreamableHTTPServerTransport` from
//   `playwright-core/lib/utilsBundle`, one per `mcp-session-id`, and answers 403 to a
//   `Host` not in `--allowed-hosts` (default: localhost, 127.0.0.1 and [::1] at the port).
// - The CLI's HTTP layer connects each session with `runHeartbeat` true: once the
//   client's GET stream is open (and a tool was called), `startHeartbeat` pings every
//   3 s and closes the server on no answer within PLAYWRIGHT_MCP_PING_TIMEOUT_MS (5000),
//   which drops a client that went away without a DELETE. `createConnection` passes
//   `runHeartbeat` false, so this file pings on its own, from the session's first GET.
// - `--browser chromium` is `channel: 'chrome-for-testing'` (`resolveBrowserParam`),
//   `--no-sandbox` is `chromiumSandbox: false`; the CLI closes an idle headless browser
//   after one hour by default (`--idle-timeout`).
// - The base image mcr.microsoft.com/playwright/mcp:v0.0.85 runs
//   `tini -s -- node /app/cli.js --headless --browser chromium --no-sandbox` as user
//   node (1000), with Chromium in /ms-playwright/chromium-1254 (the revision in that
//   playwright-core's browsers.json); its /app has no index.js, so this folder installs
//   the package itself.
//
// The Dockerfile's base image tag and package.json's `@playwright/mcp` version move
// together; the image build fails when they differ (Dockerfile, stage `playwright-mcp`).
//
// The header's value is never logged: a refused one logs only why.
import { randomUUID } from 'node:crypto';
import http from 'node:http';
import { createRequire } from 'node:module';
import net from 'node:net';

import { HEADER, contextOptionsFor, parseArgs, parseBrowserHeader } from './options.mjs';

const require = createRequire(import.meta.url);
const { createConnection } = require('@playwright/mcp');
const { chromium } = require('playwright-core');
const { StreamableHTTPServerTransport } = require('playwright-core/lib/utilsBundle');

const LAUNCH_OPTIONS = { channel: 'chrome-for-testing', headless: true, chromiumSandbox: false };

// The CLI's heartbeat (tools/utils/mcp/server.ts `startHeartbeat`): a ping every 3 s,
// closed on no answer within PLAYWRIGHT_MCP_PING_TIMEOUT_MS (default 5000, 0 turns it off).
const PING_EVERY = 3000;
const PING_TIMEOUT = (() => {
  const value = Number(process.env.PLAYWRIGHT_MCP_PING_TIMEOUT_MS ?? 5000);
  return Number.isFinite(value) ? value : 5000;
})();

const log = (message) => console.error(`[scadbuddy-playwright-mcp] ${message}`);

let args;
try {
  args = parseArgs(process.argv.slice(2));
} catch (error) {
  log(error.message);
  process.exit(2);
}

const baseContextOptions = {};
if (args.userAgent !== undefined) baseContextOptions.userAgent = args.userAgent;
if (args.viewport !== undefined) baseContextOptions.viewport = args.viewport;

// One Chromium for every session, launched on the first tool call that needs it and
// again after it disconnects (a crash, or SIGTERM's close).
let browserPromise;
function getBrowser() {
  if (!browserPromise) {
    const promise = chromium.launch(LAUNCH_OPTIONS).then(
      (browser) => {
        browser.once('disconnected', () => {
          if (browserPromise === promise) browserPromise = undefined;
        });
        return browser;
      },
      (error) => {
        if (browserPromise === promise) browserPromise = undefined;
        throw error;
      },
    );
    browserPromise = promise;
  }
  return browserPromise;
}

/** @type {Map<string, Session>} */
const sessions = new Map();

class Session {
  constructor(contextOptions) {
    this.contextOptions = contextOptions;
    this.context = undefined;
    this.closed = false;
    this.idleTimer = undefined;
    this.heartbeat = false;
    this.server = undefined;
    this.transport = undefined;
  }

  // The getter createConnection calls on the session's first tool call, and again after
  // its context closed (idle, or a browser crash).
  async newContext() {
    const browser = await getBrowser();
    const context = await browser.newContext(this.contextOptions);
    if (this.closed) {
      await context.close().catch(() => {});
      throw new Error('the MCP session closed');
    }
    this.context = context;
    context.once('close', () => {
      if (this.context === context) this.context = undefined;
    });
    this.poke();
    return context;
  }

  // Restarts the idle clock. When it runs out, a session whose client the heartbeat
  // watches closes only its context (the next tool call gets a new one with the same
  // options); a session with no heartbeat (its client never opened the GET stream, so
  // a gone client cannot be told from a quiet one) closes whole.
  poke() {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = undefined;
    if (!args.idleTimeout || this.closed) return;
    this.idleTimer = setTimeout(() => {
      this.idleTimer = undefined;
      if (this.heartbeat) this.closeContext();
      else void this.server?.close().catch(() => {});
    }, args.idleTimeout);
    this.idleTimer.unref();
  }

  // Once the client's GET stream is open, ping it as the CLI does: an answer that does
  // not come closes the server, whose transport's onclose drops the session and its
  // context. This collects a client that went away without a DELETE.
  startHeartbeat() {
    if (this.heartbeat || PING_TIMEOUT <= 0) return;
    this.heartbeat = true;
    const beat = () => {
      if (this.closed) return;
      let timer;
      Promise.race([
        this.server.ping(),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error('ping timeout')), PING_TIMEOUT);
        }),
      ]).then(
        () => {
          clearTimeout(timer);
          setTimeout(beat, PING_EVERY).unref();
        },
        () => {
          clearTimeout(timer);
          if (!this.closed) log('a client did not answer a ping: closing its session');
          void this.server.close().catch(() => {});
        },
      );
    };
    // The first ping waits a beat, so the GET stream it travels on is registered.
    setTimeout(beat, PING_EVERY).unref();
  }

  closeContext() {
    const context = this.context;
    this.context = undefined;
    return context?.close().catch(() => {});
  }

  close() {
    this.closed = true;
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = undefined;
    return this.closeContext();
  }
}

async function startSession(req, res) {
  const { options, warning } = parseBrowserHeader(req.headers[HEADER]);
  if (warning) log(warning);
  const session = new Session(contextOptionsFor(baseContextOptions, options));
  const server = await createConnection(
    {
      browser: {
        browserName: 'chromium',
        isolated: false,
        launchOptions: LAUNCH_OPTIONS,
        contextOptions: session.contextOptions,
      },
      outputDir: args.outputDir,
    },
    () => session.newContext(),
  );
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
    onsessioninitialized: (sessionId) => {
      sessions.set(sessionId, session);
      session.poke();
    },
  });
  // server.connect chains its own onclose after this one.
  transport.onclose = () => {
    if (transport.sessionId) sessions.delete(transport.sessionId);
    void session.close();
  };
  session.transport = transport;
  session.server = server;
  await server.connect(transport);
  await transport.handleRequest(req, res);
  // Not an initialize request: the transport refused it and no session exists.
  if (!transport.sessionId) await server.close().catch(() => {});
}

async function handle(req, res) {
  const host = req.headers.host?.toLowerCase();
  if (!allowAnyHost && host && !allowedHosts.includes(host)) {
    res.statusCode = 403;
    res.end('Access is only allowed at ' + allowedHosts.join(', '));
    return;
  }
  const url = new URL(req.url ?? '/', 'http://localhost');
  if (url.pathname.startsWith('/sse')) {
    res.statusCode = 404;
    res.end('This server has no SSE transport; use streamable HTTP');
    return;
  }
  const sessionId = req.headers['mcp-session-id'];
  if (sessionId !== undefined) {
    const session = typeof sessionId === 'string' ? sessions.get(sessionId) : undefined;
    if (!session) {
      res.statusCode = 404;
      res.end('Session not found');
      return;
    }
    session.poke();
    res.once('close', () => session.poke());
    const handled = session.transport.handleRequest(req, res);
    if (req.method === 'GET') session.startHeartbeat();
    await handled;
    return;
  }
  if (req.method === 'POST') {
    await startSession(req, res);
    return;
  }
  res.statusCode = 400;
  res.end('Invalid request');
}

const httpServer = http.createServer((req, res) => {
  handle(req, res).catch((error) => {
    log(`request failed: ${error?.message ?? error}`);
    if (!res.headersSent) {
      res.statusCode = 500;
      res.end('Internal error');
    } else {
      res.destroy();
    }
  });
});

let allowedHosts;
let allowAnyHost = false;

httpServer.listen(args.port, args.host, () => {
  const { port } = httpServer.address();
  const bindHost = args.host;
  const host = !bindHost || bindHost === '0.0.0.0' || bindHost === '::' ? 'localhost'
    : net.isIPv6(bindHost) ? `[${bindHost}]` : bindHost;
  const url = `http://${host}:${port}`;
  allowedHosts = args.allowedHosts ?? [
    ...new Set([`localhost:${port}`, `127.0.0.1:${port}`, `[::1]:${port}`, new URL(url).host]),
  ];
  allowAnyHost = allowedHosts.includes('*');
  log(`listening on ${url}/mcp`);
});

let stopping = false;
async function stop(signal) {
  if (stopping) return;
  stopping = true;
  log(`${signal}: closing`);
  httpServer.close();
  await Promise.allSettled([...sessions.values()].map((session) => session.close()));
  const browser = await browserPromise?.catch(() => undefined);
  await browser?.close().catch(() => {});
  process.exit(0);
}
process.on('SIGTERM', () => void stop('SIGTERM'));
process.on('SIGINT', () => void stop('SIGINT'));
