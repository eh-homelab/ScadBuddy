import { screen, waitFor } from '@testing-library/react'
import { expect } from 'vitest'

/**
 * #7276 — a panel's live region (`role="status"`) is always mounted and starts empty, so a test
 * waits for its text rather than for the region. With no `text`, any text.
 */
export async function said(text: string | RegExp = /\S/): Promise<HTMLElement> {
  let status: HTMLElement | undefined
  await waitFor(() => {
    status = screen.getByRole('status')
    expect(status).toHaveTextContent(text)
  })
  return status!
}
