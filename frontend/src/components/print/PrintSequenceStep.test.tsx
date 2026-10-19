import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PrintSequenceStep } from './PrintSequenceStep'

const noop = () => {}

describe('PrintSequenceStep (#956)', () => {
  it('says how many objects a by-object plate has', () => {
    render(<PrintSequenceStep value="by object" onChange={noop} objects={3} />)
    expect(screen.getByText(/This plate has 3 objects\./)).toBeInTheDocument()
  })

  it('says by object changes nothing for a plate of one object', () => {
    render(<PrintSequenceStep value="by object" onChange={noop} objects={1} />)
    expect(screen.getByText(/one object, so by object prints it as by layer would/)).toBeInTheDocument()
  })

  it('says no count while the check has not read one', () => {
    render(<PrintSequenceStep value="by object" onChange={noop} />)
    expect(screen.getByText(/objects need room between them\.$/)).toBeInTheDocument()
    expect(screen.queryByText(/This plate has/)).not.toBeInTheDocument()
  })

  it('says nothing about objects by layer', () => {
    render(<PrintSequenceStep value="by layer" onChange={noop} objects={3} />)
    expect(screen.queryByText(/object/i, { selector: 'p' })).not.toBeInTheDocument()
  })

  it("says how many objects a plate has when by object is the template's default (#3222)", () => {
    render(<PrintSequenceStep value={null} inherited="by object" onChange={noop} objects={3} />)
    expect(screen.getByText(/This plate has 3 objects\./)).toBeInTheDocument()
  })

  it('says nothing about objects when this print sets by layer over a by-object default', () => {
    render(<PrintSequenceStep value="by layer" inherited="by object" onChange={noop} objects={3} />)
    expect(screen.queryByText(/object/i, { selector: 'p' })).not.toBeInTheDocument()
  })

  it('says no count when the file lists no objects for the plate (#3222)', () => {
    render(<PrintSequenceStep value="by object" onChange={noop} objects={0} />)
    expect(screen.getByText(/objects need room between them\.$/)).toBeInTheDocument()
    expect(screen.queryByText(/0 objects/)).not.toBeInTheDocument()
  })
})
