#!/usr/bin/env python3
"""
arc_tabs.py: reorganize Arc's Today tabs from the terminal.

    python3 arc_tabs.py                 # pick a space, edit, apply
    python3 arc_tabs.py --space Work
    python3 arc_tabs.py --restore       # roll back to a backup

Flow:
  1. Backs up StorableSidebar.json (kept in .../Arc/sidebar-backups, last 20).
  2. Extracts the space's Today tabs + groups; copies the simplified JSON to
     the clipboard (so you can paste it to Claude).
  3. Opens an interactive editor (keys listed at the bottom of the screen).
     Press p there to paste a reorganized plan back from the clipboard.
  4. On apply: quits Arc, re-reads the file, reconciles any tabs opened/closed
     meanwhile, backs up again, writes the changes.
  5. Reopens Arc and watches briefly for sync reverting the changes.

Only Today (unpinned) is touched. Pinned tabs/folders and Favorites are not.
Note: quitting Arc interrupts anything in progress in it (uploads, unsaved form
text). The first run may ask your terminal for permission to control Arc.
"""
import argparse, copy, curses, json, locale, os, re, shlex, shutil, subprocess
import sys, tempfile, time, uuid
from pathlib import Path

ARC_DIR = Path.home() / "Library/Application Support/Arc"
SRC = ARC_DIR / "StorableSidebar.json"
BACKUP_DIR = ARC_DIR / "sidebar-backups"
KEEP_BACKUPS = 20
APPLE_EPOCH = 978307200
GROUP_KINDS = ("tabGroup", "list")   # Tidy sections are "tabGroup"; folders "list"
TITLE_FIELDS = ("title", "name", "label")
VERIFY_SECONDS = 20


# ───────────────────────── Arc file helpers ─────────────────────────

def kind(it):
    return next(iter(it.get("data") or {}), "unknown")


def kids_key(it):
    return "childrenIDs" if "childrenIDs" in it else "childrenIds"


def kids(it):
    return it.get(kids_key(it)) or []


def group_title(it):
    if it.get("title"):
        return it["title"]
    d = (it.get("data") or {}).get(kind(it))
    if isinstance(d, dict):
        for f in TITLE_FIELDS:
            if isinstance(d.get(f), str) and d[f]:
                return d[f]
    return None


def set_group_title(g, name):
    d = (g.get("data") or {}).get(kind(g))
    if isinstance(d, dict):
        for f in TITLE_FIELDS:
            if f in d:
                d[f] = name
                if g.get("title"):
                    g["title"] = name
                return
    g["title"] = name


def label(it, items):
    k = kind(it)
    if k == "tab":
        return it.get("title") or it["data"]["tab"].get("savedTitle") or "(untitled tab)"
    if k in GROUP_KINDS:
        return group_title(it) or "(untitled group)"
    inner = [label(items[c], items) for c in kids(it) if c in items]
    return f"[{k}] {it.get('title') or ' | '.join(inner)}".strip()


class Sidebar:
    def __init__(self, path=SRC):
        self.data = json.loads(path.read_text())
        self.c = next((c for c in self.data["sidebar"]["containers"]
                       if isinstance(c, dict) and "spaces" in c), None)
        if self.c is None:
            sys.exit("Couldn't find spaces in Arc's file; the format may have changed.")
        self.items = {o["id"]: o for o in self.c["items"] if isinstance(o, dict) and "id" in o}
        self.spaces = [o for o in self.c["spaces"] if isinstance(o, dict) and "id" in o]

    def space(self, sid):
        return next((s for s in self.spaces if s["id"] == sid), None)

    def today_root(self, space):
        cids = space.get("containerIDs", [])
        return self.items.get(dict(zip(cids[::2], cids[1::2])).get("unpinned"))

    def today(self, space):
        """Entries: {"tab": id} or {"group": name, "id": id, "tabs": [ids]}"""
        root = self.today_root(space)
        if root is None:
            sys.exit("Couldn't find this space's Today section.")
        out = []
        for cid in kids(root):
            it = self.items.get(cid)
            if not it:
                continue
            if kind(it) in GROUP_KINDS:
                out.append({"group": group_title(it) or "(untitled group)", "id": cid,
                            "tabs": [k for k in kids(it) if k in self.items]})
            else:
                out.append({"tab": cid})
        return out


