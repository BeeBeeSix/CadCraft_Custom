#!/usr/bin/env python3
"""
cadcraft_chprop.py - a CHPROP dialog for CADCraft.

CADCraft has no DCL, so this runs as a separate program (Python 3 + tkinter,
both standard) and drives the running CADCraft through its JSON control
channel (docs/control-protocol.md).

How to use
  1. Start CADCraft with the control channel on:   cadcraft --control 7979
  2. Select the objects you want to change in CADCraft.
  3. Run:   python cadcraft_chprop.py            (or --port 7979)
  4. Tick the properties to change, fill in values, press Apply.

Apply types this at the CADCraft command line, exactly as you would:
     CHPROP  <Enter>   (objects already selected)
     (Elevation, Global width and Closure apply to LWPOLYLINEs only)
     C <Enter> 1 <Enter>   LA <Enter> WALLS <Enter>   ...   <Enter>

Diagnostics:   python cadcraft_chprop.py --diag
  prints what CADCraft returns (layers, commands, selection). Use it if
  something does not work, then send me the output.
"""
import argparse
import glob
import json
import os
import socket
import sys
import tempfile

DEFAULT_PORT = 7979
LWEIGHTS = ["ByLayer", "ByBlock", "Default", "0.00", "0.05", "0.09", "0.13",
            "0.15", "0.18", "0.20", "0.25", "0.30", "0.35", "0.40", "0.50",
            "0.53", "0.60", "0.70", "0.80", "0.90", "1.00", "1.06", "1.20",
            "1.40", "1.58", "2.00", "2.11"]
COLORS = ["ByLayer", "ByBlock", "Red", "Yellow", "Green", "Cyan", "Blue",
          "Magenta", "White"]
LTYPES_FALLBACK = ["ByLayer", "ByBlock", "Continuous"]

