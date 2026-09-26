/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_MOCK_API?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

/**
 * Monaco's contributions are plain ESM with no type declarations beside them — they
 * are imported for their side effects (each registers an editor feature), never for
 * anything they export.
 */
declare module 'monaco-editor/editor/contrib/*'

/**
 * The generated module behind `monaco.languages.CompletionItemKind` and the other
 * enums. It imports nothing, so `lsp.test.ts` pins its hand-written kind table against
 * the installed enum without loading the editor.
 */
declare module 'monaco-editor/editor/common/standalone/standaloneEnums.js' {
  import type { languages } from 'monaco-editor/editor/editor.api'
  export const CompletionItemKind: typeof languages.CompletionItemKind
}
