import { LogInIcon, UserIcon } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import { USER_ONLY } from '../../agent/dom'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { openExternal } from '../../lib/embed'
import { CrossSiteFrameError, signIn, signOut, useAuthSession } from '../../lib/signIn'
import { toast } from '../../lib/toast'

/**
 * #4975 — the header's sign-in (spec 2026-11-01 §5.1, §6.2): a Sign in button while the
 * login is on and this browser is signed out, the person's name and Sign out once signed
 * in, and nothing while the login is off.
 */
export function AccountMenu() {
  const session = useAuthSession()
  const [busy, setBusy] = useState(false)
  if (!session) return null

  if (!session.signed_in) {
    if (!session.login_enabled) return null
    return (
      <Button
        variant="ghost"
        size="sm"
        disabled={busy}
        onClick={() => {
          // No await before signIn: the popup must open inside the click.
          setBusy(true)
          signIn()
            .catch((error: Error) =>
              toast(
                error instanceof CrossSiteFrameError
                  ? {
                      title: error.message,
                      actions: [{ label: 'Open in a new tab', onClick: () => openExternal(window.location.href) }],
                    }
                  : { title: error.message },
              ),
            )
            .finally(() => setBusy(false))
        }}
        {...USER_ONLY}
      >
        <LogInIcon aria-hidden="true" />
        Sign in
      </Button>
    )
  }

  const user = session.user
  const label = user?.name ?? user?.email ?? 'Signed in'
  // Spec §9.3: a break-glass session is shown as one, for as long as it lasts.
  const breakGlass = session.kind === 'break_glass'
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          aria-label={breakGlass ? 'Account: break-glass session' : `Account: ${label}`}
          className={breakGlass ? 'text-warn' : undefined}
        >
          <UserIcon aria-hidden="true" />
          <span className="max-w-32 truncate">{breakGlass ? 'Break-glass session' : label}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel className="font-normal">
          <span className="block text-sm">{label}</span>
          {user?.email && user.email !== label && (
            <span className="block text-xs text-muted-foreground">{user.email}</span>
          )}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/settings/system#signed-in-sessions">Signed-in sessions</Link>
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            signOut().catch((error: Error) => toast({ title: error.message }))
          }}
          {...USER_ONLY}
        >
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
