import { useEffect, useState, type FormEvent } from 'react'
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
}: {
  plugin: RemotePlugin
  /** The plugin's capabilities source, once read. */
  source: SourceView | undefined
  /** Why the source could not be read. */
  readError: string | undefined
  onSource: (source: SourceView) => void
  onRemoved: (name: string) => void
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
            <dd className="sb-num">{plugin.auth ? `${plugin.auth.header}: …${plugin.auth.last4}` : 'None'}</dd>
            <dt className="text-muted-foreground">Tools</dt>
            <dd className="sb-num">{plugin.tool_prefix}*</dd>
          </dl>
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
