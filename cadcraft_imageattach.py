#!/usr/bin/env python3
"""
cadcraft_imageattach.py - small GUI that attaches a raster image to the
current CADCraft drawing through the CADCraft control channel (same channel
and token as cadcraft_dimstyle.py).

Usage
  1. cadcraft --control 7979
  2. python cadcraft_imageattach.py            (or --port 7979)

Switches
  --offline   open the dialog without CADCraft (layout check)

How it works
  imageattach takes structured params, not command-line prompts (the command
  line cannot carry them: a space acts as Enter). Attach calls the control
  method `command` with
    {"id": "imageattach", "params": {"path": ..., "at": [x, y],
                                     "rotation": deg, "scale": width}}
  Scale is the image width in drawing units (default 1).
"""
import argparse
import glob
import json
import os
import socket
import sys
import tempfile

DEFAULT_PORT = 7979


class CadError(Exception):
    pass


def find_token(port):
    env = os.environ.get("CADCRAFT_TOKEN")
    if env:
        return env.strip()
    name = "cadcraft-control-%s.token" % port
    dirs = [os.environ[v] for v in ("XDG_RUNTIME_DIR", "LOCALAPPDATA")
            if os.environ.get(v)]
    home = os.path.expanduser("~")
    dirs += [os.path.join(home, ".cache"),
             os.path.join(home, "Library", "Caches"), tempfile.gettempdir()]
    for d in dirs:
        for pat in (os.path.join(d, name), os.path.join(d, "*", name),
                    os.path.join(d, "*", "*", name)):
            for path in glob.glob(pat):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read().strip()
                except OSError:
                    pass
    return None


class Client:
    def __init__(self, port=DEFAULT_PORT, token=None, host="127.0.0.1",
                 timeout=65):
        self.token = token if token is not None else find_token(port)
        if self.token is None:
            raise CadError("Could not find cadcraft-control-%s.token. Is "
                           "CADCraft running with --control %s? (Or set "
                           "CADCRAFT_TOKEN / --token.)" % (port, port))
        try:
            self.sock = socket.create_connection((host, port), timeout=5)
        except OSError as e:
            raise CadError("Cannot connect to CADCraft on port %s: %s"
                           % (port, e))
        self.sock.settimeout(timeout)
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self._id = 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def call(self, method, params=None):
        self._id += 1
        req = {"id": self._id, "method": method, "params": params or {},
               "token": self.token}
        self.sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        line = self.rfile.readline()
        if not line:
            raise CadError("CADCraft closed the connection.")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise CadError(str(reply.get("error", "unknown error")))
        return reply.get("result")

    def type(self, text):
        r = self.call("cmdline.input", {"text": text}) or {}
        if r.get("error"):
            raise CadError("CADCraft refused %r: %s" % (text, r["error"]))
        return r

    def state(self):
        return self.call("cmdline.state") or {}

    def escape(self):
        try:
            self.call("cmdline.key", {"key": "escape"})
        except CadError:
            pass


def attach(cl, path, x, y, scale, rotation, log):
    """Run `imageattach` through the control channel's `command` method.
    Params (from CADCraft's image_attach tests): path, at=[x,y], rotation,
    scale (= width in drawing units)."""
    try:
        params = {"path": path, "at": [float(x), float(y)],
                  "rotation": float(rotation), "scale": float(scale)}
    except ValueError:
        raise CadError("X, Y, scale and rotation must be numbers.")
    r = cl.call("command", {"id": "imageattach", "params": params}) or {}
    log("attached: %s  %s px  size %s  handle %s" % (
        r.get("name"), r.get("pixels"), r.get("size"), r.get("handle")))


def run_gui(cl):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("Attach Image - CADCraft")
    root.resizable(False, False)
    f = ttk.Frame(root, padding=10)
    f.grid()

    path = tk.StringVar()
    x, y = tk.StringVar(value="0"), tk.StringVar(value="0")
    scale, rot = tk.StringVar(value="1"), tk.StringVar(value="0")

    ttk.Label(f, text="Image:").grid(row=0, column=0, sticky="w", pady=3)
    ttk.Entry(f, textvariable=path, width=48).grid(row=0, column=1,
                                                   columnspan=3, pady=3)

    def browse():
        p = filedialog.askopenfilename(
            title="Select image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff "
                                  "*.gif"), ("All files", "*.*")])
        if p:
            path.set(p.replace("\\", "/"))
    ttk.Button(f, text="Browse...", command=browse).grid(row=0, column=4,
                                                         padx=(6, 0))

    ttk.Label(f, text="Insertion X:").grid(row=1, column=0, sticky="w")
    ttk.Entry(f, textvariable=x, width=12).grid(row=1, column=1, sticky="w")
    ttk.Label(f, text="Y:").grid(row=1, column=2, sticky="e")
    ttk.Entry(f, textvariable=y, width=12).grid(row=1, column=3, sticky="w")
    ttk.Label(f, text="Scale:").grid(row=2, column=0, sticky="w", pady=3)
    ttk.Entry(f, textvariable=scale, width=12).grid(row=2, column=1,
                                                    sticky="w")
    ttk.Label(f, text="Rotation:").grid(row=2, column=2, sticky="e")
    ttk.Entry(f, textvariable=rot, width=12).grid(row=2, column=3, sticky="w")

    log_box = tk.Text(f, width=64, height=9, state="disabled",
                      bg="#f6f6f6")
    log_box.grid(row=3, column=0, columnspan=5, pady=(8, 4))

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    def do_attach():
        if cl is None:
            log("Offline - nothing sent.")
            return
        p = path.get().strip()
        if not p or not os.path.isfile(p):
            messagebox.showerror("Attach Image", "Pick an existing image "
                                                 "file.")
            return
        try:
            float(x.get()), float(y.get()), float(scale.get()), float(
                rot.get())
        except ValueError:
            messagebox.showerror("Attach Image", "X, Y, scale and rotation "
                                                 "must be numbers.")
            return
        try:
            attach(cl, p, x.get().strip(), y.get().strip(),
                   scale.get().strip(), rot.get().strip(), log)
        except CadError as e:
            log("ERROR: " + str(e))
            messagebox.showerror("Attach Image", str(e))

    bar = ttk.Frame(f)
    bar.grid(row=4, column=0, columnspan=5, sticky="e")
    ttk.Button(bar, text="Attach", command=do_attach).grid(row=0, column=0,
                                                           padx=3)
    ttk.Button(bar, text="Close", command=root.destroy).grid(row=0, column=1,
                                                             padx=3)
    log("Offline preview." if cl is None else "Connected.")
    root.mainloop()
    return 0


def main():
    ap = argparse.ArgumentParser(description="IMAGEATTACH GUI for CADCraft")
    ap.add_argument("--port", type=int, default=int(
        os.environ.get("CADCRAFT_CONTROL_PORT", DEFAULT_PORT)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    if a.offline:
        return run_gui(None)
    try:
        cl = Client(a.port, a.token)
    except CadError as e:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("CADCraft Attach Image", str(e))
        return 1
    try:
        return run_gui(cl)
    finally:
        cl.close()


if __name__ == "__main__":
    sys.exit(main())
