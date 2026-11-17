import { useEffect, useState } from 'react'
import { api } from '../../../api/client'
import type { FilamentPresetOption, StorefrontSliceProfile } from '../../../api/types'
import { refKey } from '../../../lib/printChoices'
import { useAsync } from '../../../lib/useAsync'
import { refusalText } from '../../../components/orders/refusal'
import { StorefrontMaterials } from '../../../components/settings/StorefrontMaterials'
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
 * the shop slice profile (a printer and a filament preset per material, #6574), which is saved on its own.
 * #6258 — and the shop's default materials (`storefront_materials`), saved on their own.
 */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description="A visitor's design is sliced as the back office slices a quote: the template gives the support and process settings, and the choices the print dialog opens on give the rest. With no slice profile, the storefront shows no price, and the shop prices each line."
    >
      <RuntimeRows form={form} section={id} />
      {/* #6258 — the shop's default materials, saved on their own. */}
      <StorefrontMaterials />
      <SliceProfileEditor />
    </SavedSection>
  )
}

/** The presets to offer for `material`: those of that material, else every one. */
function presetsFor(material: string, presets: readonly FilamentPresetOption[]): readonly FilamentPresetOption[] {
  const same = presets.filter((preset) => preset.filament_type?.toLowerCase() === material.toLowerCase())
  return same.length > 0 ? same : presets
}

/**
 * #6153, #6574 — the shop slice profile (`storefront_slice_profile`): the registry printer that
 * routes the slice and prices machine time, and one filament preset per material of the price
 * book, from the presets the print dialog's Advanced step offers on that printer. A slot is
 * sliced with its material's preset in the visitor's colour, as a back-office quote slices a
 * filament that is not loaded. Support materials are listed apart: support is the shop's
 * choice, and a visitor never sees it.
 */
function SliceProfileEditor() {
  const settings = useAsync(() => api.getSettings(), [])
  const printers = useAsync(() => api.listPrinters(), [])
  const [printer, setPrinter] = useState<string | null>(null)
  const [chosen, setChosen] = useState<Record<string, string> | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const options = useAsync(() => api.storefrontSliceOptions(printer ?? undefined), [printer])

  useEffect(() => {
    if (settings.data === undefined || chosen !== null) return
    const profile = settings.data.storefront_slice_profile
    setPrinter(profile?.printer ?? null)
    setChosen(Object.fromEntries(Object.entries(profile?.materials ?? {}).map(([material, ref]) => [material, refKey(ref)])))
  }, [settings.data, chosen])

  const picks = chosen ?? {}
  const materials = options.data?.materials ?? []
  const offered = options.data?.presets ?? []
  const size = options.data?.nozzle_size ?? '0.4'
  const save = (profile: StorefrontSliceProfile | null) => {
    setBusy(true)
    setError(null)
    setSaved(false)
    api
      .putSettings({ storefront_slice_profile: profile })
      .then(() => setSaved(true))
      .catch((err: unknown) => setError(refusalText(err)))
      .finally(() => setBusy(false))
  }

  const presetRow = (material: string) => {
    const key = material.toLowerCase()
    const choices = presetsFor(material, offered)
    return (
      <div key={key} className="flex flex-col gap-1">
        <Label>{material}</Label>
        <Select
          value={picks[key] ?? ''}
          onValueChange={(value) => {
            setSaved(false)
            setChosen({ ...picks, [key]: value })
          }}
        >
          <SelectTrigger aria-label={`Filament preset for ${material}`} className="w-72">
            <SelectValue placeholder="No preset" />
          </SelectTrigger>
          <SelectContent position="popper">
            {choices.map((preset) => (
              <SelectItem key={refKey(preset.ref)} value={refKey(preset.ref)}>
                {preset.name}
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
        What a visitor cannot choose: the printer to slice for, and the filament preset each material is sliced with. Each colour slot is sliced in the visitor's colour, as a quote slices a filament that is not loaded, and priced at the price-book filament the visitor picks. The nozzles, quality and plate are the ones the print dialog opens on for the design on this printer. Publish refuses a listing with a material that has no preset here.
      </p>
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault()
          if (printer === null) {
            setError('Choose the printer to slice for.')
            return
          }
          // Until this printer's presets are read, the form shows none: saving then would
          // drop every stored preset (#6255 review).
          if (options.data === undefined) return
          // Only the materials the editor lists, and only presets this printer offers: a
          // preset the form does not show is never sent (#6255 review).
          const byKey = new Map(offered.map((preset) => [refKey(preset.ref), preset.ref]))
          const named = materials.flatMap((m) => {
            const ref = byKey.get(picks[m.material.toLowerCase()] ?? '')
            return ref ? [[m.material.toLowerCase(), ref] as const] : []
          })
          save({ printer, materials: Object.fromEntries(named) })
        }}
      >
        <div className="flex flex-col gap-1">
          <Label>Printer for storefront prices</Label>
          <Select
            value={printer ?? ''}
            onValueChange={(value) => {
              setSaved(false)
              // Another printer may offer other presets: the old choices do not carry over.
              if (value !== printer) setChosen({})
              setPrinter(value)
            }}
          >
            <SelectTrigger aria-label="Printer for storefront prices" className="w-72">
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
          <p className="text-[13px] text-muted-foreground">Choose the printer first: the presets are the ones the print dialog offers on it.</p>
        ) : options.error ? (
          <p role="alert" className="text-[13px] text-warn">
            {refusalText(options.error)}
          </p>
        ) : (
          <>
            <p className="text-xs text-muted-foreground">Presets for a {size} mm nozzle. A design remembered at another size gets no price until its preset fits.</p>
            <fieldset className="flex flex-col gap-2">
              <legend className="text-xs font-medium">Customer materials</legend>
              {materials.filter((m) => !m.support).map((m) => presetRow(m.material))}
            </fieldset>
            {materials.some((m) => m.support) && (
              <fieldset className="flex flex-col gap-2">
                <legend className="text-xs font-medium">Support (internal)</legend>
                {materials.filter((m) => m.support).map((m) => presetRow(m.material))}
              </fieldset>
            )}
          </>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" size="sm" disabled={busy || settings.data === undefined || (printer !== null && options.data === undefined)}>
            Save slice profile
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy || settings.data === undefined}
            onClick={() => {
              setPrinter(null)
              setChosen({})
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
