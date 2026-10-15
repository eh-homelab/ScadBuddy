import { BlockList, isIP } from 'node:net'
import { embeddedIPv4 } from '../http/egress.js'

// Where the headless browser may go (#982). The browser is Microsoft's Playwright MCP
// added as a remote plugin (docs/ai/headless-browser.md); this decides, from the URL
// alone, what each of its navigations is:
//
//   denied   a deny rule matches. Refused, whoever approves; deny wins over allow.
//   allowed  an allow rule matches, or the user approved the origin for the session.
//            Runs without asking.
//   ask      on neither list. An outward call: it parks for the user's approval.
//
// Rules (one per line in Settings):
//   https://host[:port] or http://host[:port]   that exact origin
//   host                                         that host exactly
//   *.host                                       any subdomain of host (never host itself)
//   address/prefix                               an IP range (CIDR), for IP-literal URLs
// A host rule or wildcard on the ALLOW list matches https on its default port only;
// any other scheme or port needs the full origin. On the DENY list it matches every
// scheme and port. A host matches whole labels only, so `makerworld.com` never
// matches `makerworld.com.evil.example`, and `*.makerworld.com` never matches
// `evilmakerworld.com`. Hosts are compared as the URL parser normalises them
// (lower case, punycode, IPv4 in dotted form), without a trailing dot.
//
// This judges names, not addresses: a permitted name that resolves to a private
// address is not caught here. The boundary for that is the network where the
// browser runs (docs/ai/headless-browser.md, "What the server can reach").

/** `ai_settings` key: `{ allow: string[], deny: string[] }`; absent means the defaults. */
export const SETTING_BROWSER_ORIGINS = 'browser_origins'
export const MAX_RULES = 200
export const MAX_RULE_LENGTH = 300

/** Model sites and references a user links to; they open without asking. */
export const DEFAULT_ALLOW: readonly string[] = [
  'makerworld.com',
  'printables.com',
  'www.printables.com',
  'thingiverse.com',
  'www.thingiverse.com',
  'github.com',
  'raw.githubusercontent.com',
  'openscad.org',
  'en.wikibooks.org',
]

/** Loopback, link-local and cloud metadata, and cluster-internal names. */
export const DEFAULT_DENY: readonly string[] = [
  'localhost',
  '*.localhost',
  '127.0.0.0/8',
  '::1/128',
  '::/128',
  '0.0.0.0/8',
  '169.254.0.0/16',
  'fe80::/10',
  '100.100.100.200/32',
  'fd00:ec2::254/128',
  'metadata',
  'metadata.goog',
  '*.internal',
  '*.svc',
  '*.cluster.local',
]

export type BrowserLists = { allow: string[]; deny: string[] }

export const DEFAULT_LISTS: BrowserLists = { allow: [...DEFAULT_ALLOW], deny: [...DEFAULT_DENY] }

export class RuleError extends Error {
  override name = 'RuleError'
}

type Rule =
  | { kind: 'origin'; text: string; origin: string }
  | { kind: 'host'; text: string; host: string }
  | { kind: 'wildcard'; text: string; suffix: string }
  | { kind: 'cidr'; text: string; net: BlockList }

/** `url`'s origin, without a trailing dot on its host: the key rules and approvals compare. */
export function originOf(url: URL): string {
  return `${url.protocol}//${url.hostname.replace(/\.$/, '')}${url.port ? `:${url.port}` : ''}`
}

function bareHost(hostname: string): string {
  const lower = hostname.toLowerCase().replace(/\.$/, '')
  return lower.startsWith('[') ? lower.slice(1, -1) : lower
}

