import { screen, waitFor, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'

/**
 * Picks `option` in a shadcn (Radix) select, as a user does: open the combobox, click the
 * option. What `user.selectOptions` did for a native `<select>` (#3671).
 */
export async function choose(user: UserEvent, combobox: HTMLElement, option: string | RegExp) {
  await user.click(combobox)
  await user.click(await screen.findByRole('option', { name: option }))
}

/** The options a shadcn select offers, by name, opened and closed again with Escape. */
export async function optionsOf(user: UserEvent, combobox: HTMLElement): Promise<string[]> {
  await user.click(combobox)
  const names = (await screen.findAllByRole('option')).map((option) => option.textContent ?? '')
  await user.keyboard('{Escape}')
  return names
}

/** Whether the option named `option` is offered disabled; the select is closed again. */
export async function optionDisabled(user: UserEvent, combobox: HTMLElement, option: string | RegExp): Promise<boolean> {
  await user.click(combobox)
  const disabled = (await screen.findByRole('option', { name: option })).getAttribute('aria-disabled') === 'true'
  await user.keyboard('{Escape}')
  return disabled
}

/**
 * #3672 — opens `trigger`'s list, picks the option named `name`, and waits for the list
 * to close.
 */
export async function selectOption(user: UserEvent, trigger: HTMLElement, name: string | RegExp): Promise<void> {
  await user.click(trigger)
  const listbox = await screen.findByRole('listbox')
  await user.click(within(listbox).getByRole('option', { name }))
  await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
}

/** #3672 — the names of `trigger`'s options, read by opening its list and closing it again. */
export async function optionNames(user: UserEvent, trigger: HTMLElement): Promise<string[]> {
  await user.click(trigger)
  const listbox = await screen.findByRole('listbox')
  const names = within(listbox)
    .getAllByRole('option')
    .map((option) => option.textContent ?? '')
  await user.keyboard('{Escape}')
  await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  return names
}

/** An access select's name (#2171): `Access of <name>`, with its visible label after it when the two differ. */
export function accessName(name: string): RegExp {
  return new RegExp(`^Access of ${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?: \\(.*\\))?$`)
}
