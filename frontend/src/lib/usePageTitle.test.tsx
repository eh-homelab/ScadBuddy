import { act, render, renderHook } from '@testing-library/react'
import { useRef } from 'react'
import { describe, expect, it } from 'vitest'
import { usePageTitle } from './usePageTitle'

function Titled({ children, onTitle }: { children: React.ReactNode; onTitle: (title: string) => void }) {
  const main = useRef<HTMLElement>(null)
  onTitle(usePageTitle(main))
  return <main ref={main}>{children}</main>
}

describe('usePageTitle (#2454)', () => {
  it('takes all of a heading with nested markup, as one line', () => {
    let title = ''
    render(
      <Titled onTitle={(t) => (title = t)}>
        <h1>
          keychain <a href="/flows/keychain/versions/2">v2</a>
        </h1>
      </Titled>,
    )
    expect(title).toBe('keychain v2 · ScadBuddy')
  })

  it('follows a heading rendered late, and is the bare name with none', async () => {
    let title = ''
    const { rerender } = render(<Titled onTitle={(t) => (title = t)}>{null}</Titled>)
    expect(title).toBe('ScadBuddy')
    rerender(
      <Titled onTitle={(t) => (title = t)}>
        <h1>Library</h1>
      </Titled>,
    )
    await act(async () => {})
    expect(title).toBe('Library · ScadBuddy')
  })

  it('is the bare name with no main', () => {
    const { result } = renderHook(() => usePageTitle({ current: null }))
    expect(result.current).toBe('ScadBuddy')
  })
})
