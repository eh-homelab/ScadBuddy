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

/** Renders inside a router so components using `Link`/`useParams` work. */
export function renderPage(
  ui: ReactElement,
  { route = '/', path, state, userEventOptions, ...options }: Options = {},
) {
  const user = userEvent.setup(userEventOptions)
  const view = render(
    <MemoryRouter initialEntries={[state === undefined ? route : { ...parse(route), state }]}>
      {path ? <Routes><Route path={path} element={ui} /></Routes> : ui}
    </MemoryRouter>,
    options,
  )
  return { user, ...view }
}
