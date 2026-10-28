#!/usr/bin/env python3
"""
cadcraft_attedit.py - edit the attributes of a selected block in CADCraft.

CADCraft keeps a block's attributes inside the block, where LISP cannot
reach them, so this runs as a separate program (Python 3 + tkinter, both
standard) and talks to the running CADCraft over its control channel,
using CADCraft's own `attedit` command ({handle, values: {TAG: value}}).

How to use
  1. Start CADCraft with the control channel on:   cadcraft --control 7979
  2. Select a block (or several) in CADCraft.
  3. Run:   python cadcraft_attedit.py
  4. Change the values in the dialog and press Apply.

Command-line use (no dialog):
  python cadcraft_attedit.py --list
      shows the selected blocks and their attributes
  python cadcraft_attedit.py --set BH-ID=XYZ --set DEPTH=4.5
      edits those attributes on the selected block
  python cadcraft_attedit.py --set BH-ID=XYZ --end-refedit discard
      same, but first ends an open in-place block edit (REFEDIT)
  python cadcraft_attedit.py --diag
      prints what CADCraft returns (use it if something does not work)
"""
import argparse
import glob
import json
import os
import socket
import sys
import tempfile

DEFAULT_PORT = 7979


# ----------------------------------------------------------------------
# Control-channel client
# ----------------------------------------------------------------------
class CadError(Exception):
    pass


class RefEditOpen(CadError):
    """CADCraft has an in-place block edit (REFEDIT) open."""


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


def fetch_inserts(cl, selected=True, page=1000):
    """Block references (selected ones by default), each with '_handle'."""
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
                h = find_key(e, "handle", (str, int))
                ins = dict(ins)
                ins["_handle"] = None if h is None else str(h)
                out.append(ins)
        offset += len(ents)
        if not ents or total is None or offset >= total:
            break
    return out


def attrib_value(a):
    t = a.get("text")
    if isinstance(t, dict) and "value" in t:
        return str(t["value"])
    return str(a.get("value", ""))


def describe(ins):
    p = ins.get("insert") or {}
    return "%s  at (%.2f, %.2f)  handle %s" % (
        ins.get("block", "?"), float(p.get("x", 0)), float(p.get("y", 0)),
        ins.get("_handle") or "?")


# ----------------------------------------------------------------------
# Editing
# ----------------------------------------------------------------------
def set_attributes(cl, ins, new_values, log):
    """new_values: {TAG: value}. Uses CADCraft's attedit command, then
    re-reads the block to confirm."""
    handle = ins.get("_handle")
    if not handle:
        raise CadError("CADCraft did not report a handle for this block, "
                       "so it cannot be edited.")
    current = {str(a.get("tag", "")).upper(): attrib_value(a)
               for a in (ins.get("attribs") or [])}
    todo = {t: v for t, v in new_values.items()
            if current.get(t.upper()) != v}
    if not todo:
        log("Nothing to change.")
        return 0
    for t, v in todo.items():
        log("%s: %r -> %r" % (t, current.get(t.upper(), "?"), v))
    try:
        res = cl.execute("attedit", {"handle": handle, "values": todo})
    except CadError as e:
        if "refedit" in str(e).lower() or "working set" in str(e).lower():
            raise RefEditOpen(str(e))
        raise
    changed = find_key(res, "changed", int)
    unknown = find_key(res, "unknownTags", list) or []
    if unknown:
        raise CadError("These tags are not on the block: %s"
                       % ", ".join(str(u) for u in unknown))
    # confirm by reading the block back
    now = [b for b in fetch_inserts(cl, selected=False)
           if b.get("_handle") == handle]
    if now:
        have = {str(a.get("tag", "")).upper(): attrib_value(a)
                for a in (now[0].get("attribs") or [])}
        bad = [t for t, v in todo.items() if have.get(t.upper()) != v]
        if bad:
            raise CadError("CADCraft accepted the edit but %s still shows "
                           "the old value. (attedit replied: %s)"
                           % (", ".join(bad), json.dumps(res)))
    log("Done (%s attribute(s) changed)." % (changed if changed is not None
                                              else len(todo)))
    return len(todo)


def close_refedit(cl, save, log):
    """End CADCraft's in-place block edit (REFCLOSE)."""
    log("Closing the in-place block edit (%s)."
        % ("saving changes" if save else "discarding changes"))
    cl.execute("refclose", {"option": "save" if save else "discard"})


# ----------------------------------------------------------------------
# Diagnostics / listing
# ----------------------------------------------------------------------
def list_selected(cl, out=print):
    sel = fetch_inserts(cl, selected=True)
    if not sel:
        out("No block is selected in CADCraft. Select a block first.")
    for ins in sel:
        out(describe(ins))
        for a in ins.get("attribs") or []:
            out("    %-20s = %r" % (a.get("tag"), attrib_value(a)))
    return sel


def diagnostics(cl, out=print):
    out("== ping: %s" % cl.call("ping"))
    cmds = cl.call("engine.commands")
    items = cmds if isinstance(cmds, list) else (
        find_key(cmds, "commands", list) or [])
    for c in items:
        if isinstance(c, dict) and c.get("id") in ("attedit", "-attedit"):
            out("== %s: %s" % (c.get("id"), json.dumps(c)))
    out("== selected blocks (raw JSON):")
    sel = fetch_inserts(cl, selected=True)
    if not sel:
        out("   none - select a block in CADCraft and run --diag again")
    for ins in sel:
        out("   " + json.dumps(ins)[:800])


