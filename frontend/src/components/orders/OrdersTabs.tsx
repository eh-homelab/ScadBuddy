import { NavLink } from 'react-router'
import { tabsListVariants } from '@/components/ui/tabs'
import { inventoryPaths, PARTS_KIND } from '../../lib/inventoryLinks'
import { cn } from '@/lib/utils'

/** shadcn's tabs, as links, as the Workflows page's are. */
const tabClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    'inline-flex h-full items-center justify-center rounded-md border border-transparent px-3 text-sm font-medium whitespace-nowrap transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
    isActive
      ? 'bg-background text-foreground shadow-sm dark:border-input dark:bg-input/30'
      : 'text-foreground/60 hover:text-foreground dark:text-muted-foreground dark:hover:text-foreground',
  )

/** #5329 — the orders list and the profit report (`/orders/profit`), one tab each; Parts opens
 * the Inventory on the parts' kind (`PARTS_KIND`, #7067), where the parts and the model rules moved. */
export function OrdersTabs() {
  return (
    <nav aria-label="Orders views" className={cn(tabsListVariants(), 'h-9')}>
      <NavLink to="/orders" end className={tabClass}>
        Orders
      </NavLink>
      <NavLink to="/orders/profit" className={tabClass}>
        Profit
      </NavLink>
      <NavLink to={inventoryPaths.kind(PARTS_KIND)} className={tabClass}>
        Parts
      </NavLink>
    </nav>
  )
}
