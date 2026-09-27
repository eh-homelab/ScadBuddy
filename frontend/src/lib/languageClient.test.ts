import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type Provider = Record<string, (...args: unknown[]) => unknown>

const providers: Record<string, Provider> = {}
const disposeProvider = vi.fn()

function register(name: string) {
  return vi.fn((_language: string, provider: Provider) => {
    providers[name] = provider
    return { dispose: disposeProvider }
  })
}

// The editor needs layout and workers jsdom does not have; what is under test is the
// protocol this module speaks and the providers it hands Monaco.
vi.mock('./monaco', () => ({
  OPENSCAD_LANGUAGE_ID: 'openscad',
  monaco: {
    languages: {
      registerCompletionItemProvider: register('completion'),
      registerHoverProvider: register('hover'),
      registerDefinitionProvider: register('definition'),
      registerDocumentFormattingEditProvider: register('formatting'),
    },
  },
}))

const { connectLanguageServer } = await import('./languageClient')

/** A method of a provider the client registered, failing the test if it did not. */
function call(name: string, method: string, ...args: unknown[]): unknown {
  const fn = providers[name]?.[method]
  if (!fn) throw new Error(`no ${name} provider registered`)
  return fn(...args)
}

interface Message {
  id?: number
  method?: string
  params?: Record<string, unknown>
  result?: unknown
}

class FakeSocket {
  static last: FakeSocket
  sent: Message[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  close = vi.fn()
  url: string

  constructor(url: string) {
    this.url = url
    FakeSocket.last = this
  }

  send(data: string) {
    this.sent.push(JSON.parse(data) as Message)
  }

  receive(message: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify({ jsonrpc: '2.0', ...message }) })
  }

  lastRequest(method: string): Message {
    const found = this.sent.filter((message) => message.method === method).at(-1)
    if (!found) throw new Error(`no ${method} sent`)
    return found
  }
}

const URI = 'file:///models/name-keychain/model.scad'

function fakeModel(uri = URI) {
  let changed: (() => void) | undefined
  let version = 1
  let text = 'cube(1);\ncu'
  const model = {
    uri: { toString: () => uri },
    getValue: () => text,
    getVersionId: () => version,
    getWordUntilPosition: () => ({ word: 'cu', startColumn: 1, endColumn: 3 }),
    onDidChangeContent: (listener: () => void) => {
      changed = listener
      return { dispose: () => (changed = undefined) }
    },
    type(next: string) {
      text = next
      version += 1
      changed?.()
    },
  }
  return model
}

const CAPABILITIES = {
  completionProvider: {},
  hoverProvider: true,
  definitionProvider: true,
  documentFormattingProvider: true,
}

async function started(model = fakeModel(), capabilities: object = CAPABILITIES) {
  const client = connectLanguageServer(model as never, '/api/v1/models/name-keychain/lsp')
  const socket = FakeSocket.last
  socket.onopen?.()
  socket.receive({ id: socket.lastRequest('initialize').id, result: { capabilities } })
  await vi.waitFor(() => socket.lastRequest('textDocument/didOpen'))
  return { client, socket, model }
}

