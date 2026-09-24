import { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor/editor/editor.api'
import EditorWorker from 'monaco-editor/editor/editor.worker?worker'

// `editor.api` is the editor WITHOUT monaco's forty bundled languages — none of them
// is OpenSCAD, and pulling `monaco-editor`'s default entry would ship every one. The
// contributions this editor actually uses are then imported by hand.
import 'monaco-editor/editor/contrib/bracketMatching/browser/bracketMatching.js'
import 'monaco-editor/editor/contrib/comment/browser/comment.js'
import 'monaco-editor/editor/contrib/find/browser/findController.js'
import 'monaco-editor/editor/contrib/folding/browser/folding.js'
import 'monaco-editor/editor/contrib/hover/browser/hoverContribution.js'
import 'monaco-editor/editor/contrib/linesOperations/browser/linesOperations.js'
import 'monaco-editor/editor/contrib/wordOperations/browser/wordOperations.js'

import { registerOpenscad } from './openscadLanguage'

export const OPENSCAD_LANGUAGE_ID = 'openscad'

/**
 * Monaco is bundled, never fetched from a CDN: `@monaco-editor/react`'s default
 * loader pulls the whole editor off jsdelivr at runtime, which a LAN-only app behind
 * the UDM firewall cannot rely on. `loader.config({ monaco })` hands it the instance
 * Vite already built instead, and `MonacoEnvironment` points the editor worker at
 * Vite's own worker build rather than a URL on the page's origin.
 */
let configured = false

export function setupMonaco(): typeof monaco {
  if (configured) return monaco
  configured = true
  window.MonacoEnvironment = { getWorker: () => new EditorWorker() }
  registerOpenscad(monaco)
  loader.config({ monaco })
  return monaco
}

export { monaco }
