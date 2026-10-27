import { useSyncExternalStore } from 'react'
import { Link } from 'react-router'
import { XIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { dismissToast, getToasts, holdToast, releaseToast, subscribeToasts } from '../../lib/toast'

export function Toasts() {
  const shown = useSyncExternalStore(subscribeToasts, getToasts)
  return (
    // The one live region, always mounted so a toast added to it is announced; the toasts
    // inside are plain, or each would be announced twice. A bare `aria-live`, not
    // role="status": an always-present empty status would be one more on every page.
    <div
      aria-live="polite"
      data-testid="toasts"
      className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
    >
      {shown.map((one) => (
        <div
          key={one.id}
          onMouseEnter={() => holdToast(one.id, 'hover')}
          onMouseLeave={() => releaseToast(one.id, 'hover')}
          onFocus={() => holdToast(one.id, 'focus')}
          onBlur={() => releaseToast(one.id, 'focus')}
          className="pointer-events-auto flex items-center gap-2 rounded-xl bg-popover py-2 pr-2 pl-4 text-sm text-popover-foreground shadow-lg ring-1 ring-foreground/10 duration-200 animate-in fade-in-0 slide-in-from-bottom-2"
        >
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            {one.href ? (
              <Link
                to={one.href}
                className="truncate font-medium underline-offset-4 hover:underline"
                onClick={() => dismissToast(one.id)}
              >
                {one.title}
              </Link>
            ) : (
              <span className="truncate font-medium">{one.title}</span>
            )}
            {one.detail && <span className="line-clamp-3 text-xs break-words text-muted-foreground">{one.detail}</span>}
            {one.actions && one.actions.length > 0 && (
              <div className="flex gap-2">
                {one.actions.map((action, i) => {
                  const act = () => {
                    dismissToast(one.id)
                    action.onClick?.()
                  }
                  return (
                    <Button
                      key={i}
                      size="xs"
                      variant={action.destructive ? 'destructive' : action.href ? 'outline' : 'default'}
                      // An action a person must take (an approval), never the browser bridge (#254).
                      {...(action.userOnly ? { 'data-agent-user-only': '' } : {})}
                      {...(action.href ? { asChild: true } : { onClick: act })}
                    >
                      {action.href ? (
                        <Link to={action.href} onClick={act}>
                          {action.label}
                        </Link>
                      ) : (
                        action.label
                      )}
                    </Button>
                  )
                })}
              </div>
            )}
          </div>
          <Button variant="ghost" size="icon-xs" aria-label="Dismiss" onClick={() => dismissToast(one.id)}>
            <XIcon aria-hidden="true" />
          </Button>
        </div>
      ))}
    </div>
  )
}
