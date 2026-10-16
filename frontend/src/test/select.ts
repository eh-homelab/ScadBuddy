import { screen } from '@testing-library/react'
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
