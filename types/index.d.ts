export type Group = 'needs' | 'stuck' | 'working' | 'idle' | 'done' | 'parked'

/** One session as the board shows it. */
export type Row = {
  /** The agents-list id (background) or the session id (interactive). */
  key: string
  /** Background job id; empty for an interactive session. */
  id: string
  sessionId: string
  name: string
  cwd: string
  kind: 'bg' | 'tty'
  pid: number
  group: Group
  /** Epoch ms the timer counts from (waiting since / quiet since / done at). */
  since: number
  /** Short reason: what it asks, what it waits on, why it looks stuck. */
  note: string
  /** The classifier's suggested answer, when it has one. */
  reply: string
  /** The job file's updatedAt, used to un-hide a row whose state moved. */
  stamp: number
}

export type Board = {
  rows: Row[]
  at: number
  error: string
  loading: boolean
}

export type View = {
  showParked: boolean
  showDone: boolean
  notify: boolean
  message: string
}

declare module 'claude-code' {
  interface PluginState {
    'agents-board': {
      board: Shaped<Board>
      view: Shaped<View>
    }
  }
}
