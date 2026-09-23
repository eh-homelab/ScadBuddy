import { render, type RenderOptions } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'

interface Options extends Omit<RenderOptions, 'wrapper'> {
  route?: string
  path?: string
  /** Passed straight to `userEvent.setup` — e.g. `{ applyAccept: false }`. */
  userEventOptions?: Parameters<typeof userEvent.setup>[0]
}

/** Renders inside a router so components using `Link`/`useParams` work. */
export function renderPage(
  ui: ReactElement,
  { route = '/', path, userEventOptions, ...options }: Options = {},
) {
  const user = userEvent.setup(userEventOptions)
  const view = render(
    <MemoryRouter initialEntries={[route]}>
      {path ? <Routes><Route path={path} element={ui} /></Routes> : ui}
    </MemoryRouter>,
    options,
  )
  return { user, ...view }
}
