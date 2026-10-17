/// <reference types="node" />
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { Button } from '../components/ui/button'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '../components/ui/alert-dialog'
import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuTrigger } from '../components/ui/context-menu'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '../components/ui/dropdown-menu'
import { HoverCard, HoverCardContent, HoverCardTrigger } from '../components/ui/hover-card'
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from '../components/ui/dialog'
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from '../components/ui/sheet'
import { renderPage as render } from '../test/utils'
import { bridge } from './bridge'
import { isUserOnly, USER_ONLY } from './dom'
import { useGlobalAgentTools } from './global'

// The user-only gate across portals (#3653). Every dialog, menu, popover and listbox
// renders under <body>, away from the element it was opened from, so a mark on a
// wrapper or on the trigger is not a DOM ancestor of what it shows. Each case renders
// one open, with the press inside it, and the agent must be refused it.

function Tools() {
  useGlobalAgentTools()
  return null
}

const press = vi.fn()

// Radix Select scrolls its selected option into view; jsdom has no layout.
Element.prototype.scrollIntoView ??= () => {}

/** The action inside every case: never marked itself, so only an inherited mark guards it. */
function Action() {
  return <Button onClick={press}>Confirm the send</Button>
}

/** A ContextMenu has no `open` prop: open it as a right-click would. */
function OpenedContextMenuTrigger() {
  return (
    <ContextMenuTrigger
      ref={(node) => {
        if (node) queueMicrotask(() => fireEvent.contextMenu(node))
      }}
    >
      Area
    </ContextMenuTrigger>
  )
}

const WRAPPED: Record<string, () => ReactNode> = {
  'a Dialog': () => (
    <Dialog open>
      <DialogContent>
        <DialogTitle>Send</DialogTitle>
        <Action />
      </DialogContent>
    </Dialog>
  ),
  'an AlertDialog': () => (
    <AlertDialog open>
      <AlertDialogContent>
        <AlertDialogTitle>Send</AlertDialogTitle>
        <AlertDialogAction onClick={press}>Confirm the send</AlertDialogAction>
      </AlertDialogContent>
    </AlertDialog>
  ),
  'a Sheet': () => (
    <Sheet open>
      <SheetContent>
        <SheetTitle>Send</SheetTitle>
        <Action />
      </SheetContent>
    </Sheet>
  ),
  'a DropdownMenu': () => (
    <DropdownMenu open modal={false}>
      <DropdownMenuTrigger>Actions</DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuItem onSelect={press}>Confirm the send</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  ),
  'a DropdownMenu submenu': () => (
    <DropdownMenu open modal={false}>
      <DropdownMenuTrigger>Actions</DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuSub open>
          <DropdownMenuSubTrigger>More</DropdownMenuSubTrigger>
          <DropdownMenuSubContent>
            <DropdownMenuItem onSelect={press}>Confirm the send</DropdownMenuItem>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
      </DropdownMenuContent>
    </DropdownMenu>
  ),
  'a ContextMenu': () => (
    <ContextMenu modal={false}>
      <OpenedContextMenuTrigger />
      <ContextMenuContent>
        <ContextMenuItem onSelect={press}>Confirm the send</ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  ),
  'a Popover': () => (
    <Popover open>
      <PopoverTrigger>Open</PopoverTrigger>
      <PopoverContent>
        <Action />
      </PopoverContent>
    </Popover>
  ),
  'a HoverCard': () => (
    <HoverCard open>
      <HoverCardTrigger>Hover</HoverCardTrigger>
      <HoverCardContent>
        <Action />
      </HoverCardContent>
    </HoverCard>
  ),
  'a Select': () => (
    <Select open value="a">
      <SelectTrigger aria-label="Printer">
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper">
        <SelectItem value="a">Keep</SelectItem>
        <SelectItem value="b">Confirm the send</SelectItem>
      </SelectContent>
    </Select>
  ),
}

