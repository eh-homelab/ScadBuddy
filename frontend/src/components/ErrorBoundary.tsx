import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** What shows instead of the children once one of them threw; `retry` mounts them again. */
  fallback: (error: Error, retry: () => void) => ReactNode
  /** A change of this value mounts the children again: a new render, another page. */
  resetKey?: unknown
  /** Runs once a child threw: drop whatever cached the failure, so a remount tries afresh. */
  onError?: (error: Error) => void
}

interface State {
  error: Error | null
}

/**
 * #361 — a component that throws while rendering unmounts the whole React tree unless
 * something above it catches. This keeps the failure to the part that failed.
 */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null }

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  // No console.error here: React 19 already reports every caught error (#1446).
  override componentDidCatch(error: unknown) {
    this.props.onError?.(error instanceof Error ? error : new Error(String(error)))
  }

  override componentDidUpdate(previous: Props, previousState: State) {
    // Only an error already shown before this update: one caught in the very update that
    // changed resetKey belongs to the new children, and clearing it would hide it (#1456).
    if (previousState.error && !Object.is(previous.resetKey, this.props.resetKey)) {
      this.setState({ error: null })
    }
  }

  retry = () => {
    this.setState({ error: null })
  }

  override render() {
    return this.state.error ? this.props.fallback(this.state.error, this.retry) : this.props.children
  }
}
