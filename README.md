# arc2vivaldi

Move from [Arc](https://arc.net) to [Vivaldi](https://vivaldi.com) on macOS
without losing your Spaces.

Vivaldi's built-in importer brings over Arc's bookmarks, history and
passwords, but **not your Spaces**. It only imports the tabs that were open
in Arc's last window. This script fills that gap and helps you move your
extensions.

- **Spaces → bookmarks:** copies every Arc Space, with its folders, pinned
  tabs and Today tabs, plus your Favorites, into one **Arc Spaces** bookmark
  folder in Vivaldi.
- **One-way sync, like `rsync --delete`:** run it as often as you like. Each
  run makes that folder match Arc exactly. New tabs are added, removed ones
  disappear, and nothing is ever doubled up.
- **Extensions:** finds which of Arc's extensions are missing or switched
  off in Vivaldi and takes you through each one. Ones already in Vivaldi are
  never added twice.
- **Guided:** run in a terminal, it takes you step by step through the whole
  move, showing what's happening and asking before it writes anything.

Requirements: macOS, Python 3 (the one that ships with the Xcode command line
tools is fine), Arc, Vivaldi. No other packages needed.

## Quick start

```bash
python3 arc2vivaldi.py
```

Run in a terminal, it walks you through six steps, showing what's happening
at each one and asking before anything is written:

1. **Check:** finds Arc and Vivaldi, asks which Vivaldi profile to use if you
   have several, and counts your Spaces and tabs.
2. **Preview:** a table of every Space with its link count and what would
   change (`+12`, `-3`, or `no change`). If everything already matches, it
   says so and moves on.
3. **Close Vivaldi:** only if something will be written. You can quit it
   yourself and the script waits, or let the script quit it for you.
4. **Copy Spaces:** backs up Vivaldi's bookmarks, writes the **Arc Spaces**
   folder, and ticks off each Space.
5. **Extensions:** shows which are already in Vivaldi (left alone), then goes
   through the rest one at a time. See [Extensions](#extensions).
6. **Done:** a summary of what changed.

Press `q` at any prompt to stop, or Ctrl-C at any point. Steps already
finished stay done and nothing is left half-written.

The result looks like this:

```
Arc Spaces/
  Favorites/          Arc's top row of favicons
  <Space name>/       pinned tabs, with Arc's folders kept
    Today/            that Space's unpinned tabs
  ...
```

To turn a Space back into something like an Arc Space, right-click its
folder in Vivaldi and open it in a new **Workspace**.

## Options

| Option | What it does |
|---|---|
| *(none)* | Guided mode when run in a terminal. When not run in a terminal (for example from cron or another script), it does a plain `--sync`. |
| `--sync` | Plain sync, no questions. Vivaldi must be closed first. |
| `--dry-run` | Shows how many links would be added or removed. Writes nothing. |
| `--no-today` | Copies only pinned tabs, not each Space's Today tabs. |
| `--keep-duplicates` | Keeps repeat links. By default, if the same URL appears more than once **within one Space**, only the first copy is kept. The same link in two different Spaces is always kept. |
| `--profile "Profile 1"` | Vivaldi profile folder to use. Default: `Default` (guided mode asks if there are several). |
| `--extensions` | Lists what's left to do for extensions, without the guided steps. |
| `--extensions --open` | The same list, then opens the pages you need. |

## Extensions

The script compares the extensions in all your Arc profiles with Vivaldi's.
**Anything already in Vivaldi is never added again.** It's matched by
extension ID, and also by folder and by name, so a self-made extension you
loaded from a different folder (which gives it a different ID) still counts
as moved.

In guided mode, each remaining extension gets its own screen:

- **Switched off in Vivaldi** (Vivaldi's importer often brings extensions in
  turned off): opens `vivaldi://extensions` so you can flip its switch.
- **From the Chrome Web Store:** opens its store page; click
  *Add to Vivaldi*.
- **One you made (unpacked):** opens `vivaldi://extensions` and puts the
  folder path on your clipboard. Turn on *Developer mode*, click
  *Load unpacked*, press ⌘⇧G in the picker and paste the path.

When you're done in Vivaldi, press Enter. The script checks Vivaldi's
settings, which can take up to 15 seconds to update, and ticks it off. Each
one can also be skipped for now, or marked **never ask again**. Those choices
are saved in
`~/Library/Application Support/arc2vivaldi/ignored-extensions.json`; delete
that file to undo them.

For a quick look without the steps:

```bash
python3 arc2vivaldi.py --extensions          # what's left to do
python3 arc2vivaldi.py --extensions --open   # and open the right pages
```

**Why it can't install them for you:** Vivaldi, like Chrome, protects its
extension list with a tamper check. Any outside tool that edits it gets its
changes thrown away or the profile flagged. The final click has to be yours.

**Settings don't move.** Each extension's own settings, data and sign-ins
(for example a password manager login) stay in Arc. Set them up again in
Vivaldi.

## Is it safe?

- **Arc is never changed.** The script only reads
  `~/Library/Application Support/Arc/StorableSidebar.json` and Arc's
  preference files.
- **Only its own folder changes.** The sync replaces just the **Arc Spaces**
  folder. Every other bookmark in Vivaldi stays as it is. The folder is
  found by a fixed internal ID, not by its name, so you can rename it or
  move it.
- **Never writes while Vivaldi is open.** Vivaldi saves its bookmarks when
  it quits and would overwrite the changes. Guided mode waits for it to
  close; `--sync` refuses to run.
- **Only writes when something changed.** If Arc Spaces already matches Arc,
  nothing is written and no backup is made.
- **Backs up first.** Before every write it copies Vivaldi's bookmarks file
  to `Bookmarks.arc2vivaldi-<date>-<time>.bak` next to the original, in
  `~/Library/Application Support/Vivaldi/<profile>/`.
- **No half-written files.** It writes to a temporary file and swaps it in
  only when it's complete.

To undo a sync, quit Vivaldi and copy a backup back over `Bookmarks`, or
just delete the **Arc Spaces** folder in Vivaldi.

## Limits and caveats

- **macOS only.** Arc for Windows keeps its data elsewhere, and the "is
  Vivaldi running?" check uses a macOS command.
- **Bookmarks, not Workspaces.** Vivaldi doesn't let outside tools create
  Workspaces safely, so Spaces become bookmark folders.
- **Depends on Arc's internal format.** `StorableSidebar.json` is
  undocumented and a future Arc version could change it. If that happens the
  script will most likely crash or skip items rather than damage anything,
  since Arc is only read. Run `--dry-run` first and check the counts.
- **Several Arc profiles are merged.** Spaces from every Arc profile go into
  the same **Arc Spaces** folder.
- **Vivaldi Sync:** if it's on, the **Arc Spaces** folder, with all of its
  links, copies to your other devices.
- **Your edits inside Arc Spaces are overwritten.** It's a one-way mirror of
  Arc. Keep anything you want to change in a different folder.
- **Skipped:** Arc's built-in "Getting Started" page, and anything in a
  Space that isn't a web page.
- **Tested on one Mac** (Arc with 17 Spaces and about 1,800 tabs, Vivaldi
  8.3, November 2026).

## License

[MIT](LICENSE)
