#!/usr/bin/env python3
"""
ctb_viewer.py - read and view AutoCAD plot style tables (.ctb / .stb).

  python ctb_viewer.py [file.ctb]

Features
  * opens colour-dependent (.ctb) and named (.stb) plot style tables,
    compressed or plain-text variants
  * table of every plot style: colour (with swatch), screening, linetype,
    lineweight (mm, looked up in the file's own lineweight table), fill, end
    and join styles, pen numbers
  * filter box (wildcards) and "only styles that differ from the file's most
    common settings" - quickly shows which styles are actually customised
  * detail panel for the selected style, lineweight table, raw text view
  * export to CSV
  * "Send to CADCraft...": creates the table in CADCraft's Plot Style
    Manager (CADCraft must be running with --control 7979)
    --probe  prints what CADCraft returns for its plot style manager

Read-only: nothing is ever written back to the .ctb.
Standard library only (tkinter, zlib).
"""
import colorsys
import csv
import fnmatch
import glob
import json
import os
import re
import socket
import sys
import tempfile
import zlib
from collections import Counter

DEFAULT_PORT = 7979


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
class CtbError(Exception):
    pass


def read_text(path):
    """Return (header_line, decompressed_text) of a CTB/STB file."""
    with open(path, "rb") as f:
        data = f.read()
    nl = data.find(b"\n")
    if not data.startswith(b"PIAFILEVERSION") or nl < 0:
        raise CtbError("Not a plot style table (missing PIAFILEVERSION "
                       "header).")
    header = data[:nl].decode("latin-1").strip()
    marker = b"pmzlibcodec"
    k = data.find(marker)
    if k >= 0:
        start = k + len(marker)
        # normally: 4 bytes checksum, 4 bytes size, 4 bytes compressed size
        for off in [start + 12] + list(range(start, start + 64)):
            try:
                raw = zlib.decompressobj().decompress(data[off:])
            except zlib.error:
                continue
            if raw:
                return header, raw.decode("latin-1")
        raise CtbError("Could not find the compressed data in the file.")
    return header, data[nl + 1:].decode("latin-1")


def _unq(v):
    """CTB strings open with a quote but are often not closed."""
    v = v.strip()
    if v.startswith('"'):
        v = v[1:]
    if v.endswith('"'):
        v = v[:-1]
    return v


def _num(v):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def _block(text, name):
    """Text between 'name{' and its matching '}' (one level of nesting
    tolerated by counting braces)."""
    m = re.search(r"(?m)^%s\{" % re.escape(name), text)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[m.end():i - 1]


def parse(path):
    header, text = read_text(path)
    info = {}
    head_part = text.split("aci_table{")[0].split("plot_style{")[0]
    for line in head_part.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = _unq(v)
    aci = {}
    blk = _block(text, "aci_table")
    if blk:
        for line in blk.splitlines():
            m = re.match(r"\s*(\d+)=(.*)", line)
            if m:
                aci[int(m.group(1))] = _unq(m.group(2))
    styles = []
    blk = _block(text, "plot_style") or ""
    for m in re.finditer(r"(?m)^ (\d+)\{\n(.*?)\n \}", blk, re.S):
        d = {"index": int(m.group(1))}
        for line in m.group(2).splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                d[k] = _unq(v) if k in ("name", "localized_name",
                                        "description") else _num(v.strip())
        styles.append(d)
    lw = []
    blk = _block(text, "custom_lineweight_table")
    if blk:
        for line in blk.splitlines():
            m = re.match(r"\s*(\d+)=(.*)", line)
            if m:
                lw.append(float(m.group(2)))
    if not styles:
        raise CtbError("No plot styles found in this file.")
    return {"path": path, "header": header, "info": info, "aci": aci,
            "styles": styles, "lineweights": lw, "text": text}


