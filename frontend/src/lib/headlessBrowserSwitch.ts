import { useEffect } from 'react'

// The headless browser has two switches in Settings that write one key
// (`headless_browser_enabled`): the "AI headless browser" section and the built-in
// `playwright` plugin's card (agent plugins/packages/builtins.ts). Each says when it
// changed it, so the other shows the same without a reload.

const EVENT = 'scadbuddy:headless-browser'

export function announceHeadlessBrowser(enabled: boolean): void {
  window.dispatchEvent(new CustomEvent<boolean>(EVENT, { detail: enabled }))
}

export function useHeadlessBrowserChanges(onChange: (enabled: boolean) => void): void {
  useEffect(() => {
    const listener = (event: Event) => onChange((event as CustomEvent<boolean>).detail)
    window.addEventListener(EVENT, listener)
    return () => window.removeEventListener(EVENT, listener)
  }, [onChange])
}
