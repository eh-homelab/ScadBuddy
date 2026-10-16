import { useState } from 'react'
import type { Param } from '../../api/types'
import { colourHex, paramLabel, rangeProblem } from '../../lib/params'
import { Field } from './Field'
import { FilamentColourPicker } from './FilamentColourPicker'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Separator } from '@/components/ui/separator'

/**
 * A whole `#RRGGBB` as typed: taken at once, before the field loses focus. An 8-digit
 * `#RRGGBBAA` therefore sends one render at its sixth digit; the value on blur is still
 * the whole one, so that render is only an extra step on the way.
 */
const WHOLE_HEX = /^#?[0-9a-f]{6}$/i

export function ColorWidget({
  param,
  value,
  extruder,
  onChange,
}: {
  param: Param
  value: string
  /** null: the latest render never used this colour (#938). */
  extruder?: number | null
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const colour = colourHex(value)
  const hex = colour ?? value
  // #2357 — what is being typed, until it is a whole colour or the field loses focus: a
  // partial or wrong value is never padded into a colour ("red" was sent as "#RREEDD");
  // a colour name becomes its hex.
  const [draft, setDraft] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  // A value set from outside (a preset, a reset) replaces a half-typed draft, so a
  // blur never commits the stale text over it.
  const [shown, setShown] = useState(value)
  if (shown !== value) {
    setShown(value)
    setDraft(null)
  }
  const error = draft === null ? rangeProblem(param, value) : null

  function commit(text: string) {
    setDraft(null)
    // Typed without its `#`, a hex is still taken as one; the value sent always has it.
    const next = colourHex(text) ?? colourHex(`#${text.trim()}`) ?? text.trim()
    // The same colour as the value, only spelt otherwise (a focus and blur of `#ff0000`):
    // not sent, or a lowercase default would read as a change from it.
    if (colour !== null && next === colour) return
    onChange(next)
  }

  return (
    <Field
      id={id}
      label={paramLabel(param)}
      name={param.name}
      error={error}
      readout={
        extruder === null ? (
          <span className="text-muted-foreground">not in this render</span>
        ) : extruder ? (
          <span className="text-foreground">extruder {extruder}</span>
        ) : undefined
      }
    >
      <div className="flex items-center gap-2">
        {/* #3672 — the swatch opens the ways to pick: any colour, or a filament's. */}
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={`Choose ${paramLabel(param)}`}
              style={{ background: colour ?? 'transparent' }}
              className="size-8 shrink-0 cursor-pointer rounded-lg border border-input ring-1 ring-black/10 ring-inset outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </PopoverTrigger>
          <PopoverContent align="start" className="w-80">
            <label className="flex items-center gap-2 text-xs font-medium">
              <input
                type="color"
                // The picker takes only #rrggbb; while the text is no colour it shows black.
                value={(colour ?? '#000000').toLowerCase()}
                // Not the caption itself, which names the hex field: fill finds that one alone.
                // That holds while the popover is non-modal; a modal one would narrow fill's
                // search to itself (agent/dom activeDialog) and find no field by the caption.
                aria-label={`${paramLabel(param)} picker`}
                onChange={(event) => commit(event.target.value)}
                className="h-8 w-14 shrink-0 cursor-pointer rounded-md border border-input bg-transparent p-0.5"
              />
              Any colour
            </label>
            <Separator />
            <p className="text-xs font-medium">From a filament</p>
            <FilamentColourPicker
              label={paramLabel(param)}
              value={hex}
              onPick={(picked) => {
                // Through commit: the swatch of the colour already set sends nothing.
                commit(picked)
                setOpen(false)
              }}
            />
          </PopoverContent>
        </Popover>
        <Input
          id={id}
          type="text"
          value={draft ?? hex}
          // Named by the Field's label, so the agent's fill finds the colour by its caption.
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
          spellCheck={false}
          onChange={(event) => {
            const text = event.target.value.toUpperCase()
            if (WHOLE_HEX.test(text.trim())) commit(text)
            else setDraft(text)
          }}
          onBlur={(event) => commit(event.target.value)}
          className="sb-num uppercase"
        />
      </div>
    </Field>
  )
}
