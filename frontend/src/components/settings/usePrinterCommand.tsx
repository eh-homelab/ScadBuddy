import { useCallback, useEffect, useRef } from 'react'
import { mayHaveRun, newRequestId, type PrinterCommandOptions } from '../../api/client'
import { cn } from '@/lib/utils'

/**
 * #7276 — what the K-profile and AMS slot panels share around `printerCommand`.
 *
 * P55: a command the page stopped following (`PRINTER_COMMAND_RUNNING`), or any answer after
 * which it may have run, keeps its `{printer, target, action, body, key}`. The next press of the
 * same target, action and body sends that key, so the backend joins the first run (P38, P41)
 * and the printer gets nothing new. Another body gets a new key; a finished answer forgets it.
 *
 * Each command and read runs under a signal that `stop` aborts, and the panel's unmount too,
 * so a closed panel or a changed nozzle stops its follow. The command itself is not stopped.
 */

interface Held {
  printer: string
  target: string
  action: string
  body: string
  key: string
}

export function usePrinterCommand(printer: string) {
  const held = useRef<Held | null>(null)
  const live = useRef(new Set<AbortController>())

  const stop = useCallback(() => {
    live.current.forEach((controller) => controller.abort())
    live.current.clear()
  }, [])

  useEffect(() => stop, [stop])

  /** A signal for one request, aborted by `stop` or the unmount. */
  const signal = useCallback(() => {
    const controller = new AbortController()
    live.current.add(controller)
    return controller.signal
  }, [])

  const send = useCallback(
    async <T,>(
      target: string,
      action: string,
      body: unknown,
      run: (options: Pick<PrinterCommandOptions, 'key' | 'signal'>) => Promise<T>,
    ): Promise<T> => {
      const sent = JSON.stringify(body ?? null)
      const last = held.current
      const same =
        last && last.printer === printer && last.target === target && last.action === action && last.body === sent
      const key = same ? last.key : newRequestId()
      try {
        const answer = await run({ key, signal: signal() })
        held.current = null
        return answer
      } catch (cause) {
        // A command stopped by the page (an abort) was sent all the same.
        held.current = stopped(cause) || mayHaveRun(cause) ? { printer, target, action, body: sent, key } : null
        throw cause
      }
    },
    [printer, signal],
  )

  return { send, signal, stop }
}

/** Whether `cause` is the abort of a stopped follow: nothing to tell the person. */
export function stopped(cause: unknown): boolean {
  return typeof cause === 'object' && cause !== null && (cause as { name?: unknown }).name === 'AbortError'
}

/**
 * A panel's one live region: always mounted, empty until a message comes, so a screen reader
 * announces the first one ("Sent to the printer. Waiting…") too, not only the later changes.
 */
export function Said({ message }: { message: { text: string; warn: boolean } | null }) {
  return (
    <p
      role="status"
      aria-live="polite"
      className={cn('text-xs empty:sr-only', message?.warn ? 'text-warn' : 'text-muted-foreground')}
    >
      {message?.text}
    </p>
  )
}
