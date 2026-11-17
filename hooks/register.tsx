import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, Timer } from 'claude-code'

import type { Board, Group, Row, View } from '../types'
import {
  age,
  classify,
  clip,
  DEFAULT_LIMITS,
  folder,
  ORDER,
  projectDir,
  sortRows,
} from './classify'
import type { AgentRow, Facts, JobFile, PidFile } from './classify'

// One pane, in whichever session you open it in, listing every Claude Code
// session on this machine (interactive tabs and background jobs, all folders),
// grouped Needs you > Stuck > Working > Idle > Done > Parked with a timer each.
// Data: `claude agents --json` joined with ~/.claude/jobs/<id>/state.json,
// ~/.claude/sessions/<pid>.json and the transcript's mtime. It polls only while
// the pane is open. Enter on a row jumps to it in cmux (its own tab when it has
// one, else a new workspace running `claude attach <id>`).

const PANE = 'agents-board'
const TITLE = 'Agents'
const POLL_MS = 10_000
const CMUX = '/Applications/cmux.app/Contents/Resources/bin/cmux'
const DONE_SHOWN = 6
const PARKED_SHOWN = 40

const boardAtom = atom({ plugin: 'agents-board', key: 'board' } as const, {
  rows: [],
  at: 0,
  error: '',
  loading: false,
}, { shape: 'board-1' })
const viewAtom = atom({ plugin: 'agents-board', key: 'view' } as const, {
  showParked: false,
  showDone: false,
  notify: true,
  message: '',
}, { shape: 'view-1' })

type Engine = EngineInterface
type Marks = Record<string, number>

const TITLES: Record<Group, string> = {
  needs: 'NEEDS YOU',
  stuck: `STUCK · quiet > ${DEFAULT_LIMITS.stuckMs / 60_000}m`,
  working: 'WORKING',
  idle: 'IDLE AT PROMPT',
  done: 'DONE · LAST 2 DAYS',
  parked: 'PARKED · NOT RUNNING, OLDER THAN 2 DAYS',
}
const COLORS: Record<Group, string> = {
  needs: 'warning',
  stuck: 'error',
  working: 'success',
  idle: 'subtle',
  done: 'subtle',
  parked: 'subtle',
}

const GLYPHS: Record<Group, string> = { needs: '●', stuck: '▲', working: '◆', idle: '◇', done: '✓', parked: '·' }
const HOLLOW: Record<Group, string> = { needs: '○', stuck: '△', working: '◇', idle: '◇', done: '✓', parked: '·' }

let homeDir = ''
let selfId = ''
let poller: Timer | undefined
let busy = false
let primed = false

async function home($: Engine): Promise<string> {
  if (homeDir === '') {
    const r = await $.process.run(['/bin/sh', '-c', 'printf %s "$HOME"'])
    homeDir = r.stdout.trim()
  }
  return homeDir
}

async function readJson<T>($: Engine, path: string): Promise<T | undefined> {
  try {
    const v: unknown = JSON.parse(await $.fs.read(path))
    return typeof v === 'object' && v !== null ? (v as T) : undefined
  } catch {
    return undefined
  }
}

async function mtime($: Engine, path: string): Promise<number | undefined> {
  try {
    return (await $.fs.stat(path)).mtimeMs
  } catch {
    return undefined
  }
}

async function marks($: Engine, key: string): Promise<Marks> {
  const v = await $.store.get(key)
  return typeof v === 'object' && v !== null ? (v as Marks) : {}
}

async function listAgents($: Engine): Promise<AgentRow[]> {
  const r = await $.process.run(['claude', 'agents', '--json', '--all'], { stdin: '', timeoutMs: 30_000 })
  if (r.exitCode !== 0) throw new Error(`claude agents failed: ${(r.stderr || r.stdout).slice(0, 200)}`)
  const start = r.stdout.indexOf('[')
  const rows: unknown = JSON.parse(start < 0 ? '[]' : r.stdout.slice(start))
  return Array.isArray(rows) ? (rows as AgentRow[]) : []
}

