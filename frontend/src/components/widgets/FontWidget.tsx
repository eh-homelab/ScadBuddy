import { useId, useState } from 'react'
import { ApiError, api } from '../../api/client'
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
  missing = [],
  onInstalled,
  onChange,
}: {
  param: Param
  value: string
  fonts: FontFamily[]
  sampleText?: string
  /** #1286 — families this value names that the last render was refused for: not installed. */
  missing?: string[]
  /** After one of `missing` is installed here, so the caller can render again. */
  onInstalled?: (installed: InstalledFamily) => void
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const listId = useId()
  const [open, setOpen] = useState(false)
  const [justInstalled, setJustInstalled] = useState<Record<string, string[]>>({})
  const [installing, setInstalling] = useState<string | null>(null)
  // Kept with the refusal it answered, so a later refusal of other families starts clean.
  const [installError, setInstallError] = useState<{ missing: string; message: string } | null>(null)

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

  const missingKey = missing.join('\n')
  const shownInstallError = installError?.missing === missingKey ? installError.message : null

  // The same install the picker makes; the value stays as it is, since it already
  // names the family.
  async function install(missingFamily: string): Promise<void> {
    const refused = missingKey
    setInstalling(missingFamily)
    setInstallError(null)
    try {
      const installed = await api.installFont(missingFamily)
      setJustInstalled((current) => ({ ...current, [installed.family]: installed.styles ?? [] }))
      onInstalled?.(installed)
    } catch (cause) {
      setInstallError({
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
        <input
          id={id}
          type="text"
          list={listId}
          value={value}
          spellCheck={false}
          placeholder="Family:style=Bold"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
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

      {missing.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {missing.map((missingFamily) => (
            <button
              key={missingFamily}
              type="button"
              disabled={installing !== null}
              aria-busy={installing === missingFamily}
              onClick={() => void install(missingFamily)}
              className="h-7 rounded-[6px] border border-line bg-surface-2 px-2.5 text-[12px] text-ink hover:border-line-strong disabled:opacity-60"
            >
              {installing === missingFamily ? `Installing ${missingFamily}…` : `Install ${missingFamily}`}
            </button>
          ))}
          {shownInstallError && (
            <p role="alert" className="text-[12px] text-warn">
              {shownInstallError}
            </p>
          )}
        </div>
      )}

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
