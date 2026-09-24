import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { CatalogueFont, FontFamily, InstalledFamily } from '../../api/types'
import {
  FONT_CATEGORIES,
  cssFontFamily,
  googleFontsCssUrl,
  installedAsCatalogue,
  orderFonts,
  readRecentFonts,
  rememberFont,
} from '../../lib/fonts'
import { useAsync } from '../../lib/useAsync'
import { useDebounced } from '../../lib/useDebounced'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

const SEARCH_DEBOUNCE_MS = 250
const ROW_LIMIT = 40

/**
 * The preview is real: each row is drawn in its own family, pulled from the Google
 * Fonts CSS endpoint by the browser. That is the only part of this feature the browser
 * talks to Google for — the catalogue and the download both go through the server, so
 * the API key never leaves it.
 */
function useGoogleFontsStylesheet(families: string[], sample: string): void {
  const href = googleFontsCssUrl(families, sample)
  useEffect(() => {
    if (!href) return
    const link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = href
    document.head.append(link)
    return () => link.remove()
  }, [href])
}

interface Props {
  open: boolean
  /** The family currently on the parameter, so the picker can mark it. */
  family: string
  /** Defaults to the text the model will actually set — the keychain's name. */
  sampleText: string
  /** What fontconfig already resolves, which is also the offline list. */
  installed: FontFamily[]
  onClose: () => void
  onPick: (installedFamily: InstalledFamily) => void
}

export function FontPicker({
  open,
  family,
  sampleText,
  installed,
  onClose,
  onPick,
}: Props) {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')
  const [sample, setSample] = useState(sampleText)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | undefined>(undefined)
  const [recent, setRecent] = useState<string[]>(() => readRecentFonts())

  const search = useDebounced(query.trim(), SEARCH_DEBOUNCE_MS)
  const catalogue = useAsync(
    () =>
      open
        ? api.listFontCatalogue({ q: search, category, limit: ROW_LIMIT })
        : Promise.resolve(undefined),
    [open, search, category],
  )

  // Reopening with a different name on the model should preview that name.
  useEffect(() => {
    if (open) setSample(sampleText)
  }, [open, sampleText])

  const installedNames = useMemo(
    () => new Set(installed.map((font) => font.family)),
    [installed],
  )

  const offline = catalogue.error !== undefined
  const rows: CatalogueFont[] = useMemo(() => {
    if (offline) {
      const needle = search.toLowerCase()
      return installedAsCatalogue(installed).filter((font) =>
        font.family.toLowerCase().includes(needle),
      )
    }
    const fonts = catalogue.data?.fonts ?? []
    // A search is already ranked by relevance server side; only browsing is reordered.
    return search ? fonts : orderFonts(fonts, { recent, installed: installedNames })
  }, [offline, catalogue.data, search, installed, recent, installedNames])

  useGoogleFontsStylesheet(
    rows.map((row) => row.family),
    `${sample}${family}`,
  )

  async function pick(chosen: string): Promise<void> {
    setBusy(chosen)
    setError(undefined)
    try {
      const result = await api.installFont(chosen)
      setRecent(rememberFont(result.family))
      onPick(result)
      onClose()
    } catch (cause) {
      // Reported here rather than left to show up as a font-less render.
      setError(
        cause instanceof ApiError
          ? cause.detail
          : `${chosen} could not be installed. Check the connection.`,
      )
    } finally {
      setBusy(null)
    }
  }

  return (
    <Dialog
      open={open}
      title="Choose a font"
      description="Google Fonts. Picking one downloads it for the renderer."
      onClose={onClose}
    >
      <div className="flex flex-col gap-3">
        <input
          type="search"
          value={query}
          aria-label="Search fonts"
          placeholder="Search fonts"
          autoFocus
          onChange={(event) => setQuery(event.target.value)}
          className="sb-field"
        />

        <div role="group" aria-label="Category" className="flex flex-wrap gap-1.5">
          {FONT_CATEGORIES.map((chip) => {
            const selected = chip.value === category
            return (
              <button
                key={chip.label}
                type="button"
                aria-pressed={selected}
                onClick={() => setCategory(chip.value)}
                className={`rounded-full border px-2.5 py-0.5 text-[12px] transition-colors ${
                  selected
                    ? 'border-accent bg-accent/15 text-ink'
                    : 'border-line text-muted hover:text-ink'
                }`}
              >
                {chip.label}
              </button>
            )
          })}
        </div>

        <label className="flex items-center gap-2 text-[12px] text-muted">
          Preview
          <input
            type="text"
            value={sample}
            aria-label="Sample text"
            onChange={(event) => setSample(event.target.value)}
            className="sb-field"
          />
        </label>

        {offline && (
          <p role="status" className="rounded-[6px] bg-surface-2 px-2.5 py-1.5 text-[12px] text-muted">
            Google Fonts is unreachable — showing the fonts already installed.
          </p>
        )}
        {error && (
          <p role="alert" className="rounded-[6px] bg-warn/10 px-2.5 py-1.5 text-[12px] text-warn">
            {error}
          </p>
        )}

        <ul data-testid="font-rows" className="max-h-[46vh] min-h-24 overflow-y-auto">
          {catalogue.loading && !offline && (
            <li className="flex items-center gap-2 px-1 py-3 text-[13px] text-muted">
              <Spinner /> Loading the catalogue
            </li>
          )}
          {!catalogue.loading && rows.length === 0 && (
            <li className="px-1 py-3 text-[13px] text-muted">No font matches that.</li>
          )}
          {rows.map((row) => (
            <li key={row.family}>
              <button
                type="button"
                disabled={busy !== null}
                aria-busy={busy === row.family}
                aria-current={row.family === family ? 'true' : undefined}
                onClick={() => void pick(row.family)}
                className={`flex w-full items-baseline justify-between gap-3 rounded-[6px] px-2 py-2 text-left hover:bg-surface-2 disabled:opacity-50 ${
                  row.family === family ? 'bg-surface-2' : ''
                }`}
              >
                <span
                  className="truncate text-[20px] leading-tight text-ink"
                  style={{ fontFamily: cssFontFamily(row.family) }}
                >
                  {sample || row.family}
                </span>
                <span className="flex shrink-0 items-center gap-1.5 text-[11px] text-faint">
                  {busy === row.family && <Spinner />}
                  {row.family}
                  {row.installed && <span className="text-ok">installed</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </Dialog>
  )
}
