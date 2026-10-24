import { createHash } from 'node:crypto'
import type { SessionFiles, WorkspaceFileInfo } from '../../src/workspace/store.js'

/**
 * A workspace in memory with what an upload reads (`stat`, `chunks`), for tests
 * without Postgres: binary tools take a workspace file only (#2510). `ref(path)`
 * is the `workspace_path` + `workspace_sha256` pair a call gives.
 */
export function memoryWorkspace(files: Record<string, Buffer>, mediaType = 'application/octet-stream') {
  const infos = new Map<string, WorkspaceFileInfo & { bytes: Buffer }>()
  for (const [path, bytes] of Object.entries(files)) {
    const sha256 = createHash('sha256').update(bytes).digest('hex')
    infos.set(path, { path, mediaType, size: bytes.length, sha256, source: null, createdAt: new Date(0), updatedAt: new Date(0), bytes })
  }
  const workspace = {
    async stat(_ws: unknown, path: string) {
      const found = infos.get(path)
      if (!found) return undefined
      const { bytes: _bytes, ...info } = found
      return info
    },
    async *chunks(_ws: unknown, path: string) {
      yield infos.get(path)!.bytes
    },
  } as unknown as SessionFiles
  return {
    workspace,
    ref: (path: string) => ({ workspace_path: path, workspace_sha256: infos.get(path)!.sha256 }),
  }
}
