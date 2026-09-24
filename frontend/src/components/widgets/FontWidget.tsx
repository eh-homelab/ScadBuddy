import { useId, useState } from 'react'
import type { FontFamily, InstalledFamily, Param } from '../../api/types'
import { cssFontFamily, formatFontValue, parseFontValue, preferredStyle } from '../../lib/fonts'
import { Field } from './Field'
import { FontPicker } from './FontPicker'

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
  onChange,
}: {
  param: Param
  value: string
  fonts: FontFamily[]
  sampleText?: string
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const listId = useId()
  const [open, setOpen] = useState(false)
  const [justInstalled, setJustInstalled] = useState<Record<string, string[]>>({})

  const label = param.caption ?? param.name
  const { family, style } = parseFontValue(value)

  const choices = fonts.flatMap((font) =>
    font.styles.length > 0
      ? font.styles.map((name) => `${font.family}:style=${name}`)
      : [font.family],
  )
  const styles =
    justInstalled[family] ?? fonts.find((font) => font.family === family)?.styles ?? []

  function onPicked(installed: InstalledFamily): void {
    const names = installed.styles ?? []
    setJustInstalled((current) => ({ ...current, [installed.family]: names }))
    onChange(formatFontValue(installed.family, preferredStyle(names)))
  }

  return (
    <Field id={id} label={label} name={param.name}>
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="text"
          list={listId}
          value={value}
          spellCheck={false}
          placeholder="Family:style=Bold"
          style={{ fontFamily: cssFontFamily(family) }}
          onChange={(event) => onChange(event.target.value)}
          className="sb-field"
        />
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="h-8 shrink-0 rounded-[6px] border border-line bg-surface-2 px-2.5 text-[12px] text-muted hover:border-line-strong hover:text-ink"
        >
          Browse
        </button>
      </div>

      {styles.length > 1 && (
        <select
          value={style}
          aria-label={`${label} style`}
          onChange={(event) => onChange(formatFontValue(family, event.target.value))}
          className="sb-field mt-2 cursor-pointer"
        >
          {!styles.includes(style) && <option value={style}>{style || 'Default'}</option>}
          {styles.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      )}

      <datalist id={listId} data-testid="font-options">
        {choices.map((choice) => (
          <option key={choice} value={choice} />
        ))}
      </datalist>

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
