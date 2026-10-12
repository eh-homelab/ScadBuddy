import { highlight, TOKEN_CLASS, type Language } from '../lib/highlight'

/**
 * #2350 — read-only code, highlighted by `lib/highlight.ts` (the plugin file viewer's):
 * a flow's script. Pages load it lazily (`FlowScript`). React renders each token as
 * text, never HTML.
 */
export function CodeView({ text, language, label }: { text: string; language: Language; label: string }) {
  return (
    <pre
      aria-label={label}
      data-testid="code-view"
      className="max-h-[32rem] overflow-auto rounded-[6px] border border-line bg-surface-2 p-3 font-mono text-[12px] leading-snug"
    >
      {highlight(text, language).map((token, i) => (
        <span key={i} data-token={token.kind} className={TOKEN_CLASS[token.kind]}>
          {token.text}
        </span>
      ))}
    </pre>
  )
}
