import { useEffect, useRef, useState, type FormEvent } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { aiPlugins, type RemotePlugin } from '../../api/aiPlugins'
import { aiCapabilities, type RefreshResult, type SourceView } from '../../api/capabilities'
import { ApiError } from '../../api/client'
import { useAsync } from '../../lib/useAsync'
import { SourceCard } from '../capabilities/SourceCard'
import { useCapabilities } from '../capabilities/useCapabilities'
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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { LoadingLines, SettingBody, SettingCard } from './form'
import { Spinner } from '../ui/spinner'

/**
 * Settings → Tools & plugins → Remote MCP plugins (#297, #464, #2171): remote MCP servers the
 * assistant reaches through the agent's loopback forwarder (agent/src/routes/plugins.ts). The
 * secret is write-only: the server shows the header name and the value's last four characters.
 *
 * A plugin is registered disabled. "Test connection" lists its tools again (the capabilities
 * `refresh`); each tool is a shared row (`SourceCard`, `ItemRow`) whose access applies at once,
 * `outward` (asks before it acts) by default, `off` in place of the old Hidden column. A server's
 * `readOnlyHint` is only shown as a suggestion, as MCP says annotations from untrusted servers are
 * untrusted. The endpoint's own controls, which handle its secret, are user-only; a tool's access
 * and the plugin's switch are not (#4208).
 *
 * A plugin without a header can be signed in instead (#780): "Sign in" opens the agent's one-use
 * link in a new window, where the person signs in at the plugin's authorization server; the agent
 * keeps the tokens (sealed) and the forwarder adds a fresh one per request, so neither this page
 * nor Claude Code ever holds one. The card polls the plugin until the sign-in lands. A sign-in the
 * server later refused reads "Sign in again". A server without dynamic client registration (GitHub)
 * needs the person's own OAuth app: its client id and secret, with the callback URL shown.
 */

const REMOTE = (source: SourceView) => source.kind === 'remote'
const sourceId = (name: string) => `remote:${name}`

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
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

/** How often, and for how long, the card looks for a sign-in finished in its window. */
const SIGN_IN_POLL_MS = 2_000
const SIGN_IN_WAIT_MS = 10 * 60_000