async function load($: Engine): Promise<Row[]> {
  const hp = await home($)
  const now = await $.clock.now()
  const agents = await listAgents($)
  const rows = await Promise.all(
    agents.map(async (a): Promise<Row> => {
      const facts: Facts = {}
      if (a.id) facts.job = await readJson<JobFile>($, `${hp}/.claude/jobs/${a.id}/state.json`)
      if (typeof a.pid === 'number') {
        facts.pidFile = await readJson<PidFile>($, `${hp}/.claude/sessions/${a.pid}.json`)
        // Transcript writes are the activity signal that tells "stuck" from busy.
        if (a.cwd && a.sessionId) {
          facts.transcriptAt = await mtime(
            $,
            `${hp}/.claude/projects/${projectDir(a.cwd)}/${a.sessionId}.jsonl`,
          )
        }
      }
      return classify(a, facts, now)
    }),
  )
  return sortRows(rows)
}

async function setView($: Engine, patch: Partial<View>): Promise<void> {
  await update($, viewAtom, v => ({ ...v, ...patch }))
}

async function notifyNew($: Engine, rows: readonly Row[]): Promise<void> {
  const hot = rows.filter(r => r.group === 'needs' || r.group === 'stuck')
  const seen = await marks($, 'notified')
  const fresh = hot.filter(r => seen[r.key] !== r.since)
  const next: Marks = {}
  for (const r of hot) next[r.key] = r.since
  await $.store.set('notified', next)
  // The first look after a load only learns what is already waiting.
  if (!primed) {
    primed = true
    return
  }
  const view = await read($, viewAtom)
  if (!view.notify || fresh.length === 0) return
  const line = fresh
    .slice(0, 3)
    .map(r => `${r.group === 'stuck' ? 'stuck' : 'needs you'}: ${clip(r.name, 40)}`)
    .join(' · ')
  $.ui.toast(line, { timeoutMs: 8000 })
  if (await $.fs.exists(CMUX)) {
    const body = fresh.map(r => `${clip(r.name, 50)} — ${clip(r.note, 80)}`).join('\n')
    await $.process
      .run([CMUX, 'notify', '--title', `Claude: ${fresh.length} need${fresh.length === 1 ? 's' : ''} you`, '--body', body], {
        stdin: '',
        timeoutMs: 5000,
      })
      .catch(() => undefined)
  }
}

async function refresh($: Engine): Promise<void> {
  if (busy) return
  busy = true
  await update($, boardAtom, b => ({ ...b, loading: true }))
  try {
    const rows = await load($)
    const at = await $.clock.now()
    await update($, boardAtom, () => ({ rows, at, error: '', loading: false }))
    const needs = rows.filter(r => r.group === 'needs').length
    const stuck = rows.filter(r => r.group === 'stuck').length
    $.ui.status(needs + stuck === 0 ? 'agents: all clear' : `agents: ${needs} need you · ${stuck} stuck`)
    await notifyNew($, rows)
  } catch (err) {
    await update($, boardAtom, b => ({ ...b, loading: false, error: String(err).slice(0, 300) }))
  } finally {
    busy = false
  }
}

function startPolling($: Engine): void {
  if (poller !== undefined) return
  poller = $.clock.every(POLL_MS, () => {
    void refresh($)
  })
}

function stopPolling($: Engine): void {
  poller?.cancel()
  poller = undefined
  $.ui.status(undefined)
}

async function cmux(
  $: Engine,
  args: string[],
): Promise<{ ok: boolean; out: string; stdout: string }> {
  try {
    const r = await $.process.run([CMUX, ...args], { stdin: '', timeoutMs: 10_000 })
    return { ok: r.exitCode === 0, out: `${r.stdout}\n${r.stderr}`.trim(), stdout: r.stdout }
  } catch (err) {
    return { ok: false, out: String(err), stdout: '' }
  }
}

type CmuxSession = { pid?: number; surfaceId?: string; workspaceId?: string }

