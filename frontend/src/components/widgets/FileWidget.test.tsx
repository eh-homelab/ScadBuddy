import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../../api/client'
import type { Param, ParamValue } from '../../api/types'
import { ASSET_REFUSAL, storeAsset } from '../../mocks/handlers'
import { ParamWidget } from './ParamWidget'

const SLUG = 'gridfinity-bin'
const HEART = '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><path d="M0 0H4V4Z"/></svg>'

const param: Param = {
  group: 'Features',
  name: 'label_art',
  type: 'file',
  initial: '',
  caption: 'Label artwork',
  accept: ['svg', 'png'],
}

function Harness({
  initial,
  onChange,
  of = param,
}: {
  initial: ParamValue
  onChange: (next: ParamValue) => void
  of?: Param
}) {
  const [value, setValue] = useState(initial)
  return (
    <ParamWidget
      param={of}
      value={value}
      slug={SLUG}
      fonts={[]}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

function setup(initial: ParamValue = '', of: Param = param) {
  const onChange = vi.fn()
  // The picker's `accept` is a hint to the OS dialog; the server sniffs regardless.
  const user = userEvent.setup({ applyAccept: false })
  render(<Harness initial={initial} onChange={onChange} of={of} />)
  return { onChange, user }
}

describe('file parameter (#204)', () => {
  // jsdom's `File` cannot be a multipart body for Node's `fetch`, so the upload goes
  // straight to what the msw handler does with it. The e2e run covers the wire.
  beforeEach(() => {
    vi.spyOn(api, 'uploadAsset').mockImplementation(async (_slug, file) => {
      const stored = await storeAsset(file)
      if (!stored) {
        throw new ApiError({ title: 'Unprocessable Content', status: 422, detail: ASSET_REFUSAL })
      }
      return stored
    })
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('starts empty, naming the kinds it takes', () => {
    setup()
    expect(screen.getByText('Label artwork')).toBeInTheDocument()
    expect(screen.getByText('svg · png')).toBeInTheDocument()
    expect(screen.getByText('Drop a file here')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear Label artwork' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Label artwork')).toHaveAttribute(
      'accept',
      '.svg,image/svg+xml,.png,image/png',
    )
  })

  it('uploads a picked file and takes its id, showing the original name', async () => {
    const { onChange, user } = setup()
    await user.upload(
      screen.getByLabelText('Label artwork'),
      new File([HEART], 'heart.svg', { type: 'image/svg+xml' }),
    )

    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1))
    const id = onChange.mock.calls[0]?.[0] as string
    expect(id).toMatch(/^[0-9a-f]{64}$/)
    expect(await screen.findByText('heart.svg')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Preview of heart.svg' })).toHaveAttribute(
      'src',
      api.assetContentUrl(SLUG, id),
    )
  })

  it('takes a file dropped on it', async () => {
    const { onChange } = setup()
    const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0])
    fireEvent.drop(screen.getByTestId('drop-label_art'), {
      dataTransfer: { files: [new File([png], 'star.png', { type: 'image/png' })] },
    })

    expect(await screen.findByText('star.png')).toBeInTheDocument()
    expect(screen.getByText('PNG · 96×96')).toBeInTheDocument()
    expect(onChange).toHaveBeenCalledWith(expect.stringMatching(/^[0-9a-f]{64}$/))
  })

  it('clears back to no file', async () => {
    const { onChange, user } = setup()
    await user.upload(
      screen.getByLabelText('Label artwork'),
      new File([HEART], 'heart.svg', { type: 'image/svg+xml' }),
    )
    await screen.findByText('heart.svg')

    await user.click(screen.getByRole('button', { name: 'Clear Label artwork' }))

    expect(onChange).toHaveBeenLastCalledWith('')
    expect(screen.getByText('Drop a file here')).toBeInTheDocument()
  })

  it('shows what the server refused, and keeps the value', async () => {
    const { onChange, user } = setup()
    await user.upload(
      screen.getByLabelText('Label artwork'),
      new File(['GIF89a'], 'cat.gif', { type: 'image/gif' }),
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'only SVG and PNG files can be attached',
    )
    expect(onChange).not.toHaveBeenCalled()
  })

  it('refuses a kind this parameter does not take', async () => {
    const { onChange, user } = setup('', { ...param, accept: ['png'] })
    await user.upload(
      screen.getByLabelText('Label artwork'),
      new File([HEART], 'heart.svg', { type: 'image/svg+xml' }),
    )

    expect(await screen.findByRole('alert')).toHaveTextContent('takes PNG, not SVG')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('names an id it was reopened on, as "Customize this version" does', async () => {
    const stored = await api.uploadAsset(
      SLUG,
      new File([HEART], 'reopened.svg', { type: 'image/svg+xml' }),
    )
    setup(stored.id)
    expect(await screen.findByText('reopened.svg')).toBeInTheDocument()
  })

  it('shows the model’s own default file for what it is', () => {
    setup('sample-overlay.svg', { ...param, initial: 'sample-overlay.svg' })
    expect(screen.getByText('Model default: sample-overlay.svg')).toBeInTheDocument()
  })
})

describe('mock API: file parameters (#204)', () => {
  it('refuses a render whose file value is not an upload', async () => {
    const error: unknown = await api
      .render(SLUG, { label_art: '../../etc/passwd' })
      .catch((caught: unknown) => caught)
    expect(error).toMatchObject({ status: 422 })
  })
})
