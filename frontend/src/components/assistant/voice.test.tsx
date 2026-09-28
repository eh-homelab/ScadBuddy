import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { bridge } from '../../agent/bridge'
import type { ClientMessage } from '../../agent/chat/protocol'
import { SPEAK_REPLIES_KEY, setSpeakReplies, spokenText } from '../../agent/chat/voice'
import { createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { renderPage } from '../../test/utils'
import type * as embed from '../../lib/embed'
import { AppShell } from '../AppShell'

vi.mock('../../agent/chat/availability', () => ({
  useAiAvailability: () => ({ available: true }),
}))

const openExternal = vi.hoisted(() => vi.fn())
vi.mock('../../lib/embed', async (importActual) => ({
  ...(await importActual<typeof embed>()),
  openExternal,
}))

/** A stand-in for Chrome's `webkitSpeechRecognition`. */
class FakeRecognition {
  static instances: FakeRecognition[] = []
  lang = ''
  continuous = false
  interimResults = false
  onresult: ((event: unknown) => void) | null = null
  onerror: ((event: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn(() => this.onend?.())
  abort = vi.fn()
  constructor() {
    FakeRecognition.instances.push(this)
  }
  /** Results so far in this session: `[text, isFinal]`. */
  hear(...results: Array<[string, boolean]>) {
    act(() => {
      this.onresult?.({
        resultIndex: 0,
        results: results.map(([transcript, isFinal]) => ({ isFinal, 0: { transcript } })),
      })
    })
  }
  fail(error: string) {
    act(() => {
      this.onerror?.({ error })
      this.onend?.()
    })
  }
}

class FakeUtterance {
  onend: (() => void) | null = null
  onerror: (() => void) | null = null
  lang = ''
  text: string
  constructor(text: string) {
    this.text = text
  }
}

const synth = { speak: vi.fn(), cancel: vi.fn() }

function installSpeech({ recognition = true, synthesis = true } = {}) {
  if (recognition) vi.stubGlobal('webkitSpeechRecognition', FakeRecognition)
  if (synthesis) {
    vi.stubGlobal('speechSynthesis', synth)
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
  }
}

let agent: MockAgentTransport
const factory = () => {
  agent = createMockAgentTransport({ stepMs: 0 })
  return agent
}

async function openPanel({ embedded = false } = {}) {
  const view = renderPage(
    <Routes>
      <Route element={<AppShell embedded={embedded} assistantTransport={factory} />}>
        <Route path="*" element={<p>page</p>} />
      </Route>
    </Routes>,
    { route: '/m/name-keychain' },
  )
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  await screen.findByRole('textbox', { name: 'Message the assistant' })
  return view
}

const userMessages = () => agent.sent.filter((m): m is Extract<ClientMessage, { type: 'user.message' }> => m.type === 'user.message')
const composer = () => screen.getByRole('textbox', { name: 'Message the assistant' })

beforeEach(() => {
  FakeRecognition.instances = []
  synth.speak.mockReset()
  synth.cancel.mockReset()
  openExternal.mockReset()
  window.localStorage.clear()
  setSpeakReplies(false)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  delete (document as unknown as Record<string, unknown>).permissionsPolicy
})

describe('voice feature detection', () => {
  it('hides the mic and the spoken-replies toggle when the browser has neither API', async () => {
    await openPanel()
    expect(screen.queryByRole('button', { name: 'Voice input' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Read replies aloud' })).not.toBeInTheDocument()
  })

  it('shows each control only for the API that exists', async () => {
    installSpeech({ recognition: false })
    await openPanel()
    expect(screen.queryByRole('button', { name: 'Voice input' })).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Read replies aloud' })).toBeInTheDocument()
  })

  it('says, by the mic, that the browser may send the audio to its servers', async () => {
    installSpeech()
    await openPanel()
    const note = screen.getByText(/Voice input is transcribed by your browser/)
    expect(note).toBeVisible()
    expect(note).toHaveTextContent('may send the audio to its maker’s servers (Chrome does)')
    expect(screen.getByRole('button', { name: 'Voice input' })).toHaveAccessibleDescription(note.textContent!)
  })

  it('has no audio note where there is no speech recognition', async () => {
    installSpeech({ recognition: false })
    await openPanel()
    expect(screen.queryByText(/Voice input is transcribed by your browser/)).not.toBeInTheDocument()
  })

  it('marks the mic and the voice setting user-only', async () => {
    installSpeech()
    await openPanel()
    expect(screen.getByRole('button', { name: 'Voice input' })).toHaveAttribute('data-agent-user-only')
    expect(screen.getByRole('checkbox', { name: 'Read replies aloud' })).toHaveAttribute('data-agent-user-only')
  })
})

describe('speech to text', () => {
  it('writes the transcript into the composer and never sends it', async () => {
    installSpeech()
    const { user } = await openPanel()
    await user.type(composer(), 'Please')
    const mic = screen.getByRole('button', { name: 'Voice input' })
    await user.click(mic)
    expect(mic).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Listening. Speak, then stop the microphone.')).toBeInTheDocument()

    const rec = FakeRecognition.instances[0]!
    expect(rec.start).toHaveBeenCalledOnce()
    expect(rec.interimResults).toBe(true)
    rec.hear(['make the name', false])
    expect(composer()).toHaveValue('Please make the name')
    rec.hear(['make the name Reagan', true], [' and the base blue', false])
    expect(composer()).toHaveValue('Please make the name Reagan and the base blue')

    await user.click(mic)
    expect(rec.stop).toHaveBeenCalledOnce()
    expect(mic).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText('Stopped listening. Review the message, then send it.')).toBeInTheDocument()
    await waitFor(() => expect(composer()).toHaveFocus())

    // Nothing leaves until the user presses Send.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    expect(userMessages()).toEqual([])
    await user.click(screen.getByRole('button', { name: 'Send' }))
    expect(userMessages()[0]).toMatchObject({ text: 'Please make the name Reagan and the base blue' })
  })

  it('keeps what the user types while dictation is still listening', async () => {
    installSpeech()
    const { user } = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Voice input' }))
    const rec = FakeRecognition.instances[0]!
    rec.hear(['make the name', true])
    expect(composer()).toHaveValue('make the name')

    await user.type(composer(), ' Reagan')
    rec.hear(['make the name', true], [' and the base blue', false])
    expect(composer()).toHaveValue('make the name Reagan and the base blue')
    rec.hear(['make the name', true], [' and the base blue', true])
    expect(composer()).toHaveValue('make the name Reagan and the base blue')

    // An edit inside the dictated text survives too.
    await user.clear(composer())
    await user.type(composer(), 'Make it')
    rec.hear(['make the name', true], [' and the base blue', true], [' please', false])
    expect(composer()).toHaveValue('Make it please')
    expect(userMessages()).toEqual([])
  })

  it('ignores a late error from a recognition it already dropped', async () => {
    installSpeech()
    const { user } = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Voice input' }))
    const rec = FakeRecognition.instances[0]!
    rec.hear(['make the name Reagan', true])
    await user.click(screen.getByRole('button', { name: 'Send' }))
    expect(rec.abort).toHaveBeenCalled()
    rec.hear(['stray words', false])
    rec.fail('network')
    expect(screen.queryByText(/Speech recognition needs a network connection/)).not.toBeInTheDocument()
    expect(composer()).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Voice input' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('is push-to-talk when held: letting go stops listening', async () => {
    installSpeech()
    const { user } = await openPanel()
    const mic = screen.getByRole('button', { name: 'Voice input' })
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_000)
    await user.pointer({ keys: '[MouseLeft>]', target: mic })
    expect(mic).toHaveAttribute('aria-pressed', 'true')
    now.mockReturnValue(2_000)
    await user.pointer({ keys: '[/MouseLeft]', target: mic })
    expect(FakeRecognition.instances[0]!.stop).toHaveBeenCalledOnce()
    now.mockRestore()
  })

  it('toggles from the keyboard', async () => {
    installSpeech()
    const { user } = await openPanel()
    const mic = screen.getByRole('button', { name: 'Voice input' })
    mic.focus()
    await user.keyboard('{Enter}')
    expect(mic).toHaveAttribute('aria-pressed', 'true')
    await user.keyboard('{Enter}')
    expect(FakeRecognition.instances[0]!.stop).toHaveBeenCalledOnce()
  })

  it('explains a denied microphone', async () => {
    installSpeech()
    const { user } = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Voice input' }))
    FakeRecognition.instances[0]!.fail('not-allowed')
    expect(screen.getByRole('alert')).toHaveTextContent('Microphone access was denied')
    expect(screen.getByRole('button', { name: 'Voice input' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('refuses an agent pressing the mic through the bridge', async () => {
    installSpeech()
    await openPanel()
    const click = await bridge.call('click', { role: 'button', name: 'Voice input' })
    expect(!click.ok && click.error.code).toBe('refused')
    const toggle = await bridge.call('click', { role: 'checkbox', name: 'Read replies aloud' })
    expect(!toggle.ok && toggle.error.code).toBe('refused')
    expect(FakeRecognition.instances).toEqual([])
    expect(window.localStorage.getItem(SPEAK_REPLIES_KEY)).toBeNull()
  })
})

describe('inside the Bambuddy iframe', () => {
  it('offers a new tab when the Permissions Policy blocks the microphone', async () => {
    installSpeech()
    Object.defineProperty(document, 'permissionsPolicy', {
      configurable: true,
      value: { allowsFeature: (feature: string) => feature !== 'microphone' },
    })
    const { user } = await openPanel({ embedded: true })
    expect(screen.queryByRole('button', { name: 'Voice input' })).not.toBeInTheDocument()
    const out = screen.getByRole('button', { name: 'Open in a new tab to use voice' })
    expect(out).toHaveAttribute('data-agent-user-only')
    await user.click(out)
    expect(openExternal).toHaveBeenCalledWith(window.location.href, true)
  })

  it('falls back to the new tab once a start is refused, where the policy can’t be read', async () => {
    installSpeech()
    const { user } = await openPanel({ embedded: true })
    await user.click(screen.getByRole('button', { name: 'Voice input' }))
    FakeRecognition.instances[0]!.fail('not-allowed')
    expect(screen.getByRole('alert')).toHaveTextContent('Open ScadBuddy in a new tab to use voice')
    expect(screen.getByRole('button', { name: 'Open in a new tab to use voice' })).toBeInTheDocument()
  })

  it('disables the new-tab fallback while another agent holds the session', async () => {
    installSpeech()
    Object.defineProperty(document, 'permissionsPolicy', {
      configurable: true,
      value: { allowsFeature: (feature: string) => feature !== 'microphone' },
    })
    const { user } = await openPanel({ embedded: true })
    await user.click(screen.getByRole('button', { name: 'Sessions (1)' }))
    await user.click(screen.getByRole('button', { name: /Tune the gridfinity bin/ }))
    await screen.findByText('Controlled by Claude Desktop')
    const out = screen.getByRole('button', { name: 'Open in a new tab to use voice' })
    expect(out).toBeDisabled()
    fireEvent.click(out)
    expect(openExternal).not.toHaveBeenCalled()
  })

  it('keeps the mic where the policy allows it', async () => {
    installSpeech()
    Object.defineProperty(document, 'permissionsPolicy', {
      configurable: true,
      value: { allowsFeature: () => true },
    })
    await openPanel({ embedded: true })
    expect(screen.getByRole('button', { name: 'Voice input' })).toBeInTheDocument()
  })
})

describe('spoken replies', () => {
  it('is off by default and remembers the choice in localStorage', async () => {
    installSpeech()
    const { user, unmount } = await openPanel()
    const toggle = screen.getByRole('checkbox', { name: 'Read replies aloud' })
    expect(toggle).not.toBeChecked()
    await user.click(toggle)
    expect(window.localStorage.getItem(SPEAK_REPLIES_KEY)).toBe('on')
    unmount()
    await openPanel()
    expect(screen.getByRole('checkbox', { name: 'Read replies aloud' })).toBeChecked()
  })

  it('survives storage that throws', async () => {
    installSpeech()
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const { user } = await openPanel()
    const toggle = screen.getByRole('checkbox', { name: 'Read replies aloud' })
    await user.click(toggle)
    expect(toggle).toBeChecked()
    await user.click(toggle)
    expect(toggle).not.toBeChecked()
  })

  it('reads the reply to the user’s message aloud, and Stop speaking cancels it', async () => {
    installSpeech()
    const { user } = await openPanel()
    await user.click(screen.getByRole('checkbox', { name: 'Read replies aloud' }))
    await user.type(composer(), 'Make the name bigger and send it{Enter}')
    await waitFor(() => expect(synth.speak).toHaveBeenCalled())
    const spoken = (synth.speak.mock.calls[0]![0] as FakeUtterance).text
    expect(spoken).not.toMatch(/[*`]/)
    await user.click(await screen.findByRole('button', { name: 'Stop speaking' }))
    expect(synth.cancel).toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Stop speaking' })).not.toBeInTheDocument()
  })

  it('stays quiet while the toggle is off', async () => {
    installSpeech()
    const { user } = await openPanel()
    await user.type(composer(), 'Make the name bigger and send it{Enter}')
    await screen.findByRole('region', { name: 'Needs your approval' })
    expect(synth.speak).not.toHaveBeenCalled()
  })
})

describe('spokenText', () => {
  it('drops markdown and code, and cuts long replies at a sentence', () => {
    expect(spokenText('I set **name** to `Reagan`.\n\n```scad\ncube();\n```\n- one\n- two')).toBe(
      'I set name to Reagan. one. two.',
    )
    const long = spokenText(`${'This is a sentence. '.repeat(40)}`)
    expect(long.length).toBeLessThan(450)
    expect(long).toMatch(/sentence\. The rest is in the panel\.$/)
  })
})
