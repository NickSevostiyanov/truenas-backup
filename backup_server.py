#!/usr/bin/env python3
r"""
backup_server.py - full lossless compressed backup of a TrueNAS share.

Streams everything under SOURCE into a single .tar.zst archive (tar +
Zstandard) written straight to the backup folder. Zstandard is used because it
is the best modern speed/ratio trade-off: multithreaded across all CPU cores,
with long-distance matching enabled for better ratios on large files.

Usage:
    python backup_server.py                          # interactive menu
    python backup_server.py --menu                   # force the menu
    python backup_server.py --run                    # skip the menu, back up now
    python backup_server.py --source Z:\ --dest D:\Backups
    python backup_server.py --choose-dest            # select destination in a dialog
    python backup_server.py --include movies         # include a default-excluded folder
    python backup_server.py --exclude another-folder
    python backup_server.py --dry-run                # measure only, write nothing
    python backup_server.py --preset smallest         # max ratio (much slower)
    python backup_server.py --level 19 --window-log 31
    python backup_server.py --keep 6                 # prune to 6 newest archives

Restore (pick one):
    tar --use-compress-program=zstd -xf truenas-full-YYYY-MM-DD.tar.zst -C <dir>
    7-Zip 24.09+ opens .tar.zst directly
    python backup_server.py --restore <archive> <dir>

The archive is written as <name>.partial and renamed only on success, so a
crashed/interrupted run never leaves a file that looks like a good backup.
Unreadable files are skipped and listed in a .log file next to the archive.
"""

import argparse
import fnmatch
import json
import os
import shutil
import stat
import sys
import tarfile
import time
from datetime import date

import zstandard as zstd
from tqdm import tqdm

DEFAULT_SOURCE = ""  # your mounted share, e.g. Z:\ or /mnt/truenas
DEFAULT_DEST = os.path.join(os.path.expanduser("~"), "TrueNAS Backups")
ARCHIVE_PREFIX = "truenas-full-"
MAX_WINDOW_LOG = 31

# Folders/files skipped by default, relative to the source root. Glob patterns
# allowed. Edit here, use the menu, or pass --exclude (all are combined).
DEFAULT_EXCLUDE = [
    # "movies",
    # "*.tmp",
]

# (label, zstd level, window_log, blurb). window_log 27 = 128 MiB match window,
# 30 = 1 GiB, 31 = 2 GiB. A bigger window finds duplicate data further apart
# across the whole archive, which is where the real size wins come from - but it
# costs roughly that much RAM per compression thread.
PRESETS = [
    ("Fastest", 3, 27, "quickest run, largest archive"),
    ("Balanced", 12, 27, "good ratio, still disk/network bound - recommended"),
    ("Smaller", 16, 30, "~5-8% smaller, ~2x CPU time, ~1 GiB/thread RAM *"),
    ("Smallest", 19, 31, "max ratio, several times slower, very RAM hungry *"),
]

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "backup_server_config.json"
)

DEFAULT_SETTINGS = {
    "source": DEFAULT_SOURCE,
    "dest": DEFAULT_DEST,
    "exclude": list(DEFAULT_EXCLUDE),
    "level": 12,
    "window_log": 27,
    "threads": -1,
    "keep": 0,
    "dry_run": False,
    "force": False,
}


def longpath(p):
    """Prefix a path so Windows accepts names longer than 260 chars."""
    p = os.path.abspath(p)
    if os.name != "nt" or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def norm(rel):
    return rel.replace("\\", "/").casefold()


def is_excluded(rel, patterns):
    r = norm(rel)
    for p in patterns:
        p = norm(p).rstrip("/")
        if r == p or r.startswith(p + "/") or fnmatch.fnmatch(r, p):
            return True
    return False


