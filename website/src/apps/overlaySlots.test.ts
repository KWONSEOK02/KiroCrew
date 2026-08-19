import { resolveSlotOverlays, type OverlayAppRecord } from './overlaySlots'

/**
 * Unit tests for host overlay slot resolution — a pure function over the
 * `/api/apps` subset, no React and no registry mutation.
 */

function app(over: Partial<OverlayAppRecord> = {}): OverlayAppRecord {
  return {
    name: 'command-bar',
    enabled: true,
    manifest: {
      ui: { overlays: [{ id: 'command-bar', label: 'Command Bar', replaces: 'quick-search' }] },
    },
    ...over,
  }
}

describe('resolveSlotOverlays', () => {
  it('gives the slot to an enabled app that claims it', () => {
    expect(resolveSlotOverlays([app()])['quick-search']).toEqual({
      app: 'command-bar',
      overlayId: 'command-bar',
      label: 'Command Bar',
    })
  })

  it('leaves the slot to the host while the app is disabled', () => {
    // The app's enable state IS the user's opt-in: a disabled app must not
    // displace the host's own surface.
    expect(resolveSlotOverlays([app({ enabled: false })])['quick-search']).toBeUndefined()
    expect(resolveSlotOverlays([app({ enabled: undefined })])['quick-search']).toBeUndefined()
  })

  it('ignores a declaration with no id or no replaces', () => {
    const noId = app({
      manifest: { ui: { overlays: [{ label: 'x', replaces: 'quick-search' }] } },
    })
    const noSlot = app({ manifest: { ui: { overlays: [{ id: 'command-bar', label: 'x' }] } } })
    expect(resolveSlotOverlays([noId])['quick-search']).toBeUndefined()
    expect(resolveSlotOverlays([noSlot])['quick-search']).toBeUndefined()
  })

  it('refuses an unknown host slot without throwing', () => {
    // Manifest data is third-party input, so a bogus slot degrades to "host keeps
    // its surface" — it must never take the dashboard down (contrast
    // reportSeamCollision, which throws in dev/test for developer-authored
    // registrations).
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const bogus = app({
      manifest: {
        ui: { overlays: [{ id: 'command-bar', label: 'x', replaces: 'not-a-real-slot' }] },
      },
    })
    expect(() => resolveSlotOverlays([bogus])).not.toThrow()
    expect(resolveSlotOverlays([bogus])).toEqual({})
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })

  it('refuses an overlay id with no registered component without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const ghost = app({
      manifest: {
        ui: { overlays: [{ id: 'not-bundled', label: 'x', replaces: 'quick-search' }] },
      },
    })
    expect(() => resolveSlotOverlays([ghost])).not.toThrow()
    expect(resolveSlotOverlays([ghost])).toEqual({})
    warn.mockRestore()
  })

  it('resolves a contested slot by app name, not response order', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const rival = app({
      name: 'zzz-other',
      manifest: {
        ui: { overlays: [{ id: 'command-bar', label: 'Rival', replaces: 'quick-search' }] },
      },
    })
    const forward = resolveSlotOverlays([app(), rival])['quick-search']
    const reversed = resolveSlotOverlays([rival, app()])['quick-search']
    expect(forward?.app).toBe('command-bar')
    expect(reversed?.app).toBe('command-bar')
    warn.mockRestore()
  })

  it('returns no owners for an empty app list', () => {
    expect(resolveSlotOverlays([])).toEqual({})
  })

  it('tolerates an app with no manifest at all', () => {
    expect(() => resolveSlotOverlays([{ name: 'bare', enabled: true }])).not.toThrow()
  })

  it('refuses an inherited Object property as an overlay id', () => {
    // The slug grammar admits `constructor`, `toString` and friends, so a membership
    // test written with `in` would accept them and the lookup would hand the host a
    // function to render as a component.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    for (const id of ['constructor', 'toString', 'valueOf']) {
      const poisoned = app({
        manifest: { ui: { overlays: [{ id, label: 'x', replaces: 'quick-search' }] } },
      })
      expect(resolveSlotOverlays([poisoned])).toEqual({})
    }
    warn.mockRestore()
  })
})
