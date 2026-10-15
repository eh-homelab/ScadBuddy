import { render, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FontFamily, Param } from '../../api/types'
import { forgetGlyphAnswers } from '../../lib/useMissingGlyphs'
import { server } from '../../mocks/server'
import { FontWidget } from './FontWidget'

// #2358 — OpenSCAD's text() leaves out a character the font has no glyph for, without a
// word; the browser draws it in a fallback font, so the field looked fine. The mock's
// fonts lack everything above U+2FFF (mocks/features/fontGlyphs.ts).

const param: Param = { group: 'Main', name: 'font', type: 'font', initial: 'Lobster Two:style=Bold', caption: 'Typeface' }
const fonts: FontFamily[] = [{ family: 'Lobster Two', styles: ['Bold'] }]

function show(props: Partial<Parameters<typeof FontWidget>[0]> = {}) {
  return render(
    <FontWidget param={param} value="Lobster Two:style=Bold" fonts={fonts} onChange={vi.fn()} {...props} />,
  )
}

beforeEach(() => forgetGlyphAnswers())

describe('FontWidget glyph coverage (#2358)', () => {
  it('names the characters of the text the font has no glyph for', async () => {
    show({ sampleText: 'Zoë 😀 "q" \\ <b> 漢' })
    const note = await screen.findByTestId('font-lacks')
    expect(note).toHaveTextContent('Lobster Two has no glyph for 😀 漢; they are left out of the print.')
    expect(screen.getByRole('combobox', { name: 'Typeface' })).toHaveAccessibleDescription(note.textContent ?? '')
  })

  it('says nothing when every character has a glyph', async () => {
    const asked: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/fonts/glyphs')) asked.push(request.url)
    })
    show({ sampleText: 'Zoë' })
    await waitFor(() => expect(asked).toHaveLength(1))
    expect(screen.queryByTestId('font-lacks')).not.toBeInTheDocument()
  })

  it('asks nothing for the default font, an empty text, or a family that is not installed', async () => {
    const asked: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/fonts/glyphs')) asked.push(request.url)
    })
    const { rerender } = show({ value: '', sampleText: '😀' })
    rerender(<FontWidget param={param} value="Lobster Two" fonts={fonts} sampleText="  " onChange={vi.fn()} />)
    rerender(
      <FontWidget param={param} value="Pacifico" fonts={fonts} sampleText="😀" missing={['Pacifico']} onChange={vi.fn()} />,
    )
    await new Promise((resolve) => setTimeout(resolve, 600))
    expect(asked).toEqual([])
    expect(screen.queryByTestId('font-lacks')).not.toBeInTheDocument()
  })

  it('says nothing when the server cannot answer', async () => {
    server.use(http.get('/api/v1/fonts/glyphs', () => HttpResponse.json({ title: 'Error', status: 500 }, { status: 500 })))
    show({ sampleText: '😀' })
    await new Promise((resolve) => setTimeout(resolve, 600))
    expect(screen.queryByTestId('font-lacks')).not.toBeInTheDocument()
  })
})
