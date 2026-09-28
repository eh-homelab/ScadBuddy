import { render, screen } from '@testing-library/react'
import { Markdown } from './Markdown'
import { parseBlocks } from './markdownBlocks'
import { pageContext, suggestedPrompts } from './pageContext'
import { toolLabel } from './labels'

describe('Markdown', () => {
  it('renders inline marks, lists and code without HTML injection', () => {
    const { container } = render(
      <Markdown
        text={'Hi **bold** and *it* with `code` <img src=x onerror=alert(1)>\n\n- one\n- [two](https://example.org)\n\n```scad\ncube(1);\n```'}
      />,
    )
    expect(screen.getByText('bold').tagName).toBe('STRONG')
    expect(screen.getByText('it').tagName).toBe('EM')
    expect(screen.getByText('code').tagName).toBe('CODE')
    expect(screen.getByRole('link', { name: 'two' })).toHaveAttribute('href', 'https://example.org')
    expect(screen.getByText('cube(1);')).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('refuses non-http links', () => {
    render(<Markdown text="[click](javascript:alert(1))" />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByText('click')).toBeInTheDocument()
  })

  it('treats an unclosed fence mid-stream as code to the end', () => {
    expect(parseBlocks('text\n```\ncube(')).toEqual([
      { kind: 'para', text: 'text' },
      { kind: 'code', lang: '', body: 'cube(' },
    ])
  })
})

describe('page context', () => {
  it('names the model on its pages', () => {
    expect(pageContext('/m/name-keychain/source')).toEqual({ route: '/m/name-keychain/source', modelSlug: 'name-keychain' })
    expect(pageContext('/settings')).toEqual({ route: '/settings' })
  })

  it("carries the browser bridge's view of the page when given one (#254)", () => {
    const view = { tools: ['get_params', 'set_param'], dialogs: [], page: { customize: { slug: 'name-keychain' } } }
    expect(pageContext('/m/name-keychain', view)).toEqual({
      route: '/m/name-keychain',
      modelSlug: 'name-keychain',
      ...view,
    })
  })

  it('suggests prompts per page', () => {
    expect(suggestedPrompts('/m/x')).toContain('Explain these settings')
    expect(suggestedPrompts('/m/x/source')).toContain('Why does this fail to render?')
    expect(suggestedPrompts('/settings')).toContain('Help me connect Bambuddy')
  })

  it('shortens tool names', () => {
    expect(toolLabel('mcp__scadbuddy__print_output')).toBe('print_output')
    expect(toolLabel('mcp__hindsight__recall')).toBe('hindsight: recall')
    expect(toolLabel('local_tool')).toBe('local_tool')
  })
})
