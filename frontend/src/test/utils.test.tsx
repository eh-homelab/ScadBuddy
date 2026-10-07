import { screen } from '@testing-library/react'
import { useState } from 'react'
import { useLocation, useParams } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderPage } from './utils'

function Counter({ label }: { label: string }) {
  const [count, setCount] = useState(0)
  const { pathname } = useLocation()
  const { id } = useParams()
  return (
    <button onClick={() => setCount((n) => n + 1)}>
      {label}: {count} at {pathname}
      {id ? ` (${id})` : ''}
    </button>
  )
}

describe('renderPage', () => {
  it('rerender updates props in place: state and the router survive (#1013)', async () => {
    const { user, rerender } = renderPage(<Counter label="first" />, { route: '/x' })
    await user.click(screen.getByRole('button'))
    const before = screen.getByRole('button')
    expect(before).toHaveTextContent('first: 1 at /x')

    rerender(<Counter label="second" />)

    expect(screen.getByRole('button')).toBe(before)
    expect(screen.getByRole('button')).toHaveTextContent('second: 1 at /x')
  })

  it('rerender keeps the route when a path is given', async () => {
    const { user, rerender } = renderPage(<Counter label="first" />, { route: '/m/7', path: '/m/:id' })
    await user.click(screen.getByRole('button'))

    rerender(<Counter label="second" />)

    expect(screen.getByRole('button')).toHaveTextContent('second: 1 at /m/7 (7)')
  })
})
