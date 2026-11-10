#!/usr/bin/env python3
"""
cadcraft_steal.py - a "Steal" tool for CADCraft (after Lee Mac's Steal.lsp).

Pick a source drawing, tick the layers / linetypes / text styles / dimension
styles / blocks you want, and they are imported into the drawing that is open
in CADCraft.

How it works
  * Python reads the SOURCE drawing itself (library: ezdxf), so no ActiveX,
    ObjectDBX or DCL is needed.
  * For the items you choose it writes a plain LISP file made only of
    (entmake ...) forms, and loads it in CADCraft through the control
    channel - the same way cadcraft_chprop.py loads CHPROP.lsp.
  * Items that already exist in CADCraft are skipped (checked with tblsearch
    inside CADCraft). Things a block needs (layers, linetypes, text styles,
    nested blocks) are imported with it automatically.

Setup:   pip install ezdxf
Use:     1. cadcraft --control 7979
         2. python cadcraft_steal.py            (or --port 7979)
Source files: DXF.  DWG works only if the ODA File Converter is installed
              (ezdxf finds it); otherwise save the source as DXF first.

Switches
  --offline     open the dialog without CADCraft; Steal writes
                steal_import.lsp next to this script instead of loading it.
  --diag        print what CADCraft returns for the current drawing's tables.
"""
import argparse
import fnmatch
import glob
import json
import os
import socket
import sys
import tempfile

