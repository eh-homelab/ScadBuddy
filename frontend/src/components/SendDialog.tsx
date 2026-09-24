import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { Output, PrintOptions, SendMode, SendResult } from '../api/types'
import { openExternal } from '../lib/embed'
import { eligibilityIssues } from '../lib/problems'
import { quantityBounds } from '../lib/printOptions'
import { PrintOptionsDisclosure } from './PrintOptionsDisclosure'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'
import { useAsync } from '../lib/useAsync'

const MODES: { value: SendMode; label: string; detail: string }[] = [
  {
    value: 'library',
    label: 'Add to library only',
    detail: 'Uploads the 3MF. Slice it yourself in Bambuddy later.',
  },
  {
    value: 'queue',
    label: 'Slice and queue',
    detail: 'Runs the configured slicer pipeline, then adds the plate to the print queue.',
  },
]

const { min: QUANTITY_MIN, max: QUANTITY_MAX } = quantityBounds()

function boundedQuantity(raw: string): number {
  const parsed = Math.round(Number(raw))
  if (!Number.isFinite(parsed)) return QUANTITY_MIN
  return Math.min(QUANTITY_MAX, Math.max(QUANTITY_MIN, parsed))
}

interface Props {
  open: boolean
  output: Output | undefined
  onClose: () => void
  onSent: (result: SendResult) => void
}

