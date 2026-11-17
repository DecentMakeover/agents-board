import type { Group, Row } from '../types'

/** A row of `claude agents --json`. */
export type AgentRow = {
  id?: string
  sessionId?: string
  name?: string
  cwd?: string
  kind?: string
  pid?: number
  state?: string
  status?: string
  waitingFor?: string
  startedAt?: number
}

/** What we read from ~/.claude/jobs/<id>/state.json. */
export type JobFile = {
  updatedAt?: string
  detail?: string
  needs?: string | null
  suggestedReply?: string | null
  tempo?: string
  inFlight?: { tasks?: number; kinds?: string[] } | null
  output?: { result?: string } | null
}

/** What we read from ~/.claude/sessions/<pid>.json. */
export type PidFile = { statusUpdatedAt?: number; status?: string; waitingFor?: string }

export type Facts = {
  job?: JobFile
  pidFile?: PidFile
  /** mtime of the session's transcript, when found. */
  transcriptAt?: number
}

export type Limits = {
  /** A working session with no activity for this long is "stuck". */
  stuckMs: number
  /** A blocked/failed/done session not running and older than this is "parked". */
  parkMs: number
}

export const DEFAULT_LIMITS: Limits = { stuckMs: 15 * 60_000, parkMs: 2 * 86_400_000 }

const s = (v: unknown): string => (typeof v === 'string' ? v : '')
const flat = (t: string): string => t.replace(/\s+/g, ' ').trim()

export function classify(a: AgentRow, f: Facts, now: number, lim: Limits = DEFAULT_LIMITS): Row {
  const job = f.job ?? {}
  const live = typeof a.pid === 'number' && a.pid > 0
  const kind: Row['kind'] = a.kind === 'interactive' ? 'tty' : 'bg'
  const updated = Date.parse(s(job.updatedAt))
  const jobAt = Number.isNaN(updated) ? 0 : updated
  const statusAt = f.pidFile?.statusUpdatedAt ?? 0
  const activity = Math.max(jobAt, statusAt, f.transcriptAt ?? 0, a.startedAt ?? 0)
  const status = s(a.status) || s(f.pidFile?.status)
  const state = s(a.state)
  const detail = flat(s(job.detail))

  let group: Group
  let since = activity
  let note = detail

  const parkOr = (g: Group, at: number): Group => (!live && now - at > lim.parkMs ? 'parked' : g)

  if (status === 'waiting') {
    group = 'needs'
    since = statusAt || jobAt || activity
    const why = s(a.waitingFor) || s(f.pidFile?.waitingFor) || 'waiting'
    note = `${why}${job.needs ? ` · ${flat(s(job.needs))}` : detail ? ` · ${detail}` : ''}`
  } else if (state === 'blocked') {
    since = jobAt || activity
    group = parkOr('needs', since)
    note = flat(s(job.needs)) || detail || 'needs input'
  } else if (state === 'failed') {
    since = jobAt || activity
    group = parkOr('needs', since)
    note = `failed · ${detail}`
  } else if (state === 'working' || (kind === 'tty' && (status === 'busy' || status === 'shell'))) {
    const tasks = job.inFlight?.tasks ?? 0
    const kinds = (job.inFlight?.kinds ?? []).join(', ')
    if (!live && kind === 'bg') {
      since = activity
      group = parkOr('stuck', since)
      note = `process gone while working · ${detail}`
    } else if (tasks > 0 && job.tempo === 'idle') {
      group = 'working'
      note = `waiting on background ${kinds || 'task'}${detail ? ` · ${detail}` : ''}`
    } else if (now - activity > lim.stuckMs) {
      group = 'stuck'
      note = `no progress${status === 'shell' ? ' · running a shell command' : ''}${detail ? ` · ${detail}` : ''}`
    } else {
      group = 'working'
    }
  } else if (state === 'done' || state === 'stopped') {
    since = jobAt || activity
    group = parkOr('done', since)
    note = flat(s(job.output?.result)) || detail || state
  } else {
    // An interactive session sitting at its prompt.
    group = 'idle'
    since = statusAt || activity
    note = 'at its prompt · your turn when you want it'
  }

  return {
    key: s(a.id) || s(a.sessionId) || String(a.pid ?? ''),
    id: s(a.id),
    sessionId: s(a.sessionId),
    name: flat(s(a.name)) || s(a.id) || 'unnamed',
    cwd: s(a.cwd),
    kind,
    pid: live ? (a.pid as number) : 0,
    group,
    since,
    note,
    reply: flat(s(job.suggestedReply)),
    stamp: jobAt || statusAt,
  }
}

export const ORDER: readonly Group[] = ['needs', 'stuck', 'working', 'idle', 'done', 'parked']

/** Longest-waiting first where you owe something; most recent first elsewhere. */
export function sortRows(rows: readonly Row[]): Row[] {
  const rank = (r: Row) => ORDER.indexOf(r.group)
  const live = (r: Row) => (r.pid > 0 ? 0 : 1)
  return [...rows].sort((a, b) => {
    if (rank(a) !== rank(b)) return rank(a) - rank(b)
    if (a.group === 'needs' || a.group === 'stuck') {
      return live(a) - live(b) || a.since - b.since
    }
    return b.since - a.since
  })
}

/** `~/.claude/projects/<this>/<sessionId>.jsonl`: every non-alphanumeric becomes '-'. */
export const projectDir = (cwd: string): string => cwd.replace(/[^a-zA-Z0-9]/g, '-')

export function age(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000))
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h${String(m % 60).padStart(2, '0')}`
  return `${Math.floor(h / 24)}d`
}

export function clip(text: string, width: number): string {
  const t = flat(text)
  return t.length <= width ? t : `${t.slice(0, Math.max(0, width - 1))}…`
}

export function folder(cwd: string, home: string): string {
  const rel = home !== '' && cwd.startsWith(home) ? `~${cwd.slice(home.length)}` : cwd
  const parts = rel.split('/').filter(Boolean)
  return parts.length <= 2 ? rel : parts.slice(-2).join('/')
}
