import { NavLink, Outlet } from 'react-router'
import { isEmbedded } from '../lib/embed'

const NAV = [
  { to: '/', label: 'Models', end: true },
  { to: '/settings', label: 'Settings', end: false },
]

export function AppShell({ embedded = isEmbedded() }: { embedded?: boolean }) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-bg text-ink">
      <header
        className={`flex shrink-0 items-center gap-5 border-b border-line bg-surface px-4 ${
          embedded ? 'h-10' : 'h-14'
        }`}
        data-embedded={embedded ? 'true' : 'false'}
      >
        <NavLink to="/" className="flex items-baseline gap-1.5 shrink-0" aria-label="ScadBuddy">
          <span className={`font-semibold tracking-tight ${embedded ? 'text-[13px]' : 'text-base'}`}>
            Scad<span className="text-accent">Buddy</span>
          </span>
          {!embedded && (
            <span className="text-[11px] text-faint">OpenSCAD customizer</span>
          )}
        </NavLink>

        <nav className="flex items-center gap-0.5" aria-label="Main">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `rounded-[6px] px-2.5 py-1 text-[13px] transition-colors ${
                  isActive
                    ? 'bg-surface-3 text-ink'
                    : 'text-muted hover:bg-surface-2 hover:text-ink'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>

      <main className="min-h-0 flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  )
}
