#!/usr/bin/env python3
"""
cadcraft_blocks_csv.py - export block information from CADCraft to CSV:

    BlockName,Attribute,X,Y,Z

CADCraft keeps a block's attributes inside the block itself, where LISP
(entget / entnext) cannot see them. This program asks the running
CADCraft for the full structured data over its control channel instead.

How to use
  1. Start CADCraft with the control channel on:   cadcraft --control 7979
  2. Run:   python cadcraft_blocks_csv.py          (opens a small dialog)
  3. Choose All blocks or Selected blocks, pick the attribute tag (or
     "first attribute"), choose the file, press Export.

Command-line use (no dialog):
  python cadcraft_blocks_csv.py --out blocks.csv
  python cadcraft_blocks_csv.py --out blocks.csv --tag BH-ID --selected

Python 3 only (tkinter is used for the dialog and comes with the standard
python.org installer). No other packages needed.
"""
import argparse
import csv
import glob
import json
import os
import socket
import sys
import tempfile

DEFAULT_PORT = 7979
FIRST = "(first attribute)"


# ----------------------------------------------------------------------
# Control-channel client
# ----------------------------------------------------------------------
class CadError(Exception):
    pass


def find_token(port):
    env = os.environ.get("CADCRAFT_TOKEN")
    if env:
        return env.strip()
    name = "cadcraft-control-%s.token" % port
    dirs = []
    for var in ("XDG_RUNTIME_DIR", "LOCALAPPDATA"):
        if os.environ.get(var):
            dirs.append(os.environ[var])
    home = os.path.expanduser("~")
    dirs += [os.path.join(home, ".cache"),
             os.path.join(home, "Library", "Caches"),
             tempfile.gettempdir()]
    for d in dirs:
        for pattern in (os.path.join(d, name),
                        os.path.join(d, "*", name),
                        os.path.join(d, "*", "*", name)):
            for path in glob.glob(pattern):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read().strip()
                except OSError:
                    pass
    return None


class Client:
    def __init__(self, port=DEFAULT_PORT, token=None, host="127.0.0.1"):
        self.token = token if token is not None else find_token(port)
        if self.token is None:
            raise CadError(
                "Could not find the session token file "
                "cadcraft-control-%s.token. Is CADCraft running with "
                "--control %s? (You can also set CADCRAFT_TOKEN or use "
                "--token.)" % (port, port))
        try:
            self.sock = socket.create_connection((host, port), timeout=5)
        except OSError as e:
            raise CadError("Cannot connect to CADCraft on port %s: %s"
                           % (port, e))
        self.sock.settimeout(65)
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self._id = 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def call(self, method, params=None):
        self._id += 1
        req = {"id": self._id, "method": method,
               "params": params or {}, "token": self.token}
        self.sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        line = self.rfile.readline()
        if not line:
            raise CadError("CADCraft closed the connection.")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise CadError(str(reply.get("error", "unknown error")))
        return reply.get("result")

    def execute(self, command, params=None):
        return self.call("engine.execute",
                         {"command": command, "params": params or {}})


# ----------------------------------------------------------------------
# Reading blocks
# ----------------------------------------------------------------------
def find_key(obj, key, kind):
    """Depth-first search for obj[key] of the given type."""
    if isinstance(obj, dict):
        if isinstance(obj.get(key), kind):
            return obj[key]
        for v in obj.values():
            r = find_key(v, key, kind)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key, kind)
            if r is not None:
                return r
    return None


def find_insert(obj):
    """Find the dict describing an insert (type 'insert' with a 'block')."""
    if isinstance(obj, dict):
        if str(obj.get("type", "")).lower() == "insert" and "block" in obj:
            return obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = find_insert(v)
                if r is not None:
                    return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_insert(v)
            if r is not None:
                return r
    return None


def fetch_inserts(cl, selected=False, page=1000):
    """All block references in the active space (or just the selected)."""
    out = []
    offset = 0
    while True:
        params = {"limit": page, "offset": offset}
        if selected:
            params["selected"] = True
        res = cl.execute("entities", params)
        ents = find_key(res, "entities", list) or []
        total = find_key(res, "count", int)
        for e in ents:
            ins = find_insert(e)
            if ins is not None:
                out.append(ins)
        offset += len(ents)
        if not ents or total is None or offset >= total:
            break
    return out


def attrib_value(a):
    text = a.get("text")
    if isinstance(text, dict) and "value" in text:
        return str(text["value"])
    if "value" in a:
        return str(a["value"])
    return ""


def pick_attrib(attribs, tag):
    """Value of ONE attribute: the named tag, or the first one."""
    if not attribs:
        return ""
    if not tag or tag == FIRST:
        return attrib_value(attribs[0])
    for a in attribs:
        if str(a.get("tag", "")).upper() == tag.upper():
            return attrib_value(a)
    return ""


def tags_of(inserts):
    seen = []
    for ins in inserts:
        for a in ins.get("attribs") or []:
            t = str(a.get("tag", ""))
            if t and t not in seen:
                seen.append(t)
    return seen


