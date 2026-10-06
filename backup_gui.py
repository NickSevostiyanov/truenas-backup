r"""
backup_gui.py - click-and-run window for backup_server.py.

Lists the folders that exist on the share right now, you tick the ones to back
up, pick a destination and press Back up. Same archive as backup_server.py:
one lossless .tar.zst, written as .partial and renamed only on success.

Build a single executable (Windows or Linux):
    pip install -r requirements.txt pyinstaller
    pyinstaller --onefile --windowed --name TrueNAS-Backup backup_gui.py
"""

import os
import stat
import sys
import tarfile
import threading
import time
import tkinter as tk
from datetime import date
from tkinter import filedialog, messagebox, ttk

import backup_server as bs

# A frozen exe unpacks to a temp dir; keep the shared settings file next to the exe.
if getattr(sys, "frozen", False):
    bs.CONFIG_PATH = os.path.join(os.path.dirname(sys.executable),
                                  "backup_server_config.json")


class Cancelled(Exception):
    pass


class Progress:
    """Stands in for the tqdm bar that bs.run_archive expects; read by the UI."""

    def __init__(self):
        self.n = self.total = 0
        self.postfix = ""
        self.cancel = False

    def update(self, k):
        if self.cancel:
            raise Cancelled
        self.n += k

    def set_postfix_str(self, s, refresh=False):
        self.postfix = s


def backup(source, dest, patterns, level, wlog, p):
    """Mirror of bs.run_backup without the console output. Returns a summary."""
    root = bs.longpath(source)
    os.makedirs(dest, exist_ok=True)
    for full, rel, isdir in bs.walk(root, patterns):
        if p.cancel:
            raise Cancelled
        if not isdir:
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                p.total += st.st_size
    if not p.total:
        raise RuntimeError("Nothing to back up - no folders selected?")

    name = f"{bs.ARCHIVE_PREFIX}{date.today():%Y-%m-%d}.tar.zst"
    final = os.path.join(dest, name)
    partial = final + ".partial"
    skipped = []
    start = time.time()
    try:
        cctx = bs.make_compressor(level, -1, wlog)
        with open(partial, "wb", buffering=1024 * 1024) as raw:
            counter = bs.CountingWriter(raw)
            with cctx.stream_writer(counter) as zw, \
                    tarfile.open(mode="w|", fileobj=zw, format=tarfile.PAX_FORMAT,
                                 bufsize=1024 * 1024) as tar:
                bs.run_archive(tar, p, counter, root, patterns, skipped)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    os.replace(partial, final)

    if skipped:
        with open(final + ".skipped.log", "w", encoding="utf-8") as lf:
            for rel, err in skipped:
                lf.write(f"{rel}\t{err}\n")
    out = os.path.getsize(final)
    msg = (f"Saved {final}\n\n{bs.human(p.total)} -> {bs.human(out)} "
           f"({out / p.total:.1%}) in {(time.time() - start) / 60:.1f} min.")
    if skipped:
        msg += f"\n\n{len(skipped)} unreadable items skipped - see {name}.skipped.log"
    if wlog > 27:
        msg += f"\n\nOther tools need zstd --long={wlog} to open this archive."
    return msg


