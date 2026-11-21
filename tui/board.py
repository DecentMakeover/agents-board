# /// script
# requires-python = ">=3.11"
# dependencies = ["textual==8.2.8"]
# ///
"""Agents board: a full-screen view of every Claude Code session on this Mac.

Groups every session (interactive tabs and background jobs, all folders) into
Needs you > Stuck > Working > Idle > Done, with a live timer each, and jumps to
one in cmux on Enter. Same rules as the agents-board mod's hooks/classify.ts.

Run:  uv run --script board.py          (or ./board, the launcher next to it)
      uv run --script board.py --demo   (made-up sessions, to try it or take a screenshot)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Group as RGroup
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

HOME = Path.home()
CLAUDE = HOME / ".claude"
CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
STATE = CLAUDE / "agents-board"
STUCK_MS = 15 * 60_000
PARK_MS = 2 * 86_400_000
DONE_SHOWN = 8
SELFTEST = "--selftest" in sys.argv[1:]
DEMO = "--demo" in sys.argv[1:] or SELFTEST
SELF = Path(__file__).resolve()
SELF_MTIME = SELF.stat().st_mtime

# ── palette ────────────────────────────────────────────────────────────────
BG = "#0d1017"
PANEL = "#131821"
LINE = "#222937"
BAND = "#182030"     # header bands: a folder / section starts here
TEXT = "#e3e8f1"
SOFT = "#a3adbf"
DIM = "#6c7689"
FAINT = "#434b5a"
ACCENT = "#b39dfa"
AMBER = "#f5c35b"
ORANGE = "#ff9f5a"
RED = "#ff6b6b"
TEAL = "#5fe3c0"
SLATE = "#8c95a8"
AMBER_SOFT = "#9c8247"   # needs you, but its process has exited ("asleep")
RED_SOFT = "#a35a5a"
BLUE = "#7f9cc9"     # done: its own calm colour, not faded out

GROUPS: dict[str, dict[str, str]] = {
    "needs": {"title": "NEEDS YOU", "color": AMBER, "glyph": "●", "chip": "need you"},
    "stuck": {"title": "STUCK", "color": RED, "glyph": "▲", "chip": "stuck"},
    "working": {"title": "WORKING", "color": TEAL, "glyph": "◆", "chip": "working"},
    "idle": {"title": "IDLE AT PROMPT", "color": SLATE, "glyph": "◇", "chip": "idle"},
    "done": {"title": "DONE · LAST 2 DAYS", "color": DIM, "glyph": "✓", "chip": "done"},
    "parked": {"title": "PARKED · NOT RUNNING, OLDER THAN 2 DAYS", "color": FAINT, "glyph": "·", "chip": "parked"},
}
ORDER = ["needs", "stuck", "working", "idle", "done", "parked"]


# ── data ───────────────────────────────────────────────────────────────────
@dataclass
class Row:
    key: str
    id: str
    session_id: str
    name: str
    cwd: str
    kind: str  # "bg" | "tab"
    pid: int
    group: str
    since: float  # epoch ms the timer counts from
    note: str
    reply: str = ""
    stamp: float = 0
    state: str = ""
    status: str = ""
    asking: str = ""
    latest: str = ""
    intent: str = ""
    result: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        v = json.loads(path.read_text())
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _flat(t: str) -> str:
    return " ".join(t.split())


def _iso_ms(v: Any) -> float:
    try:
        return datetime.fromisoformat(_s(v).replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return 0


def project_dir(cwd: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in cwd)


def classify(a: dict[str, Any], job: dict[str, Any], pidf: dict[str, Any], transcript_at: float, now: float) -> Row:
    pid = a.get("pid") if isinstance(a.get("pid"), int) else 0
    live = pid > 0
    kind = "tab" if a.get("kind") == "interactive" else "bg"
    job_at = _iso_ms(job.get("updatedAt"))
    status_at = float(pidf.get("statusUpdatedAt") or 0)
    activity = max(job_at, status_at, transcript_at, float(a.get("startedAt") or 0))
    status = _s(a.get("status")) or _s(pidf.get("status"))
    state = _s(a.get("state"))
    detail = _flat(_s(job.get("detail")))
    needs = _flat(_s(job.get("needs")))
    inflight = job.get("inFlight") or {}
    tasks = inflight.get("tasks") or 0
    kinds = ", ".join(inflight.get("kinds") or [])

    def park(g: str, at: float) -> str:
        return "parked" if not live and now - at > PARK_MS else g

    since, note = activity, detail
    if status == "waiting":
        group = "needs"
        since = status_at or job_at or activity
        note = needs or detail or (_s(a.get("waitingFor")) or "waiting for you")
    elif state == "blocked":
        since = job_at or activity
        group = park("needs", since)
        note = needs or detail or "needs input"
    elif state == "failed":
        since = job_at or activity
        group = park("needs", since)
        note = f"failed · {detail}"
    elif state == "working" or (kind == "tab" and status in ("busy", "shell")):
        if not live and kind == "bg":
            group = park("stuck", activity)
            note = f"process gone while working · {detail}"
        elif tasks > 0 and job.get("tempo") == "idle":
            group = "working"
            note = f"waiting on its background {kinds or 'task'}" + (f" · {detail}" if detail else "")
        elif now - activity > STUCK_MS:
            group = "stuck"
            note = "no progress" + (" · running a shell command" if status == "shell" else "") + (f" · {detail}" if detail else "")
        else:
            group = "working"
    elif state in ("done", "stopped"):
        since = job_at or activity
        group = park("done", since)
        note = _flat(_s((job.get("output") or {}).get("result"))) or detail or state
    else:
        group = "idle"
        since = status_at or activity
        note = "sitting at its prompt"

    return Row(
        key=_s(a.get("id")) or _s(a.get("sessionId")) or str(pid),
        id=_s(a.get("id")),
        session_id=_s(a.get("sessionId")),
        name=_flat(_s(a.get("name"))) or _s(a.get("id")) or "unnamed",
        cwd=_s(a.get("cwd")),
        kind=kind,
        pid=pid if live else 0,
        group=group,
        since=since,
        note=note,
        reply=_flat(_s(job.get("suggestedReply"))),
        stamp=job_at or status_at,
        state=state,
        status=status,
        asking=needs,
        latest=detail,
        intent=_s(job.get("intent")).strip(),
        result=_flat(_s((job.get("output") or {}).get("result"))),
        extra={"waitingFor": _s(a.get("waitingFor")), "tasks": kinds if tasks else ""},
    )


def sort_rows(rows: list[Row]) -> list[Row]:
    def k(r: Row) -> tuple:
        rank = ORDER.index(r.group)
        if r.group in ("needs", "stuck"):
            return (rank, 0 if r.pid else 1, r.since)
        return (rank, 0, -r.since)

    return sorted(rows, key=k)


def demo_rows() -> list[Row]:
    """Made-up sessions across a few repos, timed relative to now."""
    now = time.time() * 1000
    m = 60_000
    H = str(HOME)

    def r(key: str, name: str, cwd: str, group: str, mins: float, note: str, **kw: Any) -> Row:
        row = Row(key=key, id=key if kw.get("kind", "bg") == "bg" else "", session_id=f"{key}-demo", name=name,
                  cwd=f"{H}/{cwd}", kind=kw.get("kind", "bg"), pid=kw.get("pid", 4242), group=group,
                  since=now - mins * m, note=note, reply=kw.get("reply", ""), state=kw.get("state", ""),
                  asking=kw.get("asking", ""), latest=kw.get("latest", ""), intent=kw.get("intent", ""),
                  result=kw.get("result", ""))
        return row

    rows = [
        r("a1f0c2d1", "migrate auth to passkeys", "code/webapp", "needs", 47,
          "Which rollout: staged by region, or everyone at once?", reply="staged by region",
          asking="Which rollout: staged by region, or everyone at once? Staged keeps password login for 2 weeks.",
          latest="Passkey enrolment flow done; 38 tests pass.", intent="Replace password login with passkeys, keep a fallback for old browsers."),
        r("b7e21a90", "flaky checkout e2e", "code/shop", "needs", 12,
          "permission · run the full e2e suite (about 18 min)?", asking="Run the full e2e suite (about 18 min)?",
          latest="Narrowed the flake to a race in the cart service.", intent="Find out why the checkout e2e test fails 1 in 10 runs."),
        r("c3d4e5f6", "q3 metrics report", "work/analytics", "needs", 60 * 5 + 20,
          "OK to email the draft to the team list?", pid=0, reply="yes, send it",
          asking="The draft is ready. OK to email it to the team list?", intent="Build the Q3 metrics report from the warehouse."),
        r("d9e8f7a6", "nightly data backfill", "code/pipeline", "stuck", 27,
          "no progress · running a shell command · backfilling 2024-03", latest="backfilling 2024-03",
          intent="Backfill the events table for 2024 after the schema change."),
        r("e1a2b3c4", "refactor billing service", "code/payments", "working", 0.2,
          "Running the test suite (412 passed so far)", latest="Running the test suite (412 passed so far)"),
        r("f5e6d7c8", "docs site redesign", "code/docs", "working", 3,
          "waiting on its background dev server · rebuilding pages", latest="rebuilding pages"),
        r("a9b8c7d6", "dependency upgrades", "code/webapp", "working", 1.5, "Bumping react-router to 7; fixing 3 type errors"),
        r("tab-01", "scratch", "code", "idle", 60 * 3, "sitting at its prompt", kind="tab", pid=5151),
        r("tab-02", "api-explorer", "code/api", "idle", 60 * 26, "sitting at its prompt", kind="tab", pid=5252),
        r("0d1e2f3a", "fix login redirect loop", "code/webapp", "done", 35, "Fixed: the session cookie path was /app. PR #482 is open.",
          result="Fixed: the session cookie path was /app. PR #482 is open."),
        r("4b5c6d7e", "write release notes", "code/webapp", "done", 60 * 2, "Release notes for 3.8 drafted in CHANGELOG.md."),
        r("8f9a0b1c", "triage new issues", "code/shop", "done", 60 * 9, "Labelled 23 issues; 4 need a decision from you."),
    ]
    rows += [r(f"p{i:07d}", f"old experiment {i}", "code/sandbox", "parked", 60 * 24 * (3 + i), "needs input", pid=0) for i in range(6)]
    return sort_rows(rows)


def load_rows() -> list[Row]:
    if DEMO:
        return demo_rows()
    # --all: finished background sessions whose process has exited are left out otherwise.
    out = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:200])
    start = out.stdout.find("[")
    agents = json.loads(out.stdout[start:]) if start >= 0 else []
    now = time.time() * 1000
    rows = []
    for a in agents:
        job = _read_json(CLAUDE / "jobs" / a["id"] / "state.json") if a.get("id") else {}
        pidf, t_at = {}, 0.0
        if isinstance(a.get("pid"), int):
            pidf = _read_json(CLAUDE / "sessions" / f"{a['pid']}.json")
            if a.get("cwd") and a.get("sessionId"):
                try:
                    t_at = (CLAUDE / "projects" / project_dir(a["cwd"]) / f"{a['sessionId']}.jsonl").stat().st_mtime * 1000
                except OSError:
                    pass
        rows.append(classify(a, job, pidf, t_at, now))
    return sort_rows(rows)


# ── formatting ─────────────────────────────────────────────────────────────
def age(ms: float) -> str:
    sec = max(0, int(ms // 1000))
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    if m < 60:
        return f"{m}m {sec % 60:02d}s"
    h = m // 60
    if h < 48:
        return f"{h}h {m % 60:02d}m"
    return f"{h // 24}d {h % 24:02d}h"


def urgency(row: Row, ms: float) -> str:
    if row.group == "needs":
        return AMBER if ms < 30 * 60_000 else ORANGE if ms < 4 * 3600_000 else RED
    if row.group == "stuck":
        return RED
    return GROUPS[row.group]["color"] if row.group == "working" else DIM


def short_age(ms: float) -> str:
    """One unit, for the list: now · 12m · 5h · 3d."""
    sec = max(0, int(ms // 1000))
    if sec < 60:
        return "now"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def state_look(r: Row) -> tuple[str, str]:
    """(word, colour) for a row's state; an asleep session that needs you is softer."""
    live = bool(r.pid)
    if r.group == "needs" and r.state == "failed":
        return "failed", RED if live else RED_SOFT
    if r.group == "needs":
        return "needs you", AMBER if live else AMBER_SOFT
    if r.group == "stuck":
        return "stuck", RED if live else RED_SOFT
    return {"working": ("working", TEAL), "idle": ("idle", SLATE), "done": ("done", BLUE),
            "parked": ("parked", DIM)}[r.group]


