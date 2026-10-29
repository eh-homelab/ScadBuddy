import { SettingsProviders, SettingsShell } from './settings/SettingsShell'

/** #2171 — Settings by area, at `/settings/<area>`: each area's sections in one shell. */
export function SettingsPage() {
  return (
    <SettingsProviders>
      <SettingsShell />
    </SettingsProviders>
  )
}
