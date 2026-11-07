import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { pushServicesState } from '../mocks/features/push'
import { PushServicesSetting } from './PushServicesSetting'

// #815 — the push-service allowlist in Settings: one host per line, saved by the person only.

describe('PushServicesSetting', () => {
  it('shows the allowlist one host per line and saves an edit', async () => {
    render(<PushServicesSetting />)
    const box = (await screen.findByLabelText('Allowed push services')) as HTMLTextAreaElement
    expect(box.value.split('\n')).toEqual(pushServicesState().default)
    fireEvent.change(box, { target: { value: 'fcm.googleapis.com\n\n *.push.example.net ' } })
    const save = screen.getByRole('button', { name: /save/i })
    expect(save.hasAttribute('data-agent-user-only')).toBe(true)
    fireEvent.click(save)
    await waitFor(() => expect(pushServicesState().hosts).toEqual(['fcm.googleapis.com', '*.push.example.net']))
  })

  it('says why a host was refused', async () => {
    render(<PushServicesSetting />)
    const box = await screen.findByLabelText('Allowed push services')
    fireEvent.change(box, { target: { value: 'not a host' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    expect(await screen.findByText(/must be a host name/)).toBeTruthy()
  })
})
