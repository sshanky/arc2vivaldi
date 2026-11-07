#!/usr/bin/env python3
"""Mirror Arc Spaces into Vivaldi bookmarks, one-way (rsync --delete style).

Source: ~/Library/Application Support/Arc/StorableSidebar.json (read only)
Target: one folder, "Arc Spaces", at the top of Vivaldi's bookmarks.

  Arc Spaces/
    Favorites/            Arc's top favicon row
    <Space>/              pinned tabs, with Arc's folders kept
      Today/              the space's unpinned ("Today") tabs

Each run rebuilds only that folder so it matches Arc exactly: new tabs get
added, closed/removed ones disappear, nothing is ever duplicated. Every
other bookmark in Vivaldi is left alone. The folder is found by a fixed
GUID, not by name, so renaming it in Vivaldi is fine.

Vivaldi must be quit first (it rewrites its bookmarks on exit). Arc can be
open. A timestamped backup of Vivaldi's Bookmarks file is made each run.

Usage:
  arc2vivaldi.py               guided mode: walks you through every step
                               (when run in a terminal)
  arc2vivaldi.py --sync        plain sync, no questions (also the default
                               when not run in a terminal, e.g. from cron)
  arc2vivaldi.py --dry-run     show what would change, write nothing
  arc2vivaldi.py --no-today    skip each space's Today tabs
  arc2vivaldi.py --keep-duplicates   keep repeat links (default: within each
                               space, only the first copy of a URL is kept)
  arc2vivaldi.py --profile "Profile 1"   Vivaldi profile dir (default: Default)
  arc2vivaldi.py --extensions          list what's left to move, extensions only
  arc2vivaldi.py --extensions --open   ...and open the pages you need

Vivaldi protects its extension list with a tamper check, so no outside tool
can install or switch on extensions for you; the script finds what's
missing and opens the right page, and you do the final click. Extensions
already in Vivaldi (matched by ID, folder, or name) are never added again.
Extension settings and sign-ins don't move: they stay in Arc.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

HOME = os.path.expanduser("~")
ARC_SIDEBAR = f"{HOME}/Library/Application Support/Arc/StorableSidebar.json"
VIVALDI_DIR = f"{HOME}/Library/Application Support/Vivaldi"

NS = uuid.UUID("6f1c2a4e-7d0b-4c51-9a3e-a2c2b1ba1d10")  # fixed: keeps GUIDs stable across runs
ROOT_GUID = str(uuid.uuid5(NS, "arc-spaces-root"))
ROOT_TITLE = "Arc Spaces"

# Arc stores Mac absolute time (seconds since 2001-01-01); Chromium wants
# microseconds since 1601-01-01.
MAC_TO_WIN_EPOCH = 978307200 + 11644473600


def guid_for(key):
    return str(uuid.uuid5(NS, key))


def chrome_time(arc_ts):
    if not isinstance(arc_ts, (int, float)):
        arc_ts = time.time() - 978307200
    return str(int((arc_ts + MAC_TO_WIN_EPOCH) * 1_000_000))


def pairs(lst):
    """Arc encodes maps as flat [key, value, key, value, ...] lists."""
    return [(lst[i], lst[i + 1]) for i in range(0, len(lst) - 1, 2)]


class Builder:
    def __init__(self, items):
        self.items = items
        self.skipped = {}
        self.tabs = 0
        self.folders = 0

    def folder(self, key, title, children, ts=None):
        self.folders += 1
        return {"type": "folder", "name": title, "guid": guid_for(key),
                "date_added": chrome_time(ts), "date_modified": "0",
                "children": children}

    def node(self, iid, path):
        """Convert one Arc sidebar item into a list of bookmark nodes."""
        n = self.items.get(iid)
        if not n:
            return []
        data = n.get("data") or {}
        kind = next(iter(data), None)
        key = f"{path}/{iid}"
        if kind == "tab":
            t = data["tab"]
            url = t.get("savedURL")
            if not url:
                return []
            self.tabs += 1
            title = n.get("title") or t.get("savedTitle") or url
            return [{"type": "url", "name": title, "url": url,
                     "guid": guid_for(key), "date_added": chrome_time(n.get("createdAt"))}]
        if kind == "list":  # an Arc folder
            kids = self.children(n, key)
            return [self.folder(key, n.get("title") or "Untitled folder", kids, n.get("createdAt"))]
        if kind in ("splitView", "itemContainer"):  # flatten in place
            return self.children(n, key)
        self.skipped[kind] = self.skipped.get(kind, 0) + 1
        return []

    def children(self, n, path):
        out = []
        for cid in n.get("childrenIds", []):
            out += self.node(cid, path)
        return out


def dedupe_folder(folder, seen):
    """Keep only the first copy of each URL (in sidebar order) within one
    space. Folders emptied by this are dropped; folders already empty in
    Arc are kept. Returns how many links were dropped."""
    dropped = 0
    kept = []
    for c in folder["children"]:
        if c["type"] == "url":
            if c["url"] in seen:
                dropped += 1
                continue
            seen.add(c["url"])
        else:
            was_empty = not c["children"]
            dropped += dedupe_folder(c, seen)
            if not c["children"] and not was_empty:
                continue
        kept.append(c)
    folder["children"] = kept
    return dropped


def build_tree(include_today, dedupe=True):
    with open(ARC_SIDEBAR) as f:
        sidebar = json.load(f)["sidebar"]

    spaces_out = []
    favorites = []
    b = None
    for c in sidebar["containers"]:
        if not (isinstance(c, dict) and "spaces" in c):
            continue
        items = {k: v for k, v in pairs(c["items"]) if isinstance(v, dict)}
        b = b or Builder(items)
        b.items = items

        for _, cid in pairs(c.get("topAppsContainerIDs", [])):
            if isinstance(cid, str):
                favorites += b.children(items.get(cid, {}), f"fav/{cid}")

        for s in c["spaces"]:
            if not isinstance(s, dict):
                continue
            pinned, today = [], []
            for kind, cid in pairs(s.get("newContainerIDs", [])):
                if not (isinstance(kind, dict) and isinstance(cid, str)):
                    continue
                kids = b.children(items.get(cid, {}), f"space/{s['id']}/{cid}")
                if "pinned" in kind:
                    pinned += kids
                elif "unpinned" in kind and include_today:
                    today += kids
            if s.get("title") is None and not pinned and not today:
                continue
            title = s.get("title") or "Untitled space"
            kids = pinned
            if today:
                kids = kids + [b.folder(f"space/{s['id']}/today", "Today", today)]
            spaces_out.append(b.folder(f"space/{s['id']}", title, kids))

    top = []
    if favorites:
        top.append(b.folder("favorites", "Favorites", favorites))
    top += spaces_out
    b.dupes = 0
    if dedupe:
        for f in top:
            b.dupes += dedupe_folder(f, set())
    root = b.folder("root", ROOT_TITLE, top)
    root["guid"] = ROOT_GUID
    return root, b


def assign_ids(node, next_id):
    node["id"] = str(next_id[0])
    next_id[0] += 1
    for c in node.get("children", []):
        assign_ids(c, next_id)


def walk(node):
    yield node
    for c in node.get("children", []):
        yield from walk(c)


def max_id(roots):
    m = 0
    for r in roots.values():
        if isinstance(r, dict):
            for n in walk(r):
                try:
                    m = max(m, int(n.get("id", 0)))
                except ValueError:
                    pass
    return m


def url_set(node):
    return {(n["url"]) for n in walk(node) if n.get("type") == "url"} if node else set()


# ---------------------------------------------------------------- bookmarks

def vivaldi_running():
    return subprocess.run(["pgrep", "-x", "Vivaldi"], capture_output=True).returncode == 0


class SyncPlan:
    """Everything a sync would do, worked out without writing anything."""

    def __init__(self, bm_path, include_today=True, dedupe=True):
        self.bm_path = bm_path
        self.root, self.builder = build_tree(include_today=include_today, dedupe=dedupe)
        with open(bm_path) as f:
            self.bm = json.load(f)
        bar = self.bm["roots"]["bookmark_bar"]["children"]
        self.old_idx = next((i for i, c in enumerate(bar) if c.get("guid") == ROOT_GUID), None)
        self.old = bar[self.old_idx] if self.old_idx is not None else None

        others = self.bm["roots"]
        if self.old is not None:
            others = json.loads(json.dumps(others))
            del others["bookmark_bar"]["children"][self.old_idx]
        assign_ids(self.root, [max_id(others) + 1])
        # Keep the original "date added" for anything already mirrored, so a
        # re-run with no changes in Arc produces an identical file.
        if self.old is not None:
            prev = {n["guid"]: n.get("date_added") for n in walk(self.old) if "guid" in n}
            for n in walk(self.root):
                if prev.get(n["guid"]):
                    n["date_added"] = prev[n["guid"]]

        self.before, self.after = url_set(self.old), url_set(self.root)
        self.added = self.after - self.before
        self.removed = self.before - self.after

    @property
    def unchanged(self):
        return self.old is not None and json.dumps(self.old, sort_keys=True) == \
            json.dumps(self.root, sort_keys=True)

    def per_space(self):
        """[(name, links now, +added, -removed)] for each top-level folder."""
        old_by_guid = {c["guid"]: c for c in (self.old or {}).get("children", [])}
        rows = []
        for f in self.root["children"]:
            now, was = url_set(f), url_set(old_by_guid.get(f["guid"]))
            links = sum(1 for n in walk(f) if n.get("type") == "url")
            rows.append((f["name"], links, len(now - was), len(was - now)))
        return rows

    def apply(self):
        bar = self.bm["roots"]["bookmark_bar"]["children"]
        if self.old_idx is not None:
            bar[self.old_idx] = self.root
        else:
            bar.insert(0, self.root)
        self.bm.pop("checksum", None)  # Chromium accepts a missing checksum and rewrites it
        backup = f"{self.bm_path}.arc2vivaldi-{time.strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copy2(self.bm_path, backup)
        tmp = self.bm_path + ".arc2vivaldi.tmp"
        with open(tmp, "w") as f:
            json.dump(self.bm, f, indent=3, ensure_ascii=False)
        os.replace(tmp, self.bm_path)
        return backup


# --------------------------------------------------------------- extensions

ARC_PROFILES = f"{HOME}/Library/Application Support/Arc/User Data"
STATE_DIR = f"{HOME}/Library/Application Support/arc2vivaldi"
IGNORED_FILE = f"{STATE_DIR}/ignored-extensions.json"
STORE_LOCATIONS = {1, 6}  # Web Store install, external download
UNPACKED = 4
SKIP_LOCATIONS = {5, 10}  # built-in component extensions


def _ext_name(raw, base):
    """Resolve '__MSG_appName__' style names from the extension's locales."""
    if not raw or not raw.startswith("__MSG_") or not base:
        return raw or "?"
    key = raw[6:-2].lower()
    for loc in ("en", "en_US", "en_GB"):
        try:
            with open(os.path.join(base, "_locales", loc, "messages.json")) as f:
                msgs = {k.lower(): v for k, v in json.load(f).items()}
            if key in msgs:
                return msgs[key]["message"]
        except (OSError, ValueError, KeyError):
            pass
    return raw


