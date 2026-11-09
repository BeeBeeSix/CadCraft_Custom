#!/usr/bin/env python3
"""
cadcraft_dimstyle.py - a "Modify Dimension Style" dialog for CADCraft.

Looks like AutoCAD's dialog (tabs: Lines, Symbols and Arrows, Text, Fit,
Primary Units, Alternate Units, Tolerances, with a live preview) but does its
work ONLY through SETVAR typed at the CADCraft command line, using the same
control channel as cadcraft_chprop.py.

How to use
  1. Start CADCraft with the control channel on:   cadcraft --control 7979
  2. Run:   python cadcraft_dimstyle.py            (or --port 7979)
  3. Change things, press OK / Apply. Only variables whose value actually
     changed are sent:   SETVAR <Enter> DIMASZ <Enter> 0.25 <Enter>

Other switches
  --diag      print what CADCraft returns for SETVAR listing, then exit
  --offline   open the dialog with the default values (no CADCraft needed),
              handy for looking at the layout

Notes
  * Edits the CURRENT dimension style's variables (the DIM* system variables).
    SETVAR cannot save/rename styles - use DIMSTYLE in CADCraft for that.
  * A space typed at the CADCraft command line acts as Enter, so values with
    spaces are refused.
  * Empty string variables are written as "." (AutoCAD convention for "none").
"""
import argparse
import glob
import json
import os
import re
import socket
import sys
import tempfile

DEFAULT_PORT = 7979

# ----------------------------------------------------------------------
# Defaults (taken from CadCraft_Variables.txt) - used by --offline and as
# fallback for any variable CADCraft does not report.
# ----------------------------------------------------------------------
DEFAULTS = {
    "DIMADEC": "2", "DIMALT": "false", "DIMALTD": "2", "DIMALTF": "25.4",
    "DIMALTRND": "0.0", "DIMALTU": "2", "DIMALTZ": "0", "DIMAPOST": "",
    "DIMARCSYM": "0", "DIMASZ": "0.18", "DIMATFIT": "3", "DIMAUNIT": "0",
    "DIMAZIN": "0", "DIMBLK": "", "DIMBLK1": "", "DIMBLK2": "",
    "DIMCEN": "0.09", "DIMCLRD": '{"kind":"byBlock"}',
    "DIMCLRE": '{"kind":"byBlock"}', "DIMCLRT": '{"kind":"byBlock"}',
    "DIMDEC": "4", "DIMDLE": "0.0", "DIMDLI": "0.38", "DIMDSEP": ".",
    "DIMEXE": "0.18", "DIMEXO": "0.0625", "DIMFRAC": "0", "DIMFXL": "1.0",
    "DIMFXLON": "false", "DIMGAP": "0.09", "DIMJOGANG": "45.0",
    "DIMJUST": "0", "DIMLDRBLK": "", "DIMLFAC": "1.0", "DIMLIM": "false",
    "DIMLTEX1": "", "DIMLTEX2": "", "DIMLTYPE": "", "DIMLUNIT": "2",
    "DIMLWD": "-2", "DIMLWE": "-2", "DIMPOST": "", "DIMRND": "0.0",
    "DIMSCALE": "1.0", "DIMSD1": "false", "DIMSD2": "false",
    "DIMSE1": "false", "DIMSE2": "false", "DIMSOXD": "false",
    "DIMSTYLE": "Standard", "DIMTAD": "0", "DIMTDEC": "4", "DIMTFAC": "1.0",
    "DIMTFILL": "0", "DIMTFILLCLR": '{"kind":"byBlock"}', "DIMTIH": "true",
    "DIMTIX": "false", "DIMTM": "0.0", "DIMTMOVE": "0", "DIMTOFL": "false",
    "DIMTOH": "true", "DIMTOL": "false", "DIMTOLJ": "1", "DIMTP": "0.0",
    "DIMTSZ": "0.0", "DIMTVP": "0.0", "DIMTXSTY": "Standard",
    "DIMTXT": "0.18", "DIMTZIN": "0", "DIMZIN": "0",
}


# ----------------------------------------------------------------------
# Control-channel client (same protocol as cadcraft_chprop.py)
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
             os.path.join(home, "Library", "Caches"), tempfile.gettempdir()]
    for d in dirs:
        for pattern in (os.path.join(d, name), os.path.join(d, "*", name),
                        os.path.join(d, "*", "*", name)):
            for path in glob.glob(pattern):
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


# ----------------------------------------------------------------------
# SETVAR read / write
# ----------------------------------------------------------------------
LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(.*?)\s*$")


def _out(r):
    return [str(x) for x in (r.get("output") or [])]


def read_vars(cl, names, log=lambda m: None):
    """Return {NAME: raw string}. Tries the SETVAR ? listing first, then
    falls back to asking for each missing variable one at a time."""
    vals = {}
    want = set(names)
    try:
        lines = []
        lines += _out(cl.type("SETVAR"))
        lines += _out(cl.type("?"))
        r = cl.type("DIM*")
        lines += _out(r)
        for _ in range(40):                       # paged listing
            st = cl.state()
            if not st.get("running"):
                break
            lines += _out(cl.type(""))
        if cl.state().get("running"):
            cl.escape()
        for ln in lines:
            m = LINE_RE.match(ln)
            if m and m.group(1).upper() in want:
                vals[m.group(1).upper()] = m.group(2)
    except CadError as e:
        log("Listing failed (%s); reading one by one." % e)
        cl.escape()
    missing = [n for n in names if n not in vals]
    if missing:
        log("Reading %d variable(s) individually..." % len(missing))
    for n in missing:
        try:
            r = cl.type("SETVAR")
            r = cl.type(n)
            text = " ".join(_out(r) + [str(cl.state().get("prompt") or "")])
            m = re.search(r"<([^>]*)>", text)
            if m:
                vals[n] = m.group(1).strip()
        except CadError:
            pass
        finally:
            if cl.state().get("running"):
                cl.escape()
    return vals


