# Task 7 fix round 1
- test: entity_link_add run through runTool parks as pending_approval (msw onUnhandledFrame=error proves no backend call).
- test: entity_not_found asserts ToolError, status 404, problem entity_not_found.
- entities.ts: entity description lists spool_type.
Command: cd agent && pnpm typecheck && pnpm exec vitest run test/entityTools.test.ts test/coverage.test.ts
Output: typecheck clean; Test Files 2 passed (2), Tests 19 passed (19).
