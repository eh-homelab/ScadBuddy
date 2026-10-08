import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

const state = { throws: true }

function Flaky() {
  if (state.throws) throw new Error('boom')
  return <p>fine</p>
}

function fallback(error: Error, retry: () => void) {
  return (
    <button type="button" onClick={retry}>
      failed: {error.message}
    </button>
  )
}

describe('ErrorBoundary (#361)', () => {
  // The console.error spies below must not silence React's warnings in later tests.
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the fallback for a child that throws, and its siblings stay', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    state.throws = true
    render(
      <div>
        <p>header</p>
        <ErrorBoundary fallback={fallback}>
          <Flaky />
        </ErrorBoundary>
      </div>,
    )
    expect(screen.getByRole('button')).toHaveTextContent('failed: boom')
    expect(screen.getByText('header')).toBeInTheDocument()
  })

  it('tells onError once a child threw, and mounts the children again on retry', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    state.throws = true
    const onError = vi.fn(() => {
      state.throws = false
    })
    render(
      <ErrorBoundary fallback={fallback} onError={onError}>
        <Flaky />
      </ErrorBoundary>,
    )
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: 'boom' }))
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByText('fine')).toBeInTheDocument()
  })

  it('mounts the children again when resetKey changes', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    state.throws = true
    const { rerender } = render(
      <ErrorBoundary fallback={fallback} resetKey="/a">
        <Flaky />
      </ErrorBoundary>,
    )
    state.throws = false
    rerender(
      <ErrorBoundary fallback={fallback} resetKey="/b">
        <Flaky />
      </ErrorBoundary>,
    )
    expect(screen.getByText('fine')).toBeInTheDocument()
  })

  it('keeps an error caught in the same update that changed resetKey (#1456)', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    state.throws = false
    // onError drops what made the child throw, as a cache would: a boundary that cleared
    // this error would mount the child again and hide that it ever failed.
    const onError = () => {
      state.throws = false
    }
    const { rerender } = render(
      <ErrorBoundary fallback={fallback} resetKey="/a" onError={onError}>
        <Flaky />
      </ErrorBoundary>,
    )
    state.throws = true
    rerender(
      <ErrorBoundary fallback={fallback} resetKey="/b" onError={onError}>
        <Flaky />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('button')).toHaveTextContent('failed: boom')
  })

  it('logs nothing of its own: React already reports the error (#1446)', () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    state.throws = true
    render(
      <ErrorBoundary fallback={fallback}>
        <Flaky />
      </ErrorBoundary>,
    )
    expect(logged).not.toHaveBeenCalledWith('A component failed to render', expect.anything(), expect.anything())
  })
})
