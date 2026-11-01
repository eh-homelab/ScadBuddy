import { api } from '../../../api/client'
import type { RepositoryLag } from '../../../api/types'
import { Card, CardContent } from '@/components/ui/card'
import { timeAgo } from '../../../lib/format'
import { useAsync } from '../../../lib/useAsync'
import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf } from '../values'

export const id = 'git-remote'
export const area: AreaId = 'library'
export const title = 'Off-box history'
export const order = 50

const NAMES: Record<string, string> = { models: 'Models', flows: 'Flows' }

function lagText(repo: RepositoryLag): string {
  if (!repo.head) return 'No history yet'
  if (repo.behind === 0) return 'Up to date'
  const commits = repo.behind === 1 ? '1 commit' : `${repo.behind} commits`
  return repo.oldest_unpushed_at ? `${commits} behind, since ${timeAgo(repo.oldest_unpushed_at)}` : `${commits} behind`
}

/** #4974 — where each commit of the model and flow history is pushed, and how far behind it is. */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  const status = useAsync(() => api.getGitRemote(), [form.savedAt]).data
  return (
    <SavedSection
      form={form}
      id={id}
      title={title}
      description="Push the model and flow history to a git remote after each commit; a housekeeping pass retries every few minutes what did not land."
    >
      <RuntimeRows form={form} section={id} />
      {status?.configured && (
        <Card size="sm">
          <CardContent>
            <p className="text-sm font-medium">Remote</p>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm" data-testid="git-remote-lag">
              {(status.repositories ?? []).map((repo) => (
                <div key={repo.repository} className="contents">
                  <dt className="text-muted-foreground">
                    {NAMES[repo.repository] ?? repo.repository} → <code>{repo.branch}</code>
                  </dt>
                  <dd>
                    <span data-testid={`git-remote-${repo.repository}`}>{lagText(repo)}</span>
                    {repo.pushed_at && (
                      <span className="text-muted-foreground"> · last pushed {timeAgo(repo.pushed_at)}</span>
                    )}
                    {repo.error && (
                      <p role="alert" className="mt-1 text-xs break-words text-warn">
                        {repo.error}
                      </p>
                    )}
                  </dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>
      )}
    </SavedSection>
  )
}
