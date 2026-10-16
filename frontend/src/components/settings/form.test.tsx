import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { ChoiceSelect, SuggestInput } from './form'

function Suggest() {
  const [value, setValue] = useState('')
  return (
    <SuggestInput
      aria-label="Model"
      value={value}
      onChange={(event) => setValue(event.target.value)}
      onPick={setValue}
      suggestionsLabel="Suggested models"
      suggestions={[{ value: 'claude-opus' }, { value: 'claude-sonnet' }, { value: 'claude-haiku' }]}
    />
  )
}

const options = () => screen.getAllByRole('option').map((option) => option.textContent)

describe('SuggestInput (#3671)', () => {
  it('narrows the suggestions to what is typed, as a datalist did', async () => {
    const user = userEvent.setup()
    render(<Suggest />)
    await user.click(screen.getByRole('button', { name: 'Suggested models' }))
    expect(options()).toEqual(['claude-opus', 'claude-sonnet', 'claude-haiku'])
    await user.keyboard('{Escape}')

    await user.type(screen.getByRole('combobox', { name: 'Model' }), 'SON')
    await user.click(screen.getByRole('button', { name: 'Suggested models' }))
    expect(options()).toEqual(['claude-sonnet'])

    // A picked value lists them all again, so another can be picked.
    await user.click(screen.getByRole('option', { name: 'claude-sonnet' }))
    expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('claude-sonnet')
    await user.click(screen.getByRole('button', { name: 'Suggested models' }))
    expect(options()).toHaveLength(3)
  })

  it('says when nothing matches', async () => {
    const user = userEvent.setup()
    render(<Suggest />)
    await user.type(screen.getByRole('combobox', { name: 'Model' }), 'gpt')
    await user.click(screen.getByRole('button', { name: 'Suggested models' }))
    expect(screen.getByText('No suggestion matches.')).toBeInTheDocument()
  })
})

describe('ChoiceSelect (#3671)', () => {
  it('shows a value its choices do not name yet, rather than an empty trigger', () => {
    render(
      <ChoiceSelect
        aria-label="Printer"
        value="7"
        onValueChange={() => undefined}
        choices={[
          { value: '', label: 'None' },
          { value: '1', label: 'X1C' },
        ]}
      />,
    )
    expect(screen.getByRole('combobox', { name: 'Printer' })).toHaveTextContent('7')
  })
})
