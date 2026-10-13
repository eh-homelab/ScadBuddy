import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// Citation scoring (issue #1924; #259: "every suggested change carries a
// source, and the cited source actually supports the claim"). Deterministic,
// with no model and no network, so the scripted run scores exactly as a live
// one (docs/ai/evals.md, "Citation scoring", is the rule in prose):
//
//   - A suggestion is one Markdown list item of the reply (`-`, `*`, `+`, `1.`
//     or `1)`), with its continuation lines.
//   - A citation is the form .github/scripts/lint-plugin.sh accepts for a skill
//     and CLAUDE.md asks of agent-authored text (#1968): a URL, or a repository
//     file path with its `§` or section (`§9`, `section 6.3`,
//     `section "Holes and fits"`), in the same item. A path to a file that is
//     not Markdown may stand alone: such a file has no sections to cite.
//   - A cited path must exist in this repository, and a cited section must be a
//     heading of that file. A cited URL's host must be one the plugin's skills
//     cite (plugins/*/skills/*/SKILL.md): the scorer fetches nothing, so it
//     cannot read the page.
//   - A claim is a number with a unit (mm, °, degrees, %) in the suggestion,
//     outside its citations. It is supported when one of the item's resolved
//     repository sources states that number; unchecked when it does not but the
//     item also cites a known URL (whose page the scorer cannot read);
//     unsupported otherwise.

/** The repository root, with a trailing separator. */
export const REPO_ROOT = fileURLToPath(new URL('../../', import.meta.url))

export type Citation =
  | { kind: 'url'; url: string; host: string }
  | { kind: 'path'; path: string; section?: string }

export type Suggestion = { text: string; citations: Citation[] }