# ----------------------------------------------------------------------
# CHPROP.lsp (embedded so this script is a single file). CADCraft has no
# built-in CHPROP, so the script loads this the first time it is needed.
# ----------------------------------------------------------------------
CHPROP_LSP = r''';;; ------------------------------------------------------------------
;;; CHPROP.lsp
;;; Command: CHPROP
;;; Mimics AutoCAD's CHPROP: select objects, then change properties
;;; one at a time until Enter is pressed.
;;;
;;;   Enter property to change
;;;   [Color/LAyer/LType/ltScale/LWeight/Thickness/Elevation/Width/CLosure]:
;;;
;;; Type the capitalised letters to pick an option (C, LA, LT, S, LW, T,
;;; E, W, CL).
;;; Elevation, Width (global width) and CLosure (open/closed) are extras
;;; that apply to LWPOLYLINE objects only; other objects are skipped.
;;; Setting a global width removes any per-vertex widths.
;;; Uses only core LISP functions (no vl-*, no getfiled, no entupdate,
;;; no wcmatch: CADCraft lacks ENTUPDATE and wcmatch did not accept the
;;; lineweight pattern).
;;; Note: loading this replaces any built-in CHPROP command in the
;;; current session.
;;; ------------------------------------------------------------------

;; True if s is an abbreviation of word with at least minlen characters
(defun cp:kw (s word minlen)
  (and (>= (strlen s) minlen)
       (<= (strlen s) (strlen word))
       (= s (substr word 1 (strlen s))))
)

;; Remove a DXF group from an entity list
(defun cp:delg (ed code / res g)
  (foreach g ed
    (if (/= (car g) code) (setq res (cons g res))))
  (reverse res)
)

;; Set a DXF group (replace if present, otherwise insert after layer group)
(defun cp:setg (ed code val / res g)
  (if (assoc code ed)
    (subst (cons code val) (assoc code ed) ed)
    (progn
      (foreach g ed
        (setq res (cons g res))
        (if (= (car g) 8)
          (setq res (cons (cons code val) res))))
      (reverse res)
    )
  )
)

;; Apply a group change to every object in the selection set.
;; val = nil removes the group (used for BYLAYER).
(defun cp:modify (ss code val / i ent ed new ok bad)
  (setq i 0 ok 0 bad 0)
  (repeat (sslength ss)
    (setq ent (ssname ss i)
          i   (1+ i)
          ed  (entget ent)
          new (if val (cp:setg ed code val) (cp:delg ed code)))
    (if (equal new ed)
      (setq ok (1+ ok))
      (if (entmod new)
        (setq ok (1+ ok))
        (setq bad (1+ bad))
      )
    )
  )
  (prompt (strcat "\n" (itoa ok) " object(s) modified."))
  (if (> bad 0)
    (prompt (strcat " " (itoa bad)
                    " could not be changed (locked layer or unsupported property).")))
)

;; ---- Color ---------------------------------------------------------
(defun cp:color (ss / s n names)
  (setq names '(("RED" . 1) ("YELLOW" . 2) ("GREEN" . 3) ("CYAN" . 4)
                ("BLUE" . 5) ("MAGENTA" . 6) ("WHITE" . 7)))
  (setq s (strcase (getstring "\nNew color [BYLAYER/BYBLOCK] <BYLAYER>: ")))
  (cond
    ((or (= s "") (cp:kw s "BYLAYER" 3)) (cp:modify ss 62 nil))
    ((cp:kw s "BYBLOCK" 3)               (cp:modify ss 62 0))
    ((assoc s names)                     (cp:modify ss 62 (cdr (assoc s names))))
    ((and (= s (itoa (setq n (atoi s)))) (<= 0 n 256))
     (if (= n 256) (cp:modify ss 62 nil) (cp:modify ss 62 n)))
    (T (prompt "\nInvalid color. Enter 0-256, a color name, BYLAYER or BYBLOCK."))
  )
)

;; ---- Layer ---------------------------------------------------------
;; Create a new layer (white, Continuous). Tries entmake first, then the
;; LAYER command if entmake is not supported. Returns the table record or nil.
(defun cp:make-layer (s)
  (cp:entmake-layer s)
  (tblsearch "LAYER" s)
)

(defun cp:entmake-layer (s)
  (entmake (list '(0 . "LAYER")
                 '(100 . "AcDbSymbolTableRecord")
                 '(100 . "AcDbLayerTableRecord")
                 (cons 2 s)
                 '(70 . 0)
                 '(62 . 7)
                 '(6 . "Continuous")))
  (if (not (tblsearch "LAYER" s))
    (command "._-layer" "_N" s "")
  )
)

(defun cp:layer (ss / s rec ans)
  (setq s (getstring T (strcat "\nNew layer name <" (getvar "CLAYER") ">: ")))
  (if (= s "") (setq s (getvar "CLAYER")))
  (if (not (setq rec (tblsearch "LAYER" s)))
    (progn
      (setq ans (strcase (getstring
        (strcat "\nLayer \"" s "\" not found. Create it? [Yes/No] <Yes>: "))))
      (if (or (= ans "") (cp:kw ans "YES" 1))
        (if (not (setq rec (cp:make-layer s)))
          (prompt (strcat "\nCould not create layer \"" s "\". Check the name."))
          (prompt (strcat "\nLayer \"" s "\" created."))
        )
      )
    )
  )
  (if rec
    (cp:modify ss 8 (cdr (assoc 2 rec)))
  )
)

;; ---- Linetype ------------------------------------------------------
(defun cp:ltype (ss / s u rec)
  (setq s (getstring T "\nNew linetype name or [BYLAYER/BYBLOCK] <BYLAYER>: "))
  (setq u (strcase s))
  (cond
    ((or (= u "") (cp:kw u "BYLAYER" 3)) (cp:modify ss 6 nil))
    ((cp:kw u "BYBLOCK" 3)               (cp:modify ss 6 "ByBlock"))
    ((setq rec (tblsearch "LTYPE" s))    (cp:modify ss 6 (cdr (assoc 2 rec))))
    (T (prompt (strcat "\nLinetype \"" s "\" is not loaded. Load it first with LINETYPE.")))
  )
)

;; ---- Linetype scale ------------------------------------------------
(defun cp:ltscale (ss / v)
  (setq v (getreal "\nNew linetype scale <1.0000>: "))
  (if (not v) (setq v 1.0))
  (if (> v 0.0)
    (cp:modify ss 48 v)
    (prompt "\nLinetype scale must be greater than zero.")
  )
)

;; ---- Lineweight (mm) -----------------------------------------------
(defun cp:lweight (ss / s h best lst d)
  (setq lst '(0 5 9 13 15 18 20 25 30 35 40 50 53 60 70 80 90 100
              106 120 140 158 200 211))
  (setq s (strcase (getstring "\nNew lineweight in mm [BYLAYER/BYBLOCK/DEFAULT] <BYLAYER>: ")))
  (cond
    ((or (= s "") (cp:kw s "BYLAYER" 3)) (cp:modify ss 370 -1))
    ((cp:kw s "BYBLOCK" 3)               (cp:modify ss 370 -2))
    ((cp:kw s "DEFAULT" 1)               (cp:modify ss 370 -3))
    ((member (substr s 1 1)
            '("0" "1" "2" "3" "4" "5" "6" "7" "8" "9" "."))
     (setq h (* 100.0 (atof s)) best (car lst))
     (foreach d lst
       (if (< (abs (- d h)) (abs (- best h))) (setq best d)))
     (cp:modify ss 370 best))
    (T (prompt "\nInvalid lineweight. Enter a value in mm, BYLAYER, BYBLOCK or DEFAULT."))
  )
)

;; ---- Thickness -----------------------------------------------------
(defun cp:thickness (ss / v)
  (setq v (getreal "\nNew thickness <0.0000>: "))
  (if (not v) (setq v 0.0))
  (cp:modify ss 39 v)
)

;; ---- Polyline extras (LWPOLYLINE only) ------------------------------

;; Insert (code . val) directly after the first group whose code is `after`
(defun cp:ins-after (ed after code val / res g done)
  (foreach g ed
    (setq res (cons g res))
    (if (and (not done) (= (car g) after))
      (setq res (cons (cons code val) res)
            done T)))
  (if done
    (reverse res)
    (append ed (list (cons code val)))
  )
)

;; Edit one LWPOLYLINE entity list. op = "ELEV", "WIDTH" or "CLOSE"
;; (for CLOSE, val = T closes the polyline, nil opens it)
(defun cp:lw-edit (ed op val / fl)
  (cond
    ((= op "ELEV")
     (if (assoc 38 ed)
       (subst (cons 38 val) (assoc 38 ed) ed)
       (cp:ins-after ed (if (assoc 43 ed) 43 70) 38 val)))
    ((= op "WIDTH")
     (setq ed (cp:delg (cp:delg ed 40) 41))
     (if (assoc 43 ed)
       (subst (cons 43 val) (assoc 43 ed) ed)
       (cp:ins-after ed 70 43 val)))
    ((= op "CLOSE")
     (setq fl (cdr (assoc 70 ed)))
     (if (null fl) (setq fl 0))
     (setq fl (- fl (logand fl 1)))
     (if val (setq fl (+ fl 1)))
     (if (assoc 70 ed)
       (subst (cons 70 fl) (assoc 70 ed) ed)
       (cp:ins-after ed 90 70 fl)))
    (T ed)
  )
)

(defun cp:modify-lw (ss op val / i ent ed new ok bad skip)
  (setq i 0 ok 0 bad 0 skip 0)
  (repeat (sslength ss)
    (setq ent (ssname ss i)
          i   (1+ i)
          ed  (entget ent))
    (if (= (cdr (assoc 0 ed)) "LWPOLYLINE")
      (progn
        (setq new (cp:lw-edit ed op val))
        (if (equal new ed)
          (setq ok (1+ ok))
          (if (entmod new)
            (setq ok (1+ ok))
            (setq bad (1+ bad)))))
      (setq skip (1+ skip))
    )
  )
  (prompt (strcat "\n" (itoa ok) " polyline(s) modified."))
  (if (> skip 0)
    (prompt (strcat " " (itoa skip) " skipped (not an LWPOLYLINE).")))
  (if (> bad 0)
    (prompt (strcat " " (itoa bad) " could not be changed (locked layer).")))
)

(defun cp:elev (ss / v)
  (setq v (getreal "\nNew elevation <0.0000>: "))
  (if (not v) (setq v 0.0))
  (cp:modify-lw ss "ELEV" v)
)

(defun cp:width (ss / v)
  (setq v (getreal "\nNew global width <0.0000>: "))
  (if (not v) (setq v 0.0))
  (if (>= v 0.0)
    (cp:modify-lw ss "WIDTH" v)
    (prompt "\nWidth cannot be negative.")
  )
)

(defun cp:closure (ss / s)
  (setq s (strcase (getstring "\nClosure [Open/Closed] <Closed>: ")))
  (cond
    ((or (= s "") (cp:kw s "CLOSED" 1)) (cp:modify-lw ss "CLOSE" T))
    ((cp:kw s "OPEN" 1)                 (cp:modify-lw ss "CLOSE" nil))
    (T (prompt "\nEnter Open or Closed."))
  )
)

;; ---- Main command --------------------------------------------------
(defun c:CHPROP (/ ss s done)
  (setq ss (ssget))
  (if (not ss)
    (prompt "\nNothing selected.")
    (progn
      (setq done nil)
      (while (not done)
        (setq s (strcase (getstring
          "\nEnter property to change [Color/LAyer/LType/ltScale/LWeight/Thickness/Elevation/Width/CLosure]: ")))
        (cond
          ((= s "")                                 (setq done T))
          ((cp:kw s "COLOR" 1)                      (cp:color ss))
          ((cp:kw s "LAYER" 2)                      (cp:layer ss))
          ((cp:kw s "LTYPE" 2)                      (cp:ltype ss))
          ((or (= s "S") (cp:kw s "LTSCALE" 3))     (cp:ltscale ss))
          ((cp:kw s "LWEIGHT" 2)                    (cp:lweight ss))
          ((cp:kw s "THICKNESS" 1)                  (cp:thickness ss))
          ((cp:kw s "ELEVATION" 1)                  (cp:elev ss))
          ((cp:kw s "WIDTH" 1)                      (cp:width ss))
          ((cp:kw s "CLOSURE" 2)                    (cp:closure ss))
          (T (prompt "\nInvalid option keyword."))
        )
      )
    )
  )
  (princ)
)

(prompt "\nCHPROP loaded. Type CHPROP to change object properties.")
(princ)
'''