function SignIn({ plugin, onPlugin }: { plugin: RemotePlugin; onPlugin: (plugin: RemotePlugin) => void }) {
  const [ownApp, setOwnApp] = useState(false)
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [busy, setBusy] = useState<'start' | 'out' | null>(null)
  /** The `connected_at` before the window opened, while the card waits for a new one. */
  const [waitingFrom, setWaitingFrom] = useState<string | null | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)
  const callback = `${window.location.origin}/api/v1/ai/plugin-oauth/callback`
  const waiting = waitingFrom !== undefined
  // The latest callback, so a render does not restart the poll.
  const onPluginRef = useRef(onPlugin)
  useEffect(() => {
    onPluginRef.current = onPlugin
  }, [onPlugin])

  useEffect(() => {
    if (waitingFrom === undefined) return
    const started = Date.now()
    const timer = window.setInterval(() => {
      if (Date.now() - started > SIGN_IN_WAIT_MS) {
        window.clearInterval(timer)
        setWaitingFrom(undefined)
        return
      }
      aiPlugins.getRemote(plugin.name).then(
        (next) => {
          if (next.oauth && next.oauth.connected_at !== waitingFrom) {
            window.clearInterval(timer)
            setWaitingFrom(undefined)
            onPluginRef.current(next)
          }
        },
        () => {},
      )
    }, SIGN_IN_POLL_MS)
    return () => window.clearInterval(timer)
  }, [waitingFrom, plugin.name])

  async function start() {
    // Opened in the click, before the request: a window opened after an await is a blocked pop-up.
    const win = window.open('', '_blank')
    setBusy('start')
    setError(null)
    try {
      const started = await aiPlugins.startSignIn(plugin.name, {
        ...(ownApp && clientId.trim() ? { client_id: clientId.trim() } : {}),
        ...(ownApp && clientId.trim() && clientSecret ? { client_secret: clientSecret } : {}),
      })
      if (!win) throw new Error('The sign-in window was blocked: allow pop-ups for ScadBuddy and try again.')
      // The authorization server's page must not reach back into this one.
      win.opener = null
      win.location.href = started.authorize_url
      setClientSecret('')
      setWaitingFrom(plugin.oauth?.connected_at ?? null)
    } catch (caught) {
      win?.close()
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  async function signOut() {
    setBusy('out')
    setError(null)
    try {
      await aiPlugins.signOut(plugin.name)
      onPlugin(await aiPlugins.getRemote(plugin.name))
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(null)
    }
  }

  const failed = plugin.oauth?.status === 'error'
  return (
    <div className="mt-2 space-y-2" aria-label={`Sign-in for ${plugin.name}`} role="group">
      {failed && (
        <p role="alert" className="text-xs text-warn">
          Sign-in needs renewing: {plugin.oauth?.error ?? 'the server refused it'}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => void start()} disabled={busy !== null} aria-busy={busy === 'start'} {...USER_ONLY}>
          {busy === 'start' && <Spinner />}
          {plugin.oauth ? 'Sign in again' : 'Sign in'}
        </Button>
        {plugin.oauth && (
          <Button size="sm" variant="ghost" onClick={() => void signOut()} disabled={busy !== null} aria-busy={busy === 'out'} {...USER_ONLY}>
            {busy === 'out' && <Spinner />}
            Sign out
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => setOwnApp(!ownApp)} aria-expanded={ownApp} {...USER_ONLY}>
          {ownApp ? 'Use registration' : 'Use my own OAuth app'}
        </Button>
      </div>
      {waiting && (
        <p role="status" className="text-xs text-muted-foreground">
          Finish signing in in the window that opened…
        </p>
      )}
      {ownApp && (
        <div className="grid gap-3 sm:grid-cols-2" {...USER_ONLY}>
          <p className="text-xs text-muted-foreground sm:col-span-2">
            For a server without dynamic registration (GitHub): register an OAuth app (a GitHub App gives expiring
            tokens limited to the repositories it is installed on) with the callback URL{' '}
            <span className="sb-num break-all">{callback}</span>
          </p>
          <div>
            <Label htmlFor={`oauth-client-${plugin.name}`} className="mb-1.5 font-normal">
              Client ID
            </Label>
            <Input id={`oauth-client-${plugin.name}`} value={clientId} onChange={(event) => setClientId(event.target.value)} className="sb-num" />
          </div>
          <div>
            <Label htmlFor={`oauth-secret-${plugin.name}`} className="mb-1.5 font-normal">
              Client secret
            </Label>
            <Input
              id={`oauth-secret-${plugin.name}`}
              type="password"
              autoComplete="off"
              value={clientSecret}
              onChange={(event) => setClientSecret(event.target.value)}
              placeholder="(optional)"
              className="sb-num"
            />
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

/** The Auth line: the header and its last four, the sign-in, or none. */
function authLine(plugin: RemotePlugin): string {
  if (plugin.auth) return `${plugin.auth.header}: …${plugin.auth.last4}`
  if (plugin.oauth?.status === 'connected') return `Signed in with ${plugin.oauth.issuer}`
  if (plugin.oauth) return `Sign-in failed (${plugin.oauth.issuer})`
  return 'None'
}

/** A test's line: its result (with how many tools it listed) and the server it reached. */
function testSummary(test: RefreshResult['test']): string {
  return `${test.detail}${test.server ? ` (${test.server.name} ${test.server.version})` : ''}`
}

function RemoteCard({
  plugin,
  source,
  readError,
  onSource,
  onRemoved,
  onPlugin,
}: {
  plugin: RemotePlugin
  /** The plugin's capabilities source, once read. */
  source: SourceView | undefined
  /** Why the source could not be read. */
  readError: string | undefined
  onSource: (source: SourceView) => void
  onRemoved: (name: string) => void
  /** The plugin as a sign-in or sign-out left it. */
  onPlugin: (plugin: RemotePlugin) => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [test, setTest] = useState<RefreshResult['test'] | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const enabled = source?.enabled ?? plugin.enabled

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

  const actions = (
    <>
      <Button
        size="sm"
        variant="outline"
        onClick={() =>
          void act('test', async () => {
            const result = await aiCapabilities.refresh(sourceId(plugin.name))
            onSource(result.source)
            setTest(result.test)
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
        variant="destructive"
        onClick={() => setConfirmDelete(true)}
        disabled={busy !== null}
        {...USER_ONLY}
      >
        Remove
      </Button>
    </>
  )

  return (
    <li aria-label={`Plugin endpoint ${plugin.name}`}>
      <Card size="sm">
        <CardContent>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{plugin.name}</span>
            <Badge variant="outline" className={enabled ? 'border-ok/40 bg-ok/10 text-ok' : 'text-muted-foreground'}>
              {enabled ? 'Enabled' : 'Disabled'}
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
            <dd className="sb-num">{authLine(plugin)}</dd>
            <dt className="text-muted-foreground">Tools</dt>
            <dd className="sb-num">{plugin.tool_prefix}*</dd>
          </dl>
          {!plugin.auth && <SignIn plugin={plugin} onPlugin={onPlugin} />}
          {test && (
            // #3048: the test's line; the rows it listed stay folded under the source's summary.
            <p role="status" className={test.ok ? 'mt-2 text-xs text-ok' : 'mt-2 text-xs text-warn'}>
              {testSummary(test)}
            </p>
          )}
          {source?.truncated && (
            <p className="mt-1 text-xs text-warn">The stored list may not be every tool the server has.</p>
          )}
          {!source && readError && (
            <p role="alert" className="mt-2 text-xs text-warn">
              Could not read the plugin&rsquo;s tools: {readError}
            </p>
          )}
          {error && (
            <p role="alert" className="mt-2 text-xs text-warn">
              {error}
            </p>
          )}
          <div className="mt-3">
            {source ? (
              <SourceCard source={source} onChange={onSource} headerExtra={actions} />
            ) : (
              // Until the source is read (or when it cannot be), the endpoint's own buttons stand alone.
              <div className="flex flex-wrap items-center gap-2">{actions}</div>
            )}
          </div>
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
  const plugins = (state.data ?? []).filter((p): p is RemotePlugin => p.built_in !== true)
  const caps = useCapabilities(REMOTE)
  /**
   * Sources newer than the tree's first read, by id: each write's answer, and the source of a
   * plugin registered since. They take precedence, so a write that lands before the tree does
   * is not undone by it.
   */
  const [own, setOwn] = useState<Record<string, SourceView>>({})
  const onSource = (source: SourceView) => setOwn((sources) => ({ ...sources, [source.id]: source }))
  /** Why a plugin's own source read failed, by name; Test connection reads it again. */
  const [readErrors, setReadErrors] = useState<Record<string, string>>({})
  const sourceOf = (name: string) => own[sourceId(name)] ?? caps.sources.find((s) => s.id === sourceId(name))
  const unread = caps.loading || caps.error ? [] : plugins.map((p) => p.name).filter((name) => sourceOf(name) === undefined)
  const unreadKey = unread.join('/')

  useEffect(() => {
    if (unreadKey === '') return
    // Plugin names are lower-case letters, digits and hyphens, so "/" joins them safely.
    for (const name of unreadKey.split('/')) {
      aiCapabilities.source(sourceId(name)).then(onSource, (caught: unknown) =>
        setReadErrors((errors) => ({ ...errors, [name]: message(caught) })),
      )
    }
  }, [unreadKey])

  const onPlugin = (next: RemotePlugin) => state.setData((state.data ?? []).map((p) => (p.name === next.name ? next : p)))

  const onRemoved = (name: string) => {
    state.setData((state.data ?? []).filter((p) => p.name !== name))
    setOwn(({ [sourceId(name)]: _gone, ...rest }) => rest)
  }

  return (
    <SettingCard title="Plugins" headingId="plugin-endpoints-heading">
      <SettingBody>
        <p className="text-xs text-muted-foreground">
          Remote MCP servers (Streamable HTTP over HTTPS) whose tools the assistant may use. Registered disabled: test
          the connection, set each tool&rsquo;s access, then enable it.
        </p>
        {state.loading && <LoadingLines label="Loading plugins" lines={['h-16 w-full', 'h-16 w-full']} />}
        {state.error && (
          <p role="alert" className="text-xs text-warn">
            Plugins are unavailable: {state.error.message}
          </p>
        )}
        {/* A 503 is the agent without its database: `GET /plugins` says so already. */}
        {caps.error && !state.error && !(caps.error instanceof ApiError && caps.error.status === 503) && (
          <p role="alert" className="text-xs text-warn">
            Could not load the plugins&rsquo; tools: {caps.error.message}
          </p>
        )}
        {caps.loading && plugins.length > 0 && (
          <p role="status" className="text-xs text-muted-foreground">
            Loading the plugins&rsquo; tools…
          </p>
        )}
        {plugins.length > 0 && (
          <ul className="space-y-3" aria-label="Plugin endpoints">
            {plugins.map((plugin) => (
              <RemoteCard
                key={plugin.name}
                plugin={plugin}
                source={sourceOf(plugin.name)}
                readError={readErrors[plugin.name]}
                onSource={onSource}
                onRemoved={onRemoved}
                onPlugin={onPlugin}
              />
            ))}
          </ul>
        )}
        {!state.loading && !state.error && plugins.length === 0 && (
          <p className="text-xs text-muted-foreground">No plugin endpoints.</p>
        )}
        <RegisterForm onCreated={(plugin) => state.setData([...(state.data ?? []), plugin])} />
      </SettingBody>
    </SettingCard>
  )
}