const LIST_ITEM = /^\s{0,3}(?:[-*+]|\d+[.)])\s+(.*)$/
const URL_RE = /https?:\/\/[^\s<>()[\]`]+/g
const PATH_RE =
  /(?:\.\/)?((?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]*[A-Za-z0-9_-]\.(?:md|py|tsx?|js|json|sh|scad|ya?ml|toml))(?![A-Za-z0-9_])/g
const SECTION_RE = /§\s*(\d+(?:\.\d+)*)|\bsection\s+(?:"([^"]+)"|“([^”]+)”|(\d+(?:\.\d+)*))/gi
const CLAIM_RE = /(?<![\w.])(\d+(?:\.\d+)?)\s*(?:mm\b|°|degrees?\b|%)/g

/** The text with every match of `re` blanked out, so positions still line up. */
function blank(text: string, re: RegExp): string {
  return text.replace(re, (m) => ' '.repeat(m.length))
}

function trimUrl(url: string): string {
  return url.replace(/[.,;:!?'"]+$/, '')
}

/** The citations in one suggestion, in the order they appear. */
export function citationsOf(text: string): Citation[] {
  const found: { at: number; citation: Citation }[] = []
  for (const m of text.matchAll(URL_RE)) {
    const url = trimUrl(m[0])
    let host: string
    try {
      host = new URL(url).hostname
    } catch {
      continue
    }
    found.push({ at: m.index, citation: { kind: 'url', url, host } })
  }
  const rest = blank(text, URL_RE)
  const sections = [...rest.matchAll(SECTION_RE)].map((m) => ({
    at: m.index,
    name: (m[1] ?? m[2] ?? m[3] ?? m[4] ?? '').trim(),
    used: false,
  }))
  const paths = [...blank(rest, SECTION_RE).matchAll(PATH_RE)].map((m) => ({ at: m.index, end: m.index + m[0].length, path: m[1]! }))
  paths.forEach((p, i) => {
    const before = i > 0 ? paths[i - 1]!.end : 0
    const after = paths[i + 1]?.at ?? Infinity
    // The first section after the path, else the nearest unclaimed one before it.
    const section =
      sections.find((s) => !s.used && s.at >= p.end && s.at < after) ??
      sections.filter((s) => !s.used && s.at < p.at && s.at >= before).at(-1)
    if (section) section.used = true
    found.push({ at: p.at, citation: { kind: 'path', path: p.path, ...(section ? { section: section.name } : {}) } })
  })
  const seen = new Set<string>()
  return found
    .sort((a, b) => a.at - b.at)
    .map((f) => f.citation)
    .filter((c) => {
      const key = JSON.stringify(c)
      return seen.has(key) ? false : (seen.add(key), true)
    })
}

/** Each list item of a reply, continuation lines joined, with its citations. */
export function suggestionsOf(reply: string): Suggestion[] {
  const items: string[] = []
  let current: string | undefined
  for (const line of reply.split('\n')) {
    const item = LIST_ITEM.exec(line)
    if (item) {
      if (current !== undefined) items.push(current)
      current = item[1]!.trim()
    } else if (current !== undefined && /^\s+\S/.test(line)) {
      current = `${current} ${line.trim()}`
    } else {
      if (current !== undefined) items.push(current)
      current = undefined
    }
  }
  if (current !== undefined) items.push(current)
  return items.map((text) => ({ text, citations: citationsOf(text) }))
}

/** The numbers with units a suggestion states, outside its citations, each once. */
export function claimsOf(text: string): string[] {
  const bare = blank(blank(blank(text, URL_RE), SECTION_RE), PATH_RE)
  return [...new Set([...bare.matchAll(CLAIM_RE)].map((m) => m[1]!))]
}

export type Resolved = { ok: true; text: string } | { ok: false; reason: string }

export type Sources = {
  resolve(citation: Extract<Citation, { kind: 'path' }>): Resolved
  /** The hosts the plugin's skills cite. */
  knownHosts: ReadonlySet<string>
}

/** The Markdown section headed by `section` (a number or a name), to the next heading of its level. */
export function markdownSection(markdown: string, section: string): string | undefined {
  const lines = markdown.split('\n')
  const headings: { line: number; level: number; title: string }[] = []
  let fenced = false
  lines.forEach((line, i) => {
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced
    const h = fenced ? null : /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line)
    if (h) headings.push({ line: i, level: h[1]!.length, title: h[2]!.replace(/`/g, '') })
  })
  const numbered = /^\d+(\.\d+)*$/.test(section)
  const wanted = numbered ? new RegExp(`^§?\\s*${section.replace(/\./g, '\\.')}\\.?(\\s|$)`) : undefined
  const at = headings.findIndex((h) =>
    wanted ? wanted.test(h.title) : h.title.toLowerCase().includes(section.replace(/`/g, '').toLowerCase()),
  )
  if (at < 0) return undefined
  const start = headings[at]!
  const end = headings.slice(at + 1).find((h) => h.level <= start.level)?.line ?? lines.length
  return lines.slice(start.line, end).join('\n')
}

/** Resolves citations against the repository at `root` (this checkout by default). */
export function repoSources(root = REPO_ROOT): Sources {
  const files = new Map<string, string | undefined>()
  const read = (rel: string) => {
    if (!files.has(rel)) {
      const full = path.join(root, rel)
      let text: string | undefined
      try {
        text = statSync(full).isFile() ? readFileSync(full, 'utf8') : undefined
      } catch {
        text = undefined
      }
      files.set(rel, text)
    }
    return files.get(rel)
  }
  const knownHosts = new Set<string>()
  try {
    for (const plugin of readdirSync(path.join(root, 'plugins'))) {
      let skills: string[] = []
      try {
        skills = readdirSync(path.join(root, 'plugins', plugin, 'skills'))
      } catch {
        continue
      }
      for (const skill of skills) {
        const text = read(path.join('plugins', plugin, 'skills', skill, 'SKILL.md')) ?? ''
        for (const m of text.matchAll(URL_RE)) {
          try {
            knownHosts.add(new URL(trimUrl(m[0])).hostname)
          } catch {
            // not a URL after all
          }
        }
      }
    }
  } catch {
    // no plugins/ (not a checkout): no URL is known
  }
  return {
    knownHosts,
    resolve(citation) {
      const rel = path.normalize(citation.path)
      if (path.isAbsolute(rel) || rel.split(path.sep)[0] === '..') {
        return { ok: false, reason: `${citation.path} is outside the repository` }
      }
      const text = read(rel)
      if (text === undefined) return { ok: false, reason: `${citation.path} does not exist` }
      if (!rel.endsWith('.md')) return { ok: true, text }
      if (citation.section === undefined) return { ok: false, reason: `${citation.path} is Markdown: cite its § or section` }
      const section = markdownSection(text, citation.section)
      return section === undefined
        ? { ok: false, reason: `${citation.path} has no section ${citation.section}` }
        : { ok: true, text: section }
    },
  }
}

export type SuggestionScore = {
  text: string
  citations: Citation[]
  /** The item cites at least one source. */
  cited: boolean
  /** Why a cited source did not resolve. */
  unresolved: string[]
  /** Claims no resolved source states, and no known URL could. */
  unsupported: string[]
  /** Claims cited only to a known URL, which the scorer cannot read. */
  unchecked: string[]
}

function states(text: string, number: string): boolean {
  return new RegExp(`(?<![\\d.])${number.replace('.', '\\.')}(?!\\d|\\.\\d)`).test(text)
}

/** Scores every suggestion of a reply against its cited sources. */
export function scoreCitations(reply: string, sources: Sources = repoSources()): SuggestionScore[] {
  return suggestionsOf(reply).map(({ text, citations }) => {
    const unresolved: string[] = []
    const texts: string[] = []
    let knownUrl = false
    for (const c of citations) {
      if (c.kind === 'url') {
        if (sources.knownHosts.has(c.host)) knownUrl = true
        else unresolved.push(`${c.url}: ${c.host} is not a host the plugin skills cite`)
        continue
      }
      const found = sources.resolve(c)
      if (found.ok) texts.push(found.text)
      else unresolved.push(found.reason)
    }
    const unsupported: string[] = []
    const unchecked: string[] = []
    if (citations.length) {
      for (const claim of claimsOf(text)) {
        if (texts.some((t) => states(t, claim))) continue
        ;(knownUrl ? unchecked : unsupported).push(claim)
      }
    }
    return { text, citations, cited: citations.length > 0, unresolved, unsupported, unchecked }
  })
}
