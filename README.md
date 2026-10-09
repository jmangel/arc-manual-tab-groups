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
python3 arc_tabs.py --verify-seconds 60   # watch the file longer after applying
python3 arc_tabs.py --no-restart    # experimental: apply without quitting Arc
```

On the first run, macOS can ask for permission for your terminal to control Arc.

### Editor keys

| Key | Action |
| --- | --- |
| `↑` `↓` / `j` `k` | Move the cursor |
| `J` `K` | Move the tab or group up or down (see below) |
| `space` | Mark a tab |
| `m` | Move the marked tabs (or the tab at the cursor) to a group |
| `n` | New group |
| `r` | Rename a group |
| `x` | Ungroup the tab (or the marked tabs); on a group header, ungroup the whole group |
| `u` | Undo |
| `e` | Edit the plan in `$VISUAL` / `$EDITOR` |
| `c` / `p` | Copy the plan to the clipboard / paste a plan from the clipboard |
| `a` | Apply |
| `q` | Quit |

### Ungrouped tabs stay on top

In Arc, ungrouped Today tabs must come before all groups. Arc would put an ungrouped tab that comes after a group into that group. Therefore the editor only allows ungrouped tabs at the top:

- `J`/`K` on a grouped tab moves it within its group, then into the next or previous group.
- Moving a tab up past the top of the first group makes it the last ungrouped tab.
- Moving the last ungrouped tab down puts it at the top of the first group.
- Groups move only among other groups.
- `x` on a tab moves that tab (or the marked tabs) to the end of the ungrouped section. `x` on a group header does the same with all of the group's tabs.
- If a pasted plan puts an ungrouped tab below a group, the script moves the tab up to the ungrouped section (so Arc does not add it to the group) and tells you.

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
- **Sync.** Arc sync can revert the changes. After Arc reopens, the script watches the file for 20 seconds (`--verify-seconds` changes this) and shows any differences from what it wrote.

This project is not affiliated with The Browser Company.

### Testing live edits (`--no-restart`)

The script normally quits Arc before it writes, because Arc keeps the sidebar in memory and seems to save it over the file. `--no-restart` writes while Arc is running so you can test that:

1. Run `python3 arc_tabs.py --no-restart`, make a small, easy-to-see change (for example, rename a group), and apply it.
2. Watch the sidebar. Does the change appear without a restart?
3. The script watches the file. If Arc saves over it, the script shows what changed and how long it took.
4. If the file was left alone, quit and reopen Arc anyway, and check that the change is still there. Arc may save its own copy when it quits.

If the change is lost, run the script again without `--no-restart`.

## Support

If this tool saves you time, you can [buy me a coffee on Venmo](https://venmo.com/u/JohnMangel).

## License

[MIT](LICENSE)
