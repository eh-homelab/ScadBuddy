import { createContext, useContext, useLayoutEffect, useRef } from 'react'
import { bridge as appBridge, type AgentBridge } from './bridge'
import type { ToolImpls, ToolName } from './types'

/** Tests can hand a component a bridge of their own; the app uses the singleton. */
export const AgentBridgeContext = createContext<AgentBridge>(appBridge)

export function useAgentBridge(): AgentBridge {
  return useContext(AgentBridgeContext)
}

/**
 * Registers `impls` with the bridge for as long as the calling component is mounted.
 *
 * The handlers are read through a ref, so they always see the render's latest state and
 * callbacks without re-registering on every render; the registration itself only changes
 * when the set of tool names does. `describe` feeds `snapshot().page[label]`.
 */
export function useAgentHandlers(
  label: string,
  impls: ToolImpls,
  describe?: () => unknown,
) {
  const bridge = useAgentBridge()
  const latest = useRef({ impls, describe })
  useLayoutEffect(() => {
    latest.current = { impls, describe }
  })

  const names = (Object.keys(impls) as ToolName[]).sort().join(',')
  const hasDescribe = describe !== undefined

  // A layout effect, so the tools are live in the same commit as the DOM they act on:
  // anything that can see the page can call them.
  useLayoutEffect(() => {
    const forwarding: Record<string, (args: unknown) => unknown> = {}
    for (const name of names.split(',').filter(Boolean) as ToolName[]) {
      forwarding[name] = (args) => {
        const impl = latest.current.impls[name] as ((args: unknown) => unknown) | undefined
        if (!impl) throw new Error(`"${name}" is no longer provided here.`)
        return impl(args)
      }
    }
    return bridge.register(forwarding as ToolImpls, {
      label,
      describe: hasDescribe ? () => latest.current.describe?.() : undefined,
    })
  }, [bridge, label, names, hasDescribe])
}

export { useLatest } from '../lib/useLatest'
