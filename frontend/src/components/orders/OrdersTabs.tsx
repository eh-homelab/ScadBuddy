import { NavLink } from 'react-router'
import { tabsListVariants } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'

/** shadcn's tabs, as links, as the Workflows page's are. */
const tabClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    'inline-flex h-full items-center justify-center rounded-md border border-transparent px-3 text-sm font-medium whitespace-nowrap transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
    isActive
      ? 'bg-background text-foreground shadow-sm dark:border-input dark:bg-input/30'
      : 'text-foreground/60 hover:text-foreground dark:text-muted-foreground dark:hover:text-foreground',
  )

/** #5329 — the orders list, the profit report (`/orders/profit`) and the parts inventory
 * (`/orders/parts`, #5543), one tab each. */
export function OrdersTabs() {
  return (
    <nav aria-label="Orders views" className={cn(tabsListVariants(), 'h-9')}>
      <NavLink to="/orders" end className={tabClass}>
        Orders
      </NavLink>
      <NavLink to="/orders/profit" className={tabClass}>
        Profit
      </NavLink>
      <NavLink to="/orders/parts" className={tabClass}>
        Parts
      </NavLink>
    </nav>
  )
}
