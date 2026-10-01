import { useEffect, useRef } from 'react'
import { api } from '../api/client'
import type { JsonObject } from '../lib/inputs'
import { useLatest } from '../lib/useLatest'
import { createHost, type HostDeps, type HostHandle } from './host'
import { loadUiModule } from './loadModule'
import { adoptAppStyles } from './styles'
import { UI_API_SUPPORTED, type Mount, type TemplateUiFailure, type UiDeclaration } from './types'

interface Props {
  slug: string
  ui: UiDeclaration
  /** The revision to load the module from; undefined for the live template. */
  version: string | undefined
  deps: HostDeps
  inputs: JsonObject
  onFailure: (failure: TemplateUiFailure) => void
}

function mountOf(module: unknown): Mount | undefined {
  const mount = (module as { mount?: unknown } | null)?.mount
  return typeof mount === 'function' ? (mount as Mount) : undefined
}

function theme(): 'light' | 'dark' {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** A template's own interface (spec 2026-09-27 §4.2): not sandboxed, style-isolated. */
export function TemplateUi({ slug, ui, version, deps, inputs, onFailure }: Props) {
  const element = useRef<HTMLDivElement>(null)
  const handle = useRef<HostHandle | null>(null)
  const latest = useLatest({ deps, onFailure })
  const slot = ui.slot ?? 'panel'

  useEffect(() => {
    const el = element.current
    if (!el) return
    const fail = (message: string) => latest.current.onFailure({ file: ui.module, message })
    if (!UI_API_SUPPORTED.includes(ui.api)) {
      fail(`written for host API ${ui.api}; this ScadBuddy supports ${UI_API_SUPPORTED.join(', ')}`)
      return
    }
    // One element and shadow root per mount: a slow `mount` of the template this
    // replaces writes into its own detached root, never into the next one's.
    const holder = document.createElement('div')
    holder.dataset['testid'] = 'template-ui'
    holder.className = 'h-full min-h-0'
    el.append(holder)
    const root = holder.attachShadow({ mode: 'open' })
    adoptAppStyles(root)
    // This mount's own deps: a host the template kept after its unmount never reaches
    // the next template's.
    const created = createHost(latest.current.deps)
    handle.current = created
    let active = true
    let cleanup: (() => void) | void
    void (async () => {
      try {
        const module = await loadUiModule(api.uiFileUrl(slug, version, ui.module.replace(/^ui\//, '')))
        const mount = mountOf(module)
        if (!mount) throw new Error(`${ui.module} does not export a mount function`)
        if (!active) return
        const result = await mount(root, created.host, { slot, version: version ?? null, theme: theme(), api: ui.api })
        if (active) cleanup = result
        else if (typeof result === 'function') result()
      } catch (cause) {
        if (active) fail(cause instanceof Error ? cause.message : String(cause))
      }
    })()
    return () => {
      active = false
      created.dispose()
      handle.current = null
      try {
        if (typeof cleanup === 'function') cleanup()
      } catch (cause) {
        console.error(`${ui.module}: its cleanup threw`, cause)
      }
      root.replaceChildren()
      holder.remove()
    }
  }, [slug, version, ui.module, ui.api, slot, latest])

  useEffect(() => {
    handle.current?.notify(inputs)
  }, [inputs])

  return <div ref={element} className="h-full min-h-0 overflow-auto" />
}
