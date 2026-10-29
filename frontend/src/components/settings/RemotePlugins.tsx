import { useState, type FormEvent, type ReactNode } from 'react'
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
import { ChevronRightIcon } from 'lucide-react'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'
import { ChoiceSelect, LoadingLines, SettingBody, SettingCard } from './form'
import { Spinner } from '../ui/spinner'

/**
 * Settings → Tools & plugins (#297, #464, #1953). First ScadBuddy's own tool sets (`built_in`): the
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
      <p className="text-xs text-muted-foreground">
        A tool&rsquo;s tier can be raised, never set below the tier ScadBuddy gives it.
      </p>
      <ToolTable flag="Enabled">
        {set.tools.map((tool) => (
          <tr key={tool.name} className="align-top">
            <td className="px-3 py-1.5">
              <span className="sb-num break-all">{tool.harness_name}</span>
            </td>
            <td className="px-3 py-1.5">
              <ChoiceSelect
                aria-label={`Tier of ${tool.name}`}
                size="sm"
                value={tiers[tool.name] ?? tool.risk}
                onValueChange={(raw) => {
                  const tier = raw as RiskTier
                  const { [tool.name]: _dropped, ...rest } = tiers
                  setTiers(tier === tool.risk ? rest : { ...rest, [tool.name]: tier })
                }}
                choices={TIERS.slice(TIERS.indexOf(tool.risk)).map((t) => ({ value: t, label: tierLabel(t) }))}
                className="w-auto min-w-36"
              />
            </td>
            <td className="px-3 py-1.5">
              <Switch
                size="sm"
                aria-label={`Enable ${tool.name}`}
                checked={!disabled.includes(tool.name)}
                onCheckedChange={(on) =>
                  setDisabled(on ? disabled.filter((d) => d !== tool.name) : [...disabled, tool.name])
                }
              />
            </td>
          </tr>
        ))}
      </ToolTable>
      <Button size="sm" variant="outline" onClick={() => void save()} disabled={busy} aria-busy={busy} {...USER_ONLY}>
        {busy && <Spinner />}
        Save tool settings
      </Button>
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

/** A plugin's tools, each with its tier and a switch: a table in a card. */
function ToolTable({ flag, children }: { flag: string; children: ReactNode }) {
  return (
    <Card size="sm" className="overflow-x-auto py-0">
      <table className="w-full text-left text-xs">
        <thead className="border-b text-muted-foreground">
          <tr>
            <th className="px-3 py-2 font-medium">Tool</th>
            <th className="px-3 py-2 font-medium">Tier</th>
            <th className="px-3 py-2 font-medium">{flag}</th>
          </tr>
        </thead>
        <tbody className="divide-y">{children}</tbody>
      </table>
    </Card>
  )
}

