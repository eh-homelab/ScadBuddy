import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { committed, touch, waitFor } from '../agent/highlight'
import { AgentToolError } from '../agent/types'
import { useAgentHandlers, useLatest } from '../agent/useAgentHandlers'
import { ApiError, api } from '../api/client'
import type { SourceCheck } from '../api/types'
import type { DefinitionFile } from '../lib/lsp'
import { refusedCheck } from '../lib/problems'
import { useDebounced } from '../lib/useDebounced'
import { useLeaveGuard } from '../lib/useLeaveGuard'
import { SourceEditor, type SourceEditHandle } from './SourceEditor'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
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
  /** An existing model, so its sibling includes resolve while checking. */
  slug?: string
  saveLabel: string
  canSave: boolean
  /** #184 — a built-in template: no save, and the editor refuses input. */
  readOnly?: boolean
  /** #159 — what a read-only source offers in place of Save ("Duplicate to edit"). */
  readOnlyActions?: ReactNode
  /** #997 — unsaved edits: leaving the page asks first. */
  dirty: boolean
  /** Saves, and answers where to go now that it has. */
  onSave: (force: boolean) => Promise<string>
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
  slug,
  saveLabel,
  canSave,
  readOnly = false,
  readOnlyActions,
  dirty,
  onSave,
}: Props) {
  const guard = useLeaveGuard(dirty)
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
    const inflight = new AbortController()
    const { signal } = inflight
    setChecking(true)
    api
      .checkSource(settled, slug, signal)
      .then((result) => {
        if (!signal.aborted) setVerdict({ source: settled, result })
      })
      .catch((cause: unknown) => {
        // An abort is this effect's own doing, not a failed check.
        if (!signal.aborted) {
          setError(cause instanceof ApiError ? cause.detail : 'The parse check did not run.')
        }
      })
      .finally(() => {
        if (!signal.aborted) setChecking(false)
      })
    return () => {
      inflight.abort()
    }
  }, [settled, slug])

  // A verdict about older text is not a verdict about this one.
  const check = verdict?.source === source ? verdict.result : undefined
  const refused = check !== undefined && !check.ok
  const busy = checking || saving
  const saveDisabled = readOnly || busy || !canSave || !source.trim()

  // #185 — a saved model's sibling files and pinned libraries, for go-to-definition.
  // Memoized: a new function would start a new language server session.
  const readFile = useMemo(
    () => (slug ? (file: DefinitionFile) => api.getDefinitionFile(slug, file) : undefined),
    [slug],
  )

  // #254 — the source editor's browser tools.
  const editRef = useRef<SourceEditHandle | null>(null)
  const editorBox = useRef<HTMLDivElement>(null)
  const live = useLatest({ source, check, checking, error })

  useAgentHandlers(
    'source',
    {
      get_editor_text: () => ({
        text: source,
        lines: source.split('\n').length,
        read_only: readOnly,
        uri,
      }),
      replace_range: async ({ start_line, start_column, end_line, end_column, text }) => {
        if (readOnly) {
          throw new AgentToolError(
            'refused',
            'This is a built-in template, so its source is read-only. The user can Duplicate to edit.',
          )
        }
        const start = offsetOf(source, start_line, start_column)
        const end = offsetOf(source, end_line, end_column)
        if (end < start) throw new AgentToolError('invalid_args', 'The range ends before it starts.')
        const range = {
          startLineNumber: start_line,
          startColumn: start_column,
          endLineNumber: end_line,
          endColumn: end_column,
        }
        const next = source.slice(0, start) + text + source.slice(end)
        if (editRef.current) editRef.current.replace(range, text)
        // No editor mounted (it is still loading, or a test stands in for it): the same
        // change, through the same `onSourceChange` the editor's own edits go through.
        else onSourceChange(next)
        touch(editRef.current?.element() ?? editorBox.current)
        await committed(() => live.current.source === next, 'the editor to take the edit')
        return { lines: next.split('\n').length, length: next.length, saved: false }
      },
      get_problems: async ({ wait, timeout_ms }) => {
        if (wait) {
          await waitFor(
            () => {
              const now = live.current
              if (!now.source.trim()) return true
              return now.check !== undefined || now.error ? true : undefined
            },
            { timeout: timeout_ms, what: "OpenSCAD's check of the current source" },
          )
        }
        const { check: current, checking: busyNow, error: failure } = live.current
        return {
          current: current !== undefined,
          checking: busyNow,
          ok: current?.ok ?? null,
          checked: current?.checked ?? null,
          timed_out: current?.timed_out ?? false,
          parameters: current?.parameters ?? null,
          diagnostics: current?.diagnostics ?? [],
          log_tail: current && !current.ok && !(current.diagnostics ?? []).length ? current.log_tail ?? [] : undefined,
          error: failure,
        }
      },
    },
    () => ({
      read_only: readOnly,
      lines: source.split('\n').length,
      check: check ? (check.ok ? 'ok' : 'errors') : checking ? 'checking' : 'none',
      problems: check?.diagnostics?.length ?? 0,
    }),
  )

  async function save(force: boolean) {
    setSaving(true)
    setError(null)
    try {
      // Saved, so not leaving anything behind: `dirty` has not caught up yet.
      guard.leaveTo(await onSave(force))
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
          {readOnly ? (
            <div className="flex shrink-0 items-center gap-2">
              <span
                data-testid="builtin-badge"
                className="shrink-0 rounded-[6px] bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted"
              >
                Built-in template — read-only
              </span>
              {readOnlyActions}
            </div>
          ) : (
            <div className="flex shrink-0 items-center gap-2">
              <Button
                size="sm"
                variant="primary"
                onClick={() => void save(false)}
                disabled={saveDisabled}
              >
                {saving && <Spinner />}
                {saveLabel}
              </Button>
              {refused && (
                <Button size="sm" variant="danger" onClick={() => void save(true)} disabled={busy || !canSave}>
                  Save anyway
                </Button>
              )}
            </div>
          )}
        </div>
        {fields}
      </div>

      <div ref={editorBox} className="min-h-0 border-b border-line">
        <SourceEditor
          editRef={editRef}
          value={source}
          onChange={onSourceChange}
          errors={check?.diagnostics ?? []}
          uri={uri}
          languageServer={api.languageServerPath(slug)}
          readFile={readFile}
          label="OpenSCAD source"
          readOnly={readOnly}
          // #997 — Ctrl/Cmd+S is the Save button, and does nothing when it would not.
          onSave={() => {
            if (!saveDisabled) void save(false)
          }}
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

      <Dialog
        open={guard.pending !== null}
        title="Leave without saving?"
        description="Your edits to this source have not been saved, and will be lost."
        onClose={guard.stay}
        footer={
          <>
            <Button variant="ghost" onClick={guard.stay}>
              Stay
            </Button>
            <Button variant="danger" onClick={guard.leave}>
              Leave without saving
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-muted">Save first to keep them.</p>
      </Dialog>
    </div>
  )
}

/** The string offset of a 1-based line and column; a position past the text is refused. */
function offsetOf(source: string, line: number, column: number): number {
  const lines = source.split('\n')
  const text = lines[line - 1]
  if (text === undefined) {
    throw new AgentToolError('invalid_args', `Line ${line} is past the end (${lines.length} lines).`)
  }
  if (column > text.length + 1) {
    throw new AgentToolError('invalid_args', `Line ${line} has ${text.length} characters; column ${column} is past its end.`)
  }
  let offset = 0
  for (let index = 0; index < line - 1; index++) offset += (lines[index]?.length ?? 0) + 1
  return offset + column - 1
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

  if (check.timed_out) {
    return (
      <p role="alert" className="text-[13px] text-warn">
        The check timed out. The source may be valid but slow to evaluate — saving it
        will hit the same limit.
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
