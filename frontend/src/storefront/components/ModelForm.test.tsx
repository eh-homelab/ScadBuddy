import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ModelForm } from './ModelForm'

const BUCKET = 'https://bucket.example.test/staging'

function slotsFor(body: { files: { filename: string }[] }) {
  return {
    uploads: body.files.map((file, index) => ({
      id: `${index}`.padStart(32, '0'),
      filename: file.filename,
      url: `${BUCKET}/${index}?X-Amz-Signature=s`,
      headers: { 'Content-Type': 'model/stl', 'x-amz-acl': 'private' },
      expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
    })),
  }
}

/** The bucket's CORS rule (spec §6.4): jsdom sends the preflight a browser would. */
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'PUT',
  'Access-Control-Allow-Headers': 'content-type, x-amz-acl',
}
const preflight = http.options(`${BUCKET}/:id`, () => new HttpResponse(null, { status: 204, headers: CORS }))

const EMPTY_DRAFT = { id: 'd1', currency: 'USD', minor_exponent: 2, items: [], shipping_options: [] }

describe('ModelForm', () => {
  it('uploads each file to its slot, then sends the request naming the slots', async () => {
    const puts: { url: string; type: string | null; acl: string | null }[] = []
    let sent: Record<string, unknown> | null = null
    server.use(
      http.post('/api/store/v1/uploads', async ({ request }) => HttpResponse.json(slotsFor((await request.json()) as never))),
      http.put(`${BUCKET}/:id`, ({ request }) => {
        puts.push({ url: request.url, type: request.headers.get('Content-Type'), acl: request.headers.get('x-amz-acl') })
        return new HttpResponse(null, { status: 200, headers: CORS })
      }),
      preflight,
      http.post('/api/store/v1/draft/models', async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(EMPTY_DRAFT)
      }),
    )
    const { user } = renderPage(<ModelForm />)
    const files = [new File(['solid a'], 'base.stl', { type: '' }), new File(['solid b'], 'lid.stl', { type: '' })]
    await user.upload(await screen.findByLabelText('Model files'), files)
    await user.type(screen.getByLabelText('Link to a model'), 'https://www.printables.com/model/1-cube')
    await user.click(screen.getByRole('button', { name: 'Add link' }))
    await user.clear(screen.getByLabelText('How many'))
    await user.type(screen.getByLabelText('How many'), '2')
    await user.type(screen.getByLabelText('Anything else'), 'Two of the base, one of the lid.')
    await user.click(screen.getByRole('button', { name: 'Add a thing to buy' }))
    await user.type(screen.getByLabelText('Link to buy 1'), 'https://parts.example.com/magnets')
    await user.type(screen.getByLabelText('Note for link 1'), '6 mm, 20 of them')
    await user.click(screen.getByRole('button', { name: 'Add to my order' }))
    expect(await screen.findByText('Your models are in your order.')).toBeInTheDocument()
    expect(puts).toHaveLength(2)
    expect(puts.every((put) => put.type === 'model/stl' && put.acl === 'private')).toBe(true)
    expect(sent).toEqual({
      uploads: ['0'.padStart(32, '0'), '1'.padStart(32, '0')],
      links: [
        { url: 'https://www.printables.com/model/1-cube', purpose: 'print', note: '' },
        { url: 'https://parts.example.com/magnets', purpose: 'purchase', note: '6 mm, 20 of them' },
      ],
      quantity: 2,
      colours: { shop_choice: true, filaments: [] },
      note: 'Two of the base, one of the lid.',
    })
  })

  it('tries a failed upload again on the same URL, with no new slot', async () => {
    let slotCalls = 0
    let putCalls = 0
    const urls: string[] = []
    server.use(
      http.post('/api/store/v1/uploads', async ({ request }) => {
        slotCalls += 1
        return HttpResponse.json(slotsFor((await request.json()) as never))
      }),
      http.put(`${BUCKET}/:id`, ({ request }) => {
        putCalls += 1
        urls.push(request.url)
        return new HttpResponse(null, { status: putCalls === 1 ? 500 : 200, headers: CORS })
      }),
      preflight,
      http.post('/api/store/v1/draft/models', () => HttpResponse.json(EMPTY_DRAFT)),
    )
    const { user } = renderPage(<ModelForm />)
    await user.upload(await screen.findByLabelText('Model files'), [new File(['solid a'], 'base.stl')])
    await user.click(screen.getByRole('button', { name: 'Add to my order' }))
    const line = (await screen.findByText('The file did not upload. Try again.')).closest('li')!
    await user.click(within(line).getByRole('button', { name: 'Try base.stl again' }))
    await user.click(screen.getByRole('button', { name: 'Add to my order' }))
    expect(await screen.findByText('Your models are in your order.')).toBeInTheDocument()
    expect(slotCalls).toBe(1)
    expect(urls).toHaveLength(2)
    expect(urls[1]).toBe(urls[0])
  })

  it('refuses a file that is not an STL or a 3MF before sending', async () => {
    const { user } = renderPage(<ModelForm />, { userEventOptions: { applyAccept: false } })
    await user.upload(await screen.findByLabelText('Model files'), [new File(['x'], 'photo.png', { type: 'image/png' })])
    expect(await screen.findByText('Add only .stl or .3mf files.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add to my order' })).toBeDisabled()
  })

  it('asks for at least one model', async () => {
    const { user } = renderPage(<ModelForm />)
    await user.click(await screen.findByRole('button', { name: 'Add to my order' }))
    expect(await screen.findByText('Add a model file or a link to a model.')).toBeInTheDocument()
  })

  it('offers the shop’s colours, at most 8', async () => {
    const filaments = Array.from({ length: 10 }, (_, i) => ({
      id: `f${i}`,
      colour: '#112233',
      colour_name: `Colour ${i}`,
      material: 'PLA',
    }))
    let sent: { colours?: unknown } | null = null
    server.use(
      http.get('/api/store/v1/models/colours', () => HttpResponse.json({ filaments })),
      http.post('/api/store/v1/draft/models', async ({ request }) => {
        sent = (await request.json()) as { colours?: unknown }
        return HttpResponse.json(EMPTY_DRAFT)
      }),
    )
    const { user } = renderPage(<ModelForm />)
    await user.click(await screen.findByRole('radio', { name: 'Choose colours' }))
    for (let i = 0; i < 8; i += 1) await user.click(await screen.findByRole('checkbox', { name: `Colour ${i} PLA` }))
    expect(screen.getByRole('checkbox', { name: 'Colour 8 PLA' })).toBeDisabled()
    await user.type(screen.getByLabelText('Link to a model'), 'https://makerworld.com/en/models/1')
    await user.click(screen.getByRole('button', { name: 'Add link' }))
    await user.click(screen.getByRole('button', { name: 'Add to my order' }))
    await waitFor(() => expect(sent).not.toBeNull())
    expect(sent!.colours).toEqual({ shop_choice: false, filaments: ['f0', 'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7'] })
  })

  it('shows a link as text and refuses a link that is not http or https', async () => {
    const { user } = renderPage(<ModelForm />)
    await user.type(await screen.findByLabelText('Link to a model'), 'javascript:alert(1)')
    await user.click(screen.getByRole('button', { name: 'Add link' }))
    expect(await screen.findByText('Give a link that starts with https:// or http://.')).toBeInTheDocument()
    await user.clear(screen.getByLabelText('Link to a model'))
    await user.type(screen.getByLabelText('Link to a model'), 'https://makerworld.com/en/models/1')
    await user.click(screen.getByRole('button', { name: 'Add link' }))
    expect(await screen.findByText('https://makerworld.com/en/models/1')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'https://makerworld.com/en/models/1' })).toBeNull()
  })
})
