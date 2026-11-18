import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'

// Mirrors frontend/eslint.config.js without the React plugins: the same
// recommended sets and the same two house rules.
export default tseslint.config(
  { ignores: ['dist', 'coverage', 'src/api/schema.d.ts'] },
  js.configs.recommended,
  tseslint.configs.recommended,
  // The gen-api hook both packages share lives at the repo root, where no package's
  // lint reaches by itself (#6671). `pnpm lint` lints it from the root with this file.
  {
    files: ['scripts/gen-api-request-defaults.mjs'],
    ...js.configs.recommended,
    languageOptions: { ecmaVersion: 2024, sourceType: 'module', globals: { ...globals.node } },
  },
  {
    files: ['**/*.ts'],
    languageOptions: {
      ecmaVersion: 2024,
      globals: { ...globals.node },
    },
    rules: {
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
)