def tab_ids_of(entries):
    return [t for e in entries for t in ([e["tab"]] if "tab" in e else e["tabs"])]


def normalize(entries):
    """Ungrouped Today tabs must come before all groups: Arc treats a loose tab
    placed after a group as part of that group. Stable within each kind."""
    return [e for e in entries if "tab" in e] + [e for e in entries if "tab" not in e]


# ───────────────────────── plan text format ─────────────────────────

def export_text(space_name, space_id, entries, titles, n):
    js = lambda s: json.dumps(s, ensure_ascii=False)
    sid = lambda i: js(i[:n])
    blocks = []
    for e in entries:
        if "tab" in e:
            blocks.append(f'  [{sid(e["tab"])}, {js(titles[e["tab"]])}]')
            continue
        head = f'  {{"group": {js(e["group"])}' + (f', "id": {sid(e["id"])}' if e.get("id") else "")
        rows = ",\n".join(f'   [{sid(t)}, {js(titles[t])}]' for t in e["tabs"])
        blocks.append(head + (f', "tabs": [\n{rows}\n  ]}}' if rows else ', "tabs": []}'))
    return (f'{{\n "space": {js(space_name)},\n "spaceId": {js(space_id)},\n "today": [\n'
            + ",\n".join(blocks) + "\n ]\n}\n")


def short_len(ids):
    n = 6
    while n < 64 and len({i[:n].lower() for i in ids}) < len(ids):
        n += 1
    return n


def parse_plan(text, tab_ids, group_ids):
    """Parse a plan (pasted or edited). Returns (entries, n_missing_added)."""
    text = re.sub(r"^\s*```\w*\s*$", "", text.strip(), flags=re.M)
    obj = json.loads(text)
    today = obj.get("today") if isinstance(obj, dict) else obj
    if not isinstance(today, list):
        raise ValueError('No "today" list found.')

    def res(short, pool, what):
        if isinstance(short, list):
            short = short[0] if short else None
        if not isinstance(short, str) or not short:
            raise ValueError(f"Bad {what} entry: {short!r}")
        m = [i for i in pool if i.lower().startswith(short.lower())]
        if len(m) != 1:
            raise ValueError(f"{what} id {short!r} " + ("not found" if not m else "is ambiguous"))
        return m[0]

    entries, seen, used_groups = [], [], set()
    for e in today:
        if isinstance(e, dict) and "group" in e:
            name = str(e["group"] or "").strip()
            if not name:
                raise ValueError("A group has an empty name.")
            gid = res(e["id"], group_ids, "group") if e.get("id") else None
            if gid in used_groups:
                gid = None
            if gid:
                used_groups.add(gid)
            tabs = [res(t, tab_ids, "tab") for t in e.get("tabs", [])]
            seen += tabs
            entries.append({"group": name, "id": gid, "tabs": tabs})
        else:
            t = res(e, tab_ids, "tab")
            seen.append(t)
            entries.append({"tab": t})
    dupes = {t for t in seen if seen.count(t) > 1}
    if dupes:
        raise ValueError(f"{len(dupes)} tab(s) appear more than once.")
    missing = [t for t in tab_ids if t not in seen]
    return [{"tab": t} for t in missing] + entries, len(missing)


# ───────────────────────── system helpers ─────────────────────────

def clip_copy(text):
    try:
        subprocess.run("pbcopy", input=text.encode(), check=True)
        return True
    except Exception:
        return False


def clip_paste():
    return subprocess.run("pbpaste", capture_output=True).stdout.decode("utf-8", "replace")


def arc_running():
    return subprocess.run(["pgrep", "-x", "Arc"], capture_output=True).returncode == 0


