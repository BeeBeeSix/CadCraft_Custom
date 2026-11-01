#!/usr/bin/env python3
"""
cadcraft_launcher.py - pick any of your CADCraft Python tools from a
dropdown and start it.

It scans a folder for scripts (default: %APPDATA%\\CADCraft\\support), so a
new script you drop in there appears automatically (press Refresh).

  python cadcraft_launcher.py                  (uses the default folder)
  python cadcraft_launcher.py --folder D:\\my\\tools

What is listed
  Scripts that use tkinter (dialogs). Tick "Include command-line tools" to
  also list the others (helper modules such as cadcraft_apollonius.py).
  This launcher is never listed.

What happens on Launch
  The script is started as its own process with the same Python, working
  directory = the scripts folder (so scripts that import each other work),
  and --port N is added when the script supports it. Anything extra goes in
  "Extra arguments". Output and errors appear in the log pane, and the tool
  keeps running if you close the launcher.
"""
import argparse
import ast
import os
import shlex
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

DEFAULT_PORT = 7979
FALLBACK = r"C:\Users\bkd6b\AppData\Roaming\CADCraft\support"
SELF = os.path.basename(os.path.abspath(__file__)).lower()


def default_folder():
    appdata = os.environ.get("APPDATA")
    for d in ((os.path.join(appdata, "CADCraft", "support") if appdata
               else None), FALLBACK):
        if d and os.path.isdir(d):
            return d
    return os.path.dirname(os.path.abspath(__file__))


def describe_script(path):
    """Read a script's metadata WITHOUT importing or running it.
    Returns dict: doc (full docstring), summary (one line), gui, has_port."""
    info = {"doc": "", "summary": "", "gui": False, "has_port": False}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return info
    try:
        info["doc"] = (ast.get_docstring(ast.parse(text)) or "").strip()
    except SyntaxError:
        info["doc"] = "(this file has a syntax error)"
    first = info["doc"].splitlines()[0] if info["doc"] else ""
    # "name.py - what it does"  ->  "what it does"
    for sep in (" - ", " \u2013 ", " \u2014 "):
        if sep in first:
            first = first.split(sep, 1)[1]
            break
    info["summary"] = first.strip()
    info["gui"] = "tkinter" in text
    info["has_port"] = "--port" in text
    return info


def scan(folder, include_cli):
    out = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return out
    for n in names:
        if not n.lower().endswith(".py") or n.startswith("_"):
            continue
        if n.lower() == SELF:
            continue
        info = describe_script(os.path.join(folder, n))
        if info["gui"] or include_cli:
            out.append((n, info))
    return out


