import path from 'node:path'

// A plugin package's files as Settings shows them for review (#1029): the
// list with sizes, and one file's content. The routes
// (routes/pluginPackages.ts) serve only a path the pin's own file list names,
// from the copy that hashes to the pin (install.ts `readFile`), or a built-in's
// listed file (builtins.ts `readBuiltInFile`).

/** Characters of text sent unless the whole file is asked for. */
export const PREVIEW_CHARS = 64 * 1024

export type PackageFileEntry = { path: string; size: number }

export type PackageFileContent = {
  path: string
  size: number
  /** Not UTF-8 text (or it has a NUL byte): `content` is null. */
  binary: boolean
  media_type: string
  /** `content` stops at PREVIEW_CHARS; ask with `full` for the rest. */
  truncated: boolean
  content: string | null
}

const TYPES: Record<string, string> = {
  '.md': 'text/markdown',
  '.markdown': 'text/markdown',
  '.json': 'application/json',
  '.scad': 'text/x-openscad',
  '.sh': 'text/x-shellscript',
  '.bash': 'text/x-shellscript',
  '.py': 'text/x-python',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.cjs': 'text/javascript',
  '.ts': 'text/typescript',
  '.yaml': 'application/yaml',
  '.yml': 'application/yaml',
  '.toml': 'application/toml',
  '.html': 'text/html',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.pdf': 'application/pdf',
  '.zip': 'application/zip',
  '.3mf': 'model/3mf',
  '.stl': 'model/stl',
}

const BINARY_DEFAULT = 'application/octet-stream'

function mediaTypeOf(rel: string, binary: boolean): string {
  return TYPES[path.posix.extname(rel).toLowerCase()] ?? (binary ? BINARY_DEFAULT : 'text/plain')
}

const UTF8 = new TextDecoder('utf-8', { fatal: true })

export function fileContent(rel: string, bytes: Buffer, full: boolean): PackageFileContent {
  let text: string | null = null
  if (!bytes.includes(0)) {
    try {
      text = UTF8.decode(bytes)
    } catch {
      text = null
    }
  }
  const binary = text === null
  const truncated = !full && text !== null && text.length > PREVIEW_CHARS
  return {
    path: rel,
    size: bytes.length,
    binary,
    media_type: mediaTypeOf(rel, binary),
    truncated,
    content: truncated ? text!.slice(0, PREVIEW_CHARS) : text,
  }
}
