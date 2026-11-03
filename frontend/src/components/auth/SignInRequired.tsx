import { LogInIcon } from 'lucide-react'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { USER_ONLY } from '../../agent/dom'
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { isEmbedded, openExternal } from '../../lib/embed'
import { CrossSiteFrameError, signIn, useAuthSession, wasSignedIn } from '../../lib/signIn'
import { isSignedOut, subscribeSignedOut } from '../../lib/signedOut'
import { toast } from '../../lib/toast'

/**
 * #4975 phase 8 — signed out under SCADBUDDY_AUTH_MODE=enforce (spec 2026-11-01 §8.2): a
 * 401 from the gate, a socket closed with 4401, or the first session read. A page that
 * never was signed in, in a tab of its own, goes straight to the sign-in and comes back here
 * (`return_to`, §5.1). Otherwise the page stays as it is under this dialog, so nothing typed
 * is lost: its Sign in is the popup in a tab too, which needs the click.
 */
export function SignInRequired() {
  const signedOut = useSyncExternalStore(subscribeSignedOut, isSignedOut)
  const session = useAuthSession()
  const [busy, setBusy] = useState(false)
  const loginEnabled = session?.login_enabled === true
  const straightAway = signedOut && loginEnabled && !wasSignedIn() && !isEmbedded()

  useEffect(() => {
    if (straightAway) void signIn(false)
  }, [straightAway])

  if (!signedOut || straightAway || session === null) return null
  return (
    <AlertDialog open>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle>Sign in to continue</AlertDialogTitle>
          <AlertDialogDescription>
            {loginEnabled
              ? 'ScadBuddy needs you signed in. This page stays as it is while you sign in.'
              : 'ScadBuddy needs you signed in, and sign-in is not set up. Whoever runs ScadBuddy can give you a break-glass link.'}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {loginEnabled && (
          <AlertDialogFooter>
            <Button
              disabled={busy}
              onClick={() => {
                // Always the popup, so this page and what is unsaved on it stay; no await
                // before it, so the popup opens inside the click.
                setBusy(true)
                signIn(true)
                  .catch((error: Error) =>
                    toast(
                      error instanceof CrossSiteFrameError
                        ? {
                            title: error.message,
                            actions: [
                              { label: 'Open in a new tab', onClick: () => openExternal(window.location.href) },
                            ],
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
          </AlertDialogFooter>
        )}
      </AlertDialogContent>
    </AlertDialog>
  )
}