# ----------------------------------------------------------------------
# Decoding of values
# ----------------------------------------------------------------------
def aci_rgb(i):
    """AutoCAD Colour Index -> (r, g, b)."""
    fixed = {1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0),
             4: (0, 255, 255), 5: (0, 0, 255), 6: (255, 0, 255),
             7: (255, 255, 255), 8: (65, 65, 65), 9: (128, 128, 128)}
    if i in fixed:
        return fixed[i]
    if 250 <= i <= 255:
        g = (51, 91, 132, 173, 214, 255)[i - 250]
        return (g, g, g)
    if 10 <= i <= 249:
        hue = ((i - 10) // 10) * 15 / 360.0
        pat = [(1, 1), (.5, 1), (1, .65), (.5, .65), (1, .5), (.5, .5),
               (1, .3), (.5, .3), (1, .15), (.5, .15)][(i - 10) % 10]
        r, g, b = colorsys.hsv_to_rgb(hue, pat[0], pat[1])
        return (round(r * 255), round(g * 255), round(b * 255))
    return (0, 0, 0)


def decode_color(v):
    """-> (label, (r,g,b) or None for 'use object colour')."""
    if not isinstance(v, int):
        return str(v), None
    u = v & 0xFFFFFFFF
    if u == 0xC3FFFFFF or u == 0xFFFFFFFF:
        return "Use object color", None
    top = u >> 24
    if top == 0xC3:
        rgb = ((u >> 16) & 255, (u >> 8) & 255, u & 255)
        return "#%02X%02X%02X" % rgb, rgb
    if top == 0xC2:
        rgb = aci_rgb(u & 255)
        return "ACI %d" % (u & 255), rgb
    return "0x%08X" % u, None


LINETYPES = {0: "Solid", 1: "Dashed", 2: "Dotted", 3: "Dash Dot",
             31: "Use object linetype"}
FILLS = {64: "Solid", 65: "Checkerboard", 66: "Crosshatch", 67: "Diamonds",
         68: "Horizontal Bars", 69: "Slant Left", 70: "Slant Right",
         71: "Square Dots", 72: "Vertical Bars", 73: "Use object fill style"}
ENDS = {0: "Butt", 1: "Square", 2: "Round", 3: "Diamond",
        4: "Use object end style"}
JOINS = {0: "Miter", 1: "Bevel", 2: "Round", 3: "Diamond",
         5: "Use object join style"}


def lookup(table, v, label="Value"):
    return table.get(v, "%s %s" % (label, v))


def lineweight_text(ctb, idx):
    lw = ctb["lineweights"]
    if isinstance(idx, int) and 0 <= idx < len(lw):
        return "%.3f" % lw[idx] if lw[idx] else "0.000"
    return "Use object lineweight" if idx in (31, None) else str(idx)


COLUMNS = [  # key, heading, width, anchor
    ("index", "#", 44, "e"), ("name", "Name", 110, "w"),
    ("color", "Color", 120, "w"), ("screen", "Screen %", 70, "e"),
    ("linetype", "Linetype", 140, "w"), ("lineweight", "Lineweight mm", 110,
                                         "e"),
    ("fill", "Fill", 140, "w"), ("end", "End", 160, "w"),
    ("join", "Join", 160, "w"), ("pen", "Pen", 40, "e"),
    ("vpen", "Virt. pen", 60, "e"), ("adaptive", "Adaptive", 65, "w"),
    ("description", "Description", 160, "w"),
]


def row_of(ctb, s):
    """Display values for one style."""
    label, rgb = decode_color(s.get("color"))
    mlabel, _ = decode_color(s.get("mode_color", s.get("color")))
    return {
        "index": s["index"] + (1 if ctb["info"].get(
            "aci_table_available", "").upper() == "TRUE" else 0),
        "name": s.get("name", ""),
        "color": label,
        "screen": s.get("screen", ""),
        "linetype": lookup(LINETYPES, s.get("linetype"), "Pattern"),
        "lineweight": lineweight_text(ctb, s.get("lineweight")),
        "fill": lookup(FILLS, s.get("fill_style"), "Fill"),
        "end": lookup(ENDS, s.get("end_style"), "End"),
        "join": lookup(JOINS, s.get("join_style"), "Join"),
        "pen": s.get("physical_pen_number", ""),
        "vpen": s.get("virtual_pen_number", ""),
        "adaptive": s.get("adaptive_linetype", ""),
        "description": s.get("description", ""),
    }


def plot_rgb(ctb, s):
    """Colour the style will plot in (object colour -> the ACI colour)."""
    label, rgb = decode_color(s.get("color"))
    if rgb is not None:
        return rgb
    if ctb["info"].get("aci_table_available", "").upper() == "TRUE":
        return aci_rgb(s["index"] + 1)
    return None


DIFF_KEYS = ("color", "screen", "linetype", "lineweight", "fill", "end",
             "join", "pen", "vpen", "adaptive")


def differing_styles(ctb):
    """Indexes of styles that differ from the most common settings."""
    rows = [row_of(ctb, s) for s in ctb["styles"]]
    # 'Use object colour' is the default colour - compare by label
    common = {k: Counter(str(r[k]) for r in rows).most_common(1)[0][0]
              for k in DIFF_KEYS}
    return {i for i, r in enumerate(rows)
            if any(str(r[k]) != common[k] for k in DIFF_KEYS)}, common


# ----------------------------------------------------------------------
# Send to CADCraft (control channel + its `stylesmanager` command)
# ----------------------------------------------------------------------
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
                 timeout=60):
        self.token = token if token is not None else find_token(port)
        if self.token is None:
            raise CadError("Could not find the CADCraft session token "
                           "(cadcraft-control-%s.token). Is CADCraft running "
                           "with --control %s?" % (port, port))
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

    def execute(self, command, params=None):
        r = self.call("engine.execute",
                      {"command": command, "params": params or {}})
        if isinstance(r, dict) and r.get("error"):
            raise CadError("%s: %s" % (command, r["error"]))
        return r