DEFAULT_PORT = 7979
CONFIG = os.path.join(os.path.expanduser("~"), ".cadcraft_steal.json")


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
                 timeout=120):
        self.token = token if token is not None else find_token(port)
        if self.token is None:
            raise CadError("Could not find the session token file "
                           "cadcraft-control-%s.token. Is CADCraft running "
                           "with --control %s? (Or set CADCRAFT_TOKEN / "
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

    def execute(self, command, params=None):
        """Run a CADCraft command with named parameters (engine.execute)."""
        r = self.call("engine.execute",
                      {"command": command, "params": params or {}})
        if isinstance(r, dict) and r.get("error"):
            raise CadError("%s: %s" % (command, r["error"]))
        return r

    def type(self, text):
        r = self.call("cmdline.input", {"text": text}) or {}
        if r.get("error"):
            raise CadError("CADCraft refused %r: %s" % (text, r["error"]))
        return r


def find_collection(obj, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys and isinstance(v, (list, dict)):
                return v
        for v in obj.values():
            f = find_collection(v, keys)
            if f is not None:
                return f
    elif isinstance(obj, list):
        for v in obj:
            f = find_collection(v, keys)
            if f is not None:
                return f
    return None


def names_from(coll):
    out = []
    if isinstance(coll, dict):
        out = [str(k) for k in coll.keys()]
    elif isinstance(coll, list):
        for item in coll:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                for key in ("name", "Name", "id", "layer"):
                    if isinstance(item.get(key), str):
                        out.append(item[key])
                        break
    return out


CATEGORIES = [
    ("Layers", "LAYER", {"layers"}),
    ("Linetypes", "LTYPE", {"linetypes", "ltypes", "linetype_styles"}),
    ("Text Styles", "STYLE", {"textstyles", "text_styles", "styles"}),
    ("Dimension Styles", "DIMSTYLE", {"dimstyles", "dim_styles"}),
    ("Blocks", "BLOCK", {"blocks", "block_definitions"}),
]


def current_names(cl):
    """Names already in CADCraft per category (lower case). Best effort:
    an empty set just means 'unknown' - the LISP skips existing items too."""
    res = {c[0]: set() for c in CATEGORIES}
    if cl is None:
        return res
    try:
        insp = cl.call("drawing.inspect", {"entities": False})
    except CadError:
        return res
    for cat, _tbl, keys in CATEGORIES:
        coll = find_collection(insp, keys)
        res[cat] = {n.lower() for n in names_from(coll or [])}
    return res


# ----------------------------------------------------------------------
# Source drawing
# ----------------------------------------------------------------------
BUILTIN_LTYPES = {"continuous", "bylayer", "byblock"}
STRIP = {5, 105, 330, 360, 102, 390, 347, 348}
DIMSTYLE_STRIP = {340, 341, 342, 343, 344, 345}
POINT_BASES = set(range(10, 19)) | {110, 111, 112, 210}
# entity types that can be rebuilt safely with entmake
OK_TYPES = {"LINE", "CIRCLE", "ARC", "ELLIPSE", "LWPOLYLINE", "POLYLINE",
            "VERTEX", "SEQEND", "TEXT", "MTEXT", "POINT", "INSERT", "ATTDEF",
            "ATTRIB", "SOLID", "TRACE", "SPLINE", "HATCH", "XLINE", "RAY",
            "3DFACE", "LEADER"}


def load_source(path):
    try:
        import ezdxf
    except ImportError:
        raise CadError("The 'ezdxf' package is needed:  pip install ezdxf")
    if path.lower().endswith(".dwg"):
        try:
            from ezdxf.addons import odafc
            return odafc.readfile(path)
        except Exception as e:
            raise CadError("Cannot read DWG (%s). Install the ODA File "
                           "Converter or save the drawing as DXF." % e)
    try:
        from ezdxf import recover
        doc, _aud = recover.readfile(path)
        return doc
    except Exception as e:
        raise CadError("Cannot read %s: %s" % (path, e))


class Source:
    def __init__(self, path):
        self.path = path
        self.doc = load_source(path)
        d = self.doc
        self.layers = {l.dxf.name: l for l in d.layers if l.dxf.name != "0"}
        self.ltypes = {l.dxf.name: l for l in d.linetypes
                       if l.dxf.name.lower() not in BUILTIN_LTYPES}
        self.styles = {s.dxf.name: s for s in d.styles}
        # dimension styles containing "$" (xref-bound copies) are ignored
        self.dimstyles = {s.dxf.name: s for s in d.dimstyles
                          if "$" not in s.dxf.name}
        self.blocks = {b.name: b for b in d.blocks
                       if not b.name.startswith("*")
                       and not (b.block.dxf.flags & 4)}   # skip xref blocks

    def names(self, cat):
        d = {"Layers": self.layers, "Linetypes": self.ltypes,
             "Text Styles": self.styles, "Dimension Styles": self.dimstyles,
             "Blocks": self.blocks}[cat]
        return sorted(d, key=str.lower)

    # ---- tags ---------------------------------------------------------
    def tags(self, entity, strip=()):
        from ezdxf.lldxf.tagwriter import TagCollector
        c = TagCollector(dxfversion=self.doc.dxfversion)
        entity.export_dxf(c)
        out = []
        raw = [(t.code, t.value) for t in c.tags]
        i = 0
        while i < len(raw):
            code, val = raw[i]
            if code in STRIP or code in strip:
                i += 1
                continue
            if code in POINT_BASES:
                pt = [val]
                j = i + 1
                for step in (10, 20):
                    if j < len(raw) and raw[j][0] == code + step:
                        pt.append(raw[j][1])
                        j += 1
                    else:
                        break
                out.append((code, pt))
                i = j
                continue
            out.append((code, val))
            i += 1
        return out

    def records(self, entity):
        """Split an entity's tags into one list per DXF record (a POLYLINE
        yields POLYLINE, VERTEX..., SEQEND)."""
        recs, cur = [], []
        for code, val in self.tags(entity):
            if code == 0 and cur:
                recs.append(cur)
                cur = []
            cur.append((code, val))
        if cur:
            recs.append(cur)
        return recs

    # ---- dependencies -----------------------------------------------------
    def block_deps(self, name, seen=None):
        """(layers, ltypes, styles, blocks) a block needs, recursively."""
        seen = seen if seen is not None else set()
        lay, lt, st, bl = set(), set(), set(), set()
        if name in seen or name not in self.blocks:
            return lay, lt, st, bl
        seen.add(name)
        for e in self.blocks[name]:
            t = e.dxftype()
            if e.dxf.hasattr("layer"):
                lay.add(e.dxf.layer)
            if e.dxf.hasattr("linetype"):
                lt.add(e.dxf.linetype)
            if e.dxf.hasattr("style") and t in ("TEXT", "MTEXT", "ATTDEF",
                                                "ATTRIB"):
                st.add(e.dxf.style)
            if t == "INSERT":
                n = e.dxf.name
                bl.add(n)
                a, b, c, d = self.block_deps(n, seen)
                lay |= a
                lt |= b
                st |= c
                bl |= d
        return lay, lt, st, bl


# ----------------------------------------------------------------------
# LISP generation
# ----------------------------------------------------------------------
def lstr(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def lnum(x):
    if isinstance(x, bool):
        return str(int(x))
    if isinstance(x, int):
        return str(x)
    s = repr(float(x))
    if "e" in s or "E" in s:
        m, e = s.lower().split("e")
        if "." not in m:
            m += ".0"
        return "%se%s" % (m, e)
    return s if "." in s else s + ".0"


def lval(code, val):
    if isinstance(val, list):
        return "(%d %s)" % (code, " ".join(lnum(v) for v in val))
    if isinstance(val, str):
        return "(%d . %s)" % (code, lstr(val))
    return "(%d . %s)" % (code, lnum(val))


def lrec(rec):
    return "(" + " ".join(lval(c, v) for c, v in rec) + ")"


def resolve(src, wanted):
    """Add everything the chosen items depend on. Returns (want, notes)."""
    want = {c[0]: set(wanted.get(c[0], ())) for c in CATEGORIES}
    for b in list(want["Blocks"]):
        lay, lt, st, bl = src.block_deps(b)
        want["Layers"] |= {n for n in lay if n in src.layers}
        want["Linetypes"] |= {n for n in lt if n in src.ltypes}
        want["Text Styles"] |= {n for n in st if n in src.styles}
        want["Blocks"] |= {n for n in bl if n in src.blocks}
    for n in list(want["Layers"]):
        lt = src.layers[n].dxf.get("linetype", "Continuous")
        if lt in src.ltypes:
            want["Linetypes"].add(lt)
    for n in list(want["Dimension Styles"]):
        ts = src.dimstyles[n].dxf.get("dimtxsty")
        if ts in src.styles:
            want["Text Styles"].add(ts)
    return want


LISP_HEAD = ['(defun st:res (tag name msg)',
             '  (prompt (strcat "\\n" tag " " name ": " msg)))']


def build_lisp(src, want, kinds=("Linetypes", "Blocks")):
    """LISP (entmake) for linetypes and blocks.
    Returns (text, item count, warnings)."""
    warn = []
    out = [";;; generated by cadcraft_steal.py"] + LISP_HEAD
    total = 0

    def table_item(tbl, name, entity, after=""):
        rec = src.tags(entity)
        return ('(if (tblsearch %s %s) (st:res %s %s "exists - skipped")\n'
                '  (st:res %s %s (if (entmake (quote %s)) (progn %s "imported") '
                '"FAILED")))'
                % (lstr(tbl), lstr(name), lstr(tbl), lstr(name),
                   lstr(tbl), lstr(name), lrec(rec), after))

    if "Linetypes" in kinds:
        for name in sorted(want["Linetypes"], key=str.lower):
            out.append(table_item("LTYPE", name, src.ltypes[name]))
            total += 1
    if "Blocks" in kinds:
        order, seen = [], set()

        def visit(n):
            if n in seen or n not in src.blocks:
                return
            seen.add(n)
            for e in src.blocks[n]:
                if e.dxftype() == "INSERT" and e.dxf.name in want["Blocks"]:
                    visit(e.dxf.name)
            order.append(n)
        for n in sorted(want["Blocks"], key=str.lower):
            visit(n)
        for n in order:
            blk = src.blocks[n]
            recs = []
            for e in blk:
                if e.dxftype() not in OK_TYPES:
                    warn.append("Block %s: skipped unsupported %s"
                                % (n, e.dxftype()))
                    continue
                recs.extend(src.records(e))
            head = src.tags(blk.block)
            body = "\n    ".join(lrec(r) for r in recs)
            out.append(
                '(if (tblsearch "BLOCK" %s) (st:res "BLOCK" %s "exists - skipped")\n'
                '  (if (entmake (quote %s))\n'
                '    (progn\n'
                '      (setq st:bad 0)\n'
                '      (foreach e (quote (\n    %s))\n'
                '        (if (not (entmake e)) (setq st:bad (1+ st:bad))))\n'
                '      (entmake (quote ((0 . "ENDBLK"))))\n'
                '      (st:res "BLOCK" %s\n'
                '        (if (= st:bad 0) "imported"\n'
                '          (strcat "imported, " (itoa st:bad) " object(s) failed"))))\n'
                '    (st:res "BLOCK" %s "FAILED")))'
                % (lstr(n), lstr(n), lrec(head), body, lstr(n), lstr(n)))
            total += 1
    out.append("(princ)")
    return "\n".join(out) + "\n", total, warn


# system variables CADCraft reports (from CadCraft_Variables.txt)
DIM_VARS = """DIMADEC DIMALT DIMALTD DIMALTF DIMALTRND DIMALTU DIMALTZ DIMAPOST
DIMARCSYM DIMASZ DIMATFIT DIMAUNIT DIMAZIN DIMBLK DIMBLK1 DIMBLK2 DIMCEN
DIMCLRD DIMCLRE DIMCLRT DIMDEC DIMDLE DIMDLI DIMDSEP DIMEXE DIMEXO DIMFRAC
DIMFXL DIMFXLON DIMGAP DIMJOGANG DIMJUST DIMLDRBLK DIMLFAC DIMLIM DIMLTEX1
DIMLTEX2 DIMLTYPE DIMLUNIT DIMLWD DIMLWE DIMPOST DIMRND DIMSCALE DIMSD1
DIMSD2 DIMSE1 DIMSE2 DIMSOXD DIMTAD DIMTDEC DIMTFAC DIMTFILL DIMTFILLCLR
DIMTIH DIMTIX DIMTM DIMTMOVE DIMTOFL DIMTOH DIMTOL DIMTOLJ DIMTP DIMTSZ
DIMTVP DIMTXSTY DIMTXT DIMTZIN DIMZIN""".split()
DIM_DEFAULT = {"DIMADEC": 2, "DIMALT": 0, "DIMALTD": 2, "DIMALTF": 25.4,
               "DIMALTRND": 0.0, "DIMALTU": 2, "DIMALTZ": 0, "DIMAPOST": "",
               "DIMARCSYM": 0, "DIMASZ": 0.18, "DIMATFIT": 3, "DIMAUNIT": 0,
               "DIMAZIN": 0, "DIMBLK": "", "DIMBLK1": "", "DIMBLK2": "",
               "DIMCEN": 0.09, "DIMCLRD": 0, "DIMCLRE": 0, "DIMCLRT": 0,
               "DIMDEC": 4, "DIMDLE": 0.0, "DIMDLI": 0.38, "DIMDSEP": ".",
               "DIMEXE": 0.18, "DIMEXO": 0.0625, "DIMFRAC": 0, "DIMFXL": 1.0,
               "DIMFXLON": 0, "DIMGAP": 0.09, "DIMJOGANG": 45.0,
               "DIMJUST": 0, "DIMLDRBLK": "", "DIMLFAC": 1.0, "DIMLIM": 0,
               "DIMLTEX1": "", "DIMLTEX2": "", "DIMLTYPE": "",
               "DIMLUNIT": 2, "DIMLWD": -2, "DIMLWE": -2, "DIMPOST": "",
               "DIMRND": 0.0, "DIMSCALE": 1.0, "DIMSD1": 0, "DIMSD2": 0,
               "DIMSE1": 0, "DIMSE2": 0, "DIMSOXD": 0, "DIMTAD": 0,
               "DIMTDEC": 4, "DIMTFAC": 1.0, "DIMTFILL": 0,
               "DIMTFILLCLR": 0, "DIMTIH": 1, "DIMTIX": 0, "DIMTM": 0.0,
               "DIMTMOVE": 0, "DIMTOFL": 0, "DIMTOH": 1, "DIMTOL": 0,
               "DIMTOLJ": 1, "DIMTP": 0.0, "DIMTSZ": 0.0, "DIMTVP": 0.0,
               "DIMTXSTY": "Standard", "DIMTXT": 0.18, "DIMTZIN": 0,
               "DIMZIN": 0}


BOOL_VARS = {"DIMALT", "DIMFXLON", "DIMLIM", "DIMSD1", "DIMSD2", "DIMSE1",
             "DIMSE2", "DIMSOXD", "DIMTIH", "DIMTIX", "DIMTOFL", "DIMTOH",
             "DIMTOL"}
COLOR_VARS = {"DIMCLRD", "DIMCLRE", "DIMCLRT", "DIMTFILLCLR"}


def dimstyle_params(src, name):
    """Parameters for CADCraft's `dimstyle` command: name + DIM* variables."""
    d = src.dimstyles[name].dxf
    p = {"name": name, "current": False}
    for var in DIM_VARS:
        val = d.get(var.lower())
        if val is None:
            val = DIM_DEFAULT[var]
        if var in BOOL_VARS:
            val = bool(val)
        elif var in COLOR_VARS:
            val = {0: "ByBlock", 256: "ByLayer"}.get(int(val), int(val))
        elif var == "DIMDSEP" and isinstance(val, int):
            val = chr(val) if val else "."
        p[var] = val
    return p


def import_dimstyle(cl, src, name, log):
    p = dimstyle_params(src, name)
    try:
        cl.execute("dimstyle", p)
    except CadError as e:
        log("  first try rejected (%s); retrying without empty values" % e)
        p = {k: v for k, v in p.items() if v != ""}
        cl.execute("dimstyle", p)
    log("DIMSTYLE %s: imported" % name)


def import_textstyle(cl, src, name, log):
    d = src.styles[name].dxf
    p = {"name": name, "current": False,
         "height": float(d.get("height", 0.0)),
         "widthFactor": float(d.get("width", 1.0)),
         "oblique": float(d.get("oblique", 0.0))}
    if d.get("font"):
        p["font"] = d.get("font")
    if d.get("bigfont"):
        p["bigFont"] = d.get("bigfont")
    gen = int(d.get("generation_flags", 0) or 0)
    p["backwards"] = bool(gen & 2)
    p["upsideDown"] = bool(gen & 4)
    p["vertical"] = bool(int(d.get("flags", 0) or 0) & 4)
    cl.execute("style", p)
    log("STYLE %s: imported" % name)


def import_layer(cl, src, name, exists, log):
    lay = src.layers[name]
    d = lay.dxf
    col = int(d.get("color", 7))
    p = {"name": name, "color": abs(col) if 1 <= abs(col) <= 255 else 7}
    lt = d.get("linetype", "Continuous")
    if lt and lt.lower() != "continuous":
        p["linetype"] = lt
    lw = d.get("lineweight", -3)
    if lw is not None and int(lw) >= 0:
        p["lineweight"] = int(lw) / 100.0
    cmd = "layer.set" if exists else "layer.new"
    if not exists:
        p["current"] = False
    try:
        cl.execute(cmd, p)
    except CadError as e:
        if "linetype" in p:
            log("  %s: linetype %s not accepted (%s); retrying without"
                % (name, p.pop("linetype"), e))
            cl.execute(cmd, p)
        else:
            raise
    flags = int(d.get("flags", 0))
    extra = {}
    if col < 0:
        extra["on"] = False
    if flags & 1:
        extra["frozen"] = True
    if flags & 4:
        extra["locked"] = True
    if d.get("plot", 1) == 0:
        extra["plot"] = False
    if extra:
        extra["name"] = name
        cl.execute("layer.set", extra)
    log("LAYER %s: %s" % (name, "updated" if exists else "imported"))


def write_lisp(text, log, name="steal_import.lsp"):
    """Save in a folder whose path has no spaces (a space acts as Enter at
    the CADCraft command line)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, tempfile.gettempdir(), os.path.expanduser("~"), "C:/",
              "/tmp"):
        path = os.path.join(d, name).replace("\\", "/")
        if " " in path or not os.path.isdir(d):
            continue
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return path
        except OSError:
            continue
    raise CadError("No folder without spaces in its path to save the LISP.")


def run_lisp(cl, text, log, name="steal_import.lsp"):
    """Write + load a LISP file in CADCraft; log its output and make sure no
    command is left hanging."""
    path = write_lisp(text, log, name)
    if cl is None:
        log("Offline: wrote %s" % path)
        return []
    r = cl.type('(load"%s")' % path) or {}
    out = [str(x) for x in (r.get("output") or [])]
    for line in out:
        if line.strip().lower() != "nil":
            log("  " + line)
    st = cl.call("cmdline.state") or {}
    if st.get("running"):
        log("  !! CADCraft is stuck at the prompt: %r - cancelling."
            % st.get("prompt"))
        cl.call("cmdline.key", {"key": "escape"})
    return out


def get_sysvar_text(cl, var):
    r = cl.type('(getvar"%s")' % var) or {}
    for line in r.get("output") or []:
        line = str(line)
        if '"' in line:
            return line.split('"')[1]
    return None


# ----------------------------------------------------------------------
# Dialog
# ----------------------------------------------------------------------
def run_gui(cl, start_file=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("Steal - import from another drawing")
    frm = ttk.Frame(root, padding=10)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    state = {"src": None, "existing": {}, "shown": []}
    cfg = {}
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass

    # source row
    path_var = tk.StringVar(value=start_file or "")
    ttk.Label(frm, text="Source drawing:").grid(row=0, column=0, sticky="w")
    ttk.Entry(frm, textvariable=path_var, width=46).grid(row=0, column=1,
                                                         sticky="ew")
    btn_row = ttk.Frame(frm)
    btn_row.grid(row=0, column=2, padx=(6, 0))

    # lists
    ttk.Label(frm, text="Collections").grid(row=1, column=0, sticky="w",
                                            pady=(8, 0))
    ttk.Label(frm, text="Items").grid(row=1, column=1, sticky="w",
                                      pady=(8, 0))
    cat_lb = tk.Listbox(frm, exportselection=False, width=22, height=14)
    cat_lb.grid(row=2, column=0, sticky="ns")
    item_lb = tk.Listbox(frm, selectmode="extended", exportselection=False,
                         width=46, height=14)
    item_lb.grid(row=2, column=1, sticky="nsew")
    sb = ttk.Scrollbar(frm, command=item_lb.yview)
    sb.grid(row=2, column=2, sticky="nsw")
    item_lb.configure(yscrollcommand=sb.set)
    frm.columnconfigure(1, weight=1)

    # search / options
    opt = ttk.Frame(frm)
    opt.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    ttk.Label(opt, text="Search:").grid(row=0, column=0)
    search_var = tk.StringVar()
    ttk.Entry(opt, textvariable=search_var, width=28).grid(row=0, column=1,
                                                           padx=4)
    ttk.Label(opt, text="(wildcards * ? allowed)").grid(row=0, column=2)
    hide_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opt, text="Hide items already in CADCraft",
                    variable=hide_var,
                    command=lambda: show_items()).grid(row=0, column=3,
                                                       padx=12)

    ovr_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(opt, text="Overwrite properties of existing layers",
                    variable=ovr_var).grid(row=1, column=0, columnspan=4,
                                           sticky="w", pady=(4, 0))

    log_box = tk.Text(frm, height=9, width=70, state="disabled")
    log_box.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    # ---- behaviour ----------------------------------------------------------
    def cur_cat():
        s = cat_lb.curselection()
        return CATEGORIES[s[0]][0] if s else None

    def visible(cat):
        src = state["src"]
        if not src:
            return []
        names = src.names(cat)
        pat = search_var.get().strip().lower()
        if pat:
            if not any(ch in pat for ch in "*?["):
                pat = "*" + pat + "*"
            names = [n for n in names if fnmatch.fnmatch(n.lower(), pat)]
        if hide_var.get():
            ex = state["existing"].get(cat, set())
            names = [n for n in names if n.lower() not in ex]
        return names

    def refresh_counts():
        sel = cur_cat()
        cat_lb.delete(0, "end")
        for cat, _t, _k in CATEGORIES:
            n = len(visible(cat)) if state["src"] else 0
            cat_lb.insert("end", "%s (%d)" % (cat, n))
        idx = [c[0] for c in CATEGORIES].index(sel) if sel else 0
        cat_lb.selection_set(idx)

    def show_items(*_):
        cat = cur_cat()
        item_lb.delete(0, "end")
        state["shown"] = visible(cat) if cat else []
        for n in state["shown"]:
            item_lb.insert("end", n)
        refresh_counts_only()

    def refresh_counts_only():
        sel = cat_lb.curselection()
        for i, (cat, _t, _k) in enumerate(CATEGORIES):
            cat_lb.delete(i)
            cat_lb.insert(i, "%s (%d)" % (cat, len(visible(cat))
                                          if state["src"] else 0))
        if sel:
            cat_lb.selection_set(sel[0])

    def refresh(*_):
        try:
            state["existing"] = current_names(cl)
        except CadError as e:
            log("Could not read CADCraft tables: %s" % e)
        show_items()

    def open_source(path=None):
        path = path or filedialog.askopenfilename(
            title="Select drawing to steal from",
            filetypes=[("Drawings", "*.dxf *.dwg"), ("All files", "*.*")])
        if not path:
            return
        path_var.set(path)
        try:
            log("Reading %s ..." % path)
            state["src"] = Source(path)
        except CadError as e:
            state["src"] = None
            messagebox.showerror("Steal", str(e))
            log("ERROR: %s" % e)
            return
        cfg["last"] = path
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        except OSError:
            pass
        cat_lb.selection_clear(0, "end")
        cat_lb.selection_set(0)
        refresh()
        log("Loaded.")

    def run_import(wanted):
        src = state["src"]
        if not src:
            messagebox.showinfo("Steal", "Select a source drawing first.")
            return
        if not any(wanted.values()):
            messagebox.showinfo("Steal", "Nothing selected.")
            return
        try:
            want = resolve(src, wanted)
            if cl is not None:
                state["existing"] = current_names(cl)
            ex = state["existing"]
            todo = {c: {n for n in want[c] if n.lower() not in ex.get(c, ())}
                    for c in want}
            total = sum(len(v) for v in todo.values())
            ovr = ovr_var.get()
            if ovr:
                todo["Layers"] = set(want["Layers"])
            log("Importing...")
            if cl is None:
                log("Offline: nothing is sent. Connect to CADCraft to import.")
                return

            def attempt(fn, *args):
                try:
                    fn(cl, src, *args, log)
                except CadError as e:
                    log("  %s: FAILED - %s" % (args[0], e))

            # 1. linetypes (entmake)
            if todo["Linetypes"]:
                t, n, w = build_lisp(src, todo, ("Linetypes",))
                run_lisp(cl, t, log, "steal_ltypes.lsp")
            # 2. text styles
            for name in sorted(todo["Text Styles"], key=str.lower):
                attempt(import_textstyle, name)
            # 3. layers
            for name in sorted(todo["Layers"], key=str.lower):
                exists = name.lower() in ex.get("Layers", ())
                attempt(import_layer, name, exists)
            # 4. blocks (entmake)
            if todo["Blocks"]:
                t, n, w = build_lisp(src, todo, ("Blocks",))
                for x in w:
                    log("note: " + x)
                run_lisp(cl, t, log, "steal_blocks.lsp")
            # 5. dimension styles
            for name in sorted(todo["Dimension Styles"], key=str.lower):
                attempt(import_dimstyle, name)
            log("Done (%d new item(s) requested)." % total)
        except CadError as e:
            log("ERROR: %s" % e)
            messagebox.showerror("Steal", str(e))
            return
        refresh()

    def steal_selected():
        cat = cur_cat()
        names = {state["shown"][i] for i in item_lb.curselection()}
        wanted = {cat: names} if cat else {}
        run_import(wanted)

    def steal_all():
        src = state["src"]
        if not src:
            return
        ex = state["existing"]
        wanted = {c[0]: {n for n in src.names(c[0])
                         if n.lower() not in ex.get(c[0], set())}
                  for c in CATEGORIES}
        run_import(wanted)

    cat_lb.bind("<<ListboxSelect>>", show_items)
    search_var.trace_add("write", lambda *a: show_items())
    item_lb.bind("<Control-a>", lambda e: item_lb.selection_set(0, "end"))

    ttk.Button(btn_row, text="Browse...",
               command=open_source).grid(row=0, column=0)
    bar = ttk.Frame(frm)
    bar.grid(row=5, column=0, columnspan=3, sticky="e", pady=(8, 0))
    ttk.Button(bar, text="Refresh", command=refresh).grid(row=0, column=0,
                                                          padx=3)
    ttk.Button(bar, text="Select all in list",
               command=lambda: item_lb.selection_set(0, "end")).grid(
        row=0, column=1, padx=3)
    ttk.Button(bar, text="Steal selected",
               command=steal_selected).grid(row=0, column=2, padx=3)
    ttk.Button(bar, text="Steal everything",
               command=steal_all).grid(row=0, column=3, padx=3)
    ttk.Button(bar, text="Done", command=root.destroy).grid(row=0, column=4,
                                                            padx=3)

    refresh_counts()
    start = start_file or cfg.get("last")
    if start and os.path.isfile(start):
        root.after(100, lambda: open_source(start))
    root.mainloop()
    return 0


def diagnostics(cl):
    print("== ping:", cl.call("ping"))
    print("== current tables:")
    for cat, n in current_names(cl).items():
        print("   %-18s %d  %s" % (cat, len(n), sorted(n)[:8]))
    r = cl.type('(prompt (strcat "\\ntblsearch LAYER 0: "'
                ' (if (tblsearch "LAYER" "0") "ok" "NO")))') or {}
    print("== LISP test:", r.get("output"))


def show_commands(cl):
    """Print CADCraft's own definition of the commands Steal needs."""
    cmds = cl.call("engine.commands")
    items = cmds if isinstance(cmds, list) else (
        find_collection(cmds, {"commands"}) or [])
    print("engine.commands returned: %s, %d item(s)"
          % (type(cmds).__name__, len(items) if hasattr(items, "__len__")
             else -1))
    if isinstance(cmds, dict):
        print("top-level keys:", list(cmds.keys())[:20])
    if isinstance(items, dict):
        items = [dict(v, _key=k) if isinstance(v, dict) else {"_key": k,
                 "value": v} for k, v in items.items()]
    words = ("dimstyle", "style", "layer", "linetype", "ltype")
    hits = 0
    for c in items:
        text = json.dumps(c).lower()
        head = text[:80]
        if any(w in head for w in words):
            print(json.dumps(c, indent=1)[:1500])
            print("-" * 40)
            hits += 1
    if not hits:
        print("No dimstyle/style/layer entries found. First 5 raw items:")
        for c in items[:5]:
            print(json.dumps(c)[:600])
        print("All names:", [(c.get("name") or c.get("_key") or str(c)[:30])
                             if isinstance(c, dict) else str(c)
                             for c in items][:400])


def main():
    ap = argparse.ArgumentParser(description="Steal for CADCraft")
    ap.add_argument("file", nargs="?", help="source drawing (DXF/DWG)")
    ap.add_argument("--port", type=int, default=int(
        os.environ.get("CADCRAFT_CONTROL_PORT", DEFAULT_PORT)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--cmds", action="store_true",
                    help="print CADCraft's dimstyle/style/layer command "
                         "definitions")
    a = ap.parse_args()
    if a.offline:
        return run_gui(None, a.file)
    try:
        cl = Client(a.port, a.token)
    except CadError as e:
        print("ERROR:", e)
        return 1
    try:
        if a.cmds:
            show_commands(cl)
            return 0
        if a.diag:
            diagnostics(cl)
            return 0
        return run_gui(cl, a.file)
    finally:
        cl.close()


if __name__ == "__main__":
    sys.exit(main())