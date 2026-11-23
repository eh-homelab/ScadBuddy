import { useId } from 'react'
import { ChevronDownIcon, MonitorIcon, SmartphoneIcon } from 'lucide-react'
import { browserProblem, PHONE, presetOf, type BrowserDraft } from '../../agent/chat/browserDraft'
import { Button } from '../ui/button'
import { Input } from '../ui/input'

// #7027 — the composer's "Browser" disclosure for a new chat: the toggle sits among the
// composer's buttons, and the panel is rendered after Send (the composer holds `open`), so
// the tab order follows what is on screen. Every button is `type="button"`: they sit inside
// the composer's form, and a submit would send the draft with the options not yet set.

/** What the toggle shows beside "Browser": the preset, or the locale alone, or nothing. */
function summary(d: BrowserDraft): string | null {
  const preset = presetOf(d)
  if (preset === 'phone') return 'Phone'
  if (preset === 'desktop') return d.locale.trim() || null
  return 'Custom'
}

interface ToggleProps {
  value: BrowserDraft
  open: boolean
  onOpenChange: (open: boolean) => void
  panelId: string
  disabled?: boolean
}

export function NewChatBrowserToggle({ value, open, onOpenChange, panelId, disabled }: ToggleProps) {
  const shown = summary(value)
  return (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      aria-expanded={open}
      aria-controls={panelId}
      disabled={disabled}
      onClick={() => onOpenChange(!open)}
    >
      Browser
      {shown && <span className="max-w-24 truncate font-normal text-muted-foreground">{shown}</span>}
      <ChevronDownIcon data-icon="inline-end" className={`transition-transform motion-reduce:transition-none ${open ? 'rotate-180' : ''}`} />
    </Button>
  )
}

interface PanelProps {
  value: BrowserDraft
  onChange: (value: BrowserDraft) => void
  id: string
  disabled?: boolean
}

/** A side the viewport cannot take: empty while the other is set, or not a whole number. */
const badSide = (side: string, other: string) => {
  const v = side.trim()
  return v ? !Number.isInteger(Number(v)) : Boolean(other.trim())
}

export function NewChatBrowserPanel({ value, onChange, id, disabled }: PanelProps) {
  const problemId = `${useId()}-problem`
  const preset = presetOf(value)
  const problem = browserProblem(value)
  const set = (patch: Partial<BrowserDraft>) => onChange({ ...value, ...patch })
  const sideProps = (side: string, other: string) => ({
    type: 'number',
    inputMode: 'numeric' as const,
    step: 1,
    placeholder: 'Default',
    'aria-invalid': problem && badSide(side, other) ? true : undefined,
    'aria-describedby': problem ? problemId : undefined,
    className: 'h-7 text-[12px] md:text-[12px]',
  })

  return (
    <fieldset id={id} disabled={disabled} className="mt-1.5 min-w-0 rounded-lg border border-border bg-muted/40 p-2 text-[12px]">
      <legend className="sr-only">This chat's browser</legend>
      <div className="flex flex-wrap items-center gap-2">
        <p className="min-w-0 flex-1 text-[11.5px] text-muted-foreground">
          What the assistant's web browser reports in this chat. It stays fixed once the chat starts.
        </p>
        <div className="flex gap-1">
          <Button
            type="button"
            size="xs"
            variant={preset === 'desktop' ? 'secondary' : 'ghost'}
            aria-pressed={preset === 'desktop'}
            onClick={() => set({ userAgent: '', width: '', height: '' })}
          >
            <MonitorIcon data-icon="inline-start" />
            Desktop (default)
          </Button>
          <Button
            type="button"
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
          <Input value={value.width} onChange={(e) => set({ width: e.target.value })} {...sideProps(value.width, value.height)} />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-muted-foreground">Height</span>
          <Input value={value.height} onChange={(e) => set({ height: e.target.value })} {...sideProps(value.height, value.width)} />
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
  )
}