# CTB numbers -> the names CADCraft's stylesmanager is likely to accept.
# (colour / screening / linetype / lineweight are documented; end, join and
#  fill value spellings are a best guess and are dropped automatically if
#  CADCraft rejects them.)
END_NAMES = {0: "butt", 1: "square", 2: "round", 3: "diamond"}
JOIN_NAMES = {0: "miter", 1: "bevel", 2: "round", 3: "diamond"}
FILL_NAMES = {64: "solid", 65: "checkerboard", 66: "crosshatch",
              67: "diamonds", 68: "horizontalBars", 69: "slantLeft",
              70: "slantRight", 71: "squareDots", 72: "verticalBars"}
LT_NAMES = {0: "Solid", 1: "Dashed", 2: "Dotted", 3: "Dash Dot"}


def style_params(ctb, s, with_extras=True):
    """One plot style -> the `style` dict for stylesmanager action=set."""
    label, rgb = decode_color(s.get("color"))
    p = {"name": s.get("name", ""),
         "color": ("#%02X%02X%02X" % rgb) if rgb else None,
         "grayscale": bool(int(s.get("color_policy", 0) or 0) & 2),
         "screening": int(s.get("screen", 100)),
         "linetype": LT_NAMES.get(s.get("linetype")),
         "description": s.get("description", "")}
    lw = s.get("lineweight")
    table = ctb["lineweights"]
    p["lineweight"] = (float(table[lw]) if isinstance(lw, int)
                       and 0 <= lw < len(table) else None)
    if with_extras:
        if s.get("end_style") in END_NAMES:
            p["end"] = END_NAMES[s["end_style"]]
        if s.get("join_style") in JOIN_NAMES:
            p["join"] = JOIN_NAMES[s["join_style"]]
        if s.get("fill_style") in FILL_NAMES:
            p["fill"] = FILL_NAMES[s["fill_style"]]
    return p


