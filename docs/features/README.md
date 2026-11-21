# Features

One page per user-visible feature, each with what it does, how a request moves through
the processes, the key code, its data, its tests and its open issues. The list comes from
the back office's routes (`frontend/src/App.tsx:77-127`), the storefront's
(`frontend/src/storefront/App.tsx:86-97`) and the API's routers. How the system fits
together is in [`docs/architecture/`](../architecture/README.md); the person's view is
the [user guide](../user-guide.md).

| Feature | Doc | Main code entrypoint |
|---|---|---|
| Catalogue, adding, duplicating and deleting models | [catalogue-and-models.md](catalogue-and-models.md) | `frontend/src/pages/CataloguePage.tsx`, `backend/scadbuddy/api/models.py` |
| Customizer, parameters, presets, colours and fonts | [customizer-and-parameters.md](customizer-and-parameters.md) | `frontend/src/pages/CustomizePage.tsx`, `backend/scadbuddy/render/schema.py` |
| Preview, render, 3MF export and Send | [preview-and-3mf.md](preview-and-3mf.md) | `backend/scadbuddy/api/jobs.py:401`, `backend/scadbuddy/workflows/pipelines.py:524` |
| Source editing and libraries | [source-editing.md](source-editing.md) | `frontend/src/components/SourceEditor.tsx:105`, `backend/scadbuddy/api/lsp.py:74` |
| Template-owned UIs and pipelines | [template-uis-and-pipelines.md](template-uis-and-pipelines.md) | `frontend/src/template-ui/TemplateUi.tsx:63`, `backend/scadbuddy/workflows/ctx.py` |
| Mesh models | [mesh-models.md](mesh-models.md) | `backend/scadbuddy/api/mesh_models.py:143` |
| Arrange and plates | [arrange-and-plates.md](arrange-and-plates.md) | `backend/scadbuddy/api/outputs.py:728` |
| Print dialog, slicing and `process_overrides` | [print-dialog-and-slicing.md](print-dialog-and-slicing.md) | `frontend/src/components/PrintPicker.tsx:329`, `backend/scadbuddy/api/printing.py:928` |
| Printers and the native hot path | [printer-hot-path.md](printer-hot-path.md) | `backend/scadbuddy/printers/hot_path.py:32`, `backend/scadbuddy/printers/print_activities.py:307` |
| Print library, search, copies and batches | [print-library.md](print-library.md) | `frontend/src/pages/LibraryPage.tsx`, `backend/scadbuddy/api/library_print.py:194` |
| Prints, history and the print watcher | [print-history-and-watcher.md](print-history-and-watcher.md) | `frontend/src/pages/PrintsPage.tsx:38`, `backend/scadbuddy/print_watch/sweep.py` |
| Spools and filament | [spools-and-filament.md](spools-and-filament.md) | `backend/scadbuddy/api/filament.py:45`, `backend/scadbuddy/spools/overlay.py` |
| Versions and history | [versions-and-history.md](versions-and-history.md) | `frontend/src/pages/VersionsPage.tsx`, `backend/scadbuddy/api/versions.py:54` |
| Orders, quotes, pricing and job cost | [orders-and-quotes.md](orders-and-quotes.md) | `backend/scadbuddy/api/orders.py:187`, `backend/scadbuddy/orders/lifecycle.py:607` |
| Storefront and payments | [storefront-and-payments.md](storefront-and-payments.md) | `backend/scadbuddy/storefront/__main__.py`, `backend/scadbuddy/api/payment_webhooks.py:58` |
| MakerWorld import | [makerworld-import.md](makerworld-import.md) | `backend/scadbuddy/api/makerworld.py:126` |
| Flows (workflows) | [flows.md](flows.md) | `backend/scadbuddy/api/flows.py:123`, `backend/scadbuddy/workflows/project.py:126` |
| Activity | [activity.md](activity.md) | `backend/scadbuddy/api/activity.py:46` |
| The AI assistant | [ai-assistant.md](ai-assistant.md) | `agent/src/routes/chat.ts:76`, `frontend/src/components/assistant/AssistantChat.tsx:154` |
| Claude plugin and MCP | [claude-plugin-and-mcp.md](claude-plugin-and-mcp.md) | `agent/src/mcp/http.ts:446`, `plugins/scadbuddy/` |
| Settings, sign-in and access tokens | [settings-and-sign-in.md](settings-and-sign-in.md) | `frontend/src/pages/SettingsPage.tsx`, `backend/scadbuddy/api/auth.py:50` |
