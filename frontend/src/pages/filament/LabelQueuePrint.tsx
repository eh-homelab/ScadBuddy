import { RefreshCwIcon, TagIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import { labelsFile, type LabelsRequest } from '../../api/filament'
import type { SettingsUpdate } from '../../api/types'
import { DEFAULT_JOB_OPTIONS, fits, labelProblem, type LabelJobOptions, type LabelQueue, labelQueues, printLabels, ZEBRA_ZPL } from '../../api/labelPrinting'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useSettingSaver } from '../../lib/useSettingSaver'
import { cn } from '@/lib/utils'

type Page = { widthMm: number; heightMm: number }

type Queues = { state: 'reading' } | { state: 'read'; list: LabelQueue[] } | { state: 'down' } | { state: 'failed'; message: string }

const MEDIA_TYPES: { value: LabelJobOptions['media_type']; label: string }[] = [
  { value: 'Saved', label: 'Printer default' },
  { value: 'Direct', label: 'Direct thermal' },
  { value: 'Thermal', label: 'Thermal transfer' },
]
const DARKNESS = [-1, ...Array.from({ length: 30 }, (_, at) => at + 1)]
const RATES = ['Default', ...Array.from({ length: 12 }, (_, at) => String(at + 1))]

const STATE_TEXT: Record<LabelQueue['state'], string> = { idle: 'Idle', processing: 'Busy', stopped: 'Stopped', '': 'Unknown' }
const STATE_DOT: Record<LabelQueue['state'], string> = { idle: 'bg-ok', processing: 'bg-warn', stopped: 'bg-destructive', '': 'bg-muted-foreground' }

const nameOf = (queue: LabelQueue) => queue.info || queue.name
const shorten = (text: string) => (text.length > 60 ? `${text.slice(0, 57)}…` : text)
const mm = (n: number) => String(Math.round(n * 10) / 10)

/**
 * R7 — the queue shown: the stored one when it fits the page, else the only queue that fits,
 * else none. A queue shown always fits the page.
 */
function pickOf(fitting: LabelQueue[], stored: string | null): LabelQueue | null {
  return fitting.find((queue) => queue.name === stored) ?? (fitting.length === 1 ? fitting[0]! : null)
}

/**
 * #7170 R9 — "Print to label printer": a CUPS queue that fits the labels' page, the Zebra ZPL
 * options, and a button that asks for the labels' PDF and sends it to the queue. The queue
 * (`filament_label_queue`) and the options (`filament_label_job_options`) are saved at each change.
 */