def table_kind(ctb):
    return ("colorDependent" if ctb["info"].get(
        "aci_table_available", "").upper() == "TRUE" else "named")


def send_table(cl, ctb, tablename, indexes, log, progress=lambda i, n: None):
    """Create (or update) a plot style table in CADCraft."""
    exists = False
    try:
        r = cl.execute("stylesmanager", {"action": "list"})
        exists = tablename.lower() in json.dumps(r).lower()
    except CadError as e:
        log("could not list existing tables (%s)" % e)
    if exists:
        log("Table %s already exists - updating its styles." % tablename)
    else:
        p = {"action": "new", "name": tablename, "kind": table_kind(ctb)}
        if ctb["info"].get("description"):
            p["description"] = ctb["info"]["description"]
        cl.execute("stylesmanager", p)
        log("Created table %s (%s)." % (tablename, p["kind"]))
    extras = True
    fails = 0
    n = len(indexes)
    for k, i in enumerate(indexes):
        s = ctb["styles"][i]
        params = {"action": "set", "name": tablename,
                  "style": style_params(ctb, s, extras)}
        try:
            cl.execute("stylesmanager", params)
        except CadError as e:
            if extras:
                log("end/join/fill not accepted (%s) - sending without them."
                    % e)
                extras = False
                params["style"] = style_params(ctb, s, False)
                try:
                    cl.execute("stylesmanager", params)
                except CadError as e2:
                    fails += 1
                    log("  %s: FAILED - %s" % (s.get("name"), e2))
            else:
                fails += 1
                log("  %s: FAILED - %s" % (s.get("name"), e))
            if fails >= 5:
                raise CadError("Stopped after repeated failures - last "
                               "error: %s" % e)
        progress(k + 1, n)
    log("Sent %d style(s)%s." % (n - fails,
                                  "" if extras else " (without end/join/fill)"))
    return n - fails, not extras


def probe(cl):
    """Print what CADCraft returns for the plot style manager (diagnostic)."""
    r = cl.execute("stylesmanager", {"action": "list"})
    print("list ->", json.dumps(r)[:1500])
    try:
        names = re.findall(r'[\w &.\-]+\.(?:ctb|stb)', json.dumps(r),
                           re.I)
        if names:
            g = cl.execute("stylesmanager", {"action": "get",
                                             "name": names[0]})
            print("get %s ->" % names[0], json.dumps(g)[:2500])
    except CadError as e:
        print("get failed:", e)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
