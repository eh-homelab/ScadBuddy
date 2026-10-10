import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../../agent/dom'
import {
  aiPlugins,
  type BuiltInToolSet,
  type ListedPlugin,
  type PluginTest,
  type RemotePlugin,
  type RiskTier,
} from '../../api/aiPlugins'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

/**
 * Settings → Plugins (#297, #464, #1953). First ScadBuddy's own tool sets (`built_in`): the
 * same per-tool table as a remote plugin, but a tier can only be raised above the tier the
 * code gives the tool (the select starts there), and a set is never removed. Then remote MCP
 * servers the assistant reaches
 * through the agent's loopback forwarder (agent/src/routes/plugins.ts). The secret is
 * write-only: the server shows the header name and the value's last four characters.
 *
 * A plugin is registered disabled. "Test connection" lists its tools with the tier the
 * harness would apply; every tool is `outward` (asks before it acts) until you give it
 * a tier here. A server's `readOnlyHint` is only shown as a suggestion, as MCP says
 * annotations from untrusted servers are untrusted. All writes are user-only.
 */

const TIERS: RiskTier[] = ['read', 'write', 'outward']

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function tierLabel(tier: RiskTier): string {
  return tier === 'outward' ? 'outward (asks first)' : tier
}

function BuiltInToolTable({ set, onSaved }: { set: BuiltInToolSet; onSaved: (set: BuiltInToolSet) => void }) {
  const [tiers, setTiers] = useState<Record<string, RiskTier>>(set.tool_tiers)
  const [disabled, setDisabled] = useState<string[]>(set.disabled_tools)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    setBusy(true)
    setError(null)
    try {
      onSaved(await aiPlugins.updateBuiltIn(set.name, { tool_tiers: tiers, disabled_tools: disabled }))
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-2 space-y-2" {...USER_ONLY}>
      <p className="text-[12px] text-muted">
        A tool&rsquo;s tier can be raised, never set below the tier ScadBuddy gives it.
      </p>
      <table className="w-full text-left text-[12px]">
        <thead className="text-muted">
          <tr>
            <th className="py-1 font-normal">Tool</th>
            <th className="py-1 font-normal">Tier</th>
            <th className="py-1 font-normal">Disabled</th>
          </tr>
        </thead>
        <tbody>
          {set.tools.map((tool) => (
            <tr key={tool.name} className="border-t border-line align-top">
              <td className="py-1 pr-2">
                <span className="sb-num break-all">{tool.harness_name}</span>
              </td>
              <td className="py-1 pr-2">
                <select
                  aria-label={`Tier of ${tool.name}`}
                  value={tiers[tool.name] ?? tool.risk}
                  onChange={(event) => {
                    const tier = event.target.value as RiskTier
                    const { [tool.name]: _dropped, ...rest } = tiers
                    setTiers(tier === tool.risk ? rest : { ...rest, [tool.name]: tier })
                  }}
                  className="sb-field h-7 py-0"
                >
                  {TIERS.slice(TIERS.indexOf(tool.risk)).map((t) => (
                    <option key={t} value={t}>
                      {tierLabel(t)}
                    </option>
                  ))}
                </select>
              </td>
              <td className="py-1">
                <input
                  type="checkbox"
                  aria-label={`Disable ${tool.name}`}
                  checked={disabled.includes(tool.name)}
                  onChange={(event) =>
                    setDisabled(
                      event.target.checked ? [...disabled, tool.name] : disabled.filter((d) => d !== tool.name),
                    )
                  }
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Button size="sm" onClick={() => void save()} disabled={busy} aria-busy={busy} {...USER_ONLY}>
        {busy && <Spinner />}
        Save tool settings
      </Button>
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

function BuiltInCard({ set, onChange }: { set: BuiltInToolSet; onChange: (set: BuiltInToolSet) => void }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const raised = Object.keys(set.tool_tiers).length

  async function toggle() {
    setBusy(true)
    setError(null)
    try {
      onChange(await aiPlugins.updateBuiltIn(set.name, { enabled: !set.enabled }))
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="rounded-[6px] border border-line p-3" aria-label={`Built-in plugin ${set.name}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium">{set.name}</span>
        <span className="rounded-full border border-line px-2 py-px text-[11px] text-muted">Built in</span>
        <span className={`rounded-full border px-2 py-px text-[11px] ${set.enabled ? 'border-ok/40 text-ok' : 'border-line text-muted'}`}>
          {set.enabled ? 'Enabled' : 'Disabled'}
        </span>
      </div>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[12px]">
        <dt className="text-muted">Tools</dt>
        <dd className="sb-num">
          {set.tool_prefix}* ({set.tools.length}
          {raised > 0 && `, ${raised} raised`}
          {set.disabled_tools.length > 0 && `, ${set.disabled_tools.length} disabled`})
        </dd>
      </dl>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? 'Hide tools' : 'Review tools'}
        </Button>
        {set.switchable && (
          <Button size="sm" onClick={() => void toggle()} disabled={busy} aria-busy={busy} {...USER_ONLY}>
            {set.enabled ? 'Disable' : 'Enable'}
          </Button>
        )}
      </div>
      {open && <BuiltInToolTable set={set} onSaved={onChange} />}
      {error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {error}
        </p>
      )}
    </li>
  )
}

function RegisterForm({ onCreated }: { onCreated: (plugin: RemotePlugin) => void }) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [header, setHeader] = useState('Authorization')
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const plugin = await aiPlugins.createRemote({
        name: name.trim(),
        url: url.trim(),
        ...(secret ? { secret, auth_header: header.trim() || 'Authorization' } : {}),
      })
      setName('')
      setUrl('')
      setSecret('')
      onCreated(plugin)
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="space-y-3" aria-label="Add a plugin endpoint" {...USER_ONLY}>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="remote-name" className="block text-[13px]">
            Name
          </label>
          <input
            id="remote-name"
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="hindsight"
            className="sb-field sb-num mt-1.5"
          />
        </div>
        <div>
          <label htmlFor="remote-url" className="block text-[13px]">
            MCP endpoint URL
          </label>
          <input
            id="remote-url"
            type="url"
            required
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://memory.example/mcp/"
            className="sb-field sb-num mt-1.5"
          />
        </div>
        <div>
          <label htmlFor="remote-header" className="block text-[13px]">
            Auth header
          </label>
          <input
            id="remote-header"
            value={header}
            onChange={(event) => setHeader(event.target.value)}
            className="sb-field sb-num mt-1.5"
          />
        </div>
        <div>
          <label htmlFor="remote-secret" className="block text-[13px]">
            Header value
          </label>
          <input
            id="remote-secret"
            type="password"
            autoComplete="off"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder="Bearer … (optional)"
            className="sb-field sb-num mt-1.5"
          />
        </div>
      </div>
      <Button type="submit" disabled={busy} aria-busy={busy} {...USER_ONLY}>
        {busy && <Spinner />}
        Add endpoint
      </Button>
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

function ToolReview({
  plugin,
  test,
  onSaved,
}: {
  plugin: RemotePlugin
  test: PluginTest
  onSaved: (plugin: RemotePlugin) => void
}) {
  const [tiers, setTiers] = useState<Record<string, RiskTier>>(plugin.tool_tiers)
  const [hidden, setHidden] = useState<string[]>(plugin.disabled_tools)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    setBusy(true)
    setError(null)
    try {
      onSaved(await aiPlugins.updateRemote(plugin.name, { tool_tiers: tiers, disabled_tools: hidden }))
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-2 space-y-2" {...USER_ONLY}>
      <table className="w-full text-left text-[12px]">
        <thead className="text-muted">
          <tr>
            <th className="py-1 font-normal">Tool</th>
            <th className="py-1 font-normal">Tier</th>
            <th className="py-1 font-normal">Hidden</th>
          </tr>
        </thead>
        <tbody>
          {test.tools.map((tool) => {
            const fixed = tool.tier_source === 'renamed' || tool.tier_source === 'collision'
            return (
              <tr key={tool.name} className="border-t border-line align-top">
                <td className="py-1 pr-2">
                  <span className="sb-num break-all">{tool.harness_name}</span>
                  {tool.suggested_tier && (
                    <span className="ml-1.5 text-muted">(server suggests {tool.suggested_tier})</span>
                  )}
                  {fixed && (
                    <span className="block text-warn">
                      {tool.tier_source === 'renamed'
                        ? 'Renamed by Claude Code, so it stays outward.'
                        : `Shares a name with ${tool.collides_with.join(', ')}; hidden.`}
                    </span>
                  )}
                </td>
                <td className="py-1 pr-2">
                  <select
                    aria-label={`Tier of ${tool.name}`}
                    value={fixed ? 'outward' : (tiers[tool.name] ?? 'outward')}
                    disabled={fixed}
                    onChange={(event) => setTiers({ ...tiers, [tool.name]: event.target.value as RiskTier })}
                    className="sb-field h-7 py-0"
                  >
                    {TIERS.map((t) => (
                      <option key={t} value={t}>
                        {tierLabel(t)}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="py-1">
                  <input
                    type="checkbox"
                    aria-label={`Hide ${tool.name}`}
                    checked={hidden.includes(tool.name) || tool.tier_source === 'collision'}
                    disabled={tool.tier_source === 'collision'}
                    onChange={(event) =>
                      setHidden(event.target.checked ? [...hidden, tool.name] : hidden.filter((h) => h !== tool.name))
                    }
                  />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {test.truncated && <p className="text-[12px] text-warn">The server has more tools than one page listed.</p>}
      <Button size="sm" onClick={() => void save()} disabled={busy} aria-busy={busy} {...USER_ONLY}>
        {busy && <Spinner />}
        Save tool settings
      </Button>
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

function RemoteCard({
  plugin,
  onChange,
  onRemoved,
}: {
  plugin: RemotePlugin
  onChange: (plugin: RemotePlugin) => void
  onRemoved: (name: string) => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [test, setTest] = useState<PluginTest | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  async function act(what: string, run: () => Promise<void>) {
    setBusy(what)
    setError(null)
    try {
      await run()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  return (
    <li className="rounded-[6px] border border-line p-3" aria-label={`Plugin endpoint ${plugin.name}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium">{plugin.name}</span>
        <span className={`rounded-full border px-2 py-px text-[11px] ${plugin.enabled ? 'border-ok/40 text-ok' : 'border-line text-muted'}`}>
          {plugin.enabled ? 'Enabled' : 'Disabled'}
        </span>
        {!plugin.usable && (
          <span className="rounded-full border border-warn/40 px-2 py-px text-[11px] text-warn">
            Secret sealed with another key; save it again
          </span>
        )}
      </div>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[12px]">
        <dt className="text-muted">Endpoint</dt>
        <dd className="sb-num break-all">{plugin.url}</dd>
        <dt className="text-muted">Auth</dt>
        <dd className="sb-num">{plugin.auth ? `${plugin.auth.header}: …${plugin.auth.last4}` : 'None'}</dd>
        <dt className="text-muted">Tools</dt>
        <dd className="sb-num">{plugin.tool_prefix}*</dd>
      </dl>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          onClick={() => void act('test', async () => setTest(await aiPlugins.testRemote(plugin.name)))}
          disabled={busy !== null}
          aria-busy={busy === 'test'}
          {...USER_ONLY}
        >
          {busy === 'test' && <Spinner />}
          Test connection
        </Button>
        <Button
          size="sm"
          onClick={() =>
            void act('enable', async () => onChange(await aiPlugins.updateRemote(plugin.name, { enabled: !plugin.enabled })))
          }
          disabled={busy !== null}
          {...USER_ONLY}
        >
          {plugin.enabled ? 'Disable' : 'Enable'}
        </Button>
        <Button size="sm" variant="danger" onClick={() => setConfirmDelete(true)} disabled={busy !== null} {...USER_ONLY}>
          Remove
        </Button>
      </div>
      {test && (
        <div className="mt-2">
          <p role="status" className={`text-[12px] ${test.ok ? 'text-ok' : 'text-warn'}`}>
            {test.detail}
            {test.server && ` (${test.server.name} ${test.server.version})`}
          </p>
          {test.ok && <ToolReview plugin={plugin} test={test} onSaved={onChange} />}
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {error}
        </p>
      )}
      <Dialog
        open={confirmDelete}
        title={`Remove ${plugin.name}?`}
        description="The endpoint and its stored secret are deleted."
        onClose={() => setConfirmDelete(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() =>
                void act('delete', async () => {
                  await aiPlugins.deleteRemote(plugin.name)
                  setConfirmDelete(false)
                  onRemoved(plugin.name)
                })
              }
              {...USER_ONLY}
            >
              Remove endpoint
            </Button>
          </>
        }
      >
        <p className="text-[13px]">Sessions stop using its tools from their next turn.</p>
      </Dialog>
    </li>
  )
}

export function RemotePluginsPanel() {
  const state = useAsync(() => aiPlugins.listPlugins(), [])
  const listed = state.data ?? []
  const builtIns = listed.filter((p): p is BuiltInToolSet => p.built_in === true)
  const plugins = listed.filter((p): p is RemotePlugin => p.built_in !== true)
  const replace = (plugin: ListedPlugin) =>
    state.setData(
      listed.some((p) => p.name === plugin.name)
        ? listed.map((p) => (p.name === plugin.name ? plugin : p))
        : [...listed, plugin],
    )

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface" aria-labelledby="plugin-endpoints-heading">
      <h2 id="plugin-endpoints-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        Plugins
      </h2>
      <div className="space-y-4 p-4">
        <p className="text-[12px] text-muted">
          ScadBuddy&rsquo;s own tools, built in: raise a tool&rsquo;s tier or disable it. Then remote MCP servers
          (Streamable HTTP over HTTPS) whose tools the assistant may use. Registered disabled: test the connection,
          review each tool&rsquo;s tier, then enable it.
        </p>
        {state.loading && <Spinner />}
        {state.error && (
          <p role="alert" className="text-[12px] text-warn">
            Plugins are unavailable: {state.error.message}
          </p>
        )}
        {builtIns.length > 0 && (
          <ul className="space-y-3" aria-label="Built-in plugins">
            {builtIns.map((set) => (
              <BuiltInCard key={set.name} set={set} onChange={replace} />
            ))}
          </ul>
        )}
        {plugins.length > 0 && (
          <ul className="space-y-3" aria-label="Plugin endpoints">
            {plugins.map((plugin) => (
              <RemoteCard
                key={plugin.name}
                plugin={plugin}
                onChange={replace}
                onRemoved={(name) => state.setData(listed.filter((p) => p.name !== name))}
              />
            ))}
          </ul>
        )}
        {!state.loading && !state.error && plugins.length === 0 && (
          <p className="text-[12px] text-muted">No plugin endpoints.</p>
        )}
        <RegisterForm onCreated={replace} />
      </div>
    </section>
  )
}
