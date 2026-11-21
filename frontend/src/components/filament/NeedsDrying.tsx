import { DropletsIcon } from 'lucide-react'
import type { Spool } from '../../api/filament'
import { Badge } from '@/components/ui/badge'

/** "Needs drying" when the spool has been outside dry storage longer than its material allows (#6268). */
export function NeedsDrying({ spool }: { spool: Pick<Spool, 'needs_drying'> }) {
  if (!spool.needs_drying) return null
  return (
    <Badge variant="outline" className="border-warn/50 font-normal text-warn">
      <DropletsIcon /> Needs drying
    </Badge>
  )
}