/** Jump to a session: its own cmux tab, else a cmux workspace attached to it. */
async function go($: Engine, row: Row): Promise<string> {
  const attachCmd = row.id ? `claude attach ${row.id}` : ''
  if (!(await $.fs.exists(CMUX))) {
    return attachCmd ? `cmux not found; run: ${attachCmd}` : `pid ${row.pid} in ${row.cwd}`
  }
  const hp = await home($)
  const map = await readJson<{ sessions?: Record<string, CmuxSession> }>(
    $,
    `${hp}/.cmuxterm/claude-hook-sessions.json`,
  )
  const own = map?.sessions?.[row.sessionId]
  if (own?.surfaceId && own.workspaceId && row.pid > 0 && own.pid === row.pid) {
    const a = await cmux($, ['select-workspace', '--workspace', own.workspaceId])
    const b = await cmux($, ['focus-panel', '--panel', own.surfaceId, '--workspace', own.workspaceId])
    return a.ok && b.ok ? `Jumped to ${row.name}.` : `cmux could not focus it: ${clip(a.out || b.out, 120)}`
  }
  if (!row.id) return `${row.name}: pid ${row.pid} in ${row.cwd} is not in a cmux tab.`
  const title = `attach · ${clip(row.name, 32)}`
  const list = await cmux($, ['--json', 'workspace', 'list'])
  if (list.ok) {
    try {
      const parsed = JSON.parse(list.stdout) as {
        workspaces?: { id?: string; title?: string | null; custom_title?: string | null }[]
      }
      // An attached session titles its cmux tab with the session name; a tab
      // this board opened carries our "attach · <name>" title.
      const hit =
        parsed.workspaces?.find(w => w.custom_title === title) ??
        parsed.workspaces?.find(w => w.title === row.name)
      if (hit?.id) {
        const s = await cmux($, ['select-workspace', '--workspace', hit.id])
        if (s.ok) return `Switched to the attached tab for ${row.name}.`
      }
    } catch {
      // fall through and open a new one
    }
  }
  const made = await cmux($, [
    'new-workspace',
    '--name',
    title,
    '--cwd',
    row.cwd || hp,
    '--command',
    attachCmd,
    '--focus',
    'true',
  ])
  return made.ok ? `Opened ${row.name} in a new cmux tab.` : `cmux: ${clip(made.out, 160)} — run: ${attachCmd}`
}

async function hide($: Engine, row: Row): Promise<void> {
  const hidden = await marks($, 'hidden')
  hidden[row.key] = row.stamp
  await $.store.set('hidden', hidden)
  await update($, boardAtom, b => ({ ...b })) // redraw
}

async function openBoard($: Engine): Promise<void> {
  await $.ui.open({ id: PANE, title: TITLE, focus: true, rows: 18 })
  startPolling($)
  await refresh($)
}

const FULL_TITLE = '◆ agents board'

/** Where uv lives, when it does: the full-screen board runs through it. */
async function findUv($: Engine): Promise<string | undefined> {
  try {
    const r = await $.process.run(
      ['/bin/sh', '-c', 'PATH="$PATH:$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin" command -v uv'],
      { stdin: '', timeoutMs: 10_000 },
    )
    const path = r.stdout.trim().split('\n')[0] ?? ''
    return path === '' ? undefined : path
  } catch {
    return undefined
  }
}

/** The command that starts the full-screen board bundled with this plugin. */
async function fullBoardCommand($: Engine): Promise<string | undefined> {
  const uv = await findUv($)
  if (uv === undefined) return undefined
  return `'${uv}' run --quiet --script '${$.plugin.root}/tui/board.py'`
}

/**
 * The full-screen board (tui/board.py) in its own cmux tab: focus it, or open
 * it. Answers what happened, or why it could not (no cmux, no uv).
 */