def run_gui(start=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("CTB Viewer")
    root.geometry("1180x700")
    state = {"ctb": None, "diff": set(), "common": {}, "sort": (None, False)}
    swatches = {}

    top = ttk.Frame(root, padding=(8, 8, 8, 0))
    top.pack(fill="x")
    ttk.Button(top, text="Open...", command=lambda: open_file()).pack(
        side="left")
    ttk.Button(top, text="Export CSV...", command=lambda: export()).pack(
        side="left", padx=6)
    ttk.Button(top, text="Send to CADCraft...",
               command=lambda: send_dialog()).pack(side="left", padx=6)
    path_var = tk.StringVar()
    ttk.Label(top, textvariable=path_var).pack(side="left", padx=10)

    info_var = tk.StringVar()
    ttk.Label(root, textvariable=info_var, padding=(10, 4),
              foreground="#444").pack(fill="x")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=4)

    # --- styles tab
    tab = ttk.Frame(nb)
    nb.add(tab, text="Plot styles")
    bar = ttk.Frame(tab)
    bar.pack(fill="x", pady=4)
    ttk.Label(bar, text="Filter:").pack(side="left")
    filt = tk.StringVar()
    ttk.Entry(bar, textvariable=filt, width=24).pack(side="left", padx=4)
    ttk.Label(bar, text="(name / colour / linetype... wildcards * ?)",
              foreground="#666").pack(side="left")
    diff_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(bar, text="Only styles that differ from the most "
                    "common settings", variable=diff_var,
                    command=lambda: fill()).pack(side="left", padx=14)
    count_var = tk.StringVar()
    ttk.Label(bar, textvariable=count_var).pack(side="right")

    body = ttk.PanedWindow(tab, orient="vertical")
    body.pack(fill="both", expand=True)
    tf = ttk.Frame(body)
    body.add(tf, weight=4)
    tree = ttk.Treeview(tf, columns=[c[0] for c in COLUMNS],
                        show="tree headings", selectmode="browse")
    tree.column("#0", width=34, stretch=False)
    tree.heading("#0", text="")
    for key, head, w, anc in COLUMNS:
        tree.heading(key, text=head, command=lambda k=key: sort_by(k))
        tree.column(key, width=w, anchor=anc, stretch=False)
    vs = ttk.Scrollbar(tf, command=tree.yview)
    hs = ttk.Scrollbar(tf, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vs.grid(row=0, column=1, sticky="ns")
    hs.grid(row=1, column=0, sticky="ew")
    tf.rowconfigure(0, weight=1)
    tf.columnconfigure(0, weight=1)
    tree.tag_configure("odd", background="#f4f6fa")

    detail = ttk.LabelFrame(body, text="Selected style")
    body.add(detail, weight=1)
    sw = tk.Canvas(detail, width=70, height=70, highlightthickness=1,
                   highlightbackground="#888")
    sw.grid(row=0, column=0, rowspan=4, padx=10, pady=8, sticky="n")
    det_text = tk.Text(detail, height=9, width=100, relief="flat",
                       background=root.cget("background"),
                       font=("Consolas", 10))
    det_text.grid(row=0, column=1, rowspan=4, sticky="nsew")
    detail.columnconfigure(1, weight=1)

    # --- lineweights tab
    lw_tab = ttk.Frame(nb)
    nb.add(lw_tab, text="Lineweight table")
    lw_tree = ttk.Treeview(lw_tab, columns=("i", "mm", "bar"),
                           show="headings", height=20)
    for k, h, w in (("i", "Index", 70), ("mm", "mm", 90), ("bar", "", 300)):
        lw_tree.heading(k, text=h)
        lw_tree.column(k, width=w, anchor="w")
    lw_tree.pack(fill="both", expand=True, padx=4, pady=4)

    # --- raw tab
    raw_tab = ttk.Frame(nb)
    nb.add(raw_tab, text="Raw text")
    raw = tk.Text(raw_tab, wrap="none", font=("Consolas", 9))
    rs = ttk.Scrollbar(raw_tab, command=raw.yview)
    raw.configure(yscrollcommand=rs.set)
    rs.pack(side="right", fill="y")
    raw.pack(fill="both", expand=True)

    # ---- helpers ---------------------------------------------------------
    def swatch(rgb):
        key = rgb or "none"
        if key not in swatches:
            img = tk.PhotoImage(width=22, height=12)
            if rgb is None:
                img.put("#ffffff", to=(0, 0, 22, 12))
                for x in range(22):
                    img.put("#cc3333", to=(x, x * 12 // 22,
                                           x + 1, x * 12 // 22 + 1))
            else:
                img.put("#%02x%02x%02x" % rgb, to=(0, 0, 22, 12))
            img.put("#666666", to=(0, 0, 22, 1))
            img.put("#666666", to=(0, 11, 22, 12))
            img.put("#666666", to=(0, 0, 1, 12))
            img.put("#666666", to=(21, 0, 22, 12))
            swatches[key] = img
        return swatches[key]

    def visible_indexes():
        ctb = state["ctb"]
        out = []
        pat = filt.get().strip().lower()
        if pat and not any(c in pat for c in "*?["):
            pat = "*" + pat + "*"
        for i, s in enumerate(ctb["styles"]):
            if diff_var.get() and i not in state["diff"]:
                continue
            if pat:
                r = row_of(ctb, s)
                hay = " ".join(str(v) for v in r.values()).lower()
                if not fnmatch.fnmatch(hay, pat):
                    continue
            out.append(i)
        key, rev = state["sort"]
        if key:
            def k(i):
                v = row_of(ctb, ctb["styles"][i])[key]
                try:
                    return (0, float(v))
                except (TypeError, ValueError):
                    return (1, str(v).lower())
            out.sort(key=k, reverse=rev)
        return out

    def fill(*_):
        ctb = state["ctb"]
        tree.delete(*tree.get_children())
        if not ctb:
            return
        idxs = visible_indexes()
        for n, i in enumerate(idxs):
            s = ctb["styles"][i]
            r = row_of(ctb, s)
            tree.insert("", "end", iid=str(i), image=swatch(plot_rgb(ctb, s)),
                        values=[r[c[0]] for c in COLUMNS],
                        tags=("odd",) if n % 2 else ())
        count_var.set("%d of %d styles" % (len(idxs), len(ctb["styles"])))

    def sort_by(key):
        k, rev = state["sort"]
        state["sort"] = (key, (not rev) if k == key else False)
        fill()

    def show_detail(*_):
        ctb = state["ctb"]
        sel = tree.selection()
        sw.delete("all")
        det_text.configure(state="normal")
        det_text.delete("1.0", "end")
        if not ctb or not sel:
            det_text.configure(state="disabled")
            return
        s = ctb["styles"][int(sel[0])]
        rgb = plot_rgb(ctb, s)
        if rgb:
            sw.create_rectangle(2, 2, 70, 70, fill="#%02x%02x%02x" % rgb,
                                outline="")
        else:
            sw.create_line(2, 2, 70, 70, fill="#cc3333", width=2)
        r = row_of(ctb, s)
        lab, _ = decode_color(s.get("color"))
        mlab, _ = decode_color(s.get("mode_color"))
        lines = [
            "Name         : %s" % r["name"],
            "Description  : %s" % r["description"],
            "Color        : %s   (mode color: %s, policy %s)"
            % (lab, mlab, s.get("color_policy")),
            "Screening    : %s %%" % r["screen"],
            "Linetype     : %s   (pattern size %s, adaptive %s)"
            % (r["linetype"], s.get("linepattern_size"), r["adaptive"]),
            "Lineweight   : %s mm   (table index %s)"
            % (r["lineweight"], s.get("lineweight")),
            "Fill / End / Join : %s / %s / %s"
            % (r["fill"], r["end"], r["join"]),
            "Pen          : physical %s, virtual %s"
            % (r["pen"], r["vpen"]),
        ]
        det_text.insert("1.0", "\n".join(lines))
        det_text.configure(state="disabled")

    tree.bind("<<TreeviewSelect>>", show_detail)
    filt.trace_add("write", fill)

    def load(path):
        try:
            ctb = parse(path)
        except (CtbError, OSError) as e:
            messagebox.showerror("CTB Viewer", "%s\n\n%s" % (path, e))
            return
        state["ctb"] = ctb
        state["diff"], state["common"] = differing_styles(ctb)
        path_var.set(path)
        root.title("CTB Viewer - %s" % os.path.basename(path))
        i = ctb["info"]
        info_var.set(
            "%s   |   %d plot styles   |   description: %s   |   scale "
            "factor: %s (apply: %s)   |   ACI table: %s   |   "
            "%d lineweights"
            % (ctb["header"].split(",")[1] if "," in ctb["header"] else "",
               len(ctb["styles"]), i.get("description") or "(none)",
               i.get("scale_factor", "?"), i.get("apply_factor", "?"),
               i.get("aci_table_available", "?"), len(ctb["lineweights"])))
        fill()
        lw_tree.delete(*lw_tree.get_children())
        for n, v in enumerate(ctb["lineweights"]):
            lw_tree.insert("", "end", values=(n, "%.3f" % v,
                                              "\u2588" * int(v * 12 + 1)))
        raw.configure(state="normal")
        raw.delete("1.0", "end")
        raw.insert("1.0", ctb["text"])
        raw.configure(state="disabled")
        kids = tree.get_children()
        if kids:
            tree.selection_set(kids[0])

    def open_file():
        p = filedialog.askopenfilename(
            title="Open plot style table",
            filetypes=[("Plot style tables", "*.ctb *.stb"),
                       ("All files", "*.*")])
        if p:
            load(p)

    def export():
        ctb = state["ctb"]
        if not ctb:
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=os.path.splitext(os.path.basename(ctb["path"]))[0]
            + ".csv")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([c[1] for c in COLUMNS])
            for i in visible_indexes():
                r = row_of(ctb, ctb["styles"][i])
                w.writerow([r[c[0]] for c in COLUMNS])
        messagebox.showinfo("CTB Viewer", "Saved %s" % p)

    def send_dialog():
        ctb = state["ctb"]
        if not ctb:
            messagebox.showinfo("CTB Viewer", "Open a plot style table first.")
            return
        dlg = tk.Toplevel(root)
        dlg.title("Send to CADCraft")
        dlg.transient(root)
        dlg.resizable(False, False)
        f = ttk.Frame(dlg, padding=12)
        f.pack()
        ext = ".ctb" if table_kind(ctb) == "colorDependent" else ".stb"
        base = os.path.splitext(os.path.basename(ctb["path"]))[0]
        name_var = tk.StringVar(value=base + ext)
        ttk.Label(f, text="Plot style table name in CADCraft:").grid(
            row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=name_var, width=40).grid(
            row=1, column=0, sticky="ew", pady=(2, 8))
        shown_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Send only the styles currently shown in "
                        "the list", variable=shown_var).grid(
            row=2, column=0, sticky="w")
        msg = tk.StringVar(value="Needs CADCraft running with --control.")
        ttk.Label(f, textvariable=msg, foreground="#444",
                  wraplength=380).grid(row=3, column=0, sticky="w",
                                       pady=(8, 4))
        logbox = tk.Text(f, height=9, width=56, state="disabled")
        logbox.grid(row=4, column=0, pady=4)

        def log(m):
            logbox.configure(state="normal")
            logbox.insert("end", m + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
            dlg.update_idletasks()

        def go():
            name = name_var.get().strip()
            if not re.search(r"\.(ctb|stb)$", name, re.I):
                name += ext
            idx = (visible_indexes() if shown_var.get()
                   else list(range(len(ctb["styles"]))))
            go_btn.state(["disabled"])
            cl = None
            try:
                cl = Client(int(os.environ.get("CADCRAFT_CONTROL_PORT",
                                               DEFAULT_PORT)))
                ok, dropped = send_table(
                    cl, ctb, name, idx, log,
                    lambda k, n: msg.set("Sending %d / %d ..." % (k, n)))
                msg.set("Done - open File > Plot Styles in CADCraft "
                        "(reopen the manager if it was already open).")
            except CadError as e:
                msg.set("Failed: %s" % e)
                log("ERROR: %s" % e)
            finally:
                if cl:
                    cl.close()
                go_btn.state(["!disabled"])

        bar = ttk.Frame(f)
        bar.grid(row=5, column=0, sticky="e")
        go_btn = ttk.Button(bar, text="Send", command=go)
        go_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Close", command=dlg.destroy).pack(side="left")

    if start:
        root.after(50, lambda: load(start))
    root.mainloop()


def main():
    if "--probe" in sys.argv:
        cl = Client(DEFAULT_PORT)
        try:
            probe(cl)
        finally:
            cl.close()
        return 0
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else None
    if path and "--dump" in sys.argv:
        ctb = parse(path)
        for s in ctb["styles"]:
            print(row_of(ctb, s))
        return 0
    run_gui(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
