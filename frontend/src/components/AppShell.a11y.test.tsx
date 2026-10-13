import { screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { Link, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../test/utils'
import { AppShell } from './AppShell'

function Renamed() {
  const [name, setName] = useState('Draft')
  return (
    <>
      <h1>{name}</h1>
      <button type="button" onClick={() => setName('Keychain')}>
        Rename
      </button>
    </>
  )
}

function shell() {
  return renderPage(
    <Routes>
      <Route element={<AppShell embedded={false} tabLink={null} />}>
        <Route
          path="/"
          element={
            <>
              <h1>Models</h1>
              <Link to="/m/x">a model</Link>
            </>
          }
        />
        <Route path="/m/x" element={<Renamed />} />
        <Route path="/blank" element={<p>no heading</p>} />
      </Route>
    </Routes>,
  )
}

describe('AppShell, for assistive technology (#2454)', () => {
  it('titles each page by its own heading, following it as it changes', async () => {
    const { user } = shell()
    await waitFor(() => expect(document.title).toBe('Models · ScadBuddy'))

    await user.click(screen.getByRole('link', { name: 'a model' }))
    await waitFor(() => expect(document.title).toBe('Draft · ScadBuddy'))
    await user.click(screen.getByRole('button', { name: 'Rename' }))
    await waitFor(() => expect(document.title).toBe('Keychain · ScadBuddy'))
  })

  it('is plain ScadBuddy for a page with no heading', async () => {
    renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} tabLink={null} />}>
          <Route path="*" element={<p>no heading</p>} />
        </Route>
      </Routes>,
    )
    await waitFor(() => expect(document.title).toBe('ScadBuddy'))
  })

  it('skips to the main content from the first Tab stop', async () => {
    const { user } = shell()
    await user.tab()
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skip).toHaveFocus()

    await user.keyboard('{Enter}')

    expect(screen.getByRole('main')).toHaveFocus()
  })

  it('names the logo link by the text it shows', () => {
    shell()
    expect(screen.getByRole('link', { name: 'ScadBuddy OpenSCAD customizer' })).toHaveAttribute('href', '/')
  })
})