/** A host as the URL parser writes it, or undefined when it is not one. */
function normalHost(raw: string): string | undefined {
  const text = isIP(raw) === 6 ? `[${raw}]` : raw
  if (!text || /[/?#@\s\\:]/.test(text.replace(/^\[.*\]$/, ''))) return undefined
  try {
    const url = new URL(`http://${text}/`)
    return url.port === '' ? bareHost(url.hostname) || undefined : undefined
  } catch {
    return undefined
  }
}

/** One rule, checked; throws RuleError saying what is wrong with it. */
function parseRule(raw: string): Rule {
  const text = raw.trim()
  if (!text) throw new RuleError('an empty rule')
  if (text.length > MAX_RULE_LENGTH) throw new RuleError(`"${text.slice(0, 40)}…" is longer than ${MAX_RULE_LENGTH} characters`)
  if (text.includes('://')) {
    let url: URL
    try {
      url = new URL(text)
    } catch {
      throw new RuleError(`"${text}" is not a URL`)
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') throw new RuleError(`"${text}" is not an http or https origin`)
    if (url.username || url.password || (url.pathname !== '/' && url.pathname !== '') || url.search || url.hash || /\/./.test(text.split('://')[1] ?? '')) {
      throw new RuleError(`"${text}" is not an origin: give scheme, host and port only, with no path`)
    }
    return { kind: 'origin', text, origin: originOf(url) }
  }
  const slash = text.indexOf('/')
  if (slash > 0) {
    const address = text.slice(0, slash)
    const prefix = Number(text.slice(slash + 1))
    const family = isIP(address)
    const max = family === 4 ? 32 : 128
    if (family === 0 || !/^\d{1,3}$/.test(text.slice(slash + 1)) || prefix > max) {
      throw new RuleError(`"${text}" is not an address range (address/prefix)`)
    }
    const net = new BlockList()
    net.addSubnet(address, prefix, family === 4 ? 'ipv4' : 'ipv6')
    return { kind: 'cidr', text, net }
  }
  if (text.startsWith('*.')) {
    const suffix = normalHost(text.slice(2))
    if (!suffix || isIP(suffix)) throw new RuleError(`"${text}" is not a host pattern (*.example.com)`)
    return { kind: 'wildcard', text, suffix }
  }
  if (text.includes('*')) throw new RuleError(`"${text}": a wildcard may only be a leading "*."`)
  const host = normalHost(text)
  if (!host) throw new RuleError(`"${text}" is not a host, an origin or an address range`)
  return { kind: 'host', text, host }
}

/** The rules, each checked, at most MAX_RULES; throws RuleError naming the first bad one. */
export function checkRules(rules: readonly string[], list: 'allow' | 'deny'): string[] {
  if (rules.length > MAX_RULES) throw new RuleError(`the ${list} list has more than ${MAX_RULES} rules`)
  const out: string[] = []
  for (const raw of rules) {
    const text = raw.trim()
    try {
      parseRule(text)
    } catch (err) {
      throw new RuleError(`${list} list: ${(err as Error).message}`)
    }
    if (!out.includes(text)) out.push(text)
  }
  return out
}

/**
 * The stored lists, or the defaults when none is stored. A stored value that cannot be
 * read fails closed: no allow list (every page asks), and the default deny list.
 */
export function listsOf(stored: unknown): BrowserLists {
  if (stored === undefined || stored === null) return { allow: [...DEFAULT_ALLOW], deny: [...DEFAULT_DENY] }
  const { allow, deny } = (typeof stored === 'object' ? stored : {}) as { allow?: unknown; deny?: unknown }
  const strings = (v: unknown) => (Array.isArray(v) && v.every((s) => typeof s === 'string') ? (v as string[]) : undefined)
  const readAllow = strings(allow)
  const readDeny = strings(deny)
  return { allow: readAllow ?? [], deny: readDeny ? [...new Set([...readDeny])] : [...DEFAULT_DENY] }
}

/**
 * Compiled lists. An allow rule that no longer parses (stored before a check changed)
 * is skipped, which only asks more; a deny rule that does not is kept as `broken`, and
 * then every page is refused (fail closed: the rule meant to refuse something).
 */
export type CompiledLists = { allow: Rule[]; deny: Rule[]; broken?: string[] }

export function compile(lists: BrowserLists): CompiledLists {
  const broken: string[] = []
  const each = (rules: readonly string[], bad?: string[]) =>
    rules.flatMap((r) => {
      try {
        return [parseRule(r)]
      } catch {
        bad?.push(r)
        return []
      }
    })
  const compiled: CompiledLists = { allow: each(lists.allow), deny: each(lists.deny, broken) }
  return broken.length ? { ...compiled, broken } : compiled
}

function ipMatches(net: BlockList, host: string): boolean {
  const family = isIP(host)
  if (family === 0) return false
  if (net.check(host, family === 4 ? 'ipv4' : 'ipv6')) return true
  const inner = family === 6 ? embeddedIPv4(host) : undefined
  return inner !== undefined && net.check(inner, 'ipv4')
}

function matches(rule: Rule, url: URL, list: 'allow' | 'deny'): boolean {
  const host = bareHost(url.hostname)
  // A bare host or wildcard allows https on its default port only; it denies them all.
  const plain = list === 'deny' || (url.protocol === 'https:' && url.port === '')
  switch (rule.kind) {
    case 'origin':
      return originOf(url) === rule.origin
    case 'host':
      return plain && (host === rule.host || (isIP(host) === 6 && embeddedIPv4(host) === rule.host))
    case 'wildcard':
      return plain && host.endsWith(`.${rule.suffix}`)
    case 'cidr':
      return ipMatches(rule.net, host)
  }
}

/** An origin the user approved in a session: `session` opens without asking; `once` was opened once, so its pages may be read. */
export type ApprovedScope = 'once' | 'session'

export type Judgement =
  /** Not an address the browser may be sent to at all (not http(s), or with credentials). */
  | { kind: 'refused'; reason: string }
  | { kind: 'denied'; origin: string; rule: string }
  | { kind: 'allowed'; origin: string; rule?: string }
  | { kind: 'ask'; origin: string }

/**
 * What `url` is under the lists and the session's approved origins. `use` is
 * `navigate` for a call that would send the browser there, where only a `session`
 * approval stands in for the allow list, and `read` for a page it is already on, where
 * any approval does (it was opened with the user's consent).
 */
/**
 * `raw` parsed once with the WHATWG URL parser, accepted only when it is already that
 * parser's own serialisation (`href`; a bare origin may leave off the final `/`). So
 * what is judged is exactly the string the browser is given, or reports: a URL another
 * parser could read differently (a backslash, whitespace or a control character, a
 * percent-encoded or upper-case host, a number for an address, a scheme in capitals)
 * is refused, with the form to send instead (security review of #3654:
 * parser-differential). Undefined plus why, when it is not one.
 */
export function canonicalUrl(raw: string): { url: URL } | { problem: string } {
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u0020\u007f\\]/.test(raw)) {
    return { problem: `"${raw.slice(0, 200)}" contains whitespace, a control character or a backslash` }
  }
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return { problem: `"${raw.slice(0, 200)}" is not an absolute URL` }
  }
  if (url.href !== raw && !(url.pathname === '/' && !url.search && !url.hash && `${raw}/` === url.href)) {
    return { problem: `"${raw.slice(0, 200)}" is not written as a browser writes it; send ${url.href.slice(0, 200)} instead` }
  }
  return { url }
}

