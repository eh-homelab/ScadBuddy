import { screen } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { Route, Routes } from 'react-router'
import { renderPage } from '../../test/utils'
import { SettingsLoaded } from './cache'
import type { SettingsSection } from './registry'
import { SettingsProviders, SettingsShell } from './SettingsShell'

/** #2171 — `ui` (one section or several) with everything Settings gives it, once loaded. */
export function renderSettingsSection(
  ui: ReactElement,
  { route = '/settings/general', sections }: { route?: string; sections?: readonly SettingsSection[] } = {},
) {
  return renderPage(
    <SettingsProviders sections={sections}>
      <SettingsLoaded>{ui}</SettingsLoaded>
    </SettingsProviders>,
    { route },
  )
}

/** #2171 — the Settings shell over `sections`, beside a page outside Settings to leave for. */
export function renderSettingsShell(sections: readonly SettingsSection[], { route = '/settings' }: { route?: string } = {}) {
  return renderPage(
    <Routes>
      <Route
        path="/settings/*"
        element={
          <SettingsProviders sections={sections}>
            <SettingsShell sections={sections} />
          </SettingsProviders>
        }
      />
      <Route path="*" element={<p>Outside Settings</p>} />
    </Routes>,
    { route },
  )
}

/**
 * A section, by its heading. A role query over all of Settings (~1000 elements) runs
 * jsdom's getComputedStyle on each of them, ~0.5 s after every change, and these tests
 * ran into their 5 s under load (#1485); so buttons are looked for in their section.
 */
export function region(name: string): HTMLElement {
  const section = screen.getByText(name, { selector: 'h3 button' }).closest('section')
  if (!section) throw new Error(`no section headed ${name}`)
  return section
}

/** `text` in `field` as one paste: each keystroke re-renders all of Settings (#1485). */
export async function enter(user: UserEvent, field: HTMLElement, text: string) {
  await user.click(field)
  await user.paste(text)
}
