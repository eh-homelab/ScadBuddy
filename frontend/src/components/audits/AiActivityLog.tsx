import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import {
  AUDIT_KINDS,
  AUDIT_OUTCOMES,
  type AuditEntry,
  type AuditKind,
  type AuditOutcome,
  fetchAudit,
  MAX_RETENTION_DAYS,
  MIN_RETENTION_DAYS,
  saveAuditRetention,
} from '../../agent/audit'
import { useAiAvailability } from '../../agent/chat/availability'
import { downloadBlob } from '../../lib/embed'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tip } from '../assistant/Tip'
import { Spinner } from '../ui/spinner'

/**
 * Settings → Audits → AI activity (#258): the agent service's audit log of AI actions
 * (`GET /api/v1/ai/audit`), newest first, filterable by kind and outcome, with the
 * retention setting. Hidden when AI is off, like the rest of the assistant.
 */
export function AiActivityLog() {
  const ai = useAiAvailability()
  if (!ai.available) return null
  return <AuditLogView />
}

export const AUDIT_PAGE = 25

/** The filters' "All": Radix's Select takes no empty value. */
const ALL = 'all'

const KIND_LABEL: Record<AuditKind, string> = {
  tool_call: 'Tool calls',
  // #2373 — the panel's session actions (fork, rename, archive) share this kind.
  resource: 'Sessions & resources',
  approval: 'Approvals',
  credential: 'Credentials',
  plugin: 'Plugins',
  settings: 'Settings',
  token: 'MCP tokens',
  memory: 'Memory',
  http: 'HTTP requests',
  question: 'Questions',
  turn: 'Turns',
}

const OUTCOME_LABEL: Record<AuditOutcome, string> = {
  ok: 'OK',
  error: 'Error',
  refused: 'Refused',
  denied: 'Denied',
}

const SURFACE_LABEL: Record<AuditEntry['surface'], string> = {
  harness: 'assistant',
  mcp: 'MCP',
  // #2373 — every browser route: Settings, and the panel's approvals, answers and sessions.
  http: 'the app',
  system: 'ScadBuddy',
}

/** #2373 — a session action recorded as a resource row. */
const SESSION_ACTION: Record<string, string> = {
  session_fork: 'Session fork',
  session_update: 'Session update',
}

/** `mcp__scadbuddy__print_output` → `print_output`; other names as they are. */
function toolName(action: string): string {
  return action.replace(/^mcp__scadbuddy__/, '')
}

function describe(entry: AuditEntry): string {
  switch (entry.kind) {
    case 'tool_call':
      return toolName(entry.action)
    case 'approval':
      return `Approval ${entry.action}`
    case 'resource':
      return SESSION_ACTION[entry.action] ?? `Resource ${entry.action}`
    case 'memory':
      return entry.action === 'recall' ? 'Memory recall' : entry.action === 'retain' ? 'Memory save' : `Memory ${entry.action}`
    case 'http':
      return `HTTP ${entry.action}`
    case 'question':
      return `Question ${entry.action}`
    case 'turn':
      return `Turn ${entry.action}`
    default:
      return `${KIND_LABEL[entry.kind]}: ${entry.action}`
  }
}

function outcomeClass(outcome: AuditOutcome): string {
  return outcome === 'ok' ? 'text-ok' : 'text-warn'
}

