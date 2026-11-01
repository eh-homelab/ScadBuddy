import { HttpResponse, http } from 'msw'
import type { GitRemoteStatus } from '../../api/types'
import { mockSettings } from '../handlers'

/**
 * #4974 — `GET /settings/git-remote`: with a remote set, both repositories, the models
 * one up to date and the flows one two commits behind with a failed push.
 */
export function gitRemoteStatus(configured: boolean): GitRemoteStatus {
  if (!configured) return { configured: false, repositories: [] }
  return {
    configured: true,
    repositories: [
      {
        repository: 'models',
        branch: 'models',
        head: 'a'.repeat(40),
        pushed_commit: 'a'.repeat(40),
        pushed_at: '2026-11-01T12:00:00Z',
        attempted_at: '2026-11-01T12:00:00Z',
        error: null,
        behind: 0,
        oldest_unpushed_at: null,
      },
      {
        repository: 'flows',
        branch: 'flows',
        head: 'b'.repeat(40),
        pushed_commit: null,
        pushed_at: null,
        attempted_at: '2026-11-01T12:05:00Z',
        error: 'git push failed: remote: Permission denied',
        behind: 2,
        oldest_unpushed_at: '2026-11-01T11:00:00Z',
      },
    ],
  }
}

export const handlers = [
  http.get('/api/v1/settings/git-remote', () => HttpResponse.json(gitRemoteStatus(Boolean(mockSettings().git_remote_url)))),
]