def write_var(cl, name, value, log):
    if " " in value:
        raise CadError("%s: value %r contains a space (acts as Enter at the "
                       "CADCraft command line)." % (name, value))
    cl.type("SETVAR")
    cl.type(name)
    r = cl.type(value)
    for line in _out(r):
        log("   | " + line)
    if cl.state().get("running"):
        cl.escape()
        raise CadError("SETVAR %s did not accept %r." % (name, value))
    log("SETVAR %s %s" % (name, value))


# ----------------------------------------------------------------------
# Codecs: raw CADCraft string  <->  value shown in the dialog
# ----------------------------------------------------------------------
def fmt(x):
    s = ("%.6f" % x).rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def real_codec():
    def dec(raw):
        try:
            return fmt(float(raw))
        except ValueError:
            return "0"
    return dec, lambda disp: fmt(float(disp))


def int_codec():
    def dec(raw):
        try:
            return str(int(float(raw)))
        except ValueError:
            return "0"
    return dec, lambda disp: str(int(float(disp)))


def bool_codec():
    return (lambda raw: "1" if raw.strip().lower() in ("1", "true") else "0",
            lambda disp: "1" if disp in ("1", "true") else "0")


def str_codec():
    return (lambda raw: raw.strip().strip('"'),
            lambda disp: disp.strip() if disp.strip() else ".")


def map_codec(mapping):
    """mapping: {int: label}. Display = label."""
    rev = {v: k for k, v in mapping.items()}

    def dec(raw):
        try:
            return mapping.get(int(float(raw)), str(raw))
        except ValueError:
            return str(raw)
    return dec, lambda disp: str(rev[disp]) if disp in rev else str(int(float(disp)))


COLOR_NAMES = {"Red": 1, "Yellow": 2, "Green": 3, "Cyan": 4, "Blue": 5,
               "Magenta": 6, "White": 7}
COLORS = ["ByBlock", "ByLayer"] + list(COLOR_NAMES)


def color_codec():
    def dec(raw):
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                d = json.loads(raw)
            except ValueError:
                return raw
            kind = str(d.get("kind", "")).lower()
            if kind == "byblock":
                return "ByBlock"
            if kind == "bylayer":
                return "ByLayer"
            for v in d.values():
                if isinstance(v, int) and not isinstance(v, bool):
                    for n, i in COLOR_NAMES.items():
                        if i == v:
                            return n
                    return str(v)
            return raw
        low = raw.lower()
        if low in ("byblock", "0"):
            return "ByBlock"
        if low in ("bylayer", "256"):
            return "ByLayer"
        for n, i in COLOR_NAMES.items():
            if low == n.lower() or low == str(i):
                return n
        return raw

    def enc(disp):
        d = disp.strip()
        if d.lower() in ("byblock", "bylayer"):
            return d.upper()
        if d in COLOR_NAMES:
            return str(COLOR_NAMES[d])
        return str(int(d))
    return dec, enc


LWEIGHTS = ["ByLayer", "ByBlock", "Default", "0.00", "0.05", "0.09", "0.13",
            "0.15", "0.18", "0.20", "0.25", "0.30", "0.35", "0.40", "0.50",
            "0.53", "0.60", "0.70", "0.80", "0.90", "1.00", "1.06", "1.20",
            "1.40", "1.58", "2.00", "2.11"]
LW_STD = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90,
          100, 106, 120, 140, 158, 200, 211]


def lw_codec():
    def dec(raw):
        try:
            v = int(float(raw))
        except ValueError:
            return "ByBlock"
        return {-1: "ByLayer", -2: "ByBlock", -3: "Default"}.get(
            v, "%.2f" % (v / 100.0))

    def enc(disp):
        d = disp.strip().lower()
        if d == "bylayer":
            return "-1"
        if d == "byblock":
            return "-2"
        if d == "default":
            return "-3"
        h = float(d) * 100
        return str(min(LW_STD, key=lambda s: abs(s - h)))
    return dec, enc


LTYPES = ["ByBlock", "ByLayer", "Continuous"]


def ltype_codec():
    def dec(raw):
        r = raw.strip().strip('"')
        return r if r else "Continuous"

    def enc(disp):
        d = disp.strip()
        return "." if d.lower() == "continuous" else d
    return dec, enc


ARROWS = [("Closed filled", ""), ("Closed blank", "_CLOSEDBLANK"),
          ("Open", "_OPEN"), ("Dot", "_DOT"), ("Dot small", "_DOTSMALL"),
          ("Dot blank", "_DOTBLANK"), ("Box filled", "_BOXFILLED"),
          ("Oblique", "_OBLIQUE"), ("Architectural tick", "_ARCHTICK"),
          ("None", "_NONE")]
ARROW_NAMES = [a for a, _ in ARROWS]
TICKS = ("Oblique", "Architectural tick")


def arrow_codec():
    to_name = {b.upper(): a for a, b in ARROWS}
    to_blk = {a: b for a, b in ARROWS}

    def dec(raw):
        r = raw.strip().strip('"').upper().lstrip("_")
        r = "_" + r if r else ""
        return to_name.get(r, raw.strip().strip('"') or "Closed filled")

    def enc(disp):
        b = to_blk.get(disp, disp)
        return b if b else "."
    return dec, enc