class App:
    def __init__(self, win):
        self.win, self.s = win, bs.load_settings()
        self.names, self.worker, self.p, self.result = [], None, None, None
        win.title("TrueNAS Backup")
        win.minsize(600, 500)
        win.protocol("WM_DELETE_WINDOW", self.close)
        f = ttk.Frame(win, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(2, weight=1)

        ttk.Label(f, text="Source").grid(row=0, column=0, sticky="w")
        self.source = tk.StringVar(value=self.s["source"])
        ttk.Entry(f, textvariable=self.source).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(f, text="Browse...", command=self.pick_source).grid(row=0, column=2)

        ttk.Label(f, text="Folders to back up (click to toggle):").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 2))
        box = ttk.Frame(f)
        box.grid(row=2, column=0, columnspan=3, sticky="nsew")
        self.list = tk.Listbox(box, selectmode="multiple", exportselection=False,
                               activestyle="none")
        sb = ttk.Scrollbar(box, command=self.list.yview)
        self.list.config(yscrollcommand=sb.set)
        self.list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        sel = ttk.Frame(f)
        sel.grid(row=3, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Button(sel, text="All", command=lambda: self.list.select_set(0, "end")).pack(side="left")
        ttk.Button(sel, text="None", command=lambda: self.list.select_clear(0, "end")).pack(side="left")
        ttk.Label(sel, text="  Loose files in the share root are always included.").pack(side="left")

        ttk.Label(f, text="Save to").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.dest = tk.StringVar(value=self.s["dest"])
        ttk.Entry(f, textvariable=self.dest).grid(row=4, column=1, sticky="ew", padx=5, pady=(8, 0))
        ttk.Button(f, text="Browse...", command=self.browse).grid(row=4, column=2, pady=(8, 0))

        ttk.Label(f, text="Compression").grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.labels = [f"{n} - {b}" for n, _, _, b in bs.PRESETS]
        self.preset = ttk.Combobox(f, values=self.labels, state="readonly")
        self.preset.current(next((i for i, (_, lv, w, _) in enumerate(bs.PRESETS)
                                  if (lv, w) == (self.s["level"], self.s["window_log"])), 1))
        self.preset.grid(row=5, column=1, columnspan=2, sticky="ew", padx=5, pady=(8, 0))

        self.bar = ttk.Progressbar(f, maximum=1000)
        self.bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 2))
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(f, textvariable=self.status).grid(row=7, column=0, columnspan=3, sticky="w")
        self.go = ttk.Button(f, text="Back up", command=self.start)
        self.go.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.refresh()

    def refresh(self):
        if not self.source.get():
            self.status.set("Choose your TrueNAS share to start.")
            return
        try:
            with os.scandir(bs.longpath(self.source.get())) as it:
                self.names = sorted((e.name for e in it if e.is_dir()), key=str.casefold)
        except OSError as e:
            self.names = []
            self.status.set(f"Cannot read source: {e}")
        skip = {bs.norm(p) for p in self.s["exclude"]}
        self.list.delete(0, "end")
        for i, n in enumerate(self.names):
            self.list.insert("end", n)
            if bs.norm(n) not in skip:
                self.list.select_set(i)

    def pick_source(self):
        d = filedialog.askdirectory(initialdir=self.source.get() or None,
                                    title="Choose your TrueNAS share")
        if d:
            self.source.set(os.path.normpath(d))
            self.refresh()

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.dest.get(), title="Save backup to")
        if d:
            self.dest.set(os.path.normpath(d))

    def start(self):
        if self.worker:  # button doubles as Cancel
            self.p.cancel = True
            self.status.set("Cancelling...")
            return
        if not self.names:
            messagebox.showerror("TrueNAS Backup", "Choose your TrueNAS share first.")
            return
        picked = set(self.list.curselection())
        top = {bs.norm(n) for n in self.names}
        # Unticked folders + any saved non-top-level patterns (globs, subpaths).
        patterns = [p for p in self.s["exclude"] if bs.norm(p) not in top]
        patterns += [n for i, n in enumerate(self.names) if i not in picked]
        _, level, wlog, _ = bs.PRESETS[self.preset.current()]
        self.s.update(source=self.source.get(), dest=self.dest.get(),
                      exclude=patterns, level=level, window_log=wlog)
        bs.save_settings(self.s)  # remember choices for next time

        self.p, self.result, self.t0 = Progress(), None, time.time()
        args = (self.s["source"], self.s["dest"], patterns, level, wlog, self.p)
        self.worker = threading.Thread(target=self.run, args=args, daemon=True)
        self.worker.start()
        self.go.config(text="Cancel")
        self.tick()

    def run(self, *args):
        try:
            self.result = (True, backup(*args))
        except Cancelled:
            self.result = (False, "Backup cancelled; nothing was kept.")
        except Exception as e:
            self.result = (False, f"Backup failed: {e}")

    def tick(self):
        p = self.p
        if self.worker.is_alive():
            if not p.n:
                self.bar.config(mode="indeterminate")
                self.bar.step(10)
                self.status.set(f"Scanning... {bs.human(p.total)} found")
            else:
                self.bar.config(mode="determinate", value=1000 * p.n / p.total)
                rate = p.n / max(time.time() - self.t0, 1)
                eta = (p.total - p.n) / rate / 60
                self.status.set(f"{bs.human(p.n)} / {bs.human(p.total)}  "
                                f"{bs.human(rate)}/s  ~{eta:.0f} min left  {p.postfix}")
            self.win.after(300, self.tick)
            return
        self.worker = None
        self.go.config(text="Back up")
        self.bar.config(mode="determinate", value=1000 if self.result[0] else 0)
        ok, msg = self.result
        self.status.set(msg.splitlines()[0])
        if getattr(self, "closing", False):
            self.win.destroy()
        else:
            (messagebox.showinfo if ok else messagebox.showerror)("TrueNAS Backup", msg)

    def close(self):
        if not self.worker:
            self.win.destroy()
        elif messagebox.askyesno("TrueNAS Backup", "A backup is running. Cancel it and quit?"):
            self.closing, self.p.cancel = True, True


if __name__ == "__main__":
    try:  # sharp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
