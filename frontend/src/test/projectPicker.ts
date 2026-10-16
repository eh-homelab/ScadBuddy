import { screen, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'

/**
 * #3651 — the project picker is a combobox over a searchable list, not a `<select>`:
 * its value is the trigger's `data-value` (an id, '' for no project, 'new' while the
 * create form is open).
 */
export function pickerValue(trigger: HTMLElement): string | null {
  return trigger.getAttribute('data-value')
}

/** Opens the picker and returns its list (each project is an `option`). */
export async function openPicker(user: UserEvent, trigger: HTMLElement): Promise<HTMLElement> {
  if (trigger.getAttribute('aria-expanded') !== 'true') await user.click(trigger)
  return screen.findByTestId(`${trigger.dataset.testid}-list`)
}

/** Chooses a project by id, `null` for No project, or 'new' to open the create form. */
export async function chooseProject(
  user: UserEvent,
  trigger: HTMLElement,
  choice: number | null | 'new',
): Promise<void> {
  const list = await openPicker(user, trigger)
  const testId =
    choice === null ? 'project-option-none' : choice === 'new' ? 'new-project' : `project-option-${choice}`
  await user.click(within(list).getByTestId(testId))
}

/** Chooses the new project's parent by its path, or 'None'. */
export async function chooseParent(user: UserEvent, label: string): Promise<void> {
  await user.click(screen.getByTestId('new-project-parent'))
  await user.click(await screen.findByRole('option', { name: label }))
}
