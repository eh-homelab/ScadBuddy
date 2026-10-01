import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BUILTIN_SLUG, GALLERY_SLUG, media } from '../mocks/fixtures'
import { server } from '../mocks/server'
import {
  ApiError,
  BAMBUDDY_UNAVAILABLE,
  UNANSWERED,
  api,
  mayHaveRun,
  newRequestId,
  printRunPoll,
} from './client'
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

  it('rejects with what the status means when the answer is not a problem', async () => {
    const { pending, xhr } = upload()
    xhr.status = 502
    xhr.statusText = 'Bad Gateway'
    xhr.responseText = '<html>'
    xhr.onload?.()
    await expect(pending).rejects.toMatchObject({
      status: 502,
      detail: 'The server is not answering right now (HTTP 502).',
      problem: { title: 'Bad Gateway' },
    })
  })

  it('rejects when the connection fails', async () => {
    const { pending, xhr } = upload()
    xhr.onerror?.()
    await expect(pending).rejects.toThrow('The upload failed')
    // Like a dropped fetch: no answer arrived, so the upload may have landed.
    expect(mayHaveRun(await pending.catch((caught: unknown) => caught))).toBe(true)
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
    printRunPoll.reattempts = 3
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
            type: 'https://scadbuddy.dev/problems/bambuddy-unavailable',
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
    // The dialog adds the queue advice; the detail is the backend's own.
    expect((error as ApiError).detail).toBe('Bambuddy did not answer in time.')
    expect((error as ApiError).problem).toMatchObject({
      type: 'https://scadbuddy.dev/problems/bambuddy-unavailable',
      status: 504,
      may_have_queued: true,
    })
    expect(mayHaveRun(error)).toBe(true)
  })

  it('does not count a failed run that never tried to queue, whatever its type', async () => {
    printRunPoll.intervalMs = 1
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () =>
        HttpResponse.json({
          ...started,
          status: 'failed',
          may_have_queued: false,
          error: {
            type: BAMBUDDY_UNAVAILABLE,
            status: 504,
            title: 'Gateway Timeout',
            detail: 'could not reach Bambuddy to slice the plate: ReadTimeout',
            extensions: {},
          },
        }),
      ),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect((error as ApiError).status).toBe(504)
    expect(mayHaveRun(error)).toBe(false)
  })

  it('counts a run lost while queueing as maybe queued, from the flag', async () => {
    const lost =
      'ScadBuddy restarted while it was preparing this print, after it had started queueing it, ' +
      'so it cannot tell whether the print was queued.'
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () =>
        HttpResponse.json({
          ...started,
          status: 'failed',
          may_have_queued: true,
          repeated: true,
          error: { status: 500, title: 'Internal Server Error', detail: lost, extensions: {} },
        }),
      ),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect((error as ApiError).detail).toBe(lost)
    expect(mayHaveRun(error)).toBe(true)
  })

  it('re-sends the same request when its answer never arrived, and re-attaches to the run', async () => {
    printRunPoll.intervalMs = 1
    const result = { queue_item_ids: [7], warnings: [] }
    const sent: unknown[] = []
    let reads = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', async ({ request }) => {
        sent.push(await request.json())
        return sent.length === 1 ? HttpResponse.error() : HttpResponse.json(started, { status: 202 })
      }),
      http.get('/api/v1/print/runs/run-1', () => {
        reads += 1
        // A proxy's own 504 page: not ScadBuddy's answer, so read again.
        if (reads === 1) return new HttpResponse('<html>504</html>', { status: 504 })
        return HttpResponse.json({ ...started, status: 'succeeded', result })
      }),
    )

    const press = { ...body, request_id: 'press-1' }
    await expect(api.runPrint('out-1', press)).resolves.toEqual(result)
    expect(sent).toEqual([press, press])
    expect(reads).toBe(2)
  })

  it("does not re-send a request ScadBuddy's server refused", async () => {
    printRunPoll.intervalMs = 1
    let posts = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => {
        posts += 1
        return HttpResponse.json(
          { type: 'about:blank', title: 'Bad Gateway', status: 502, detail: 'Bambuddy refused the API key' },
          { status: 502, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect((error as ApiError).detail).toBe('Bambuddy refused the API key')
    expect(posts).toBe(1)
  })

  it('stops reading the run once its signal is aborted', async () => {
    printRunPoll.intervalMs = 5
    const controller = new AbortController()
    let reads = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => HttpResponse.json(started, { status: 202 })),
      http.get('/api/v1/print/runs/run-1', () => {
        reads += 1
        if (reads === 2) controller.abort()
        return HttpResponse.json(started)
      }),
    )

    const error = await api
      .runPrint('out-1', body, controller.signal)
      .catch((caught: unknown) => caught)
    expect(controller.signal.aborted).toBe(true)
    expect(error).toBe(controller.signal.reason)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(reads).toBe(2)
  })

  it('gives up re-attaching after a few unanswered tries', async () => {
    printRunPoll.intervalMs = 1
    printRunPoll.reattempts = 2
    let posts = 0
    server.use(
      http.post('/api/v1/print/outputs/out-1/run', () => {
        posts += 1
        return HttpResponse.error()
      }),
    )

    const error = await api.runPrint('out-1', body).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).problem).toMatchObject({ type: UNANSWERED, status: 0 })
    expect(mayHaveRun(error)).toBe(true)
    expect(posts).toBe(3)
  })
})

