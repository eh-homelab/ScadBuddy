/**
 * A unified patch, rendered as git emits it.
 *
 * Deliberately a plain unified diff rather than a side-by-side editor: the paste/edit
 * path (#92) is what brings Monaco into the bundle, and pulling a 2 MB editor in for a
 * read-only diff before then is a bad trade. When it lands, this is the one component
 * to swap for its diff editor.
 */
const LINE_STYLE: { prefix: string; className: string }[] = [
  { prefix: 'diff --git', className: 'text-faint' },
  { prefix: 'index ', className: 'text-faint' },
  { prefix: '--- ', className: 'text-muted' },
  { prefix: '+++ ', className: 'text-muted' },
  { prefix: '@@', className: 'text-accent' },
  { prefix: '+', className: 'text-ok bg-ok/8' },
  { prefix: '-', className: 'text-warn bg-warn/8' },
]

function classFor(line: string): string {
  return LINE_STYLE.find((style) => line.startsWith(style.prefix))?.className ?? 'text-muted'
}

export function UnifiedDiff({ patch }: { patch: string }) {
  const lines = patch.replace(/\n$/, '').split('\n')

  if (patch.trim() === '') {
    return <p className="p-4 text-[12px] text-faint">These two revisions are identical.</p>
  }

  return (
    <pre
      data-testid="diff"
      aria-label="Unified diff"
      className="sb-num overflow-x-auto p-3 text-[12px] leading-[1.5]"
    >
      {lines.map((line, index) => (
        <div key={index} className={`whitespace-pre px-1 ${classFor(line)}`}>
          {line === '' ? ' ' : line}
        </div>
      ))}
    </pre>
  )
}
