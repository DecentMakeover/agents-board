import { expect, test } from 'claude-code/testing'

import { age, classify, projectDir, sortRows } from '../hooks/classify'

const NOW = Date.parse('2026-11-16T14:00:00Z')
const iso = (minsAgo: number) => new Date(NOW - minsAgo * 60_000).toISOString()
const HOME = '/Users/me'

test('classify: waiting, blocked, quiet, background task, gone, parked', async () => {
  const waiting = classify(
    { id: 'w1', kind: 'background', state: 'blocked', status: 'waiting', waitingFor: 'input needed', pid: 5 },
    { job: { updatedAt: iso(90), needs: 'Which branch?' }, pidFile: { statusUpdatedAt: NOW - 30 * 60_000 } },
    NOW,
  )
  expect(waiting.group).toBe('needs')
  expect(waiting.since).toBe(NOW - 30 * 60_000)
  expect(waiting.note).toBe('input needed · Which branch?')

  const oldBlockedDead = classify({ id: 'b1', kind: 'background', state: 'blocked' }, { job: { updatedAt: iso(60 * 72) } }, NOW)
  expect(oldBlockedDead.group).toBe('parked')
  const recentBlockedDead = classify({ id: 'b2', kind: 'background', state: 'blocked' }, { job: { updatedAt: iso(60 * 8) } }, NOW)
  expect(recentBlockedDead.group).toBe('needs')

  const quiet = classify(
    { id: 'q1', kind: 'background', state: 'working', status: 'shell', pid: 7 },
    { job: { updatedAt: iso(40), tempo: 'active', inFlight: { tasks: 0 } }, transcriptAt: NOW - 25 * 60_000 },
    NOW,
  )
  expect(quiet.group).toBe('stuck')
  expect(quiet.note.startsWith('no progress · running a shell command')).toBe(true)
  expect(quiet.since).toBe(NOW - 25 * 60_000)

  const bgTask = classify(
    { id: 't1', kind: 'background', state: 'working', status: 'busy', pid: 8 },
    { job: { updatedAt: iso(120), tempo: 'idle', inFlight: { tasks: 1, kinds: ['monitor'] } } },
    NOW,
  )
  expect(bgTask.group).toBe('working')
  expect(bgTask.note.startsWith('waiting on background monitor')).toBe(true)

  const gone = classify({ id: 'g1', kind: 'background', state: 'working' }, { job: { updatedAt: iso(60) } }, NOW)
  expect(gone.group).toBe('stuck')

  const tab = classify({ sessionId: 's-tab', kind: 'interactive', status: 'idle', pid: 9 }, { pidFile: { statusUpdatedAt: NOW - 5000 } }, NOW)
  expect(tab.group).toBe('idle')
  expect(tab.key).toBe('s-tab')

  const sorted = sortRows([tab, bgTask, quiet, waiting, recentBlockedDead, gone])
  expect(sorted.map(r => r.key)).toEqual(['w1', 'b2', 'q1', 'g1', 't1', 's-tab'])
  expect(age(3 * 3600_000 + 5 * 60_000)).toBe('3h05')
  expect(projectDir('/Users/me/code/my_app.v2')).toBe('-Users-me-code-my-app-v2')
})

const AGENTS = JSON.stringify([
  { id: 'aaaa1111', sessionId: 'sa', name: 'release checklist', cwd: `${HOME}/code/webapp`, kind: 'background', state: 'blocked', status: 'waiting', waitingFor: 'input needed', pid: 11 },
  { id: 'bbbb2222', sessionId: 'sb', name: 'long render', cwd: `${HOME}/code/ml`, kind: 'background', state: 'working', status: 'shell', pid: 12 },
  { id: 'cccc3333', sessionId: 'sc', name: 'fresh work', cwd: `${HOME}/code/tools`, kind: 'background', state: 'working', status: 'busy', pid: 13 },
  { sessionId: 'sd', name: 'api tab', cwd: `${HOME}/code/api`, kind: 'interactive', status: 'idle', pid: 14 },
  { id: 'eeee5555', sessionId: 'se', name: 'ancient', cwd: HOME, kind: 'background', state: 'blocked' },
])
const JOBS: Record<string, object> = {
  aaaa1111: { updatedAt: iso(130), needs: 'Which release branch should I cut?', tempo: 'blocked' },
  bbbb2222: { updatedAt: iso(45), detail: 'batch 10 of 40', tempo: 'active', inFlight: { tasks: 0 } },
  cccc3333: { updatedAt: iso(1), detail: 'Running tests', tempo: 'active', inFlight: { tasks: 0 } },
  eeee5555: { updatedAt: iso(60 * 24 * 30) },
}

