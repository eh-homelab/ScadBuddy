import { useEffect, useId, useMemo, useState } from 'react'
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
import { useMissingGlyphs } from '../../lib/useMissingGlyphs'
import { Spinner } from '../ui/spinner'
import { Command, CommandEmpty, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

const SEARCH_DEBOUNCE_MS = 250
/** "All" is the empty category, which a toggle item cannot carry as its value. */
const ALL = 'all'
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

/**
 * #2358 — an installed family's missing glyphs for the sample. The specimen beside it
 * cannot show them: the browser draws a glyph a font lacks in a fallback font, so every
 * row looks complete. Only an installed family can be asked: the server reads its file.
 */
function RowLacks({ family, sample }: { family: string; sample: string }) {
  const lacking = useMissingGlyphs(family, sample)
  if (lacking.length === 0) return null
  return <span className="text-destructive">no {lacking.join(' ')}</span>
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

  const sampleId = useId()
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] grid-rows-[auto_minmax(0,1fr)] sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Choose a font</DialogTitle>
          <DialogDescription>Google Fonts. Picking one downloads it for the renderer.</DialogDescription>
        </DialogHeader>
        {/* #3672 — cmdk for the rows and the keys; the search is the server's, so no filter here. */}
        <Command shouldFilter={false} label="Search fonts" className="min-h-0 gap-3 rounded-none! bg-transparent p-0">
          <CommandInput value={query} onValueChange={setQuery} placeholder="Search fonts" />

          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            spacing={1}
            aria-label="Category"
            value={category || ALL}
            // A second press on the chosen one would clear it: there is always a category.
            onValueChange={(next) => next && setCategory(next === ALL ? '' : next)}
            className="flex-wrap"
          >
            {FONT_CATEGORIES.map((chip) => (
              <ToggleGroupItem key={chip.label} value={chip.value || ALL} className="rounded-full px-3">
                {chip.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>

          <div className="flex items-center gap-2">
            <Label htmlFor={sampleId} className="font-normal text-muted-foreground">
              Preview
            </Label>
            <Input
              id={sampleId}
              type="text"
              value={sample}
              aria-label="Sample text"
              onChange={(event) => setSample(event.target.value)}
            />
          </div>

          {offline && (
            <p role="status" className="rounded-lg bg-muted px-2.5 py-1.5 text-xs text-muted-foreground">
              Google Fonts is unreachable — showing the fonts already installed.
            </p>
          )}
          {error && (
            <p role="alert" className="rounded-lg bg-destructive/10 px-2.5 py-1.5 text-xs text-destructive">
              {error}
            </p>
          )}

          <CommandList data-testid="font-rows" label="Fonts" className="max-h-[46vh] min-h-24">
            {catalogue.loading && !offline ? (
              <div role="status" aria-label="Loading the catalogue" className="flex flex-col gap-2 p-1">
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
              </div>
            ) : (
              <CommandEmpty>No font matches that.</CommandEmpty>
            )}
            {rows.map((row) => (
              <CommandItem
                key={row.family}
                value={row.family}
                disabled={busy !== null}
                aria-busy={busy === row.family}
                data-checked={row.family === family}
                onSelect={() => void pick(row.family)}
                className="items-baseline justify-between gap-3 py-2"
              >
                {/* The specimen is decorative: it repeats the SAME sample word on every
                    row, so leaving it in the accessible name makes each row announce as
                    "<sample> <family>" — the sample forty times over, and the family
                    (the only thing that distinguishes one row from the next) last. Hidden
                    here so the row is named by its family alone. */}
                <span
                  aria-hidden="true"
                  className="truncate text-xl leading-tight"
                  style={{ fontFamily: cssFontFamily(row.family) }}
                >
                  {sample || row.family}
                </span>
                <span className="ml-auto flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
                  {busy === row.family && <Spinner />}
                  {row.family}
                  {row.family === family && <span className="sr-only">(chosen)</span>}
                  {row.installed && <span className="text-ok">installed</span>}
                  {row.installed && <RowLacks family={row.family} sample={sample} />}
                </span>
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  )
}
