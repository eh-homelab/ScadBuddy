import { createContext, useContext } from "react"

/**
 * Where the overlays below it portal to (#3672): `document.body` unless a provider says
 * otherwise. The customizer's full-screen workspace provides itself, since in the
 * Fullscreen API's top layer only the full-screen element and what is inside it show; a
 * select or a dialog opened from the parameters flyout would otherwise open unseen.
 */
const PortalContainerContext = createContext<HTMLElement | null>(null)

export const PortalContainer = PortalContainerContext.Provider

/** The element to portal into; undefined for Radix's default, `document.body`. */
export function usePortalContainer(): HTMLElement | undefined {
  return useContext(PortalContainerContext) ?? undefined
}
