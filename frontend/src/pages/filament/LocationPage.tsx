import { ChevronLeftIcon, PrinterIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import { ApiError } from '../../api/client'
import { getLocation, type LocationDetail } from '../../api/filament'
import { AttachmentsPanel } from '../../components/entities/AttachmentsPanel'
import { LinksPanel } from '../../components/entities/LinksPanel'
import { RgbaSwatch } from '../../components/filament/RgbaSwatch'
import { SpoolLink } from '../../components/filament/SpoolLink'
import { filamentLinks } from '../../lib/filamentLinks'

/** #6955 — a place's page: what `/x/L-12` opens. Its path, what is inside it, and the link to its label. */
export function LocationPage() {
  const { id = '' } = useParams()
  const [place, setPlace] = useState<LocationDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setPlace(null)
    setError(null)
    getLocation(id).then(
      (found) => live && setPlace(found),
      (cause: unknown) => {
        if (live) setError(cause instanceof ApiError && cause.status === 404 ? 'No such location' : 'The location could not be loaded. Try again.')
      },
    )
    return () => {
      live = false
    }
  }, [id])

  return (
    <div className="mx-auto flex w-full max-w-2xl min-w-0 flex-col gap-4 p-4 text-[13px]">
      <Link to={filamentLinks.locations} className="inline-flex items-center gap-1 text-muted-foreground hover:underline">
        <ChevronLeftIcon className="size-3.5" /> Locations
      </Link>
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      {!place && !error && <p className="text-muted-foreground">Loading…</p>}
      {place && (
        <>
          {place.path.length > 0 && (
            <nav aria-label="Path" className="flex flex-wrap items-center gap-x-1 text-muted-foreground">
              {place.path.map((step) => (
                <span key={step.id} className="flex items-center gap-1">
                  <Link to={filamentLinks.location(step.id)} className="break-words hover:underline">
                    {step.name}
                  </Link>
                  <span aria-hidden="true">/</span>
                </span>
              ))}
            </nav>
          )}
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h1 className="min-w-0 break-words text-lg font-semibold tracking-tight">{place.name}</h1>
            <span className="tabular-nums text-muted-foreground">{place.code}</span>
            <span className="text-muted-foreground">{place.kind_name}</span>
            {place.archived_at && <span className="rounded-full bg-muted px-2 text-[12px] text-muted-foreground">Archived</span>}
          </div>
          <div className="flex flex-wrap gap-3">
            <Link to={filamentLinks.locationLabel(place.id)} className="inline-flex items-center gap-1 underline">
              <PrinterIcon className="size-3.5" /> Print label
            </Link>
            {place.printer_root && place.printer_id && (
              <Link to={filamentLinks.printerLabels(place.printer_id)} className="inline-flex items-center gap-1 underline">
                <PrinterIcon className="size-3.5" /> Print this printer's labels
              </Link>
            )}
            <Link to={filamentLinks.locationSpools(place.id)} className="underline">
              View spools
            </Link>
            {place.printer_root && (
              <Link to={filamentLinks.printers} className="underline">
                Printer settings
              </Link>
            )}
          </div>
          {place.children.length > 0 && (
            <section aria-label={place.printer_root ? 'Units' : 'Inside'} className="flex flex-col gap-1">
              <h2 className="font-medium">{place.printer_root ? 'Units' : 'Inside'}</h2>
              <ul className="flex flex-col gap-1">
                {place.children.map((child) => (
                  <li key={child.id} className="min-w-0">
                    <Link to={filamentLinks.location(child.id)} className="break-words hover:underline">
                      {child.name}
                    </Link>{' '}
                    <span className="text-muted-foreground">{child.code}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <section aria-label="Spools" className="flex flex-col gap-1">
            <h2 className="font-medium">Spools</h2>
            {place.spools.length === 0 ? (
              <p className="text-muted-foreground">Nothing is here.</p>
            ) : (
              <ul className="flex flex-col gap-1">
                {place.spools.map((spool) => (
                  <li key={spool.id} className="min-w-0">
                    <SpoolLink spool={spool} className="inline-flex items-center gap-2 hover:underline">
                      {spool.colour_rgba && <RgbaSwatch rgba={spool.colour_rgba} />}
                      <span className="break-words">
                        {spool.code}
                        {spool.material ? ` ${spool.material}` : ''}
                      </span>
                      {spool.grams_left != null && <span className="tabular-nums text-muted-foreground">{Number(spool.grams_left.toFixed(1))} g</span>}
                    </SpoolLink>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section aria-label="Files and links" className="flex flex-col gap-2">
            <h2 className="font-medium">Files and links</h2>
            <AttachmentsPanel entity={`location:${place.id}`} />
            <LinksPanel entity={`location:${place.id}`} />
          </section>
        </>
      )}
    </div>
  )
}
