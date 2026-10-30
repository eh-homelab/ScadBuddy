import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { setMintedMcpToken, useMintedMcpToken } from './mcpMintedToken'

describe('mcpMintedToken', () => {
  afterEach(() => act(() => setMintedMcpToken(null)))

  it('gives the hook the value set', () => {
    const { result } = renderHook(() => useMintedMcpToken())
    expect(result.current).toBeNull()
    act(() => setMintedMcpToken('sbmcp_x'))
    expect(result.current).toBe('sbmcp_x')
  })

  it('does not render again when the same value is set', () => {
    let renders = 0
    renderHook(() => {
      renders += 1
      return useMintedMcpToken()
    })
    const before = renders
    act(() => setMintedMcpToken(null))
    expect(renders).toBe(before)
    act(() => setMintedMcpToken('sbmcp_x'))
    const mid = renders
    act(() => setMintedMcpToken('sbmcp_x'))
    expect(renders).toBe(mid)
  })

  it('stops notifying a hook that unmounted', () => {
    let renders = 0
    const { unmount } = renderHook(() => {
      renders += 1
      return useMintedMcpToken()
    })
    unmount()
    const before = renders
    act(() => setMintedMcpToken('sbmcp_y'))
    expect(renders).toBe(before)
  })
})
