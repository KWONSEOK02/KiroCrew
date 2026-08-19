import { render, screen, waitFor } from '@testing-library/react'

import QuickSearchSurface from './QuickSearchSurface'
import type { SlotOwners } from '../apps/overlaySlots'

/**
 * The opt-in guarantee, asserted at the seam: with no app owning `quick-search` the
 * shell renders its OWN palette (so a user who never enables anything sees no change),
 * and an owning app's overlay replaces it wholesale.
 */

vi.mock('./CommandPalette', () => ({
  default: ({ open }: { open: boolean }) => (open ? <div data-testid="legacy-palette" /> : null),
}))

const mounted = vi.fn()

vi.mock('../apps/overlayRegistry', () => ({
  getBuiltinOverlay: (id: string) => {
    if (id === 'command-bar') {
      return ({ open }: { open: boolean }) => {
        // Records the MOUNT, not the render output: "closed means unmounted" is a
        // statement about the component tree, and an overlay that returns null while
        // closed would satisfy an output-only assertion either way.
        mounted()
        return open ? <div data-testid="command-bar" /> : null
      }
    }
    if (id === 'throws') {
      return () => {
        throw new Error('chunk gone')
      }
    }
    return undefined
  },
}))

const OWNED: SlotOwners = {
  'quick-search': { app: 'command-bar', overlayId: 'command-bar', label: 'Command Bar' },
}

const THROWS: SlotOwners = {
  'quick-search': { app: 'broken', overlayId: 'throws', label: 'Broken' },
}

describe('QuickSearchSurface', () => {
  beforeEach(() => mounted.mockClear())

  it('renders the shell palette when no app owns the slot', () => {
    render(<QuickSearchSurface owners={{}} open onClose={() => {}} />)
    expect(screen.getByTestId('legacy-palette')).toBeTruthy()
    expect(screen.queryByTestId('command-bar')).toBeNull()
  })

  it('renders the owning app overlay instead of the palette', async () => {
    render(<QuickSearchSurface owners={OWNED} open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByTestId('command-bar')).toBeTruthy())
    expect(screen.queryByTestId('legacy-palette')).toBeNull()
  })

  it('falls back to the palette when the owner names an unregistered overlay', () => {
    const ghost: SlotOwners = {
      'quick-search': { app: 'x', overlayId: 'not-bundled', label: 'X' },
    }
    render(<QuickSearchSurface owners={ghost} open onClose={() => {}} />)
    expect(screen.getByTestId('legacy-palette')).toBeTruthy()
  })

  it('renders nothing visible while closed', () => {
    const { container } = render(<QuickSearchSurface owners={{}} open={false} onClose={() => {}} />)
    expect(container.textContent).toBe('')
  })

  it('does not mount the owning overlay at all while closed', async () => {
    // A closed bar must hold no live subscriptions: the scoped session view's query
    // would otherwise stay enabled behind a dismissed surface and refetch on the next
    // window focus, turning a closed search into a background corpus scan.
    render(<QuickSearchSurface owners={OWNED} open={false} onClose={() => {}} />)
    await waitFor(() => expect(screen.queryByTestId('command-bar')).toBeNull())
    expect(mounted).not.toHaveBeenCalled()
    expect(screen.queryByTestId('legacy-palette')).toBeNull()
  })

  it('falls back to the palette when the overlay throws', async () => {
    // Suspense catches a PENDING import, never a rejected one, and a tab left open
    // across a deploy asks for a chunk hash that no longer exists.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<QuickSearchSurface owners={THROWS} open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByTestId('legacy-palette')).toBeTruthy())
    warn.mockRestore()
    error.mockRestore()
  })
})
