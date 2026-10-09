/**
 * #782 — the agent's `GET /api/v1/ai/sessions/:id/blobs/:name`, which serves the images
 * a session's tool results carried (agent `src/routes/sessions.ts`). The scripted agent
 * (`../agent.ts`) returns one, a render view of the name keychain, in its first turn;
 * any other name is a 404, as the real route answers a name the session never stored.
 * It also serves the images the user sent (#1891's full-size view), which the attachment
 * mock stores here as a turn takes them (`storeSentImage`).
 */
import { HttpResponse, http } from 'msw'
import type { ToolImage } from '../../agent/chat/protocol'

/** A 192×120 PNG: an orange keychain plate with raised letters. */
const PREVIEW_PNG =
  'iVBORw0KGgoAAAANSUhEUgAAAMAAAAB4CAIAAAArJ2pIAAADGElEQVR42u3dTW9NURSH8X6P3vdXnwVVVaWqqlRVFR8JIYQQhBCCEEIIQghBCCHEQAwMjUx3zu3evfecZt//yn52frOddQbrPLnDe0Z+//kL5DbCCkBAICAQEAgIICAQEAgIBAQQEAgIBAQCAggIBAQCAgEBBAQCAgGBgEBAAAGBgEBAICCAgEBAICAQEEBAICAQEAgoh1/HNiAy8wH9PNqFCGMB/TjShSAbAX1f6UKWekDfVroQJx3Q18MdiNMN6MtyByaIBvR5uQMTRAP6dKgDE0QD+rjUhgmKAX1YasMQuYDeH2zDELmA3i22YYhcQG8XWzBELqA3B1owRC6g1wstGCIX0KuFVmT/nBO+9Z10pnrJBfRyfzMyd2XhW99JZ6qXXEAv9jUjc1cWvvWddKZ6yQX0fL4Zmbuy8K3vpDPVSy6gZ/PNgeQYyXBXFr71nXSmVt2/VkBP9zb6UWQ2w11Z+NZ30pnq80UMM6Anc401FRzPcFcWvvWddKYGehfDCejxXCOs+BMy3JWFb30nnakcryN2QI/2NMKKPwHrSC6gh7P1sOJPwDqSC+jBbD1gXR6S4f5oh299J52p3G8kXkD3d9fDij8hw11Z+NZ30pnK8TpiB3Rvph5W/AkZ7srCt76TzlSO1xE7oLsztbDiT8hwVxa+9Z10pnK8jtgB3dlVW1PB8Qx3ZeFb30lnaqB3MZyAbk/X+lFkNsNdWfjWd9KZ6vNFDDOgW9O1geQYyXBXFr71nXSmVt2/VkA3d1Yjc1cWvvWddKZ6yQV0Y0cVhsgFdH2qCkPkAro2VYUhcgFd3V6BIXIBXZmswBC5gC5PVmCIXECXtlVgiFxAFyfKMETxD6YuTJRhgug/lJ3fWoYJogGdGy/DBNGAzo6XYILuH42f2VKCOOl/qj89VoI49Y+tnBorQZaNzz2d3DwKQcY+OHdi0yhEmP/k5fGNo4iMj+6Cj+6CgAACAgGBgEBAAAGBgEBAICAQECsAAYGAQEAgIICAQEAgIBAQQEAgIBAQCAggIBAQCAgEBBAQCAgEBAICAQEEhNj+AyLbnjC+y6/RAAAAAElFTkSuQmCC'

/** The image the scripted first turn's render view returned, as its `tool.result` names it. */
export const PREVIEW_IMAGE: ToolImage = {
  name: '1ecb7926669dcc9928a1d464a609ea20f263d7d6c615deb4447d250ffdc3b156.png',
  mediaType: 'image/png',
}

const bytes = (base64: string) => Uint8Array.from(atob(base64), (c) => c.charCodeAt(0))

const EXTENSION: Record<string, string> = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp' }

/** The images users sent, by the name a `user.turn` gives them. */
const sent = new Map<string, { mediaType: string; data: string }>()

export function reset(): void {
  sent.clear()
}

/**
 * Keeps a sent image and returns the name it is served under. The real agent names it by
 * its bytes' sha256; any name of that shape does here.
 */
export function storeSentImage(image: { mediaType: string; data: string }): string {
  const name = `${(sent.size + 1).toString(16).padStart(64, '0')}.${EXTENSION[image.mediaType] ?? 'png'}`
  sent.set(name, image)
  return name
}

export const handlers = [
  http.get('/api/v1/ai/sessions/:id/blobs/:name', ({ params }) => {
    const image =
      params.name === PREVIEW_IMAGE.name ? { mediaType: 'image/png', data: PREVIEW_PNG } : sent.get(String(params.name))
    return image
      ? new HttpResponse(bytes(image.data), {
          headers: {
            'Content-Type': image.mediaType,
            'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': 'inline',
            'Cache-Control': 'private, max-age=31536000, immutable',
          },
        })
      : HttpResponse.json({ detail: `no image ${String(params.name)}` }, { status: 404 })
  }),
]
