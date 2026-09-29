import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import type { JsonObject } from '../lib/inputs'
import { findParamRow } from './elements'
import { setUiModuleLoader } from './loadModule'
import { TemplateUi } from './TemplateUi'

afterEach(() => setUiModuleLoader(null))

function page(inputs: JsonObject, onInputs: (next: JsonObject) => void, slot: 'panel' | 'page' = 'panel') {
  return (
    <TemplateUi
      slug="name-keychain"
      ui={{ module: 'ui/index.js', slot, api: 1 }}
      version={undefined}
      inputs={inputs}
      onFailure={vi.fn()}
      deps={{
        slug: 'name-keychain',
        version: undefined,
        getSchema: () => keychainSchema,
        getInputs: () => inputs,
        setInputs: onInputs,
        generate: vi.fn(),
        openPrint: vi.fn(),
        presets: { list: vi.fn(), save: vi.fn(), load: vi.fn() },
        onDescribe: vi.fn(),
      }}
      elementContext={{
        schema: keychainSchema,
        slug: 'name-keychain',
        fonts: [],
        inputs,
        onInputs,
        preview: <div data-testid="the-preview" />,
        generate: <button type="button">Generate</button>,
      }}
    />
  )
}

function shadowOf(container: HTMLElement): ShadowRoot {
  const root = container.querySelector('[data-testid="template-ui"]')?.shadowRoot
  if (!root) throw new Error('no shadow root')
  return root
}

describe('host custom elements', () => {
  it('renders a bound parameter widget and writes through its path', async () => {
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<sb-param name="name"></sb-param><sb-param name="name" bind="style.label"></sb-param><sb-param name="nope"></sb-param>'
      },
    }))
    const onInputs = vi.fn()
    const { container } = render(page({ params: { name: 'Hi' }, style: { label: 'Yo' } }, onInputs))
    await waitFor(() => expect(shadowOf(container).querySelectorAll('input').length).toBeGreaterThanOrEqual(2))
    const [first, second] = Array.from(shadowOf(container).querySelectorAll('input'))
    expect((first as HTMLInputElement).value).toBe('Hi')
    expect((second as HTMLInputElement).value).toBe('Yo')
    fireEvent.change(second as HTMLInputElement, { target: { value: 'Ho' } })
    expect(onInputs).toHaveBeenLastCalledWith({ params: { name: 'Hi' }, style: { label: 'Ho' } })
    expect(shadowOf(container).textContent).toContain('model.scad has no parameter “nope”')
    // The agent's highlight finds the widget through the shadow root, as it does a panel row.
    const row = shadowOf(container).querySelector('[data-param="name"]')
    expect(row).not.toBeNull()
    expect(findParamRow('name')).toBe(row)
    expect(findParamRow('nope')).toBeNull()
  })

  it('renders into an element inside a nested component shadow root', async () => {
    if (!customElements.get('x-nested-card')) {
      customElements.define(
        'x-nested-card',
        class extends HTMLElement {
          connectedCallback() {
            if (!this.shadowRoot) this.attachShadow({ mode: 'open' }).innerHTML = '<sb-param name="name"></sb-param>'
          }
        },
      )
    }
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<x-nested-card></x-nested-card>'
      },
    }))
    const { container } = render(page({ params: { name: 'Hi' } }, vi.fn()))
    await waitFor(() =>
      expect(shadowOf(container).querySelector('x-nested-card')?.shadowRoot?.querySelector('input')?.value).toBe('Hi'),
    )
  })

  it('shows the preview only in the page slot, and Generate in both', async () => {
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<sb-preview></sb-preview><sb-generate></sb-generate>'
      },
    }))
    const panel = render(page({ params: {} }, vi.fn(), 'panel'))
    await waitFor(() => expect(shadowOf(panel.container).textContent).toContain('Generate'))
    expect(shadowOf(panel.container).querySelector('[data-testid="the-preview"]')).toBeNull()
    panel.unmount()
    const full = render(page({ params: {} }, vi.fn(), 'page'))
    await waitFor(() => expect(shadowOf(full.container).querySelector('[data-testid="the-preview"]')).not.toBeNull())
  })
})