def make_rows(inserts, tag):
    rows = []
    for ins in inserts:
        pt = ins.get("insert") or {}
        rows.append([
            str(ins.get("block", "")),
            pick_attrib(ins.get("attribs"), tag),
            "%.4f" % float(pt.get("x", 0.0)),
            "%.4f" % float(pt.get("y", 0.0)),
            "%.4f" % float(pt.get("z", 0.0)),
        ])
    return rows


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["BlockName", "Attribute", "X", "Y", "Z"])
        w.writerows(rows)


# ----------------------------------------------------------------------
# Dialog
# ----------------------------------------------------------------------
def run_gui(port, token):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    try:
        cl = Client(port, token)
    except CadError as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("CADCraft blocks to CSV", str(e))
        return 1

    root = tk.Tk()
    root.title("CADCraft - Export blocks to CSV")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    scope = tk.StringVar(value="all")
    tag = tk.StringVar(value=FIRST)
    path = tk.StringVar(value=os.path.join(os.getcwd(), "blocks.csv"))
    status = tk.StringVar(value="")

    ttk.Label(frm, text="Blocks to export:").grid(row=0, column=0,
                                                  sticky="w")
    ttk.Radiobutton(frm, text="All blocks in the drawing", value="all",
                    variable=scope).grid(row=0, column=1, sticky="w")
    ttk.Radiobutton(frm, text="Only the selected blocks", value="sel",
                    variable=scope).grid(row=1, column=1, sticky="w")

    ttk.Label(frm, text="Attribute:").grid(row=2, column=0, sticky="w",
                                           pady=(8, 0))
    tag_box = ttk.Combobox(frm, textvariable=tag, values=[FIRST], width=30)
    tag_box.grid(row=2, column=1, sticky="w", pady=(8, 0))

    ttk.Label(frm, text="CSV file:").grid(row=3, column=0, sticky="w",
                                          pady=(8, 0))
    ttk.Entry(frm, textvariable=path, width=46).grid(row=3, column=1,
                                                     sticky="w", pady=(8, 0))

    def browse():
        p = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=os.path.basename(path.get()),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if p:
            path.set(p)

    ttk.Button(frm, text="Browse...", command=browse).grid(
        row=3, column=2, padx=(6, 0), pady=(8, 0))

    def inserts():
        return fetch_inserts(cl, selected=(scope.get() == "sel"))

    def refresh_tags():
        try:
            ins = inserts()
            tags = tags_of(ins)
            tag_box.configure(values=[FIRST] + tags)
            status.set("%d block(s); attribute tags: %s"
                       % (len(ins), ", ".join(tags) if tags else "none"))
        except CadError as e:
            status.set("Error: %s" % e)

    def export():
        try:
            ins = inserts()
            if not ins:
                messagebox.showinfo("Export", "No blocks found.")
                return
            rows = make_rows(ins, tag.get())
            write_csv(path.get(), rows)
            filled = sum(1 for r in rows if r[1] != "")
            status.set("%d block(s) written to %s (%d with an attribute value)"
                       % (len(rows), path.get(), filled))
        except (CadError, OSError) as e:
            status.set("Error: %s" % e)
            messagebox.showerror("Export", str(e))

    btns = ttk.Frame(frm)
    btns.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))
    ttk.Button(btns, text="Find attribute tags",
               command=refresh_tags).grid(row=0, column=0, padx=4)
    ttk.Button(btns, text="Export", command=export).grid(row=0, column=1,
                                                         padx=4)
    ttk.Button(btns, text="Close", command=root.destroy).grid(row=0,
                                                              column=2,
                                                              padx=4)
    ttk.Label(frm, textvariable=status, wraplength=520,
              foreground="#444").grid(row=5, column=0, columnspan=3,
                                      sticky="w", pady=(10, 0))
    refresh_tags()
    root.mainloop()
    cl.close()
    return 0


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Export CADCraft blocks to CSV")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CADCRAFT_CONTROL_PORT",
                                               DEFAULT_PORT)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--out", help="CSV file to write (skips the dialog)")
    ap.add_argument("--tag", default="",
                    help="attribute tag to report (default: first attribute)")
    ap.add_argument("--selected", action="store_true",
                    help="only the blocks selected in CADCraft")
    args = ap.parse_args()

    if not args.out:
        return run_gui(args.port, args.token)
    try:
        cl = Client(args.port, args.token)
        ins = fetch_inserts(cl, selected=args.selected)
        rows = make_rows(ins, args.tag)
        write_csv(args.out, rows)
        cl.close()
    except (CadError, OSError) as e:
        print("ERROR:", e)
        return 1
    filled = sum(1 for r in rows if r[1] != "")
    print("%d block(s) written to %s (%d with an attribute value)"
          % (len(rows), args.out, filled))
    return 0


if __name__ == "__main__":
    sys.exit(main())
