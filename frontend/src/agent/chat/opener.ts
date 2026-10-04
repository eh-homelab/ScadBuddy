import { createContext, useContext } from 'react'

/**
 * #931 — opens the assistant panel on one session, from anywhere on the page (a model's
 * "Changed by assistant" list). AppShell provides it while the assistant is shown;
 * without one (AI off, or a page rendered on its own) there is nothing to open, and
 * whoever reads it shows nothing.
 */
export interface AssistantOpener {
  openSession: (sessionId: string) => void
}

export const AssistantOpenerContext = createContext<AssistantOpener | null>(null)

export function useAssistantOpener(): AssistantOpener | null {
  return useContext(AssistantOpenerContext)
}