const PANE = {
  plugin: 'agents-board',
  component: 'Pane',
  requestId: 'agents-board',
  props: { title: 'Agents', isFocused: true, bodyColumns: 90, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
} as const

test('/board groups every session and Enter jumps through cmux', async ($, on) => {
  const ran: string[][] = []
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'sc' }))
  on('clock.now', () => ({ value: NOW }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  const store = new Map<string, unknown>()
  on('store.get', (_, e) => ({ value: store.get(e.key) }))
  on('store.set', (_, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  on('process.run', (_, e) => {
    const argv = [...e.argv]
    ran.push(argv)
    const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
    if (argv[0] === '/bin/sh' && argv[2]?.includes('command -v uv')) return ok('/usr/bin/uv\n')
    if (argv[0] === '/bin/sh') return ok(HOME)
    if (argv[0] === 'claude' && argv[1] === 'agents') return ok(AGENTS)
    if (argv.includes('workspace') && argv.includes('list')) return ok(JSON.stringify({ workspaces: [{ id: 'WS-B', title: 'long render', custom_title: null }] }))
    return ok()
  })
  on('fs.read', (_, e) => {
    const m = /\/jobs\/([^/]+)\/state\.json$/.exec(e.path)
    if (m && m[1] && JOBS[m[1]]) return { value: JSON.stringify(JOBS[m[1]]) }
    if (e.path.endsWith('claude-hook-sessions.json')) {
      return { value: JSON.stringify({ sessions: { sd: { pid: 14, surfaceId: 'SURF-D', workspaceId: 'WS-D' } } }) }
    }
    throw new Error('ENOENT')
  })
  on('fs.stat', (_, e) => {
    if (e.path.includes('/sb.jsonl')) return { value: { kind: 'file', size: 1, mtimeMs: NOW - 30 * 60_000, isLink: false } }
    if (e.path.includes('/sc.jsonl')) return { value: { kind: 'file', size: 1, mtimeMs: NOW - 20_000, isLink: false } }
    throw new Error('ENOENT')
  })
  on('fs.exists', () => ({ value: true }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  // Plain /board outside a cmux workspace it can place (identify answers nothing) opens the shared board tab.
  const full = await $.command.run({ command: 'board', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })
  expect(full.text).toBe('Opened the agents board in its own cmux tab.')
  const tab = ran.find(a => a.includes('new-workspace'))
  expect(tab?.slice(-4, -2)).toEqual(['--command', expect.stringMatching(/'\/usr\/bin\/uv' run --quiet --script '.*\/tui\/board\.py'$/)])
  ran.length = 0

  const out = await $.command.run({ command: 'board', args: 'pane', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })
  expect(out.text).toBe('Agents board opened: 1 need you, 1 stuck, 5 sessions in all.')

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /^NEEDS YOU\s+1$/ })) !== undefined).toBe(true)
    expect((await ui.find({ type: 'Text', text: /^STUCK .*\s1$/ })) !== undefined).toBe(true)
    expect((await ui.find({ type: 'Text', text: /^WORKING\s+1$/ })) !== undefined).toBe(true)
    expect((await ui.find({ type: 'Text', text: /Which release branch/ })) !== undefined).toBe(true)
    expect((await ui.find({ type: 'Text', text: /ancient/ })) === undefined).toBe(true)
    await ui.unmount()
  }

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'go:aaaa1111' })
  const made = ran.find(a => a.includes('new-workspace'))
  expect(made?.slice(-6)).toEqual(['--cwd', `${HOME}/code/webapp`, '--command', 'claude attach aaaa1111', '--focus', 'true'])

  // A bg session already attached in a cmux tab (titled with its name) is switched to, not reopened.
  await ui.press({ key: 'go:bbbb2222' })
  expect(ran.filter(a => a.includes('new-workspace')).length).toBe(1)
  expect(ran.some(a => a.includes('select-workspace') && a.includes('WS-B'))).toBe(true)

  await ui.press({ key: 'go:sd' })
  expect(ran.some(a => a.includes('focus-panel') && a.includes('SURF-D'))).toBe(true)
  expect(ran.some(a => a.includes('select-workspace') && a.includes('WS-D'))).toBe(true)
  await ui.unmount()
})

