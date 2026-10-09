import { useRef, useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import type { McpAuthMode } from '../api/mcpTokens'
import { copyText, selectContents } from '../lib/clipboard'
import { NEW_TAB } from '../lib/embed'
import { Button } from './ui/Button'

const PLUGIN_DOC = 'https://github.com/eh-homelab/ScadBuddy/blob/main/docs/ai/claude-plugin.md'
const PLACEHOLDER = '<your token>'

interface Props {
  /** SCADBUDDY_PUBLIC_URL as Settings has it; this page's origin when unset. */
  publicUrl: string | null | undefined
  /** What `/mcp` applies: no header while it is `disabled`. */
  authMode: McpAuthMode | null | undefined
  /** A token just minted, shown once; a placeholder otherwise. */
  token?: string | undefined
}

/** The server URL an outside client is given: the public URL's origin and path, plus `/mcp`. */
function mcpUrl(publicUrl: string | null | undefined): string {
  const base = publicUrl?.trim() || window.location.origin
  return `${base.replace(/\/+$/, '')}/mcp`
}

/**
 * #1910 (#251) — copy-paste setup for Claude Code and Claude Desktop, built from the
 * public URL and following the MCP auth mode: no `Authorization` header while it is
 * `disabled`, `Bearer` with a token otherwise (OIDC included: bearer tokens keep working
 * beside it). A token just minted is filled in; otherwise a placeholder stands for one.
 *
 * Claude Code takes a remote HTTP server with a header directly (`claude mcp add
 * --transport http … --header`). Claude Desktop's config file starts local servers
 * only, so it reaches `/mcp` through `mcp-remote`, with the header in an environment
 * variable (no space after the colon, so no argument needs quoting).
 *
 * The snippets are text in a <pre>, never a field's value, and the copy buttons are
 * USER_ONLY: a minted token must not reach the browser agent (McpTokensSection).
 */
export function McpConnectSnippet({ publicUrl, authMode, token }: Props) {
  const url = mcpUrl(publicUrl)
  const header = authMode !== 'disabled'
  const bearer = `Bearer ${token ?? PLACEHOLDER}`
  const claudeCode = `claude mcp add --transport http scadbuddy ${url}${header ? ` --header "Authorization: ${bearer}"` : ''}`
  const claudeDesktop = JSON.stringify(
    {
      mcpServers: {
        scadbuddy: {
          command: 'npx',
          args: ['-y', 'mcp-remote', url, ...(header ? ['--header', 'Authorization:${SCADBUDDY_AUTH}'] : [])],
          ...(header ? { env: { SCADBUDDY_AUTH: bearer } } : {}),
        },
      },
    },
    null,
    2,
  )

  return (
    <div className="space-y-3" data-testid="mcp-snippet">
      <p className="text-[13px] font-medium">Connect Claude</p>
      <p className="text-[12px] text-muted">
        {header && !token && <>Create a token below, then paste it where the snippet says {PLACEHOLDER}. </>}
        {!header && <>Authentication is off, so the snippets send no token. </>}
        For ScadBuddy&rsquo;s skills and subagents too, install the{' '}
        <a href={PLUGIN_DOC} {...NEW_TAB} className="text-accent underline">
          ScadBuddy Claude plugin ↗
        </a>{' '}
        instead; it asks for this address without <code className="sb-num">/mcp</code> and the token.
      </p>
      <Snippet id="mcp-snippet-claude-code" title="Claude Code" copyLabel="Copy the Claude Code command" text={claudeCode} />
      <Snippet
        id="mcp-snippet-claude-desktop"
        title="Claude Desktop (claude_desktop_config.json, needs Node.js)"
        copyLabel="Copy the Claude Desktop config"
        text={claudeDesktop}
      />
    </div>
  )
}

function Snippet({ id, title, copyLabel, text }: { id: string; title: string; copyLabel: string; text: string }) {
  const ref = useRef<HTMLPreElement>(null)
  const [copied, setCopied] = useState<'copied' | 'manual' | null>(null)
  return (
    <div>
      <div className="flex items-center gap-2">
        <p className="text-[12px]">{title}</p>
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          aria-label={copyLabel}
          onClick={() => void copyText(text, ref.current).then((ok) => setCopied(ok ? 'copied' : 'manual'))}
          {...USER_ONLY}
        >
          Copy
        </Button>
        {copied && (
          <span role="status" className={`text-[12px] ${copied === 'copied' ? 'text-ok' : 'text-warn'}`}>
            {copied === 'copied' ? 'Copied.' : 'Selected: press Ctrl+C (⌘C on a Mac).'}
          </span>
        )}
      </div>
      <pre
        ref={ref}
        data-testid={id}
        onClick={(event) => selectContents(event.currentTarget)}
        className="sb-num mt-1 cursor-text overflow-x-auto whitespace-pre-wrap break-all rounded-[4px] border border-line bg-surface-2 px-2 py-1.5 text-[12px]"
        {...USER_ONLY}
      >
        {text}
      </pre>
    </div>
  )
}
