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
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

/**
 * Settings → "AI activity" (#258): the agent service's audit log of AI actions
 * (`GET /api/v1/ai/audit`), newest first, filterable by kind and outcome, with the
 * retention setting. Hidden when AI is off, like the rest of the assistant.
 */
export function AiAuditSection() {
  const ai = useAiAvailability()
  if (!ai.available) return null
  return <AuditLogView />
}

export const AUDIT_PAGE = 25

const KIND_LABEL: Record<AuditKind, string> = {
  tool_call: 'Tool calls',
  approval: 'Approvals',
  credential: 'Credentials',
  plugin: 'Plugins',
  settings: 'Settings',
  token: 'MCP tokens',
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
  http: 'Settings',
  system: 'ScadBuddy',
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

function AuditRow({ entry }: { entry: AuditEntry }) {
  const text = entry.detail ?? entry.input_summary
  return (
    <li className="border-b border-line px-4 py-2 text-[13px] last:border-b-0" data-testid="audit-entry">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="font-medium">{describe(entry)}</span>
        {entry.tier && <span className="text-[12px] text-muted">{entry.tier}</span>}
        <span className={`text-[12px] font-medium ${outcomeClass(entry.outcome)}`}>
          {OUTCOME_LABEL[entry.outcome]}
        </span>
        <span className="ml-auto text-[12px] text-muted sb-num">{when(entry.at)}</span>
      </div>
      <div className="mt-0.5 text-[12px] text-muted">
        {entry.actor.label} via {SURFACE_LABEL[entry.surface]}
        {entry.client_ip && <> · <span className="sb-num">{entry.client_ip}</span></>}
        {entry.duration_ms !== null && <> · <span className="sb-num">{entry.duration_ms} ms</span></>}
        {entry.approval_id && <> · approval <span className="sb-num">{entry.approval_id.slice(0, 8)}</span></>}
      </div>
      {text && (
        <p className="mt-0.5 truncate font-mono text-[12px] text-muted" title={text}>
          {text}
        </p>
      )}
    </li>
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
    <section className="mt-4 rounded-[6px] border border-line bg-surface" aria-labelledby="ai-audit-heading">
      <h2 id="ai-audit-heading" className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        AI activity
      </h2>
      <div className="space-y-3 p-4">
        <p className="text-[12px] text-muted">
          Every tool call the assistant or an MCP client made, every approval decision, and every
          change to AI credentials, plugins, settings and MCP tokens. Inputs are shown scrubbed;
          secrets are never recorded.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-[13px]">
            <span className="block text-muted">Kind</span>
            <select
              className="sb-field mt-1"
              value={kind}
              onChange={(event) => filter(() => setKind(event.target.value as AuditKind | ''))}
            >
              <option value="">All</option>
              {AUDIT_KINDS.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABEL[k]}
                </option>
              ))}
            </select>
          </label>
          <label className="text-[13px]">
            <span className="block text-muted">Outcome</span>
            <select
              className="sb-field mt-1"
              value={outcome}
              onChange={(event) => filter(() => setOutcome(event.target.value as AuditOutcome | ''))}
            >
              <option value="">All</option>
              {AUDIT_OUTCOMES.map((o) => (
                <option key={o} value={o}>
                  {OUTCOME_LABEL[o]}
                </option>
              ))}
            </select>
          </label>
          <Button size="sm" onClick={() => filter(() => first.reload())} disabled={first.loading}>
            Refresh
          </Button>
        </div>
      </div>

      {first.error ? (
        <p role="alert" className="px-4 pb-4 text-[13px] text-warn">
          Could not read the audit log: {first.error.message}
        </p>
      ) : first.loading && !first.data ? (
        <p className="flex items-center gap-2 px-4 pb-4 text-[13px] text-muted">
          <Spinner /> Loading…
        </p>
      ) : entries.length === 0 ? (
        <p className="px-4 pb-4 text-[13px] text-muted">Nothing recorded{kind || outcome ? ' for these filters' : ''}.</p>
      ) : (
        <ul className="border-t border-line" aria-label="AI activity entries">
          {entries.map((entry) => (
            <AuditRow key={entry.id} entry={entry} />
          ))}
        </ul>
      )}

      {(next || moreError) && !first.error && (
        <div className="flex items-center gap-3 border-t border-line px-4 py-2.5">
          {next && (
            <Button size="sm" onClick={() => void loadOlder()} disabled={loadingMore} aria-busy={loadingMore}>
              {loadingMore && <Spinner />}
              Load older
            </Button>
          )}
          {moreError && (
            <p role="alert" className="text-[12px] text-warn">
              {moreError}
            </p>
          )}
        </div>
      )}

      <div className="border-t border-line p-4">
        <label htmlFor="audit-retention" className="block text-[13px]">
          Keep entries for (days)
        </label>
        <div className="mt-1.5 flex items-center gap-2">
          <input
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
            className="sb-field sb-num w-28"
            {...USER_ONLY}
          />
          <Button
            size="sm"
            onClick={() => void saveRetention()}
            disabled={saving || retentionText === ''}
            aria-busy={saving}
            {...USER_ONLY}
          >
            {saving && <Spinner />}
            Save
          </Button>
          {saved && (
            <span role="status" className="text-[12px] text-ok">
              {saved}
            </span>
          )}
        </div>
        {saveError && (
          <p role="alert" className="mt-1.5 text-[12px] text-warn">
            {saveError}
          </p>
        )}
        <p className="mt-1.5 text-[12px] text-muted">
          Older entries are deleted hourly. The log itself can&rsquo;t be edited.
        </p>
      </div>
    </section>
  )
}