test('/board tab reuses a live board tab and replaces a dead one', async ($, on) => {
  let beatAt = NOW - 2000
  const ran: string[][] = []
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'self' }))
  on('clock.now', () => ({ value: NOW }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', (_, e) => {
    if (e.path.endsWith('/running-WS-BOARD.json')) return { value: JSON.stringify({ pid: 1, at: beatAt }) }
    if (e.path.endsWith('/running-WS-DEAD.json')) return { value: JSON.stringify({ pid: 2, at: NOW - 3_000_000 }) }
    throw new Error('ENOENT')
  })
  on('process.run', (_, e) => {
    const argv = [...e.argv]
    ran.push(argv)
    const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
    if (argv[0] === '/bin/sh' && argv[2]?.includes('command -v uv')) return ok('/usr/bin/uv\n')
    if (argv[0] === '/bin/sh') return ok(HOME)
    if (argv.includes('workspace') && argv.includes('list')) {
      // A dead board tab left at a shell, listed before the live one.
      return ok(JSON.stringify({ workspaces: [{ id: 'WS-DEAD', custom_title: '◆ agents board' }, { id: 'WS-BOARD', custom_title: '◆ agents board' }] }))
    }
    return ok()
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const run = () => $.command.run({ command: 'board', args: 'tab', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })

  expect((await run()).text).toBe('Switched to the agents board tab.')
  expect(ran.some(a => a.includes('select-workspace') && a.includes('WS-BOARD'))).toBe(true)
  expect(ran.some(a => a.includes('close-workspace') && a.includes('WS-DEAD'))).toBe(true)
  expect(ran.some(a => a.includes('close-workspace') && a.includes('WS-BOARD'))).toBe(false)

  beatAt = NOW - 60_000 // the board quit: its heartbeat went stale
  ran.length = 0
  expect((await run()).text).toBe('Opened the agents board in its own cmux tab.')
  expect(ran.some(a => a.includes('close-workspace') && a.includes('WS-BOARD'))).toBe(true)
  expect(ran.some(a => a.includes('new-workspace'))).toBe(true)
})

test('/board opens a board in this workspace, and focuses it once it runs there', async ($, on) => {
  let beat: object | undefined
  const ran: string[][] = []
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'self' }))
  on('clock.now', () => ({ value: NOW }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', (_, e) => {
    if (beat && e.path.endsWith('/running-WS-TWO.json')) return { value: JSON.stringify(beat) }
    throw new Error('ENOENT')
  })
  on('process.run', (_, e) => {
    const argv = [...e.argv]
    ran.push(argv)
    const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
    if (argv[0] === '/bin/sh' && argv[2]?.includes('command -v uv')) return ok('/usr/bin/uv\n')
    if (argv[0] === '/bin/sh') return ok(HOME)
    if (argv.includes('identify')) {
      return ok(JSON.stringify({ focused: { workspace_id: 'WS-TWO', surface_id: 'SURF-CHAT' }, caller: { workspace_id: 'WS-OLD', surface_id: 'X' } }))
    }
    return ok()
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const run = (args = '') => $.command.run({ command: 'board', args, origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })

  // No board in this workspace yet: one is split off to the left of the chat, in this workspace.
  expect((await run()).text).toBe('Opened an agents board in this workspace.')
  const split = ran.find(a => a.includes('new-split'))
  expect(split?.slice(1, 7)).toEqual(['new-split', 'left', '--workspace', 'WS-TWO', '--surface', 'SURF-CHAT'])
  expect(ran.some(a => a.includes('new-workspace') || a.includes('select-workspace'))).toBe(false)

  // Its board now runs: /board focuses that pane instead of opening another.
  beat = { pid: 3, at: NOW - 2000, surface: 'SURF-BOARD' }
  ran.length = 0
  expect((await run()).text).toBe("Switched to this workspace's agents board.")
  expect(ran.some(a => a.includes('focus-panel') && a.includes('SURF-BOARD') && a.includes('WS-TWO'))).toBe(true)
  expect(ran.some(a => a.includes('new-split'))).toBe(false)

  // /board tab still opens the one shared board tab.
  ran.length = 0
  expect((await run('tab')).text).toBe('Opened the agents board in its own cmux tab.')
  expect(ran.some(a => a.includes('new-workspace'))).toBe(true)
})
