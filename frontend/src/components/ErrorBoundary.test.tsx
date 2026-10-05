import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
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

  it('mounts the children again on retry, after onRetry', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    state.throws = true
    const onRetry = vi.fn(() => {
      state.throws = false
    })
    render(
      <ErrorBoundary fallback={fallback} onRetry={onRetry}>
        <Flaky />
      </ErrorBoundary>,
    )
    fireEvent.click(screen.getByRole('button'))
    expect(onRetry).toHaveBeenCalledOnce()
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
})
