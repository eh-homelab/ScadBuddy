import { screen, waitFor, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'

/**
 * #3672 — a Radix select, as a test drives it: the trigger is a button (role combobox)
 * and its options are a listbox in a portal while it is open. What `selectOptions` and
 * `toHaveValue` did for a native select.
 */

/** Opens `trigger`'s list and picks the option named `name`. */
export async function selectOption(user: UserEvent, trigger: HTMLElement, name: string | RegExp): Promise<void> {
  await user.click(trigger)
  const listbox = await screen.findByRole('listbox')
  await user.click(within(listbox).getByRole('option', { name }))
  await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
}

/** The names of `trigger`'s options, read by opening its list and closing it again. */
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
