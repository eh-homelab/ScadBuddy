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
