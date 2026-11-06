import { type Kek, SECRET_MIN_CHARS } from '../secrets.js'
import type { PluginOAuth } from './oauthStore.js'
import type { PackagesForRun } from './packages/install.js'
import { packageHeaderSecrets } from './packages/mcp.js'
import { headerSecretVariants, type PluginStore } from './registry.js'

/**
 * Every plugin secret the agent holds, read from its own stores alone: no address check,
 * no forwarder route, no plugin or token endpoint contacted.
 *
 * - each enabled remote plugin's header value (`ai_plugins`, opened under the KEK);
 * - each package http server's header values (`ai_plugin_packages`);
 * - every OAuth sign-in's access and refresh tokens and client secret (`ai_plugin_oauth`,
 *   opened under the KEK).
 *
 * A turn's ToolCall tools redact and refuse uploads of these (main.ts `turnHeaderSecrets`,
 * tools/harness.ts), and a file picked from a workspace in the UI is refused when it holds
 * one (routes/sessionFiles.ts, #4703). One reader for both, so the two cannot drift.
 *
 * It includes every sign-in's tokens, a plugin that is not enabled too: a token stays a
 * secret while it is stored. A value shorter than SECRET_MIN_CHARS is left out, so it
 * cannot redact ordinary text in a turn's tool output.
 */
export async function readPluginSecrets(deps: {
  kek: Kek | undefined
  plugins?: Pick<PluginStore, 'enabled'> | undefined
  oauth?: Pick<PluginOAuth, 'storedSecrets'> | undefined
  packages?: (() => Promise<PackagesForRun>) | undefined
}): Promise<string[]> {
  const out: string[] = []
  if (deps.plugins) {
    const { plugins: enabled } = await deps.plugins.enabled(deps.kek)
    for (const plugin of enabled) if (plugin.header) out.push(...headerSecretVariants(plugin.header.value))
  }
  if (deps.oauth) out.push(...(await deps.oauth.storedSecrets(deps.kek)))
  if (deps.packages) {
    const loaded = await deps.packages()
    try {
      out.push(...packageHeaderSecrets(loaded.loaded))
    } finally {
      loaded.release()
    }
  }
  return [...new Set(out)].filter((value) => value.length >= SECRET_MIN_CHARS)
}
