#!/usr/bin/env python3
"""
cadcraft_mtext_editor.py - an in-place style MTEXT editor for CADCraft.

Works like AutoCAD's MTEXT editor: type and format text on screen (bold,
italic, underline, overline, strikethrough, font, colour, height, oblique,
width factor, tracking), insert symbols, stack/unstack fractions, change
case, bullets and numbering, find & replace, import text, and set the
object's own properties (style, height, width, rotation, justification,
line spacing).

It runs as a separate program (Python 3 + tkinter, both standard) and talks
to the running CADCraft over its control channel.

How to use
  1. Start CADCraft with the control channel on:   cadcraft --control 7979
  2. Select an MTEXT object in CADCraft.
  3. Run:   python cadcraft_mtext_editor.py
  4. Edit, then press Apply.

The ruler above the text works like AutoCAD's: drag the red handle to set the
width, the blue triangles for the first-line and left indents, and click the
ruler to add a tab stop (drag it to move it, double-click or drag it off the
left edge to remove it, right-click to cycle left / centre / right).
Indents and tabs apply to the paragraph at the cursor, or to every paragraph
in the selection.

Tabs are written as real tab characters, as CADCraft does (it would draw
"^I" literally).

The "MTEXT codes" tab shows the raw text CADCraft stores (for example
{\\fArial|b1|i0;bold}). You can edit it there too. Anything the editor
cannot display as formatting (stacked fractions, paragraph indents, column
breaks...) is kept as grey code text so nothing is lost.

  python cadcraft_mtext_editor.py --diag     what CADCraft returns
  python cadcraft_mtext_editor.py --codes    print the selected MTEXT's codes
"""
import argparse
import glob
import json
import os
import re
import socket
import sys
import tempfile
import time

DEFAULT_PORT = 7979
DEFAULT_FONT = "Arial"      # used when bold/italic is set but no font is
ATTACH_CODES = ["TL", "TC", "TR", "ML", "MC", "MR", "BL", "BC", "BR"]
ACI_HEX = {1: "#e00000", 2: "#c8a000", 3: "#00a000", 4: "#00a8a8",
           5: "#0000e0", 6: "#c000c0", 7: "#000000", 8: "#808080",
           9: "#b0b0b0"}
COLOR_CHOICES = [("ByLayer", None), ("ByBlock", "c:A0"), ("Red", "c:A1"),
                 ("Yellow", "c:A2"), ("Green", "c:A3"), ("Cyan", "c:A4"),
                 ("Blue", "c:A5"), ("Magenta", "c:A6"), ("White", "c:A7"),
                 ("Other...", "other")]
SYMBOLS = [("Degrees  \u00b0", "\u00b0"), ("Plus/Minus  \u00b1", "\u00b1"),
           ("Diameter  \u00d8", "\u00d8"), ("Almost equal  \u2248", "\u2248"),
           ("Angle  \u2220", "\u2220"), ("Delta  \u0394", "\u0394"),
           ("Ohm  \u03a9", "\u03a9"), ("Not equal  \u2260", "\u2260"),
           ("Identity  \u2261", "\u2261"), ("Squared  \u00b2", "\u00b2"),
           ("Cubed  \u00b3", "\u00b3"),
           ("Non-breaking space", "\\~")]
FAMILY_PREFIXES = ("c:", "h:", "w:", "t:", "q:", "f:")


# ======================================================================
# Paragraph properties (\p codes): first-line indent, left indent, right
# indent, alignment and tab stops, all as multiples of the text height.
# A paragraph's settings persist until the next \p code, so every
# character carries a tag  p:i<n>;l<n>;r<n>;q<c>;t<tabs>  when its
# paragraph is not the default.
# ======================================================================
PARA_DEFAULT = (0.0, 0.0, 0.0, "", ())     # i, l, r, q, tabs((type, pos)..)


def _num(x):
    return "%g" % round(float(x), 3)


def para_tag(p):
    if p == PARA_DEFAULT:
        return None
    tabs = ",".join(t + _num(x) for t, x in p[4])
    return "p:i%s;l%s;r%s;q%s;t%s" % (_num(p[0]), _num(p[1]), _num(p[2]),
                                      p[3], tabs)


def para_from_tag(tag):
    f = {part[0]: part[1:] for part in tag[2:].split(";") if part}
    tabs = []
    for spec in (f.get("t") or "").split(","):
        if spec:
            tabs.append((spec[0], float(spec[1:])))
    return (float(f.get("i", 0)), float(f.get("l", 0)),
            float(f.get("r", 0)), f.get("q", ""), tuple(tabs))


def para_from_style(style):
    for t in style:
        if t.startswith("p:"):
            return para_from_tag(t)
    return PARA_DEFAULT


def parse_para_args(arg, prev):
    """Apply the arguments of a \\p code to the previous paragraph state.
    Raises ValueError when the code is not understood."""
    i, l, r, q, tabs = prev
    arg = arg.replace("x", "").replace(" ", "")
    in_tabs = False
    new_tabs = None

    def val(t):
        return 0.0 if t == "*" else float(t)

    def add_tab(spec):
        typ = "l"
        if spec[:1] in ("r", "c"):
            typ, spec = spec[0], spec[1:]
        new_tabs.append((typ, val(spec)))

    for part in arg.split(","):
        if not part:
            continue
        k = part[0]
        if k == "t":
            in_tabs = True
            new_tabs = []
            if part[1:]:
                add_tab(part[1:])
        elif k == "i":
            i = val(part[1:])
        elif k == "l":
            l = val(part[1:])
        elif k == "q":
            q = "" if part[1:] in ("", "*") else part[1:2]
        elif in_tabs:
            add_tab(part)
        elif k == "r":
            r = val(part[1:])
        else:
            raise ValueError(part)
    return (i, l, r, q, tuple(new_tabs) if new_tabs is not None else tabs)


def para_code(p, prev=PARA_DEFAULT):
    """The \\p code that changes paragraph settings from prev to p."""
    if p == PARA_DEFAULT:
        return "\\pi*,l*,r*,q*,t;"
    parts = ["i" + _num(p[0]), "l" + _num(p[1])]
    if p[2] or prev[2]:
        parts.append("r" + _num(p[2]))
    if p[3]:
        parts.append("q" + p[3])
    elif prev[3]:
        parts.append("q*")
    if p[4]:
        parts.append("t" + ",".join(
            (t if t != "l" else "") + _num(x) for t, x in p[4]))
    elif prev[4]:
        parts.append("t")
    return "\\px" + ",".join(p_ for p_ in parts if p_) + ";"


