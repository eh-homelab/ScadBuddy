import { describe, expect, it } from 'vitest'
import type { CustomizerSchema } from '../api/types'
import { bindingOf } from './bindings'
import type { HostElement } from './elements'

// A parameter OpenSCAD exports without an initial value (`Parameter.initial` may be null).
const schema = {
  parameters: [{ name: 'label', type: 'string', group: 'Text', initial: null, caption: '' }],
} as unknown as CustomizerSchema

function sbParam(attributes: Record<string, string>): HostElement {
  const element = document.createElement('div')
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value)
  return element as unknown as HostElement
}

describe('bindingOf', () => {
  it('gives a parameter with no initial value and nothing bound an empty value, not undefined', () => {
    const binding = bindingOf(sbParam({ name: 'label' }), { schema, inputs: { params: {} } })
    expect(binding).toMatchObject({ name: 'label', value: '' })
  })

  it('names the parameter type when a mistyped value meets no initial value', () => {
    const binding = bindingOf(sbParam({ name: 'label', bind: 'style.label' }), {
      schema,
      inputs: { params: {}, style: { label: { not: 'scalar' } } },
    })
    expect(binding).toMatchObject({ value: '' })
    const mistyped = typeof binding === 'string' ? binding : binding.mistyped
    expect(mistyped).toContain('style.label')
    expect(mistyped).not.toContain('undefined')
    expect(mistyped).not.toContain('a object')
  })
})