def read_extensions(profile_dir):
    """Return {id: {...}} for user-installed extensions in one profile."""
    out = {}
    for fn in ("Preferences", "Secure Preferences"):
        try:
            with open(os.path.join(profile_dir, fn)) as f:
                settings = json.load(f).get("extensions", {}).get("settings", {})
        except (OSError, ValueError):
            continue
        for eid, v in settings.items():
            loc = v.get("location")
            if loc in SKIP_LOCATIONS or loc is None:
                continue
            path = v.get("path", "")
            if loc == UNPACKED:
                base = path
                if not os.path.isdir(base):
                    continue
            else:
                vdir = os.path.join(profile_dir, "Extensions", eid)
                if not os.path.isdir(vdir):
                    continue  # stale entry, not actually installed
                versions = sorted(os.listdir(vdir))
                base = os.path.join(vdir, versions[-1]) if versions else None
            manifest = v.get("manifest") or {}
            if not manifest and base:
                try:
                    with open(os.path.join(base, "manifest.json")) as f:
                        manifest = json.load(f)
                except (OSError, ValueError):
                    pass
            out[eid] = {
                "id": eid,
                "name": _ext_name(manifest.get("name"), base),
                "kind": "unpacked" if loc == UNPACKED else
                        "store" if loc in STORE_LOCATIONS else "other",
                "path": path if loc == UNPACKED else "",
                "enabled": not v.get("disable_reasons") and v.get("state", 1) != 0,
            }
    return out


