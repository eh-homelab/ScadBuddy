import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

const BACK_OFFICE = 'The storefront imports nothing from the back office (spec §5.1)'
// `no-restricted-imports` reads gitignore patterns, which have no `{a,b}`: one per folder.
const BACK_OFFICE_COMPONENTS = [
  'orders', 'settings', 'library', 'print', 'prints', 'activity', 'assistant', 'flows', 'auth',
  'records', 'media', 'workspace', 'audits', 'capabilities', 'storefront',
].map((folder) => `**/components/${folder}/**`)

/**
 * `no-restricted-imports` for the storefront's files at `files`. `ownPages` and
 * `ownClient` re-allow the storefront's own `pages/` and `api/client` as written there.
 */
function storefrontImports(files, ownPages, ownClient) {
  return {
    files: [files],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            { group: ['**/pages/**', ...ownPages], message: BACK_OFFICE },
            { group: ['**/api/client', ...ownClient], message: BACK_OFFICE },
            { group: BACK_OFFICE_COMPONENTS, message: BACK_OFFICE },
            { group: ['**/agent/**'], message: BACK_OFFICE },
          ],
        },
      ],
    },
  }
}

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'playwright-report', 'test-results', 'public'] },
  js.configs.recommended,
  tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser, ...globals.node },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // ScadBuddy syncs React state to things React does not own: a poll-driven
      // render job, a schema that arrives over the network, a settings form seeded
      // from the server. Those are effect-shaped, and the rule has no exception for
      // them short of adopting a query library.
      'react-hooks/set-state-in-effect': 'off',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/consistent-type-imports': [
        'error',
        { prefer: 'type-imports', fixStyle: 'inline-type-imports' },
      ],
    },
  },
  // The public storefront page (spec 2026-11-02 §5.1) imports nothing from the back
  // office: not its pages, its API client, its feature components or the agent. It may
  // share components/Preview.tsx, components/widgets and lib/. The patterns read the
  // import's text, not the file it resolves to, so `../api/client` is the storefront's
  // own client one folder down and the back office's at the top: one block per depth.
  storefrontImports('src/storefront/*.{ts,tsx}', ['!./pages/**'], ['!./api/client']),
  storefrontImports('src/storefront/*/**/*.{ts,tsx}', ['!../pages/**'], ['!../api/client', '!./client']),
  {
    // Copies of bundled template UIs (models/*/ui/*.js): plain browser modules.
    files: ['src/mocks/ui/**/*.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module', globals: globals.browser },
  },
)