export function SendDialog({ open, output, onClose, onSent }: Props) {
  const [mode, setMode] = useState<SendMode>('queue')
  // #88 — per-send overrides. Remembered ones live on the server and are merged there.
  // Copies is one of these rather than a control of its own: `SendRequest.copies` and
  // `options.quantity` are the same value, and two independent controls for it left the
  // disclosure's Quantity row showing a number that was no longer going to be sent.
  const [options, setOptions] = useState<PrintOptions>({})
  // Only to tell "no link was configured" apart from "Bambuddy refused the note":
  // the send result reports an absent link the same way for both.
  const publicUrl = useAsync(() => api.getSettings(), []).data?.public_url ?? null
  const [effective, setEffective] = useState<PrintOptions>({})
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [issues, setIssues] = useState<string[]>([])
  const [result, setResult] = useState<SendResult | null>(null)

  // The dialog is mounted once and reused for every output in the session (ActionBar and
  // HistoryPage both render it without a `key`), so per-send overrides have to be dropped
  // explicitly. Without this, an `auto_off_after` set for one urgent print rode along on
  // every later send, from a disclosure that stays collapsed and so never showed it.
  useEffect(() => {
    setOptions({})
    setEffective({})
  }, [output?.id])

  // What the print will actually be queued with. `options.quantity` first, not just
  // `effective`, because the disclosure reports the merge back through an effect and a
  // controlled input cannot wait a render for the keystroke it was just given.
  //
  // Once a send has happened the server's own answer wins outright: it resolves the four
  // scopes itself, and a Send that beat the disclosure's fetch would otherwise have the
  // confirmation claim one copy while a remembered quantity had been queued.
  const quantity = result?.options?.quantity ?? options.quantity ?? effective.quantity ?? 1

  function close() {
    setError(null)
    setIssues([])
    setResult(null)
    setSending(false)
    setOptions({})
    setEffective({})
    onClose()
  }

  async function send() {
    if (!output) return
    setSending(true)
    setError(null)
    setIssues([])
    try {
      const sent = await api.sendOutput(output.id, { mode, options })
      setResult(sent)
      onSent(sent)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Send failed. Check the connection.')
      // Bambuddy's pipeline-eligibility report comes through verbatim; list what blocked it.
      setIssues(cause instanceof ApiError ? eligibilityIssues(cause.problem) : [])
    } finally {
      setSending(false)
    }
  }

  return (
    <Dialog
      open={open}
      title="Send to Bambuddy"
      description={result ? undefined : 'The file is uploaded from ScadBuddy, not your browser.'}
      onClose={close}
      footer={
        result ? (
          <>
            <Button onClick={close}>Done</Button>
            {result.bambuddy_url && (
              <Button variant="primary" onClick={() => openExternal(result.bambuddy_url as string)}>
                {result.mode === 'queue' ? 'Open in queue' : 'Open in library'}
              </Button>
            )}
          </>
        ) : (
          <>
            <Button onClick={close} disabled={sending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void send()} disabled={sending || !output}>
              {sending && <Spinner />}
              {sending ? 'Sending' : 'Send'}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <>
          <p className="text-[13px] text-ink">
            {result.queue_item_id ? (
              <>
                Queued as <span className="sb-num">#{result.queue_item_id}</span> with{' '}
                <span className="sb-num">{quantity}</span>{' '}
                {quantity === 1 ? 'copy' : 'copies'}.
              </>
            ) : result.pipeline_run_id ? (
              <>
                Pipeline run <span className="sb-num">#{result.pipeline_run_id}</span> started for{' '}
                <span className="sb-num">{quantity}</span>{' '}
                {quantity === 1 ? 'copy' : 'copies'}.
              </>
            ) : (
              <>
                Added to the library as{' '}
                <span className="sb-num">{result.filename}</span> (#{result.library_file_id}).
              </>
            )}
          </p>
          {/* The note is best-effort, so say which way it went rather than implying
              the link is on the file when Bambuddy refused it. Silence when no public
              URL is set: there was no link to attach, which is not a failure. */}
          {result.edit_url ? (
            <p className="mt-1.5 text-[12px] text-muted">
              Bambuddy has the link back to these parameters.
            </p>
          ) : publicUrl ? (
            <p className="mt-1.5 text-[12px] text-muted">
              Bambuddy would not take the link back to these parameters.
            </p>
          ) : null}
        </>
      ) : (
        <>
          <fieldset>
            <legend className="sr-only">What to do with the file</legend>
            <ul className="space-y-2">
              {MODES.map((option) => (
                <li key={option.value}>
                  <label
                    className={`flex cursor-pointer gap-2.5 rounded-[6px] border p-3 transition-colors ${
                      mode === option.value
                        ? 'border-accent bg-accent/8'
                        : 'border-line bg-surface-2 hover:border-line-strong'
                    }`}
                  >
                    <input
                      type="radio"
                      name="send-mode"
                      value={option.value}
                      checked={mode === option.value}
                      onChange={() => setMode(option.value)}
                      className="mt-0.5 accent-[var(--sb-accent)]"
                    />
                    <span>
                      <span className="block text-[13px] text-ink">{option.label}</span>
                      <span className="mt-0.5 block text-[12px] text-muted">{option.detail}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>

          <div className="mt-4 flex items-center gap-3">
            <label htmlFor="send-copies" className="text-[13px] text-ink">
              Copies
            </label>
            <input
              id="send-copies"
              type="number"
              // The same bound the Quantity row uses, because it is the same field.
              min={QUANTITY_MIN}
              max={QUANTITY_MAX}
              step={1}
              value={quantity}
              disabled={mode !== 'queue'}
              onChange={(event) =>
                setOptions((current) => ({
                  ...current,
                  // Rounded as well as bounded: the field is an `int` server-side.
                  // A `type="number"` input reports a lone "-" as its value, which is
                  // `NaN` — and `??` does not fall through NaN, so it would stick.
                  quantity: boundedQuantity(event.target.value),
                }))
              }
              className="sb-field sb-num w-20 text-right"
            />
            {mode !== 'queue' && (
              <span className="text-[12px] text-faint">Only used when queueing a print.</span>
            )}
          </div>

          {mode === 'queue' && output && (
            <PrintOptionsDisclosure
              slug={output.slug}
              value={options}
              onChange={setOptions}
              onEffective={setEffective}
            />
          )}

          {error && (
            <div role="alert" className="mt-3 text-[13px] text-warn">
              <p>{error}</p>
              {issues.length > 0 && (
                <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-[12px]">
                  {issues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}
    </Dialog>
  )
}