# ======================================================================
# MTEXT codes  <->  runs of (text, set of style tags)
#
# Style tags: b i u o k (bold, italic, underline, overline, strike)
#             f:<font>  c:A<aci>  c:R<bgr int>  h:<n[x]>  w:<n[x]>
#             t:<n[x]>  q:<angle>  raw (kept verbatim, not formatting)
# ======================================================================
def parse_mtext(s):
    """Parse MTEXT contents into [(text, frozenset(tags)), ...]."""
    runs = []
    stack = []
    cur = set()
    para = [PARA_DEFAULT]

    def put(text, raw=False):
        tags = set(cur)
        pt = para_tag(para[0])
        if pt:
            tags.add(pt)
        if raw:
            tags.add("raw")
        fs = frozenset(tags)
        if runs and runs[-1][1] == fs:
            runs[-1] = (runs[-1][0] + text, fs)
        else:
            runs.append((text, fs))

    def setfam(prefix, tag):
        for t in [t for t in cur if t.startswith(prefix)]:
            cur.discard(t)
        if tag:
            cur.add(tag)

    def flag(tag, on):
        if on:
            cur.add(tag)
        else:
            cur.discard(tag)

    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            c = s[i + 1]
            if c in "\\{}":
                put(c)
                i += 2
            elif c == "P":
                put("\n")
                i += 2
            elif c in "Ll":
                flag("u", c == "L")
                i += 2
            elif c in "Oo":
                flag("o", c == "O")
                i += 2
            elif c in "Kk":
                flag("k", c == "K")
                i += 2
            elif c in "CcHWTQfFASp":
                end = s.find(";", i + 2)
                if c == "A" and (end < 0 or end > i + 5):
                    end = i + 2
                if end < 0:
                    put(s[i:], raw=True)
                    i = n
                    continue
                arg = s[i + 2:end]
                tok = s[i:end + 1]
                ok = True
                try:
                    if c == "C":
                        k = int(arg)
                        if not 0 <= k <= 256:
                            raise ValueError
                        setfam("c:", "c:A%d" % k)
                    elif c == "c":
                        setfam("c:", "c:R%d" % (int(arg) & 0xFFFFFF))
                    elif c in "HWTQ":
                        float(arg[:-1] if arg.endswith("x") else arg)
                        setfam(c.lower() + ":", c.lower() + ":" + arg)
                    elif c in "fF":
                        parts = arg.split("|")
                        setfam("f:", ("f:" + parts[0]) if parts[0] else None)
                        if c == "f":
                            for p in parts[1:]:
                                if p[:1] == "b":
                                    flag("b", p[1:] == "1")
                                elif p[:1] == "i":
                                    flag("i", p[1:] == "1")
                    elif c == "p":
                        para[0] = parse_para_args(arg, para[0])
                    else:                       # \A \S  -> keep verbatim
                        ok = False
                except ValueError:
                    ok = False
                if not ok:
                    put(tok, raw=True)
                i = end + 1
            else:                               # \~ \N \X ... keep verbatim
                put(s[i:i + 2], raw=True)
                i += 2
        elif ch == "{":
            stack.append(set(cur))
            i += 1
        elif ch == "}":
            if stack:
                cur.clear()
                cur.update(stack.pop())
            i += 1
        elif ch == "^" and s[i + 1:i + 2] == "I":
            put("\t")
            i += 2
        elif s[i:i + 3].lower() in ("%%c", "%%d", "%%p"):
            put({"c": "\u00d8", "d": "\u00b0", "p": "\u00b1"}[s[i + 2].lower()])
            i += 3
        else:
            put(ch)
            i += 1
    return runs


def merge_runs(runs):
    out = []
    for text, st in runs:
        if not text:
            continue
        if out and out[-1][1] == st:
            out[-1] = (out[-1][0] + text, st)
        else:
            out.append((text, frozenset(st)))
    return out


def esc_text(text):
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch in "{}":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\P")
        elif ch == "\t":
            out.append("\t")        # CADCraft writes real tabs; "^I" shows literally
        else:
            out.append(ch)
    return "".join(out)


def style_codes(style, default_font=DEFAULT_FONT):
    s = ""
    if "u" in style:
        s += "\\L"
    if "o" in style:
        s += "\\O"
    if "k" in style:
        s += "\\K"
    for t in sorted(style):
        if t.startswith("c:A"):
            s += "\\C%s;" % t[3:]
        elif t.startswith("c:R"):
            s += "\\c%s;" % t[3:]
        elif t[:2] in ("h:", "w:", "t:", "q:"):
            s += "\\%s%s;" % (t[0].upper(), t[2:])
    fonts = [t for t in style if t.startswith("f:")]
    if fonts or "b" in style or "i" in style:
        name = fonts[0][2:] if fonts else default_font
        s = ("\\f%s|b%d|i%d|c0|p34;" % (name, "b" in style, "i" in style)) + s
    return s


def serialize(runs, default_font=DEFAULT_FONT):
    """Runs back to MTEXT contents. Each styled run is wrapped in {...}."""
    out = []
    cur_p = PARA_DEFAULT
    for text, style in merge_runs(runs):
        p = para_from_style(style)
        if p != cur_p:
            out.append(para_code(p, cur_p))
            cur_p = p
        raw = "raw" in style
        body = text if raw else esc_text(text)
        codes = style_codes(style - {"raw"}, default_font)
        out.append("{%s%s}" % (codes, body) if codes else body)
    return "".join(out)


def plain_text(runs):
    return "".join(t for t, st in runs if "raw" not in st)