/** A summary line that folds what follows it; the body stays mounted while folded. */
function Fold({
  open,
  onOpenChange,
  summary,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  summary: ReactNode
  children: ReactNode
}) {
  return (
    <Collapsible open={open} onOpenChange={onOpenChange}>
      <CollapsibleTrigger className="inline-flex min-h-6 items-center gap-1.5 text-left">
        <ChevronRightIcon aria-hidden="true" className={cn('size-3.5 shrink-0 transition-transform', open && 'rotate-90')} />
        {summary}
      </CollapsibleTrigger>
      <CollapsibleContent forceMount hidden={!open}>
        {children}
      </CollapsibleContent>
    </Collapsible>
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
    <li aria-label={`Built-in tools ${set.name}`}>
      <Card size="sm">
        <CardContent>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{set.name}</span>
            <Badge variant="outline" className="text-muted-foreground">
              Built in
            </Badge>
            <Badge variant="outline" className={set.enabled ? 'border-ok/40 bg-ok/10 text-ok' : 'text-muted-foreground'}>
              {set.enabled ? 'Enabled' : 'Disabled'}
            </Badge>
          </div>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
            <dt className="text-muted-foreground">Tools</dt>
            <dd className="sb-num">
              {set.tool_prefix}* ({set.tools.length}
              {raised > 0 && `, ${raised} raised`}
              {set.disabled_tools.length > 0 && `, ${set.disabled_tools.length} disabled`})
            </dd>
          </dl>
          <Collapsible open={open} onOpenChange={setOpen}>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <CollapsibleTrigger asChild>
                <Button size="sm" variant="outline">
                  {open ? 'Hide tools' : 'Review tools'}
                </Button>
              </CollapsibleTrigger>
              {set.switchable && (
                <Button size="sm" variant="outline" onClick={() => void toggle()} disabled={busy} aria-busy={busy} {...USER_ONLY}>
                  {set.enabled ? 'Disable' : 'Enable'}
                </Button>
              )}
            </div>
            <CollapsibleContent>
              {/* Keyed by what is stored, so a newer copy of the set resets the table. */}
              <BuiltInToolTable key={JSON.stringify([set.tool_tiers, set.disabled_tools])} set={set} onSaved={onChange} />
            </CollapsibleContent>
          </Collapsible>
          {error && (
            <p role="alert" className="mt-2 text-xs text-warn">
              {error}
            </p>
          )}
        </CardContent>
      </Card>
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
          <Label htmlFor="remote-name" className="mb-1.5 font-normal">
            Name
          </Label>
          <Input
            id="remote-name"
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="hindsight"
            className="sb-num"
          />
        </div>
        <div>
          <Label htmlFor="remote-url" className="mb-1.5 font-normal">
            MCP endpoint URL
          </Label>
          <Input
            id="remote-url"
            type="url"
            required
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://memory.example/mcp/"
            className="sb-num"
          />
        </div>
        <div>
          <Label htmlFor="remote-header" className="mb-1.5 font-normal">
            Auth header
          </Label>
          <Input
            id="remote-header"
            value={header}
            onChange={(event) => setHeader(event.target.value)}
            className="sb-num"
          />
        </div>
        <div>
          <Label htmlFor="remote-secret" className="mb-1.5 font-normal">
            Header value
          </Label>
          <Input
            id="remote-secret"
            type="password"
            autoComplete="off"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder="Bearer … (optional)"
            className="sb-num"
          />
        </div>
      </div>
      <Button type="submit" disabled={busy} aria-busy={busy} {...USER_ONLY}>
        {busy && <Spinner />}
        Add endpoint
      </Button>
      {error && (
        <p role="alert" className="text-xs text-warn">
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
      <ToolTable flag="Shown">
        {test.tools.map((tool) => {
          const fixed = tool.tier_source === 'renamed' || tool.tier_source === 'collision'
          return (
            <tr key={tool.name} className="align-top">
              <td className="px-3 py-1.5">
                <span className="sb-num break-all">{tool.harness_name}</span>
                {tool.suggested_tier && (
                  <span className="ml-1.5 text-muted-foreground">(server suggests {tool.suggested_tier})</span>
                )}
                {fixed && (
                  <span className="block text-warn">
                    {tool.tier_source === 'renamed'
                      ? 'Renamed by Claude Code, so it stays outward.'
                      : `Shares a name with ${tool.collides_with.join(', ')}; hidden.`}
                  </span>
                )}
              </td>
              <td className="px-3 py-1.5">
                <ChoiceSelect
                  aria-label={`Tier of ${tool.name}`}
                  size="sm"
                  value={fixed ? 'outward' : (tiers[tool.name] ?? 'outward')}
                  disabled={fixed}
                  onValueChange={(tier) => setTiers({ ...tiers, [tool.name]: tier as RiskTier })}
                  choices={TIERS.map((t) => ({ value: t, label: tierLabel(t) }))}
                  className="w-auto min-w-36"
                />
              </td>
              <td className="px-3 py-1.5">
                <Switch
                  size="sm"
                  aria-label={`Show ${tool.name}`}
                  checked={!(hidden.includes(tool.name) || tool.tier_source === 'collision')}
                  disabled={tool.tier_source === 'collision'}
                  onCheckedChange={(shown) =>
                    setHidden(shown ? hidden.filter((h) => h !== tool.name) : [...hidden, tool.name])
                  }
                />
              </td>
            </tr>
          )
        })}
      </ToolTable>
      {test.truncated && <p className="text-xs text-warn">The server has more tools than one page listed.</p>}
      <Button size="sm" variant="outline" onClick={() => void save()} disabled={busy} aria-busy={busy} {...USER_ONLY}>
        {busy && <Spinner />}
        Save tool settings
      </Button>
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

/** A test's line: its result (with how many tools it listed) and the server it reached. */
function testSummary(test: PluginTest): string {
  return `${test.detail}${test.server ? ` (${test.server.name} ${test.server.version})` : ''}`
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
  // #2172 — the tool review folds to its summary line once its tiers are saved.
  const [reviewOpen, setReviewOpen] = useState(true)
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
    <li aria-label={`Plugin endpoint ${plugin.name}`}>
      <Card size="sm">
        <CardContent>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{plugin.name}</span>
            <Badge variant="outline" className={plugin.enabled ? 'border-ok/40 bg-ok/10 text-ok' : 'text-muted-foreground'}>
              {plugin.enabled ? 'Enabled' : 'Disabled'}
            </Badge>
            {!plugin.usable && (
              <Badge variant="outline" className="h-auto border-warn/40 bg-warn/10 whitespace-normal text-warn">
                Secret sealed with another key; save it again
              </Badge>
            )}
          </div>
          {plugin.problem && (
            <p role="alert" className="mt-1 text-xs text-warn">
              Not loaded: {plugin.problem}
            </p>
          )}
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
            <dt className="text-muted-foreground">Endpoint</dt>
            <dd className="sb-num break-all">{plugin.url}</dd>
            <dt className="text-muted-foreground">Auth</dt>
            <dd className="sb-num">{plugin.auth ? `${plugin.auth.header}: …${plugin.auth.last4}` : 'None'}</dd>
            <dt className="text-muted-foreground">Tools</dt>
            <dd className="sb-num">{plugin.tool_prefix}*</dd>
          </dl>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                void act('test', async () => {
                  setTest(await aiPlugins.testRemote(plugin.name))
                  setReviewOpen(true)
                })
              }
              disabled={busy !== null}
              aria-busy={busy === 'test'}
              {...USER_ONLY}
            >
              {busy === 'test' && <Spinner />}
              Test connection
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                void act('enable', async () => onChange(await aiPlugins.updateRemote(plugin.name, { enabled: !plugin.enabled })))
              }
              disabled={busy !== null}
              {...USER_ONLY}
            >
              {plugin.enabled ? 'Disable' : 'Enable'}
            </Button>
            <Button size="sm" variant="destructive" onClick={() => setConfirmDelete(true)} disabled={busy !== null} {...USER_ONLY}>
              Remove
            </Button>
          </div>
          {test && (
            <div className="mt-2 text-xs">
              {test.ok ? (
                // Folded, not unmounted: tiers changed and not yet saved are kept.
                <Fold
                  open={reviewOpen}
                  onOpenChange={setReviewOpen}
                  summary={
                    <span role="status" className="text-ok">
                      {testSummary(test)}
                    </span>
                  }
                >
                  <ToolReview
                    plugin={plugin}
                    test={test}
                    onSaved={(saved) => {
                      onChange(saved)
                      setReviewOpen(false)
                    }}
                  />
                </Fold>
              ) : (
                <p role="status" className="text-warn">
                  {testSummary(test)}
                </p>
              )}
            </div>
          )}
          {error && (
            <p role="alert" className="mt-2 text-xs text-warn">
              {error}
            </p>
          )}
          <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>{`Remove ${plugin.name}?`}</AlertDialogTitle>
                <AlertDialogDescription>
                  The endpoint and its stored secret are deleted. Sessions stop using its tools from their next turn.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                {/* Not an AlertDialogAction: the dialog closes once the delete is done. */}
                <Button
                  variant="destructive"
                  disabled={busy === 'delete'}
                  aria-busy={busy === 'delete'}
                  onClick={() =>
                    void act('delete', async () => {
                      await aiPlugins.deleteRemote(plugin.name)
                      setConfirmDelete(false)
                      onRemoved(plugin.name)
                    })
                  }
                  {...USER_ONLY}
                >
                  {busy === 'delete' && <Spinner />}
                  Remove endpoint
                </Button>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </CardContent>
      </Card>
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
    <SettingCard title="Plugins" headingId="plugin-endpoints-heading">
      <SettingBody>
        <p className="text-xs text-muted-foreground">
          ScadBuddy&rsquo;s own tools, built in: raise a tool&rsquo;s tier or disable it. Then remote MCP servers
          (Streamable HTTP over HTTPS) whose tools the assistant may use. Registered disabled: test the connection,
          review each tool&rsquo;s tier, then enable it.
        </p>
        {state.loading && <LoadingLines label="Loading plugins" lines={['h-16 w-full', 'h-16 w-full']} />}
        {state.error && (
          <p role="alert" className="text-xs text-warn">
            Plugins are unavailable: {state.error.message}
          </p>
        )}
        {builtIns.length > 0 && (
          <ul className="space-y-3" aria-label="Built-in tools">
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
          <p className="text-xs text-muted-foreground">No plugin endpoints.</p>
        )}
        <RegisterForm onCreated={replace} />
      </SettingBody>
    </SettingCard>
  )
}
