import { useEffect, useState } from 'react'
import { api } from '../../../api/client'
import type { ProfileType, StorefrontSliceProfile } from '../../../api/types'
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
 * the shop slice profile (a printer and a filament type per material, #6574), which is saved on its own.
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

/** The types to offer for `material`: those of that material, else every one. */
function typesFor(material: string, types: readonly ProfileType[]): readonly ProfileType[] {
  const same = types.filter((type) => type.material.toLowerCase() === material.toLowerCase())
  return same.length > 0 ? same : types
}

/**
 * #6153, #6574 — the shop slice profile (`storefront_slice_profile`): the registry printer that
 * routes the slice and prices machine time, and one filament type per material of the price
 * book. A slot is sliced with its type's preset for the printer's nozzle (Filament → Types), in
 * the visitor's colour and with no spool, so the profile keeps no preset of its own. Support
 * materials are listed apart: support is the shop's choice, and a visitor never sees it.
 */
function SliceProfileEditor() {
  const settings = useAsync(() => api.getSettings(), [])
  const printers = useAsync(() => api.listPrinters(), [])
  const [printer, setPrinter] = useState<string | null>(null)
  const [chosen, setChosen] = useState<Record<string, string> | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const options = useAsync(() => api.storefrontSliceOptions(), [])

  useEffect(() => {
    if (settings.data === undefined || chosen !== null) return
    const profile = settings.data.storefront_slice_profile
    setPrinter(profile?.printer ?? null)
    setChosen({ ...(profile?.materials ?? {}) })
  }, [settings.data, chosen])

  const picks = chosen ?? {}
  const materials = options.data?.materials ?? []
  const offered = options.data?.types ?? []
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

  const typeRow = (material: string) => {
    const key = material.toLowerCase()
    const choices = typesFor(material, offered)
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
          <SelectTrigger aria-label={`Filament type for ${material}`} className="w-72">
            <SelectValue placeholder="No filament type" />
          </SelectTrigger>
          <SelectContent position="popper">
            {choices.map((type) => (
              <SelectItem key={type.id} value={type.id}>
                {type.label}
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
        What a visitor cannot choose: the printer to slice for, and the filament type each material is sliced as. Each colour slot is sliced in the visitor's colour with the type's preset for the nozzle (set on Filament → Types), and priced at the price-book filament the visitor picks. The nozzles, quality and plate are the ones the print dialog opens on for the design on this printer. A type with no preset for that nozzle gives no price. Publish refuses a listing with a material that has no type here.
      </p>
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault()
          if (printer === null) {
            setError('Choose the printer to slice for.')
            return
          }
          // Until the types are read, the form shows none: saving then would drop every
          // stored type (#6255 review).
          if (options.data === undefined) return
          // Only the materials the editor lists, and only types it offers: a type the form
          // does not show is never sent (#6255 review).
          const live = new Set(offered.map((type) => type.id))
          const named = materials.flatMap((m) => {
            const type = picks[m.material.toLowerCase()]
            return type && live.has(type) ? [[m.material.toLowerCase(), type] as const] : []
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
        {options.error ? (
          <p role="alert" className="text-[13px] text-warn">
            {refusalText(options.error)}
          </p>
        ) : (
          <>
            <fieldset className="flex flex-col gap-2">
              <legend className="text-xs font-medium">Customer materials</legend>
              {materials.filter((m) => !m.support).map((m) => typeRow(m.material))}
            </fieldset>
            {materials.some((m) => m.support) && (
              <fieldset className="flex flex-col gap-2">
                <legend className="text-xs font-medium">Support (internal)</legend>
                {materials.filter((m) => m.support).map((m) => typeRow(m.material))}
              </fieldset>
            )}
          </>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" size="sm" disabled={busy || settings.data === undefined || options.data === undefined}>
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