/** The mark on the trigger alone, the content its sibling. */
const TRIGGERED: Record<string, () => ReactNode> = {
  'a Dialog': () => (
    <Dialog open>
      <DialogTrigger {...USER_ONLY}>Send</DialogTrigger>
      <DialogContent>
        <DialogTitle>Send</DialogTitle>
        <Action />
      </DialogContent>
    </Dialog>
  ),
  'an AlertDialog': () => (
    <AlertDialog open>
      <AlertDialogTrigger {...USER_ONLY}>Send</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogTitle>Send</AlertDialogTitle>
        <AlertDialogAction onClick={press}>Confirm the send</AlertDialogAction>
      </AlertDialogContent>
    </AlertDialog>
  ),
  'a Sheet': () => (
    <Sheet open>
      <SheetTrigger {...USER_ONLY}>Send</SheetTrigger>
      <SheetContent>
        <SheetTitle>Send</SheetTitle>
        <Action />
      </SheetContent>
    </Sheet>
  ),
  'a DropdownMenu': () => (
    <DropdownMenu open modal={false}>
      <DropdownMenuTrigger {...USER_ONLY}>Actions</DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuItem onSelect={press}>Confirm the send</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  ),
  'a Popover': () => (
    <Popover open>
      <PopoverTrigger {...USER_ONLY}>Open</PopoverTrigger>
      <PopoverContent>
        <Action />
      </PopoverContent>
    </Popover>
  ),
  'a Select': () => (
    <Select open value="a">
      <SelectTrigger aria-label="Printer" {...USER_ONLY}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper">
        <SelectItem value="a">Keep</SelectItem>
        <SelectItem value="b">Confirm the send</SelectItem>
      </SelectContent>
    </Select>
  ),
}

async function confirmation(): Promise<HTMLElement> {
  return screen.findByText('Confirm the send', { selector: 'button, [role="menuitem"], [role="option"], [role="option"] *' })
}

async function expectRefused() {
  const target = await confirmation()
  const control = target.closest('button, [role="menuitem"], [role="option"]') as HTMLElement
  // Not a DOM descendant of the mark: this is what the portal does.
  expect(control.closest('[data-agent-user-only]')).toBeNull()
  expect(isUserOnly(control)).toBe(true)

  await waitFor(() => expect(bridge.liveNames()).toContain('click'))
  const role = control.getAttribute('role') ?? 'button'
  const outcome = await bridge.call('click', { role, name: 'Confirm the send' })
  expect(!outcome.ok && outcome.error.code).toBe('refused')
  expect(press).not.toHaveBeenCalled()
  expect(bridge.snapshot().elements).toContainEqual(expect.objectContaining({ name: 'Confirm the send', userOnly: true }))
}

describe('the user-only mark across a portal', () => {
  it.each(Object.entries(WRAPPED))('holds for %s inside a marked wrapper', async (_, Case) => {
    press.mockClear()
    render(
      <>
        <Tools />
        <section {...USER_ONLY}>
          <Case />
        </section>
      </>,
    )
    await expectRefused()
  })

  it.each(Object.entries(TRIGGERED))('holds for %s whose trigger is marked', async (_, Case) => {
    press.mockClear()
    render(
      <>
        <Tools />
        <Case />
      </>,
    )
    await expectRefused()
  })

  it('leaves an unmarked one to the agent', async () => {
    press.mockClear()
    render(
      <>
        <Tools />
        <DropdownMenu open modal={false}>
          <DropdownMenuTrigger>Actions</DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuItem onSelect={press}>Confirm the send</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </>,
    )
    const item = (await confirmation()).closest('[role="menuitem"]') as HTMLElement
    expect(isUserOnly(item)).toBe(false)
    await waitFor(() => expect(bridge.liveNames()).toContain('click'))
    expect(await bridge.call('click', { role: 'menuitem', name: 'Confirm the send' })).toMatchObject({ ok: true })
    expect(press).toHaveBeenCalled()
  })

  it('covers every src/components/ui primitive that portals', () => {
    // A new portaled primitive needs a case above: the gate is only proven where tested.
    const ui = join(import.meta.dirname, '..', 'components', 'ui')
    const tested = readFileSync(join(import.meta.dirname, 'userOnly.test.tsx'), 'utf8')
    const portaling = readdirSync(ui, { recursive: true, encoding: 'utf8' })
      .filter((name) => name.endsWith('.tsx') && !name.endsWith('.test.tsx'))
      .filter((name) => /\bPortal\b|createPortal/.test(readFileSync(join(ui, name), 'utf8')))
      .map((name) => name.replace(/(\/index)?\.tsx$/, ''))
      // A tooltip holds a label, never a control: nothing in it is the agent's to press.
      .filter((name) => name !== 'tooltip')
    const missing = portaling.filter((name) => !tested.includes(`'../components/ui/${name}'`))
    expect(missing).toEqual([])
  })
})
