import { useEffect } from 'react'
import { useLatest } from '../../lib/useLatest'

/**
 * #6937 — two panels of Settings → Tools & plugins show the plugin packages: Packages, and
 * Hosted plugins (whose Offer installs or re-pins one). A panel that changed them says so, and
 * the other reads them again. A panel ignores its own announcement.
 */
const EVENT = 'scadbuddy:plugin-packages-changed'

export type PackagesPanel = 'packages' | 'hosted'

export function announcePackagesChanged(from: PackagesPanel): void {
  window.dispatchEvent(new CustomEvent<PackagesPanel>(EVENT, { detail: from }))
}

export function usePackagesChanged(self: PackagesPanel, onChange: () => void): void {
  const latest = useLatest({ onChange })
  useEffect(() => {
    const listener = (event: Event) => {
      if ((event as CustomEvent<PackagesPanel>).detail !== self) latest.current.onChange()
    }
    window.addEventListener(EVENT, listener)
    return () => window.removeEventListener(EVENT, listener)
  }, [self, latest])
}