# ----------------------------------------------------------------------
# Control-channel client
# ----------------------------------------------------------------------
class CadError(Exception):
    pass


def find_token(port):
    """Read the per-session token the app writes when --control starts."""
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
    def __init__(self, port=DEFAULT_PORT, token=None, host="127.0.0.1",
                 timeout=65):
        self.port = port
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

    def execute(self, command, params=None):
        return self.call("engine.execute",
                         {"command": command, "params": params or {}})

    def type(self, text):
        """Type one line at the command line. Raises if the line is refused."""
        r = self.call("cmdline.input", {"text": text}) or {}
        if r.get("error"):
            raise CadError("CADCraft refused %r: %s" % (text, r["error"]))
        return r


# ----------------------------------------------------------------------
# Helpers to read replies defensively (field names beyond the documented
# ones are not guaranteed)
# ----------------------------------------------------------------------
def find_collection(obj, keys):
    """Depth-first search for a list/dict stored under one of `keys`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys and isinstance(v, (list, dict)):
                return v
        for v in obj.values():
            found = find_collection(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_collection(v, keys)
            if found is not None:
                return found
    return None


def names_from(coll):
    out = []
    if isinstance(coll, dict):
        if all(isinstance(v, (dict, list, int, float, bool, type(None)))
               for v in coll.values()):
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


def find_int(obj, key):
    if isinstance(obj, dict):
        if isinstance(obj.get(key), int):
            return obj[key]
        for v in obj.values():
            r = find_int(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_int(v, key)
            if r is not None:
                return r
    return None


def get_layers(cl):
    insp = cl.call("drawing.inspect", {"entities": False})
    return names_from(find_collection(insp, {"layers"}) or [])


def get_linetypes(cl):
    insp = cl.call("drawing.inspect", {"entities": False})
    coll = find_collection(insp, {"linetypes", "ltypes", "linetype_styles"})
    names = names_from(coll or [])
    return names


def selected_count(cl):
    r = cl.execute("entities", {"selected": True, "limit": 1})
    return find_int(r, "count") or 0


# ----------------------------------------------------------------------
# Building and running the CHPROP command line
# ----------------------------------------------------------------------
def keyword_value(prop, raw):
    """Turn a dialog value into the text typed at the CHPROP prompts."""
    raw = raw.strip()
    if prop in ("color", "ltype", "lweight") and raw.lower() in (
            "bylayer", "byblock", "default"):
        return raw.upper()
    if prop in ("color", "closure"):
        return raw.upper()
    return raw


OPTION_KEY = {"color": "COLOR", "layer": "LAYER", "ltype": "LTYPE",
              "ltscale": "LTSCALE", "lweight": "LWEIGHT",
              "thickness": "THICKNESS", "elevation": "ELEVATION",
              "width": "WIDTH", "closure": "CLOSURE"}
PROPERTY_ORDER = ("layer", "color", "ltype", "ltscale", "lweight",
                  "thickness", "elevation", "width", "closure")


def load_chprop_lsp(cl, log):
    """Write the embedded CHPROP.lsp to a folder whose path has no spaces
    (a space acts as Enter at the CADCraft command line) and load it."""
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.path.expanduser("~")
    candidates = [here, tempfile.gettempdir(), home, "C:/", "/tmp"]
    for d in candidates:
        path = os.path.join(d, "CHPROP.lsp").replace("\\", "/")
        if " " in path or not os.path.isdir(d):
            continue
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(CHPROP_LSP)
        except OSError:
            continue
        r = cl.type('(load"%s")' % path) or {}
        log("Loaded %s" % path)
        for line in r.get("output") or []:
            log("  " + str(line))
        return path
    raise CadError("Could not find a folder without spaces in its path to "
                   "save CHPROP.lsp. Copy CHPROP.lsp to such a folder and "
                   "load it with APPLOAD.")


def is_idle(st):
    """True when CADCraft is back at the plain Command: prompt."""
    p = (st.get("prompt") or "").strip().lower()
    return (not st.get("running")) and p in ("command:", "command")


def step(cl, text, log, expect_running=True):
    """Type one line. Stop immediately if CHPROP has ended, so nothing is
    typed at the bare Command: prompt where letters would start other
    commands (E = ERASE, S = STRETCH, W = WBLOCK ...)."""
    r = cl.type(text)
    out = [str(x) for x in (r.get("output") or [])]
    for line in out:
        log("   | " + line)
    if expect_running and is_idle(r):
        try:
            cl.call("cmdline.key", {"key": "escape"})
        except CadError:
            pass
        raise CadError(
            "CHPROP ended right after %r - CADCraft is back at the Command: "
            "prompt, so nothing more was typed.\nCADCraft said:\n%s"
            % (text, "\n".join(out) if out else "(no output)"))
    return r


def run_chprop(cl, changes, log):
    """changes: list of (property, value). Types the CHPROP dialogue."""
    if not changes:
        raise CadError("Nothing to change.")
    if not getattr(cl, "lsp_loaded", False):
        log("Loading CHPROP.lsp into CADCraft.")
        load_chprop_lsp(cl, log)
        cl.lsp_loaded = True
    try:
        st = step(cl, "CHPROP", log)
    except CadError as e:
        if "unknown command" in str(e).lower():
            raise CadError("CHPROP.lsp did not load in CADCraft. Load it "
                           "with APPLOAD and check for LISP errors.")
        raise
    prompt = (st.get("prompt") or "")
    if "select" in prompt.lower():
        log("No objects were pre-selected; trying 'Previous' selection.")
        step(cl, "P", log)
        step(cl, "", log)
    try:
        for prop, value in changes:
            step(cl, OPTION_KEY[prop], log)
            step(cl, keyword_value(prop, value), log)
            log("  %s -> %s" % (prop, value))
        step(cl, "", log, expect_running=False)   # Enter ends CHPROP
    except CadError:
        cl.call("cmdline.key", {"key": "escape"})
        raise
    st = cl.call("cmdline.state")
    if st and st.get("running"):
        cl.call("cmdline.key", {"key": "escape"})


def create_layer(cl, name, log):
    cl.type("-LAYER")
    cl.type("N")
    cl.type(name)
    cl.type("")
    st = cl.call("cmdline.state")
    if st and st.get("running"):
        cl.type("")
    log("Created layer %s" % name)


# ----------------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------------
def diagnostics(cl, out=print):
    out("== ping: %s" % cl.call("ping"))
    st = cl.call("cmdline.state")
    out("== cmdline.state: %s" % json.dumps(st)[:400])
    insp = cl.call("drawing.inspect", {"entities": False})
    out("== drawing.inspect keys: %s" %
        (list(insp.keys()) if isinstance(insp, dict) else type(insp)))
    out("== layers found: %s" % get_layers(cl))
    out("== linetypes found: %s" % get_linetypes(cl))
    out("== selected objects: %s" % selected_count(cl))
    cmds = cl.call("engine.commands")
    items = cmds if isinstance(cmds, list) else (
        find_collection(cmds, {"commands"}) or [])
    words = ("prop", "layer", "color", "ltype", "linetype", "lweight",
             "thick", "ltscale")
    out("== commands mentioning %s:" % ", ".join(words))
    for c in items:
        text = json.dumps(c).lower()
        if any(w in text for w in words):
            out("   " + json.dumps(c)[:300])


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
        messagebox.showerror("CADCraft CHPROP", str(e))
        return 1

    root = tk.Tk()
    root.title("CADCraft - Change Properties")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    sel_var = tk.StringVar(value="")
    ttk.Label(frm, textvariable=sel_var).grid(row=0, column=0,
                                              columnspan=3, sticky="w",
                                              pady=(0, 8))

    rows = {}   # prop -> (enabled BooleanVar, value StringVar, widget)

    def add_row(r, prop, label, values, default):
        en = tk.BooleanVar(value=False)
        val = tk.StringVar(value=default)
        ttk.Checkbutton(frm, text=label, variable=en).grid(
            row=r, column=0, sticky="w", padx=(0, 10), pady=2)
        w = ttk.Combobox(frm, textvariable=val, values=values, width=28)
        w.grid(row=r, column=1, sticky="w", pady=2)
        rows[prop] = (en, val, w)
        # changing the value (typing or picking) ticks the box automatically
        val.trace_add("write", lambda *a, v=en: v.set(True))
        return w

    add_row(1, "layer", "Layer", [], "")
    add_row(2, "color", "Color", COLORS, "ByLayer")
    add_row(3, "ltype", "Linetype", LTYPES_FALLBACK, "ByLayer")
    add_row(4, "ltscale", "Linetype scale", ["1.0"], "1.0")
    add_row(5, "lweight", "Lineweight (mm)", LWEIGHTS, "ByLayer")
    add_row(6, "thickness", "Thickness", ["0"], "0")
    ttk.Label(frm, text="Polylines only (LWPOLYLINE):").grid(
        row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
    add_row(8, "elevation", "Elevation", ["0"], "0")
    add_row(9, "width", "Global width", ["0"], "0")
    add_row(10, "closure", "Closure", ["Closed", "Open"], "Closed")

    log_box = tk.Text(frm, width=62, height=8, state="disabled")
    log_box.grid(row=12, column=0, columnspan=3, pady=(10, 0))

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    def refresh(*_):
        try:
            n = selected_count(cl)
            sel_var.set("%d object(s) selected in CADCraft" % n)
            layers = get_layers(cl)
            rows["layer"][2].configure(values=layers)
            lts = get_linetypes(cl)
            if lts:
                merged = ["ByLayer", "ByBlock"] + [
                    x for x in lts if x.lower() not in ("bylayer", "byblock")]
                rows["ltype"][2].configure(values=merged)
        except CadError as e:
            log("Refresh failed: %s" % e)

    def apply(*_):
        changes = []
        for prop in PROPERTY_ORDER:
            en, val, _w = rows[prop]
            if en.get():
                v = val.get().strip()
                if not v:
                    messagebox.showwarning("CHPROP",
                                           "Enter a value for %s." % prop)
                    return
                changes.append((prop, v))
        if not changes:
            messagebox.showinfo("CHPROP", "Tick at least one property.")
            return
        try:
            if selected_count(cl) == 0:
                if not messagebox.askyesno(
                        "CHPROP",
                        "Nothing is selected in CADCraft.\n"
                        "Try the previous selection instead?"):
                    return
            layer = dict(changes).get("layer")
            if layer and layer.lower() not in [
                    n.lower() for n in get_layers(cl)]:
                if messagebox.askyesno(
                        "CHPROP", "Layer \"%s\" does not exist.\n"
                        "Create it?" % layer):
                    create_layer(cl, layer, log)
                else:
                    return
            run_chprop(cl, changes, log)
            log("Done.")
            refresh()
        except CadError as e:
            log("ERROR: %s" % e)
            messagebox.showerror("CHPROP", str(e))

    btns = ttk.Frame(frm)
    btns.grid(row=11, column=0, columnspan=3, sticky="e", pady=(10, 0))
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


def main():
    ap = argparse.ArgumentParser(description="CHPROP dialog for CADCraft")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CADCRAFT_CONTROL_PORT",
                                               DEFAULT_PORT)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--diag", action="store_true",
                    help="print diagnostics and exit")
    args = ap.parse_args()
    if args.diag:
        try:
            cl = Client(args.port, args.token)
            diagnostics(cl)
            cl.close()
            return 0
        except CadError as e:
            print("ERROR:", e)
            return 1
    return run_gui(args.port, args.token)


if __name__ == "__main__":
    sys.exit(main())