def quit_arc():
    if not arc_running():
        return True
    subprocess.run(["osascript", "-e", 'tell application "Arc" to quit'], capture_output=True)
    for _ in range(60):
        if not arc_running():
            time.sleep(1.5)  # let the final save land
            return True
        time.sleep(0.5)
    return False


def backup(tag):
    BACKUP_DIR.mkdir(exist_ok=True)
    dst = BACKUP_DIR / f"StorableSidebar-{time.strftime('%Y%m%d-%H%M%S')}-{tag}.json"
    shutil.copy2(SRC, dst)
    for old in sorted(BACKUP_DIR.glob("StorableSidebar-*.json"))[:-KEEP_BACKUPS]:
        old.unlink()
    return dst


def write_sidebar(data):
    tmp = SRC.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(SRC)


# ───────────────────────── apply ─────────────────────────

def apply_plan(sb, space, entries):
    """Mutates sb.data in place. Returns notes."""
    notes = []
    root = sb.today_root(space)
    cur = sb.today(space)
    cur_groups = {e["id"] for e in cur if "group" in e}
    cur_units = tab_ids_of(cur)
    plan = normalize(copy.deepcopy(entries))

    planned = set(tab_ids_of(plan))
    gone = planned - set(cur_units)
    if gone:
        for e in plan:
            if "group" in e:
                e["tabs"] = [t for t in e["tabs"] if t not in gone]
        plan = [e for e in plan if not ("tab" in e and e["tab"] in gone)]
        notes.append(f"{len(gone)} tab(s) were closed while editing; skipped.")
    new = [u for u in cur_units if u not in planned]
    if new:
        plan = [{"tab": u} for u in new] + plan
        notes.append(f"{len(new)} tab(s) opened while editing; left ungrouped at the top.")

    template = (next((sb.items[g] for g in cur_groups), None) or
                next((o for o in sb.items.values() if kind(o) in GROUP_KINDS), None))
    used, new_root, empty = set(), [], 0
    for e in plan:
        if "tab" in e:
            sb.items[e["tab"]]["parentID"] = root["id"]
            new_root.append(e["tab"])
            continue
        if not e["tabs"]:
            empty += 1
            continue
        gid = e.get("id") if e.get("id") in cur_groups and e.get("id") not in used else None
        if gid:
            g = sb.items[gid]
        else:
            if template is None:
                raise RuntimeError("No existing group to copy for new groups. "
                                   "Create one group in Arc (e.g. Tidy), then retry.")
            g = copy.deepcopy(template)
            g["id"] = str(uuid.uuid4()).upper()
            if isinstance(g.get("createdAt"), (int, float)):
                now = time.time()
                g["createdAt"] = now - APPLE_EPOCH if g["createdAt"] < 1.5e9 else now
            sb.c["items"] += [g["id"], g]
            sb.items[g["id"]] = g
        used.add(g["id"])
        set_group_title(g, e["group"])
        g["parentID"] = root["id"]
        g[kids_key(g)] = list(e["tabs"])
        for t in e["tabs"]:
            sb.items[t]["parentID"] = g["id"]
        new_root.append(g["id"])
    root[kids_key(root)] = new_root

    removed = cur_groups - used
    if removed:
        sb.c["items"] = [x for x in sb.c["items"]
                         if not ((isinstance(x, str) and x in removed) or
                                 (isinstance(x, dict) and x.get("id") in removed))]
        for r in removed:
            sb.items.pop(r, None)
    if empty:
        notes.append(f"{empty} empty group(s) dropped.")
    return notes


def structure(sb, space):
    return [("g", e["group"], tuple(e["tabs"])) if "group" in e else ("t", e["tab"])
            for e in sb.today(space)]


# ───────────────────────── interactive editor ─────────────────────────

HELP = [
    "↑↓/jk move  J/K reorder  space mark  m move-to-group  n new group  r rename  x ungroup",
    "u undo  e $EDITOR  p paste plan  c copy plan  a APPLY  q quit",
]
MENU_KEYS = "123456789abcdefghijklmoprstuvwxyz"