describe('connectLanguageServer', () => {
  beforeEach(() => {
    vi.stubGlobal('WebSocket', FakeSocket)
    for (const key of Object.keys(providers)) delete providers[key]
    disposeProvider.mockClear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('opens the socket on the page origin and names the model directory as its root', () => {
    connectLanguageServer(fakeModel() as never, '/api/v1/models/name-keychain/lsp')
    const socket = FakeSocket.last
    expect(socket.url).toBe('ws://localhost:3000/api/v1/models/name-keychain/lsp')

    socket.onopen?.()
    expect(socket.lastRequest('initialize').params).toMatchObject({
      rootUri: 'file:///models/name-keychain/',
      workspaceFolders: [{ uri: 'file:///models/name-keychain/', name: 'model' }],
    })
  })

  it('opens the document once initialized, then sends every edit whole', async () => {
    const { socket, model } = await started()
    expect(socket.sent.map((message) => message.method)).toEqual([
      'initialize',
      'initialized',
      'textDocument/didOpen',
    ])
    expect(socket.lastRequest('textDocument/didOpen').params).toEqual({
      textDocument: { uri: URI, languageId: 'openscad', version: 1, text: 'cube(1);\ncu' },
    })

    model.type('cube(2);')
    expect(socket.lastRequest('textDocument/didChange').params).toEqual({
      textDocument: { uri: URI, version: 2 },
      contentChanges: [{ text: 'cube(2);' }],
    })
  })

  it('registers only what the server says it can do', async () => {
    await started(fakeModel(), { hoverProvider: true })
    expect(Object.keys(providers)).toEqual(['hover'])
  })

  it('completes from the server, over the word under the cursor', async () => {
    const { socket, model } = await started()
    const pending = call('completion', 'provideCompletionItems', model, {
      lineNumber: 2,
      column: 3,
    }) as Promise<{ suggestions: { label: string; range: object }[] }>

    const request = socket.lastRequest('textDocument/completion')
    expect(request.params).toEqual({ textDocument: { uri: URI }, position: { line: 1, character: 2 } })
    socket.receive({ id: request.id, result: { isIncomplete: true, items: [{ label: 'cube' }] } })

    const { suggestions } = await pending
    expect(suggestions.map((item) => item.label)).toEqual(['cube'])
    expect(suggestions[0]?.range).toEqual({
      startLineNumber: 2,
      startColumn: 1,
      endLineNumber: 2,
      endColumn: 3,
    })
  })

  it('leaves models other than its own alone', async () => {
    const { socket } = await started()
    const sent = socket.sent.length
    expect(
      call('hover', 'provideHover', fakeModel('file:///models/other/model.scad'), {
        lineNumber: 1,
        column: 1,
      }),
    ).toBeUndefined()
    expect(socket.sent).toHaveLength(sent)
  })

  it('jumps to definitions in this file only', async () => {
    const { socket, model } = await started()
    const pending = call('definition', 'provideDefinition', model, { lineNumber: 1, column: 1 }) as Promise<
      { uri: { toString(): string } }[]
    >
    const range = { start: { line: 0, character: 0 }, end: { line: 0, character: 4 } }
    socket.receive({
      id: socket.lastRequest('textDocument/definition').id,
      result: [
        { uri: URI, range },
        { uri: 'file:///models/name-keychain/helper.scad', range },
      ],
    })

    const locations = await pending
    expect(locations).toHaveLength(1)
    expect(locations[0]?.uri).toBe(model.uri)
  })

  it('answers what the server asks of it, so the server is never left waiting', async () => {
    const { socket } = await started()
    socket.receive({ id: 'config-1', method: 'workspace/configuration', params: { items: [] } })
    expect(socket.sent.at(-1)).toEqual({ jsonrpc: '2.0', id: 'config-1', result: null })
  })

  it('settles open requests with nothing when the socket goes', async () => {
    const { socket, model } = await started()
    const pending = call('hover', 'provideHover', model, { lineNumber: 1, column: 1 })
    socket.onclose?.()
    await expect(pending).resolves.toBeUndefined()
  })

  it('closes the socket and drops its providers when disposed', async () => {
    const { client, socket } = await started()
    client.dispose()
    expect(socket.close).toHaveBeenCalled()
    expect(disposeProvider).toHaveBeenCalledTimes(4)
  })

  it('registers nothing if disposed before the server answered', async () => {
    const client = connectLanguageServer(fakeModel() as never, '/api/v1/lsp')
    const socket = FakeSocket.last
    socket.onopen?.()
    client.dispose()
    socket.receive({ id: socket.lastRequest('initialize').id, result: { capabilities: CAPABILITIES } })
    await Promise.resolve()
    expect(Object.keys(providers)).toEqual([])
    expect(socket.sent.map((message) => message.method)).toEqual(['initialize'])
  })
})
