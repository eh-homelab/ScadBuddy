import { Navigate, Route, Routes } from 'react-router'
import { AppShell } from './components/AppShell'
import { CataloguePage } from './pages/CataloguePage'
import { CustomizePage } from './pages/CustomizePage'
import { EditSourcePage } from './pages/EditSourcePage'
import { HistoryPage } from './pages/HistoryPage'
import { NewModelPage } from './pages/NewModelPage'
import { SettingsPage } from './pages/SettingsPage'

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<CataloguePage />} />
        <Route path="new" element={<NewModelPage />} />
        <Route path="m/:slug" element={<CustomizePage />} />
        <Route path="m/:slug/source" element={<EditSourcePage />} />
        <Route path="m/:slug/history" element={<HistoryPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
