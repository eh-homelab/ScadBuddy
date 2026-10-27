import { useState } from 'react'
import { ChevronRightIcon } from 'lucide-react'
import { USER_ONLY } from '../../../agent/dom'
import { api, ApiError } from '../../../api/client'
import type { ConnectionTest, SidebarLink } from '../../../api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { Spinner } from '../../../components/ui/spinner'
import { syncSummary } from '../../../lib/printerRegistry'
import { useSettingsConnectionAgent, useSettingsAgentField } from '../agentFields'
import { useBambuddyResources } from '../bambuddyResources'
import { useSettingsCache } from '../cache'
import { FieldRow } from '../controls'
import { FieldBadge, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'connection'
export const area: AreaId = 'general'
export const title = 'Connection'
export const order = 10

/** How ScadBuddy reaches Bambuddy and Bambuddy reaches ScadBuddy, its test and the sidebar entry. */
export function Component() {
  const cache = useSettingsCache()
  const { connected, targets, projects, status } = useBambuddyResources()
  const form = useBackendSettings(id, (s) => fieldsOf(id, s), {
    publish: ['bambuddy_url'],
    // A Connection save that loses the Bambuddy URL takes the store back to local with it,
    // or the server would refuse the save (it never keeps an unready Bambuddy store).
    // Only this save's own change counts: another section's unsaved draft does not.
    alsoSend: ({ changed, value, settings }) =>
      changed('bambuddy_url') &&
      value('bambuddy_url') === '' &&
      settings.store_backend === 'bambuddy' &&
      // ScadBuddy's own library cannot sit on the local store: the save goes as it is, the
      // server's refusal is shown, and the user picks s3 first.
      settings.library_backend !== 'native'
        ? { store_backend: 'local' }
        : {},
    // Resetting the URL the Bambuddy store needs takes the store back to local with it.
    alsoReset: (name, s) =>
      name === 'bambuddy_url' && s.store_backend === 'bambuddy' && s.library_backend !== 'native'
        ? { store_backend: 'local' }
        : {},
    onSaved: () => {
      targets.reload()
      projects.reload()
    },
    onReset: (name) => {
      if (name === 'bambuddy_url' || name === 'bambuddy_api_key') {
        targets.reload()
        projects.reload()
      }
    },
  })
  const { settings, value, setField, errors, clearing, clearSecret } = form

  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<ConnectionTest | null>(null)
  // #4439: what the printer import did after a save whose test passed, or why it could not.
  const [imported, setImported] = useState<string | null>(null)
  const [registering, setRegistering] = useState(false)
  const [sidebar, setSidebar] = useState<SidebarLink | null>(null)
  const [error, setError] = useState<string | null>(null)

  useSettingsAgentField('bambuddy_url', {
    value: () => value('bambuddy_url'),
    set: (next) => setField('bambuddy_url', next),
  })
  useSettingsAgentField('public_url', {
    value: () => value('public_url'),
    set: (next) => setField('public_url', next),
  })
  useSettingsConnectionAgent({
    // Test connection minus its save: the agent tests only with nothing unsaved.
    testStored: async () => {
      setTesting(true)
      setError(null)
      setTest(null)
      try {
        const result = await cache.own(() => api.testSettings())
        setTest(result)
        return result
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.detail : 'The connection test could not run.')
        throw cause
      } finally {
        setTesting(false)
      }
    },
    lastTest: () => test,
    error: () => error,
    apiKeyTyped: () => value('bambuddy_api_key').length > 0,
  })

  async function runTest() {
    setTesting(true)
    setError(null)
    setTest(null)
    try {
      // The server tests what it has stored, so save first or the test lags the form.
      const result = await cache.own(async () => ((await form.save()) ? api.testSettings() : null))
      if (result === null) return
      setTest(result)
      status.reload()
      setImported(null)
      // #4439: a connection that tests OK imports and links Bambuddy's printers, as the
      // server's own Schedule would within minutes. The agent's test_connection does not:
      // it is a read, and the import stores access codes.
      if (result.ok) {
        api
          .syncPrintersFromBambuddy()
          .then((sync) => setImported(`Printers from Bambuddy: ${syncSummary(sync)}.`))
          .catch((cause: unknown) =>
            setImported(
              `Printers from Bambuddy were not imported: ${cause instanceof ApiError ? cause.detail : 'the import failed'}.`,
            ),
          )
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The connection test could not run.')
    } finally {
      setTesting(false)
    }
  }

  async function addSidebar() {
    setRegistering(true)
    setError(null)
    try {
      setSidebar(await api.registerSidebar())
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not register the sidebar entry.')
    } finally {
      setRegistering(false)
    }
  }

  const bambuddy = status.data

  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description="How ScadBuddy reaches Bambuddy, and how Bambuddy reaches ScadBuddy."
    >
      {error && (
        <p role="alert" className="text-sm text-warn">
          {error}
        </p>
      )}

      <FieldRow id="bambuddy-url" label="Bambuddy URL" badge={<FieldBadge form={form} name="bambuddy_url" />} error={errors.bambuddy_url}>
        <Input
          id="bambuddy-url"
          type="url"
          value={value('bambuddy_url')}
          onChange={(event) => setField('bambuddy_url', event.target.value)}
          placeholder="https://bambuddy.internal.example"
          className="sb-num"
        />
      </FieldRow>

      <FieldRow
        id="bambuddy-web-urls"
        label="Bambuddy web URLs"
        badge={<FieldBadge form={form} name="bambuddy_web_urls" />}
        error={errors.bambuddy_web_urls}
        help="Where browsers reach Bambuddy, when the URL above is one only ScadBuddy's server can. Comma-separated: links use the first, or whichever of them ScadBuddy is opened inside."
      >
        <Input
          id="bambuddy-web-urls"
          type="text"
          value={value('bambuddy_web_urls')}
          onChange={(event) => setField('bambuddy_web_urls', event.target.value)}
          placeholder="https://bambuddy.example, https://bambuddy.lan"
          className="sb-num"
        />
      </FieldRow>

      <FieldRow
        id="bambuddy-key"
        label="API key"
        badge={<FieldBadge form={form} name="bambuddy_api_key" />}
        error={errors.bambuddy_api_key}
        help="Needs Read Status, Manage Library and Manage Queue; Manage Projects for projects, Manage Archives for print photos and Manage Inventory to record a tray's spool."
      >
        <div className="flex gap-2">
          <Input
            id="bambuddy-key"
            type="password"
            value={value('bambuddy_api_key')}
            autoComplete="off"
            onChange={(event) => setField('bambuddy_api_key', event.target.value)}
            placeholder={
              clearing.includes('bambuddy_api_key')
                ? 'Cleared when you save.'
                : settings.has_api_key
                  ? 'A key is stored. Paste a new one to replace it.'
                  : 'Paste the key'
            }
            className="sb-num"
          />
          {settings.has_api_key && (
            <Button
              variant="outline"
              disabled={clearing.includes('bambuddy_api_key')}
              onClick={() => clearSecret('bambuddy_api_key')}
              {...USER_ONLY}
            >
              Remove key
            </Button>
          )}
        </div>
      </FieldRow>

      <FieldRow
        id="bambuddy-render-key"
        label="Render key"
        badge={<FieldBadge form={form} name="bambuddy_render_api_key" />}
        error={errors.bambuddy_render_api_key}
        help="A second key with Manage Library only. Render workers run template code and hold this key alone."
      >
        <div className="flex gap-2">
          <Input
            id="bambuddy-render-key"
            type="password"
            value={value('bambuddy_render_api_key')}
            autoComplete="off"
            onChange={(event) => setField('bambuddy_render_api_key', event.target.value)}
            placeholder={
              clearing.includes('bambuddy_render_api_key')
                ? 'Cleared when you save.'
                : settings.has_render_api_key
                  ? 'A key is stored. Paste a new one to replace it.'
                  : 'Paste the key'
            }
            className="sb-num"
          />
          {settings.has_render_api_key && (
            <Button
              variant="outline"
              disabled={clearing.includes('bambuddy_render_api_key')}
              onClick={() => clearSecret('bambuddy_render_api_key')}
              {...USER_ONLY}
            >
              Remove key
            </Button>
          )}
        </div>
        {settings.render_key_fallback && (
          <p
            role="status"
            data-testid="render-key-fallback"
            className="mt-1.5 rounded-lg bg-warn/10 px-2.5 py-2 text-xs ring-1 ring-warn/50"
          >
            Render workers hold the full Bambuddy key; template code can print. Create a key with only
            Manage Library in Bambuddy and paste it above as the render key.
          </p>
        )}
      </FieldRow>

      <FieldRow
        id="public-url"
        label="ScadBuddy’s own URL"
        badge={<FieldBadge form={form} name="public_url" />}
        error={errors.public_url}
        help="What Bambuddy links to. ScadBuddy cannot infer it — it sits behind a proxy."
      >
        <Input
          id="public-url"
          type="url"
          value={value('public_url')}
          onChange={(event) => setField('public_url', event.target.value)}
          placeholder="https://scadbuddy.internal.example"
          className="sb-num"
        />
      </FieldRow>

      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            onClick={() => void runTest()}
            disabled={testing}
            aria-busy={testing}
            data-testid="test-connection"
            {...USER_ONLY}
          >
            {testing && <Spinner />}
            Test connection
          </Button>
          {test && (
            <Badge
              role="status"
              variant="outline"
              className={cn('h-auto whitespace-normal', test.ok ? 'border-ok/40 text-ok' : 'border-warn/40 text-warn')}
            >
              {test.detail}
            </Badge>
          )}
        </div>
        {test?.upstream && <UpstreamAnswerDetails answer={test.upstream} />}
        {test?.ok && imported && (
          <p role="status" aria-label="Printer import" className="text-xs text-muted-foreground">
            {imported}
          </p>
        )}
        {test && (test.scopes?.length ?? 0) > 0 && (
          <ul aria-label="Scopes" className="space-y-1 rounded-lg p-3 text-xs ring-1 ring-foreground/10">
            {test.scopes?.map((check) => (
              <li key={check.scope} className="flex gap-2">
                <span
                  aria-hidden
                  className={
                    check.status === 'ok'
                      ? 'text-ok'
                      : check.required && check.status !== 'unknown'
                        ? 'text-warn'
                        : 'text-muted-foreground'
                  }
                >
                  {check.status === 'ok' ? '✓' : check.status === 'missing' ? '✗' : '?'}
                </span>
                <span>
                  <span className="font-medium">{check.scope}</span>
                  {!check.required && <span className="text-muted-foreground"> (optional)</span>}
                  {': '}
                  <span className="text-muted-foreground">
                    {check.status === 'ok'
                      ? 'granted. '
                      : check.status === 'missing'
                        ? 'missing. '
                        : check.status === 'unknown'
                          ? 'not checked. '
                          : 'could not check. '}
                    {check.detail}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {connected && (
        <Card size="sm" className="text-xs" data-testid="bambuddy-status">
          <CardContent>
            {status.loading ? (
              <div role="status" aria-busy="true" className="space-y-2">
                <span className="sr-only">Reading Bambuddy</span>
                <Skeleton className="h-3.5 w-1/2" />
                <Skeleton className="h-3.5 w-5/6" />
              </div>
            ) : status.error ? (
              <span className="text-muted-foreground">
                Could not read Bambuddy:{' '}
                {status.error instanceof ApiError ? status.error.detail : 'no answer'}
              </span>
            ) : (
              <div className="space-y-1">
                <p>
                  Finish photo in Bambuddy:{' '}
                  <span className={bambuddy?.capture_finish_photo === false ? 'text-warn' : ''}>
                    {bambuddy?.capture_finish_photo === true
                      ? 'on'
                      : bambuddy?.capture_finish_photo === false
                        ? 'off'
                        : 'unknown'}
                  </span>
                  {bambuddy?.capture_finish_photo !== true && bambuddy?.settings_url && (
                    <>
                      {' · '}
                      <a
                        href={bambuddy.settings_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-primary underline-offset-2 hover:underline"
                      >
                        Turn it on in Bambuddy
                      </a>
                    </>
                  )}
                </p>
                <p className="text-muted-foreground">
                  Bambuddy photographs a print when it finishes, and print history shows the photo.
                  ScadBuddy only reads this setting.{bambuddy?.detail ? ` ${bambuddy.detail}` : ''}
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <div className="border-t pt-4">
        <p className="text-sm text-muted-foreground">
          Adds ScadBuddy to Bambuddy&rsquo;s sidebar as an External Link called &ldquo;ScadBuddy&rdquo;,
          opening inside Bambuddy rather than a new tab, at ScadBuddy&rsquo;s own URL. Running it again
          updates the existing entry.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={() => void addSidebar()} disabled={registering} aria-busy={registering} {...USER_ONLY}>
            {registering && <Spinner />}
            Add to Bambuddy sidebar
          </Button>
          {sidebar && (
            <p role="status" className="text-xs text-ok">
              {sidebar.created ? 'Added to the sidebar' : 'Updated the sidebar entry'} at{' '}
              <span className="sb-num">{sidebar.embed_path}</span>.
            </p>
          )}
        </div>
      </div>
    </SavedSection>
  )
}

/** #1542 — what Bambuddy itself answered a failed connection test, as it said it. */
function UpstreamAnswerDetails({ answer }: { answer: NonNullable<ConnectionTest['upstream']> }) {
  const [open, setOpen] = useState(false)
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="rounded-lg text-xs ring-1 ring-foreground/10">
      <CollapsibleTrigger className="flex min-h-8 w-full items-center gap-1.5 rounded-lg px-3 py-1.5 text-left text-muted-foreground hover:bg-muted/50 hover:text-foreground">
        <ChevronRightIcon aria-hidden="true" className={cn('size-3.5 shrink-0 transition-transform', open && 'rotate-90')} />
        What Bambuddy answered
      </CollapsibleTrigger>
      <CollapsibleContent forceMount hidden={!open} className="border-t px-3 py-2">
        <dl className="space-y-1">
          {answer.status != null && (
            <div className="flex gap-2">
              <dt className="text-muted-foreground">Status</dt>
              <dd>{answer.status}</dd>
            </div>
          )}
          {answer.retry_after != null && (
            <div className="flex gap-2">
              <dt className="text-muted-foreground">Retry-After</dt>
              <dd>{answer.retry_after}</dd>
            </div>
          )}
          {answer.detail != null && (
            <div className="flex gap-2">
              <dt className="text-muted-foreground">Its message</dt>
              <dd className="break-all">{answer.detail}</dd>
            </div>
          )}
          {answer.error != null && (
            <div className="flex gap-2">
              <dt className="text-muted-foreground">Error</dt>
              <dd className="break-all">{answer.error}</dd>
            </div>
          )}
        </dl>
      </CollapsibleContent>
    </Collapsible>
  )
}
