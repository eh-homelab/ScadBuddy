import { render, type RenderOptions } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'

interface Options extends Omit<RenderOptions, 'wrapper'> {
  route?: string
  path?: string
  /** Router state for the initial entry — what `<Navigate state={…}>` hands over. */
  state?: unknown
  /** Passed straight to `userEvent.setup` — e.g. `{ applyAccept: false }`. */
  userEventOptions?: Parameters<typeof userEvent.setup>[0]
}

function parse(route: string) {
  const [pathname = '/', search] = route.split('?')
  return { pathname, search: search ? `?${search}` : '' }
}

/**
 * Renders inside a router so components using `Link`/`useParams` work.
 *
 * `rerender` takes the bare element and re-renders it in place inside the same
 * router, as testing-library's does: component state survives (#1013).
 * Pass the bare element, never one wrapped in a `MemoryRouter`: `rerender`
 * re-wraps it, so a wrapped element nests a router inside a router and throws.
 */
export function renderPage(
  ui: ReactElement,
  { route = '/', path, state, userEventOptions, ...options }: Options = {},
) {
  const user = userEvent.setup(userEventOptions)
  const entries = [state === undefined ? route : { ...parse(route), state }]
  const tree = (element: ReactElement) => (
    <MemoryRouter initialEntries={entries}>
      {path ? <Routes><Route path={path} element={element} /></Routes> : element}
    </MemoryRouter>
  )
  const view = render(tree(ui), options)
  return { user, ...view, rerender: (next: ReactElement) => view.rerender(tree(next)) }
}
