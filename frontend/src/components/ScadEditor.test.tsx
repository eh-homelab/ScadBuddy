import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ScadEditor } from './ScadEditor'

const SOURCE = 'size = 10;\ncube([size, size, size)\n'

describe('ScadEditor', () => {
  it('shows the source in a labelled editor with a gutter', () => {
    const { container } = render(
      <ScadEditor value={SOURCE} onChange={() => {}} label="OpenSCAD source" />,
    )
    expect(screen.getByLabelText('OpenSCAD source')).toBeInTheDocument()
    expect(container.querySelector('.cm-gutters')).not.toBeNull()
    expect(container.textContent).toContain('cube([size, size, size)')
  })

  it('marks the lines OpenSCAD complained about', () => {
    const { container, rerender } = render(
      <ScadEditor value={SOURCE} onChange={() => {}} label="OpenSCAD source" />,
    )
    expect(container.querySelectorAll('.cm-sb-error-line')).toHaveLength(0)

    rerender(
      <ScadEditor value={SOURCE} onChange={() => {}} errorLines={[2]} label="OpenSCAD source" />,
    )
    expect(container.querySelectorAll('.cm-sb-error-line')).toHaveLength(1)
  })

  it('takes a new value from outside without losing the marks', () => {
    const { container, rerender } = render(
      <ScadEditor value={SOURCE} onChange={() => {}} label="OpenSCAD source" />,
    )
    rerender(
      <ScadEditor value="cube(1);\n" onChange={() => {}} label="OpenSCAD source" />,
    )
    expect(container.textContent).toContain('cube(1);')
    expect(container.textContent).not.toContain('size = 10;')
  })
})