def walk(root, patterns):
    """Yield (fullpath, relpath, is_dir), pruning excluded directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        reldir = os.path.relpath(dirpath, root)
        kept = []
        for d in sorted(dirnames):
            rel = d if reldir == "." else os.path.join(reldir, d)
            if is_excluded(rel, patterns):
                continue
            kept.append(d)
            yield os.path.join(dirpath, d), rel, True
        dirnames[:] = kept
        for f in sorted(filenames):
            rel = f if reldir == "." else os.path.join(reldir, f)
            if not is_excluded(rel, patterns):
                yield os.path.join(dirpath, f), rel, False


def make_compressor(level, threads, window_log=27):
    """Zstd is lossless at every level: only speed, size and RAM change."""
    if threads is None or threads < 0:
        threads = os.cpu_count() or 1
    try:
        params = zstd.ZstdCompressionParameters.from_level(
            level, threads=threads, window_log=window_log, enable_ldm=True
        )
        return zstd.ZstdCompressor(compression_params=params)
    except Exception:
        return zstd.ZstdCompressor(level=level, threads=threads)


class CountingWriter:
    """Pass-through file wrapper that tallies the compressed bytes written."""

    def __init__(self, f):
        self.f, self.written = f, 0

    def write(self, data):
        self.written += len(data)
        return self.f.write(data)

    def flush(self):
        self.f.flush()


class ExactReader:
    """Yield exactly `size` bytes from a file (zero-padded if it shrinks
    mid-read, so the tar stream can never be corrupted) and feed the bar."""

    def __init__(self, f, size, bar):
        self.f, self.remaining, self.bar = f, size, bar
        self.truncated = False

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        n = self.remaining if n < 0 else min(n, self.remaining)
        data = self.f.read(n)
        if not data:
            data = b"\0" * n
            self.truncated = True
        self.remaining -= len(data)
        self.bar.update(len(data))
        return data


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:,.2f} {unit}"
        n /= 1024


def scan(root, patterns):
    total, n_files = 0, 0
    with tqdm(desc="Scanning", unit=" files", leave=False) as bar:
        for full, rel, isdir in walk(root, patterns):
            if isdir:
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
            n_files += 1
            bar.update(1)
    return total, n_files


def prune(dest, keep):
    archives = sorted(
        f for f in os.listdir(dest)
        if f.startswith(ARCHIVE_PREFIX) and f.endswith(".tar.zst")
    )
    for old in archives[:-keep] if keep else []:
        os.remove(os.path.join(dest, old))
        print(f"Pruned old backup: {old}")


def restore(archive, target):
    os.makedirs(target, exist_ok=True)
    # Archives made with a window above the 128 MiB default need the decoder to
    # be told it may allocate that much, or it refuses with "Frame requires too
    # much memory for decoding".
    dctx = zstd.ZstdDecompressor(max_window_size=1 << MAX_WINDOW_LOG)
    with open(archive, "rb") as raw, dctx.stream_reader(raw) as zr, \
            tarfile.open(fileobj=zr, mode="r|") as tar:
        tar.extractall(target, filter="data")  # refuse ../, absolute paths, outside links
    print(f"Restored to {target}")


def choose_destination(initial):
    """Open a folder picker without affecting non-interactive backups."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print("ERROR: the destination picker requires tkinter. Type the path instead.")
        return None

    window = tk.Tk()
    window.withdraw()
    try:
        window.attributes("-topmost", True)
        initialdir = initial if os.path.isdir(initial) else None
        selected = filedialog.askdirectory(
            parent=window,
            title="Select backup destination folder",
            initialdir=initialdir,
            mustexist=True,
        )
    finally:
        window.destroy()

    return selected or None


# ---------------------------------------------------------------- settings io


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings["exclude"] = list(DEFAULT_EXCLUDE)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return settings
    for key in DEFAULT_SETTINGS:
        if key in saved and isinstance(saved[key], type(DEFAULT_SETTINGS[key])):
            settings[key] = saved[key]
    settings["exclude"] = [str(p) for p in settings["exclude"]]
    return settings


def save_settings(settings):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return True
    except OSError as e:
        print(f"Could not save settings: {e}")
        return False


# ---------------------------------------------------------------- the backup


