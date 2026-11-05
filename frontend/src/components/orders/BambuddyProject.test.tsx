import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { resetBambuddyLinks, setBambuddyLinks } from '../../lib/bambuddyLinks'
import { BambuddyProject } from './BambuddyProject'

// #4253 §8.1 — the project mirroring an order links to its page in Bambuddy.
describe('BambuddyProject', () => {
  afterEach(() => resetBambuddyLinks())

  it("links to the project's page on Bambuddy's web URL", () => {
    setBambuddyLinks({ bambuddy_url: 'http://bambuddy.svc', bambuddy_web_urls: 'https://bambuddy.example/' })
    render(<BambuddyProject projectId={42} label="Bambuddy project" />)
    expect(screen.getByRole('link', { name: 'Bambuddy project' })).toHaveAttribute('href', 'https://bambuddy.example/projects/42')
  })

  it('names the project by its number with no web URL known', () => {
    render(<BambuddyProject projectId={42} label="Bambuddy project" />)
    expect(screen.getByTestId('bambuddy-project')).toHaveTextContent('Bambuddy project #42')
  })
})