def dsep_codec():
    return (lambda raw: raw.strip().strip('"') or ".",
            lambda disp: disp if disp in (".", ",") else ".")


M_JUST = {0: "Centered", 1: "At ext line 1", 2: "At ext line 2",
          3: "Over ext line 1", 4: "Over ext line 2"}
M_TAD = {0: "Centered", 1: "Above", 2: "Outside", 3: "JIS", 4: "Below"}
M_TFILL = {0: "None", 1: "Background", 2: "Color"}
M_ARCSYM = {0: "Before dimension text", 1: "Above dimension text", 2: "None"}
M_LUNIT = {1: "Scientific", 2: "Decimal", 3: "Engineering",
           4: "Architectural", 5: "Fractional", 6: "Windows Desktop"}
M_FRAC = {0: "Horizontal", 1: "Diagonal", 2: "Not stacked"}
M_AUNIT = {0: "Decimal Degrees", 1: "Deg/Min/Sec", 2: "Grads", 3: "Radians"}
M_TOLJ = {0: "Bottom", 1: "Middle", 2: "Top"}
M_ATFIT = {0: "Both text and arrows", 1: "Arrows first, then text",
           2: "Text first, then arrows", 3: "Either (best fit)"}
M_TMOVE = {0: "Beside the dimension line", 1: "Over dim line, with leader",
           2: "Over dim line, no leader"}
METHODS = ["None", "Symmetrical", "Deviation", "Limits"]