def run_archive(tar, bar, counter, root, patterns, skipped):
    """Stream every kept file into the tar, updating the progress bar."""
    next_refresh = 0.0
    for full, rel, isdir in walk(root, patterns):
        try:
            ti = tar.gettarinfo(full, arcname=rel)
        except OSError as e:
            skipped.append((rel, str(e)))
            continue
        if ti is None:
            continue
        if not ti.isreg():
            tar.addfile(ti)
            continue
        try:
            f = open(full, "rb", buffering=1024 * 1024)
        except OSError as e:
            skipped.append((rel, str(e)))
            bar.update(ti.size)
            continue
        with f:
            reader = ExactReader(f, ti.size, bar)
            tar.addfile(ti, reader)
            if reader.truncated:
                skipped.append((rel, "file shrank during read; zero-padded"))
        now = time.time()
        if now >= next_refresh:
            next_refresh = now + 1.0
            if bar.n and counter.written:
                bar.set_postfix_str(
                    f"{human(counter.written)} written "
                    f"({counter.written / bar.n:.0%})",
                    refresh=False,
                )


def run_backup(s):
    """Run a backup (or dry run) from a settings dict. Returns True on success."""
    patterns = list(s["exclude"])
    root = longpath(s["source"])

    print()
    print(f"Source:   {s['source']}")
    print(f"Dest:     {s['dest']}")
    print(f"Excludes: {', '.join(patterns) if patterns else 'none'}")
    print(f"Compress: {preset_label(s)} - level {s['level']}, "
          f"{window_label(s['window_log'])} window, "
          f"{threads_label(s['threads'])} (lossless)")

    if not s["source"] or not os.path.isdir(root):
        print(f"ERROR: source folder not found: {s['source']}")
        return False

    if not s["dry_run"]:
        try:
            os.makedirs(s["dest"], exist_ok=True)
        except OSError as e:
            print(f"ERROR: cannot create destination {s['dest']}: {e}")
            return False

    total, n_files = scan(root, patterns)
    try:
        free = shutil.disk_usage(s["dest"]).free
    except OSError as e:
        print(f"ERROR: cannot read destination {s['dest']}: {e}")
        return False
    print(f"To back up: {human(total)} in {n_files:,} files "
          f"| destination free: {human(free)}")

    if s["dry_run"]:
        print("Dry run: nothing written.")
        return True
    if total == 0:
        print("ERROR: nothing to back up (everything is excluded?).")
        return False
    if free < total * 1.02 and not s["force"]:
        print("ERROR: destination may not have enough free space for a worst-case "
              "(incompressible) archive. Enable 'Force' in the menu (or --force) "
              "to proceed anyway.")
        return False

    name = f"{ARCHIVE_PREFIX}{date.today():%Y-%m-%d}.tar.zst"
    final = os.path.join(s["dest"], name)
    partial = final + ".partial"
    skipped = []
    start = time.time()

    try:
        cctx = make_compressor(s["level"], s["threads"], s["window_log"])
        with open(partial, "wb", buffering=1024 * 1024) as raw:
            counter = CountingWriter(raw)
            with cctx.stream_writer(counter) as zw, \
                    tarfile.open(mode="w|", fileobj=zw, format=tarfile.PAX_FORMAT,
                                 bufsize=1024 * 1024) as tar, \
                    tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024,
                         desc="Backing up", smoothing=0.05, mininterval=0.5,
                         bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/"
                                    "{total_fmt} [{elapsed}<{remaining}, "
                                    "{rate_fmt}{postfix}]") as bar:
                run_archive(tar, bar, counter, root, patterns, skipped)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise

    os.replace(partial, final)
    out_size = os.path.getsize(final)
    mins = (time.time() - start) / 60

    if skipped:
        with open(final + ".skipped.log", "w", encoding="utf-8") as lf:
            for rel, err in skipped:
                lf.write(f"{rel}\t{err}\n")

    print(f"\nDone: {final}")
    print(f"  {human(total)} -> {human(out_size)} "
          f"({out_size / total:.1%} of original, {human(total - out_size)} saved)")
    print(f"  Took {mins:.1f} min at "
          f"{human(total / max(time.time() - start, 1))}/s of source data")
    if skipped:
        print(f"  WARNING: {len(skipped)} items skipped - see {name}.skipped.log")

    print("\n  To restore:")
    print(f'    python backup_server.py --restore "{final}" "<folder>"')
    if s["window_log"] > 27:
        print(f"    (external tools need --long={s['window_log']}: "
              f'tar --use-compress-program="zstd -d --long={s["window_log"]}" '
              f'-xf "{name}" -C <folder>)')

    if s["keep"]:
        prune(s["dest"], s["keep"])
    return True


