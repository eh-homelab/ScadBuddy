import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { refusalText } from '../orders/refusal'
import { MaterialOrder } from '../storefront/MaterialOrder'
import { priceBookMaterials } from '../storefront/listingDraft'

/**
 * #6258 — the shop's default materials (`storefront_materials`, a live setting): the
 * materials a print of a listing that names none of its own may be made of, first
 * preferred. Ordered as a listing's "Materials allowed" is (`MaterialOrder`); at least one.
 * Saved on its own.
 */
export function StorefrontMaterials() {
  const settings = useAsync(() => api.getSettings(), [])
  const book = useAsync(() => api.priceBook(), [])
  const [materials, setMaterials] = useState<string[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    if (settings.data && materials === null) setMaterials([...(settings.data.storefront_materials ?? [])])
  }, [settings.data, materials])

  return (
    <section aria-label="Storefront materials" className="space-y-2 rounded-md border p-3">
      <h4 className="text-sm font-medium">Storefront materials</h4>
      <p className="text-xs text-muted-foreground">
        The materials a listing may be printed in when it does not choose its own, first preferred. A print never mixes materials: it is made of the first one on this list that has every colour
        the customer picks. Customers never see the material.
      </p>
      {materials === null ? (
        <p className="text-[13px] text-muted-foreground">{settings.error ? refusalText(settings.error) : 'Reading the settings…'}</p>
      ) : (
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            setBusy(true)
            setError(null)
            setSaved(false)
            api
              .putSettings({ storefront_materials: materials })
              .then((next) => {
                setMaterials([...(next.storefront_materials ?? materials)])
                setSaved(true)
              })
              .catch((err: unknown) => setError(refusalText(err)))
              .finally(() => setBusy(false))
          }}
        >
          <MaterialOrder
            label="Storefront materials, first preferred"
            materials={materials}
            choices={priceBookMaterials(book.data?.filaments ?? [])}
            onChange={(next) => {
              setSaved(false)
              setMaterials(next)
            }}
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="sm" disabled={busy}>
              Save materials
            </Button>
            {saved && <span className="text-xs text-muted-foreground">Saved.</span>}
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
