# Vendored: the official `playwright` Claude plugin

The headless browser for the agent harness (issue #349, AI spec D11 and §5.3).

| | |
|---|---|
| Source | [`anthropics/claude-plugins-official`, `external_plugins/playwright`](https://github.com/anthropics/claude-plugins-official/tree/fa59bc9037741ecfa131aa27938272605710d7b2/external_plugins/playwright) |
| Pinned commit | `fa59bc9037741ecfa131aa27938272605710d7b2` (HEAD on 2026-09-28) |
| License | Apache-2.0 (the repository's root `LICENSE`; the plugin directory has none of its own) |
| Server | [`@playwright/mcp`](https://www.npmjs.com/package/@playwright/mcp/v/0.0.82) **0.0.82**, an exact dependency in `agent/package.json`, Apache-2.0, [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) |

`.claude-plugin/plugin.json` is the upstream file, byte for byte (sha256
`f2d7c0f611b93287ebb07454255d0cccf3a343d7095b7f8f763ff21d1995c096`).

The upstream `.mcp.json` is deliberately **not** vendored. It is
`{"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}`, which fetches
an unpinned package at runtime (spec D11, "Rejected"). Instead
`src/harness/headlessBrowser.ts` writes a copy of this plugin per session, with a
`.mcp.json` that starts the pinned `cli.js` under `env -i` with that session's config
(origin allow-list, agent-actor marker, output directory). See
`docs/ai/headless-browser.md`.

To re-pin: fetch `plugin.json` at the new commit, update the commit and hash here and in
`PLAYWRIGHT_PLUGIN_COMMIT`; to bump the server, change `agent/package.json`,
`PLAYWRIGHT_MCP_VERSION` and the Dockerfile's Chromium install together, and re-run every
§3.2 row for §5.3 (the e2e test `test/headlessBrowser.e2e.test.ts` measures them).
