import { createContext, useContext, type ReactNode } from 'react'
import { api } from '../../api/client'
import type { BambuddyStatus, BambuddyTargets, ProjectChoices } from '../../api/types'
import { useAsync, type AsyncState } from '../../lib/useAsync'
import { useSettingsCache } from './cache'

export type BambuddyResources = {
  connected: boolean
  targets: AsyncState<BambuddyTargets | null>
  projects: AsyncState<ProjectChoices | null>
  status: AsyncState<BambuddyStatus | null>
}

const ResourcesContext = createContext<BambuddyResources | null>(null)

/**
 * #2171 — what Settings reads from Bambuddy, read once for every section that shows it.
 * The pickers need a live Bambuddy, so nothing is fetched until a URL is saved.
 * Inside {@link SettingsCacheProvider}.
 */
export function BambuddyResourcesProvider({ children }: { children: ReactNode }) {
  const { settings } = useSettingsCache()
  const connected = Boolean(settings?.bambuddy_url)
  const targets = useAsync(() => (connected ? api.getBambuddyTargets() : Promise.resolve(null)), [connected])
  const projects = useAsync(() => (connected ? api.getProjects() : Promise.resolve(null)), [connected])
  const status = useAsync(
    () => (connected ? api.getBambuddyStatus() : Promise.resolve(null)),
    [connected, settings?.bambuddy_url],
  )
  return <ResourcesContext.Provider value={{ connected, targets, projects, status }}>{children}</ResourcesContext.Provider>
}

export function useBambuddyResources(): BambuddyResources {
  const resources = useContext(ResourcesContext)
  if (!resources) throw new Error('useBambuddyResources outside BambuddyResourcesProvider')
  return resources
}
