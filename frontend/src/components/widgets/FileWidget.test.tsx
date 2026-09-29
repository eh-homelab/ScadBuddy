import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  version,
}: {
  initial: ParamValue
  onChange: (next: ParamValue) => void
  of?: Param
  version?: string
}) {
  const [value, setValue] = useState(initial)
  return (
    <ParamWidget
      param={of}
      value={value}
      slug={SLUG}
      version={version}
      fonts={[]}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

function setup(initial: ParamValue = '', of: Param = param, version?: string) {
  const onChange = vi.fn()
  // The picker's `accept` is a hint to the OS dialog; the server sniffs regardless.
  const user = userEvent.setup({ applyAccept: false })
  render(<Harness initial={initial} onChange={onChange} of={of} version={version} />)
  return { onChange, user }
}

// jsdom's `File` cannot be a multipart body for Node's `fetch`, so the upload goes
// straight to what the msw handler does with it. The e2e run covers the wire.
function mockUpload() {
  vi.spyOn(api, 'uploadAsset').mockImplementation(async (_slug, file) => {
    const stored = await storeAsset(file)
    if (!stored) {
      throw new ApiError({ title: 'Unprocessable Content', status: 422, detail: ASSET_REFUSAL })
    }
    return stored
  })
}

describe('file parameter (#204)', () => {
  beforeEach(mockUpload)
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

describe('file parameter samples', () => {
  beforeEach(mockUpload)
  afterEach(() => {
    vi.restoreAllMocks()
  })
  const withSamples: Param = { ...param, samples: ['sample-heart.svg', 'sample-star.png'] }

  it('offers no sample row when the template ships none', () => {
    setup()
    expect(screen.queryByTestId('samples-label_art')).not.toBeInTheDocument()
  })

  it('lists each sample with a thumbnail from the template', () => {
    setup('', withSamples)
    const group = screen.getByTestId('samples-label_art')
    const buttons = within(group).getAllByRole('button')
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual([
      'Use sample sample-heart.svg',
      'Use sample sample-star.png',
    ])
    expect(buttons.every((button) => button.getAttribute('aria-pressed') === 'false')).toBe(true)
    expect(within(buttons[0]!).getByRole('presentation')).toHaveAttribute(
      'src',
      api.sampleContentUrl(SLUG, 'sample-heart.svg'),
    )
  })

  it('takes a picked sample by its bare name and previews it', async () => {
    const { onChange, user } = setup('', withSamples)
    await user.click(screen.getByRole('button', { name: 'Use sample sample-star.png' }))

    expect(onChange).toHaveBeenCalledWith('sample-star.png')
    expect(screen.getByRole('button', { name: 'Use sample sample-star.png' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByText('Template sample')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Preview of sample-star.png' })).toHaveAttribute(
      'src',
      api.sampleContentUrl(SLUG, 'sample-star.png'),
    )
    // A sample can still be cleared, and an upload still replaces it.
    expect(screen.getByRole('button', { name: 'Clear Label artwork' })).toBeInTheDocument()
  })

  it('switches from an upload to a sample and back to none', async () => {
    const { onChange, user } = setup('', withSamples)
    await user.upload(
      screen.getByLabelText('Label artwork'),
      new File([HEART], 'heart.svg', { type: 'image/svg+xml' }),
    )
    await screen.findByText('heart.svg')

    await user.click(screen.getByRole('button', { name: 'Use sample sample-heart.svg' }))
    expect(onChange).toHaveBeenLastCalledWith('sample-heart.svg')
    expect(screen.queryByText('heart.svg')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Clear Label artwork' }))
    expect(onChange).toHaveBeenLastCalledWith('')
    expect(screen.getByText('Drop a file here')).toBeInTheDocument()
  })

  it('marks a default that is one of the samples as both', () => {
    setup('sample-heart.svg', { ...withSamples, initial: 'sample-heart.svg' })
    expect(screen.getByText('Model default · template sample')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Preview of sample-heart.svg' })).toBeInTheDocument()
  })

  it('reads samples at the revision being customized', () => {
    setup('sample-heart.svg', withSamples, 'abc1234')
    expect(screen.getByRole('img', { name: 'Preview of sample-heart.svg' })).toHaveAttribute(
      'src',
      api.sampleContentUrl(SLUG, 'sample-heart.svg', 'abc1234'),
    )
    expect(api.sampleContentUrl(SLUG, 'sample-heart.svg', 'abc1234')).toMatch(
      /\/samples\/sample-heart\.svg\?version=abc1234$/,
    )
  })
})

describe('mock API: file parameters (#204)', () => {
  it('refuses a render whose file value is not an upload', async () => {
    const error: unknown = await api
      .render(SLUG, { params: { label_art: '../../etc/passwd' } })
      .catch((caught: unknown) => caught)
    expect(error).toMatchObject({ status: 422 })
  })

  it('takes a render whose file value is a listed sample', async () => {
    const accepted = await api.render(SLUG, { params: { label_art: 'sample-star.png' } })
    expect(accepted.job_id).toBeTruthy()
  })

  it('refuses a sample name the template does not ship', async () => {
    const error: unknown = await api
      .render(SLUG, { params: { label_art: 'sample-gone.svg' } })
      .catch((caught: unknown) => caught)
    expect(error).toMatchObject({ status: 422 })
  })

  it('serves a listed sample and 404s anything else', async () => {
    const svg = await fetch(api.sampleContentUrl(SLUG, 'sample-heart.svg'))
    expect(svg.status).toBe(200)
    expect(svg.headers.get('content-type')).toBe('image/svg+xml')
    expect(await svg.text()).toContain('<svg')
    expect((await fetch(api.sampleContentUrl(SLUG, 'model.scad'))).status).toBe(404)
  })
})
