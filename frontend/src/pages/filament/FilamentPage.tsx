import { NavLink } from 'react-router'
import { tabsListVariants } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
import { CatalogTab } from './CatalogTab'
import { LedgerTab } from './LedgerTab'
import { LocationsTab } from './LocationsTab'
import { SpoolsTab } from './SpoolsTab'
import { TagsTab } from './TagsTab'
import { TypesTab } from './TypesTab'
import { FILAMENT_TABS, type TabId } from './tabs'

/** shadcn's tabs, as links: each tab is a page of its own, as on the Workflows page. */
const tabClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    'inline-flex h-full items-center justify-center rounded-md border border-transparent px-3 text-sm font-medium whitespace-nowrap transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
    isActive
      ? 'bg-background text-foreground shadow-sm dark:border-input dark:bg-input/30'
      : 'text-foreground/60 hover:text-foreground dark:text-muted-foreground dark:hover:text-foreground',
  )

/** What each tab holds, under the heading. */
const INTRO: Record<TabId, string> = {
  spools: 'Every spool on the shelf and in the printers: its colour, what is left, and where it is.',
  types: 'The products your spools are: material, colour, weights, temperatures and price.',
  tags: 'The RFID tags read from spools, the spool each is on, and where each has been.',
  suppliers: 'Where you buy filament.',
  invoices: 'Invoices, and the spools each one added.',
  locations: 'The places spools are kept: rooms, shelves, dry boxes and AMS trays.',
  catalog: 'The lists a type or a spool picks from: materials, brands, colours and the rest.',
  metrics: 'What you print with, and how fast it goes.',
  'shopping-list': 'What to buy next.',
  ledger: 'Every change to every spool: weighings, prints, moves and edits.',
}

/**
 * #6262 — the Filament section (`/filament/<tab>`, spec §10): ScadBuddy's own spools and
 * what describes them, one tab per page. Only the available tabs (`tabs.ts`) are shown.
 */
export function FilamentPage({ tab }: { tab: TabId }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <div className="mb-4">
          <h1 className="text-lg font-semibold tracking-tight">Filament</h1>
          <p className="mt-0.5 text-[13px] text-muted-foreground">{INTRO[tab]}</p>
        </div>
        <nav aria-label="Filament" className={cn(tabsListVariants(), 'mb-4 h-9 max-w-full justify-start overflow-x-auto')}>
          {FILAMENT_TABS.filter((one) => one.available).map((one) => (
            <NavLink key={one.id} to={one.path} className={tabClass}>
              {one.title}
            </NavLink>
          ))}
        </nav>
        {tab === 'spools' && <SpoolsTab />}
        {tab === 'types' && <TypesTab />}
        {tab === 'tags' && <TagsTab />}
        {tab === 'locations' && <LocationsTab />}
        {tab === 'catalog' && <CatalogTab />}
        {tab === 'ledger' && <LedgerTab />}
      </div>
    </div>
  )
}
