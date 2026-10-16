import { useId, useRef, useState } from 'react'
import { useCommandState } from 'cmdk'
import { useFillable } from '../../agent/useFillable'
import { ApiError, api } from '../../api/client'
import type { FontFamily, InstalledFamily, Param } from '../../api/types'
import { cssFontFamily, formatFontValue, parseFontValue, preferredStyle } from '../../lib/fonts'
import { forgetGlyphAnswers, useMissingGlyphs } from '../../lib/useMissingGlyphs'
import { Field } from './Field'
import { FontPicker } from './FontPicker'
import { paramLabel } from '../../lib/params'
import { Button } from '@/components/ui/button'
import { Command, CommandGroup, CommandItem, CommandList } from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Spinner } from '../ui/Spinner'

/** A style that is "" (none named): Radix refuses an empty item value. */
const STYLE_DEFAULT = '\u0000default'

/** The most completions shown at once: the rest are a few more letters away. */
const MAX_SUGGESTIONS = 50

/**
 * The field itself, a combobox over the installed `Family:style=…` values (#3672, was a
 * `<datalist>`). It stays free text: a model can name a font no catalogue lists, and
 * Enter only takes a completion while the list is open.
 */
function FontField({
  id,
  value,
  choices,
  family,
  invalid,
  describedBy,
  onChange,
}: {
  id: string
  value: string
  choices: string[]
  family: string
  invalid: boolean
  describedBy: string | undefined
  onChange: (next: string) => void
}) {
  const listId = useId()
  const anchor = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const typed = value.trim().toLowerCase()
  const matches = choices.filter((choice) => choice.toLowerCase().includes(typed)).slice(0, MAX_SUGGESTIONS)
  // Nothing to complete once the value is the one match.
  const shown = open && matches.length > 0 && !(matches.length === 1 && matches[0] === value)
  return (
    <Popover open={shown} onOpenChange={setOpen}>
      <Command shouldFilter={false} loop className="h-auto flex-1 overflow-visible rounded-none! bg-transparent p-0">
        <PopoverAnchor asChild>
          <div ref={anchor} className="w-full">
            <ActiveInput
              id={id}
              value={value}
              listId={listId}
              expanded={shown}
              invalid={invalid}
              describedBy={describedBy}
              family={family}
              onType={(next) => {
                setOpen(true)
                onChange(next)
              }}
              onOpenList={() => setOpen(true)}
              onCloseList={() => setOpen(false)}
            />
          </div>
        </PopoverAnchor>
        <PopoverContent
          // What the field's aria-controls names: cmdk keeps the list's own id to itself.
          id={listId}
          align="start"
          className="w-(--radix-popover-trigger-width) min-w-64 p-1"
          // The field keeps the focus: the list is completion, not a place to go.
          onOpenAutoFocus={(event) => event.preventDefault()}
          // Nor does a press on the list take it: the field's blur would close the list
          // before the click that picks the option lands (#3672 review).
          onMouseDown={(event) => event.preventDefault()}
          // A press in the field is no press outside. By the event's path, since in a
          // template UI's shadow root the target the document sees is the host element.
          onInteractOutside={(event) => {
            if (anchor.current && event.detail.originalEvent.composedPath().includes(anchor.current)) event.preventDefault()
          }}
        >
          <CommandList label="Installed fonts" className="max-h-64">
            <CommandGroup>
              {matches.map((choice) => (
                <CommandItem
                  key={choice}
                  value={choice}
                  onSelect={() => {
                    onChange(choice)
                    setOpen(false)
                  }}
                  style={{ fontFamily: cssFontFamily(parseFontValue(choice).family) }}
                >
                  {choice}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </PopoverContent>
      </Command>
    </Popover>
  )
}

/** The input, inside the Command so its arrows and Enter drive the list. */
function ActiveInput({
  id,
  value,
  listId,
  expanded,
  invalid,
  describedBy,
  family,
  onType,
  onOpenList,
  onCloseList,
}: {
  id: string
  value: string
  listId: string
  expanded: boolean
  invalid: boolean
  describedBy: string | undefined
  family: string
  onType: (next: string) => void
  onOpenList: () => void
  onCloseList: () => void
}) {
  const active = useCommandState((state) => state.selectedItemId)
  return (
    <Input
      id={id}
      type="text"
      role="combobox"
      aria-autocomplete="list"
      aria-expanded={expanded}
      aria-controls={expanded ? listId : undefined}
      aria-activedescendant={expanded ? active : undefined}
      value={value}
      spellCheck={false}
      autoComplete="off"
      placeholder="Family:style=Bold"
      aria-invalid={invalid ? true : undefined}
      aria-describedby={describedBy}
      style={{ fontFamily: cssFontFamily(family) }}
      onChange={(event) => onType(event.target.value)}
      onBlur={onCloseList}
      onKeyDown={(event) => {
        if (event.key === 'ArrowDown' && !expanded) onOpenList()
        // Enter with the list closed is the value as typed: not cmdk's to take.
        if (event.key === 'Enter' && !expanded) event.stopPropagation()
        if (event.key === 'Escape' && expanded) {
          event.preventDefault()
          event.stopPropagation()
          onCloseList()
        }
      }}
    />
  )
}

/**
 * OpenSCAD font values look like `Liberation Sans:style=Bold`. Browse opens the Google
 * Fonts picker, which installs what it is given; the input itself stays free text
 * because a model can name a font no catalogue lists.
 *
 * The styles a freshly installed family offers are kept here rather than re-read from
 * `GET /fonts`: they come back on the install response, already as fontconfig's own
 * names, which is exactly what `Family:style=…` has to carry.
 */
export function FontWidget({
  param,
  value,
  fonts,
  sampleText = '',
  missing = [],
  refusal,
  onInstalled,
  onChange,
}: {
  param: Param
  value: string
  fonts: FontFamily[]
  sampleText?: string
  /** #1286 — families this value names that the last render was refused for: not installed. */
  missing?: string[]
  /**
   * The refusal `missing` answers, by identity (#2215): an install error belongs to
   * it, so a later refusal of the same families starts clean. Without one, the
   * error is kept with the families themselves.
   */
  refusal?: object
  /** After one of `missing` is installed here, so the caller can render again. */
  onInstalled?: (installed: InstalledFamily) => void
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const [open, setOpen] = useState(false)
  const [justInstalled, setJustInstalled] = useState<Record<string, string[]>>({})
  const [installing, setInstalling] = useState<string | null>(null)
  // Kept with the refusal it answered, so a later refusal starts clean (#2215).
  const [installError, setInstallError] = useState<{
    refusal: object | undefined
    missing: string
    message: string
  } | null>(null)

  const label = paramLabel(param)
  const { family, style } = parseFontValue(value)
  // #2358 — characters of the model's text this font has no glyph for: the render
  // leaves them out without a word. Not asked while the family is not installed, which
  // the error above says instead.
  const lacking = useMissingGlyphs(value, sampleText, missing.length === 0)

  const choices = fonts.flatMap((font) =>
    font.styles.length > 0
      ? font.styles.map((name) => `${font.family}:style=${name}`)
      : [font.family],
  )
  const styles =
    justInstalled[family] ?? fonts.find((font) => font.family === family)?.styles ?? []

  function onPicked(installed: InstalledFamily): void {
    forgetGlyphAnswers()
    const names = installed.styles ?? []
    setJustInstalled((current) => ({ ...current, [installed.family]: names }))
    onChange(formatFontValue(installed.family, preferredStyle(names)))
  }

  const styleTrigger = useRef<HTMLButtonElement>(null)
  useFillable(styleTrigger, {
    // As the list offers them: the current style first when the family does not list it.
    options: () => [
      ...(styles.includes(style) ? [] : [{ value: style, label: style || 'Default' }]),
      ...styles.map((name) => ({ value: name, label: name })),
    ],
    value: () => style,
    set: (next) => onChange(formatFontValue(family, next)),
  })

  const missingKey = missing.join('\n')
  const shownInstallError =
    installError && installError.refusal === refusal && installError.missing === missingKey ? installError.message : null

  // The same install the picker makes; the value stays as it is, since it already
  // names the family.
  async function install(missingFamily: string): Promise<void> {
    const refused = missingKey
    const answering = refusal
    setInstalling(missingFamily)
    setInstallError(null)
    try {
      const installed = await api.installFont(missingFamily)
      setJustInstalled((current) => ({ ...current, [installed.family]: installed.styles ?? [] }))
      forgetGlyphAnswers()
      onInstalled?.(installed)
    } catch (cause) {
      setInstallError({
        refusal: answering,
        missing: refused,
        message: cause instanceof ApiError ? cause.detail : `${missingFamily} could not be installed. Check the connection.`,
      })
    } finally {
      setInstalling(null)
    }
  }

  const error =
    missing.length === 0
      ? null
      : `${missing.join(' and ')} ${missing.length === 1 ? 'is' : 'are'} not installed, so this would render in the default font.`

  return (
    <Field id={id} label={label} name={param.name} error={error}>
      <div className="flex items-center gap-2">
        <FontField
          id={id}
          value={value}
          choices={choices}
          family={family}
          invalid={Boolean(error)}
          describedBy={error ? `${id}-error` : lacking.length > 0 ? `${id}-glyphs` : undefined}
          onChange={onChange}
        />
        <Button type="button" variant="outline" onClick={() => setOpen(true)}>
          Browse
        </Button>
      </div>

      {lacking.length > 0 && (
        <p id={`${id}-glyphs`} role="status" data-testid="font-lacks" className="text-xs text-destructive">
          {family} has no glyph for {lacking.join(' ')}; {lacking.length === 1 ? 'it is' : 'they are'} left out
          of the print.
        </p>
      )}

      {missing.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {missing.map((missingFamily) => (
            <Button
              key={missingFamily}
              type="button"
              variant="secondary"
              size="sm"
              disabled={installing !== null}
              aria-busy={installing === missingFamily}
              onClick={() => void install(missingFamily)}
            >
              {installing === missingFamily && <Spinner />}
              {installing === missingFamily ? `Installing ${missingFamily}…` : `Install ${missingFamily}`}
            </Button>
          ))}
          {shownInstallError && (
            <p role="alert" className="text-xs text-destructive">
              {shownInstallError}
            </p>
          )}
        </div>
      )}

      {styles.length > 1 && (
        <Select value={style || STYLE_DEFAULT} onValueChange={(next) => onChange(formatFontValue(family, next === STYLE_DEFAULT ? '' : next))}>
          <SelectTrigger ref={styleTrigger} aria-label={`${label} style`} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            {!styles.includes(style) && <SelectItem value={style || STYLE_DEFAULT}>{style || 'Default'}</SelectItem>}
            {styles.map((name) => (
              <SelectItem key={name} value={name}>
                {name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}

      <FontPicker
        open={open}
        family={family}
        sampleText={sampleText}
        installed={fonts}
        onClose={() => setOpen(false)}
        onPick={onPicked}
      />
    </Field>
  )
}