export function judge(lists: CompiledLists, approved: ReadonlyMap<string, ApprovedScope>, url: string, use: 'navigate' | 'read'): Judgement {
  const canonical = canonicalUrl(url)
  if ('problem' in canonical) return { kind: 'refused', reason: canonical.problem }
  const parsed = canonical.url
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    return { kind: 'refused', reason: `only http and https pages may be opened, not ${parsed.protocol}` }
  }
  if (parsed.username || parsed.password) return { kind: 'refused', reason: 'a URL with a user name or password is not opened' }
  const origin = originOf(parsed)
  if (lists.broken?.length) return { kind: 'denied', origin, rule: lists.broken[0]! }
  const deny = lists.deny.find((r) => matches(r, parsed, 'deny'))
  if (deny) return { kind: 'denied', origin, rule: deny.text }
  const allow = lists.allow.find((r) => matches(r, parsed, 'allow'))
  if (allow) return { kind: 'allowed', origin, rule: allow.text }
  const scope = approved.get(origin)
  if (scope === 'session' || (scope !== undefined && use === 'read')) return { kind: 'allowed', origin }
  return { kind: 'ask', origin }
}

/**
 * The text a browser result is screened as, the same in a classic turn (a PostToolUse
 * hook's `tool_response`, the content blocks) and a durable one (the MCP result's
 * `content`): the whole of it, whatever its shape, so a page named anywhere in it is read.
 */
export function screenedText(response: unknown): string {
  return typeof response === 'string' ? response : (JSON.stringify(response) ?? '')
}

/**
 * The `Page URL:` lines of a Playwright MCP result (raw text or JSON-escaped in the
 * untrusted envelope), up to the end of the line. Each is judged with `judge`, which
 * refuses one that is not a URL as the browser writes it, so a line the extraction cut
 * or a forged one never passes as an allowed page.
 */
export function pageUrls(text: string): string[] {
  return [...text.matchAll(/Page URL:[ \t]*((?:[^\r\n"\\]|\\(?![nrt"\\]))*)/g)].map((m) => m[1]!.trim())
}

/** What the model reads in place of a result from a page the lists do not let it read. */
export function withheldText(judgement: Exclude<Judgement, { kind: 'allowed' }>): string {
  if (judgement.kind === 'refused') {
    return `ScadBuddy withheld this result: the browser is on a page it may not read (${judgement.reason}).`
  }
  if (judgement.kind === 'denied') {
    return (
      `ScadBuddy withheld this result: the browser is now on ${judgement.origin}, which the deny rule "${judgement.rule}" ` +
      'refuses (Settings → Assistant → AI headless browser). Nothing the page said is shown. Do not try to open it.'
    )
  }
  return (
    `ScadBuddy withheld this result: the browser is now on ${judgement.origin} (a redirect, a link or a new tab), which is ` +
    'not on the allow list and was not approved in this session. Nothing the page said is shown. To read it, call ' +
    'browser_navigate with that address: the user is asked first.'
  )
}

/** What the model reads in place of a blank tab's result while another tab is open (browser/guard.ts). */
export function blankTabText(url: string): string {
  return (
    `ScadBuddy withheld this result: the browser is on a blank tab (${url}) while another tab is open, and a page in ` +
    "another tab can write into a tab it opened, so it cannot be checked against the headless browser's lists. Close " +
    'the other tabs, or navigate this one to a page, to read it.'
  )
}

/** What the model reads in place of a browser result that does not say which page it is from. */
export function unplacedText(tool: string): string {
  return (
    `ScadBuddy withheld this result of ${tool}: it does not say which page the browser is on, so it cannot be checked ` +
    'against the headless browser\'s lists. Call browser_snapshot to see the page, which names it.'
  )
}

/** The refusal a navigation the lists refuse gets. */
export function refusalText(judgement: Extract<Judgement, { kind: 'refused' | 'denied' }>): string {
  return judgement.kind === 'denied'
    ? `${judgement.origin} is refused by the deny rule "${judgement.rule}" (Settings → Assistant → AI headless browser), ` +
        'so the browser was not sent there. An approval cannot open it; do not retry.'
    : `The browser was not sent there: ${judgement.reason}.`
}