# ----------------------------------------------------------------------
# Dialog
# ----------------------------------------------------------------------
def run_gui(port, token):
    import tkinter as tk
    from tkinter import ttk, messagebox

    try:
        cl = Client(port, token)
    except CadError as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("CADCraft attributes", str(e))
        return 1

    root = tk.Tk()
    root.title("CADCraft - Edit block attributes")
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    blocks = []
    pick = tk.StringVar()
    rows = []   # (tag, original value, StringVar)

    ttk.Label(frm, text="Selected block:").grid(row=0, column=0, sticky="w")
    pick_box = ttk.Combobox(frm, textvariable=pick, state="readonly",
                            width=52)
    pick_box.grid(row=0, column=1, sticky="w")
    att_frame = ttk.Frame(frm)
    att_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))

    log_box = tk.Text(frm, width=78, height=7, state="disabled")
    log_box.grid(row=3, column=0, columnspan=3, pady=(10, 0))

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    def show_block(*_):
        for w in att_frame.winfo_children():
            w.destroy()
        rows.clear()
        i = pick_box.current()
        if i < 0 or i >= len(blocks):
            return
        atts = blocks[i].get("attribs") or []
        if not atts:
            ttk.Label(att_frame, text="This block has no attributes."
                      ).grid(row=0, column=0)
        for r, a in enumerate(atts):
            tag = str(a.get("tag", ""))
            ttk.Label(att_frame, text=tag, width=18
                      ).grid(row=r, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=attrib_value(a))
            ttk.Entry(att_frame, textvariable=v, width=48
                      ).grid(row=r, column=1, sticky="w", pady=2)
            rows.append((tag, attrib_value(a), v))

    pick_box.bind("<<ComboboxSelected>>", show_block)

    def refresh(*_):
        nonlocal blocks
        keep = pick_box.current()
        try:
            blocks = fetch_inserts(cl, selected=True)
        except CadError as e:
            log("Refresh failed: %s" % e)
            return
        pick_box.configure(values=[describe(b) for b in blocks])
        if blocks:
            pick_box.current(keep if 0 <= keep < len(blocks) else 0)
        else:
            pick.set("")
            log("No block is selected in CADCraft. Select one, then "
                "press Refresh.")
        show_block()

    def apply(*_):
        i = pick_box.current()
        if i < 0:
            messagebox.showinfo("Attributes", "Select a block first.")
            return
        changed = {tag: v.get() for tag, orig, v in rows if v.get() != orig}
        if not changed:
            messagebox.showinfo("Attributes", "Nothing was changed.")
            return
        try:
            try:
                set_attributes(cl, blocks[i], changed, log)
            except RefEditOpen:
                ans = messagebox.askyesnocancel(
                    "Block edit is open",
                    "CADCraft has an in-place block edit (REFEDIT) open, "
                    "and it will not change attributes until that ends.\n\n"
                    "Yes = end it and DISCARD any changes made inside it "
                    "(choose this if you only wanted to edit the "
                    "attribute)\nNo = end it and SAVE its changes\n"
                    "Cancel = leave it open and stop")
                if ans is None:
                    log("Cancelled - the block edit is still open.")
                    return
                close_refedit(cl, save=(ans is False), log=log)
                set_attributes(cl, blocks[i], changed, log)
        except CadError as e:
            log("ERROR: %s" % e)
            messagebox.showerror("Attributes", str(e))
        refresh()

    btns = ttk.Frame(frm)
    btns.grid(row=2, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(btns, text="Refresh", command=refresh).grid(row=0, column=0,
                                                           padx=4)
    ttk.Button(btns, text="Apply", command=apply).grid(row=0, column=1,
                                                       padx=4)
    ttk.Button(btns, text="Close", command=root.destroy).grid(row=0,
                                                              column=2,
                                                              padx=4)
    refresh()
    root.mainloop()
    cl.close()
    return 0


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Edit block attributes in CADCraft")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CADCRAFT_CONTROL_PORT",
                                               DEFAULT_PORT)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--diag", action="store_true",
                    help="print diagnostics and exit")
    ap.add_argument("--list", action="store_true",
                    help="list the selected blocks and their attributes")
    ap.add_argument("--set", action="append", default=[], metavar="TAG=VALUE",
                    help="set an attribute on the selected block (repeatable)")
    ap.add_argument("--end-refedit", choices=["discard", "save"],
                    help="if an in-place block edit (REFEDIT) is open, end "
                         "it this way and carry on")
    args = ap.parse_args()

    try:
        if args.diag or args.list or args.set:
            cl = Client(args.port, args.token)
            if args.diag:
                diagnostics(cl)
                return 0
            if args.list:
                list_selected(cl)
                return 0
            sel = fetch_inserts(cl, selected=True)
            if len(sel) != 1:
                print("ERROR: select exactly one block (found %d)." % len(sel))
                return 1
            values = {}
            for item in args.set:
                if "=" not in item:
                    print("ERROR: use TAG=VALUE, got %r" % item)
                    return 1
                tag, value = item.split("=", 1)
                values[tag] = value
            try:
                set_attributes(cl, sel[0], values, print)
            except RefEditOpen:
                if not args.end_refedit:
                    print("ERROR: CADCraft has an in-place block edit "
                          "(REFEDIT) open. End it with REFCLOSE, or rerun "
                          "with --end-refedit discard (or save).")
                    return 1
                close_refedit(cl, args.end_refedit == "save", print)
                set_attributes(cl, sel[0], values, print)
            return 0
    except CadError as e:
        print("ERROR:", e)
        return 1
    return run_gui(args.port, args.token)


if __name__ == "__main__":
    sys.exit(main())
