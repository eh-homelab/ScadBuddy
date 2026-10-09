/**
 * A small syntax highlighter for reading a plugin package's files before approving it
 * (#1029, Settings → Plugin packages). It splits text into tokens whose texts join back
 * to exactly the input, so what is shown is what the file says; React renders each
 * token as text, never HTML. It knows the file types a plugin is made of (Markdown,
 * JSON, scripts, OpenSCAD); anything else is plain text. Monaco is not used: its
 * bundle here carries OpenSCAD only (`lib/monaco.ts`).
 */

export type TokenKind = 'plain' | 'comment' | 'string' | 'number' | 'keyword' | 'key' | 'heading' | 'meta' | 'punct'

export interface Token {
  kind: TokenKind
  text: string
}

export type Language = 'json' | 'markdown' | 'shell' | 'python' | 'javascript' | 'openscad' | 'yaml' | 'plain'

const BY_EXTENSION: Record<string, Language> = {
  json: 'json',
  md: 'markdown',
  markdown: 'markdown',
  sh: 'shell',
  bash: 'shell',
  py: 'python',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'javascript',
  scad: 'openscad',
  yaml: 'yaml',
  yml: 'yaml',
}

const BY_MEDIA_TYPE: Record<string, Language> = {
  'application/json': 'json',
  'text/markdown': 'markdown',
  'text/x-shellscript': 'shell',
  'text/x-python': 'python',
  'text/javascript': 'javascript',
  'text/typescript': 'javascript',
  'text/x-openscad': 'openscad',
  'application/yaml': 'yaml',
}

export function languageOf(path: string, mediaType?: string): Language {
  const name = path.split('/').pop() ?? ''
  const dot = name.lastIndexOf('.')
  const byName = dot > 0 ? BY_EXTENSION[name.slice(dot + 1).toLowerCase()] : undefined
  return byName ?? (mediaType ? BY_MEDIA_TYPE[mediaType] : undefined) ?? 'plain'
}

type Rule = [TokenKind, RegExp]

const words = (list: string) => new RegExp(`\\b(?:${list.split(' ').join('|')})\\b`, 'y')
const DQ = /"(?:[^"\\\n]|\\.)*"/y
const SQ = /'(?:[^'\\\n]|\\.)*'/y
const NUMBER = /\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/y
const SLASH_COMMENTS: Rule[] = [
  ['comment', /\/\/.*/y],
  ['comment', /\/\*[\s\S]*?(?:\*\/|$)/y],
]

const RULES: Record<Exclude<Language, 'markdown' | 'plain'>, Rule[]> = {
  json: [
    ['key', /"(?:[^"\\\n]|\\.)*"(?=\s*:)/y],
    ['string', DQ],
    ['number', /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y],
    ['keyword', words('true false null')],
    ['punct', /[{}[\],:]/y],
  ],
  shell: [
    ['comment', /(?<![^\s;])#.*/y],
    ['string', DQ],
    ['string', SQ],
    ['keyword', words('if then else elif fi for while until do done case esac in function return export local set exit')],
    ['number', NUMBER],
  ],
  python: [
    ['comment', /#.*/y],
    ['string', /"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)/y],
    ['string', DQ],
    ['string', SQ],
    [
      'keyword',
      words(
        'def class return if elif else for while in not and or is import from as with try except finally raise pass break continue lambda yield async await None True False',
      ),
    ],
    ['number', NUMBER],
  ],
  javascript: [
    ...SLASH_COMMENTS,
    ['string', DQ],
    ['string', SQ],
    ['string', /`(?:[^`\\]|\\.)*`/y],
    [
      'keyword',
      words(
        'const let var function return if else for while do switch case break continue new class extends import export from default async await try catch finally throw typeof instanceof in of null undefined true false this',
      ),
    ],
    ['number', NUMBER],
  ],
  openscad: [
    ...SLASH_COMMENTS,
    ['string', DQ],
    ['keyword', words('module function include use for if else let each assert echo true false undef')],
    ['number', NUMBER],
  ],
  yaml: [
    ['comment', /(?<![^\s])#.*/y],
    ['key', /(?<=^|\n)[ \t]*(?:- )?[\w.-]+(?=:(?:\s|$))/y],
    ['string', DQ],
    ['string', SQ],
    ['keyword', words('true false null yes no')],
    ['number', NUMBER],
  ],
}

function push(out: Token[], kind: TokenKind, text: string) {
  if (!text) return
  const last = out[out.length - 1]
  if (last && last.kind === kind) last.text += text
  else out.push({ kind, text })
}

function scan(text: string, rules: Rule[], out: Token[] = []): Token[] {
  let i = 0
  while (i < text.length) {
    let matched = false
    for (const [kind, re] of rules) {
      re.lastIndex = i
      const m = re.exec(text)
      if (m && m[0].length > 0) {
        push(out, kind, m[0])
        i += m[0].length
        matched = true
        break
      }
    }
    if (!matched) {
      push(out, 'plain', text[i]!)
      i += 1
    }
  }
  return out
}

const INLINE_MARKDOWN: Rule[] = [['string', /`[^`\n]+`/y]]

function markdown(text: string): Token[] {
  const out: Token[] = []
  const lines = text.split(/(?<=\n)/)
  let i = 0
  // Frontmatter: what Claude Code reads as the skill's settings.
  if (lines[0]?.replace(/\r?\n$/, '') === '---') {
    const end = lines.findIndex((line, n) => n > 0 && line.replace(/\r?\n$/, '') === '---')
    if (end > 0) {
      push(out, 'meta', lines.slice(0, end + 1).join(''))
      i = end + 1
    }
  }
  while (i < lines.length) {
    const line = lines[i]!
    if (/^\s*(```|~~~)/.test(line)) {
      const fence = line.trim().slice(0, 3)
      let end = lines.findIndex((l, n) => n > i && l.trim().startsWith(fence))
      if (end < 0) end = lines.length - 1
      push(out, 'string', lines.slice(i, end + 1).join(''))
      i = end + 1
      continue
    }
    const heading = /^#{1,6}\s.*/.exec(line)
    if (heading) {
      push(out, 'heading', heading[0])
      push(out, 'plain', line.slice(heading[0].length))
    } else {
      scan(line, INLINE_MARKDOWN, out)
    }
    i += 1
  }
  return out
}

export function highlight(text: string, language: Language): Token[] {
  if (language === 'plain') return text ? [{ kind: 'plain', text }] : []
  if (language === 'markdown') return markdown(text)
  return scan(text, RULES[language])
}