# ---------------------------------------------------------------- the menu


def ask(prompt, default=""):
    """Read a line; empty input keeps the default."""
    suffix = f" [{default}]" if default != "" else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def confirm(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            answer = input(f"{prompt} ({hint}): ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def pause():
    try:
        input("\nPress Enter to continue...")
    except EOFError:
        pass


def preset_label(s):
    for name, level, wlog, _ in PRESETS:
        if s["level"] == level and s["window_log"] == wlog:
            return name
    return "Custom"


def window_label(wlog):
    return human(1 << wlog).replace(".00", "")


def threads_label(n):
    return "all cores" if n < 0 else str(n)


def print_settings(s):
    print(f"  Source           : {s['source']}")
    print(f"  Destination      : {s['dest']}")
    print(f"  Excluded folders : {', '.join(s['exclude']) if s['exclude'] else 'none'}")
    print(f"  Compression      : {preset_label(s)} - level {s['level']}, "
          f"{window_label(s['window_log'])} match window (lossless)")
    print(f"  Threads          : {threads_label(s['threads'])}")
    print(f"  Keep newest      : {s['keep'] or 'all'}")
    print(f"  Dry run          : {'ON (writes nothing)' if s['dry_run'] else 'off'}")
    print(f"  Ignore space warn: {'ON' if s['force'] else 'off'}")


def edit_destination(s):
    print("\n--- Destination ---")
    print(f"Current: {s['dest']}")
    print("  1) Type a path")
    print("  2) Pick a folder in a dialog")
    print(f"  3) Reset to default ({DEFAULT_DEST})")
    print("  0) Back")
    choice = ask("Choose", "0")
    if choice == "1":
        new = ask("New destination path", s["dest"])
        if new:
            s["dest"] = new
    elif choice == "2":
        picked = choose_destination(s["dest"])
        if picked:
            s["dest"] = os.path.normpath(picked)
        else:
            print("No folder selected; destination unchanged.")
    elif choice == "3":
        s["dest"] = DEFAULT_DEST

    if choice in ("1", "2", "3"):
        print(f"Destination is now: {s['dest']}")
        if not os.path.isdir(s["dest"]):
            print("Note: that folder does not exist yet; it is created when the "
                  "backup starts.")


def edit_excludes(s):
    while True:
        print("\n--- Excluded folders (relative to the source root) ---")
        if s["exclude"]:
            for i, p in enumerate(s["exclude"], 1):
                print(f"  {i}. {p}")
        else:
            print("  (nothing excluded - everything gets backed up)")
        print("\n  a) Add an exclusion")
        print("  r) Remove an exclusion")
        print("  d) Reset to defaults "
              f"({', '.join(DEFAULT_EXCLUDE) if DEFAULT_EXCLUDE else 'none'})")
        print("  c) Clear all exclusions")
        print("  0) Back")
        choice = ask("Choose", "0").lower()

        if choice == "a":
            new = ask("Folder / relative path / glob to exclude")
            if not new:
                continue
            if any(norm(new) == norm(p) for p in s["exclude"]):
                print("Already excluded.")
            else:
                s["exclude"].append(new)
                print(f"Excluded: {new}")
        elif choice == "r":
            if not s["exclude"]:
                print("Nothing to remove.")
                continue
            raw = ask("Number to remove")
            if raw.isdigit() and 1 <= int(raw) <= len(s["exclude"]):
                print(f"Removed: {s['exclude'].pop(int(raw) - 1)}")
            else:
                print("Not a valid number.")
        elif choice == "d":
            s["exclude"] = list(DEFAULT_EXCLUDE)
            print("Exclusions reset to defaults.")
        elif choice == "c":
            if confirm("Back up EVERYTHING with no exclusions?", False):
                s["exclude"] = []
        elif choice == "0":
            return


def edit_compression(s):
    print("\n--- Compression ---")
    print("Every option here is lossless - zstd rebuilds your files byte for "
          "byte. Only speed, archive size and RAM use change.")
    print(f"Current: {preset_label(s)} (level {s['level']}, "
          f"{window_label(s['window_log'])} window)\n")
    for i, (name, level, wlog, blurb) in enumerate(PRESETS, 1):
        print(f"  {i}) {name:<9} level {level:<2} / {window_label(wlog):<8} - {blurb}")
    print("  5) Custom level and window")
    print("  0) Back")
    print("\n  * a window above 128 MiB makes a smaller archive, but other tools "
          "(7-Zip,\n    plain tar) need an explicit --long flag to open it. "
          "--restore here always works.")
    choice = ask("Choose", "0")

    if choice in ("1", "2", "3", "4"):
        _, s["level"], s["window_log"], _ = PRESETS[int(choice) - 1]
        print(f"Set to {preset_label(s)}.")
        return
    if choice != "5":
        return

    raw = ask("Compression level 1-22", str(s["level"]))
    if raw.isdigit() and 1 <= int(raw) <= 22:
        s["level"] = int(raw)
    else:
        print("Level unchanged (must be 1-22).")

    raw = ask("Match window log 20-31 (bigger = smaller archive, more RAM)",
              str(s["window_log"]))
    if raw.isdigit() and 20 <= int(raw) <= 31:
        s["window_log"] = int(raw)
    else:
        print("Window unchanged (must be 20-31).")

    raw = ask("Threads (-1 = all cores)", str(s["threads"]))
    try:
        threads = int(raw)
    except ValueError:
        print("Threads unchanged.")
        return
    if threads == 0:
        print("Threads unchanged (0 is not valid; use -1 for all cores).")
    else:
        s["threads"] = threads


def edit_retention(s):
    print("\n--- Retention ---")
    print("After a successful backup, delete all but the N newest archives in "
          "the destination. 0 keeps everything.")
    raw = ask("Keep how many archives", str(s["keep"]))
    if raw.isdigit():
        s["keep"] = int(raw)
    else:
        print("Retention unchanged.")


def do_restore(s):
    print("\n--- Restore ---")
    archive = ask("Path to the .tar.zst archive")
    if not archive:
        return
    if not os.path.isfile(archive):
        print(f"Not a file: {archive}")
        return
    target = ask("Restore into which folder")
    if not target:
        return
    print(f"\n  Archive : {archive}")
    print(f"  Into    : {target}")
    if not confirm("Restore now? Existing files may be overwritten", False):
        print("Cancelled.")
        return
    try:
        restore(archive, target)
    except Exception as e:
        print(f"Restore failed: {e}")


def start_backup(s):
    print("\n=== Confirm these settings ===")
    print_settings(s)
    print()
    if not confirm("Start the backup with these settings?", True):
        print("Cancelled - back to the menu.")
        return
    try:
        run_backup(s)
    except KeyboardInterrupt:
        print("\nBackup interrupted; the partial archive was deleted.")
    except Exception as e:
        print(f"\nBackup failed: {e}")


def menu(s):
    print(r"""
========================================
   TrueNAS server backup
========================================""")
    while True:
        print("\n--- Current settings ---")
        print_settings(s)
        print("\n--- Menu ---")
        print("  1) Change destination")
        print("  2) Change excluded folders")
        print("  3) Change source")
        print("  4) Change compression (size vs speed)")
        print("  5) Change retention (keep N newest)")
        print("  6) Toggle dry run "
              f"(now: {'ON' if s['dry_run'] else 'off'})")
        print("  7) Toggle ignore-free-space-warning "
              f"(now: {'ON' if s['force'] else 'off'})")
        print("  8) Save these settings as my defaults")
        print("  9) Restore from an archive")
        print("  s) START BACKUP")
        print("  0) Quit")

        choice = ask("Choose", "s").lower()

        if choice == "1":
            edit_destination(s)
        elif choice == "2":
            edit_excludes(s)
        elif choice == "3":
            s["source"] = ask("Source path", s["source"]) or s["source"]
            if not os.path.isdir(longpath(s["source"])):
                print("Warning: that source folder is not reachable right now.")
        elif choice == "4":
            edit_compression(s)
        elif choice == "5":
            edit_retention(s)
        elif choice == "6":
            s["dry_run"] = not s["dry_run"]
        elif choice == "7":
            s["force"] = not s["force"]
        elif choice == "8":
            if save_settings(s):
                print(f"Saved to {CONFIG_PATH}")
        elif choice == "9":
            do_restore(s)
            pause()
        elif choice in ("s", "b", "start"):
            start_backup(s)
            pause()
        elif choice in ("0", "q", "quit", "exit"):
            print("Bye.")
            return
        else:
            print("Not an option.")


# ---------------------------------------------------------------- entry point


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--menu", action="store_true",
                    help="always show the interactive menu")
    ap.add_argument("--run", action="store_true",
                    help="skip the menu and back up straight away")
    ap.add_argument("--source")
    ap.add_argument("--dest",
                    help=f"folder in which to save the backup (default: {DEFAULT_DEST})")
    ap.add_argument("--choose-dest", action="store_true",
                    help="select the destination folder in a dialog")
    ap.add_argument("--exclude", action="append", default=[], metavar="NAME",
                    help="top-level folder / relative path / glob to skip (repeatable)")
    ap.add_argument("--include", action="append", default=[], metavar="NAME",
                    type=lambda value: norm(value).strip("/"),
                    help="include a normally excluded folder (repeatable)")
    ap.add_argument("--preset", choices=[n.lower() for n, _, _, _ in PRESETS],
                    help="compression preset: " + ", ".join(
                        f"{n.lower()} (level {lv}, {1 << w >> 20} MiB window)"
                        for n, lv, w, _ in PRESETS))
    ap.add_argument("--level", type=int,
                    help="zstd level 1-22 (default 12; 19+ = max ratio, much slower)")
    ap.add_argument("--window-log", type=int, dest="window_log",
                    help="match-window log 20-31 (27 = 128 MiB; bigger = smaller "
                         "archive but ~2^N bytes of RAM per thread)")
    ap.add_argument("--threads", type=int,
                    help="compression threads (-1 = all cores)")
    ap.add_argument("--keep", type=int,
                    help="after success, keep only the N newest archives (0 = keep all)")
    ap.add_argument("--dry-run", action="store_true", help="scan and report, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="proceed even if destination free space looks too small")
    ap.add_argument("--restore", nargs=2, metavar=("ARCHIVE", "DIR"),
                    help="extract ARCHIVE into DIR instead of backing up")
    args = ap.parse_args()

    if args.restore:
        restore(*args.restore)
        return

    s = load_settings()
    if args.preset:
        for name, level, wlog, _ in PRESETS:
            if name.lower() == args.preset:
                s["level"], s["window_log"] = level, wlog
    for key in ("source", "dest", "level", "window_log", "threads", "keep"):
        if getattr(args, key) is not None:
            s[key] = getattr(args, key)
    if args.dry_run:
        s["dry_run"] = True
    if args.force:
        s["force"] = True
    if args.choose_dest:
        picked = choose_destination(s["dest"])
        if not picked:
            sys.exit("Backup cancelled: no destination folder selected.")
        s["dest"] = os.path.normpath(picked)

    included = set(args.include)
    patterns, seen = [], set()
    for pat in [p for p in s["exclude"] if norm(p) not in included] + args.exclude:
        if norm(pat) not in seen:
            seen.add(norm(pat))
            patterns.append(pat)
    s["exclude"] = patterns

    if args.run or (not args.menu and not sys.stdin.isatty()):
        sys.exit(0 if run_backup(s) else 1)

    menu(s)


if __name__ == "__main__":
    main()
