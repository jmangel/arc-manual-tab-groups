# arc-manual-tab-groups

Reorganize the **Today** (unpinned) tabs of the [Arc browser](https://arc.net) into groups, from the terminal.

`arc_tabs.py` reads Arc's sidebar file and shows your Today tabs in a keyboard-driven editor. You can move tabs, make and rename groups, or paste a full plan (for example, one an AI assistant wrote). When you apply, the script quits Arc, writes the changes, and opens Arc again.

Pinned tabs, pinned folders, and Favorites are not changed.

## Requirements

- macOS (the script uses `pbcopy`, `pbpaste`, `osascript`, and `open`)
- Python 3.8 or later (standard library only)
- Arc

## Usage

```sh
python3 arc_tabs.py                 # pick a space, edit, apply
python3 arc_tabs.py --space Work    # go directly to a space
python3 arc_tabs.py --restore       # roll back to a backup
python3 arc_tabs.py --no-verify     # skip the check after Arc reopens
```

On the first run, macOS can ask for permission for your terminal to control Arc.

### Editor keys

| Key | Action |
| --- | --- |
| `↑` `↓` / `j` `k` | Move the cursor |
| `J` `K` | Move the tab or group up or down |
| `space` | Mark a tab |
| `m` | Move the marked tabs (or the tab at the cursor) to a group |
| `n` | New group |
| `r` | Rename a group |
| `x` | Ungroup |
| `u` | Undo |
| `e` | Edit the plan in `$VISUAL` / `$EDITOR` |
| `c` / `p` | Copy the plan to the clipboard / paste a plan from the clipboard |
| `a` | Apply |
| `q` | Quit |

### Plan format

At startup, the script copies a JSON plan to the clipboard. Each tab has a short ID and its title:

```json
{
 "space": "Work",
 "spaceId": "…",
 "today": [
  ["1a2b3c", "Loose tab title"],
  {"group": "Reading", "id": "4d5e6f", "tabs": [
   ["7a8b9c", "Some article"]
  ]}
 ]
}
```

Change the order or the groups, then paste the plan back with `p`. A group without an `id` becomes a new group. The script puts tabs that the plan does not mention at the top.

## Read this before you use it

- **Your tab titles go to the clipboard.** Tab titles can contain private data, for example email subjects, document names, or account details. Check the plan before you paste it into another app or an AI assistant.
- **Arc quits when you apply.** Uploads stop, and form text that you did not save is lost.
- **The script changes a private Arc file.** `StorableSidebar.json` does not have a public format. An Arc update can break this script.
- **Backups.** Before each change, the script writes a backup to `~/Library/Application Support/Arc/sidebar-backups/` and keeps the last 20. Use `--restore` to go back.
- **Sync.** Arc sync can revert the changes. After Arc reopens, the script watches the file for 20 seconds and tells you if it changed.

This project is not affiliated with The Browser Company.

## License

[MIT](LICENSE)
