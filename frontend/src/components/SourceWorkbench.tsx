import { useEffect, useState, type ReactNode } from 'react'
import { ApiError, api } from '../api/client'
import type { SourceCheck } from '../api/types'
import { refusedCheck } from '../lib/problems'
import { useDebounced } from '../lib/useDebounced'
import { SourceEditor } from './SourceEditor'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/** Long enough that a burst of typing is one check, short enough to feel live. */
export const CHECK_DEBOUNCE_MS = 700

interface Props {
  breadcrumb: ReactNode
  /** Rendered under the bar — the name field on a new model, nothing on an edit. */
  fields?: ReactNode
  source: string
  onSourceChange: (next: string) => void
  /** The model URI the editor opens the source under. */
  uri: string
  saveLabel: string
  canSave: boolean
  onSave: (force: boolean) => Promise<void>
}

/** A verdict is only ever shown for the exact text it was computed from. */
interface Verdict {
  source: string
  result: SourceCheck
}

/**
 * The paste surface shared by "New model" and "Edit source". OpenSCAD is asked to
 * parse the source as it settles, and again — server-side, authoritatively — on save:
 * the write routes refuse source that does not parse unless `force` is set, which is
 * what "Save anyway" sends.
 */
export function SourceWorkbench({
  breadcrumb,
  fields,
  source,
  onSourceChange,
  uri,
  saveLabel,
  canSave,
  onSave,
}: Props) {
  const [verdict, setVerdict] = useState<Verdict | undefined>(undefined)
  const [checking, setChecking] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const settled = useDebounced(source, CHECK_DEBOUNCE_MS)

  useEffect(() => {
    if (!settled.trim()) {
      setVerdict(undefined)
      return
    }
    let cancelled = false
    setChecking(true)
    api
      .checkSource(settled)
      .then((result) => {
        if (!cancelled) setVerdict({ source: settled, result })
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof ApiError ? cause.detail : 'The parse check did not run.')
        }
      })
      .finally(() => {
        if (!cancelled) setChecking(false)
      })
    return () => {
      cancelled = true
    }
  }, [settled])

  // A verdict about older text is not a verdict about this one.
  const check = verdict?.source === source ? verdict.result : undefined
  const refused = check !== undefined && !check.ok
  const busy = checking || saving

  async function save(force: boolean) {
    setSaving(true)
    setError(null)
    try {
      await onSave(force)
    } catch (cause) {
      if (cause instanceof ApiError) {
        const fromServer = refusedCheck(cause.problem)
        if (fromServer) setVerdict({ source, result: fromServer })
        else setError(cause.detail)
      } else {
        setError('Could not save. Try again.')
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto]">
      <div className="border-b border-line bg-surface">
        <div className="flex items-center justify-between gap-3 px-3 py-1.5">
          <div className="flex min-w-0 items-baseline gap-2">{breadcrumb}</div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              size="sm"
              variant="primary"
              onClick={() => void save(false)}
              disabled={busy || !canSave || !source.trim()}
            >
              {saving && <Spinner />}
              {saveLabel}
            </Button>
            {refused && (
              <Button size="sm" variant="danger" onClick={() => void save(true)} disabled={busy}>
                Save anyway
              </Button>
            )}
          </div>
        </div>
        {fields}
      </div>

      <div className="min-h-0 border-b border-line">
        <SourceEditor
          value={source}
          onChange={onSourceChange}
          errors={check?.diagnostics ?? []}
          uri={uri}
          label="OpenSCAD source"
        />
      </div>

      <div className="max-h-[35vh] overflow-y-auto px-3 py-2">
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        <CheckReport check={check} checking={checking} />
      </div>
    </div>
  )
}

function CheckReport({ check, checking }: { check: SourceCheck | undefined; checking: boolean }) {
  if (checking && !check) {
    return (
      <p className="flex items-center gap-2 text-[12px] text-muted">
        <Spinner /> Asking OpenSCAD
      </p>
    )
  }

  if (!check) {
    return (
      <p className="text-[12px] text-faint">
        OpenSCAD parses the source as you type. Errors appear here and in the editor,
        against their line.
      </p>
    )
  }

  if (check.ok) {
    return (
      <p role="status" className="text-[13px] text-ok">
        {check.checked
          ? `Parses cleanly${check.parameters == null ? '' : ` — ${check.parameters} parameters`}.`
          : 'No OpenSCAD available here, so the source was not checked.'}
      </p>
    )
  }

  const diagnostics = check.diagnostics ?? []
  return (
    <div role="alert" data-testid="check-report">
      <p className="text-[13px] font-medium text-warn">OpenSCAD could not parse this.</p>
      {diagnostics.length > 0 ? (
        <ul className="mt-1.5 space-y-1">
          {diagnostics.map((diagnostic, index) => (
            <li key={index} className="flex gap-2 text-[13px]">
              <span className="sb-num shrink-0 text-faint">
                {diagnostic.line == null ? '—' : `Line ${diagnostic.line}`}
              </span>
              <span className={diagnostic.severity === 'error' ? 'text-warn' : 'text-muted'}>
                {diagnostic.message}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <pre className="sb-num mt-1.5 overflow-x-auto text-[12px] text-muted">
          {(check.log_tail ?? []).join('\n')}
        </pre>
      )}
    </div>
  )
}
