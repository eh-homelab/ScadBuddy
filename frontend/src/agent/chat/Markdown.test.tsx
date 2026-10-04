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

  it('renders a GFM table with column alignment in a horizontal scroller (#820)', () => {
    const { container } = render(
      <Markdown
        text={'Plan:\n\n| Part | Spool | Loaded |\n|:--|:-:|--:|\n| body | **PLA** red | yes |\n| text | `#fff` | no |'}
      />,
    )
    const table = screen.getByRole('table')
    expect(table.parentElement).toHaveClass('overflow-x-auto')
    expect(screen.getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['Part', 'Spool', 'Loaded'])
    for (const th of screen.getAllByRole('columnheader')) expect(th).toHaveAttribute('scope', 'col')
    const rows = screen.getAllByRole('row')
    expect(rows).toHaveLength(3)
    const cells = screen.getAllByRole('cell')
    expect(cells.map((td) => td.textContent)).toEqual(['body', 'PLA red', 'yes', 'text', '#fff', 'no'])
    expect(cells.map((td) => td.style.textAlign)).toEqual(['left', 'center', 'right', 'left', 'center', 'right'])
    expect(screen.getByText('PLA').tagName).toBe('STRONG')
    expect(container.textContent).not.toContain('|')
  })

  it('parses table rows with and without edge pipes, escaped pipes and short rows', () => {
    expect(parseBlocks('a | b\n--- | ---\n1 \\| 2 | 3\n| 4 |')).toEqual([
      {
        kind: 'table',
        align: [null, null],
        header: ['a', 'b'],
        rows: [
          ['1 | 2', '3'],
          ['4', ''],
        ],
      },
    ])
  })

  it('keeps a heading or list item with a pipe as itself, not a table header', () => {
    const blocks = parseBlocks('# A | B\n-|-\n\n- x | y\n--|--')
    expect(blocks.map((b) => b.kind)).not.toContain('table')
    expect(blocks[0]).toEqual({ kind: 'heading', level: 1, text: 'A | B' })
  })

  it('reads a pipe after an escaped backslash as a real cell edge', () => {
    expect(parseBlocks('| a | b |\n|---|---|\n| x\\\\ | y\\\\|')).toEqual([
      { kind: 'table', align: [null, null], header: ['a', 'b'], rows: [['x\\\\', 'y\\\\']] },
    ])
  })

  it('needs a pipe in the delimiter row, so `text` over `--` is not a table (setext, as in cmark-gfm)', () => {
    expect(parseBlocks('a\n--\n1').map((b) => b.kind)).not.toContain('table')
  })

  it('leaves pipes without a delimiter row as a paragraph', () => {
    expect(parseBlocks('| a | b |\n| c | d |')).toEqual([{ kind: 'para', text: '| a | b | | c | d |' }])
  })

  it('shows a same-origin API image inline, such as a render view (#820)', () => {
    render(<Markdown text={'Top: ![top view](/api/v1/jobs/j1/views/top.png?size=256)'} />)
    const img = screen.getByRole('img', { name: 'top view' })
    expect(img).toHaveAttribute('src', '/api/v1/jobs/j1/views/top.png?size=256')
  })

  it('never fetches a remote, protocol-relative or non-API image', () => {
    const { container } = render(
      <Markdown
        text={
          '![remote](https://evil.example/x.png) ![proto](//evil.example/x.png) ' +
          '![data](data:text/html;base64,PHNjcmlwdD4=) ![js](javascript:alert(1)) ![other](/assets/x.png) ' +
          '![dots](/api/v1/../../x.png) ![slash](/api/v1\\evil) ![settings](/api/v1/settings) ' +
          '![print](/api/v1/prints/1/thumbnail) ![climb](/api/v1/models/m/../../settings#thumbnail) ' +
          '![rel](thumbnail.png)'
        }
      />,
    )
    expect(container.querySelector('img')).toBeNull()
    for (const alt of ['remote', 'proto', 'data', 'js', 'other', 'dots', 'slash', 'settings', 'print', 'climb', 'rel']) {
      expect(screen.getByText(alt)).toBeInTheDocument()
    }
  })

  it('shows only the read-only media routes: views, colours, thumbnails, plates', () => {
    const { container } = render(
      <Markdown
        text={
          '![a](/api/v1/jobs/j1/views/iso.png) ![b](/api/v1/jobs/j1/colours.png) ' +
          '![c](/api/v1/outputs/o1/thumbnail) ![d](/api/v1/outputs/o1/views/top.png) ' +
          '![e](/api/v1/outputs/o1/plates/2/thumbnail) ![f](/api/v1/models/my-box/thumbnail?v=3)'
        }
      />,
    )
    expect(container.querySelectorAll('img')).toHaveLength(6)
  })

  it('shows an inline data: image (#951)', () => {
    render(<Markdown text={'![dot](data:image/png;base64,iVBORw0KGgo=)'} />)
    expect(screen.getByRole('img', { name: 'dot' })).toHaveAttribute('src', 'data:image/png;base64,iVBORw0KGgo=')
  })

  it('resolves a relative image against the model it is given as a base (#951)', () => {
    const { container } = render(
      <Markdown
        text={'![cover](thumbnail.png) ![arch](images/arch-ring-stand.png) ![out](../x.png) ![abs](/etc/x.png)'}
        base={{ slug: 'chunky-name-sign', revision: 'abc1234' }}
      />,
    )
    expect(screen.getByRole('img', { name: 'cover' })).toHaveAttribute(
      'src',
      '/api/v1/models/chunky-name-sign/images/thumbnail.png?commit=abc1234',
    )
    expect(screen.getByRole('img', { name: 'arch' })).toHaveAttribute(
      'src',
      '/api/v1/models/chunky-name-sign/images/images/arch-ring-stand.png?commit=abc1234',
    )
    expect(container.querySelectorAll('img')).toHaveLength(2)
    expect(screen.getByText('out')).toBeInTheDocument()
    expect(screen.getByText('abs')).toBeInTheDocument()
  })

  it('resolves a relative image nested in a list, a table and bold text too', () => {
    render(
      <Markdown
        text={'- **![a](a.png)**\n\n| x |\n|---|\n| ![b](b.png) |'}
        base={{ slug: 'demo' }}
      />,
    )
    expect(screen.getByRole('img', { name: 'a' })).toHaveAttribute('src', '/api/v1/models/demo/images/a.png')
    expect(screen.getByRole('img', { name: 'b' })).toHaveAttribute('src', '/api/v1/models/demo/images/b.png')
  })

  it('renders an image and a link side by side', () => {
    render(<Markdown text={'see ![a](/api/v1/models/m/thumbnail) and [b](https://example.org)'} />)
    expect(screen.getByRole('img', { name: 'a' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'b' })).toHaveAttribute('href', 'https://example.org')
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
