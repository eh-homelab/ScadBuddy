import { api } from '../api/client'
import type { PluginFeedPackage, PluginFeedUnlisted, PluginFeedView } from '../api/pluginFeeds'
import { useMcpAuthVersion } from '../lib/mcpAuthChanges'
import { useMintedMcpToken } from '../lib/mcpMintedToken'
import { useAsync } from '../lib/useAsync'
import { CopySnippet } from './McpConnectSnippet'

const PLACEHOLDER = '<your token>'

/** Why a package is named by the feed but not listed in it, as the person can act on it. */
const UNLISTED: Record<PluginFeedUnlisted, string> = {
  'not-approved': 'Not listed until its pin is approved',
  'no-version': 'Not listed: its pin is no published version',
  unpinnable: 'Not listed: Claude Code cannot pin this source',
}

function pinOf(p: PluginFeedPackage): string {
  if (!p.listed) return UNLISTED[p.reason ?? 'not-approved']
  if (p.kind === 'hosted') return `Version ${p.version}`
  return `Commit ${p.commit?.slice(0, 7) ?? ''}`
}

/**
 * The setup text for Claude Code (spec 2026-11-21 §6.4, §9). With a token, the feed is
 * registered in the user's own settings, because `/plugin marketplace add` sends no headers;
 * the headers of a `url` marketplace source go with the catalog's fetch and with each archive
 * on the same origin ([settings reference, extraKnownMarketplaces](https://code.claude.com/docs/en/settings-reference.md#extraknownmarketplaces);
 * [host a marketplace, authenticate archive downloads](https://code.claude.com/docs/en/plugins/host-marketplace.md#authenticate-archive-downloads)).
 * The token is written as it is: the docs do not say `headers` values expand environment
 * variables, and a `headersHelper` would need a command on the user's machine.
 */
function feedSnippets(view: PluginFeedView, url: string, token: string | undefined) {
  const header = view.auth_mode !== 'disabled'
  const source: Record<string, unknown> = { source: 'url', url }
  if (header) source.headers = { Authorization: `Bearer ${token ?? PLACEHOLDER}` }
  const settings = JSON.stringify({ extraKnownMarketplaces: { [view.name]: { source } } }, null, 2)
  const first = view.packages.find((p) => p.listed)?.name ?? '<plugin>'
  return { header, settings, add: `/plugin marketplace add ${url}`, install: `/plugin install ${first}@${view.name}` }
}

/** Settings → MCP access: the plugins the feed lists, and how Claude Code installs them (#6933). */
export function PluginFeedSection() {
  const authVersion = useMcpAuthVersion()
  const feed = useAsync(() => api.getPluginFeed('default'), [authVersion])
  const token = useMintedMcpToken() ?? undefined

  if (feed.error) return <p className="text-sm text-warn">The plugin feed could not be read: {feed.error.message}</p>
  const view = feed.data
  if (!view) return null
  const url = view.marketplace_url
  const snippets = url ? feedSnippets(view, url, token) : undefined

  return (
    <div className="space-y-4" data-testid="plugin-feed">
      <p className="text-xs text-muted-foreground">
        Claude Code outside ScadBuddy installs these plugins from ScadBuddy, each at the pin an admin approved.
        A newer pin reaches it after the admin approves it and the user runs{' '}
        <code className="sb-num">/plugin marketplace update {view.name}</code>.
      </p>

      {view.packages.length === 0 ? (
        <p className="text-sm text-muted-foreground">No plugins are in the feed yet.</p>
      ) : (
        <ul aria-label="Plugins in the feed" className="divide-y rounded-md border">
          {view.packages.map((p) => (
            <li key={p.name} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-3 py-2">
              <span className={`text-sm font-medium ${p.listed ? '' : 'text-muted-foreground'}`}>{p.name}</span>
              <span className={`sb-num text-xs ${p.listed ? 'text-muted-foreground' : 'text-warn'}`}>{pinOf(p)}</span>
            </li>
          ))}
        </ul>
      )}

      {!snippets ? (
        <p className="text-sm text-warn">
          The feed has no address: set ScadBuddy&rsquo;s public URL (SCADBUDDY_PUBLIC_URL) for the agent.
        </p>
      ) : (
        <div className="space-y-3">
          {snippets.header ? (
            <>
              <p className="text-xs text-muted-foreground">
                {!token && <>Create a token under Tokens, then paste it where the entry says {PLACEHOLDER}. </>}
                Add the entry to <code className="sb-num">~/.claude/settings.json</code>, not to a project&rsquo;s
                settings file, so the token stays out of the repository. Claude Code sends no token with{' '}
                <code className="sb-num">/plugin marketplace add</code>.
              </p>
              <CopySnippet
                id="plugin-feed-settings"
                title="Claude Code user settings"
                copyLabel="Copy the settings entry"
                text={snippets.settings}
              />
            </>
          ) : (
            <CopySnippet id="plugin-feed-add" title="Add the feed in Claude Code" copyLabel="Copy the add command" text={snippets.add} />
          )}
          <CopySnippet id="plugin-feed-install" title="Install a plugin" copyLabel="Copy the install command" text={snippets.install} />
        </div>
      )}
    </div>
  )
}