function when(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

/**
 * A memory row's summary (#818) in words: the agent stores `{bank, document_id?,
 * results?, tool?}` as JSON, never the query or a memory.
 */
function memoryText(entry: AuditEntry): string | null {
  let parts: string[] = []
  try {
    const s = JSON.parse(entry.input_summary ?? '{}') as Record<string, unknown>
    if (typeof s.bank === 'string') parts.push(`bank ${s.bank}`)
    if (typeof s.results === 'number') parts.push(`${s.results} ${s.results === 1 ? 'memory' : 'memories'}`)
    if (typeof s.document_id === 'string') parts.push(s.document_id)
    if (typeof s.tool === 'string') parts.push(`result of ${toolName(s.tool)}`)
  } catch {
    parts = entry.input_summary ? [entry.input_summary] : []
  }
  if (entry.detail) parts.push(entry.detail)
  return parts.length ? parts.join(' · ') : null
}

/**
 * An http row's summary (#827) in words: the agent stores `{method, scheme, host,
 * status?, size_bytes?, redirect?, saved?}` as JSON, never a path, a header or a
 * body (`saved` is the id of a saved body; see savedBodyUrl).
 */
function httpText(entry: AuditEntry): string | null {
  let parts: string[] = []
  try {
    const s = JSON.parse(entry.input_summary ?? '{}') as Record<string, unknown>
    if (typeof s.host === 'string') parts.push(typeof s.scheme === 'string' ? `${s.scheme}://${s.host}` : s.host)
    if (typeof s.status === 'number') parts.push(`HTTP ${s.status}`)
    if (typeof s.size_bytes === 'number') parts.push(`${s.size_bytes} bytes`)
    if (typeof s.redirect === 'number') parts.push(`redirect ${s.redirect}`)
  } catch {
    parts = entry.input_summary ? [entry.input_summary] : []
  }
  if (entry.detail) parts.push(entry.detail)
  return parts.length ? parts.join(' · ') : null
}

const SAVED_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

/**
 * #1292 — where a saved response body is served, for an http row whose response
 * was too long (or binary) to return inline: the row names the id it was saved
 * under, and the agent serves it from the session's directory
 * (`GET /api/v1/ai/sessions/:id/http/:saved`). Null for any other row.
 */
function savedBodyUrl(entry: AuditEntry): string | null {
  if (entry.kind !== 'http' || !entry.session_id) return null
  let saved: unknown
  try {
    saved = (JSON.parse(entry.input_summary ?? '{}') as Record<string, unknown>).saved
  } catch {
    return null
  }
  if (typeof saved !== 'string' || !SAVED_ID.test(saved)) return null
  return `/api/v1/ai/sessions/${encodeURIComponent(entry.session_id)}/http/${saved}`
}

const usd = (value: number) => `$${value.toFixed(4)}`

/**
 * A turn row's cost (#1922). A turn Claude Code never priced (no result came back)
 * is flagged, with ScadBuddy's own estimate when it made one, never shown as a
 * priced $0.
 */
function turnCost(entry: AuditEntry): string | null {
  if (entry.kind !== 'turn' || entry.cost_usd === null) return null
  const estimated = entry.cost_estimated_usd ?? 0
  if (entry.cost_priced === false) {
    return estimated > 0 ? `not priced by Claude Code · ~${usd(estimated)} estimated` : 'not priced by Claude Code'
  }
  return estimated > 0 ? `${usd(entry.cost_usd)} (incl. ~${usd(estimated)} estimated)` : usd(entry.cost_usd)
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  const cost = turnCost(entry)
  const body = savedBodyUrl(entry)
  const text =
    entry.kind === 'memory'
      ? memoryText(entry)
      : entry.kind === 'http'
        ? httpText(entry)
        : (entry.detail ?? entry.input_summary)
  return (
    <li className="border-b border-border px-4 py-2 text-[13px] last:border-b-0" data-testid="audit-entry">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="font-medium">{describe(entry)}</span>
        {entry.tier && <span className="text-xs text-muted-foreground">{entry.tier}</span>}
        <span className={`text-[12px] font-medium ${outcomeClass(entry.outcome)}`}>
          {OUTCOME_LABEL[entry.outcome]}
        </span>
        <span className="sb-num ml-auto text-xs text-muted-foreground">{when(entry.at)}</span>
      </div>
      <div className="mt-0.5 text-xs text-muted-foreground">
        {entry.actor.label} via {SURFACE_LABEL[entry.surface]}
        {entry.client_ip && <> · <span className="sb-num">{entry.client_ip}</span></>}
        {entry.duration_ms !== null && <> · <span className="sb-num">{entry.duration_ms} ms</span></>}
        {entry.approval_id && <> · approval <span className="sb-num">{entry.approval_id.slice(0, 8)}</span></>}
        {entry.approved_by && <> · approved by {entry.approved_by.label}</>}
        {cost && <> · <span className="sb-num">{cost}</span></>}
      </div>
      {text && (
        <Tip content={text}>
          <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">{text}</p>
        </Tip>
      )}
      {body && <SavedBody url={body} />}
    </li>
  )
}

/**
 * Downloads a saved response body (#1292) through lib/embed's `downloadBlob`: a plain
 * link would start the download inside Bambuddy's sandboxed frame, which has no
 * `allow-downloads`, and Chromium drops it without a word (#612).
 */
function SavedBody({ url }: { url: string }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load(): Promise<Blob> {
    const res = await fetch(url)
    if (res.status === 404) throw new Error("That body is no longer kept: only the session's 10 newest saved responses are.")
    if (!res.ok) throw new Error(`The body could not be read (HTTP ${res.status}).`)
    return res.blob()
  }

  async function download() {
    setBusy(true)
    setError(null)
    try {
      await downloadBlob(load, `http-response-${url.slice(url.lastIndexOf('/') + 1, url.lastIndexOf('/') + 9)}`)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The body could not be downloaded.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-0.5 text-xs">
      <Tip wrap={busy} content="The body is kept while the session's 10 newest saved responses include it.">
        <Button variant="link" size="xs" className="h-auto px-0" onClick={() => void download()} disabled={busy}>
          Download body
        </Button>
      </Tip>
      {error && (
        <span role="alert" className="ml-2 text-warn">
          {error}
        </span>
      )}
    </div>
  )
}

function AuditLogView() {
  const [kind, setKind] = useState<AuditKind | ''>('')
  const [outcome, setOutcome] = useState<AuditOutcome | ''>('')
  // Older pages, appended below the first; cleared whenever the filters change.
  const [older, setOlder] = useState<{ entries: AuditEntry[]; next: string | null } | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState<string | null>(null)
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const first = useAsync(
    () => fetchAudit({ limit: AUDIT_PAGE, ...(kind ? { kind } : {}), ...(outcome ? { outcome } : {}) }),
    [kind, outcome],
  )
  const entries = [...(first.data?.entries ?? []), ...(older?.entries ?? [])]
  const next = older ? older.next : (first.data?.next ?? null)
  const retention = first.data?.retention_days
  const retentionText = draft ?? (retention === undefined ? '' : String(retention))

  function filter(update: () => void) {
    update()
    setOlder(null)
    setMoreError(null)
  }

  async function loadOlder() {
    if (!next) return
    setLoadingMore(true)
    setMoreError(null)
    try {
      const page = await fetchAudit({
        limit: AUDIT_PAGE,
        before: next,
        ...(kind ? { kind } : {}),
        ...(outcome ? { outcome } : {}),
      })
      setOlder({ entries: [...(older?.entries ?? []), ...page.entries], next: page.next })
    } catch (err) {
      setMoreError((err as Error).message)
    } finally {
      setLoadingMore(false)
    }
  }

  async function saveRetention() {
    const days = Number(retentionText)
    setSaved(null)
    if (!Number.isInteger(days) || days < MIN_RETENTION_DAYS || days > MAX_RETENTION_DAYS) {
      setSaveError(`Keep entries for ${MIN_RETENTION_DAYS} to ${MAX_RETENTION_DAYS} days.`)
      return
    }
    setSaving(true)
    setSaveError(null)
    try {
      const stored = await saveAuditRetention(days)
      setDraft(String(stored))
      setSaved(`Entries are kept for ${stored} days.`)
    } catch (err) {
      setSaveError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card className="mt-4 gap-0 py-0">
      <CardHeader className="border-b py-3">
        <h4 data-testid="ai-audit-heading" className="font-heading text-sm font-medium">
          AI activity
        </h4>
      </CardHeader>
      <CardContent className="space-y-3 py-4">
        <p className="text-xs text-muted-foreground">
          Every tool call the assistant or an MCP client made, what each assistant turn cost, every
          approval decision, and every change to AI credentials, plugins, settings and MCP tokens. Inputs are shown scrubbed;
          secrets are never recorded.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="audit-kind" className="text-[13px] font-normal text-muted-foreground">
              Kind
            </Label>
            <Select
              value={kind || ALL}
              onValueChange={(next) => filter(() => setKind(next === ALL ? '' : (next as AuditKind)))}
            >
              <SelectTrigger id="audit-kind" className="min-w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All</SelectItem>
                {AUDIT_KINDS.map((k) => (
                  <SelectItem key={k} value={k}>
                    {KIND_LABEL[k]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="audit-outcome" className="text-[13px] font-normal text-muted-foreground">
              Outcome
            </Label>
            <Select
              value={outcome || ALL}
              onValueChange={(next) => filter(() => setOutcome(next === ALL ? '' : (next as AuditOutcome)))}
            >
              <SelectTrigger id="audit-outcome" className="min-w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All</SelectItem>
                {AUDIT_OUTCOMES.map((o) => (
                  <SelectItem key={o} value={o}>
                    {OUTCOME_LABEL[o]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button variant="outline" onClick={() => filter(() => first.reload())} disabled={first.loading}>
            Refresh
          </Button>
        </div>
      </CardContent>

      {first.error ? (
        <p role="alert" className="px-4 py-4 text-[13px] text-warn">
          Could not read the audit log: {first.error.message}
        </p>
      ) : first.loading && !first.data ? (
        <div role="status" className="flex flex-col gap-2 px-4 py-4">
          <span className="sr-only">Loading…</span>
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-3/4" />
        </div>
      ) : entries.length === 0 ? (
        <p className="px-4 py-4 text-[13px] text-muted-foreground">Nothing recorded{kind || outcome ? ' for these filters' : ''}.</p>
      ) : (
        <ul className="border-t border-border" aria-label="AI activity entries">
          {entries.map((entry) => (
            <AuditRow key={entry.id} entry={entry} />
          ))}
        </ul>
      )}

      {(next || moreError) && !first.error && (
        <div className="flex items-center gap-3 border-t border-border px-4 py-2.5">
          {next && (
            <Button size="sm" variant="outline" onClick={() => void loadOlder()} disabled={loadingMore} aria-busy={loadingMore}>
              {loadingMore && <Spinner />}
              Load older
            </Button>
          )}
          {moreError && (
            <p role="alert" className="text-xs text-warn">
              {moreError}
            </p>
          )}
        </div>
      )}

      <div className="border-t border-border p-4">
        <Label htmlFor="audit-retention" className="text-[13px] font-normal">
          Keep entries for (days)
        </Label>
        <div className="mt-1.5 flex items-center gap-2">
          <Input
            id="audit-retention"
            type="number"
            inputMode="numeric"
            min={MIN_RETENTION_DAYS}
            max={MAX_RETENTION_DAYS}
            value={retentionText}
            onChange={(event) => {
              setDraft(event.target.value)
              setSaved(null)
            }}
            className="sb-num w-28"
            {...USER_ONLY}
          />
          <Button
            onClick={() => void saveRetention()}
            disabled={saving || retentionText === ''}
            aria-busy={saving}
            {...USER_ONLY}
          >
            {saving && <Spinner />}
            Save
          </Button>
          {saved && (
            <span role="status" className="text-xs text-ok">
              {saved}
            </span>
          )}
        </div>
        {saveError && (
          <p role="alert" className="mt-1.5 text-xs text-warn">
            {saveError}
          </p>
        )}
        <p className="mt-1.5 text-xs text-muted-foreground">
          Older entries are deleted hourly. The log itself can&rsquo;t be edited.
        </p>
      </div>
    </Card>
  )
}
