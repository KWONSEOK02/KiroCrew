import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Command, Cog, Link2, MessageSquare, Package, Terminal } from 'lucide-react'

import { api } from '../../api/client'
import { appNavTargets, type AppNavRecord } from '../../appNav'
import { Highlighted } from '../../components/commandPalette/Highlighted'
import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { settingsRoute } from '../../components/commandPalette/settingsRoute'
import { usePaletteActions } from '../../components/commandPalette/paletteActions'
import { useSessionsProvider } from '../../components/commandPalette/providers/sessionsProvider'
import type { Result } from '../../components/commandPalette/types'
import { useVisualViewport } from '../../hooks/useVisualViewport'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { i18nT } from '../../i18n/t'

import { loadUsage, recordUse, type UsageMap } from './frecency'
import { rankRootRows, type RankedRow, type RootGroup, type RootRow } from './rootIndex'

/**
 * Command Bar — the ⌘K launcher.
 *
 * Contributed by the `command-bar` app as an overlay claiming the host's
 * `quick-search` slot, so the host renders it only while that app is enabled.
 *
 * The shape it is built around: the FIRST PAGE is a launcher over rows already in
 * memory (commands, app destinations, quicklinks, settings) and never queries a
 * backend, so typing in it costs nothing no matter how much history the instance
 * holds. A content search is a ROW you enter — entering it is the activation
 * event that lets that engine run its first query. The previous surface fanned
 * every keystroke out to every provider, which is why typing could stall the
 * gateway's event loop; here a keystroke in the root has nothing to fan out to.
 *
 * No prefix sigils: the entry gesture is Enter on a row, and habit (frecency
 * ranking) is what makes a frequent row reachable in one or two keystrokes.
 */

/** Scoped views the bar can enter. Each one owns its own engine. */
type Scope = null | 'sessions'

const SESSIONS_MIN_CHARS = 2
const DEBOUNCE_MS = 150

interface QuicklinkStored {
  id?: string
  title?: string
  route?: string
}

/**
 * User-authored quicklinks. These are user DATA, not a plugin: adding one must
 * never require authoring an app manifest, so they live in local storage rather
 * than in `contributes`.
 */
function loadQuicklinks(): RootRow[] {
  let raw: string | null = null
  try {
    raw = localStorage.getItem('mc-command-bar-quicklinks')
  } catch {
    return []
  }
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return (parsed as QuicklinkStored[])
      .filter(q => q && typeof q.title === 'string' && typeof q.route === 'string')
      .map((q, i) => ({
        id: `quicklink:${q.id ?? i}`,
        title: q.title as string,
        subtitle: q.route,
        group: 'quicklinks' as RootGroup,
        kind: 'navigate' as const,
        route: q.route as string,
      }))
  } catch {
    return []
  }
}

function groupLabel(group: RootGroup): string {
  switch (group) {
    case 'commands':
      return i18nT('apps.commandBar.group_commands')
    case 'apps':
      return i18nT('apps.commandBar.group_apps')
    case 'quicklinks':
      return i18nT('apps.commandBar.group_quicklinks')
    case 'settings':
      return i18nT('apps.commandBar.group_settings')
  }
}

function groupIcon(group: RootGroup) {
  switch (group) {
    case 'commands':
      return <Terminal size={14} className="lucide-inline" />
    case 'apps':
      return <Package size={14} className="lucide-inline" />
    case 'quicklinks':
      return <Link2 size={14} className="lucide-inline" />
    case 'settings':
      return <Cog size={14} className="lucide-inline" />
  }
}