# ======================================================================
# Control-channel client
# ======================================================================
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
    """Control-channel client. CADCraft closes connections that are idle for
    10 minutes, so this reconnects by itself (re-reading the token, which
    also copes with CADCraft having been restarted)."""

    IDLE_RECONNECT = 240        # seconds without traffic before reconnecting
    READ_ONLY = ("ping", "cmdline.state", "drawing.inspect",
                 "engine.commands", "control.methods")
    READ_ONLY_COMMANDS = ("entities", "drawing.inspect", "engine.commands")

    def __init__(self, port=DEFAULT_PORT, token=None, host="127.0.0.1",
                 timeout=65):
        self.port = port
        self.host = host
        self.timeout = timeout
        self._token_arg = token
        self.sock = None
        self.rfile = None
        self._id = 0
        self._last = 0.0
        self._connect()

    def _connect(self):
        self.close()
        self.token = (self._token_arg if self._token_arg is not None
                      else find_token(self.port))
        if self.token is None:
            raise CadError(
                "Could not find the session token file "
                "cadcraft-control-%s.token. Is CADCraft running with "
                "--control %s? (You can also set CADCRAFT_TOKEN or use "
                "--token.)" % (self.port, self.port))
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=5)
        except OSError as e:
            raise CadError("Cannot connect to CADCraft on port %s: %s"
                           % (self.port, e))
        self.sock.settimeout(self.timeout)
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self._last = time.monotonic()

    def close(self):
        for obj in (self.rfile, self.sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self.rfile = None
        self.sock = None

    def _is_read_only(self, method, params):
        if method in self.READ_ONLY:
            return True
        return (method == "engine.execute"
                and (params or {}).get("command") in self.READ_ONLY_COMMANDS)

    def call(self, method, params=None):
        if self.sock is None or (time.monotonic() - self._last
                                 > self.IDLE_RECONNECT):
            self._connect()
        self._id += 1
        req = {"id": self._id, "method": method,
               "params": params or {}, "token": self.token}
        data = (json.dumps(req) + "\n").encode("utf-8")
        sent = False
        try:
            self.sock.sendall(data)
            sent = True
            line = self.rfile.readline()
            if not line:
                raise ConnectionAbortedError("connection closed")
        except socket.timeout:
            self.close()
            raise CadError("CADCraft did not answer within %s seconds."
                           % self.timeout)
        except OSError:
            # the connection was dropped: reconnect, and retry when that is
            # safe (the request never left, or it only reads)
            self._connect()
            if (not sent) or self._is_read_only(method, params):
                try:
                    self.sock.sendall(data)
                    line = self.rfile.readline()
                    if not line:
                        raise ConnectionAbortedError("connection closed")
                except OSError as e:
                    self.close()
                    raise CadError("Lost the connection to CADCraft: %s" % e)
            else:
                raise CadError(
                    "The connection to CADCraft was lost, so this change may "
                    "not have been made. It has reconnected - check the "
                    "drawing, then try again.")
        self._last = time.monotonic()
        reply = json.loads(line)
        if not reply.get("ok"):
            msg = str(reply.get("error", "unknown error"))
            if "refedit" in msg.lower() or "working set" in msg.lower():
                raise globals().get("RefEditOpen", CadError)(msg)
            raise CadError(msg)
        return reply.get("result")

    def execute(self, command, params=None):
        return self.call("engine.execute",
                         {"command": command, "params": params or {}})


# ======================================================================
# Reading and writing MTEXT objects
# ======================================================================
KEYS = {
    "text": ("text", "contents", "content", "value", "string"),
    "height": ("height", "textHeight", "charHeight"),
    "width": ("width", "refWidth", "boxWidth", "columnWidth"),
    "rotation": ("rotation", "angle"),
    "attach": ("attach", "attachment", "attachmentPoint", "justify",
               "justification"),
    "style": ("style", "textStyle"),
    "lineSpacing": ("lineSpacing", "lineSpacingFactor", "spacing"),
}
NUMERIC = ("height", "width", "rotation", "lineSpacing")


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


def entity_type(d):
    t = d.get("type") if isinstance(d, dict) else None
    return t.lower() if isinstance(t, str) else ""


def editable_dict(e):
    """(fields, plain) for an MTEXT - or a single-line TEXT, plain=True -
    entity, else (None, False).

    CADCraft wraps each object: handle / layer / colour are on the outside
    and the object's own fields (contents, height, width ...) are inside
    "geometry". The two are merged so the fields can be found either way."""
    cands = [e]
    if isinstance(e, dict):
        for k in ("geometry", "entity", "data"):
            if isinstance(e.get(k), dict):
                cands.append(e[k])

    def merged(d):
        inner = None
        for k in ("geometry", "entity", "data"):
            if isinstance(d.get(k), dict):
                inner = d[k]
                break
        if inner is None:
            return d
        m = dict(d)
        m.update(inner)
        return m

    for d in cands:
        t = entity_type(d)
        if "mtext" in t or "multiline" in t:
            return merged(d), False
    for d in cands:
        if entity_type(d) in ("text", "dtext"):
            return merged(d), True
    return None, False


def any_type(e):
    t = entity_type(e)
    if not t and isinstance(e, dict):
        for k in ("geometry", "entity", "data"):
            if isinstance(e.get(k), dict):
                t = entity_type(e[k])
                if t:
                    break
    return t or "unknown"


def scan_selection(cl, selected=True, page=1000):
    """-> (items, others, first_other)
    items: editable text objects [{handle, raw, keys, plain}]
    others: {type: count} of everything else, first_other: its first JSON"""
    items, others, first_other = [], {}, None
    offset = 0
    while True:
        params = {"limit": page, "offset": offset}
        if selected:
            params["selected"] = True
        res = cl.execute("entities", params)
        ents = find_key(res, "entities", list) or []
        total = find_key(res, "count", int)
        for e in ents:
            d, plain = editable_dict(e)
            if d is None:
                t = any_type(e)
                others[t] = others.get(t, 0) + 1
                if first_other is None:
                    first_other = e
                continue
            h = find_key(e, "handle", (str, int))
            keys = {}
            for prop, cands in KEYS.items():
                for k in cands:
                    if k in d:
                        keys[prop] = k
                        break
            items.append({"handle": None if h is None else str(h),
                          "raw": d, "keys": keys, "plain": plain})
        offset += len(ents)
        if not ents or total is None or offset >= total:
            break
    return items, others, first_other


def fetch_mtexts(cl, selected=True, page=1000):
    return scan_selection(cl, selected, page)[0]


SKIP_TEXT_KEYS = ("type", "handle", "layer", "style", "linetype", "color",
                  "id", "name", "block", "textstyle", "font")


def info_text(info):
    """The object's text contents, wherever CADCraft keeps them."""
    d = info["raw"]
    for k in KEYS["text"]:
        v = d.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            for kk in ("value", "text", "contents", "content", "string"):
                if isinstance(v.get(kk), str):
                    return v[kk]
    best = ""
    for k, v in d.items():
        if (isinstance(v, str) and k.lower() not in SKIP_TEXT_KEYS
                and len(v) > len(best)):
            best = v
    return best


def text_field_found(info):
    d = info["raw"]
    for k in KEYS["text"]:
        if k in d:
            return True
    return False


def info_value(info, prop, default=""):
    k = info["keys"].get(prop)
    if k is None:
        return default
    return info["raw"].get(k, default)


def attach_to_code(v):
    if isinstance(v, int) and 1 <= v <= 9:
        return ATTACH_CODES[v - 1]
    if isinstance(v, str) and v.upper() in ATTACH_CODES:
        return v.upper()
    return ""


def apply_changes(cl, info, codes, props, log):
    """Write new text (textedit) and changed properties (properties.set)."""
    handle = info["handle"]
    if not handle:
        raise CadError("CADCraft did not report a handle for this object.")
    if codes is not None:
        try:
            cl.execute("textedit", {"handle": handle, "text": codes})
        except RefEditOpen:
            raise
        except CadError as first:
            log("textedit refused (%s); trying properties.set." % first)
            last = first
            for name in ("text", info["keys"].get("text")):
                if not name:
                    continue
                try:
                    cl.execute("properties.set",
                               {"handles": [handle], name: codes})
                    break
                except RefEditOpen:
                    raise
                except CadError as e2:
                    last = e2
            else:
                raise last
        log("Text written.")
    if props:
        params = {"handles": [handle]}
        for prop, val in props.items():
            params[info["keys"][prop]] = val
        cl.execute("properties.set", params)
        log("Properties set: %s" % ", ".join(sorted(props)))


# ======================================================================
# The editor window
# ======================================================================
def run_gui(port, token):
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, messagebox, filedialog, colorchooser

    try:
        cl = Client(port, token)
    except CadError as e:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("CADCraft MTEXT editor", str(e))
        return 1

    root = tk.Tk()
    root.title("CADCraft - MTEXT editor")
    families = sorted(set(tkfont.families()))
    base_size = 13
    px_h = base_size * root.winfo_fpixels("1i") / 72.0   # pixels per text height
    v_wrap = tk.BooleanVar(value=True)

    state = {"items": [], "info": None, "orig": {}, "loaded_codes": "",
             "pending": None, "fonts": {}, "codes_dirty": False,
             "base_font": DEFAULT_FONT, "sig": None, "busy": False}

    # ---------------- variables ----------------
    v_pick = tk.StringVar()
    v_follow = tk.BooleanVar(value=True)
    v_status = tk.StringVar()
    pv = {p: tk.StringVar() for p in ("style", "height", "width", "rotation",
                                      "lineSpacing", "attach")}
    v_font = tk.StringVar()
    v_color = tk.StringVar(value="ByLayer")
    flags = {t: tk.BooleanVar() for t in ("b", "i", "u", "o", "k")}
    sv = {p: tk.StringVar() for p in ("h", "q", "w", "t")}

    outer = ttk.Frame(root, padding=8)
    outer.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(4, weight=1)

    # ---- row 0: which object ----
    r0 = ttk.Frame(outer)
    r0.grid(row=0, column=0, sticky="ew")
    ttk.Label(r0, text="Selected text:").pack(side="left")
    pick_box = ttk.Combobox(r0, textvariable=v_pick, state="readonly",
                            width=44)
    pick_box.pack(side="left", padx=6)
    ttk.Checkbutton(r0, text="Follow CADCraft selection",
                    variable=v_follow).pack(side="left", padx=4)
    ttk.Label(outer, textvariable=v_status, foreground="#8a4b00",
              wraplength=760, justify="left").grid(row=6, column=0,
                                                   sticky="w", pady=(4, 0))

    # ---- row 1: object properties ----
    r1 = ttk.LabelFrame(outer, text="Object", padding=4)
    r1.grid(row=1, column=0, sticky="ew", pady=(6, 0))
    prop_widgets = {}

    def prop_field(col, label, prop, width, values=None):
        ttk.Label(r1, text=label).grid(row=0, column=col * 2, sticky="e",
                                       padx=(8, 2))
        if values:
            w = ttk.Combobox(r1, textvariable=pv[prop], width=width,
                             values=values)
        else:
            w = ttk.Entry(r1, textvariable=pv[prop], width=width)
        w.grid(row=0, column=col * 2 + 1, sticky="w")
        prop_widgets[prop] = w

    prop_field(0, "Style", "style", 14)
    prop_field(1, "Text height", "height", 8)
    prop_field(2, "Width", "width", 8)
    prop_field(3, "Rotation", "rotation", 7)
    prop_field(4, "Line spacing", "lineSpacing", 6,
               ["1.0", "1.5", "2.0", "2.5", "3.0"])
    prop_field(5, "Justify", "attach", 5, ATTACH_CODES)
    ttk.Checkbutton(r1, text="Wrap at width", variable=v_wrap,
                    command=lambda: (restyle(), redraw_ruler())
                    ).grid(row=0, column=12, padx=(14, 0))

    # ---- row 2: character formatting ----
    r2 = ttk.LabelFrame(outer, text="Formatting", padding=4)
    r2.grid(row=2, column=0, sticky="ew", pady=(6, 0))
    font_box = ttk.Combobox(r2, textvariable=v_font, values=[""] + families,
                            width=22)
    font_box.pack(side="left", padx=(0, 6))

    def toggle_btn(tag, label, **kw):
        b = tk.Checkbutton(r2, text=label, variable=flags[tag],
                           indicatoron=False, width=3, relief="raised",
                           command=lambda: on_toggle(tag), **kw)
        b.pack(side="left", padx=1)
        return b

    toggle_btn("b", "B", font=("TkDefaultFont", 10, "bold"))
    toggle_btn("i", "I", font=("TkDefaultFont", 10, "italic"))
    toggle_btn("u", "U", font=("TkDefaultFont", 10, "underline"))
    toggle_btn("o", "\u00d6\u0305")
    toggle_btn("k", "S", font=("TkDefaultFont", 10, "overstrike"))
    color_box = ttk.Combobox(r2, textvariable=v_color, state="readonly",
                             width=9, values=[c[0] for c in COLOR_CHOICES])
    color_box.pack(side="left", padx=(8, 4))

    sym_btn = ttk.Menubutton(r2, text="Symbol")
    sym_menu = tk.Menu(sym_btn, tearoff=False)
    sym_btn["menu"] = sym_menu
    sym_btn.pack(side="left", padx=2)
    ttk.Button(r2, text="Stack", width=6,
               command=lambda: do_stack()).pack(side="left", padx=1)
    ttk.Button(r2, text="Unstack", width=8,
               command=lambda: do_unstack()).pack(side="left", padx=1)
    case_btn = ttk.Menubutton(r2, text="Case")
    case_menu = tk.Menu(case_btn, tearoff=False)
    case_btn["menu"] = case_menu
    case_btn.pack(side="left", padx=2)
    list_btn = ttk.Menubutton(r2, text="Bullets")
    list_menu = tk.Menu(list_btn, tearoff=False)
    list_btn["menu"] = list_menu
    list_btn.pack(side="left", padx=2)
    ttk.Button(r2, text="Find/Replace", command=lambda: find_dialog()
               ).pack(side="left", padx=2)
    ttk.Button(r2, text="Import text...", command=lambda: import_text()
               ).pack(side="left", padx=2)

    # ---- row 3: size-type formatting for the selection ----
    r3 = ttk.LabelFrame(outer, text="Selected text", padding=4)
    r3.grid(row=3, column=0, sticky="ew", pady=(6, 0))
    for col, (key, label) in enumerate((("h", "Height"), ("q", "Oblique \u00b0"),
                                        ("w", "Width factor"),
                                        ("t", "Tracking"))):
        ttk.Label(r3, text=label).grid(row=0, column=col * 2, sticky="e",
                                       padx=(8, 2))
        e = ttk.Entry(r3, textvariable=sv[key], width=8)
        e.grid(row=0, column=col * 2 + 1)
        e.bind("<Return>", lambda ev, k=key: on_size(k))
    ttk.Button(r3, text="Clear formatting",
               command=lambda: clear_format()).grid(row=0, column=9, padx=12)

    # ---- row 4: notebook ----
    nb = ttk.Notebook(outer)
    nb.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
    tab_ed = ttk.Frame(nb)
    tab_code = ttk.Frame(nb)
    nb.add(tab_ed, text="Editor")
    nb.add(tab_code, text="MTEXT codes")
    PAD = 14
    tab_ed.columnconfigure(0, weight=1)
    tab_ed.rowconfigure(1, weight=1)
    ruler = tk.Canvas(tab_ed, height=42, background="#f2f2f2",
                      highlightthickness=0, cursor="hand2")
    ruler.grid(row=0, column=0, sticky="ew")
    text = tk.Text(tab_ed, width=90, height=14, undo=True, wrap="word",
                   font=("TkDefaultFont", base_size), background="white",
                   foreground="black", insertbackground="black",
                   padx=PAD, borderwidth=1, highlightthickness=0)
    sb = ttk.Scrollbar(tab_ed, command=text.yview)
    text.configure(yscrollcommand=sb.set)
    text.grid(row=1, column=0, sticky="nsew")
    sb.grid(row=1, column=1, sticky="ns")
    code_box = tk.Text(tab_code, width=90, height=14, wrap="char",
                       font=("Courier", 11))
    code_box.pack(fill="both", expand=True)

    # ---- row 5: log + buttons ----
    r5 = ttk.Frame(outer)
    r5.grid(row=5, column=0, sticky="ew", pady=(6, 0))
    log_box = tk.Text(r5, width=70, height=4, state="disabled")
    log_box.pack(side="left", fill="x", expand=True)
    btns = ttk.Frame(r5)
    btns.pack(side="right", padx=(8, 0))

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    # ------------------------------------------------------------------
    # attribute tags and display
    # ------------------------------------------------------------------
    def is_attr(name):
        return not (name.startswith("d|") or name in ("sel", "found"))

    def attrs_at(index):
        return {t for t in text.tag_names(index) if is_attr(t)}

    def segments(start="1.0", end="end-1c"):
        out = []
        if text.compare(start, ">=", end):
            return out
        active = attrs_at(start)
        for key, value, index in text.dump(start, end, tag=True, text=True):
            if key == "tagon":
                if is_attr(value):
                    active.add(value)
            elif key == "tagoff":
                active.discard(value)
            elif key == "text":
                out.append((value, frozenset(active)))
        return merge_runs(out)

    def display_font(style):
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        obj_h = 0.0
        try:
            obj_h = float(pv["height"].get())
        except ValueError:
            pass
        size = float(base_size)
        raw = "raw" in style
        for t in style:
            if t.startswith("f:") and t[2:] in families:
                fam = t[2:]
            if t.startswith("h:"):
                v = t[2:]
                try:
                    if v.endswith("x"):
                        size = base_size * float(v[:-1])
                    elif obj_h > 0:
                        size = base_size * float(v) / obj_h
                except ValueError:
                    pass
        if raw:
            fam = "Courier"
            size = base_size - 1
        key = (fam, int(size), "b" in style, "i" in style, "u" in style,
               "k" in style)
        if key not in state["fonts"]:
            state["fonts"][key] = tkfont.Font(
                family=fam, size=max(6, int(size)),
                weight="bold" if "b" in style else "normal",
                slant="italic" if "i" in style else "roman",
                underline="u" in style, overstrike="k" in style)
        return state["fonts"][key]

    def display_color(style):
        for t in style:
            if t.startswith("c:A"):
                n = int(t[3:])
                return ACI_HEX.get(n, "#505050")
            if t.startswith("c:R"):
                v = int(t[3:])
                return "#%02x%02x%02x" % (v & 255, (v >> 8) & 255,
                                          (v >> 16) & 255)
        return "black"

    def restyle():
        for name in text.tag_names():
            if name.startswith("d|"):
                text.tag_delete(name)
        active = set()
        for key, value, index in text.dump("1.0", "end-1c", tag=True,
                                           text=True):
            if key == "tagon":
                if is_attr(value):
                    active.add(value)
            elif key == "tagoff":
                active.discard(value)
            elif key == "text":
                st = frozenset(active)
                name = "d|" + "|".join(sorted(st))
                if name not in text.tag_names():
                    cfg = {"font": display_font(st),
                           "foreground": "#707070" if "raw" in st
                           else display_color(st)}
                    if "raw" in st:
                        cfg["background"] = "#f0f0f0"
                    elif "o" in st:
                        cfg["background"] = "#e3ecff"
                    cfg.update(para_display(st))
                    text.tag_configure(name, **cfg)
                text.tag_add(name, index, "%s+%dc" % (index, len(value)))
        text.tag_raise("sel")

    def insert_runs(index, runs):
        for t, st in runs:
            text.insert(index, t, tuple(sorted(st)))
            index = text.index("%s+%dc" % (index, len(t)))
        return index

    def load_codes(codes):
        text.delete("1.0", "end")
        insert_runs("1.0", parse_mtext(codes))
        restyle()
        text.edit_reset()

    def current_codes():
        segs = segments()
        if state["info"] and state["info"].get("plain"):
            return "".join(t for t, st in segs).replace("\n", " ")
        return serialize(segs, state["base_font"])

    def sync_codes_from_editor():
        code_box.delete("1.0", "end")
        code_box.insert("1.0", current_codes())

    def sync_editor_from_codes():
        load_codes(code_box.get("1.0", "end-1c"))

    def on_tab(_e=None):
        if nb.index("current") == 1:
            sync_codes_from_editor()
            state["codes_dirty"] = False
        elif state["codes_dirty"]:
            sync_editor_from_codes()
            state["codes_dirty"] = False

    nb.bind("<<NotebookTabChanged>>", on_tab)
    code_box.bind("<Key>", lambda e: state.__setitem__("codes_dirty", True))

    # ------------------------------------------------------------------
    # formatting actions
    # ------------------------------------------------------------------
    def sel_range():
        r = text.tag_ranges("sel")
        return (str(r[0]), str(r[1])) if r else None

    def style_here():
        if state["pending"] is not None:
            return set(state["pending"])
        if text.compare("insert", ">", "1.0"):
            return attrs_at("insert-1c") - {"raw"}
        return set()

    def sel_common():
        rng = sel_range()
        if not rng:
            return style_here()
        segs = segments(*rng)
        if not segs:
            return set()
        common = set(segs[0][1])
        for _t, st in segs[1:]:
            common &= st
        return common

    def refresh_toolbar(_e=None):
        st = sel_common()
        for t, var in flags.items():
            var.set(t in st)
        f = [t[2:] for t in st if t.startswith("f:")]
        v_font.set(f[0] if f else "")
        c = [t for t in st if t.startswith("c:")]
        label = "ByLayer"
        if c:
            label = next((n for n, tag in COLOR_CHOICES if tag == c[0]),
                         "Other...")
        v_color.set(label)
        for k in sv:
            m = [t[2:] for t in st if t.startswith(k + ":")]
            sv[k].set(m[0] if m else "")
        sync_ruler()

    def apply_flag(tag, on):
        rng = sel_range()
        if rng:
            (text.tag_add if on else text.tag_remove)(tag, *rng)
            restyle()
        else:
            p = style_here()
            (p.add if on else p.discard)(tag)
            state["pending"] = p

    def on_toggle(tag):
        apply_flag(tag, flags[tag].get())

    def apply_family(prefix, newtag):
        rng = sel_range()
        if rng:
            for name in text.tag_names():
                if name.startswith(prefix) and is_attr(name):
                    text.tag_remove(name, *rng)
            if newtag:
                text.tag_add(newtag, *rng)
            restyle()
        else:
            p = {t for t in style_here() if not t.startswith(prefix)}
            if newtag:
                p.add(newtag)
            state["pending"] = p

    def on_font(_e=None):
        name = v_font.get().strip()
        apply_family("f:", ("f:" + name) if name else None)

    def on_color(_e=None):
        label = v_color.get()
        tag = dict(COLOR_CHOICES).get(label)
        if tag == "other":
            c = colorchooser.askcolor(title="Text colour")
            if not c or not c[0]:
                refresh_toolbar()
                return
            r, g, b = (int(x) for x in c[0])
            tag = "c:R%d" % (r | (g << 8) | (b << 16))
        apply_family("c:", tag)

    def on_size(key):
        val = sv[key].get().strip()
        if val:
            try:
                float(val[:-1] if val.endswith("x") else val)
            except ValueError:
                messagebox.showerror("MTEXT", "Enter a number (for example "
                                     "2.5 or 1.5x).")
                return
        apply_family(key + ":", (key + ":" + val) if val else None)

    font_box.bind("<<ComboboxSelected>>", on_font)
    font_box.bind("<Return>", on_font)
    color_box.bind("<<ComboboxSelected>>", on_color)

    def clear_format():
        rng = sel_range() or ("1.0", "end-1c")
        for name in text.tag_names():
            if is_attr(name) and name != "raw" and not name.startswith("p:"):
                text.tag_remove(name, *rng)
        state["pending"] = None
        restyle()
        refresh_toolbar()

    # ---- replacing a range while keeping each piece's formatting ----
    def replace_range(start, end, runs):
        text.delete(start, end)
        insert_runs(start, runs)
        restyle()

    def do_case(fn):
        rng = sel_range()
        if not rng:
            return
        runs = [(fn(t) if "raw" not in st else t, st)
                for t, st in segments(*rng)]
        replace_range(rng[0], rng[1], runs)

    case_menu.add_command(label="UPPERCASE",
                          command=lambda: do_case(str.upper))
    case_menu.add_command(label="lowercase",
                          command=lambda: do_case(str.lower))

    def do_stack():
        rng = sel_range()
        if not rng:
            messagebox.showinfo("Stack", "Select the text to stack first, "
                                "for example 1/2, 1#2 or 1^2.")
            return
        segs = segments(*rng)
        sel = "".join(t for t, st in segs if "raw" not in st)
        m = re.match(r"^(.*?)([/#^])(.*)$", sel, re.S)
        if not m:
            messagebox.showinfo("Stack", "The selection needs a /, # or ^ "
                                "between the two parts.")
            return
        base = set(segs[0][1]) - {"raw"}
        token = "\\S%s%s%s;" % (m.group(1), m.group(2), m.group(3))
        replace_range(rng[0], rng[1], [(token, frozenset(base | {"raw"}))])

    def do_unstack():
        rng = sel_range()
        idx = rng[0] if rng else "insert"
        # find the raw \S...; token at the selection start / caret
        line_start = text.index("%s linestart" % idx)
        line_end = text.index("%s lineend" % idx)
        pos = line_start
        for t, st in segments(line_start, line_end):
            end = text.index("%s+%dc" % (pos, len(t)))
            if "raw" in st and t.startswith("\\S") and \
                    text.compare(pos, "<=", idx) and text.compare(idx, "<=", end):
                inner = t[2:-1] if t.endswith(";") else t[2:]
                replace_range(pos, end, [(inner, frozenset(st - {"raw"}))])
                return
            pos = end
        messagebox.showinfo("Unstack", "Put the cursor in a stacked "
                            "fraction (grey \\S code) first.")

    def insert_symbol(sym):
        if sym == "\\~":
            insert_here("\\~", raw=True)
        else:
            insert_here(sym)

    def insert_here(s, raw=False):
        rng = sel_range()
        if rng:
            text.delete(*rng)
        st = style_here() - {"raw"}
        if raw:
            st.add("raw")
        text.insert("insert", s, tuple(sorted(st)))
        state["pending"] = None
        restyle()

    for label, sym in SYMBOLS:
        sym_menu.add_command(label=label,
                             command=lambda s=sym: insert_symbol(s))

    # ---- bullets and numbering ----
    PREFIX = re.compile(r"^(\d+\.|[A-Za-z]\.|\u2022) ")

    def do_list(kind):
        a = text.index("sel.first linestart") if sel_range() \
            else text.index("insert linestart")
        b = text.index("sel.last lineend") if sel_range() \
            else text.index("insert lineend")
        line = int(a.split(".")[0])
        last = int(b.split(".")[0])
        n = 0
        for ln in range(line, last + 1):
            start = "%d.0" % ln
            body = text.get(start, "%d.end" % ln)
            m = PREFIX.match(body)
            if m:
                text.delete(start, "%s+%dc" % (start, len(m.group(0))))
            if kind == "off" or not text.get(start, "%d.end" % ln).strip():
                continue
            n += 1
            mark = {"num": "%d." % n, "upper": "%s." % chr(64 + (n - 1) % 26 + 1),
                    "lower": "%s." % chr(96 + (n - 1) % 26 + 1),
                    "bullet": "\u2022"}[kind]
            text.insert(start, mark + " ")
        restyle()

    for label, kind in (("Off", "off"), ("Numbered", "num"),
                        ("Lettered (A, B...)", "upper"),
                        ("Lettered (a, b...)", "lower"),
                        ("Bulleted", "bullet")):
        list_menu.add_command(label=label, command=lambda k=kind: do_list(k))

    # ---- import ----
    def import_text():
        p = filedialog.askopenfilename(
            filetypes=[("Text files", "*.txt *.rtf *.csv"), ("All", "*.*")])
        if not p:
            return
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError as e:
            messagebox.showerror("Import", str(e))
            return
        insert_here(data.replace("\r\n", "\n"))

    # ---- find / replace ----
    def find_dialog():
        win = tk.Toplevel(root)
        win.title("Find and Replace")
        fv, rv = tk.StringVar(), tk.StringVar()
        case = tk.BooleanVar()
        ttk.Label(win, text="Find:").grid(row=0, column=0, sticky="e", padx=4,
                                          pady=3)
        ttk.Entry(win, textvariable=fv, width=30).grid(row=0, column=1)
        ttk.Label(win, text="Replace with:").grid(row=1, column=0, sticky="e",
                                                  padx=4, pady=3)
        ttk.Entry(win, textvariable=rv, width=30).grid(row=1, column=1)
        ttk.Checkbutton(win, text="Match case", variable=case).grid(
            row=2, column=1, sticky="w")
        msg = tk.StringVar()
        ttk.Label(win, textvariable=msg).grid(row=4, column=0, columnspan=2)

        def find_next():
            text.tag_remove("sel", "1.0", "end")
            if not fv.get():
                return None
            start = text.index("insert")
            idx = text.search(fv.get(), start, "end", nocase=not case.get())
            if not idx:
                idx = text.search(fv.get(), "1.0", start,
                                  nocase=not case.get())
            if not idx:
                msg.set("Not found.")
                return None
            end = "%s+%dc" % (idx, len(fv.get()))
            text.tag_add("sel", idx, end)
            text.mark_set("insert", end)
            text.see(idx)
            msg.set("")
            return idx

        def replace_one():
            rng = sel_range()
            if rng and text.get(*rng).lower() == fv.get().lower():
                segs = segments(*rng)
                st = segs[0][1] if segs else frozenset()
                replace_range(rng[0], rng[1], [(rv.get(), st - {"raw"})])
                text.mark_set("insert", "%s+%dc" % (rng[0], len(rv.get())))
            find_next()

        def replace_all():
            if not fv.get():
                return
            n = 0
            pos = "1.0"
            while True:
                idx = text.search(fv.get(), pos, "end", nocase=not case.get())
                if not idx:
                    break
                end = "%s+%dc" % (idx, len(fv.get()))
                segs = segments(idx, end)
                st = segs[0][1] if segs else frozenset()
                replace_range(idx, end, [(rv.get(), st - {"raw"})])
                pos = "%s+%dc" % (idx, max(len(rv.get()), 1))
                n += 1
            msg.set("%d replaced." % n)

        bf = ttk.Frame(win)
        bf.grid(row=3, column=0, columnspan=2, pady=4)
        ttk.Button(bf, text="Find next", command=find_next).pack(side="left",
                                                                 padx=3)
        ttk.Button(bf, text="Replace", command=replace_one).pack(side="left",
                                                                 padx=3)
        ttk.Button(bf, text="Replace all", command=replace_all).pack(
            side="left", padx=3)
        win._find_funcs = (find_next, replace_one, replace_all, fv, rv)
        state["find"] = win

    # ------------------------------------------------------------------
    # typing keeps the formatting of the previous character (like AutoCAD)
    # ------------------------------------------------------------------
    def type_text(s):
        rng = sel_range()
        if rng:
            text.delete(*rng)
        st = style_here() - {"raw"}
        text.insert("insert", s, tuple(sorted(st)))
        state["pending"] = None
        restyle()

    def on_key(ev):
        if ev.state & 0x4:                      # Ctrl shortcuts
            return None
        ch = ev.char
        if ev.keysym == "Return":
            ch = "\n"
        elif ev.keysym == "Tab":
            ch = "\t"
        if not ch or (ord(ch[0]) < 32 and ch not in "\n\t"):
            return None
        type_text(ch)
        return "break"

    def after_edit(_e=None):
        restyle()
        refresh_toolbar()

    text.bind("<KeyPress>", on_key)
    text.bind("<KeyRelease>", lambda e: (state.__setitem__(
        "pending", None) if e.keysym in ("Left", "Right", "Up", "Down",
                                         "Home", "End") else None,
        after_edit()))
    text.bind("<ButtonRelease-1>", lambda e: (state.__setitem__(
        "pending", None), refresh_toolbar()))
    text.bind("<<Selection>>", refresh_toolbar)
    text.bind("<<Paste>>", lambda e: root.after(10, after_edit))
    text.bind("<<Cut>>", lambda e: root.after(10, after_edit))

    # ------------------------------------------------------------------
    # loading from / applying to CADCraft
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # ruler: width handle, first-line / left indent markers, tab stops
    # ------------------------------------------------------------------
    ru = {"para": PARA_DEFAULT, "drag": None, "tip": ""}
    SNAP = 0.25

    def obj_height():
        try:
            h = float(pv["height"].get())
            return h if h > 0 else 2.5
        except ValueError:
            return 2.5

    def width_units():
        try:
            return max(0.0, float(pv["width"].get() or 0))
        except ValueError:
            return 0.0

    def ppu():                       # pixels per drawing unit
        return px_h / obj_height()

    def x0():                        # left edge of the text area
        return int(text.cget("borderwidth")) + PAD

    def avail():                     # width of the text area in pixels
        return max(160, text.winfo_width() - 2 * x0())

    def para_display(st):
        """Tk margins and tab stops for a paragraph's settings."""
        i, l, r, q, tabs = para_from_style(st)
        cfg = {"lmargin1": int(max(0, (l + i) * px_h)),
               "lmargin2": int(max(0, l * px_h))}
        rm = r * px_h
        W = width_units()
        if W > 0 and v_wrap.get():
            rm += max(0, avail() - W * ppu())
        cfg["rmargin"] = int(max(0, rm))
        if tabs:
            stops = []
            for typ, pos in tabs:
                stops += [str(int(pos * px_h)),
                          {"l": "left", "c": "center", "r": "right"}[typ]]
        else:                        # MTEXT's default stops: every 4 heights
            stops = [str(int(4 * k * px_h)) for k in range(1, 48)]
        cfg["tabs"] = tuple(stops)
        return cfg

    def snap(v):
        return max(0.0, round(v / SNAP) * SNAP)

    def paragraph_lines():
        rng = sel_range()
        if rng:
            a = int(text.index(rng[0]).split(".")[0])
            b = int(text.index("%s-1c" % rng[1]).split(".")[0])
            return list(range(a, max(a, b) + 1))
        return [int(text.index("insert").split(".")[0])]

    def para_at_line(n):
        idx = "%d.0" % n
        if text.compare(idx, "<", "end-1c"):
            return para_from_style(attrs_at(idx))
        return PARA_DEFAULT

    def set_paragraphs(lines, newp):
        for n in lines:
            start = "%d.0" % n
            end = text.index("%d.end+1c" % n)
            if text.compare(end, ">", "end-1c"):
                end = text.index("end-1c")
            if text.compare(start, ">=", end):
                continue
            for name in text.tag_names():
                if name.startswith("p:"):
                    text.tag_remove(name, start, end)
            tag = para_tag(newp)
            if tag:
                text.tag_add(tag, start, end)
        restyle()

    def commit():
        set_paragraphs(paragraph_lines(), ru["para"])
        redraw_ruler()

    def sync_ruler(_e=None):
        ru["para"] = para_at_line(paragraph_lines()[0])
        redraw_ruler()

    def nice_step():
        for st in (0.1, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200,
                   250, 500, 1000, 5000, 10000):
            if st * ppu() >= 44:
                return st
        return 100000

    def redraw_ruler():
        c = ruler
        c.delete("all")
        left, wide = x0(), avail()
        i, l, r, q, tabs = ru["para"]
        W = width_units()
        xw = left + (W * ppu() if W > 0 else wide)
        c.create_rectangle(left, 16, left + wide, 28, fill="white",
                           outline="#b0b0b0")
        if W > 0 and xw < left + wide:
            c.create_rectangle(xw, 16, left + wide, 28, fill="#d6d6d6",
                               outline="")
        step = nice_step()
        u = 0.0
        while left + u * ppu() <= left + wide + 1:
            x = left + u * ppu()
            c.create_line(x, 16, x, 24, fill="#555")
            c.create_text(x + 2, 22, text=("%g" % round(u, 4)), anchor="nw",
                          font=("TkDefaultFont", 7), fill="#444")
            hx = x + step * ppu() / 2
            if hx <= left + wide:
                c.create_line(hx, 16, hx, 20, fill="#888")
            u += step
        for typ, pos in tabs:                    # tab stops
            x = left + pos * px_h
            c.create_line(x, 18, x, 27, fill="#1a1a1a", width=2)
            if typ == "l":
                c.create_line(x, 27, x + 6, 27, fill="#1a1a1a", width=2)
            elif typ == "r":
                c.create_line(x - 6, 27, x, 27, fill="#1a1a1a", width=2)
            else:
                c.create_line(x - 4, 27, x + 4, 27, fill="#1a1a1a", width=2)
        xf = left + max(0.0, l + i) * px_h       # first-line indent (top)
        c.create_polygon(xf - 5, 3, xf + 5, 3, xf, 15, fill="#2f6db5",
                         outline="#1c4a80")
        xl = left + l * px_h                     # left indent (bottom)
        c.create_polygon(xl - 5, 40, xl + 5, 40, xl, 29, fill="#2f6db5",
                         outline="#1c4a80")
        colour = "#c00000"                       # width handle
        c.create_line(xw, 2, xw, 40, fill=colour, width=2,
                      dash=() if W > 0 else (3, 2))
        c.create_rectangle(xw - 4, 15, xw + 4, 29, fill=colour, outline="")
        c.create_polygon(xw + 4, 18, xw + 10, 22, xw + 4, 26, fill=colour,
                         outline="")
        label = ru["tip"] or ("Width %g" % W if W > 0 else
                              "Width: auto  (drag the red handle to set)")
        c.create_text(left + wide - 4, 2, text=label, anchor="ne",
                      font=("TkDefaultFont", 8), fill="#8a0000")

    def r_hit(x, y):
        i, l, r, q, tabs = ru["para"]
        left, wide = x0(), avail()
        W = width_units()
        xw = left + (W * ppu() if W > 0 else wide)
        if abs(x - xw) <= 7:
            return ("width", None)
        if y < 17 and abs(x - (left + max(0.0, l + i) * px_h)) <= 7:
            return ("first", None)
        if y > 27 and abs(x - (left + l * px_h)) <= 7:
            return ("left", None)
        for k, (typ, pos) in enumerate(tabs):
            if abs(x - (left + pos * px_h)) <= 5 and 16 <= y <= 30:
                return ("tab", k)
        if 16 <= y <= 28 and left <= x <= left + wide:
            return ("new", None)
        return (None, None)

    def r_press(ev):
        kind, k = r_hit(ev.x, ev.y)
        i, l, r, q, tabs = ru["para"]
        if kind == "new":
            pos = snap((ev.x - x0()) / px_h)
            tl = sorted(list(tabs) + [("l", pos)], key=lambda t: t[1])
            ru["para"] = (i, l, r, q, tuple(tl))
            k, kind = tl.index(("l", pos)), "tab"
            commit()
        ru["drag"] = (kind, k) if kind else None

    def r_motion(ev):
        d = ru["drag"]
        if not d:
            return
        kind, k = d
        i, l, r, q, tabs = ru["para"]
        pos = (ev.x - x0()) / px_h
        if kind == "width":
            W = (ev.x - x0()) / ppu()
            new = 0.0 if ev.x >= x0() + avail() - 2 else max(0.5, round(W * 2) / 2)
            ru["tip"] = "Width %g" % new if new else "Width: auto"
            pv["width"].set("%g" % new)
            return
        if kind == "first":
            a = snap(pos)
            ru["para"] = (a - l, l, r, q, tabs)
            ru["tip"] = "First line %g" % a
        elif kind == "left":
            a = snap(pos)
            ru["para"] = (l + i - a, a, r, q, tabs)
            ru["tip"] = "Left indent %g" % a
        elif kind == "tab":
            tl = list(tabs)
            if ev.x < x0() - 14:                  # dragged off the ruler
                del tl[k]
                ru["para"] = (i, l, r, q, tuple(tl))
                ru["drag"] = None
                ru["tip"] = "Tab removed"
                commit()
                return
            typ = tl[k][0]
            tl[k] = (typ, snap(pos))
            moved = tl[k]
            tl.sort(key=lambda t: t[1])
            ru["drag"] = ("tab", tl.index(moved))
            ru["para"] = (i, l, r, q, tuple(tl))
            ru["tip"] = "Tab %g" % moved[1]
        commit()

    def r_release(_ev):
        ru["drag"] = None
        ru["tip"] = ""
        redraw_ruler()

    def r_remove_tab(ev):
        kind, k = r_hit(ev.x, ev.y)
        if kind == "tab":
            i, l, r, q, tabs = ru["para"]
            tl = list(tabs)
            del tl[k]
            ru["para"] = (i, l, r, q, tuple(tl))
            ru["drag"] = None
            commit()

    def r_cycle_tab(ev):                          # right-click: L -> C -> R
        kind, k = r_hit(ev.x, ev.y)
        if kind == "tab":
            i, l, r, q, tabs = ru["para"]
            tl = list(tabs)
            typ, pos = tl[k]
            tl[k] = ({"l": "c", "c": "r", "r": "l"}[typ], pos)
            ru["para"] = (i, l, r, q, tuple(tl))
            commit()

    ruler.bind("<ButtonPress-1>", r_press)
    ruler.bind("<B1-Motion>", r_motion)
    ruler.bind("<ButtonRelease-1>", r_release)
    ruler.bind("<Double-Button-1>", r_remove_tab)
    ruler.bind("<Button-3>", r_cycle_tab)
    ruler.bind("<Configure>", lambda e: (restyle(), redraw_ruler()))
    text.bind("<Configure>", lambda e: (restyle(), redraw_ruler()))

    def on_geometry_var(*_):
        if not state.get("loading"):
            restyle()
            redraw_ruler()

    pv["width"].trace_add("write", on_geometry_var)
    pv["height"].trace_add("write", on_geometry_var)

    def show_item(*_):
        i = pick_box.current()
        if i < 0 or i >= len(state["items"]):
            return
        info = state["items"][i]
        state["info"] = info
        state["loading"] = True
        for prop, var in pv.items():
            if prop in info["keys"]:
                v = info_value(info, prop)
                if prop == "attach":
                    v = attach_to_code(v) or str(v)
                elif prop in NUMERIC and v != "":
                    try:
                        v = "%g" % float(v)
                    except (TypeError, ValueError):
                        pass
                var.set(str(v))
                prop_widgets[prop].configure(state="normal")
            else:
                var.set("")
                prop_widgets[prop].configure(state="disabled")
        state["orig"] = {p: pv[p].get() for p in pv}
        codes = info_text(info)
        state["base_font"] = DEFAULT_FONT
        state["loading"] = False
        if info["plain"]:
            ruler.grid_remove()
        else:
            ruler.grid()
        if info["plain"]:
            text.delete("1.0", "end")
            text.insert("1.0", codes)
            restyle()
            text.edit_reset()
        else:
            load_codes(codes)
        state["loaded_codes"] = current_codes()
        state["repair"] = (not info["plain"]) and "^I" in codes
        if state["repair"]:
            log("This text contains ^I, which CADCraft draws literally "
                "(an earlier version of this editor wrote it). It is shown "
                "here as real tabs - press Apply to fix it in the drawing.")
        code_box.delete("1.0", "end")
        code_box.insert("1.0", codes)
        state["codes_dirty"] = False
        state["pending"] = None
        refresh_toolbar()
        sync_ruler()

    pick_box.bind("<<ComboboxSelected>>", show_item)

    def signature(items):
        return tuple((it["handle"], info_text(it)) for it in items)

    def is_dirty():
        if not state["info"]:
            return False
        if current_codes() != state["loaded_codes"]:
            return True
        return any(pv[p].get().strip() != state["orig"].get(p, "")
                   for p in pv)

    def refresh(*_, quiet=False):
        keep = pick_box.current()
        try:
            items, others, first_other = scan_selection(cl, selected=True)
        except CadError as e:
            v_status.set("Could not read CADCraft: %s" % e)
            if not quiet:
                log("Refresh failed: %s" % e)
            return
        state["items"] = items
        state["sig"] = signature(items)
        names = []
        for it in items:
            t = plain_text(parse_mtext(info_text(it))) if not it["plain"] \
                else info_text(it)
            t = t.replace("\n", " / ")
            names.append("%s%s: %s" % ("TEXT " if it["plain"] else "",
                                       it["handle"], t[:40]))
        pick_box.configure(values=names)
        seen = ", ".join("%d x %s" % (n, t) for t, n in sorted(others.items()))
        if names:
            pick_box.current(keep if 0 <= keep < len(names) else 0)
            show_item()
            info = state["info"]
            msg = "Loaded %d text object(s)." % len(names)
            if others:
                msg += " Other selected objects ignored: %s." % seen
            v_status.set(msg)
            if not quiet:
                log(msg)
                if info["plain"]:
                    log("This is single-line TEXT: it is edited as plain "
                        "text (formatting is not stored).")
                if not text_field_found(info):
                    log("WARNING: could not find this object's text field. "
                        "Its fields are: %s. Run with --diag and send me "
                        "the output." % ", ".join(sorted(info["raw"])))
        else:
            if others:
                msg = ("No MTEXT is selected in CADCraft. What is selected: "
                       "%s. Select an MTEXT and it will load here." % seen)
            else:
                msg = ("Nothing is selected in CADCraft. Select an MTEXT "
                       "and it will load here.")
            v_status.set(msg)
            if not quiet:
                log(msg)
                if first_other is not None:
                    log("First selected object, as CADCraft describes it "
                        "(if this IS an MTEXT, send me this line): %s"
                        % json.dumps(first_other)[:600])

    def poll():
        try:
            if (v_follow.get() and not state["busy"]
                    and root.winfo_exists()):
                try:
                    items, _o, _f = scan_selection(cl, selected=True)
                except CadError:
                    items = None
                if items is not None and signature(items) != state["sig"]:
                    if not is_dirty():
                        refresh(quiet=True)
                    else:
                        v_status.set(
                            "The selection in CADCraft changed. Your "
                            "unsaved edits are kept - press Apply, or "
                            "Refresh to load the new selection.")
        finally:
            try:
                root.after(1200, poll)
            except tk.TclError:
                pass

    def collect_props():
        info = state["info"]
        out = {}
        for prop in pv:
            if prop not in info["keys"]:
                continue
            new = pv[prop].get().strip()
            if new == state["orig"].get(prop, ""):
                continue
            if prop in NUMERIC:
                try:
                    out[prop] = float(new)
                except ValueError:
                    raise CadError("%s must be a number." % prop)
            elif prop == "attach":
                code = new.upper()
                if code not in ATTACH_CODES:
                    raise CadError("Justify must be one of %s."
                                   % ", ".join(ATTACH_CODES))
                raw = info_value(info, "attach")
                out[prop] = (ATTACH_CODES.index(code) + 1
                             if isinstance(raw, int) else code)
            else:
                out[prop] = new
        return out

    def do_apply(codes, props):
        try:
            apply_changes(cl, state["info"], codes, props, log)
        except RefEditOpen:
            ans = messagebox.askyesnocancel(
                "Block edit is open",
                "CADCraft has an in-place block edit (REFEDIT) open and "
                "will not change other objects until it ends.\n\n"
                "Yes = end it and DISCARD changes made inside it\n"
                "No = end it and SAVE them\nCancel = stop")
            if ans is None:
                log("Cancelled - the block edit is still open.")
                return False
            cl.execute("refclose", {"option": "discard" if ans else "save"})
            apply_changes(cl, state["info"], codes, props, log)
        return True

    def apply(*_):
        if not state["info"]:
            messagebox.showinfo("MTEXT", "Select an MTEXT in CADCraft first.")
            return
        state["busy"] = True
        try:
            _apply_inner()
        finally:
            state["busy"] = False
        refresh(quiet=True)

    def _apply_inner():
        try:
            if nb.index("current") == 1 and state["codes_dirty"]:
                sync_editor_from_codes()
                state["codes_dirty"] = False
            codes = current_codes()
            props = collect_props()
            if codes == state["loaded_codes"] and not state.get("repair"):
                codes = None
            if codes is None and not props:
                messagebox.showinfo("MTEXT", "Nothing was changed.")
                return
            if do_apply(codes, props):
                handle = state["info"]["handle"]
                now = [m for m in fetch_mtexts(cl, selected=False)
                       if m["handle"] == handle]
                if codes is not None and now:
                    stored = info_text(now[0])
                    if stored != codes:
                        log("Note: CADCraft stored the text as: %s" % stored)
                log("Done.")
        except CadError as e:
            log("ERROR: %s" % e)
            messagebox.showerror("MTEXT", str(e))

    ttk.Button(btns, text="Refresh", command=refresh).pack(side="left",
                                                           padx=3)
    ttk.Button(btns, text="Apply", command=apply).pack(side="left", padx=3)
    ttk.Button(btns, text="Close", command=root.destroy).pack(side="left",
                                                              padx=3)
    state["api"] = dict(text=text, apply=apply, refresh=refresh, pv=pv,
                        sv=sv, flags=flags, on_toggle=on_toggle,
                        on_font=on_font, on_size=on_size,
                        do_stack=do_stack, do_unstack=do_unstack,
                        do_case=do_case, do_list=do_list,
                        insert_symbol=insert_symbol, find_dialog=find_dialog,
                        current_codes=current_codes, v_font=v_font,
                        v_color=v_color, on_color=on_color, nb=nb,
                        code_box=code_box, root=root, state=state,
                        clear_format=clear_format, type_text=type_text,
                        r_remove_tab=r_remove_tab, ruler=ruler, ru=ru,
                        log_box=log_box, refresh_toolbar=refresh_toolbar)
    refresh()
    root.after(1200, poll)
    root.mainloop()
    cl.close()
    return 0


# ======================================================================
def diagnostics(cl, out=print):
    out("== ping: %s" % cl.call("ping"))
    cmds = cl.call("engine.commands")
    items = cmds if isinstance(cmds, list) else (
        find_key(cmds, "commands", list) or [])
    for c in items:
        if isinstance(c, dict) and c.get("id") in (
                "textedit", "mtext", "properties.set", "mtedit"):
            out("== %s\n   params: %s" % (c.get("id"), c.get("params")))
    out("== selected objects:")
    sel, others, first_other = scan_selection(cl, selected=True)
    out("   editable text objects: %d; others: %s" % (len(sel), others or "none"))
    if first_other is not None:
        out("   first other object: " + json.dumps(first_other)[:800])
    if not sel:
        out("   no MTEXT/TEXT selected - select one in CADCraft and run --diag again")
    for it in sel:
        out("   handle %s keys %s" % (it["handle"], it["keys"]))
        out("   " + json.dumps(it["raw"])[:1500])


def main():
    ap = argparse.ArgumentParser(description="MTEXT editor for CADCraft")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CADCRAFT_CONTROL_PORT",
                                               DEFAULT_PORT)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--codes", action="store_true",
                    help="print the selected MTEXT's contents")
    args = ap.parse_args()
    try:
        if args.diag or args.codes:
            cl = Client(args.port, args.token)
            if args.diag:
                diagnostics(cl)
            else:
                for it in fetch_mtexts(cl, selected=True):
                    print(it["handle"], info_text(it))
            return 0
    except CadError as e:
        print("ERROR:", e)
        return 1
    return run_gui(args.port, args.token)


if __name__ == "__main__":
    sys.exit(main())
