import { matchPath } from 'react-router'
import type { PageContext } from './protocol'

interface RoutePrompts {
  pattern: string
  prompts: string[]
}

// Most specific first: the first pattern that matches wins. The customizer's three
// are the examples #256 gives.
const ROUTES: RoutePrompts[] = [
  {
    pattern: '/m/:slug/source',
    prompts: ['Explain what this code does', 'Why does this fail to render?', 'Add a parameter for the wall thickness'],
  },
  {
    pattern: '/m/:slug/versions',
    prompts: ['What changed in the latest version?', 'Which version should I go back to?'],
  },
  {
    pattern: '/m/:slug/history',
    prompts: ['Summarise what I have printed from this model'],
  },
  {
    pattern: '/m/:slug',
    prompts: ['Explain these settings', 'Make it fit the A1 mini plate', 'Why is this preview empty?'],
  },
  {
    pattern: '/edit/:outputId',
    prompts: ['What did I change from the defaults?'],
  },
  {
    pattern: '/new',
    prompts: ['Write a parametric cable clip', 'Help me turn a sketch into a model'],
  },
  {
    pattern: '/settings',
    prompts: ['Help me connect Bambuddy', 'What does each setting do?'],
  },
  {
    pattern: '/',
    prompts: ['Find a model for a name tag', 'What can I print in two colours?'],
  },
]

export function suggestedPrompts(pathname: string): string[] {
  return ROUTES.find((r) => matchPath({ path: r.pattern, end: true }, pathname))?.prompts ?? []
}

/**
 * What the agent is told about the page with each turn (#256 "page context").
 *
 * Seam for the browser bridge (#254, in PR #339): once it lands, the bridge's page
 * snapshot (live handlers, selected model, focused control) should be merged in here
 * rather than the panel talking to the bridge directly. Until then the route is all
 * the panel knows.
 */
export function pageContext(pathname: string): PageContext {
  const match =
    matchPath({ path: '/m/:slug/*', end: false }, pathname) ??
    matchPath({ path: '/m/:slug', end: true }, pathname)
  const slug = match?.params.slug
  return slug ? { route: pathname, modelSlug: slug } : { route: pathname }
}