def run_gui(folder):
    root = tk.Tk()
    root.title("CADCraft Tools")
    root.minsize(640, 520)
    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(4, weight=1)
    frm.rowconfigure(6, weight=1)

    folder_var = tk.StringVar(value=folder)
    port_var = tk.StringVar(value=str(os.environ.get(
        "CADCRAFT_CONTROL_PORT", DEFAULT_PORT)))
    args_var = tk.StringVar()
    cli_var = tk.BooleanVar(value=False)
    tool_var = tk.StringVar()
    summary_var = tk.StringVar()
    state = {"items": {}, "procs": []}

    # ---- folder row -----------------------------------------------------
    ttk.Label(frm, text="Folder").grid(row=0, column=0, sticky="w")
    ttk.Entry(frm, textvariable=folder_var).grid(row=0, column=1,
                                                 sticky="ew", padx=6)
    brow = ttk.Frame(frm)
    brow.grid(row=0, column=2)
    ttk.Button(brow, text="Browse...", command=lambda: browse()).grid(
        row=0, column=0, padx=2)
    ttk.Button(brow, text="Refresh", command=lambda: refresh()).grid(
        row=0, column=1, padx=2)

    # ---- tool dropdown --------------------------------------------------
    ttk.Label(frm, text="Tool").grid(row=1, column=0, sticky="w", pady=(10, 0))
    combo = ttk.Combobox(frm, textvariable=tool_var, state="readonly",
                         width=44)
    combo.grid(row=1, column=1, sticky="ew", padx=6, pady=(10, 0))
    ttk.Checkbutton(frm, text="Include command-line tools", variable=cli_var,
                    command=lambda: refresh()).grid(row=1, column=2,
                                                    pady=(10, 0))
    ttk.Label(frm, textvariable=summary_var, wraplength=600,
              foreground="#444").grid(row=2, column=0, columnspan=3,
                                      sticky="w", pady=(4, 0))

    # ---- options --------------------------------------------------------
    opt = ttk.Frame(frm)
    opt.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    opt.columnconfigure(3, weight=1)
    ttk.Label(opt, text="Port").grid(row=0, column=0, sticky="w")
    ttk.Entry(opt, textvariable=port_var, width=7).grid(row=0, column=1,
                                                        padx=(6, 14))
    ttk.Label(opt, text="Extra arguments").grid(row=0, column=2, sticky="w")
    ttk.Entry(opt, textvariable=args_var).grid(row=0, column=3, sticky="ew",
                                               padx=6)
    ttk.Button(opt, text="Launch", command=lambda: launch()).grid(
        row=0, column=4, padx=(6, 0))

    # ---- docstring ------------------------------------------------------
    doc = tk.Text(frm, height=10, wrap="word", state="disabled",
                  font=("TkFixedFont", 9))
    doc.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(10, 0))

    ttk.Label(frm, text="Log").grid(row=5, column=0, sticky="w", pady=(8, 0))
    log_box = tk.Text(frm, height=8, wrap="word", state="disabled",
                      font=("TkFixedFont", 9))
    log_box.grid(row=6, column=0, columnspan=3, sticky="nsew")

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    def set_doc(text):
        doc.configure(state="normal")
        doc.delete("1.0", "end")
        doc.insert("1.0", text)
        doc.configure(state="disabled")

    # ---- behaviour ------------------------------------------------------
    def show_tool(*_):
        item = state["items"].get(tool_var.get())
        if not item:
            summary_var.set("")
            set_doc("")
            return
        summary_var.set(item["summary"] or "(no description)")
        set_doc(item["doc"] or "(this script has no docstring)")

    def refresh(*_):
        d = folder_var.get().strip()
        if not os.path.isdir(d):
            combo.configure(values=[])
            tool_var.set("")
            state["items"] = {}
            show_tool()
            log("Folder not found: %s" % d)
            return
        found = scan(d, cli_var.get())
        state["items"] = {n: dict(i, path=os.path.join(d, n))
                          for n, i in found}
        names = [n for n, _ in found]
        combo.configure(values=names)
        if tool_var.get() not in state["items"]:
            tool_var.set(names[0] if names else "")
        show_tool()
        log("%d tool(s) in %s" % (len(names), d))

    def browse():
        d = filedialog.askdirectory(initialdir=folder_var.get() or None)
        if d:
            folder_var.set(os.path.normpath(d))
            refresh()

    def launch(*_):
        item = state["items"].get(tool_var.get())
        if not item:
            messagebox.showinfo("CADCraft Tools", "Pick a tool first.")
            return
        cmd = [sys.executable, "-u", item["path"]]
        port = port_var.get().strip()
        if item["has_port"] and port:
            cmd += ["--port", port]
        try:
            cmd += shlex.split(args_var.get(), posix=(os.name != "nt"))
        except ValueError as e:
            messagebox.showerror("CADCraft Tools", "Bad arguments: %s" % e)
            return
        # output goes to a file (not a pipe) so the tool survives the
        # launcher being closed
        fd, logpath = tempfile.mkstemp(prefix="cadcraft_tool_", suffix=".log")
        out = os.fdopen(fd, "w", encoding="utf-8")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        try:
            p = subprocess.Popen(cmd, cwd=os.path.dirname(item["path"]),
                                 stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, **kw)
        except OSError as e:
            out.close()
            log("Could not start %s: %s" % (tool_var.get(), e))
            return
        out.close()
        name = tool_var.get()
        log("Started %s (pid %d)" % (name, p.pid))
        state["procs"].append({"p": p, "name": name, "log": logpath,
                               "pos": 0})

    def poll():
        alive = []
        for pr in state["procs"]:
            try:
                with open(pr["log"], "r", encoding="utf-8",
                          errors="replace") as f:
                    f.seek(pr["pos"])
                    chunk = f.read()
                    pr["pos"] = f.tell()
            except OSError:
                chunk = ""
            for line in chunk.splitlines():
                log("[%s] %s" % (pr["name"], line))
            code = pr["p"].poll()
            if code is None:
                alive.append(pr)
            else:
                log("[%s] exited (code %s)" % (pr["name"], code))
        state["procs"] = alive
        root.after(500, poll)

    combo.bind("<<ComboboxSelected>>", show_tool)
    root.bind("<Return>", launch)
    refresh()
    poll()
    root.mainloop()
    return 0


def main():
    ap = argparse.ArgumentParser(description="Launcher for CADCraft tools")
    ap.add_argument("--folder", default=None)
    a = ap.parse_args()
    return run_gui(a.folder or default_folder())


if __name__ == "__main__":
    sys.exit(main())
