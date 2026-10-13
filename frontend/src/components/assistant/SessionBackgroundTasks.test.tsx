import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { seedBackgroundTask } from '../../mocks/features/backgroundTasks'
import { renderPage } from '../../test/utils'
import { SessionBackgroundTasks } from './SessionBackgroundTasks'

// The jobs and wakeups a session's CronCreate and ScheduleWakeup left running, over the
// agent's /api/v1/ai/sessions/:id/background-tasks (msw: mocks/features/backgroundTasks.ts).

it('shows nothing when the session has nothing scheduled', async () => {
  const { container } = renderPage(<SessionBackgroundTasks sessionId="s-none" canWrite />)
  await new Promise((r) => setTimeout(r, 50))
  expect(container.querySelector('section')).toBeNull()
})

it("lists the running tasks, and the session's owner cancels one", async () => {
  seedBackgroundTask('s1', { id: 'aaaa1111', schedule: 'Every 5 minutes', prompt: 'check the build' })
  seedBackgroundTask('s1', { id: 'bbbb2222', kind: 'wakeup', schedule: 'once, 1200 s after it was set', prompt: 'go on' })
  seedBackgroundTask('s1', { id: 'cccc3333', state: 'done', prompt: 'old' })
  renderPage(<SessionBackgroundTasks sessionId="s1" canWrite />)
  const list = await screen.findByRole('region', { name: 'Scheduled in this session' })
  expect(list).toHaveTextContent('Every 5 minutes')
  expect(list).toHaveTextContent('check the build')
  expect(list).toHaveTextContent('go on')
  expect(list).not.toHaveTextContent('old')
  await userEvent.click(screen.getByRole('button', { name: 'Cancel aaaa1111' }))
  expect(await screen.findByRole('button', { name: 'Cancel bbbb2222' })).toBeInTheDocument()
  expect(screen.queryByText('check the build')).not.toBeInTheDocument()
})

it('offers no cancel to someone who does not own the session', async () => {
  seedBackgroundTask('s2', { id: 'dddd4444' })
  renderPage(<SessionBackgroundTasks sessionId="s2" canWrite={false} />)
  await screen.findByRole('region', { name: 'Scheduled in this session' })
  expect(screen.queryByRole('button', { name: /Cancel/ })).not.toBeInTheDocument()
})
