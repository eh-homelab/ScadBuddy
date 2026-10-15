import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router'
import { AppShell } from './components/AppShell'
import { ActivityPage } from './pages/ActivityPage'
import { CataloguePage } from './pages/CataloguePage'

// #2356 — every page but the catalogue is its own chunk, so the shell's entry chunk
// carries no page's dependencies: three.js only loads with a page that draws in 3D.
// The shell's Suspense (`AppShell`) shows while a page's chunk loads: on a first visit.
// A navigation does not blank the page it leaves, as `BrowserRouter` applies a location
// change inside `startTransition` (react-router 8, unless `useTransitions={false}`), so
// React keeps the old page up until the next one's chunk is in.
const CustomizePage = lazy(async () => ({ default: (await import('./pages/CustomizePage')).CustomizePage }))
const EditPage = lazy(async () => ({ default: (await import('./pages/EditPage')).EditPage }))
const FlowPage = lazy(async () => ({ default: (await import('./pages/FlowPage')).FlowPage }))
const HistoryPage = lazy(async () => ({ default: (await import('./pages/HistoryPage')).HistoryPage }))
const LibraryPage = lazy(async () => ({ default: (await import('./pages/LibraryPage')).LibraryPage }))
const PrintDetailPage = lazy(async () => ({ default: (await import('./pages/PrintDetailPage')).PrintDetailPage }))
// Two pages of one module, so one chunk.
const loadPrints = () => import('./pages/PrintsPage')
const PrintsPage = lazy(async () => ({ default: (await loadPrints()).PrintsPage }))
const TemplatePrintsPage = lazy(async () => ({ default: (await loadPrints()).TemplatePrintsPage }))
const SettingsPage = lazy(async () => ({ default: (await import('./pages/SettingsPage')).SettingsPage }))
const VersionsPage = lazy(async () => ({ default: (await import('./pages/VersionsPage')).VersionsPage }))
const WorkflowRunPage = lazy(async () => ({ default: (await import('./pages/WorkflowRunPage')).WorkflowRunPage }))
const WorkflowsPage = lazy(async () => ({ default: (await import('./pages/WorkflowsPage')).WorkflowsPage }))
// #3653 — the component gallery, in the mocked build only: a production bundle has neither
// the route nor the chunk.
const UiGalleryPage =
  import.meta.env.VITE_MOCK_API === '1'
    ? lazy(async () => ({ default: (await import('./pages/UiGalleryPage')).UiGalleryPage }))
    : null

// Monaco is the biggest thing in the bundle and only these two routes want it, so
// they are split out: opening the customizer never downloads an editor. A flow's page
// (#2487) loads the same chunk for its editor and diff only (`FlowPage`'s own lazy imports).
const NewModelPage = lazy(async () => ({
  default: (await import('./pages/NewModelPage')).NewModelPage,
}))
const EditSourcePage = lazy(async () => ({
  default: (await import('./pages/EditSourcePage')).EditSourcePage,
}))
const EditSourceFilePage = lazy(async () => ({
  default: (await import('./pages/EditSourceFilePage')).EditSourceFilePage,
}))

function Editing({ children }: { children: ReactNode }) {
  return (
    <Suspense
      fallback={
        <p className="flex h-full items-center justify-center text-[13px] text-muted">
          Loading the editor
        </p>
      }
    >
      {children}
    </Suspense>
  )
}

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<CataloguePage />} />
        <Route
          path="new"
          element={
            <Editing>
              <NewModelPage />
            </Editing>
          }
        />
        <Route path="m/:slug" element={<CustomizePage />} />
        <Route
          path="m/:slug/source"
          element={
            <Editing>
              <EditSourcePage />
            </Editing>
          }
        />
        <Route
          path="m/:slug/source/:file"
          element={
            <Editing>
              <EditSourceFilePage />
            </Editing>
          }
        />
        <Route path="m/:slug/history" element={<HistoryPage />} />
        <Route path="m/:slug/versions" element={<VersionsPage />} />
        <Route path="m/:slug/prints" element={<TemplatePrintsPage />} />
        <Route path="prints" element={<PrintsPage />} />
        <Route path="edit/:outputId" element={<EditPage />} />
        <Route path="prints/:archiveId" element={<PrintDetailPage />} />
        <Route path="library/*" element={<LibraryPage />} />
        <Route path="workflows" element={<WorkflowsPage />} />
        <Route path="workflows/flows" element={<WorkflowsPage tab="flows" />} />
        <Route path="workflows/flows/:id" element={<FlowPage />} />
        <Route path="workflows/runs/:id" element={<WorkflowRunPage />} />
        <Route path="activity" element={<ActivityPage />} />
        <Route path="settings" element={<SettingsPage />} />
        {UiGalleryPage && <Route path="ui-gallery" element={<UiGalleryPage />} />}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