def arc_extensions():
    arc = {}
    if os.path.isdir(ARC_PROFILES):
        for prof in sorted(os.listdir(ARC_PROFILES)):
            pdir = os.path.join(ARC_PROFILES, prof)
            if os.path.isfile(os.path.join(pdir, "Preferences")):
                for eid, e in read_extensions(pdir).items():
                    arc.setdefault(eid, e)
    return arc


def load_ignored():
    try:
        with open(IGNORED_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_ignored(ids):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(IGNORED_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=1)


def find_in_vivaldi(e, viv):
    """The Vivaldi copy of Arc extension e, if it's already been moved.

    Matched by extension ID first. A self-made extension loaded from another
    folder gets a different ID, so those are also matched by folder and by
    name: that way nothing that's already in Vivaldi is ever added twice."""
    if e["id"] in viv:
        return viv[e["id"]]
    for v in viv.values():
        if e["path"] and v["path"] and os.path.realpath(e["path"]) == os.path.realpath(v["path"]):
            return v
    for v in viv.values():
        if v["name"].strip().lower() == e["name"].strip().lower():
            return v
    return None


def extension_status(vivaldi_profile):
    """Sort Arc's extensions by what's left to do in Vivaldi.

    Returns {"done", "off", "store", "unpacked", "other", "ignored"}: lists
    of (arc_ext, vivaldi_ext_or_None)."""
    arc = arc_extensions()
    viv = read_extensions(os.path.join(VIVALDI_DIR, vivaldi_profile))
    ignored = load_ignored()
    out = {k: [] for k in ("done", "off", "store", "unpacked", "other", "ignored")}
    for e in sorted(arc.values(), key=lambda x: x["name"].lower()):
        v = find_in_vivaldi(e, viv)
        if v and v["enabled"]:
            out["done"].append((e, v))
        elif v:
            out["off"].append((e, v))
        elif e["id"] in ignored:
            out["ignored"].append((e, None))
        else:
            out[e["kind"]].append((e, None))
    return out


def store_url(eid):
    return f"https://chromewebstore.google.com/detail/{eid}"


def open_in_vivaldi(target):
    subprocess.run(["open", "-a", "Vivaldi", target], check=False)


def print_extension_report(vivaldi_profile, do_open):
    st = extension_status(vivaldi_profile)
    total = sum(len(v) for v in st.values())
    print(f"Arc has {total} extensions.\n")
    sections = [
        ("done", "Already in Vivaldi and on (won't be touched):", "✓"),
        ("off", "In Vivaldi but switched off: turn on at vivaldi://extensions", "○"),
        ("store", "Missing, from the Web Store: open the page, click 'Add to Vivaldi'", "+"),
        ("unpacked", "Missing, ones you made: vivaldi://extensions → Developer mode → "
                     "Load unpacked → pick the folder", "+"),
        ("other", "Installed some other way in Arc; add by hand if you need them:", "?"),
        ("ignored", "Ignored (you chose 'never' in the guided mode):", "-"),
    ]
    for key, title, mark in sections:
        if st[key]:
            print(title)
            for e, _ in st[key]:
                extra = store_url(e["id"]) if key == "store" else e["path"] if key == "unpacked" else ""
                print(f"  {mark} {e['name']}" + (f"\n    {extra}" if extra else ""))
            print()
    todo = st["off"] or st["store"] or st["unpacked"] or st["other"]
    if not todo:
        print("Nothing left to do: every extension from Arc is already in Vivaldi.")
        return
    print("Settings and sign-ins inside extensions don't carry over; set them up again in Vivaldi.")
    if do_open:
        for e, _ in st["store"]:
            open_in_vivaldi(store_url(e["id"]))
        if st["off"] or st["unpacked"]:
            open_in_vivaldi("vivaldi://extensions")
        for e, _ in st["unpacked"]:
            subprocess.run(["open", e["path"]], check=False)
    else:
        print("Re-run with --open to open these pages for you, or run with no options for the guided mode.")


# ------------------------------------------------------------- guided mode

class UI:
    """Small terminal helpers: colour when it's a real terminal, plain otherwise."""

    def __init__(self):
        self.tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def c(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.tty else s

    def bold(self, s): return self.c("1", s)
    def dim(self, s): return self.c("2", s)
    def green(self, s): return self.c("32", s)
    def yellow(self, s): return self.c("33", s)
    def red(self, s): return self.c("31", s)
    def cyan(self, s): return self.c("36", s)

    def screen(self, step, total, title, steps):
        if self.tty:
            print("\033[2J\033[H", end="")
        else:
            print("\n" + "=" * 64)
        print(self.bold("Arc → Vivaldi") + self.dim("  ·  moving your Spaces and extensions"))
        line = []
        for i, name in enumerate(steps, 1):
            if i < step:
                line.append(self.green(f"✓ {name}"))
            elif i == step:
                line.append(self.cyan(self.bold(f"▶ {name}")))
            else:
                line.append(self.dim(f"○ {name}"))
        print("  ".join(line))
        print(self.dim("─" * 64))
        print(self.bold(f"Step {step} of {total}: {title}") + "\n")

    def ok(self, s): print(f"  {self.green('✓')} {s}")
    def warn(self, s): print(f"  {self.yellow('!')} {s}")
    def bad(self, s): print(f"  {self.red('✗')} {s}")
    def info(self, s): print(f"    {s}")

    def ask(self, prompt, choices):
        """choices: {"key": "label"}; Enter picks the first one."""
        keys = list(choices)
        hint = "  ".join(
            (self.bold("[Enter]") if i == 0 else self.bold(f"[{k}]")) + f" {lbl}"
            for i, (k, lbl) in enumerate(choices.items()))
        while True:
            print(f"\n{prompt}\n  {hint}" if prompt else f"\n  {hint}")
            try:
                ans = input("  > ").strip().lower()
            except EOFError:
                ans = "q" if "q" in keys else keys[0]
            if ans == "":
                return keys[0]
            if ans in keys:
                return ans
            print(self.dim(f"  Please type one of: {', '.join(keys)}"))

    def wait(self, label, done, timeout):
        """Spin until done() is true or timeout seconds pass. Returns done()."""
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        start, i = time.time(), 0
        while time.time() - start < timeout:
            if done():
                if self.tty:
                    print("\r\033[K", end="")
                return True
            if self.tty:
                print(f"\r  {self.cyan(frames[i % len(frames)])} {label}", end="", flush=True)
            i += 1
            time.sleep(0.25)
        if self.tty:
            print("\r\033[K", end="")
        return done()


def vivaldi_profiles():
    """[(dir, display name)] of Vivaldi profiles that have bookmarks."""
    names = {}
    try:
        with open(os.path.join(VIVALDI_DIR, "Local State")) as f:
            for d, info in json.load(f).get("profile", {}).get("info_cache", {}).items():
                names[d] = info.get("name") or d
    except (OSError, ValueError):
        pass
    out = []
    if os.path.isdir(VIVALDI_DIR):
        for d in sorted(os.listdir(VIVALDI_DIR)):
            if os.path.isfile(os.path.join(VIVALDI_DIR, d, "Bookmarks")):
                out.append((d, names.get(d, d)))
    return out


def guided(a):
    ui = UI()
    steps = ["Check", "Preview", "Close Vivaldi", "Copy Spaces", "Extensions", "Done"]
    T = len(steps)
    summary = []
    test_file = a.bookmarks_file  # testing hook: write to a copy, skip quitting Vivaldi

    # 1. Check ---------------------------------------------------------------
    ui.screen(1, T, "Checking what's on this Mac", steps)
    problems = False
    if sys.platform != "darwin":
        ui.bad("This only works on macOS.")
        return 1
    if os.path.isfile(ARC_SIDEBAR):
        ui.ok("Found Arc's data (it is only read, never changed)")
    else:
        ui.bad(f"Arc's data isn't where expected:\n      {ARC_SIDEBAR}")
        problems = True
    profiles = vivaldi_profiles()
    if profiles or test_file:
        ui.ok("Found Vivaldi")
    else:
        ui.bad("Vivaldi has no profile with bookmarks yet. Open Vivaldi once, then run this again.")
        problems = True
    if problems:
        return 1

    profile = a.profile
    if not test_file and len(profiles) > 1 and a.profile == "Default":
        print("\n  Vivaldi has more than one profile:")
        for i, (d, n) in enumerate(profiles, 1):
            print(f"    {i}. {n} {ui.dim('(' + d + ')')}")
        choice = ui.ask("Which one should get your Arc Spaces?",
                        {str(i): n for i, (d, n) in enumerate(profiles, 1)} | {"q": "quit"})
        if choice == "q":
            return 0
        profile = profiles[int(choice) - 1][0]
    bm_path = test_file or os.path.join(VIVALDI_DIR, profile, "Bookmarks")
    ui.ok(f"Using Vivaldi profile: {profile}")

    try:
        plan = SyncPlan(bm_path, include_today=not a.no_today, dedupe=not a.keep_duplicates)
    except Exception as ex:  # Arc's format is undocumented; fail clearly
        ui.bad(f"Couldn't read the bookmarks or Arc's Spaces: {ex}")
        return 1
    spaces = plan.per_space()
    ui.ok(f"Arc has {len(spaces)} Spaces (counting Favorites) with "
          f"{plan.builder.tabs:,} tabs")
    if ui.ask("Ready to look at what would change? Nothing is written yet.",
              {"": "continue", "q": "quit"}) == "q":
        return 0

    # 2. Preview -------------------------------------------------------------
    ui.screen(2, T, "What the sync would do", steps)
    print(f"  One folder, {ui.bold(ROOT_TITLE)}, in Vivaldi's bookmarks gets made to match Arc.")
    print("  Nothing else in your bookmarks is touched.\n")
    w = max(len(n) for n, *_ in spaces)
    print(ui.dim(f"    {'Space'.ljust(w)}   links   change"))
    for name, links, plus, minus in spaces:
        ch = []
        if plus:
            ch.append(ui.green(f"+{plus}"))
        if minus:
            ch.append(ui.red(f"-{minus}"))
        print(f"    {name.ljust(w)}  {links:6,}   {' '.join(ch) or ui.dim('no change')}")
    print()
    if plan.builder.dupes:
        ui.info(ui.dim(f"{plan.builder.dupes:,} repeat links skipped (same URL twice in one Space)."))
    do_sync, up_to_date = True, False
    if plan.unchanged:
        ui.ok(f"{ROOT_TITLE} already matches Arc. Nothing to copy.")
        do_sync, up_to_date = False, True
        summary.append(("Spaces", "already up to date"))
        ans = ui.ask("Move on to extensions?", {"": "continue", "q": "quit"})
    else:
        verb = "Update" if plan.old else "Create"
        print(f"  {verb} {ui.bold(ROOT_TITLE)}: "
              f"{ui.green(f'+{len(plan.added):,}')} new links, "
              f"{ui.red(f'-{len(plan.removed):,}')} removed links.")
        ans = ui.ask("Copy your Spaces into Vivaldi?",
                     {"": "yes, continue", "s": "skip to extensions", "q": "quit"})
        if ans == "s":
            do_sync = False
            summary.append(("Spaces", "skipped"))
    if ans == "q":
        return 0

    # 3. Close Vivaldi -------------------------------------------------------
    ui.screen(3, T, "Closing Vivaldi", steps)
    if not do_sync:
        ui.ok("Not needed: no bookmarks are being written.")
        time.sleep(0.8)
    elif test_file or not vivaldi_running():
        ui.ok("Vivaldi is closed.")
        time.sleep(0.5)
    else:
        print("  Vivaldi is open. It keeps bookmarks in memory and saves them when it")
        print("  quits, which would overwrite the copy. It needs to be closed first.")
        print(ui.dim("  Your tabs come back when you reopen it."))
        while vivaldi_running():
            ans = ui.ask("How do you want to close it?",
                         {"": "I'll quit it myself (⌘Q), wait for me",
                          "c": "close it for me", "q": "stop here"})
            if ans == "q":
                return 0
            if ans == "c":
                subprocess.run(["osascript", "-e", 'tell application "Vivaldi" to quit'],
                               capture_output=True)
            if ui.wait("Waiting for Vivaldi to close…", lambda: not vivaldi_running(), 60):
                ui.ok("Vivaldi is closed.")
            else:
                ui.warn("Vivaldi is still open (it may be asking something, like "
                        "'close all tabs?').")

    # 4. Copy Spaces ---------------------------------------------------------
    ui.screen(4, T, "Copying your Spaces", steps)
    if do_sync:
        # Vivaldi saved its bookmarks as it quit: work from that saved copy.
        plan = SyncPlan(bm_path, include_today=not a.no_today, dedupe=not a.keep_duplicates)
        if plan.unchanged:
            ui.ok(f"{ROOT_TITLE} already matches Arc. Nothing to copy.")
            summary.append(("Spaces", "already up to date"))
        else:
            ui.ok("Read Arc's Spaces")
            backup = plan.apply()
            ui.ok("Backed up Vivaldi's bookmarks to")
            ui.info(ui.dim(backup))
            for name, links, plus, minus in plan.per_space():
                ui.ok(f"{name} {ui.dim(f'({links:,} links)')}")
                if ui.tty:
                    time.sleep(0.03)
            ui.ok(f"Saved. {ROOT_TITLE}: +{len(plan.added):,} new, -{len(plan.removed):,} removed")
            summary.append(("Spaces", f"+{len(plan.added):,} new, -{len(plan.removed):,} removed "
                                      f"links; backup saved"))
    elif up_to_date:
        ui.ok(f"Nothing to copy: {ROOT_TITLE} already matches Arc.")
    else:
        ui.ok("Skipped, as you chose.")
    if ui.ask("Next: extensions.", {"": "continue", "q": "quit"}) == "q":
        return 0

    # 5. Extensions ----------------------------------------------------------
    ui.screen(5, T, "Extensions", steps)
    st = extension_status(profile)
    for e, _ in st["done"]:
        ui.ok(f"{e['name']} {ui.dim('already in Vivaldi, left alone')}")
    for e, _ in st["ignored"]:
        print(f"  {ui.dim('- ' + e['name'] + ' (ignored)')}")
    todo = [("off", e) for e, _ in st["off"]] + [("store", e) for e, _ in st["store"]] + \
           [("unpacked", e) for e, _ in st["unpacked"]] + [("other", e) for e, _ in st["other"]]
    moved, skipped = 0, 0
    if not todo:
        print()
        ui.ok("Every extension from Arc is already in Vivaldi. Nothing to move.")
        summary.append(("Extensions", "all already in Vivaldi"))
    else:
        print(f"\n  {len(todo)} to go. Vivaldi doesn't let scripts install extensions,")
        print("  so for each one this opens the right page and you do the final click.")
        print(ui.dim("  Settings and sign-ins inside extensions stay in Arc; set them up again."))
        if not vivaldi_running() and not test_file:
            open_in_vivaldi("vivaldi://extensions")
            ui.wait("Opening Vivaldi…", vivaldi_running, 20)
        for n, (kind, e) in enumerate(todo, 1):
            print("\n" + ui.dim("─" * 64))
            print(ui.bold(f"  {n}/{len(todo)}  {e['name']}"))

            def present():
                v = find_in_vivaldi(e, read_extensions(os.path.join(VIVALDI_DIR, profile)))
                return v is not None and (v["enabled"] or kind == "other")

            if present():  # finished while we were on an earlier one
                ui.ok("Already done.")
                moved += 1
                continue
            if kind == "off":
                print("  It's in Vivaldi but switched off. Flip its switch on.")
                target = "vivaldi://extensions"
            elif kind == "store":
                print("  From the Chrome Web Store. Click 'Add to Vivaldi', then 'Add extension'.")
                target = store_url(e["id"])
            elif kind == "unpacked":
                print("  One you made. On the extensions page: turn on Developer mode (top")
                print("  right), click 'Load unpacked', then pick this folder:")
                print("    " + ui.cyan(e["path"]))
                print(ui.dim("  The path is on your clipboard: in the picker press ⌘⇧G, then ⌘V."))
                target = "vivaldi://extensions"
            else:
                print("  Arc got this one some other way (not the Web Store or a folder),")
                print("  so there's nothing to open. Find it again by name if you need it.")
                target = None
            choices = {"": "open it"} if target else {}
            choices |= {"s": "skip for now", "n": "never ask about this one", "q": "quit"}
            ans = ui.ask("", choices)
            if ans == "q":
                break
            if ans in ("s", "n"):
                skipped += 1
                if ans == "n":
                    save_ignored(load_ignored() | {e["id"]})
                    ui.info(ui.dim("Won't ask again. (Undo by deleting " + IGNORED_FILE + ")"))
                continue
            if kind == "unpacked":
                subprocess.run(["pbcopy"], input=e["path"].encode(), check=False)
            if target:
                open_in_vivaldi(target)
            while True:
                ans = ui.ask("Do it in Vivaldi, then come back here.",
                             {"": "done, check it", "s": "skip this one"})
                if ans == "s":
                    skipped += 1
                    break
                # Vivaldi saves extension changes a few seconds after they happen.
                if ui.wait("Checking Vivaldi… (it can take up to 15 seconds to save)",
                           present, 20):
                    ui.ok(f"{e['name']} is in Vivaldi and on.")
                    moved += 1
                    break
                ui.warn("Not seen yet. Check it's added and switched on, then try again.")
        summary.append(("Extensions", f"{moved} moved, {skipped} skipped, "
                                      f"{len(st['done'])} were already there"))

    # 6. Done ----------------------------------------------------------------
    ui.screen(6, T, "Done", steps)
    for what, result in summary:
        ui.ok(f"{ui.bold(what)}: {result}")
    print()
    ui.info("Run this again any time. It only copies what changed in Arc, and")
    ui.info("never adds an extension that's already in Vivaldi.")
    if do_sync and not test_file:
        if ui.ask("Open Vivaldi now?", {"": "yes", "n": "no"}) == "":
            subprocess.run(["open", "-a", "Vivaldi"], check=False)
    return 0


# --------------------------------------------------------------------- main

def plain_sync(a):
    bm_path = a.bookmarks_file or f"{VIVALDI_DIR}/{a.profile}/Bookmarks"
    if not a.dry_run and not a.bookmarks_file and vivaldi_running():
        sys.exit("Vivaldi is running. Quit it (Cmd+Q) and run this again.")
    plan = SyncPlan(bm_path, include_today=not a.no_today, dedupe=not a.keep_duplicates)
    b = plan.builder
    print(f"Arc: {b.tabs} tabs, {b.folders - 1} folders/spaces")
    if b.dupes:
        print(f"Dropped {b.dupes} repeat links (same URL twice in one space)")
    if b.skipped:
        print(f"Skipped non-tab items: {b.skipped}")
    print(f"'{ROOT_TITLE}' folder: {'updating' if plan.old else 'creating'} | "
          f"+{len(plan.added)} new URLs, -{len(plan.removed)} removed URLs")
    if a.dry_run:
        print("Dry run: nothing written.")
        return
    if plan.unchanged:
        print("Already up to date: nothing written.")
        return
    backup = plan.apply()
    print(f"Wrote {bm_path}\nBackup: {backup}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-today", action="store_true")
    ap.add_argument("--keep-duplicates", action="store_true")
    ap.add_argument("--extensions", action="store_true")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--profile", default="Default")
    ap.add_argument("--guided", action="store_true", help=argparse.SUPPRESS)  # force guided mode
    ap.add_argument("--bookmarks-file", help=argparse.SUPPRESS)  # for testing
    a = ap.parse_args()

    if a.extensions:
        print_extension_report(a.profile, a.open)
        return
    if a.guided or (not (a.sync or a.dry_run) and sys.stdin.isatty() and sys.stdout.isatty()):
        try:
            sys.exit(guided(a))
        except KeyboardInterrupt:
            print("\n\nStopped. Anything already finished stays done; nothing half-written.")
            sys.exit(130)
    plain_sync(a)


if __name__ == "__main__":
    main()
