import type { CredentialRepo } from '../credentials.js'
import { type Kek, SECRET_MIN_CHARS } from '../secrets.js'
import type { PluginOAuth } from './oauthStore.js'
import type { PackageInstaller } from './packages/install.js'
import type { PackageRepo } from './packages/store.js'
import { headerSecretVariants, type PluginStore } from './registry.js'

/** Where the enabled packages' header secrets are read from: their pins, and the installer's verified copies. */
export type PackageSecretSource = {
  store: Pick<PackageRepo, 'enabledPins'>
  installer: Pick<PackageInstaller, 'headerSecrets'>
}

/**
 * Every plugin secret the agent holds, read from its own stores and its packages' pinned
 * files (fetched again from the pin when no verified copy is cached): no address check, no
 * forwarder route, no plugin or token endpoint contacted.
 *
 * - each enabled remote plugin's header value (`ai_plugins`, opened under the KEK);
 * - each enabled package's http server header values: literals in its pinned files, read from
 *   the copy that hashes to its pin (`ai_plugin_packages`, install.ts `headerSecrets`), never
 *   from what a turn loaded, so a package that fails to load keeps them in the list (#5485);
 * - every OAuth sign-in's access and refresh tokens and client secret (`ai_plugin_oauth`,
 *   opened under the KEK).
 *
 * Every tool redacts and refuses uploads of these in every mode, an in-process turn, a
 * ToolCall, /mcp and a background task alike (main.ts `toolServices.secrets`, #5480), and a
 * file picked from a workspace in the UI is refused when it holds one (routes/sessionFiles.ts,
 * #4703). One reader for all, so they cannot drift.
 *
 * It includes every sign-in's tokens, a plugin that is not enabled too: a token stays a
 * secret while it is stored. A value shorter than SECRET_MIN_CHARS is left out, so it
 * cannot redact ordinary text in a turn's tool output. A package whose files cannot be had
 * (its fetch fails and no verified copy is cached) fails the read: the list fails closed.
 */
export async function readPluginSecrets(deps: {
  kek: Kek | undefined
  plugins?: Pick<PluginStore, 'enabled'> | undefined
  oauth?: Pick<PluginOAuth, 'storedSecrets'> | undefined
  packages?: PackageSecretSource | undefined
}): Promise<string[]> {
  const out: string[] = []
  if (deps.plugins) {
    const { plugins: enabled } = await deps.plugins.enabled(deps.kek)
    for (const plugin of enabled) if (plugin.header) out.push(...headerSecretVariants(plugin.header.value))
  }
  if (deps.oauth) out.push(...(await deps.oauth.storedSecrets(deps.kek)))
  if (deps.packages) {
    const { store, installer } = deps.packages
    for (const values of await Promise.all((await store.enabledPins()).map((pin) => installer.headerSecrets(pin)))) out.push(...values)
  }
  return [...new Set(out)].filter((value) => value.length >= SECRET_MIN_CHARS)
}

/** How long one read of the plugin secrets serves every tool call: a header changed since is redacted once it lapses. */
export const PLUGIN_SECRETS_CACHE_MS = 30_000

/**
 * What every tool redacts and every upload refuses, in every mode (an in-process turn, a
 * ToolCall, /mcp, a background task; #5480): each saved Claude credential's secret, read at
 * the call, and `readPluginSecrets`, one read serving `PLUGIN_SECRETS_CACHE_MS`. A read that
 * failed is not kept, so the next call reads again; the call itself fails (fail-closed). A
 * turn adds its loaded plugins' current headers on top (tools/harness.ts).
 *
 * `fresh` reads both with no cache, for a workspace pick in the UI (#4703): a pick is rare,
 * and a secret stored a moment ago counts. main.ts wires both from here, and the tests use
 * this too, so a regression in the wiring fails them (#5483).
 */
export function toolSecrets(deps: {
  kek: Kek | undefined
  credentials?: Pick<CredentialRepo, 'revealAll'> | undefined
  plugins?: Pick<PluginStore, 'enabled'> | undefined
  oauth?: Pick<PluginOAuth, 'storedSecrets'> | undefined
  packages?: PackageSecretSource | undefined
}): { secrets: () => Promise<string[]>; fresh: () => Promise<string[]> } {
  const { kek, credentials } = deps
  // None without the KEK, which opens them.
  const saved = async (): Promise<string[]> =>
    credentials && kek ? (await credentials.revealAll(kek)).flatMap((row) => (row.credential ? [row.credential.secret] : [])) : []
  const read = () => readPluginSecrets(deps)
  let cache: { at: number; read: Promise<string[]> } | undefined
  const cached = (): Promise<string[]> => {
    if (!cache || Date.now() - cache.at > PLUGIN_SECRETS_CACHE_MS) {
      const next = read()
      cache = { at: Date.now(), read: next }
      next.catch(() => {
        if (cache?.read === next) cache = undefined
      })
    }
    return cache.read
  }
  return {
    secrets: async () => [...(await saved()), ...(await cached())],
    fresh: async () => [...(await saved()), ...(await read())],
  }
}
