import type { ComponentProps } from 'react'
import { Link, useLocation } from 'react-router'
import type { Spool } from '../../api/filament'
import { spoolPath } from './spoolsQuery'

/** A link to a spool's page that remembers the list's filters (`location.search`), so the page's back link returns to them. */
export function SpoolLink({ spool, ...props }: { spool: Pick<Spool, 'id'> } & Omit<ComponentProps<typeof Link>, 'to' | 'state'>) {
  const location = useLocation()
  return <Link {...props} to={spoolPath(spool)} state={{ back: location.search }} />
}
