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
      // A task queue's pollers are read on every partition (#7306, agent/CLAUDE.md
      // "Poller reads"): only temporal/pollers.ts names DescribeTaskQueue. Any reference
      // is refused (a name, a string or a template key), so no call gets past by never
      // naming it as the callee (#7439); only an object literal's key is allowed, which is
      // how a unit test's stub defines it (test/lintRules.test.ts).
      'no-restricted-syntax': [
        'error',
        {
          selector: [
            "Identifier[name='describeTaskQueue']:not(ObjectExpression > Property > Identifier.key)",
            "Literal[value='describeTaskQueue']:not(ObjectExpression > Property > Literal.key)",
            "TemplateLiteral[expressions.length=0][quasis.0.value.cooked='describeTaskQueue']",
          ].join(', '),
          message: 'Read pollers with queuePollers (src/temporal/pollers.ts): DescribeTaskQueue reports one partition (#7306).',
        },
      ],
    },
  },
  {
    files: ['src/temporal/pollers.ts'],
    rules: { 'no-restricted-syntax': 'off' },
  },
)
