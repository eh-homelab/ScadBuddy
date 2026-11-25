import { useEffect } from 'react'
import { useSubscription, type Confirmation, type RealtimeSignal } from '../../lib/realtime'
import { useLatest } from '../../lib/useLatest'

/**
 * #6937 — two panels of Settings → Tools & plugins show the plugin packages: Packages, and
 * Hosted plugins (whose Offer installs or re-pins one). A panel that changed them says so, and
 * the other reads them again at once. A panel ignores its own announcement.
 *
 * #7460 — a change made outside the page (the assistant's `hosted_plugin_publish`, another tab,
 * an approval elsewhere) reaches it as the server's `plugin_packages.changed` on
 * {@link PACKAGES_TOPIC} (`backend/scadbuddy/api/realtime.py`), which the API publishes at each
 * package write and each hosted-plugin create, change, publish and delete. Both only make the
 * panels read again, so the page's own announcement and the server's later event agree.
 */
const EVENT = 'scadbuddy:plugin-packages-changed'

/** The realtime topic of every plugin package and hosted plugin change (#7460). */
export const PACKAGES_TOPIC = 'plugin-packages'

export type PackagesPanel = 'packages' | 'hosted'

export function announcePackagesChanged(from: PackagesPanel): void {
  window.dispatchEvent(new CustomEvent<PackagesPanel>(EVENT, { detail: from }))
}

/**
 * Calls `onChange` for each server change on {@link PACKAGES_TOPIC} and each resync after lost
 * events. The subscription's confirmation calls it only when `stale` says the panel's last read
 * may have missed a change (#7672: a change between the mount's read and the confirmation;
 * `useAsync` `staleFor`, or `readAgain` over a `recordRead`).
 */
export function usePackagesTopic(
  onChange: (signal: RealtimeSignal) => void,
  stale: (confirmation: Confirmation) => Promise<boolean>,
): void {
  useSubscription(PACKAGES_TOPIC, (signal, confirmation) => {
    if (!confirmation) return onChange(signal)
    void stale(confirmation).then((again) => {
      if (again) onChange(signal)
    })
  })
}

export function usePackagesChanged(
  self: PackagesPanel,
  onChange: () => void,
  stale: (confirmation: Confirmation) => Promise<boolean>,
): void {
  const latest = useLatest({ onChange })
  useEffect(() => {
    const listener = (event: Event) => {
      if ((event as CustomEvent<PackagesPanel>).detail !== self) latest.current.onChange()
    }
    window.addEventListener(EVENT, listener)
    return () => window.removeEventListener(EVENT, listener)
  }, [self, latest])
  usePackagesTopic(() => latest.current.onChange(), stale)
}
