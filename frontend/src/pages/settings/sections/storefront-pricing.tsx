import { useEffect, useState } from 'react'
import { api } from '../../../api/client'
import type { ProfileSpool } from '../../../api/types'
import { useAsync } from '../../../lib/useAsync'
import { refusalText } from '../../../components/orders/refusal'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'storefront-pricing'
export const area: AreaId = 'printing'
export const title = 'Storefront pricing'
export const order = 45

/**
 * #6153 — how the storefront slices a visitor's design to price it: the slices at once, and
 * the shop slice profile (a printer and a spool per material), which is saved on its own.
 */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description="A visitor's design is sliced as the back office slices a quote: the template gives the support and process settings, and the print dialog's remembered choices give the rest. With no slice profile, the storefront shows no price, and the shop prices each line."
    >
      <RuntimeRows form={form} section={id} />
      <SliceProfileEditor />
    </SavedSection>
  )
}

/** The spools to offer for `material`: those of that material, else every one. */
function spoolsFor(material: string, spools: readonly ProfileSpool[]): readonly ProfileSpool[] {
  const same = spools.filter((spool) => spool.material.toLowerCase() === material.toLowerCase())
  return same.length > 0 ? same : spools
}

/**
 * #6153 — the shop slice profile (`storefront_slice_profile`): the registry printer that
 * routes the slice and prices machine time, and one spool per material of the price book,
 * from the spools the print dialog offers on that printer. Support materials are listed
 * apart: support is the shop's choice, and a visitor never sees it.
 */
function SliceProfileEditor() {
  const settings = useAsync(() => api.getSettings(), [])
  const printers = useAsync(() => api.listPrinters(), [])
  const [printer, setPrinter] = useState<string | null>(null)
  const [spools, setSpools] = useState<Record<string, string> | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const options = useAsync(() => api.storefrontSliceOptions(printer ?? undefined), [printer])

  useEffect(() => {
    if (settings.data === undefined || spools !== null) return
    const profile = settings.data.storefront_slice_profile
    setPrinter(profile?.printer ?? null)
    setSpools(Object.fromEntries(Object.entries(profile?.spools ?? {}).map(([material, spool]) => [material, String(spool)])))
  }, [settings.data, spools])

  const chosen = spools ?? {}
  const materials = options.data?.materials ?? []
  const offered = options.data?.spools ?? []
  const save = (profile: { printer: string; spools: Record<string, number> } | null) => {
    setBusy(true)
    setError(null)
    setSaved(false)
    api
      .putSettings({ storefront_slice_profile: profile })
      .then(() => setSaved(true))
      .catch((err: unknown) => setError(refusalText(err)))
      .finally(() => setBusy(false))
  }

  const spoolRow = (material: string) => {
    const key = material.toLowerCase()
    const choices = spoolsFor(material, offered)
    return (
      <div key={key} className="flex flex-col gap-1">
        <Label>{material}</Label>
        <Select
          value={chosen[key] ?? ''}
          onValueChange={(value) => {
            setSaved(false)
            setSpools({ ...chosen, [key]: value })
          }}
        >
          <SelectTrigger aria-label={`Spool for ${material}`} className="w-72">
            <SelectValue placeholder="No spool" />
          </SelectTrigger>
          <SelectContent position="popper">
            {choices.map((spool) => (
              <SelectItem key={spool.spool_id} value={String(spool.spool_id)}>
                {spool.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    )
  }

  return (
    <section aria-label="Shop slice profile" className="space-y-2 rounded-md border p-3">
      <h4 className="text-sm font-medium">Shop slice profile</h4>
      <p className="text-xs text-muted-foreground">
        What a visitor cannot choose: the printer to slice for, and the spool whose filament preset slices each material. The colour comes from the design, and the price from the price-book filament the visitor picks. Publish refuses a listing with a material that has no spool here.
      </p>
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault()
          if (printer === null) {
            setError('Choose the printer to slice for.')
            return
          }
          const named = Object.entries(chosen).filter(([, spool]) => spool !== '')
          save({ printer, spools: Object.fromEntries(named.map(([material, spool]) => [material, Number(spool)])) })
        }}
      >
        <div className="flex flex-col gap-1">
          <Label>Printer</Label>
          <Select
            value={printer ?? ''}
            onValueChange={(value) => {
              setSaved(false)
              setPrinter(value)
            }}
          >
            <SelectTrigger aria-label="Printer" className="w-72">
              <SelectValue placeholder="No printer" />
            </SelectTrigger>
            <SelectContent position="popper">
              {(printers.data ?? []).map((row) => (
                <SelectItem key={row.id} value={row.id}>
                  {row.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {printer === null ? (
          <p className="text-[13px] text-muted-foreground">Choose the printer first: the spools are the ones the print dialog offers on it.</p>
        ) : options.error ? (
          <p role="alert" className="text-[13px] text-warn">
            {refusalText(options.error)}
          </p>
        ) : (
          <>
            <fieldset className="flex flex-col gap-2">
              <legend className="text-xs font-medium">Customer materials</legend>
              {materials.filter((m) => !m.support).map((m) => spoolRow(m.material))}
            </fieldset>
            {materials.some((m) => m.support) && (
              <fieldset className="flex flex-col gap-2">
                <legend className="text-xs font-medium">Support (internal)</legend>
                {materials.filter((m) => m.support).map((m) => spoolRow(m.material))}
              </fieldset>
            )}
          </>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" size="sm" disabled={busy || settings.data === undefined}>
            Save slice profile
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy || settings.data === undefined}
            onClick={() => {
              setPrinter(null)
              setSpools({})
              save(null)
            }}
          >
            Remove slice profile
          </Button>
          {saved && <span className="text-xs text-muted-foreground">Saved.</span>}
        </div>
      </form>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
