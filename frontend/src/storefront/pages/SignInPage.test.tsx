import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SignInPage } from './SignInPage'

const CODE = 'ABCD-EFGH-JKMN-PQRS'

function Where() {
  const location = useLocation()
  return <output data-testid="where">{`${location.pathname}${location.hash}`}</output>
}

function page(route = '/account/sign-in') {
  return renderPage(
    <>
      <SignInPage />
      <Where />
    </>,
    { route },
  )
}

describe('SignInPage', () => {
  it('fills the code from the mailed link and drops it from the address', async () => {
    page(`/account/sign-in#code=${CODE}`)
    expect(await screen.findByLabelText('Code')).toHaveValue(CODE)
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/account\/sign-in$/))
  })

  it('signs in with the address and the code, then shows the account', async () => {
    let sent: unknown = null
    server.use(
      http.post('/api/store/v1/account/verify', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ email: 'ada@example.com' })
      }),
    )
    const { user } = page()
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Code'), CODE)
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/account$/))
    expect(sent).toEqual({ email: 'ada@example.com', code: CODE })
  })

  it('says what the shop said about a code that did not work', async () => {
    const { user } = page()
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Code'), 'nope')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That code did not sign you in.')
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/account\/sign-in$/)
  })

  it('says how long to wait after too many tries', async () => {
    server.use(
      http.post('/api/store/v1/account/verify', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/sign_in_limited', title: 'Too Many Requests', status: 429, detail: 'Too many tries.' },
          { status: 429, headers: { 'Content-Type': 'application/problem+json', 'Retry-After': '120' } },
        ),
      ),
    )
    const { user } = page()
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.type(screen.getByLabelText('Code'), CODE)
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many tries. Try again in 120 seconds.')
  })
})