def short_folder(cwd: str) -> str:
    rel = "~" + cwd[len(str(HOME)):] if cwd.startswith(str(HOME)) else cwd
    parts = [p for p in rel.split("/") if p]
    return rel if len(parts) <= 2 else "/".join(parts[-2:])


def chip(label: str, color: str, *, solid: bool = True) -> Text:
    # Styled as a span, not the Text's base style, so text appended after it stays plain.
    t = Text()
    t.append(f" {label} ", style=f"bold {BG} on {color}" if solid else f"bold {color}")
    return t


PILL_WIDTH = 11


def status_pill(r: Row) -> Text:
    """The row's state in words, in a fixed-width column. What needs you is a solid
    block (needs you / failed / stuck); what doesn't is coloured text only."""
    if r.group == "needs" and r.state == "failed":
        label, color, solid = "FAILED", RED, True
    elif r.group == "needs":
        label, color, solid = "NEEDS YOU", AMBER, True
    elif r.group == "stuck":
        label, color, solid = "STUCK", RED, True
    elif r.group == "working":
        label, color, solid = "WORKING", TEAL, False
    elif r.group == "idle":
        label, color, solid = "IDLE", SLATE, False
    elif r.group == "done":
        label, color, solid = "DONE", DIM, False
    else:
        label, color, solid = "PARKED", FAINT, False
    t = Text()
    t.append(f" {label} ", style=f"bold {BG} on {color}" if solid else f"bold {color}")
    return t


# ── persisted per-viewer marks ─────────────────────────────────────────────
def _marks(name: str) -> dict[str, float]:
    return _read_json(STATE / f"{name}.json")