export default function CommandBarOverlay({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const vv = useVisualViewport()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [scope, setScope] = useState<Scope>(null)
  const [selected, setSelected] = useState(0)
  const [usage, setUsage] = useState<UsageMap>(() => loadUsage())

  const { navigate } = usePaletteActions()
  // `aria-modal` is a promise that Tab cannot reach the page behind the dialog, so
  // the trap has to be real. Escape is left to the input's own handler, which needs
  // it to pop a scope before it closes the bar.
  useDialogFocusTrap(dialogRef, onClose, true, false)
  // Constructing the sessions engine is just memoized closures — it issues no
  // request until `search()` is called, and only the sessions VIEW calls it. That
  // call site, not the construction, is what the root must never reach.
  const sessions = useSessionsProvider({})

  // The app list is READ, never fetched: the shell publishes its own
  // `GET /api/apps` response under this key, and `enabled: false` makes this a
  // cache subscriber that re-renders when that write lands. Fetching here would
  // reintroduce a request on any open past the stale window, which is exactly the
  // cost this surface exists to remove. Before the shell's first response the Apps
  // group is simply empty; commands and settings are local and render regardless.
  const { data: apps } = useQuery({
    queryKey: ['apps'],
    queryFn: () => api.listApps() as Promise<AppNavRecord[]>,
    enabled: false,
  })

  useEffect(() => {
    if (!open) return
    setQuery('')
    setDebounced('')
    setScope(null)
    setSelected(0)
    setUsage(loadUsage())
  }, [open])

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [query])

  const rootRows: RootRow[] = useMemo(() => {
    const rows: RootRow[] = [
      {
        id: 'command:search-sessions',
        title: i18nT('apps.commandBar.cmd_search_sessions'),
        group: 'commands',
        kind: 'view',
        view: 'sessions',
        keywords: ['history', 'chat', 'conversation'],
      },
    ]
    for (const target of appNavTargets(apps ?? [])) {
      rows.push({
        id: `app:${target.name}`,
        title: target.label,
        group: 'apps',
        kind: 'navigate',
        route: target.route,
      })
    }
    for (const entry of SETTINGS_REGISTRY) {
      rows.push({
        id: `setting:${entry.id}`,
        title: entry.labelKey ? i18nT(entry.labelKey) : entry.label,
        subtitle: entry.tab,
        group: 'settings',
        kind: 'navigate',
        route: settingsRoute(entry),
      })
    }
    rows.push(...loadQuicklinks())
    return rows
  }, [apps])

  // The root ranks from the LIVE query, not the debounced one. Ranking is pure and
  // local, so there is nothing to throttle, and debouncing it would let a fast Enter
  // -- two keystrokes then return, which is the whole point of a launcher -- activate
  // the row selected against the previous query. Debounce exists for the scoped
  // views, which do hit the network.
  const ranked: RankedRow[] = useMemo(
    () => (scope ? [] : rankRootRows(rootRows, query, usage)),
    [scope, rootRows, query, usage],
  )

  // Sessions view. Enabled only inside the scope, so the root cannot trigger it.
  const scopedQuery = scope === 'sessions' ? debounced.trim() : ''
  const { data: scopedResults, isFetching } = useQuery({
    queryKey: ['command-bar', 'sessions', scopedQuery],
    queryFn: () => Promise.resolve(sessions.search(scopedQuery)) as Promise<Result[]>,
    enabled: scope === 'sessions' && scopedQuery.length >= SESSIONS_MIN_CHARS,
    staleTime: 15_000,
  })

  const use = useCallback((id: string) => setUsage(prev => recordUse(id, Date.now(), prev)), [])

  const enterScope = useCallback((view: Scope, keepQuery: string) => {
    setScope(view)
    setQuery(keepQuery)
    setDebounced(keepQuery)
    setSelected(0)
    inputRef.current?.focus()
  }, [])

  const activateRoot = useCallback(
    (row: RankedRow) => {
      use(row.id)
      if (row.kind === 'view') {
        // Entering is the activation event: the engine's first query happens
        // here, not while the user was still typing in the root.
        enterScope((row.view as Scope) ?? null, '')
        return
      }
      if (row.kind === 'navigate' && row.route) {
        navigate(row.route)
        onClose()
        return
      }
      row.run?.()
      onClose()
    },
    [enterScope, navigate, onClose, use],
  )

  const rows: (RankedRow | Result)[] = useMemo(
    () => (scope ? (scopedResults ?? []) : ranked),
    [scope, scopedResults, ranked],
  )
  const fallbackVisible = !scope && query.trim().length > 0
  const rowCount = rows.length + (fallbackVisible ? 1 : 0)

  useEffect(() => {
    if (selected >= rowCount) setSelected(Math.max(0, rowCount - 1))
  }, [rowCount, selected])

  const activateIndex = useCallback(
    (index: number) => {
      if (fallbackVisible && index === rows.length) {
        enterScope('sessions', query)
        return
      }
      const row = rows[index]
      if (!row) return
      if (scope) {
        ;(row as Result).onActivate()
        onClose()
        return
      }
      activateRoot(row as RankedRow)
    },
    [activateRoot, enterScope, fallbackVisible, onClose, query, rows, scope],
  )

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelected(i => (rowCount === 0 ? 0 : (i + 1) % rowCount))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelected(i => (rowCount === 0 ? 0 : (i - 1 + rowCount) % rowCount))
      } else if (e.key === 'Enter') {
        e.preventDefault()
        activateIndex(selected)
      } else if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      } else if (e.key === 'Backspace' && query === '' && scope) {
        // Leaving a scope is Backspace on an empty input — the same gesture that
        // deletes a character, so it needs no separate key to learn.
        e.preventDefault()
        setScope(null)
        setSelected(0)
      }
    },
    [activateIndex, onClose, query, rowCount, scope, selected],
  )

  if (!open) return null

  const scopeName = scope === 'sessions' ? i18nT('apps.commandBar.cmd_search_sessions') : ''
  const listId = 'command-bar-list'
  const rowId = (i: number) => `command-bar-row-${i}`

  return createPortal(
    <div
      className="fixed left-0 right-0 z-[9999] flex items-start justify-center bg-bg/60 backdrop-blur-sm animate-rise"
      style={{ top: vv.offsetTop, height: vv.height }}
      // The backdrop is a click target for dismissal, not a control: the dialog role
      // belongs to the card below, and screen readers should skip this layer.
      role="presentation"
      // Dismiss only when the press lands on the backdrop ITSELF. Testing the target
      // beats stopping propagation on the card, which would put a mouse handler on a
      // non-interactive dialog element for no behavioural gain.
      onMouseDown={e => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="w-full max-w-xl mx-4 bg-card border border-border rounded-xl shadow-xl overflow-hidden flex flex-col"
        style={{ marginTop: Math.round(vv.height * 0.12), maxHeight: Math.round(vv.height * 0.7) }}
        role="dialog"
        aria-modal="true"
        aria-label={i18nT('apps.commandBar.title')}
      >
        <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border">
          <Command size={15} className="lucide-inline text-muted shrink-0" />
          {scope && (
            <span className="shrink-0 text-[11px] px-1.5 py-0.5 rounded bg-accent-subtle text-accent">
              {scopeName}
            </span>
          )}
          <input
            ref={inputRef}
            autoFocus
            value={query}
            onChange={e => {
              setQuery(e.target.value)
              setSelected(0)
            }}
            onKeyDown={onKeyDown}
            placeholder={
              scope
                ? i18nT('apps.commandBar.placeholder_sessions')
                : i18nT('apps.commandBar.placeholder')
            }
            aria-label={i18nT('apps.commandBar.title')}
            // Selection stays on the input and is announced through
            // aria-activedescendant, so arrow keys never move DOM focus off it.
            role="combobox"
            aria-expanded={rowCount > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={rowCount > 0 ? rowId(selected) : undefined}
            className="flex-1 bg-transparent border-none outline-none text-[13px] text-text placeholder:text-muted"
          />
        </div>

        <div className="overflow-y-auto py-1" id={listId} role="listbox" aria-label={i18nT('apps.commandBar.title')}>
          {rowCount === 0 ? (
            <div className="px-3 py-6 text-center text-[12px] text-muted">
              {scope
                ? // Inside a scope the root's "no commands" copy would be a lie: the
                  // view searches sessions, and a fresh scope has no query yet.
                  scopedQuery.length < SESSIONS_MIN_CHARS
                  ? i18nT('apps.commandBar.keep_typing', { min: SESSIONS_MIN_CHARS })
                  : isFetching
                    ? i18nT('apps.commandBar.searching')
                    : i18nT('apps.commandBar.no_sessions')
                : i18nT('apps.commandBar.no_matches')}
            </div>
          ) : (
            <>
              {rows.map((row, i) => {
                const isRoot = !scope
                const rr = row as RankedRow
                const sr = row as Result
                const prev = i > 0 ? (rows[i - 1] as RankedRow) : undefined
                const header =
                  isRoot && (i === 0 || prev?.group !== rr.group) ? groupLabel(rr.group) : null
                return (
                  <div key={isRoot ? rr.id : sr.id}>
                    {header && (
                      <div className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wide text-muted">
                        {header}
                      </div>
                    )}
                    <div
                      id={rowId(i)}
                      role="option"
                      tabIndex={-1}
                      aria-selected={selected === i}
                      onMouseDown={() => activateIndex(i)}
                      onMouseEnter={() => setSelected(i)}
                      className={`flex items-center gap-2 px-3 py-1.5 cursor-pointer text-[13px] ${
                        selected === i ? 'bg-bg-hover text-text' : 'text-text'
                      }`}
                    >
                      <span className="shrink-0 text-muted">
                        {isRoot ? groupIcon(rr.group) : <MessageSquare size={14} className="lucide-inline" />}
                      </span>
                      <span className="flex-1 truncate">
                        {isRoot ? (
                          <Highlighted text={rr.title} indices={rr.indices} />
                        ) : (
                          <Highlighted text={sr.title} indices={sr.indices} />
                        )}
                      </span>
                      {isRoot && rr.kind === 'view' && (
                        <ArrowRight size={13} className="lucide-inline text-muted shrink-0" />
                      )}
                    </div>
                  </div>
                )
              })}
              {fallbackVisible && (
                <div
                  id={rowId(rows.length)}
                  role="option"
                  tabIndex={-1}
                  aria-selected={selected === rows.length}
                  onMouseDown={() => activateIndex(rows.length)}
                  onMouseEnter={() => setSelected(rows.length)}
                  className={`flex items-center gap-2 px-3 py-1.5 cursor-pointer text-[13px] border-t border-border ${
                    selected === rows.length ? 'bg-bg-hover' : ''
                  }`}
                >
                  {/* The root does not search content, so the typed text still has
                      somewhere to go: one Enter carries it into the sessions view
                      instead of scanning the corpus on every keystroke. */}
                  <MessageSquare size={14} className="lucide-inline text-muted shrink-0" />
                  <span className="flex-1 truncate text-muted">
                    {i18nT('apps.commandBar.fallback_sessions', { query })}
                  </span>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