export function LabelQueuePrint({
  body,
  page,
  sizeUnset = false,
  disabled = false,
}: {
  body: LabelsRequest
  page: Page | null
  /** True when the templates are read and the labels still have no page size (a custom size not set, or a saved sheet that is gone). */
  sizeUnset?: boolean
  disabled?: boolean
}) {
  const [queues, setQueues] = useState<Queues>({ state: 'reading' })
  // Undefined until the settings answer (or fail): no queue is picked before the stored one is known.
  const [stored, setStored] = useState<string | null | undefined>(undefined)
  const [options, setOptions] = useState<LabelJobOptions>(DEFAULT_JOB_OPTIONS)
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState<{ text: string; message: string | null } | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const queueSaver = useSettingSaver('filament_label_queue')
  const optionsSaver = useSettingSaver('filament_label_job_options')
  const reads = useRef(0)

  /** Reads the queue list; only the newest read's answer lands. */
  const readQueues = async (): Promise<LabelQueue[] | null> => {
    const read = ++reads.current
    try {
      const list = await labelQueues()
      if (read === reads.current) setQueues({ state: 'read', list })
      return list
    } catch (cause) {
      if (read === reads.current)
        setQueues(
          labelProblem(cause) === 'label_server_unreachable'
            ? { state: 'down' }
            : { state: 'failed', message: cause instanceof Error ? cause.message : 'The label queues could not be read.' },
        )
      return null
    }
  }

  useEffect(() => {
    let live = true
    void readQueues()
    api
      .getSettings()
      .then((settings) => {
        if (!live) return
        if (!queueSaver.savedBeforeRead) setStored(settings.filament_label_queue ?? null)
        if (!optionsSaver.savedBeforeRead) setOptions(settings.filament_label_job_options ?? DEFAULT_JOB_OPTIONS)
      })
      .catch(() => {
        if (live && !queueSaver.savedBeforeRead) setStored(null)
      })
    return () => {
      live = false
    }
    // Read once. `savedBeforeRead` is a getter over the saver's own ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const list = queues.state === 'read' ? queues.list : []
  const fitting = list.filter((queue) => fits(queue, page))
  const chosen = stored === undefined ? null : pickOf(fitting, stored)
  const zebra = chosen?.make_and_model === ZEBRA_ZPL

  const pickQueue = (name: string) => {
    setStored(name)
    setSent(null)
    setProblem(null)
    queueSaver.save(name)
  }

  const pickOption = (part: Partial<LabelJobOptions>) => {
    const next = { ...options, ...part }
    setOptions(next)
    // The setting's `print_rate` is a list of strings; the picker offers only those (`RATES`).
    optionsSaver.save(next as SettingsUpdate['filament_label_job_options'])
  }

  const send = async () => {
    if (!chosen) return
    const queue = chosen
    setBusy(true)
    setSent(null)
    setProblem(null)
    try {
      const pdf = await labelsFile({ ...body, format: 'pdf' })
      const job = await printLabels(queue.name, pdf, zebra ? options : DEFAULT_JOB_OPTIONS)
      const now = (await readQueues())?.find((one) => one.name === queue.name) ?? queue
      setSent({ text: `Sent to ${nameOf(queue)} (job ${job.job_id}).`, message: now.state !== 'idle' && now.state_message ? now.state_message : null })
    } catch (cause) {
      const reason = labelProblem(cause)
      if (reason === 'label_queue_unknown') {
        setProblem(`The print server no longer has the queue ${nameOf(queue)}. Choose another queue.`)
        void readQueues()
      } else if (reason === 'label_server_unreachable') {
        setProblem('The label print server does not answer. Try again, or use Print or Download.')
      } else {
        setProblem(cause instanceof Error ? cause.message : 'The labels could not be sent. Try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-label="Label printer" className="basis-full rounded-md border border-border bg-muted/30 px-3 py-2.5 text-[13px]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="flex items-center gap-1.5 font-medium">
          <TagIcon className="size-3.5 text-muted-foreground" aria-hidden="true" /> Label printer
        </span>
        {queues.state === 'reading' && <span className="text-muted-foreground">Reading the print server's queues…</span>}
        {queues.state === 'down' && (
          <>
            <span className="text-muted-foreground">The label print server does not answer. Print and Download still work.</span>
            <Button type="button" size="sm" variant="ghost" onClick={() => void readQueues()}>
              <RefreshCwIcon /> Check again
            </Button>
          </>
        )}
        {queues.state === 'failed' && (
          <>
            <span className="text-warn">{queues.message}</span>
            <Button type="button" size="sm" variant="ghost" onClick={() => void readQueues()}>
              <RefreshCwIcon /> Check again
            </Button>
          </>
        )}
        {queues.state === 'read' && page === null && (
          <span className="text-muted-foreground">{sizeUnset ? 'Choose a label size first.' : 'Reading the label sizes…'}</span>
        )}
        {queues.state === 'read' && page !== null && fitting.length === 0 && (
          <span className="text-muted-foreground">
            {list.length === 0
              ? 'The print server has no queues.'
              : `No label queue fits this page (${mm(page.widthMm)} x ${mm(page.heightMm)} mm). Print and Download still work.`}
          </span>
        )}
        {queues.state === 'read' && fitting.length > 0 && (
          <>
            <Select value={chosen?.name ?? ''} onValueChange={pickQueue}>
              <SelectTrigger aria-label="Label queue" size="sm" className="w-auto min-w-56">
                <SelectValue placeholder="Choose a queue">{chosen ? nameOf(chosen) : null}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {fitting.map((queue) => (
                  <SelectItem key={queue.name} value={queue.name}>
                    <span>{nameOf(queue)}</span>{' '}
                    <span className="text-muted-foreground">
                      {STATE_TEXT[queue.state]}
                      {queue.state_message && <>. {shorten(queue.state_message)}</>}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {zebra && (
              <>
                <Select value={options.media_type} onValueChange={(value) => pickOption({ media_type: value as LabelJobOptions['media_type'] })}>
                  <SelectTrigger aria-label="Print mode" size="sm" className="w-auto">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {MEDIA_TYPES.map((one) => (
                      <SelectItem key={one.value} value={one.value}>
                        {one.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Select value={String(options.darkness)} onValueChange={(value) => pickOption({ darkness: Number(value) })}>
                  <SelectTrigger aria-label="Darkness" size="sm" className="w-auto">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {DARKNESS.map((one) => (
                      <SelectItem key={one} value={String(one)}>
                        {one === -1 ? 'Printer default' : String(one)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Select value={options.print_rate} onValueChange={(value) => pickOption({ print_rate: value })}>
                  <SelectTrigger aria-label="Speed" size="sm" className="w-auto">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {RATES.map((one) => (
                      <SelectItem key={one} value={one}>
                        {one === 'Default' ? 'Printer default' : `${one} in/s`}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </>
            )}
            <Button type="button" size="sm" className="ml-auto" disabled={disabled || busy || !chosen} onClick={() => void send()}>
              {busy ? 'Sending…' : 'Print to label printer'}
            </Button>
          </>
        )}
      </div>
      {chosen && (
        <p className="mt-1.5 flex items-start gap-2 text-muted-foreground">
          <span aria-hidden="true" className={cn('mt-1.5 size-2 shrink-0 rounded-full', STATE_DOT[chosen.state])} />
          <span>
            <span className="text-foreground">{STATE_TEXT[chosen.state]}</span>
            {!chosen.accepting && ', not taking jobs'}
            {chosen.state_message && <>. {chosen.state_message}</>}
          </span>
        </p>
      )}
      <div role="status" className="empty:hidden mt-1.5">
        {sent && (
          <>
            <span>{sent.text}</span>
            {sent.message && <span className="block text-warn">{sent.message}</span>}
          </>
        )}
      </div>
      {problem && (
        <p role="alert" className="mt-1.5 text-warn">
          {problem}
        </p>
      )}
    </section>
  )
}