# ----------------------------------------------------------------------
# The dialog
# ----------------------------------------------------------------------
def run_gui(cl):
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("Modify Dimension Style")
    root.resizable(False, False)

    V = {}        # variable name -> tk.StringVar (display value)
    CODEC = {}    # variable name -> (decode, encode)
    ORIG = {}     # variable name -> canonical encoded value when loaded
    PSEUDO = {}   # extras not stored 1:1 in a variable: name -> StringVar
    canvases = []
    state_hooks = []
    guard = {"busy": False}

    def reg(name, codec, default=None):
        if name not in V:
            V[name] = tk.StringVar(value="")
            CODEC[name] = codec
            V[name].trace_add("write", lambda *a: on_change())
        return V[name]

    def pseudo(name, value=""):
        PSEUDO[name] = tk.StringVar(value=value)
        PSEUDO[name].trace_add("write", lambda *a: on_change())
        return PSEUDO[name]

    def on_change():
        if guard["busy"]:
            return
        for h in state_hooks:
            h()
        redraw()

    # --- small widget helpers -------------------------------------------
    def label(p, r, text, c=0, **kw):
        ttk.Label(p, text=text).grid(row=r, column=c, sticky="w",
                                     padx=(6, 6), pady=3, **kw)

    def combo(p, r, text, name, values, codec, width=22, readonly=False,
              c=0):
        label(p, r, text, c)
        v = reg(name, codec)
        w = ttk.Combobox(p, textvariable=v, values=values, width=width,
                         state="readonly" if readonly else "normal")
        w.grid(row=r, column=c + 1, sticky="w", pady=3)
        return w

    def spin(p, r, text, name, step=0.01, lo=0.0, hi=100000.0, codec=None,
             c=0, width=10):
        label(p, r, text, c)
        v = reg(name, codec or real_codec())
        w = ttk.Spinbox(p, textvariable=v, from_=lo, to=hi, increment=step,
                        width=width)
        w.grid(row=r, column=c + 1, sticky="w", pady=3)
        return w

    def entry(p, r, text, name, codec, width=22, c=0):
        label(p, r, text, c)
        v = reg(name, codec)
        w = ttk.Entry(p, textvariable=v, width=width)
        w.grid(row=r, column=c + 1, sticky="w", pady=3)
        return w

    def check(p, r, text, name, c=0, span=2):
        v = reg(name, bool_codec())
        w = ttk.Checkbutton(p, text=text, variable=v, onvalue="1",
                            offvalue="0")
        w.grid(row=r, column=c, columnspan=span, sticky="w", padx=6, pady=2)
        return w

    def pcheck(p, r, text, pname, c=0, span=2):
        v = PSEUDO.get(pname) or pseudo(pname, "0")
        w = ttk.Checkbutton(p, text=text, variable=v, onvalue="1",
                            offvalue="0")
        w.grid(row=r, column=c, columnspan=span, sticky="w", padx=6, pady=2)
        return w

    def radio(p, r, text, var, value, span=2):
        w = ttk.Radiobutton(p, text=text, variable=var, value=value)
        w.grid(row=r, column=0, columnspan=span, sticky="w", padx=10, pady=1)
        return w

    def bitcheck(p, r, text, name, mask, c=0, span=2):
        """Checkbox that flips one bit of an integer variable."""
        iv = V[name]
        bv = tk.StringVar(value="0")

        def from_var(*_):
            try:
                bv.set("1" if int(float(iv.get() or 0)) & mask else "0")
            except ValueError:
                bv.set("0")

        def to_var(*_):
            try:
                cur = int(float(iv.get() or 0))
            except ValueError:
                cur = 0
            new = (cur | mask) if bv.get() == "1" else (cur & ~mask)
            if new != cur:
                iv.set(str(new))
        iv.trace_add("write", from_var)
        bv.trace_add("write", to_var)
        w = ttk.Checkbutton(p, text=text, variable=bv, onvalue="1",
                            offvalue="0")
        w.grid(row=r, column=c, columnspan=span, sticky="w", padx=6, pady=2)
        return w

    def group(parent, text, r, c=0, rs=1, cs=1, **kw):
        g = ttk.LabelFrame(parent, text=text)
        g.grid(row=r, column=c, rowspan=rs, columnspan=cs, sticky="nsew",
               padx=4, pady=4, **kw)
        return g

    def set_state(w, enabled):
        try:
            w.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    # --- notebook + preview per tab ---------------------------------------
    top = ttk.Frame(root, padding=8)
    top.grid()
    nb = ttk.Notebook(top)
    nb.grid(row=0, column=0, columnspan=4)
    title_var = tk.StringVar(value="")
    root.title("Modify Dimension Style")

    def add_tab(title, preview=True):
        f = ttk.Frame(nb, padding=4)
        nb.add(f, text=title)
        left = ttk.Frame(f)
        left.grid(row=0, column=0, sticky="n")
        if preview:
            c = tk.Canvas(f, width=290, height=270, bg="white",
                          relief="sunken", bd=1, highlightthickness=0)
            c.grid(row=0, column=1, sticky="n", padx=(8, 0), pady=6)
            canvases.append(c)
        return left

    # ================= Lines ================================================
    t = add_tab("Lines")
    g = group(t, "Dimension lines", 0)
    combo(g, 0, "Color:", "DIMCLRD", COLORS, color_codec(), 16)
    combo(g, 1, "Linetype:", "DIMLTYPE", LTYPES, ltype_codec(), 16)
    combo(g, 2, "Lineweight:", "DIMLWD", LWEIGHTS, lw_codec(), 16)
    w_dle = spin(g, 3, "Extend beyond ticks:", "DIMDLE", 0.01)
    spin(g, 4, "Baseline spacing:", "DIMDLI", 0.01)
    f = ttk.Frame(g)
    f.grid(row=5, column=0, columnspan=2, sticky="w")
    label(f, 0, "Suppress:")
    check(f, 0, "Dim line 1", "DIMSD1", c=1, span=1)
    check(f, 0, "Dim line 2", "DIMSD2", c=2, span=1)
    g = group(t, "Extension lines", 1)
    combo(g, 0, "Color:", "DIMCLRE", COLORS, color_codec(), 16)
    combo(g, 1, "Linetype ext line 1:", "DIMLTEX1", LTYPES, ltype_codec(), 16)
    combo(g, 2, "Linetype ext line 2:", "DIMLTEX2", LTYPES, ltype_codec(), 16)
    combo(g, 3, "Lineweight:", "DIMLWE", LWEIGHTS, lw_codec(), 16)
    f = ttk.Frame(g)
    f.grid(row=4, column=0, columnspan=2, sticky="w")
    label(f, 0, "Suppress:")
    check(f, 0, "Ext line 1", "DIMSE1", c=1, span=1)
    check(f, 0, "Ext line 2", "DIMSE2", c=2, span=1)
    spin(g, 5, "Extend beyond dim lines:", "DIMEXE", 0.01)
    spin(g, 6, "Offset from origin:", "DIMEXO", 0.01)
    check(g, 7, "Fixed length extension lines", "DIMFXLON")
    w_fxl = spin(g, 8, "Length:", "DIMFXL", 0.1)

    # ================= Symbols and Arrows ===================================
    t = add_tab("Symbols and Arrows")
    g = group(t, "Arrowheads", 0)
    combo(g, 0, "First:", "DIMBLK1", ARROW_NAMES, arrow_codec(), 20, True)
    combo(g, 1, "Second:", "DIMBLK2", ARROW_NAMES, arrow_codec(), 20, True)
    combo(g, 2, "Leader:", "DIMLDRBLK", ARROW_NAMES, arrow_codec(), 20, True)
    spin(g, 3, "Arrow size:", "DIMASZ", 0.01)
    g = group(t, "Center marks", 1)
    cm = pseudo("CENTER", "Mark")
    label(g, 0, "Type:")
    ttk.Combobox(g, textvariable=cm, values=["None", "Mark", "Line"],
                 state="readonly", width=10).grid(row=0, column=1, sticky="w")
    reg("DIMCEN", real_codec())
    w_cen = ttk.Spinbox(g, textvariable=V["DIMCEN"], from_=0, to=100000,
                        increment=0.01, width=10)
    label(g, 1, "Size:")
    w_cen.grid(row=1, column=1, sticky="w", pady=3)
    g = group(t, "Arc length symbol", 2)
    combo(g, 0, "Placement:", "DIMARCSYM", list(M_ARCSYM.values()),
          map_codec(M_ARCSYM), 22, True)
    g = group(t, "Radius dimension jog", 3)
    spin(g, 0, "Jog angle:", "DIMJOGANG", 1.0, 5, 90)

    # ================= Text ==================================================
    t = add_tab("Text")
    g = group(t, "Text appearance", 0)
    entry(g, 0, "Text style:", "DIMTXSTY", str_codec(), 18)
    combo(g, 1, "Text color:", "DIMCLRT", COLORS, color_codec(), 16)
    combo(g, 2, "Fill:", "DIMTFILL", list(M_TFILL.values()),
          map_codec(M_TFILL), 16, True)
    combo(g, 3, "Fill color:", "DIMTFILLCLR", COLORS, color_codec(), 16)
    spin(g, 4, "Text height:", "DIMTXT", 0.01)
    spin(g, 5, "Fraction height scale:", "DIMTFAC", 0.1)
    pseudo("FRAME", "0")
    pcheck(g, 6, "Draw frame around text", "FRAME")
    g = group(t, "Text placement", 1)
    combo(g, 0, "Vertical:", "DIMTAD", list(M_TAD.values()),
          map_codec(M_TAD), 16, True)
    combo(g, 1, "Horizontal:", "DIMJUST", list(M_JUST.values()),
          map_codec(M_JUST), 16, True)
    spin(g, 2, "Offset from dim line:", "DIMGAP", 0.01)
    g = group(t, "Text alignment", 2)
    reg("DIMTIH", bool_codec())
    reg("DIMTOH", bool_codec())
    align = pseudo("ALIGN", "Horizontal")
    radio(g, 0, "Horizontal", align, "Horizontal")
    radio(g, 1, "Align with dimension line", align, "Aligned")
    radio(g, 2, "ISO standard", align, "ISO")

    # ================= Fit ====================================================
    t = add_tab("Fit")
    g = group(t, "Fit options", 0)
    reg("DIMATFIT", int_codec())
    for i, (k, lab) in enumerate(M_ATFIT.items()):
        radio(g, i, lab, V["DIMATFIT"], str(k))
    g = group(t, "Text placement", 1)
    reg("DIMTMOVE", int_codec())
    for i, (k, lab) in enumerate(M_TMOVE.items()):
        radio(g, i, lab, V["DIMTMOVE"], str(k))
    g = group(t, "Scale for dimension features", 2)
    spin(g, 0, "Overall scale:", "DIMSCALE", 0.1, 0.0001)
    g = group(t, "Fine tuning", 3)
    check(g, 0, "Always keep text between ext lines", "DIMTIX")
    check(g, 1, "Suppress arrows if not enough room", "DIMSOXD")
    check(g, 2, "Draw dim line between ext lines", "DIMTOFL")

    # ================= Primary Units ==========================================
    t = add_tab("Primary Units")
    g = group(t, "Linear dimensions", 0)
    combo(g, 0, "Unit format:", "DIMLUNIT", list(M_LUNIT.values()),
          map_codec(M_LUNIT), 18, True)
    spin(g, 1, "Precision (decimals):", "DIMDEC", 1, 0, 8, int_codec())
    combo(g, 2, "Fraction format:", "DIMFRAC", list(M_FRAC.values()),
          map_codec(M_FRAC), 18, True)
    combo(g, 3, "Decimal separator:", "DIMDSEP", [".", ","], dsep_codec(),
          6, True)
    spin(g, 4, "Round off:", "DIMRND", 0.01)
    entry(g, 5, "Prefix/suffix (<>=value):", "DIMPOST", str_codec(), 14)
    spin(g, 6, "Scale factor:", "DIMLFAC", 0.1)
    reg("DIMZIN", int_codec())
    bitcheck(g, 7, "Suppress leading zeros", "DIMZIN", 4)
    bitcheck(g, 8, "Suppress trailing zeros", "DIMZIN", 8)
    g = group(t, "Angular dimensions", 1)
    combo(g, 0, "Units format:", "DIMAUNIT", list(M_AUNIT.values()),
          map_codec(M_AUNIT), 18, True)
    spin(g, 1, "Precision (decimals):", "DIMADEC", 1, 0, 8, int_codec())
    reg("DIMAZIN", int_codec())
    bitcheck(g, 2, "Suppress leading zeros", "DIMAZIN", 1)
    bitcheck(g, 3, "Suppress trailing zeros", "DIMAZIN", 2)

    # ================= Alternate Units ========================================
    t = add_tab("Alternate Units")
    g = group(t, "Alternate units", 0)
    check(g, 0, "Display alternate units", "DIMALT")
    combo(g, 1, "Unit format:", "DIMALTU", list(M_LUNIT.values()),
          map_codec(M_LUNIT), 18, True)
    spin(g, 2, "Precision (decimals):", "DIMALTD", 1, 0, 8, int_codec())
    spin(g, 3, "Multiplier:", "DIMALTF", 0.1)
    spin(g, 4, "Round distances to:", "DIMALTRND", 0.01)
    entry(g, 5, "Prefix/suffix:", "DIMAPOST", str_codec(), 14)
    reg("DIMALTZ", int_codec())
    bitcheck(g, 6, "Suppress leading zeros", "DIMALTZ", 4)
    bitcheck(g, 7, "Suppress trailing zeros", "DIMALTZ", 8)

    # ================= Tolerances ==============================================
    t = add_tab("Tolerances")
    g = group(t, "Tolerance format", 0)
    meth = pseudo("METHOD", "None")
    label(g, 0, "Method:")
    ttk.Combobox(g, textvariable=meth, values=METHODS, state="readonly",
                 width=16).grid(row=0, column=1, sticky="w")
    spin(g, 1, "Upper value:", "DIMTP", 0.01)
    spin(g, 2, "Lower value:", "DIMTM", 0.01)
    spin(g, 3, "Scaling for height:", "DIMTFAC", 0.1)
    combo(g, 4, "Vertical position:", "DIMTOLJ", list(M_TOLJ.values()),
          map_codec(M_TOLJ), 16, True)
    spin(g, 5, "Precision (decimals):", "DIMTDEC", 1, 0, 8, int_codec())
    reg("DIMTZIN", int_codec())
    bitcheck(g, 6, "Suppress leading zeros", "DIMTZIN", 4)
    bitcheck(g, 7, "Suppress trailing zeros", "DIMTZIN", 8)
    reg("DIMTOL", bool_codec())
    reg("DIMLIM", bool_codec())
    reg("DIMTSZ", real_codec())
    reg("DIMTVP", real_codec())
    reg("DIMBLK", arrow_codec())
    reg("DIMSTYLE", str_codec())

    # --- cross-field logic -------------------------------------------------
    def num(name, default=0.0):
        try:
            return float(V[name].get())
        except (KeyError, ValueError):
            return default

    def hook_states():
        ticks = (V["DIMBLK1"].get() in TICKS or V["DIMBLK2"].get() in TICKS)
        set_state(w_dle, ticks)
        set_state(w_fxl, V["DIMFXLON"].get() == "1")
        set_state(w_cen, cm.get() != "None")
    state_hooks.append(hook_states)

    def center_sync(*_):
        if guard["busy"]:
            return
        size = abs(num("DIMCEN", 0.09)) or 0.09
        guard["busy"] = True
        V["DIMCEN"].set(fmt(size))
        guard["busy"] = False
        on_change()
    cm.trace_add("write", center_sync)

    # --- preview ------------------------------------------------------------
    ACI = {"Red": "#e00000", "Yellow": "#c8a000", "Green": "#00a000",
           "Cyan": "#00a0a0", "Blue": "#0000e0", "Magenta": "#c000c0",
           "White": "#999999"}

    def col(name):
        return ACI.get(V[name].get(), "black") if name in V else "black"

    def lw_px(name):
        d = V[name].get()
        try:
            return max(1, min(4, round(float(d) / 0.35)))
        except ValueError:
            return 1

    def dash(name):
        d = V[name].get().lower()
        return () if d in ("continuous", "byblock", "bylayer", "") else (4, 3)

    def label_text(val):
        unit = {v: k for k, v in M_LUNIT.items()}.get(V["DIMLUNIT"].get(), 2)
        val = val * (num("DIMLFAC", 1.0) or 1.0)
        dec = max(0, min(8, int(num("DIMDEC", 4))))
        if unit == 4:
            ft, inch = divmod(round(val * 8) / 8.0, 12)
            s = "%d'-%s\"" % (ft, fmt(inch))
        elif unit == 5:
            s = "%s" % fmt(val)
        elif unit == 1:
            s = ("%.*E" % (dec, val))
        else:
            s = "%.*f" % (dec, val)
        zin = int(num("DIMZIN", 0))
        if unit in (2, 6, 1) and "." in s and "E" not in s:
            if zin & 8:
                s = s.rstrip("0").rstrip(".")
            if zin & 4 and s.startswith("0."):
                s = s[1:]
        s = s.replace(".", V["DIMDSEP"].get() or ".") if unit in (2, 6) else s
        post = V["DIMPOST"].get()
        if post:
            s = post.replace("<>", s) if "<>" in post else s + post
        if V["DIMALT"].get() == "1":
            alt = val * num("DIMALTF", 25.4)
            s += " [%.*f]" % (max(0, min(8, int(num("DIMALTD", 2)))), alt)
        m = meth.get()
        tp, tm = fmt(num("DIMTP")), fmt(num("DIMTM"))
        if m == "Symmetrical":
            s += " \u00b1%s" % tp
        elif m == "Deviation":
            s += " +%s/-%s" % (tp, tm)
        elif m == "Limits":
            s = "%s / %s" % (fmt(val + num("DIMTP")), fmt(val - num("DIMTM")))
        return s

    def draw_arrow(c, T, lx, direction, style, size, color):
        """Arrow tip at local (lx, 0) pointing along -direction."""
        if style == "None" or size <= 0:
            return
        hw = size / 3.0
        tip = T(lx, 0)
        base = lx + direction * size
        if style in TICKS:
            c.create_line(*T(lx - size / 2, -size / 2),
                          *T(lx + size / 2, size / 2), fill=color, width=2)
        elif style.startswith("Dot") or style.startswith("Box"):
            r = size / 2.2
            x, y = T(lx + direction * r, 0)
            if style.startswith("Box"):
                c.create_rectangle(x - r, y - r, x + r, y + r, fill=color,
                                   outline=color)
            else:
                c.create_oval(x - r, y - r, x + r, y + r, outline=color,
                              fill="" if style == "Dot blank" else color)
        elif style == "Open":
            c.create_line(*T(base, hw), *tip, *T(base, -hw), fill=color)
        else:
            fill = "" if style == "Closed blank" else color
            c.create_polygon(*tip, *T(base, hw), *T(base, -hw), fill=fill,
                             outline=color)

    def draw_dim(c, T, L, off, text, vertical):
        k = 13.0 / (num("DIMTXT", 0.18) or 0.18)
        th = 13
        asz = num("DIMASZ", 0.18) * k
        exo, exe = num("DIMEXO") * k, num("DIMEXE") * k
        gap = abs(num("DIMGAP")) * k
        a1, a2 = V["DIMBLK1"].get(), V["DIMBLK2"].get()
        ticks = a1 in TICKS or a2 in TICKS
        dcol, ecol, tcol = col("DIMCLRD"), col("DIMCLRE"), col("DIMCLRT")
        # extension lines
        for i, x in enumerate((0, L)):
            if V["DIMSE%d" % (i + 1)].get() == "1":
                continue
            y0 = exo
            if V["DIMFXLON"].get() == "1":
                y0 = max(exo, off - num("DIMFXL", 1.0) * k)
            c.create_line(*T(x, y0), *T(x, off + exe), fill=ecol,
                          width=lw_px("DIMLWE"),
                          dash=dash("DIMLTEX%d" % (i + 1)))
        # text
        tad = {v: k_ for k_, v in M_TAD.items()}.get(V["DIMTAD"].get(), 0)
        just = {v: k_ for k_, v in M_JUST.items()}.get(V["DIMJUST"].get(), 0)
        tw = max(10, len(text) * th * 0.58)
        mid = L / 2.0
        tx = mid
        if just in (1, 3):
            tx = asz * 2 + tw / 2
        elif just in (2, 4):
            tx = L - asz * 2 - tw / 2
        ty = off
        if tad in (1, 2, 3):
            ty = off + gap + th / 2 + 2
        elif tad == 4:
            ty = off - gap - th / 2 - 2
        # dimension line (broken around text when centered)
        ext = num("DIMDLE") * k if ticks else 0
        cut = (tad == 0)
        segs = [(0, mid), (mid, L)]
        if cut and tw + 2 * asz < L:
            h = tw / 2 + gap
            segs = [(0, tx - h), (tx + h, L)]
        if V["DIMSD1"].get() == "1":
            segs[0] = None
        if V["DIMSD2"].get() == "1":
            segs[1] = None
        for i, s in enumerate(segs):
            if s:
                a, b = s
                if i == 0:
                    a -= ext
                else:
                    b += ext
                c.create_line(*T(a, off), *T(b, off), fill=dcol,
                              width=lw_px("DIMLWD"),
                              dash=dash("DIMLTYPE"))
        a1s = a1 if a1 else "Closed filled"
        draw_arrow(c, lambda x, y: T(x, off + y), 0, 1, a1, asz, dcol)
        draw_arrow(c, lambda x, y: T(x, off + y), L, -1, a2, asz, dcol)
        # text box
        cx, cy = T(tx, ty)
        ang = 90 if (vertical and V["DIMTIH"].get() != "1" and
                     PSEUDO["ALIGN"].get() != "Horizontal") else 0
        if vertical and PSEUDO["ALIGN"].get() != "Horizontal":
            ang = 90
        w, h = (th, tw) if ang == 90 else (tw, th)
        fillmode = V["DIMTFILL"].get()
        if fillmode != "None":
            c.create_rectangle(cx - w / 2, cy - h / 2, cx + w / 2,
                               cy + h / 2,
                               fill="#e6e6e6" if fillmode == "Background"
                               else col("DIMTFILLCLR") if col("DIMTFILLCLR")
                               != "black" else "#e6e6e6", outline="")
        if PSEUDO["FRAME"].get() == "1":
            c.create_rectangle(cx - w / 2 - 2, cy - h / 2 - 2,
                               cx + w / 2 + 2, cy + h / 2 + 2, outline=tcol)
        c.create_text(cx, cy, text=text, fill=tcol, angle=ang,
                      font=("Helvetica", -th))

    def draw_center(c, cx, cy, r):
        k = 13.0 / (num("DIMTXT", 0.18) or 0.18)
        s = abs(num("DIMCEN")) * k
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#b8860b")
        mode = cm.get()
        if mode == "None" or s <= 0:
            return
        c.create_line(cx - s, cy, cx + s, cy, fill="black")
        c.create_line(cx, cy - s, cx, cy + s, fill="black")
        if mode == "Line":
            c.create_line(cx - r - s, cy, cx + r + s, cy, fill="black")
            c.create_line(cx, cy - r - s, cx, cy + r + s, fill="black")

    def redraw():
        for c in canvases:
            c.delete("all")
            try:
                x0, y0, x1, y1 = 100, 105, 262, 215
                c.create_rectangle(x0, y0, x1, y1, outline="#b8860b", width=2)
                draw_center(c, 180, 168, 16)
                H = x1 - x0
                draw_dim(c, lambda lx, ly: (x0 + lx, y0 - ly), H, 42,
                         label_text(4.25), False)
                draw_dim(c, lambda lx, ly: (x0 - ly, y1 - lx), y1 - y0, 40,
                         label_text(5.0), True)
            except (tk.TclError, ValueError, KeyError):
                pass

    # --- load / apply ----------------------------------------------------------
    status = tk.StringVar(value="")

    def log(msg):
        status.set(msg[:110])
        root.update_idletasks()

    def load(raw):
        guard["busy"] = True
        try:
            for name, (dec, enc) in CODEC.items():
                r = raw.get(name, DEFAULTS.get(name, ""))
                V[name].set(dec(r))
            # derived values
            g_ = V["DIMGAP"].get()
            try:
                neg = float(g_) < 0
            except ValueError:
                neg = False
            PSEUDO["FRAME"].set("1" if neg else "0")
            V["DIMGAP"].set(fmt(abs(num("DIMGAP"))))
            tih, toh = V["DIMTIH"].get(), V["DIMTOH"].get()
            PSEUDO["ALIGN"].set("Horizontal" if (tih == "1" and toh == "1")
                                else "ISO" if toh == "1" else "Aligned")
            cen = num("DIMCEN")
            cm.set("None" if cen == 0 else "Line" if cen < 0 else "Mark")
            V["DIMCEN"].set(fmt(abs(cen)))
            if V["DIMLIM"].get() == "1":
                meth.set("Limits")
            elif V["DIMTOL"].get() == "1":
                meth.set("Symmetrical" if num("DIMTP") == num("DIMTM")
                         else "Deviation")
            else:
                meth.set("None")
            if num("DIMTSZ") > 0:
                for n in ("DIMBLK1", "DIMBLK2"):
                    V[n].set("Architectural tick")
            else:
                blk = V["DIMBLK"].get()
                for n in ("DIMBLK1", "DIMBLK2"):
                    if not raw.get(n, DEFAULTS.get(n, "")).strip():
                        V[n].set(blk)
        finally:
            guard["busy"] = False
        title = V["DIMSTYLE"].get() or "Standard"
        root.title("Modify Dimension Style: %s" % title)
        desired(ORIG)          # remember canonical values as loaded
        on_change()

    def desired(out):
        """Fill `out` with the canonical SETVAR value for every variable."""
        out.clear()
        for name, (dec, enc) in CODEC.items():
            try:
                out[name] = enc(V[name].get())
            except (ValueError, KeyError):
                raise CadError("Invalid value for %s: %r"
                               % (name, V[name].get()))
        # --- pseudo fields back into variables
        gap = abs(float(out["DIMGAP"]))
        out["DIMGAP"] = fmt(-gap if PSEUDO["FRAME"].get() == "1" else gap)
        a = PSEUDO["ALIGN"].get()
        out["DIMTIH"], out["DIMTOH"] = {"Horizontal": ("1", "1"),
                                        "ISO": ("0", "1"),
                                        "Aligned": ("0", "0")}[a]
        cen = abs(float(out["DIMCEN"])) or 0.09
        out["DIMCEN"] = {"None": "0", "Mark": fmt(cen),
                         "Line": fmt(-cen)}[cm.get()]
        m = meth.get()
        out["DIMLIM"] = "1" if m == "Limits" else "0"
        out["DIMTOL"] = "1" if m in ("Symmetrical", "Deviation") else "0"
        if m == "Symmetrical":
            out["DIMTM"] = out["DIMTP"]
        # --- arrowheads / ticks
        b1, b2 = V["DIMBLK1"].get(), V["DIMBLK2"].get()
        if b1 in TICKS or b2 in TICKS:
            out["DIMTSZ"] = fmt(float(out["DIMASZ"])) if float(
                out["DIMTSZ"]) <= 0 else out["DIMTSZ"]
            out["DIMBLK1"] = ORIG.get("DIMBLK1", out["DIMBLK1"]) \
                if ORIG else out["DIMBLK1"]
            out["DIMBLK2"] = ORIG.get("DIMBLK2", out["DIMBLK2"]) \
                if ORIG else out["DIMBLK2"]
        else:
            out["DIMTSZ"] = "0"
            if b1 == b2:
                out["DIMBLK"] = CODEC["DIMBLK1"][1](b1)
        return out

    def apply():
        try:
            new = desired({})
        except CadError as e:
            messagebox.showerror("Dimension Style", str(e))
            return False
        changes = [(n, v) for n, v in new.items()
                   if n != "DIMSTYLE" and ORIG.get(n) != v]
        if not changes:
            log("Nothing changed.")
            return True
        try:
            for n, v in changes:
                write_var(cl, n, v, log)
            log("%d variable(s) changed." % len(changes))
            load(read_vars(cl, list(CODEC), log))
            return True
        except CadError as e:
            cl.escape()
            messagebox.showerror("Dimension Style", str(e))
            return False

    def ok():
        if apply():
            root.destroy()

    def reload_():
        try:
            load(read_vars(cl, list(CODEC), log))
            log("Reloaded from CADCraft.")
        except CadError as e:
            messagebox.showerror("Dimension Style", str(e))

    bar = ttk.Frame(top)
    bar.grid(row=1, column=0, columnspan=4, sticky="e", pady=(8, 0))
    ttk.Label(top, textvariable=status, foreground="#555").grid(
        row=2, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 0))
    ttk.Button(bar, text="Reload", command=reload_).grid(row=0, column=0,
                                                         padx=3)
    ttk.Button(bar, text="Apply", command=apply).grid(row=0, column=1,
                                                      padx=3)
    ttk.Button(bar, text="OK", command=ok).grid(row=0, column=2, padx=3)
    ttk.Button(bar, text="Cancel", command=root.destroy).grid(row=0, column=3,
                                                              padx=3)
    root.bind("<Escape>", lambda e: root.destroy())

    if cl is None:
        load({})
        log("Offline preview - nothing is sent to CADCraft.")
    else:
        reload_()
    root.mainloop()
    return 0


