import { screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderPage } from '../../test/utils'
import { DataTable, type Column } from './DataTable'

type Row = { id: string; name: string; total: number }

const columns: Column<Row>[] = [
  {
    key: 'name',
    header: 'Name',
    cell: (row, open) => (
      <button type="button" onClick={open}>
        {row.name}
      </button>
    ),
  },
  { key: 'total', header: 'Total', cell: (row) => row.total, className: (row) => (row.total < 0 ? 'text-warn' : '') },
]

describe('DataTable (#4249)', () => {
  it('renders a column per spec, a row per record, and opens one', async () => {
    const onOpen = vi.fn()
    const { user } = renderPage(
      <DataTable
        label="Orders"
        columns={columns}
        rows={[
          { id: 'a', name: 'Ada', total: 5 },
          { id: 'b', name: 'Bob', total: -1 },
        ]}
        rowKey={(row) => row.id}
        rowTestId={(row) => `order-row-${row.id}`}
        onOpen={onOpen}
      />,
    )
    const table = screen.getByRole('table', { name: 'Orders' })
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Name', 'Total'])
    expect(screen.getByTestId('order-row-b')).toHaveTextContent('Bob')
    expect(within(screen.getByTestId('order-row-b')).getAllByRole('cell')[1]).toHaveClass('text-warn')
    await user.click(within(screen.getByTestId('order-row-a')).getByRole('button', { name: 'Ada' }))
    expect(onOpen).toHaveBeenCalledWith({ id: 'a', name: 'Ada', total: 5 })
  })

  it('shows the empty text in place of the table', () => {
    renderPage(<DataTable label="Orders" columns={columns} rows={[]} rowKey={(row) => row.id} empty="No orders yet." />)
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.getByText('No orders yet.')).toBeInTheDocument()
  })
})