async function openFullBoard($: Engine): Promise<{ ok: boolean; text: string }> {
  const command = await fullBoardCommand($)
  if (command === undefined) {
    return { ok: false, text: 'The full-screen board needs uv (https://docs.astral.sh/uv/); showing the pane instead.' }
  }
  if (!(await $.fs.exists(CMUX))) {
    return { ok: false, text: `For the full-screen board, run this in a terminal: ${command}` }
  }
  const hp = await home($)
  const list = await cmux($, ['--json', 'workspace', 'list'])
  if (list.ok) {
    try {
      const parsed = JSON.parse(list.stdout) as { workspaces?: { id?: string; custom_title?: string | null }[] }
      const hit = parsed.workspaces?.find(w => w.custom_title === FULL_TITLE)
      if (hit?.id) {
        // Only switch to a tab where the board still runs; one left at a shell
        // (the board quit or crashed) is replaced by a fresh one.
        // The board writes a heartbeat every 3 s, keyed by its cmux workspace.
        const beat = await readJson<{ at?: number }>($, `${hp}/.claude/agents-board/running-${hit.id}.json`)
        if (typeof beat?.at === 'number' && (await $.clock.now()) - beat.at < 15_000) {
          if ((await cmux($, ['select-workspace', '--workspace', hit.id])).ok) {
            return { ok: true, text: 'Switched to the agents board tab.' }
          }
        } else {
          await cmux($, ['close-workspace', '--workspace', hit.id, '--force'])
        }
      }
    } catch {
      // open a new one below
    }
  }
  const made = await cmux($, ['new-workspace', '--name', FULL_TITLE, '--cwd', hp, '--command', command, '--focus', 'true'])
  return made.ok
    ? { ok: true, text: 'Opened the agents board in its own cmux tab.' }
    : { ok: false, text: `cmux could not open a tab (${clip(made.out, 100)}); run: ${command}` }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'board',
      description:
        'Agents board: every Claude session on this machine, needs-you / stuck / working with timers, full screen in its own cmux tab. "/board pane" shows it beside the chat',
    })
    selfId = await $.session.id()
    // Drawing must never shell out (a render has a tight budget): learn $HOME now.
    await home($)
    // A reload keeps the pane up but drops our timer: pick polling back up.
    if ((await $.ui.panes()).some(p => p.id === PANE)) {
      startPolling($)
      void refresh($)
    }
    return next(e)
  })

  on('command.run', { command: 'board' }, async ($, e) => {
    let note = ''
    if (e.args.trim() !== 'pane') {
      const full = await openFullBoard($)
      if (full.ok) return { text: full.text }
      note = `${full.text}\n`
    }
    await openBoard($)
    const b = await read($, boardAtom)
    if (b.error !== '') return { text: `${note}Agents board opened, but loading failed: ${b.error}` }
    const n = b.rows.filter(r => r.group === 'needs').length
    const s = b.rows.filter(r => r.group === 'stuck').length
    return { text: `${note}Agents board opened: ${n} need you, ${s} stuck, ${b.rows.length} sessions in all.` }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) stopPolling($)
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    stopPolling($)
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const board = await read($, boardAtom)
    const view = await read($, viewAtom)
    const hidden = await marks($, 'hidden')
    const homePath = homeDir
    const now = board.at || (await $.clock.now())
    const width = Math.max(30, e.props.bodyColumns)
    const isInline = e.props.placement !== 'dock'

    const visible = board.rows.filter(r => hidden[r.key] !== r.stamp)
    const hiddenCount = board.rows.length - visible.length
    const by = (g: Group) => visible.filter(r => r.group === g)
    const count = (g: Group) => by(g).length

    const act = (work: () => Promise<string | void>) => () => {
      void (async () => {
        const msg = await work()
        if (typeof msg === 'string') await setView($, { message: msg })
      })()
    }

    const rowView = (r: Row) => {
      const here = r.sessionId === selfId ? '  this one' : ''
      const asleep = r.pid > 0 ? '' : '  asleep'
      const tab = r.kind === 'tty' ? '  tab' : ''
      const glyph = r.pid > 0 || r.group === 'done' ? GLYPHS[r.group] : HOLLOW[r.group]
      const nameRoom = Math.max(12, width - 34)
      const note = r.reply !== '' ? `${r.note}  →  ${r.reply}` : r.note
      return (
        <Box key={`row:${r.key}`} flexDirection="column">
          <Box flexDirection="row" gap={1}>
            <Button key={`go:${r.key}`} plain onPress={act(() => go($, r))}>
              <Text color={COLORS[r.group]}>{glyph}</Text> <Text bold>{clip(r.name, nameRoom)}</Text>
              <Text dimColor>
                {'  '}
                {folder(r.cwd, homePath)}
                {tab}
                {asleep}
                {here}
              </Text>
            </Button>
            <Text color={COLORS[r.group]} bold>
              {age(now - r.since)}
            </Text>
            <Button key={`hide:${r.key}`} plain dimColor label="×" onPress={act(() => hide($, r))} />
          </Box>
          <Text dimColor wrap="truncate">
            {'  '}
            {clip(note, width - 4)}
          </Text>
        </Box>
      )
    }

    const section = (g: Group, rows: readonly Row[], total: number) => (
      <Box key={`sec:${g}`} flexDirection="column" marginTop={1}>
        <Text bold color={COLORS[g]}>
          {TITLES[g]}  <Text dimColor>{total}</Text>
        </Text>
        {rows.map(rowView)}
        {total > rows.length && <Text dimColor>{`       +${total - rows.length} more`}</Text>}
      </Box>
    )

    const updated = board.at === 0 ? 'loading…' : `updated ${age((await $.clock.now()) - board.at)} ago`
    const groups: Group[] = isInline ? ['needs', 'stuck'] : ORDER.filter(g => g !== 'parked')

    return (
      <Box flexDirection="column">
        <Text>
          <Text bold color="warning">{count('needs')} need you</Text>
          <Text> · </Text>
          <Text bold color="error">{count('stuck')} stuck</Text>
          <Text> · </Text>
          <Text color="success">{count('working')} working</Text>
          <Text dimColor>
            {'  '}
            {board.loading ? 'refreshing…' : updated}
          </Text>
        </Text>
        <Box flexDirection="row" gap={1}>
          <Button
            key="full"
            hotkey="f"
            variant="primary"
            label="full screen"
            onPress={act(async () => (await openFullBoard($)).text)}
          />
          <Button key="refresh" hotkey="r" label="refresh" onPress={act(() => refresh($))} />
          <Button
            key="done"
            hotkey="d"
            label={view.showDone ? 'fewer done' : 'all done'}
            onPress={act(() => setView($, { showDone: !view.showDone }))}
          />
          <Button
            key="parked"
            hotkey="p"
            label={`${view.showParked ? 'hide' : 'show'} parked (${count('parked')})`}
            onPress={act(() => setView($, { showParked: !view.showParked }))}
          />
          <Button
            key="notify"
            hotkey="n"
            label={`alerts ${view.notify ? 'on' : 'off'}`}
            onPress={act(() => setView($, { notify: !view.notify }))}
          />
          {hiddenCount > 0 && (
            <Button
              key="unhide"
              hotkey="u"
              label={`unhide ${hiddenCount}`}
              onPress={act(async () => {
                await $.store.set('hidden', {})
                await update($, boardAtom, b => ({ ...b }))
                return 'Hidden rows restored.'
              })}
            />
          )}
        </Box>
        {board.error !== '' && (
          <Text color="error" wrap="wrap">
            {board.error}
          </Text>
        )}
        {view.message !== '' && (
          <Text dimColor wrap="wrap">
            {view.message}
          </Text>
        )}
        {groups.map(g => {
          const rows = by(g)
          if (rows.length === 0 && g !== 'needs') return null
          const shown = g === 'done' && !view.showDone ? rows.slice(0, DONE_SHOWN) : rows
          return section(g, shown, rows.length)
        })}
        {count('needs') === 0 && <Text dimColor>       nothing is waiting on you</Text>}
        {!isInline && view.showParked && section('parked', by('parked').slice(0, PARKED_SHOWN), count('parked'))}
        <Text dimColor>{'\n'}Tab to a row, Enter jumps to it · × hides it until it changes · ○ asleep: Enter resumes it</Text>
      </Box>
    )
  })
}