# ----------------------------------------------------------------------
# Diagnostics / main
# ----------------------------------------------------------------------
def diagnostics(cl):
    print("== ping:", cl.call("ping"))
    print("== cmdline.state:", json.dumps(cl.state())[:300])
    vals = read_vars(cl, list(DEFAULTS), print)
    print("== read %d of %d DIM* variables" % (len(vals), len(DEFAULTS)))
    for n in sorted(vals):
        print("   %-12s %s" % (n, vals[n]))
    miss = [n for n in DEFAULTS if n not in vals]
    if miss:
        print("== not reported:", ", ".join(miss))


def main():
    ap = argparse.ArgumentParser(description="Dimension style editor for "
                                             "CADCraft (SETVAR based)")
    ap.add_argument("--port", type=int, default=int(
        os.environ.get("CADCRAFT_CONTROL_PORT", DEFAULT_PORT)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    if a.offline:
        return run_gui(None)
    try:
        cl = Client(a.port, a.token)
    except CadError as e:
        if a.diag:
            print("ERROR:", e)
        else:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("CADCraft Dimension Style", str(e))
        return 1
    try:
        if a.diag:
            diagnostics(cl)
            return 0
        return run_gui(cl)
    finally:
        cl.close()


if __name__ == "__main__":
    sys.exit(main())