class Editor:
    def __init__(self, entries, titles, n, space_name, space_id, group_ids, note=""):
        self.entries = copy.deepcopy(entries)
        self.orig = json.dumps(entries)
        self.titles, self.n = titles, n
        self.space_name, self.space_id = space_name, space_id
        self.tab_ids = tab_ids_of(entries)
        self.group_ids = group_ids
        self.cur = self.top = 0
        self.marked, self.undo_stack = set(), []
        self.status = note

    # model
    def rows(self):
        r = []
        for ei, e in enumerate(self.entries):
            if "tab" in e:
                r.append(("loose", ei, None))
            else:
                r.append(("group", ei, None))
                r += [("gtab", ei, ti) for ti in range(len(e["tabs"]))]
        return r

    def tab_at(self, row):
        k, ei, ti = row
        if k == "loose":
            return self.entries[ei]["tab"]
        return self.entries[ei]["tabs"][ti] if k == "gtab" else None

    def row_of_tab(self, tid):
        return next((i for i, r in enumerate(self.rows()) if self.tab_at(r) == tid), self.cur)

    def row_of_entry(self, obj):
        return next((i for i, r in enumerate(self.rows())
                     if r[0] == "group" and self.entries[r[1]] is obj), self.cur)

    def snapshot(self):
        self.undo_stack.append(copy.deepcopy(self.entries))
        del self.undo_stack[:-200]

    def dirty(self):
        return json.dumps(self.entries) != self.orig

    def plan_text(self):
        return export_text(self.space_name, self.space_id, self.entries, self.titles, self.n)

    def remove_tab(self, t):
        for i, e in enumerate(self.entries):
            if e.get("tab") == t:
                del self.entries[i]
                return
            if "group" in e and t in e["tabs"]:
                e["tabs"].remove(t)
                return

    def move_tab(self, tid, d):
        E = self.entries
        for ei, e in enumerate(E):
            if e.get("tab") == tid:
                j = ei + d
                if not 0 <= j < len(E):
                    return
                if "tab" in E[j]:
                    E[ei], E[j] = E[j], E[ei]
                else:
                    E.pop(ei)
                    if d > 0:
                        E[j - 1]["tabs"].insert(0, tid)
                    else:
                        E[j]["tabs"].append(tid)
                return
            if "group" in e and tid in e["tabs"]:
                t = e["tabs"]
                ti = t.index(tid)
                k = ti + d
                if 0 <= k < len(t):
                    t[ti], t[k] = t[k], t[ti]
                    return
                j = ei + d
                if 0 <= j < len(E) and "group" in E[j]:
                    t.pop(ti)                       # into the neighbouring group
                    if d > 0:
                        E[j]["tabs"].insert(0, tid)
                    else:
                        E[j]["tabs"].append(tid)
                elif d < 0:
                    t.pop(ti)                       # out of the first group:
                    E.insert(ei, {"tab": tid})      # becomes the last ungrouped tab
                else:
                    pass  # last tab of the last group moving down: nowhere to go
                return

    # ui helpers
    def put(self, scr, y, x, s, attr=0):
        h, w = scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                scr.addnstr(y, x, s, max(0, w - x - 1), attr)
            except curses.error:
                pass

    def prompt(self, scr, msg):
        h, w = scr.getmaxyx()
        scr.move(h - 1, 0)
        scr.clrtoeol()
        self.put(scr, h - 1, 0, msg, curses.A_BOLD)
        curses.echo()
        curses.curs_set(1)
        try:
            s = scr.getstr(h - 1, min(len(msg), w - 2), 200).decode("utf-8", "replace")
        finally:
            curses.noecho()
            curses.curs_set(0)
        return s.strip()

    def confirm(self, scr, msg):
        h, _ = scr.getmaxyx()
        scr.move(h - 1, 0)
        scr.clrtoeol()
        self.put(scr, h - 1, 0, msg + " (y/n) ", curses.A_BOLD)
        return scr.get_wch() in ("y", "Y")

    def menu(self, scr, title, options):
        opts = options[:len(MENU_KEYS)]
        lines = [f" {MENU_KEYS[i]}  {o}" for i, o in enumerate(opts)]
        lines += [" 0  Ungrouped (top of list)", " n  New group…", " q  Cancel"]
        H, W = scr.getmaxyx()
        h = min(len(lines) + 4, H)
        w = min(max(len(x) for x in lines + [title]) + 6, W)
        win = curses.newwin(h, w, max(0, (H - h) // 2), max(0, (W - w) // 2))
        win.keypad(True)
        win.box()
        self.put(win, 1, 2, title, curses.A_BOLD)
        for i, ln in enumerate(lines[: h - 4]):
            self.put(win, i + 3, 1, ln)
        win.refresh()
        while True:
            k = win.get_wch()
            if k in ("q", "\x1b"):
                return None
            if k in ("0", "n"):
                return k
            if isinstance(k, str) and k in MENU_KEYS[:len(opts)]:
                return MENU_KEYS.index(k)

    def draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        rows = self.rows()
        body = max(1, h - 4)
        self.cur = max(0, min(self.cur, len(rows) - 1))
        if self.cur < self.top:
            self.top = self.cur
        if self.cur >= self.top + body:
            self.top = self.cur - body + 1
        ng = sum("group" in e for e in self.entries)
        head = (f" Arc Today · {self.space_name} · {len(self.tab_ids)} tabs, {ng} groups"
                + ("  · modified" if self.dirty() else "")
                + (f"  · {len(self.marked)} marked" if self.marked else ""))
        self.put(scr, 0, 0, head.ljust(w), curses.A_REVERSE)
        for i in range(body):
            idx = self.top + i
            if idx >= len(rows):
                break
            k, ei, _ = rows[idx]
            attr = curses.A_REVERSE if idx == self.cur else 0
            if k == "group":
                g = self.entries[ei]
                self.put(scr, i + 1, 0, f" ▾ {g['group']}  ({len(g['tabs'])})", attr | curses.A_BOLD)
            else:
                tid = self.tab_at(rows[idx])
                mark = "●" if tid in self.marked else " "
                indent = "     " if k == "gtab" else " • "
                self.put(scr, i + 1, 0, f"{indent}{mark} {self.titles[tid]}", attr)
        self.put(scr, h - 3, 0, HELP[0], curses.A_DIM)
        self.put(scr, h - 2, 0, HELP[1], curses.A_DIM)
        self.put(scr, h - 1, 0, self.status)
        scr.refresh()

    def load_plan(self, text, source):
        try:
            entries, added = parse_plan(text, self.tab_ids, self.group_ids)
        except (ValueError, KeyError, json.JSONDecodeError) as ex:
            self.status = f"Couldn't load {source}: {ex}"
            return
        self.snapshot()
        fixed = normalize(entries)
        moved = fixed != entries
        self.entries = fixed
        self.marked.clear()
        self.status = f"Loaded plan from {source}." + (
            f" {added} tab(s) it didn't mention were put at the top." if added else "") + (
            " Ungrouped tabs below a group were moved up to the ungrouped section "
            "(otherwise Arc would add them to the group above)." if moved else "")

    # main loop
    def run(self, scr):
        curses.curs_set(0)
        scr.keypad(True)
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        self.focus = None
        while True:
            self.entries = normalize(self.entries)
            if self.focus is not None:
                self.cur = (self.row_of_tab(self.focus) if isinstance(self.focus, str)
                            else self.row_of_entry(self.focus))
            self.draw(scr)
            k = scr.get_wch()
            rows = self.rows()
            row = rows[self.cur] if rows else None
            self.status = ""
            self.focus = None
            if k in (curses.KEY_UP, "k"):
                self.cur -= 1
            elif k in (curses.KEY_DOWN, "j"):
                self.cur += 1
            elif k == curses.KEY_PPAGE:
                self.cur -= scr.getmaxyx()[0] - 5
            elif k == curses.KEY_NPAGE:
                self.cur += scr.getmaxyx()[0] - 5
            elif k in ("g", curses.KEY_HOME):
                self.cur = 0
            elif k in ("G", curses.KEY_END):
                self.cur = len(rows) - 1
            elif k in ("K", "J", curses.KEY_SR, curses.KEY_SF) and row:
                d = -1 if k in ("K", curses.KEY_SR) else 1
                self.snapshot()
                if row[0] == "group":
                    ei, obj = row[1], self.entries[row[1]]
                    j = ei + d
                    if 0 <= j < len(self.entries) and "group" in self.entries[j]:
                        self.entries[ei], self.entries[j] = self.entries[j], self.entries[ei]
                    elif d < 0:
                        self.status = "Ungrouped tabs always stay above groups in Arc."
                    self.cur = self.row_of_entry(obj)
                else:
                    tid = self.tab_at(row)
                    self.move_tab(tid, d)
                    self.cur = self.row_of_tab(tid)
            elif k == " " and row and row[0] != "group":
                self.marked ^= {self.tab_at(row)}
                self.cur += 1
            elif k == "m":
                order = tab_ids_of(self.entries)
                tids = [t for t in order if t in self.marked] or (
                    [self.tab_at(row)] if row and row[0] != "group" else [])
                if not tids:
                    self.status = "Put the cursor on a tab, or mark tabs with space first."
                    continue
                groups = [e for e in self.entries if "group" in e]
                choice = self.menu(scr, f"Move {len(tids)} tab(s) to:", [g["group"] for g in groups])
                if choice is None:
                    continue
                name = self.prompt(scr, "New group name: ") if choice == "n" else None
                if choice == "n" and not name:
                    continue
                self.snapshot()
                anchor = self.entries[row[1]] if row else None
                anchor_i = row[1] if row else 0
                for t in tids:
                    self.remove_tab(t)
                if choice == "0":
                    self.entries[0:0] = [{"tab": t} for t in tids]
                elif choice == "n":
                    i = next((i for i, e in enumerate(self.entries) if e is anchor),
                             min(anchor_i, len(self.entries)))
                    self.entries.insert(i, {"group": name, "id": None, "tabs": tids})
                else:
                    groups[choice]["tabs"].extend(tids)
                self.marked.clear()
                self.focus = tids[0]
            elif k == "n":
                name = self.prompt(scr, "New group name: ")
                if name:
                    self.snapshot()
                    obj = {"group": name, "id": None, "tabs": []}
                    self.entries.insert(row[1] + 1 if row else 0, obj)
                    self.focus = obj
                    self.status = "Group created. Mark tabs (space) and press m to move them in."
            elif k == "r" and row and row[0] != "loose":
                g = self.entries[row[1]]
                name = self.prompt(scr, f"Rename '{g['group']}' to: ")
                if name:
                    self.snapshot()
                    g["group"] = name
            elif k == "x" and row and row[0] != "loose":
                self.snapshot()
                g = self.entries[row[1]]
                self.entries[row[1]:row[1] + 1] = [{"tab": t} for t in g["tabs"]]
                self.focus = g["tabs"][0] if g["tabs"] else None
                self.status = f"Ungrouped '{g['group']}'; its tabs moved to the ungrouped section."
            elif k == "u":
                if self.undo_stack:
                    self.entries = self.undo_stack.pop()
                    self.status = "Undone."
                else:
                    self.status = "Nothing to undo."
            elif k == "c":
                self.status = "Plan copied to clipboard." if clip_copy(self.plan_text()) \
                    else "Couldn't copy (pbcopy failed)."
            elif k == "p":
                self.load_plan(clip_paste(), "clipboard")
            elif k == "e":
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                    f.write(self.plan_text())
                    path = f.name
                curses.def_prog_mode()
                curses.endwin()
                editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano")
                subprocess.call(editor + [path])
                curses.reset_prog_mode()
                scr.refresh()
                self.load_plan(Path(path).read_text(), "editor")
                os.unlink(path)
            elif k == "a":
                if not self.dirty():
                    self.status = "No changes to apply."
                elif self.confirm(scr, "Apply? Arc will quit, update, and reopen."):
                    return self.entries
            elif k == "q":
                if not self.dirty() or self.confirm(scr, "Discard your changes?"):
                    return None


# ───────────────────────── commands ─────────────────────────

def pick_space(sb, name):
    if name:
        s = next((s for s in sb.spaces if (s.get("title") or "").lower() == name.lower()), None)
        if not s:
            sys.exit("No such space. Available: " +
                     ", ".join(s.get("title") or "(untitled)" for s in sb.spaces))
        return s
    if len(sb.spaces) == 1:
        return sb.spaces[0]
    for i, s in enumerate(sb.spaces, 1):
        print(f"  {i}. {s.get('title') or '(untitled)'}")
    return sb.spaces[int(input("Space number: ")) - 1]


def do_restore():
    backups = sorted(BACKUP_DIR.glob("StorableSidebar-*.json"), reverse=True)
    if not backups:
        sys.exit("No backups found.")
    for i, b in enumerate(backups[:15], 1):
        print(f"  {i}. {b.name}")
    pick = input("Restore which? [1] ").strip() or "1"
    chosen = backups[int(pick) - 1]
    print("Quitting Arc…")
    if not quit_arc():
        sys.exit("Arc didn't quit. Quit it manually and retry.")
    backup("pre-restore")
    shutil.copy2(chosen, SRC)
    subprocess.run(["open", "-a", "Arc"])
    print(f"Restored {chosen.name} and reopened Arc.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--space", help="space name (case-insensitive)")
    ap.add_argument("--restore", action="store_true", help="restore a backup")
    ap.add_argument("--no-verify", action="store_true", help="skip the post-reopen check")
    args = ap.parse_args()
    locale.setlocale(locale.LC_ALL, "")

    if args.restore:
        return do_restore()
    if not SRC.exists():
        sys.exit(f"Not found: {SRC}")

    sb = Sidebar()
    space = pick_space(sb, args.space)
    sid, sname = space["id"], space.get("title") or ""
    b = backup("start")

    entries = sb.today(space)
    titles = {t: label(sb.items[t], sb.items) for t in tab_ids_of(entries)}
    group_ids = [e["id"] for e in entries if "group" in e]
    n = short_len(list(titles) + group_ids)
    text = export_text(sname, sid, entries, titles, n)
    copied = clip_copy(text)
    note = (f"Backed up. Plan {'copied to clipboard' if copied else 'ready'}. "
            "Press p to paste a reorganized plan back.")

    ed = Editor(entries, titles, n, sname, sid, group_ids, note)
    result = curses.wrapper(ed.run)
    if result is None:
        print(f"No changes applied. (Backup: {b.name})")
        return

    print("Quitting Arc…")
    if not quit_arc():
        sys.exit("Arc didn't quit within 30s. Nothing was changed.")
    fresh = Sidebar()
    space2 = fresh.space(sid)
    if space2 is None:
        sys.exit("The space disappeared while editing. Nothing was changed.")
    pre = backup("pre-apply")
    try:
        notes = apply_plan(fresh, space2, result)
    except RuntimeError as ex:
        subprocess.run(["open", "-a", "Arc"])
        sys.exit(f"{ex}\nNothing was changed; Arc reopened.")
    expected = structure(fresh, space2)
    write_sidebar(fresh.data)
    for line in notes:
        print("  " + line)
    print(f"Applied. Backup: {pre}")

    subprocess.run(["open", "-a", "Arc"])
    print("Reopened Arc.")
    if args.no_verify:
        return
    print(f"Watching {VERIFY_SECONDS}s for sync reverting the change…")
    time.sleep(VERIFY_SECONDS)
    after = Sidebar()
    sp = after.space(sid)
    if sp and structure(after, sp) == expected:
        print("✓ Changes still in place. Check Arc to confirm.")
    else:
        print("⚠ Arc's file no longer matches what was written. Sync may have reverted it,\n"
              "  or you changed tabs in the meantime. Check Arc. To undo: --restore")


if __name__ == "__main__":
    main()
