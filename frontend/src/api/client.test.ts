import { afterEach, describe, expect, it, vi } from 'vitest'
import { BUILTIN_SLUG, GALLERY_SLUG, media } from '../mocks/fixtures'
import { http, HttpResponse } from 'msw'
import { server } from '../mocks/server'
import { ApiError, MAY_HAVE_QUEUED, api, printRunPoll } from './client'
import type { MediaView } from './types'

const video = media[GALLERY_SLUG]!.find((item) => item.kind === 'video')!
const picture = media[GALLERY_SLUG]![0]!

describe('media URLs (#274)', () => {
  it('addresses an item by its id, with the slug encoded', () => {
    expect(api.mediaUrl(BUILTIN_SLUG, picture)).toBe(
      `/api/v1/models/builtin%3Akeychain-template/media/${picture.id}`,
    )
  })

  it('gives a video its poster URL, and an item with no poster none', () => {
    expect(api.mediaPosterUrl('name-keychain', video)).toBe(
      `/api/v1/models/name-keychain/media/${video.id}/poster`,
    )
    expect(api.mediaPosterUrl('name-keychain', picture)).toBeUndefined()
  })
})

/** Just enough of an `XMLHttpRequest` to see what `uploadMedia` sends and to answer it. */
class FakeXhr {
  static last: FakeXhr | undefined
  method = ''
  url = ''
  body: FormData | undefined
  headers: Record<string, string> = {}
  status = 0
  statusText = ''
  responseText = ''
  readonly upload = new EventTarget()
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null

  constructor() {
    FakeXhr.last = this
  }
  open(method: string, url: string) {
    this.method = method
    this.url = url
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value
  }
  send(body: FormData) {
    this.body = body
  }
  progress(loaded: number, total: number) {
    this.upload.dispatchEvent(
      Object.assign(new Event('progress'), { lengthComputable: true, loaded, total }),
    )
  }
  answer(status: number, body: unknown, statusText = '') {
    this.status = status
    this.statusText = statusText
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
}

describe('uploadMedia (#274)', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    FakeXhr.last = undefined
  })

  function upload(
    options: { caption?: string; poster?: File } = {},
    onProgress?: (fraction: number) => void,
  ) {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    const file = new File([new Uint8Array([0x89, 0x50])], 'clip.mp4')
    const pending = api.uploadMedia(BUILTIN_SLUG, file, options, onProgress)
    return { pending, xhr: FakeXhr.last!, file }
  }

  it('posts the file, caption and poster as multipart, and reports progress', async () => {
    const poster = new File([new Uint8Array([1])], 'poster.png')
    const seen: number[] = []
    const { pending, xhr, file } = upload({ caption: 'A clip', poster }, (f) => seen.push(f))

    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('/api/v1/models/builtin%3Akeychain-template/media')
    expect(xhr.headers['Accept']).toBe('application/json')
    expect((xhr.body!.get('file') as File).name).toBe(file.name)
    expect(xhr.body!.get('caption')).toBe('A clip')
    expect((xhr.body!.get('poster') as File).name).toBe('poster.png')

    xhr.progress(25, 100)
    xhr.progress(100, 100)
    expect(seen).toEqual([0.25, 1])

    const record = { slug: 'x', media: [] as MediaView[] }
    xhr.answer(200, record)
    await expect(pending).resolves.toEqual(record)
  })

  it('leaves out the parts that were not given', () => {
    const { xhr } = upload()
    expect(xhr.body!.has('caption')).toBe(false)
    expect(xhr.body!.has('poster')).toBe(false)
  })

  it('rejects with the problem the server answered', async () => {
    const { pending, xhr } = upload()
    xhr.answer(413, {
      title: 'Content Too Large',
      status: 413,
      detail: 'a media upload is at most 1024 MB (SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES), and this one is larger',
    })
    const error = await pending.catch((cause: unknown) => cause)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(413)
    expect((error as ApiError).detail).toContain('at most 1024 MB')
  })

  it('rejects with the status text when the answer is not a problem', async () => {
    const { pending, xhr } = upload()
    xhr.status = 502
    xhr.statusText = 'Bad Gateway'
    xhr.responseText = '<html>'
    xhr.onload?.()
    await expect(pending).rejects.toMatchObject({ status: 502, detail: 'Bad Gateway' })
  })

  it('rejects when the connection fails', async () => {
    const { pending, xhr } = upload()
    xhr.onerror?.()
    await expect(pending).rejects.toThrow('The upload failed')
  })
})

describe('media writes against the mock API (#274)', () => {
  it('captions, reorders and deletes, each answering the model', async () => {
    const [first, second, third, fourth] = media[GALLERY_SLUG]!.map((item) => item.id)
    const copy = await api.duplicateModel(GALLERY_SLUG, 'Gallery')

    const captioned = await api.patchMedia(copy.slug, first!, 'The cover')
    expect(captioned.media?.[0]?.caption).toBe('The cover')

    const reordered = await api.reorderMedia(copy.slug, [fourth!, third!, second!, first!])
    expect(reordered.media?.map((item) => item.id)).toEqual([fourth, third, second, first])

    const deleted = await api.deleteMedia(copy.slug, third!)
    expect(deleted.media?.map((item) => item.id)).toEqual([fourth, second, first])
  })
})

describe('runPrint follows the run the server answers with 202 (#470)', () => {
  const body = { choices: { nozzles: [], tier: 'standard' } } as unknown as Parameters<
    typeof api.runPrint
  >[1]
  const started = {
    id: 'run-1',
    output_id: 'out-1',
    status: 'running',
    created_at: '2026-09-28T10:00:00Z',
    finished_at: null,
    result: null,
    error: null,
  }

  afterEach(() => {
    server.resetHandlers()
    printRunPoll.intervalMs = 1000
  })

  it('reads the run until it succeeds and returns its result', async () => {
    printRunPoll.intervalMs = 1
    const result = { queue_item_ids: [7], warnings: [] }
    let reads = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () => {
        reads += 1
        return HttpResponse.json(
          reads < 2 ? started : { ...started, status: 'succeeded', result },
        )
      }),
    )

    await expect(api.runPrint('out-1', body)).resolves.toEqual(result)
    expect(reads).toBe(2)
  })

  it("throws the failed run's problem, as the route used to answer it", async () => {
    printRunPoll.intervalMs = 1
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () =>
        HttpResponse.json({
          ...started,
          status: 'failed',
          error: {
            status: 502,
            title: 'Bad Gateway',
            detail: 'Bambuddy failed to slice the plate: no support',
            extensions: { slice_job_id: 9 },
          },
        }),
      ),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(502)
    expect((error as ApiError).message).toBe('Bambuddy failed to slice the plate: no support')
    expect((error as ApiError).problem).toMatchObject({ slice_job_id: 9 })
  })

  it('says a failed run that had tried to queue may be on the queue anyway', async () => {
    printRunPoll.intervalMs = 1
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () =>
        HttpResponse.json({
          ...started,
          status: 'failed',
          may_have_queued: true,
          error: {
            status: 504,
            title: 'Gateway Timeout',
            detail: 'Bambuddy did not answer in time.',
            extensions: {},
          },
        }),
      ),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(504)
    expect((error as ApiError).detail).toBe(`Bambuddy did not answer in time. ${MAY_HAVE_QUEUED}`)
    expect((error as ApiError).problem).toMatchObject({ may_have_queued: true })
  })
})
