import type * as Monaco from 'monaco-editor/editor/editor.api'
import {
  directoryOf,
  socketUrl,
  toCompletion,
  toEdits,
  toHover,
  toLocations,
  type LspCompletionItem,
  type LspHover,
  type LspTextEdit,
} from './lsp'
import { OPENSCAD_LANGUAGE_ID, monaco } from './monaco'

interface ServerCapabilities {
  completionProvider?: object
  hoverProvider?: boolean | object
  definitionProvider?: boolean | object
  documentFormattingProvider?: boolean | object
}

type DefinitionResult = Parameters<typeof toLocations>[0]
type CompletionResult = LspCompletionItem[] | { isIncomplete: boolean; items: LspCompletionItem[] }

interface Incoming {
  id?: number | string
  method?: string
  result?: unknown
}

/**
 * openscad-lsp for one editor model, over the backend's WebSocket bridge
 * (backend/scadbuddy/library/lsp.py): completion, hover, go-to-definition and
 * formatting, for as long as the model is open.
 *
 * Deliberately small rather than `monaco-languageclient`: that package now runs on
 * the VS Code workbench services and replaces `monaco-editor` itself, which undoes
 * the trimmed `editor.api` build in `./monaco`. Monaco's own bundled LSP client has
 * no dispose and syncs every model in the page. Four requests and three
 * notifications are all this editor needs from the protocol.
 *
 * Any failure — no server installed, the session cap reached, the socket dropped —
 * leaves the editor as it was without one: requests in flight settle with nothing,
 * and nothing is retried.
 */
export function connectLanguageServer(
  model: Monaco.editor.ITextModel,
  path: string,
): Monaco.IDisposable {
  const uri = model.uri.toString()
  const socket = new WebSocket(socketUrl(path))
  const pending = new Map<number, (result: unknown) => void>()
  const disposables: Monaco.IDisposable[] = []
  let nextId = 1
  let disposed = false

  function send(message: object) {
    socket.send(JSON.stringify({ jsonrpc: '2.0', ...message }))
  }

  function request<T>(method: string, params: object): Promise<T | null> {
    const id = nextId++
    return new Promise((resolve) => {
      pending.set(id, (result) => resolve(result as T | null))
      send({ id, method, params })
    })
  }

  const document = { uri }
  const position = (at: Monaco.Position) => ({ line: at.lineNumber - 1, character: at.column - 1 })
  const ours = (target: Monaco.editor.ITextModel) => target.uri.toString() === uri

  socket.onmessage = (event: MessageEvent<string>) => {
    const message = JSON.parse(event.data) as Incoming
    if (message.id === undefined) return
    if (message.method !== undefined) {
      // `workspace/configuration`, `client/registerCapability`: nothing to add.
      send({ id: message.id, result: null })
      return
    }
    const settle = pending.get(message.id as number)
    pending.delete(message.id as number)
    settle?.(message.result ?? null)
  }

  socket.onclose = () => {
    for (const settle of pending.values()) settle(null)
    pending.clear()
  }

  socket.onopen = async () => {
    const root = directoryOf(uri)
    const initialized = await request<{ capabilities: ServerCapabilities }>('initialize', {
      processId: null,
      rootUri: root,
      workspaceFolders: [{ uri: root, name: 'model' }],
      capabilities: {
        textDocument: {
          completion: {
            completionItem: {
              snippetSupport: true,
              documentationFormat: ['markdown', 'plaintext'],
            },
          },
          hover: { contentFormat: ['markdown', 'plaintext'] },
        },
      },
    })
    if (disposed || !initialized) return
    send({ method: 'initialized', params: {} })
    send({
      method: 'textDocument/didOpen',
      params: {
        textDocument: {
          uri,
          languageId: OPENSCAD_LANGUAGE_ID,
          version: model.getVersionId(),
          text: model.getValue(),
        },
      },
    })
    disposables.push(
      model.onDidChangeContent(() => {
        send({
          method: 'textDocument/didChange',
          params: {
            textDocument: { uri, version: model.getVersionId() },
            contentChanges: [{ text: model.getValue() }],
          },
        })
      }),
    )
    register(initialized.capabilities)
  }

  function register(capabilities: ServerCapabilities) {
    const { languages } = monaco
    if (capabilities.completionProvider) {
      disposables.push(
        languages.registerCompletionItemProvider(OPENSCAD_LANGUAGE_ID, {
          async provideCompletionItems(target, at) {
            if (!ours(target)) return undefined
            const word = target.getWordUntilPosition(at)
            const range = {
              startLineNumber: at.lineNumber,
              startColumn: word.startColumn,
              endLineNumber: at.lineNumber,
              endColumn: word.endColumn,
            }
            const result = await request<CompletionResult>('textDocument/completion', {
              textDocument: document,
              position: position(at),
            })
            const items = Array.isArray(result) ? result : (result?.items ?? [])
            return {
              suggestions: items.map((item) => toCompletion(item, range)),
              incomplete: !Array.isArray(result) && !!result?.isIncomplete,
            }
          },
        }),
      )
    }
    if (capabilities.hoverProvider) {
      disposables.push(
        languages.registerHoverProvider(OPENSCAD_LANGUAGE_ID, {
          provideHover(target, at) {
            if (!ours(target)) return undefined
            return request<LspHover>('textDocument/hover', {
              textDocument: document,
              position: position(at),
            }).then(toHover)
          },
        }),
      )
    }
    if (capabilities.definitionProvider) {
      disposables.push(
        languages.registerDefinitionProvider(OPENSCAD_LANGUAGE_ID, {
          async provideDefinition(target, at) {
            if (!ours(target)) return undefined
            const result = await request<DefinitionResult>('textDocument/definition', {
              textDocument: document,
              position: position(at),
            })
            // Only this file: an `include`d one has no model in this editor to open (#95).
            return toLocations(result)
              .filter((location) => location.uri === uri)
              .map((location) => ({ uri: target.uri, range: location.range }))
          },
        }),
      )
    }
    if (capabilities.documentFormattingProvider) {
      disposables.push(
        languages.registerDocumentFormattingEditProvider(OPENSCAD_LANGUAGE_ID, {
          async provideDocumentFormattingEdits(target, options) {
            if (!ours(target)) return undefined
            const edits = await request<LspTextEdit[]>('textDocument/formatting', {
              textDocument: document,
              options: { tabSize: options.tabSize, insertSpaces: options.insertSpaces },
            })
            return toEdits(edits)
          },
        }),
      )
    }
  }

  return {
    dispose() {
      disposed = true
      for (const disposable of disposables) disposable.dispose()
      disposables.length = 0
      socket.close()
    },
  }
}
