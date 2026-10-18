import type { ComponentType, ReactNode, SyntheticEvent } from 'react'
import { MoreHorizontalIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

/** One action of a file's or a folder's menu; `separator` draws a line before it. */
export interface MenuEntry {
  label: string
  icon?: ComponentType<{ className?: string }>
  onSelect: () => void
  destructive?: boolean
  disabled?: boolean
  separator?: boolean
  testId?: string
}

/**
 * A menu's events stay in it: React passes a portal's events up to the components that
 * rendered it, so a click or a key in a row's menu would otherwise also reach the row
 * (select the folder, move the tree's focus).
 */
const contain = (event: SyntheticEvent) => event.stopPropagation()

/**
 * #3650 — the same actions on right-click (or a long press on touch) as on the ⋯ button,
 * so the menu is reachable by mouse, touch and keyboard alike.
 */
export function RowContextMenu({
  label,
  entries,
  children,
}: {
  label: string
  entries: MenuEntry[]
  children: ReactNode
}) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent aria-label={label} className="w-52" onClick={contain} onKeyDown={contain}>
        <ContextMenuLabel className="truncate">{label}</ContextMenuLabel>
        {entries.map((entry) => (
          <MenuItem key={entry.label} entry={entry} kind="context" />
        ))}
      </ContextMenuContent>
    </ContextMenu>
  )
}

/** The ⋯ button and its menu. */
export function MoreMenu({
  label,
  heading,
  entries,
  disabled,
  testId,
  align = 'end',
}: {
  /** The button's accessible name, and the menu's. */
  label: string
  heading?: string
  entries: MenuEntry[]
  disabled?: boolean
  testId?: string
  align?: 'start' | 'end'
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <Button variant="ghost" size="icon-sm" aria-label={label} data-testid={testId}>
          <MoreHorizontalIcon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align={align} aria-label={label} className="w-52" onClick={contain} onKeyDown={contain}>
        {heading && <DropdownMenuLabel className="truncate">{heading}</DropdownMenuLabel>}
        {entries.map((entry) => (
          <MenuItem key={entry.label} entry={entry} kind="dropdown" />
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function MenuItem({ entry, kind }: { entry: MenuEntry; kind: 'context' | 'dropdown' }) {
  const Separator = kind === 'context' ? ContextMenuSeparator : DropdownMenuSeparator
  const Item = kind === 'context' ? ContextMenuItem : DropdownMenuItem
  const Icon = entry.icon
  return (
    <>
      {entry.separator && <Separator />}
      <Item
        variant={entry.destructive ? 'destructive' : 'default'}
        disabled={entry.disabled}
        data-testid={entry.testId}
        onSelect={entry.onSelect}
      >
        {Icon && <Icon />}
        {entry.label}
      </Item>
    </>
  )
}
