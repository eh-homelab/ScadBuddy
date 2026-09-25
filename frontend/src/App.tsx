import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router'
import { AppShell } from './components/AppShell'
import { CataloguePage } from './pages/CataloguePage'
import { CustomizePage } from './pages/CustomizePage'
import { EditPage } from './pages/EditPage'
import { HistoryPage } from './pages/HistoryPage'
import { SettingsPage } from './pages/SettingsPage'

// Monaco is the biggest thing in the bundle and only these two routes want it, so
// they are split out: opening the customizer never downloads an editor.
const NewModelPage = lazy(async () => ({
  default: (await import('./pages/NewModelPage')).NewModelPage,
}))
const EditSourcePage = lazy(async () => ({
  default: (await import('./pages/EditSourcePage')).EditSourcePage,
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
        <Route path="m/:slug/history" element={<HistoryPage />} />
        <Route path="edit/:outputId" element={<EditPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
