import { HttpResponse, http } from 'msw'

/**
 * #2358 — `GET /fonts/glyphs`, the characters a font has no glyph for. The mock's
 * fonts have every glyph up to U+2FFF and none above it (no CJK, no emoji); the
 * default font, which names no family, is never asked about.
 */
export const handlers = [
  http.get('/api/v1/fonts/glyphs', ({ request }) => {
    const url = new URL(request.url)
    const font = url.searchParams.get('font') ?? ''
    const text = url.searchParams.get('text') ?? ''
    const missing: string[] = []
    for (const char of text) {
      const point = char.codePointAt(0) ?? 0
      const selector = point >= 0xfe00 && point <= 0xfe0f
      if (point > 0x2fff && !selector && !/\s/.test(char) && !missing.includes(char)) missing.push(char)
    }
    return HttpResponse.json({ font, missing })
  }),
]
