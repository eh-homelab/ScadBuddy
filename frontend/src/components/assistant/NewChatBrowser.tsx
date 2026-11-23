import { useId, useState } from 'react'
import { ChevronDownIcon, MonitorIcon, SmartphoneIcon } from 'lucide-react'
import { browserProblem, PHONE, presetOf, type BrowserDraft } from '../../agent/chat/browserDraft'
import { Button } from '../ui/button'
import { Input } from '../ui/input'

/** What the toggle shows beside "Browser": the preset, or the locale alone, or nothing. */
function summary(d: BrowserDraft): string | null {
  const preset = presetOf(d)
  if (preset === 'phone') return 'Phone'
  if (preset === 'desktop') return d.locale.trim() || null
  return 'Custom'
}

interface Props {
  value: BrowserDraft
  onChange: (value: BrowserDraft) => void
  disabled?: boolean
}

/**
 * The "Browser" disclosure: a toggle beside the composer's buttons, and its settings on a
 * row of their own under them (`basis-full` in the composer's wrapping action row).
 */
export function NewChatBrowser({ value, onChange, disabled }: Props) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const panelId = `${id}-panel`
  const problemId = `${id}-problem`
  const preset = presetOf(value)
  const shown = summary(value)
  const problem = browserProblem(value)
  const set = (patch: Partial<BrowserDraft>) => onChange({ ...value, ...patch })

  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        aria-expanded={open}
        aria-controls={panelId}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        Browser
        {shown && <span className="max-w-24 truncate font-normal text-muted-foreground">{shown}</span>}
        <ChevronDownIcon data-icon="inline-end" className={`transition-transform motion-reduce:transition-none ${open ? 'rotate-180' : ''}`} />
      </Button>
      {open && (
        <fieldset
          id={panelId}
          aria-label="This chat's browser"
          disabled={disabled}
          className="order-last min-w-0 basis-full rounded-lg border border-border bg-muted/40 p-2 text-[12px]"
        >
          <div className="flex flex-wrap items-center gap-2">
            <p className="min-w-0 flex-1 text-[11.5px] text-muted-foreground">
              What the assistant's web browser reports in this chat. It stays fixed once the chat starts.
            </p>
            <div className="flex gap-1">
              <Button
                size="xs"
                variant={preset === 'desktop' ? 'secondary' : 'ghost'}
                aria-pressed={preset === 'desktop'}
                onClick={() => set({ userAgent: '', width: '', height: '' })}
              >
                <MonitorIcon data-icon="inline-start" />
                Desktop (default)
              </Button>
              <Button
                size="xs"
                variant={preset === 'phone' ? 'secondary' : 'ghost'}
                aria-pressed={preset === 'phone'}
                onClick={() => set(PHONE)}
              >
                <SmartphoneIcon data-icon="inline-start" />
                Phone
              </Button>
            </div>
          </div>
          <div className="mt-2 grid grid-cols-3 gap-x-2 gap-y-1.5">
            <label className="col-span-3 flex flex-col gap-0.5">
              <span className="text-muted-foreground">User agent</span>
              <Input
                value={value.userAgent}
                onChange={(e) => set({ userAgent: e.target.value })}
                placeholder="Default (desktop Chrome)"
                spellCheck={false}
                autoComplete="off"
                className="h-7 font-mono text-[11.5px] md:text-[11.5px]"
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="text-muted-foreground">Width</span>
              <Input
                type="number"
                inputMode="numeric"
                value={value.width}
                onChange={(e) => set({ width: e.target.value })}
                placeholder="Default"
                aria-invalid={problem && !value.width.trim() ? true : undefined}
                aria-describedby={problem ? problemId : undefined}
                className="h-7 text-[12px] md:text-[12px]"
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="text-muted-foreground">Height</span>
              <Input
                type="number"
                inputMode="numeric"
                value={value.height}
                onChange={(e) => set({ height: e.target.value })}
                placeholder="Default"
                aria-invalid={problem && !value.height.trim() ? true : undefined}
                aria-describedby={problem ? problemId : undefined}
                className="h-7 text-[12px] md:text-[12px]"
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="text-muted-foreground">Locale</span>
              <Input
                value={value.locale}
                onChange={(e) => set({ locale: e.target.value })}
                placeholder="Default"
                spellCheck={false}
                autoComplete="off"
                className="h-7 text-[12px] md:text-[12px]"
              />
            </label>
          </div>
          {problem && (
            <p id={problemId} className="mt-1.5 text-warn">
              {problem}
            </p>
          )}
        </fieldset>
      )}
    </>
  )
}