describe('newRequestId', () => {
  it('is a new 128-bit hex id each time', () => {
    const one = newRequestId()
    expect(one).toMatch(/^[0-9a-f]{32}$/)
    expect(newRequestId()).not.toBe(one)
  })
})

describe('failures the server did not describe (#470)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  /** A proxy in front of the backend answers with its own page, and HTTP/2 has no status text. */
  function proxy(status: number, body = '<html><body>upstream request timeout</body></html>') {
    server.use(
      http.get(
        '/api/v1/models',
        () => new HttpResponse(body, { status, headers: { 'Content-Type': 'text/html' } }),
      ),
    )
  }

  async function failure(): Promise<ApiError> {
    const caught = await api.listModels().catch((cause: unknown) => cause)
    expect(caught).toBeInstanceOf(ApiError)
    return caught as ApiError
  }

  it.each([
    [504, 'The server took too long to answer (HTTP 504).'],
    [524, 'The server took too long to answer (HTTP 524).'],
    [502, 'The server is not answering right now (HTTP 502).'],
    [503, 'The server is not answering right now (HTTP 503).'],
    [413, 'That is too large for the server to accept (HTTP 413).'],
    [500, 'The server hit an error it did not describe (HTTP 500).'],
    [404, 'The server refused the request without saying why (HTTP 404).'],
  ])('says what a non-JSON %i means, and keeps the status', async (status, detail) => {
    proxy(status)
    const error = await failure()
    expect(error.status).toBe(status)
    expect(error.detail).toBe(detail)
    expect(error.message).toBe(detail)
    expect(error.problem.title).not.toMatch(/^$|Request failed/)
  })

  it('treats JSON that is not a problem like a proxy page', async () => {
    proxy(504, '{"message":"timeout"}')
    expect((await failure()).detail).toBe('The server took too long to answer (HTTP 504).')
  })

  it('keeps a problem the backend wrote as it is', async () => {
    server.use(
      http.get('/api/v1/models', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Bad Gateway',
            status: 502,
            detail: 'Bambuddy did not answer in time.',
          },
          { status: 502, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const error = await failure()
    expect(error.detail).toBe('Bambuddy did not answer in time.')
    expect(error.problem.title).toBe('Bad Gateway')
    // Not the backend's "Bambuddy did not answer" type, so nothing says it may have run.
    expect(mayHaveRun(error)).toBe(false)
  })

  it.each([
    [504, 'could not reach Bambuddy to queue the print: ReadTimeout', true],
    [502, 'could not reach Bambuddy to queue the print: RemoteProtocolError', true],
    [409, 'Bambuddy refused the API key', false],
    // map_response's fallback: Bambuddy answered, so its "no" is not a maybe...
    [502, 'Bambuddy answered 500 when asked to queue the print', false, 500],
    [502, 'Bambuddy answered 400 when asked to queue the print', false, 400],
    // ...unless the answer is a proxy's in front of Bambuddy that gave up waiting.
    [502, 'Bambuddy answered 504 when asked to queue the print', true, 504],
    [502, 'Bambuddy answered 524 when asked to queue the print', true, 524],
  ])(
    'counts the backend’s bambuddy-unavailable %i as maybe having run: %s',
    async (status, detail, expected, bambuddy_status?: number) => {
      server.use(
        http.get('/api/v1/models', () =>
          HttpResponse.json(
            {
              type: BAMBUDDY_UNAVAILABLE,
              title: 'Bad Gateway',
              status,
              detail,
              ...(bambuddy_status === undefined ? {} : { bambuddy_status }),
            },
            { status, headers: { 'Content-Type': 'application/problem+json' } },
          ),
        ),
      )
      const error = await failure()
      expect(error.detail).toBe(detail)
      expect(mayHaveRun(error)).toBe(expected)
    },
  )

  it('keeps a detail-only body', async () => {
    server.use(
      http.get('/api/v1/models', () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })),
    )
    expect((await failure()).detail).toBe('Not Found')
  })

  it('says the server could not be reached when the connection fails', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.error()))
    const error = await failure()
    expect(error.status).toBe(0)
    expect(error.detail).toBe(
      'ScadBuddy could not reach its server, or the connection dropped before it answered.',
    )
    expect(mayHaveRun(error)).toBe(true)
  })

  it('says the browser is offline when it is', async () => {
    vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
    server.use(http.get('/api/v1/models', () => HttpResponse.error()))
    const error = await failure()
    expect(error.status).toBe(0)
    expect(error.detail).toBe('This browser is offline, so ScadBuddy could not reach its server.')
    expect(mayHaveRun(error)).toBe(false)
  })

  it('does not call it offline when the browser went offline only while waiting', async () => {
    let online = true
    vi.spyOn(navigator, 'onLine', 'get').mockImplementation(() => online)
    server.use(
      http.get('/api/v1/models', () => {
        online = false
        return HttpResponse.error()
      }),
    )
    const error = await failure()
    expect(error.problem.type).not.toBe('urn:scadbuddy:offline')
    expect(mayHaveRun(error)).toBe(true)
  })

  it('passes an abort through as it is', async () => {
    const controller = new AbortController()
    controller.abort()
    const caught = await api
      .checkSource('cube(1);', undefined, controller.signal)
      .catch((cause: unknown) => cause)
    expect(caught).not.toBeInstanceOf(ApiError)
    expect((caught as Error).name).toBe('AbortError')
  })

  it.each([
    [502, true],
    [504, true],
    [524, true],
    [503, false],
    [413, false],
    [500, false],
  ])('counts a non-JSON %i as maybe having run: %s', async (status, expected) => {
    proxy(status)
    expect(mayHaveRun(await failure())).toBe(expected)
  })

  it('does not count an error the backend described, or one that is not an ApiError', () => {
    expect(mayHaveRun(new Error('boom'))).toBe(false)
    expect(mayHaveRun(new ApiError(504, 'Bambuddy did not answer in time.'))).toBe(false)
  })

  it('says what a non-JSON upload failure means', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    const pending = api.uploadMedia(BUILTIN_SLUG, new File([new Uint8Array([1])], 'clip.mp4'))
    const xhr = FakeXhr.last!
    xhr.status = 413
    xhr.responseText = '<html>'
    xhr.onload?.()
    await expect(pending).rejects.toMatchObject({
      status: 413,
      detail: 'That is too large for the server to accept (HTTP 413).',
    })
  })
})

