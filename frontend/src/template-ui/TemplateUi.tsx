import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import type { JsonObject } from '../lib/inputs'
import { useLatest } from '../lib/useLatest'
import { defineHostElements, provideRegistry, type HostElement } from './elements'
import { createHost, type HostDeps, type HostHandle } from './host'
import { effectiveValues } from './bindings'
import { HostElementContent, type ElementContext } from './HostElementContent'
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
  /** What the host renders into `<sb-*>` elements (spec §4.3); without it they stay empty. */
  elementContext?: Omit<ElementContext, 'slot'>
}

const keys = new WeakMap<HostElement, number>()
let nextKey = 0

function keyOf(el: HostElement): string {
  let key = keys.get(el)
  if (key === undefined) {
    key = nextKey++
    keys.set(el, key)
  }
  return String(key)
}

function mountOf(module: unknown): Mount | undefined {
  const mount = (module as { mount?: unknown } | null)?.mount
  return typeof mount === 'function' ? (mount as Mount) : undefined
}

function theme(): 'light' | 'dark' {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** A template's own interface (spec 2026-09-27 §4.2): not sandboxed, style-isolated. */
export function TemplateUi({ slug, ui, version, deps, inputs, onFailure, elementContext }: Props) {
  const element = useRef<HTMLDivElement>(null)
  const handle = useRef<HostHandle | null>(null)
  const latest = useLatest({ deps, onFailure })
  const slot = ui.slot ?? 'panel'
  const [elements, setElements] = useState<readonly HostElement[]>([])
  const [, setRevision] = useState(0)
  const firstPreview = elements.find((el) => el.localName === 'sb-preview')
  const values = elementContext ? effectiveValues(elements, elementContext) : {}

  useEffect(() => {
    const el = element.current
    if (!el) return
    const fail = (message: string) => latest.current.onFailure({ file: ui.module, message })
    if (!UI_API_SUPPORTED.includes(ui.api)) {
      fail(`written for host API ${ui.api}; this ScadBuddy supports ${UI_API_SUPPORTED.join(', ')}`)
      return
    }
    defineHostElements()
    provideRegistry(el, {
      add: (added) => setElements((current) => (current.includes(added) ? current : [...current, added])),
      remove: (removed) => setElements((current) => current.filter((candidate) => candidate !== removed)),
      changed: () => setRevision((n) => n + 1),
    })
    const root = el.shadowRoot ?? el.attachShadow({ mode: 'open' })
    adoptAppStyles(root)
    const created = createHost({
      slug,
      version,
      getSchema: () => latest.current.deps.getSchema(),
      getInputs: () => latest.current.deps.getInputs(),
      setInputs: (next) => latest.current.deps.setInputs(next),
      generate: () => latest.current.deps.generate(),
      openPrint: (id) => latest.current.deps.openPrint(id),
      presets: {
        list: () => latest.current.deps.presets.list(),
        save: (name) => latest.current.deps.presets.save(name),
        load: (id) => latest.current.deps.presets.load(id),
      },
      onDescribe: (fn) => latest.current.deps.onDescribe(fn),
    })
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
      provideRegistry(el, undefined)
      setElements([])
    }
  }, [slug, version, ui.module, ui.api, slot, latest])

  useEffect(() => {
    handle.current?.notify(inputs)
  }, [inputs])

  return (
    <>
      <div
        // One element, and so one shadow root, per mount: an async mount still in flight
        // when the template changes can only write into its own, detached root.
        key={[slug, version ?? '', ui.module, ui.api, slot].join('\n')}
        ref={element}
        data-testid="template-ui"
        className="h-full min-h-0 overflow-auto"
      />
      {elementContext &&
        elements.map((el) =>
          createPortal(
            <HostElementContent
              element={el}
              context={{ ...elementContext, slot }}
              values={values}
              firstPreview={firstPreview}
            />,
            el,
            keyOf(el),
          ),
        )}
    </>
  )
}