def _save_marks(name: str, marks: dict[str, float]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{name}.json").write_text(json.dumps(marks))


# ── cmux jump ──────────────────────────────────────────────────────────────
def _cmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([CMUX, *args], capture_output=True, text=True, timeout=10, env={**os.environ, "CMUX_QUIET": "1"})


def jump(row: Row) -> str:
    if DEMO:
        return f"demo mode · this would jump to {row.name}"
    attach = f"claude attach {row.id}" if row.id else ""
    if not Path(CMUX).exists():
        return f"cmux not found · run: {attach}" if attach else f"pid {row.pid} in {row.cwd}"
    sessions = _read_json(HOME / ".cmuxterm" / "claude-hook-sessions.json").get("sessions") or {}
    own = sessions.get(row.session_id) or {}
    if own.get("surfaceId") and own.get("workspaceId") and row.pid and own.get("pid") == row.pid:
        a = _cmux("select-workspace", "--workspace", own["workspaceId"])
        b = _cmux("focus-panel", "--panel", own["surfaceId"], "--workspace", own["workspaceId"])
        return f"Jumped to {row.name}" if a.returncode == b.returncode == 0 else f"cmux: {(a.stderr or b.stderr).strip()[:120]}"
    if not row.id:
        return f"{row.name} is not in a cmux tab (pid {row.pid})"
    title = f"attach · {row.name[:32]}"
    ls = _cmux("--json", "workspace", "list")
    if ls.returncode == 0:
        try:
            spaces = json.loads(ls.stdout).get("workspaces") or []
            hit = next((w for w in spaces if w.get("custom_title") == title), None) or next(
                (w for w in spaces if w.get("title") == row.name), None
            )
            if hit and _cmux("select-workspace", "--workspace", hit["id"]).returncode == 0:
                return f"Switched to {row.name}"
        except Exception:
            pass
    made = _cmux("new-workspace", "--name", title, "--cwd", row.cwd or str(HOME), "--command", attach, "--focus", "true")
    return f"Opened {row.name} in a new tab" if made.returncode == 0 else f"cmux: {made.stderr.strip()[:120]} · run: {attach}"


def cmux_notify(title: str, body: str) -> None:
    if Path(CMUX).exists():
        try:
            _cmux("notify", "--title", title, "--body", body)
        except Exception:
            pass


def start_background(cwd: str, task: str) -> tuple[str, str]:
    """`claude --bg ["<task>"]` in `cwd`; with no task the session starts idle, waiting
    for its first prompt. Answers (job id, message); the id is empty on failure.

    Always a background session, never a plain interactive one: it shows on the board
    with its status, and closing the side pane only detaches it.
    """
    try:
        out = subprocess.run(["claude", "--bg", *([task] if task else [])], cwd=cwd, capture_output=True,
                             text=True, timeout=60, stdin=subprocess.DEVNULL)
    except Exception as err:
        return "", f"could not start claude: {err}"
    m = re.search(r"claude attach ([0-9a-f]{6,})", out.stdout)
    if out.returncode != 0 or not m:
        why = (out.stderr or out.stdout).strip().splitlines()
        return "", why[-1][:160] if why else f"claude exited {out.returncode}"
    return m.group(1), f"Started a session in {short_folder(cwd)}"


ARCHIVE = CLAUDE / "session-archive"
ARCHIVABLE = {"done", "failed", "stopped", "blocked"}


def archive_session(row: Row) -> tuple[bool, str]:
    """Archive the way the session-archive mod does, so `/unarchive` and its pane see it:
    copy the transcript (and its folder) to ~/.claude/session-archive/<sessionId>/, `claude rm`
    the session, then write meta.json. Nothing is marked archived unless `claude rm` worked.
    """
    if row.kind != "bg" or not row.id:
        return False, f"{row.name} is an interactive tab: close its tab to end it"
    if row.state not in ARCHIVABLE:
        return False, f"{row.name} is still {row.state or 'running'}: let it finish or stop it first"
    if row.pid:
        # Make the transcript final before copying it.
        subprocess.run(["claude", "stop", row.id], capture_output=True, text=True, timeout=60)
    dest = ARCHIVE / row.session_id
    dest.mkdir(parents=True, exist_ok=True)
    src = CLAUDE / "projects" / project_dir(row.cwd) / f"{row.session_id}.jsonl"
    if not src.exists():
        src = next(iter((CLAUDE / "projects").glob(f"*/{row.session_id}.jsonl")), src)
    has_transcript = src.exists()
    if has_transcript:
        try:
            shutil.copy2(src, dest / "transcript.jsonl")
            if (dest / "transcript.jsonl").stat().st_size != src.stat().st_size:
                return False, f"{row.name}: copy size mismatch, not archived"
            sibling = src.with_suffix("")
            if sibling.is_dir():
                shutil.copytree(sibling, dest / "transcript-files", dirs_exist_ok=True)
        except OSError as err:
            return False, f"{row.name}: copy failed ({err}), not archived"
    rm = subprocess.run(["claude", "rm", row.id], capture_output=True, text=True, timeout=60)
    if rm.returncode != 0:
        return False, f"{row.name}: claude rm refused: {(rm.stderr or rm.stdout).strip()[:160]}"
    job = _read_json(CLAUDE / "jobs" / row.id / "state.json")
    meta = {
        "id": row.id,
        "sessionId": row.session_id,
        "name": row.name,
        "cwd": row.cwd,
        "state": row.state,
        "detail": row.latest,
        "intent": _s(job.get("displayIntent")) or _s(job.get("intent")) or row.intent,
        "archivedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "hasTranscript": has_transcript,
        "transcriptPath": str(src) if has_transcript else "",
    }
    (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return True, f"Archived {row.name}  ·  /unarchive {row.id} brings it back"


def restore_session(session_id: str) -> tuple[bool, str]:
    """Undo an archive the way the session-archive mod's /unarchive does: put the transcript
    back if it is gone, `claude --bg --resume` it (idle, same id and name), mark meta.json."""
    dest = ARCHIVE / session_id
    meta = _read_json(dest / "meta.json")
    if not meta:
        return False, "nothing archived to restore"
    name, cwd = _s(meta.get("name")), _s(meta.get("cwd"))
    tp = Path(_s(meta.get("transcriptPath")))
    if meta.get("hasTranscript") and _s(meta.get("transcriptPath")) and not tp.exists():
        try:
            tp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest / "transcript.jsonl", tp)
            if (dest / "transcript-files").is_dir():
                shutil.copytree(dest / "transcript-files", tp.with_suffix(""), dirs_exist_ok=True)
        except OSError as err:
            return False, f"{name}: could not put its transcript back ({err})"
    if not cwd or not Path(cwd).is_dir():
        return False, f"{name}: its folder {cwd} is gone"
    named = ["--name", name] if name and name != _s(meta.get("id")) else []
    out = subprocess.run(["claude", "--bg", "--resume", session_id, *named], cwd=cwd, capture_output=True,
                         text=True, timeout=60, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        return False, f"{name}: resume failed: {(out.stderr or out.stdout).strip()[:120]}"
    meta["restoredAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return True, f"Restored {name} (idle, waiting for your next message)"


UNDO_SECONDS = 8


def _prefs() -> dict[str, Any]:
    return _read_json(STATE / "prefs.json")


def _save_prefs(prefs: dict[str, Any]) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "prefs.json").write_text(json.dumps(prefs))
    except OSError:
        pass


SLOTS = 2              # session columns to the right of the board
BOARD_SHARE = 0.32     # of the workspace width, once the slots are open


class SideViewer:
    """Up to two session columns to the right of the board: board | slot | slot.

    Opening a session attaches it (`claude attach <id>`) in a slot. While there is
    room a new column is split off; after that the least recently used slot gets
    the session (a new tab in that pane, the old one closed), so the two sessions
    you are working with stay up. A session already showing is just focused.
    Closing an attached tab only detaches: the session keeps running. Only works
    when the board itself runs inside cmux (it splits its own tab).
    """

    def __init__(self) -> None:
        self.workspace = os.environ.get("CMUX_WORKSPACE_ID", "")
        self.board_surface = os.environ.get("CMUX_SURFACE_ID", "")
        # Each slot: {"pane", "surface", "key", "used"}; left to right.
        self.slots: list[dict[str, Any]] = []
        self._restore()

    # The board restarts itself on update: keep the slots it opened.
    def _file(self) -> Path:
        return STATE / f"side-{self.workspace or 'none'}.json"

    def _restore(self) -> None:
        saved = _read_json(self._file())
        slots = saved.get("slots")
        if isinstance(slots, list):
            self.slots = [x for x in slots if isinstance(x, dict)]
        elif saved.get("pane") or saved.get("surface"):  # one-slot layout of earlier versions
            self.slots = [{"pane": _s(saved.get("pane")), "surface": _s(saved.get("surface")),
                           "key": _s(saved.get("key")), "used": 0}]

    def _save(self) -> None:
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            self._file().write_text(json.dumps({"slots": self.slots}))
        except OSError:
            pass

    @property
    def available(self) -> bool:
        return bool(self.workspace and self.board_surface and Path(CMUX).exists()) and not DEMO

    @property
    def key(self) -> str:
        """The most recently used slot's session (kept for callers of the one-slot API)."""
        return max(self.slots, key=lambda x: x.get("used", 0)).get("key", "") if self.slots else ""

    def _panes(self) -> list[dict[str, Any]]:
        out = _cmux("--json", "list-panes", "--workspace", self.workspace)
        if out.returncode != 0:
            return []
        try:
            return list(json.loads(out.stdout).get("panes") or [])
        except Exception:
            return []

    def _pane_of(self, surface: str, tries: int = 1) -> str:
        """The pane holding `surface`. Right after a split cmux may not list it yet: retry."""
        for attempt in range(tries):
            for p in self._panes():
                if surface in (p.get("surface_refs") or []) or surface in (p.get("surface_ids") or []):
                    return _s(p.get("ref"))
            if attempt + 1 < tries:
                time.sleep(0.25)
        return ""

    def _prune(self) -> None:
        """Forget slots whose pane the person closed; refind a pane from its surface."""
        panes = self._panes()
        by_ref = {_s(p.get("ref")): p for p in panes} | {_s(p.get("id")): p for p in panes}
        kept = []
        for slot in self.slots:
            pane = by_ref.get(slot.get("pane", ""))
            if pane is None and slot.get("surface"):
                ref = next((_s(p.get("ref")) for p in panes
                            if slot["surface"] in (p.get("surface_refs") or []) + (p.get("surface_ids") or [])), "")
                pane = by_ref.get(ref)
                slot["pane"] = ref
            if pane is None:
                continue
            refs = set(pane.get("surface_refs") or []) | set(pane.get("surface_ids") or [])
            if slot.get("surface") not in refs:
                slot["surface"] = next(iter(pane.get("surface_refs") or []), "")
            kept.append(slot)
        # Left to right, as on screen.
        x = {_s(p.get("ref")): (p.get("pixel_frame") or {}).get("x", 0) for p in panes}
        self.slots = sorted(kept, key=lambda sl: x.get(sl["pane"], 0))

    def _layout(self) -> None:
        """Board about a third, the slots sharing the rest evenly (cmux halves on each split)."""
        panes = sorted(self._panes(), key=lambda p: (p.get("pixel_frame") or {}).get("x", 0))
        if len(panes) < 2:
            return
        total = sum((p.get("pixel_frame") or {}).get("width", 0) for p in panes)
        board_w = round(total * BOARD_SHARE) if len(panes) > 2 else round(total * 0.42)
        slot_w = (total - board_w) // (len(panes) - 1)
        # Move each slot's left border so the pane before it has its target width.
        for i in range(1, len(panes)):
            panes = sorted(self._panes(), key=lambda p: (p.get("pixel_frame") or {}).get("x", 0))
            if i >= len(panes):
                return
            want = board_w if i == 1 else slot_w
            have = (panes[i - 1].get("pixel_frame") or {}).get("width", 0)
            delta = have - want
            if abs(delta) > 8:
                _cmux("resize-pane", "--workspace", self.workspace, "--pane", _s(panes[i].get("ref")),
                      "-L" if delta > 0 else "-R", "--amount", str(abs(delta)))

    def show(self, row: Row, focus: bool) -> str:
        return self.show_command(f"claude attach {row.id}", row.key, row.name, focus)

    def show_command(self, command: str, key: str, label: str, focus: bool) -> str:
        self._prune()
        now = time.time()
        hit = next((sl for sl in self.slots if sl.get("key") == key and sl.get("surface")), None)
        if hit is not None:
            hit["used"] = now
            if focus:
                _cmux("focus-panel", "--panel", hit["surface"], "--workspace", self.workspace)
            self._save()
            return f"{label} is already open"
        flag = "true" if focus else "false"
        if len(self.slots) < SLOTS:
            # Room for another column: split off the rightmost pane.
            parent = self.slots[-1]["surface"] if self.slots else self.board_surface
            made = _cmux("new-split", "right", "--workspace", self.workspace, "--surface", parent,
                         "--command", command, "--focus", flag)
            if made.returncode != 0:
                return f"cmux: {made.stderr.strip()[:120]}"
            surface = made.stdout.split()[1] if len(made.stdout.split()) > 1 else ""
            slot = {"pane": self._pane_of(surface, tries=8), "surface": surface, "key": key, "used": now}
            self.slots.append(slot)
            self._layout()
        else:
            # Both columns busy: the least recently used one that isn't held gets this session.
            free = [sl for sl in self.slots if not sl.get("held")]
            if not free:
                return "Both columns are held: press l on one of them to release it"
            slot = min(free, key=lambda sl: sl.get("used", 0))
            made = _cmux("new-surface", "--pane", slot["pane"], "--workspace", self.workspace,
                         "--command", command, "--focus", flag)
            if made.returncode != 0:
                return f"cmux: {made.stderr.strip()[:120]}"
            old = dict(slot)
            slot.clear()
            slot.update(pane=old["pane"], surface=made.stdout.split()[1] if len(made.stdout.split()) > 1 else "",
                        key=key, used=now)
            self._evict(old)
        if focus and slot.get("surface"):
            _cmux("focus-panel", "--panel", slot["surface"], "--workspace", self.workspace)
        self._save()
        return f"{label} opened in column {self.slots.index(slot) + 1} of {len(self.slots)}"

    def _evict(self, slot: dict[str, Any]) -> None:
        """Take a session out of its column without ending it: an attached view is closed (the
        session keeps running); an interactive tab moved here goes back to its own workspace."""
        surface = slot.get("surface", "")
        if not surface:
            return
        if slot.get("kind") == "moved":
            back = _cmux("move-surface", "--surface", surface, "--workspace", slot.get("home", ""), "--focus", "false")
            if back.returncode != 0:
                _cmux("move-tab-to-new-workspace", "--surface", surface, "--workspace", self.workspace,
                      "--title", slot.get("label", "claude"), "--focus", "false")
        else:
            _cmux("close-surface", "--surface", surface, "--workspace", self.workspace, "--force")

    def show_tab(self, row: Row, focus: bool) -> str | None:
        """Bring an interactive session's own cmux tab into a column (moved, not copied: the
        session can't run twice). None when cmux doesn't know where that tab is."""
        sessions = _read_json(HOME / ".cmuxterm" / "claude-hook-sessions.json").get("sessions") or {}
        own = sessions.get(row.session_id) or {}
        tab, home = _s(own.get("surfaceId")), _s(own.get("workspaceId"))
        if not tab or not row.pid or own.get("pid") != row.pid:
            return None
        self._prune()
        now = time.time()
        hit = self.slot_of(row.key)
        if hit is not None and hit.get("surface"):
            hit["used"] = now
            if focus:
                _cmux("focus-panel", "--panel", hit["surface"], "--workspace", self.workspace)
            self._save()
            return f"{row.name} is already open"
        if home == self.workspace:
            home = ""  # it already lives in the board's workspace: send it to a tab of its own later
        flag = "true" if focus else "false"
        if len(self.slots) < SLOTS:
            parent = self.slots[-1]["surface"] if self.slots else self.board_surface
            made = _cmux("new-split", "right", "--workspace", self.workspace, "--surface", parent, "--focus", "false")
            if made.returncode != 0:
                return f"cmux: {made.stderr.strip()[:120]}"
            placeholder = made.stdout.split()[1] if len(made.stdout.split()) > 1 else ""
            pane = self._pane_of(placeholder, tries=8)
            moved = _cmux("move-surface", "--surface", tab, "--pane", pane, "--workspace", self.workspace, "--focus", flag)
            _cmux("close-surface", "--surface", placeholder, "--workspace", self.workspace, "--force")
            if moved.returncode != 0:
                return f"cmux could not move {row.name}: {moved.stderr.strip()[:100]}"
            slot: dict[str, Any] = {"pane": pane, "surface": tab, "key": row.key, "used": now,
                                    "kind": "moved", "home": home, "label": row.name}
            self.slots.append(slot)
            self._layout()
        else:
            free = [sl for sl in self.slots if not sl.get("held")]
            if not free:
                return "Both columns are held: press l on one of them to release it"
            slot = min(free, key=lambda sl: sl.get("used", 0))
            moved = _cmux("move-surface", "--surface", tab, "--pane", slot["pane"], "--workspace", self.workspace, "--focus", flag)
            if moved.returncode != 0:
                return f"cmux could not move {row.name}: {moved.stderr.strip()[:100]}"
            old = dict(slot)
            slot.clear()
            slot.update(pane=old["pane"], surface=tab, key=row.key, used=now, kind="moved", home=home, label=row.name)
            self._evict(old)
        if focus:
            _cmux("focus-panel", "--panel", tab, "--workspace", self.workspace)
        self._save()
        return f"{row.name} moved into column {self.slots.index(slot) + 1} (it goes back to its tab when replaced)"

    def open_row(self, row: Row, focus: bool) -> str | None:
        """Open any session in a column: attach a background one, move an interactive tab."""
        return self.show(row, focus) if row.kind == "bg" and row.id else self.show_tab(row, focus)

    def slot_of(self, key: str) -> dict[str, Any] | None:
        return next((sl for sl in self.slots if sl.get("key") == key), None)

    def hold(self, row: Row) -> str:
        """Toggle holding the column that shows `row`: a held column is never replaced by
        another session. A session not open yet is opened (without leaving the board) and held."""
        self._prune()
        slot = self.slot_of(row.key)
        if slot is not None:
            slot["held"] = not slot.get("held")
            self._save()
            n = self.slots.index(slot) + 1
            return (f"Holding {row.name} in column {n}: other sessions use the other column"
                    if slot["held"] else f"Released column {n}: it takes new sessions again")
        msg = self.open_row(row, focus=False)
        if msg is None:
            return f"{row.name}: cmux doesn't know which tab it runs in, so it can't be moved into a column"
        slot = self.slot_of(row.key)
        if slot is None:
            return msg
        slot["held"] = True
        self._save()
        return f"Opened and holding {row.name} in column {self.slots.index(slot) + 1}"

    def drop(self, key: str) -> None:
        """Take `key` out of its column (its session was archived)."""
        self._prune()
        for slot in [sl for sl in self.slots if sl.get("key") == key]:
            self._evict(slot)
            self.slots.remove(slot)
        self._save()

    def close(self) -> str:
        self._prune()
        if not self.slots:
            return "No session columns open"
        for slot in self.slots:
            self._evict(slot)
        n = len(self.slots)
        self.slots = []
        self._save()
        return f"Closed {n} session column{'s' if n > 1 else ''} (the sessions keep running; moved tabs went home)"


# ── app ────────────────────────────────────────────────────────────────────
class Board(App[None]):
    TITLE = "agents"
    CSS = f"""
    Screen {{ background: {BG}; }}
    #top {{ height: 3; padding: 1 2 0 2; background: {BG}; }}
    #filter {{ dock: top; display: none; margin: 0 2; border: tall {LINE}; background: {PANEL}; }}
    #filter.on {{ display: block; }}
    #body {{ height: 1fr; }}
    #list {{
        width: 3fr; background: {BG}; border: none; padding: 0 1 0 2;
        scrollbar-size-vertical: 1; scrollbar-color: {LINE}; scrollbar-background: {BG};
    }}
    #list:focus {{ border: none; }}
    #list > .option-list--option {{ padding: 0 1; }}
    #top {{ height: 3; padding: 1 3 0 3; }}
    #list > .option-list--option-highlighted {{ background: #1a2130; text-style: none; }}
    #list:focus > .option-list--option-highlighted {{ background: #1f2840; text-style: none; }}
    #list > .option-list--option-disabled {{ text-style: none; color: {TEXT}; }}
    #list > .option-list--option-hover {{ background: #161c28; }}
    #side {{ width: 2fr; min-width: 44; background: {PANEL}; border-left: vkey {LINE}; padding: 1 3; }}
    #side.hidden {{ display: none; }}
    Footer {{ background: {BG}; }}
    Footer > .footer--key {{ color: {ACCENT}; background: {BG}; }}
    Footer > .footer--description {{ color: {DIM}; }}
    """

    BINDINGS = [
        # The footer shows the everyday keys; ? lists the rest.
        Binding("enter", "jump", "open", priority=False),
        Binding("c", "new_session", "new"),
        Binding("a", "archive", "archive"),
        Binding("f", "pin", "pin/unpin"),
        Binding("z", "undo_archive", "undo archive", show=False),
        Binding("l", "hold", "hold column", show=False),
        Binding("slash", "filter", "filter"),
        Binding("question_mark", "keys", "keys"),
        Binding("space", "peek", "peek", show=False),
        Binding("x", "close_side", "close sessions", show=False),
        Binding("g", "toggle_grouping", "by folder/status", show=False),
        Binding("i", "toggle_info", "info", show=False),
        Binding("h", "hide", "hide", show=False),
        Binding("u", "unhide", "unhide all", show=False),
        Binding("p", "toggle_parked", "parked", show=False),
        Binding("d", "toggle_done", "all done", show=False),
        Binding("n", "toggle_alerts", "alerts", show=False),
        Binding("r", "reload", "refresh", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("escape", "cancel_input", show=False),
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[Row] = []
        self.loaded_at = 0.0
        self.error = ""
        self.show_parked = False
        self.show_done = False
        self.alerts = True
        self.primed = False
        self.query_text = ""
        self.shown: list[Row] = []
        self.side = SideViewer()
        self.restart = False
        # Archives wait UNDO_SECONDS before they happen, so z can take one back untouched.
        self.pending_archive: dict[str, tuple[Row, Any, bool]] = {}   # key -> (row, timer, was pinned)
        self.archived: list[tuple[str, bool]] = []                    # (session id, was pinned), newest last
        self.testing = False
        self.rejected_mtime = 0.0
        self.by_folder = bool(_prefs().get("by_folder"))
        self._pins: set[str] = set()
        self.cursor_key = ""
        self.show_info = bool(_prefs().get("show_info"))

    # layout
    def compose(self) -> ComposeResult:
        yield Static(id="top")
        yield Input(placeholder="filter by name, folder or question…  (esc clears)", id="filter")
        with Horizontal(id="body"):
            yield OptionList(id="list")
            with VerticalScroll(id="side"):
                yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self.query_one("#list", OptionList).focus()
        self.load()
        self.set_interval(5, self.load)
        self.set_interval(1, self.tick)
        self.set_interval(3, self.check_update)
        self.check_update()
        if os.environ.pop("AGENTS_BOARD_UPDATED", None):
            self.notify("Board updated to the latest version", timeout=6)

    def check_update(self) -> None:
        """Heartbeat for /board, and a newer board.py on disk (an update): restart into it,
        but only once the new version has passed its self-test."""
        ws = os.environ.get("CMUX_WORKSPACE_ID", "")
        if ws and not DEMO:
            try:
                STATE.mkdir(parents=True, exist_ok=True)
                (STATE / f"running-{ws}.json").write_text(json.dumps({"pid": os.getpid(), "at": time.time() * 1000}))
            except OSError:
                pass
        try:
            mtime = SELF.stat().st_mtime
        except OSError:
            return
        if mtime != SELF_MTIME and mtime != self.rejected_mtime and not self.testing:
            self.testing = True
            self.try_update(mtime)

    @work(thread=True, exclusive=True, group="update")
    def try_update(self, mtime: float) -> None:
        # Draw the new version headlessly first: one that fails stays out of the way.
        try:
            out = subprocess.run([sys.executable, str(SELF), "--selftest"], capture_output=True,
                                 text=True, timeout=90)
            ok = out.returncode == 0
            why = (out.stderr or out.stdout).strip().splitlines()[-1:] if not ok else []
        except Exception as err:  # a hung or missing interpreter counts as a failure
            ok, why = False, [str(err)]
        self.testing = False
        if ok and SELF.stat().st_mtime == mtime:
            self.restart = True
            self.call_from_thread(self.exit)
        elif not ok:
            self.rejected_mtime = mtime
            self.call_from_thread(self.notify, f"Update skipped: the new version failed its self-test ({(why or ['?'])[0][:80]})",
                                  severity="warning", timeout=8)

    def on_resize(self) -> None:
        # The detail panel is off by default (the session columns sit beside the board);
        # `i` brings it back where there is room.
        self.query_one("#side").set_class(not self.show_info or self.size.width < 110, "hidden")
        self.render_list()

    def action_toggle_info(self) -> None:
        self.show_info = not self.show_info
        prefs = _prefs()
        prefs["show_info"] = self.show_info
        _save_prefs(prefs)
        if self.show_info and self.size.width < 110:
            self.notify("Not enough room for the info panel here: widen the board", timeout=3)
        self.on_resize()

    # data
    @work(thread=True, exclusive=True, group="load")
    def load(self) -> None:
        try:
            rows = load_rows()
            self.call_from_thread(self.apply, rows, "")
        except Exception as err:  # keep the last good rows on screen
            self.call_from_thread(self.apply, None, str(err))

    def apply(self, rows: list[Row] | None, error: str) -> None:
        self.error = error
        if rows is not None:
            self.rows = rows
            self.loaded_at = time.time() * 1000
            self.alert_new(rows)
        self.render_list()

    def alert_new(self, rows: list[Row]) -> None:
        if DEMO:
            return
        hot = [r for r in rows if r.group in ("needs", "stuck")]
        seen = _marks("notified")
        fresh = [r for r in hot if seen.get(r.key) != r.since]
        _save_marks("notified", {r.key: r.since for r in hot})
        if not self.primed:
            self.primed = True
            return
        if not self.alerts or not fresh:
            return
        for r in fresh[:3]:
            what = "is stuck" if r.group == "stuck" else "needs you"
            self.notify(f"{r.note[:120]}", title=f"{r.name} {what}", severity="warning", timeout=10)
        self.bell()
        cmux_notify(
            f"Claude: {len(fresh)} session{'s' if len(fresh) > 1 else ''} need{'s' if len(fresh) == 1 else ''} you",
            "\n".join(f"{r.name} — {r.note[:80]}" for r in fresh),
        )

    def visible(self) -> list[Row]:
        hidden = _marks("hidden")
        q = self.query_text.lower()
        out = []
        for r in self.rows:
            if hidden.get(r.key) == r.stamp or r.key in self.pending_archive:
                continue
            if q and q not in f"{r.name} {r.cwd} {r.note}".lower():
                continue
            out.append(r)
        return out

    # drawing
    def row_prompt(self, r: Row, now: float) -> Table:
        # Pinned rows sit outside their folder, so they name it.
        return self._row_prompt(r, now, show_folder=not self.by_folder or self.is_pinned(r))

    # pins: sessions kept in their own section at the top, by session id (stable across resumes)
    def pins(self) -> set[str]:
        return set(_marks("pins"))

    def is_pinned(self, r: Row) -> bool:
        return (r.session_id or r.key) in self._pins

    def pinned_header_prompt(self, count: int) -> Table:
        label = Text("★ Pinned", style=f"bold {ACCENT}")
        return self._band(label, Text(f"{count}", style=SOFT))

    def _band(self, left: Text, right: Text) -> RGroup:
        """A section's header: a tinted full-width band with a rule under it, so it is
        plain where one folder ends and the next begins."""
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1, justify="right", no_wrap=True)
        grid.add_row(left, right)
        return RGroup(Padding(grid, (0, 0), style=f"on {BAND}", expand=True), Rule(style=LINE, characters="─"))

    def _row_prompt(self, r: Row, now: float, show_folder: bool) -> Table:
        """name · state · age on one line, the same type for every state: only the state word
        carries colour. The row under the cursor also shows what it asks or is doing."""
        ms = now - r.since
        word, color = state_look(r)
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(width=9, justify="right", no_wrap=True)
        grid.add_column(width=4, justify="right", no_wrap=True)
        name = Text(r.name, style=TEXT)
        if show_folder:
            name.append(f"   {short_folder(r.cwd)}", style=FAINT)
        if r.kind == "tab":
            name.append("   tab", style=ACCENT)
        slot = self.side.slot_of(r.key) if self.side.available else None
        if slot is not None:
            name.append("   ◨ held" if slot.get("held") else "   ◨ open", style=f"bold {ACCENT}" if slot.get("held") else FAINT)
        grid.add_row(name, Text(word, style=color), Text(short_age(ms), style=DIM))
        if self.cursor_key == r.key and r.note:
            note = r.note
            if r.reply and r.group in ("needs", "stuck"):
                note = f"{note}  →  {r.reply}"
            grid.add_row(Text(f"  {note}", style=DIM, overflow="ellipsis", no_wrap=True), "", "")
        return grid

    def header_prompt(self, group: str, count: int) -> Table:
        g = GROUPS[group]
        return self._band(Text(g["title"].title(), style=f"bold {g['color']}"), Text(f"{count}", style=SOFT))

    def _gap(self, options: list[Option]) -> int:
        """Air above a section: two lines between sections, one at the very top."""
        return 2 if options else 1

    def folder_header_prompt(self, cwd: str, members: list[Row]) -> Table:
        where = short_folder(cwd) or "?"
        head, _, leaf = where.rpartition("/")
        left = Text(leaf, style=f"bold {TEXT}")
        if head:
            left.append(f"   {head}/", style=FAINT)
        words = {"needs": "need you", "stuck": "stuck", "working": "working", "idle": "idle", "done": "done", "parked": "parked"}
        right = Text()
        for g in ORDER:
            n = sum(1 for r in members if r.group == g)
            if n:
                if right.plain:
                    right.append("   ")
                word, color = {"needs": (words[g], AMBER), "stuck": (words[g], RED), "working": (words[g], TEAL),
                               "idle": (words[g], SLATE), "done": (words[g], BLUE), "parked": (words[g], DIM)}[g]
                right.append(f"{n} ", style=f"bold {color}")
                right.append(word, style=SOFT)
        return self._band(left, right)

    def folder_options(self, rows: list[Row], now: float, before: list[Option] | None = None) -> list[Option]:
        """Sessions grouped by folder: folders that need you first, then the most recently active."""
        rows = [r for r in rows if r.group != "parked" or self.show_parked]
        folders: dict[str, list[Row]] = {}
        for r in rows:
            folders.setdefault(r.cwd, []).append(r)

        def urgency_key(item: tuple[str, list[Row]]) -> tuple:
            members = item[1]
            live = [r.since for r in members if r.group in ("needs", "stuck", "working", "idle")]
            return (min(ORDER.index(r.group) for r in members), -(max(live) if live else max(r.since for r in members)))

        options: list[Option] = []
        for cwd, members in sorted(folders.items(), key=urgency_key):
            options.append(Option(Padding(self.folder_header_prompt(cwd, members), (self._gap((before or []) + options), 0, 0, 0)), id=f"hdr:{cwd}", disabled=True))
            done = [r for r in members if r.group in ("done", "parked")]
            active = [r for r in members if r.group not in ("done", "parked")]
            limit = len(done) if self.show_done else 3
            for r in active + done[:limit]:
                options.append(Option(self.row_prompt(r, now), id=f"row:{r.key}"))
                self.shown.append(r)
            if len(done) > limit:
                options.append(Option(Text(f"   + {len(done) - limit} more done  ·  d shows all", style=FAINT), id=f"more:{cwd}", disabled=True))
        return options

    def render_list(self) -> None:
        ol = self.query_one("#list", OptionList)
        now = time.time() * 1000
        rows = self.visible()
        keep = ol.highlighted_option.id if ol.highlighted_option is not None else None
        options: list[Option] = []
        self.shown = []
        self._pins = self.pins()
        pinned = [r for r in rows if self.is_pinned(r)]
        rows = [r for r in rows if not self.is_pinned(r)]
        if pinned:
            options.append(Option(Padding(self.pinned_header_prompt(len(pinned)), (1, 0, 0, 0)), id="hdr:pinned", disabled=True))
            for r in pinned:
                options.append(Option(self.row_prompt(r, now), id=f"row:{r.key}"))
                self.shown.append(r)
        if self.by_folder:
            options += self.folder_options(rows, now, before=options)
        groups = [] if self.by_folder else [g for g in ORDER if g != "parked" or self.show_parked]
        for g in groups:
            members = [r for r in rows if r.group == g]
            if not members and g not in ("needs",):
                continue
            options.append(Option(Padding(self.header_prompt(g, len(members)), (self._gap(options), 0, 0, 0)), id=f"hdr:{g}", disabled=True))
            if not members:
                options.append(Option(Text("   nothing is waiting on you", style=f"italic {DIM}"), id="empty:needs", disabled=True))
            limit = len(members) if (g != "done" or self.show_done) else DONE_SHOWN
            for r in members[:limit]:
                options.append(Option(self.row_prompt(r, now), id=f"row:{r.key}"))
                self.shown.append(r)
            if len(members) > limit:
                options.append(Option(Text(f"   + {len(members) - limit} more  ·  d shows all", style=FAINT), id=f"more:{g}", disabled=True))
        ol.set_options(options)
        ids = [o.id for o in options]
        if keep in ids:
            ol.highlighted = ids.index(keep)
        elif ol.highlighted is None or (ol.highlighted < len(options) and options[ol.highlighted].disabled):
            first = next((i for i, o in enumerate(options) if not o.disabled), None)
            ol.highlighted = first
        self.render_top()
        self.render_detail()

    def tick(self) -> None:
        ol = self.query_one("#list", OptionList)
        now = time.time() * 1000
        by_key = {r.key: r for r in self.shown}
        for i in range(ol.option_count):
            opt = ol.get_option_at_index(i)
            if opt.id and opt.id.startswith("row:") and (r := by_key.get(opt.id[4:])):
                ol.replace_option_prompt_at_index(i, self.row_prompt(r, now))
        self.render_top()
        self.render_detail()

    def render_top(self) -> None:
        """◆ agents, then only the counts worth a glance; problems on the right."""
        rows = self.visible()
        count = {g: sum(1 for r in rows if r.group == g) for g in ORDER}
        grid = Table.grid(expand=True)
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1, justify="right", no_wrap=True)
        left = Text("◆ ", style=f"bold {ACCENT}")
        left.append("agents", style=f"bold {TEXT}")
        if DEMO:
            left.append("  demo", style=f"italic {DIM}")
        left.append("     ")
        if count["needs"]:
            left.append_text(chip(str(count["needs"]), AMBER))
            left.append(" need you", style=SOFT)
        else:
            left.append("nothing needs you", style=f"italic {DIM}")
        if count["stuck"]:
            left.append("     ")
            left.append_text(chip(str(count["stuck"]), RED))
            left.append(" stuck", style=SOFT)
        if count["working"]:
            left.append(f"     {count['working']}", style=f"bold {TEAL}")
            left.append(" working", style=DIM)
        right = Text()
        if self.error:
            right.append(f"⚠ {self.error[:50]}", style=RED)
        elif self.loaded_at and time.time() * 1000 - self.loaded_at > 30_000:
            right.append(f"stale · synced {short_age(time.time() * 1000 - self.loaded_at)} ago", style=RED)
        elif not self.alerts:
            right.append("alerts off", style=FAINT)
        grid.add_row(left, right)
        self.query_one("#top", Static).update(grid)

    def current(self) -> Row | None:
        ol = self.query_one("#list", OptionList)
        opt = ol.highlighted_option
        if opt is None or not opt.id or not opt.id.startswith("row:"):
            return None
        return next((r for r in self.shown if r.key == opt.id[4:]), None)

    def render_detail(self) -> None:
        r = self.current()
        box = self.query_one("#detail", Static)
        if r is None:
            box.update(Text("No session selected.", style=DIM))
            return
        g = GROUPS[r.group]
        now = time.time() * 1000
        ms = now - r.since
        verb = {"needs": "waiting", "stuck": "quiet for", "working": "active", "idle": "idle for", "done": "finished", "parked": "parked"}[r.group]
        when = datetime.fromtimestamp(r.since / 1000).strftime("%a %d %b %H:%M") if r.since else "—"

        parts: list[Any] = []
        title = Text(r.name, style=f"bold {TEXT}")
        parts.append(title)
        status = chip(g["title"].split(" ·")[0], g["color"])
        status.append(f"  {verb} ", style=SOFT)
        status.append(age(ms) + (" ago" if r.group == "done" else ""), style=f"bold {urgency(r, ms)}")
        status.append(f"   since {when}", style=FAINT)
        parts += [Text(""), status, Text("")]

        facts = Table.grid(padding=(0, 2))
        facts.add_column(style=FAINT, no_wrap=True)
        facts.add_column(style=SOFT)
        facts.add_row("folder", r.cwd.replace(str(HOME), "~"))
        facts.add_row("session", f"background job {r.id}" if r.kind == "bg" else "interactive tab")
        facts.add_row("process", f"running · pid {r.pid}" if r.pid else "not running · Enter resumes it")
        if r.extra.get("tasks"):
            facts.add_row("in flight", r.extra["tasks"])
        parts.append(facts)

        def section(label: str, body: Any) -> None:
            parts.append(Text(""))
            parts.append(Text(label, style=f"bold {FAINT}"))
            parts.append(body)

        if r.asking:
            section("ASKING YOU", Text(r.asking, style=TEXT))
        if r.reply:
            parts.append(Text(""))
            parts.append(Panel(Text(r.reply, style=TEXT), title="suggested reply", title_align="left", border_style=ACCENT, padding=(0, 1)))
        if r.result and r.group in ("done", "parked"):
            section("RESULT", Text(r.result, style=SOFT))
        if r.latest and r.latest not in (r.asking, r.result):
            section("LATEST", Text(r.latest, style=SOFT))
        if r.intent:
            intent = r.intent if len(r.intent) <= 420 else r.intent[:419] + "…"
            section("ORIGINAL ASK", Text(intent, style=DIM))

        parts.append(Text(""))
        hint = Text("⏎ ", style=f"bold {ACCENT}")
        if self.opens_on_side(r):
            hint.append("open on the right", style=DIM)
            hint.append("   space ", style=f"bold {ACCENT}")
            hint.append("peek", style=DIM)
        else:
            hint.append("jump to its tab" if Path(CMUX).exists() else f"claude attach {r.id}", style=DIM)
        if r.kind == "bg" and r.state in ARCHIVABLE:
            hint.append("   a ", style=f"bold {ACCENT}")
            hint.append("archive", style=DIM)
        hint.append("   h ", style=f"bold {ACCENT}")
        hint.append("hide", style=DIM)
        parts.append(hint)
        box.update(RGroup(*parts))

    # events
    @on(OptionList.OptionHighlighted)
    def _highlighted(self) -> None:
        # The row under the cursor shows its second line: redraw when it moves.
        r = self.current()
        key = r.key if r else ""
        if key != self.cursor_key:
            self.cursor_key = key
            self.tick()
        self.render_detail()

    @on(OptionList.OptionSelected)
    def _selected(self) -> None:
        self.action_jump()

    @on(Input.Changed, "#filter")
    def _filter(self, e: Input.Changed) -> None:
        self.query_text = e.value.strip()
        self.render_list()

    @on(Input.Submitted, "#filter")
    def _filter_done(self) -> None:
        self.query_one("#list", OptionList).focus()

    # actions
    def action_jump(self) -> None:
        r = self.current()
        if r is not None:
            self.run_jump(r, True)

    def action_peek(self) -> None:
        r = self.current()
        if r is not None:
            self.run_jump(r, False)

    def action_close_side(self) -> None:
        self.run_close_side()

    def opens_on_side(self, r: Row) -> bool:
        return self.side.available and (r.kind == "bg" and bool(r.id) or r.kind == "tab" and bool(r.pid))

    @work(thread=True, exclusive=True, group="jump")
    def run_jump(self, r: Row, focus: bool) -> None:
        # Background sessions open in the split on the right; an interactive tab
        # can't be attached elsewhere, so that one still switches to its own tab.
        msg = (self.side.open_row(r, focus) if self.opens_on_side(r) else None) or jump(r)
        self.call_from_thread(self.notify, msg, timeout=3)
        self.call_from_thread(self.tick)

    @work(thread=True, exclusive=True, group="jump")
    def run_close_side(self) -> None:
        msg = self.side.close()
        self.call_from_thread(self.notify, msg, timeout=3)

    def action_archive(self) -> None:
        r = self.current()
        if r is None:
            return
        if DEMO:
            self.notify(f"demo mode · this would archive {r.name}", timeout=3)
            return
        if r.kind != "bg" or not r.id:
            self.notify(f"{r.name} is an interactive tab: close its tab to end it", severity="warning", timeout=4)
            return
        if r.state not in ARCHIVABLE:
            self.notify(f"{r.name} is still {r.state or 'running'}: let it finish or stop it first", severity="warning", timeout=4)
            return
        # Gone from the list now; archived for real in a few seconds unless z takes it back.
        timer = self.set_timer(UNDO_SECONDS, lambda key=r.key: self.commit_archive(key))
        self.pending_archive[r.key] = (r, timer, self.is_pinned(r))
        self.notify(f"Archived {r.name}  ·  z to undo", timeout=UNDO_SECONDS)
        self.render_list()

    def commit_archive(self, key: str) -> None:
        item = self.pending_archive.get(key)
        if item is not None:
            self.run_archive(item[0], item[2])

    @work(thread=True, group="archive")
    def run_archive(self, r: Row, was_pinned: bool) -> None:
        ok, msg = archive_session(r)

        def settle() -> None:
            self.pending_archive.pop(r.key, None)
            if ok:
                pins = _marks("pins")
                if pins.pop(r.session_id or r.key, None) is not None:
                    _save_marks("pins", pins)
                self.archived.append((r.session_id, was_pinned))
                self.rows = [x for x in self.rows if x.key != r.key]
            else:
                self.notify(msg, severity="warning", timeout=6)
            self.render_list()

        if ok:
            self.side.drop(r.key)
        self.call_from_thread(settle)
        self.call_from_thread(self.load)

    def action_hold(self) -> None:
        r = self.current()
        if r is None:
            return
        if not self.side.available:
            self.notify("Holding a column needs the board running in cmux", timeout=3)
            return
        self.run_hold(r)

    @work(thread=True, exclusive=True, group="jump")
    def run_hold(self, r: Row) -> None:
        msg = self.side.hold(r)
        self.call_from_thread(self.notify, msg, timeout=4)
        self.call_from_thread(self.tick)

    def action_undo_archive(self) -> None:
        # Still waiting: cancel it, and the session is back exactly as it was.
        if self.pending_archive:
            key = list(self.pending_archive)[-1]
            r, timer, _ = self.pending_archive.pop(key)
            timer.stop()
            self.notify(f"Kept {r.name}", timeout=3)
            self.render_list()
            return
        if not self.archived:
            self.notify("Nothing archived from this board to undo", timeout=3)
            return
        sid, was_pinned = self.archived.pop()
        self.run_restore(sid, was_pinned)

    @work(thread=True, group="archive")
    def run_restore(self, session_id: str, was_pinned: bool) -> None:
        ok, msg = restore_session(session_id)
        if ok and was_pinned:
            pins = _marks("pins")
            pins[session_id] = time.time()
            _save_marks("pins", pins)
        self.call_from_thread(self.notify, msg, severity="information" if ok else "warning", timeout=6)
        self.call_from_thread(self.load)

    def finish_pending_archives(self) -> None:
        """Closing or updating the board: carry out the archives still waiting."""
        for r, timer, was_pinned in list(self.pending_archive.values()):
            timer.stop()
            ok, _ = archive_session(r)
            if ok:
                pins = _marks("pins")
                if pins.pop(r.session_id or r.key, None) is not None:
                    _save_marks("pins", pins)
        self.pending_archive.clear()

    def action_hide(self) -> None:
        r = self.current()
        if r is None:
            return
        hidden = _marks("hidden")
        hidden[r.key] = r.stamp
        _save_marks("hidden", hidden)
        self.notify(f"Hid {r.name} until it changes  ·  u unhides all", timeout=3)
        self.render_list()

    def action_unhide(self) -> None:
        _save_marks("hidden", {})
        self.render_list()

    def action_toggle_parked(self) -> None:
        self.show_parked = not self.show_parked
        self.render_list()

    def action_toggle_done(self) -> None:
        self.show_done = not self.show_done
        self.render_list()

    def action_toggle_alerts(self) -> None:
        self.alerts = not self.alerts
        self.notify(f"alerts {'on' if self.alerts else 'off'}", timeout=2)
        self.render_top()

    def action_reload(self) -> None:
        self.load()

    def action_cursor_down(self) -> None:
        self.query_one("#list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#list", OptionList).action_cursor_up()

    def action_pin(self) -> None:
        r = self.current()
        if r is None:
            return
        pins = _marks("pins")
        sid = r.session_id or r.key
        if sid in pins:
            pins.pop(sid)
            msg = f"Unpinned {r.name}"
        else:
            pins[sid] = time.time()
            msg = f"Pinned {r.name}  ·  f again unpins"
        _save_marks("pins", pins)
        self.notify(msg, timeout=3)
        self.render_list()

    def action_keys(self) -> None:
        self.notify(
            "⏎ open beside the board   space peek   l hold / release a column   x close sessions\n"
            "c new session here (opens beside the board)   a archive · z undo   f pin · unpin\n"
            "h hide · u unhide\n"
            "g by folder / by status   i info panel   / filter\n"
            "p parked   d all done   n alerts   r refresh   q quit",
            title="keys", timeout=12,
        )

    def action_toggle_grouping(self) -> None:
        self.by_folder = not self.by_folder
        prefs = _prefs()
        prefs["by_folder"] = self.by_folder
        _save_prefs(prefs)
        self.notify(f"Grouped by {'folder' if self.by_folder else 'status'}", timeout=2)
        self.render_list()

    def action_new_session(self) -> None:
        """Start an idle background session in the selected session's folder, straight away,
        and open it beside the board with the cursor in it: type its first prompt there."""
        r = self.current()
        if r is None or not r.cwd:
            self.notify("Pick a session first: the new one starts in its folder", timeout=3)
            return
        self.notify(f"Starting a session in {short_folder(r.cwd)}…", timeout=2)
        self.run_new_session(r.cwd, "")

    @work(thread=True, group="new")
    def run_new_session(self, cwd: str, task: str) -> None:
        job, msg = start_background(cwd, task)
        if not job:
            self.call_from_thread(self.notify, msg, severity="error", timeout=6)
            return
        self.call_from_thread(self.load)
        if self.side.available:
            # With a task, watch it start without leaving the board; without one,
            # put the cursor in it so the first prompt can be typed right away.
            label = task[:40] or f"new session in {short_folder(cwd)}"
            self.side.show_command(f"claude attach {job}", job, label, focus=not task)
            msg += " · on the right" + ("" if task else ": type its first prompt there")
        else:
            msg += f" · claude attach {job}"
        self.call_from_thread(self.notify, msg, timeout=5)

    def action_cancel_input(self) -> None:
        self.action_clear_filter()

    def action_filter(self) -> None:
        f = self.query_one("#filter", Input)
        f.add_class("on")
        f.focus()

    def action_clear_filter(self) -> None:
        f = self.query_one("#filter", Input)
        f.value = ""
        f.remove_class("on")
        self.query_text = ""
        self.query_one("#list", OptionList).focus()
        self.render_list()


async def _selftest() -> None:
    """Draw the board headlessly on demo sessions, both views and a pinned row; raise on error."""
    app = Board()
    async with app.run_test(size=(100, 40)) as pilot:
        for _ in range(40):
            await pilot.pause(0.1)
            if app.rows:
                break
        assert app.rows, "no rows loaded"
        first = app.rows[0]
        app.pins = lambda: {first.session_id or first.key}  # type: ignore[method-assign]
        for by_folder in (False, True):
            app.by_folder = by_folder
            app.render_list()
            app.tick()
            app.render_top()
            app.render_detail()
            await pilot.pause(0.1)
        assert app.shown, "nothing drawn"


if __name__ == "__main__":
    if SELFTEST:
        import asyncio
        asyncio.run(_selftest())
        sys.exit(0)
    app = Board()
    app.run()
    app.finish_pending_archives()
    if app.restart:
        os.environ["AGENTS_BOARD_UPDATED"] = "1"
        os.execv(sys.executable, [sys.executable, str(SELF), *sys.argv[1:]])