describe('definition files (#185)', () => {
  it('reads a file beside the model and one in a pinned library', async () => {
    await expect(api.getDefinitionFile('name-keychain', { path: 'helper.scad' })).resolves.toContain(
      'module rounded_plate',
    )
    await expect(
      api.getDefinitionFile('name-keychain', { library: 'BOSL2', path: 'shapes3d.scad' }),
    ).resolves.toContain('module cuboid')
  })

  it('encodes each segment of the path, and keeps its slashes', async () => {
    let asked = ''
    server.use(
      http.get('/api/v1/models/:slug/files/*', ({ request }) => {
        asked = new URL(request.url).pathname
        return new HttpResponse('x', { headers: { 'Content-Type': 'text/plain' } })
      }),
    )
    await api.getDefinitionFile('builtin:keychain', { path: 'my parts/a#b.scad' })
    expect(asked).toBe('/api/v1/models/builtin%3Akeychain/files/my%20parts/a%23b.scad')
  })

  it('names the pinned commit a library file is from', async () => {
    let asked = ''
    server.use(
      http.get('/api/v1/models/:slug/libraries/:name/files/*', ({ request }) => {
        const url = new URL(request.url)
        asked = url.pathname + url.search
        return new HttpResponse('x', { headers: { 'Content-Type': 'text/plain' } })
      }),
    )
    await api.getDefinitionFile('name-keychain', { library: 'BOSL2', commit: 'ab12', path: 'std.scad' })
    expect(asked).toBe('/api/v1/models/name-keychain/libraries/BOSL2/files/std.scad?commit=ab12')
  })

  it('is an ApiError for a file that is not there', async () => {
    await expect(api.getDefinitionFile('name-keychain', { path: 'nope.scad' })).rejects.toMatchObject({
      status: 404,
    })
  })
})

describe('render and createOutput (spec 2026-09-27 §4.3)', () => {
  it('send inputs, not params', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/models/:slug/render', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { job_id: 'a'.repeat(32), status_url: '/api/v1/jobs/' + 'a'.repeat(32) },
          { status: 202 },
        )
      }),
      http.post('/api/v1/models/:slug/outputs', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({}, { status: 201 })
      }),
    )
    await api.render('name-keychain', { params: { name: 'Hi' }, tab: 'lid' })
    await api.createOutput('name-keychain', 'a'.repeat(32), undefined, { params: {}, tab: 'lid' })
    await api.createOutput('name-keychain', 'a'.repeat(32))
    expect(bodies).toEqual([
      { inputs: { params: { name: 'Hi' }, tab: 'lid' }, version: null },
      { job_id: 'a'.repeat(32), name: null, inputs: { params: {}, tab: 'lid' } },
      { job_id: 'a'.repeat(32), name: null },
    ])
  })

  it('addresses a template UI file, pinned by revision when there is one', () => {
    expect(api.uiFileUrl(BUILTIN_SLUG, undefined, 'app/index.html')).toBe(
      '/api/v1/models/builtin%3Akeychain-template/ui/app/index.html',
    )
    expect(api.uiFileUrl('name-keychain', 'abc', 'a b.js')).toBe(
      '/api/v1/models/name-keychain/versions/abc/ui/a%20b.js',
    )
  })
})
