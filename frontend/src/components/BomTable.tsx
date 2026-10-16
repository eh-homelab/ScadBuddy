import type { BomEntry } from '../api/types'

/** A pipeline output's bill of materials (spec 2026-09-27 §5.2). */
export function BomTable({ bom }: { bom: BomEntry[] }) {
  if (bom.length === 0) return null
  return (
    <table className="bom-table">
      <thead>
        <tr>
          <th>Piece</th>
          <th>Count</th>
          <th>Plates</th>
        </tr>
      </thead>
      <tbody>
        {bom.map((entry) => (
          <tr key={entry.piece}>
            <td>{entry.label}</td>
            <td>{entry.count}</td>
            <td>{(entry.plates ?? []).join(', ')}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
