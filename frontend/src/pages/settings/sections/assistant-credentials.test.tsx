import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderSettingsSection } from '../testing'
import { Component as AssistantCredentials } from './assistant-credentials'
import { Component as DefaultSessionMode } from './default-session-mode'
import { Component as DurableSubagents } from './durable-subagents'
import { Component as Images } from './images'
import { Component as Memory } from './memory'
import { Component as Model } from './model'
import { Component as RenderLimits } from './render-limits'
import { Component as SessionLimits } from './session-limits'
import { Component as SessionRuntime } from './session-runtime'
import { Component as Workspace } from './workspace'

describe('Assistant sections', () => {
  it('names its region Status & credentials, and the status inside it "Assistant status"', async () => {
    renderSettingsSection(<AssistantCredentials />, { route: '/settings/assistant' })
    const section = await screen.findByRole('region', { name: 'Status & credentials' })
    expect(document.getElementById('assistant-credentials')).toHaveAccessibleName('Status & credentials')
    expect(section).toBe(document.getElementById('assistant-credentials'))
    const status = document.getElementById('ai-status-heading')!.closest('[role="region"]')!
    expect(status).toHaveAccessibleName('Assistant status')
  })

  it('renders every other section without error', async () => {
    renderSettingsSection(
      <>
        <Model />
        <SessionLimits />
        <SessionRuntime />
        <RenderLimits />
        <Images />
        <Workspace />
        <DefaultSessionMode />
        <DurableSubagents />
        <Memory />
      </>,
      { route: '/settings/assistant' },
    )
    expect(await screen.findByText('Model', { selector: 'h2 button' })).toBeInTheDocument()
  })
})
