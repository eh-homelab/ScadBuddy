import { Link, useSearchParams } from 'react-router'
import { CatalogList } from '../../components/filament/CatalogList'
import { CATALOGS } from '../../components/filament/catalogs'
import { cn } from '@/lib/utils'

/**
 * #6262 — the Catalog tab: the lists a type or a spool picks from, one at a time
 * (`?kind=`, materials by default), each as a `CatalogList`.
 */
export function CatalogTab() {
  const [params] = useSearchParams()
  const current = CATALOGS.find((one) => one.kind === params.get('kind')) ?? CATALOGS[0]!
  const to = (kind: string) => {
    const next = new URLSearchParams(params)
    if (kind === CATALOGS[0]!.kind) next.delete('kind')
    else next.set('kind', kind)
    const search = next.toString()
    return search ? `?${search}` : '.'
  }
  return (
    <div className="flex flex-col gap-4">
      <nav aria-label="Catalogs" className="flex flex-wrap gap-1">
        {CATALOGS.map((one) => (
          <Link
            key={one.kind}
            to={to(one.kind)}
            replace
            aria-current={one.kind === current.kind ? 'page' : undefined}
            className={cn(
              'rounded-md border px-2.5 py-1 text-[13px] transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
              one.kind === current.kind ? 'border-transparent bg-secondary font-medium text-secondary-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {one.title}
          </Link>
        ))}
      </nav>
      <CatalogList key={current.kind} kind={current.kind} />
    </div>
  )
}
