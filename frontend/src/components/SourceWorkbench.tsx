import { useState, type ReactNode } from 'react'
import { ApiError, api } from '../api/client'
import type { SourceCheck } from '../api/types'
import { errorLines, refusedCheck } from '../lib/problems'
import { ScadEditor } from './ScadEditor'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

interface Props {
  breadcrumb: ReactNode
  /** Rendered under the bar — the name field on a new model, nothing on an edit. */
  fields?: ReactNode
  source: string
  onSourceChange: (next: string) => void
  saveLabel: string
  canSave: boolean
  onSave: (force: boolean) => Promise<void>
}

/**
 * The paste surface shared by "New model" and "Edit source": editor, an explicit
 * Check, and a Save that only goes through once OpenSCAD is happy — or once the
 * user says "Save anyway", which is what `force` is on both write routes.
 */
export function SourceWorkbench({
  breadcrumb,
  fields,
  source,
  onSourceChange,
  saveLabel,
  canSave,
  onSave,
}: Props) {
  const [check, setCheck] = useState<SourceCheck | undefined>(undefined)
  const [checking, setChecking] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refused = check !== undefined && !check.ok
  const busy = checking || saving

  function edit(next: string) {
    // The old verdict is about the old text; keep it from going stale on screen.
    setCheck(undefined)
    setError(null)
    onSourceChange(next)
  }

  async function runCheck() {
    setChecking(true)
    setError(null)
    try {
      setCheck(await api.checkSource(source))
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The parse check did not run.')
    } finally {
      setChecking(false)
    }
  }

  async function save(force: boolean) {
    setSaving(true)
    setError(null)
    try {
      await onSave(force)
    } catch (cause) {
      if (cause instanceof ApiError) {
        const fromServer = refusedCheck(cause.problem)
        if (fromServer) setCheck(fromServer)
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
            <Button size="sm" onClick={() => void runCheck()} disabled={busy || !source.trim()}>
              {checking && <Spinner />}
              Check
            </Button>
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
        <ScadEditor
          value={source}
          onChange={edit}
          errorLines={errorLines(check)}
          label="OpenSCAD source"
        />
      </div>

      <div className="max-h-[35vh] overflow-y-auto px-3 py-2">
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        <CheckReport check={check} />
      </div>
    </div>
  )
}

function CheckReport({ check }: { check: SourceCheck | undefined }) {
  if (!check) {
    return (
      <p className="text-[12px] text-faint">
        Check runs OpenSCAD over the source without saving it. Errors appear here with
        their line numbers.
      </p>
    )
  }

  if (check.ok) {
    return (
      <p role="status" className="text-[13px] text-ok">
        {check.checked
          ? 'Parses cleanly.'
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
