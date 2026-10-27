import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderSettingsSection } from '../testing'
import { Component as About } from './about'

describe('About', () => {
  it('shows the build and the values only the deployment sets', async () => {
    renderSettingsSection(<About />)
    const about = await screen.findByTestId('about')
    expect(about).toHaveTextContent('v0.42.0 (abc1234)')
    expect(about).toHaveTextContent('OpenSCAD version 2026.09.28')
    await waitFor(() => expect(about).toHaveTextContent('1.2.5.6'))
    const table = screen.getByRole('table', { name: 'Deployment values' })
    expect(table).toHaveTextContent('SCADBUDDY_DATABASE_URL')
    expect(table).toHaveTextContent('postgres.scadbuddy.svc:5432/scadbuddy')
    expect(table).toHaveTextContent('SCADBUDDY_OPENSCAD')
    // Read-only: nothing here is an input.
    expect(within(table).queryByRole('textbox')).toBeNull()
  })
})
