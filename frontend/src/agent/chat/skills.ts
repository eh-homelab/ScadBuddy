import { isBuiltIn, type ListedPackage } from '../../api/aiPlugins'

// The composer's "/" skill menu (#1920). The skills come from the plugin list Settings
// shows (GET /api/v1/ai/plugin-packages, agent routes/pluginPackages.ts): each review
// names its skills as Claude Code does, `<plugin>:<skill>`, and a message that starts
// with `/<plugin>:<skill>` runs that skill (plugins/scadbuddy/README.md).

export interface SkillChoice {
  /** `<plugin>:<skill>`, as the package review lists it. */
  name: string
  plugin: string
}

/** What follows a "/" that starts the draft, while it is still one word; otherwise null. */
export function slashQuery(draft: string): string | null {
  const match = /^\/(\S*)$/.exec(draft)
  return match ? (match[1] ?? '') : null
}

/**
 * The skills a session's turns load: an enabled built-in's, and an enabled (so approved)
 * package's. A package stored under a built-in's name is never loaded (agent install.ts
 * `loadPackagesForRun`).
 */
export function loadedSkills(packages: readonly ListedPackage[]): SkillChoice[] {
  const builtInNames = new Set(packages.filter(isBuiltIn).map((p) => p.name))
  return packages
    .filter((p) => p.enabled && p.approved && (isBuiltIn(p) || !builtInNames.has(p.name)))
    .flatMap((p) => p.review.skills.map((name) => ({ name, plugin: p.name })))
}

/**
 * The skills whose full name starts with `query`, or whose own part (after the plugin's
 * `:`) holds it, ignoring case: prefixes first, so "/pr" finds `scadbuddy:print` and "/c"
 * does not find every `scadbuddy:` skill.
 */
export function matchingSkills(skills: readonly SkillChoice[], query: string): SkillChoice[] {
  const q = query.toLowerCase()
  const rank = (s: SkillChoice): number => {
    const name = s.name.toLowerCase()
    const own = name.slice(name.indexOf(':') + 1)
    if (name.startsWith(q)) return 0
    if (own.startsWith(q)) return 1
    return own.includes(q) ? 2 : 3
  }
  const ranked = skills.map((s) => ({ s, r: rank(s) })).filter(({ r }) => r < 3)
  return [0, 1, 2].flatMap((r) => ranked.filter((x) => x.r === r).map((x) => x.s))
}

/** The text a chosen skill puts in the composer. */
export function skillInvocation(skill: SkillChoice): string {
  return `/${skill.name} `
}
