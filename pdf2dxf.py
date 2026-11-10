#!/usr/bin/env python3
"""
pdf2dxf.py - Vector PDF -> DXF converter with colour-to-layer mapping and scale calibration.

GUI :  python pdf2dxf.py                (or --gui)
CLI :  python pdf2dxf.py in.pdf out.dxf --ratio 100 --unit m --map map.json
       python pdf2dxf.py in.pdf out.dxf --calib 120.5,300,480.2,300,25.0 --unit m
       python pdf2dxf.py in.pdf --list-colours --write-map map.json
       python pdf2dxf.py pdf_folder/ dxf_folder/ --ratio 100 --unit m --recursive --suffix   (batch)

Requires:  pip install pymupdf ezdxf        (tkinter for the GUI)

Coordinates: PDF points, origin top-left of the *displayed* page (rotation applied).
DXF output : Y-up, page bottom-left = (0,0), scaled by `factor` (drawing units per PDF point).
"""
import argparse
import json
import math
import os
import queue
import re
import sys
import threading

try:
    import pymupdf
except ImportError:  # older PyMuPDF
    import fitz as pymupdf
import ezdxf
from ezdxf import colors as ezcolors
from ezdxf.lldxf.const import LWPOLYLINE_PLINEGEN

# --------------------------------------------------------------------------- core
UNITS = {            # name: (mm per unit, DXF $INSUNITS code)
    "mm": (1.0, 4),
    "cm": (10.0, 5),
    "m": (1000.0, 6),
    "in": (25.4, 1),
    "ft": (304.8, 2),
}
PT_MM = 25.4 / 72.0  # one PDF point in millimetres


def ratio_factor(ratio, unit):
    """Drawing units per PDF point for a printed scale of 1:ratio."""
    return PT_MM * ratio / UNITS[unit][0]


def calib_factor(p1, p2, real_dist):
    d = math.dist(p1, p2)
    if d <= 0:
        raise ValueError("Calibration points are identical")
    return real_dist / d


def ratio_from_factor(factor, unit):
    return factor * UNITS[unit][0] / PT_MM


def parse_pages(spec, n):
    spec = (spec or "all").strip().lower()
    if spec in ("", "all", "*"):
        return list(range(n))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else n
            out.extend(range(a - 1, b))
        elif part:
            out.append(int(part) - 1)
    return sorted({p for p in out if 0 <= p < n})


def to_hex(c):
    if c is None:
        return None
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(v * 255))) for v in c[:3])


def int_to_hex(i):
    return "#%06X" % (i & 0xFFFFFF)


def hex_to_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def nearest_aci(hexcol):
    r, g, b = hex_to_rgb(hexcol)
    best, best_d = 7, 1e18
    for idx in range(1, 256):
        v = ezcolors.DXF_DEFAULT_COLORS[idx]
        rr, gg, bb = (v >> 16) & 255, (v >> 8) & 255, v & 255
        d = (r - rr) ** 2 + (g - gg) ** 2 + (b - bb) ** 2
        if d < best_d:
            best, best_d = idx, d
    return best


def clean_layer(name):
    name = re.sub(r'[<>/\\":;?*|=`]', "_", name.strip())
    return name or "0"


def _close(a, b, tol=1e-3):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _bezier(p0, p1, p2, p3, n):
    pts = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def _subpaths(items, segs, tf):
    """Chain PDF path items into sub-paths -> list of (points, closed)."""
    subs, cur = [], []
    closed_flag = False

    def flush():
        nonlocal cur, closed_flag
        if len(cur) >= 2:
            closed = closed_flag
            if len(cur) > 2 and _close(cur[0], cur[-1]):
                cur = cur[:-1]
                closed = True
            subs.append((cur, closed))
        cur, closed_flag = [], False

    for it in items:
        k = it[0]
        if k == "l":
            p1, p2 = tf(it[1]), tf(it[2])
            if not cur or not _close(cur[-1], p1):
                flush()
                cur = [p1]
            cur.append(p2)
        elif k == "c":
            p = [tf(q) for q in it[1:5]]
            if not cur or not _close(cur[-1], p[0]):
                flush()
                cur = [p[0]]
            cur.extend(_bezier(p[0], p[1], p[2], p[3], segs))
        elif k == "re":
            flush()
            r = it[1]
            cur = [tf((r.x0, r.y0)), tf((r.x1, r.y0)), tf((r.x1, r.y1)), tf((r.x0, r.y1))]
            closed_flag = True
            flush()
        elif k == "qu":
            flush()
            q = it[1]
            cur = [tf(q.ul), tf(q.ur), tf(q.lr), tf(q.ll)]
            closed_flag = True
            flush()
    flush()
    return subs


def parse_dash(s):
    """PyMuPDF dash string '[3 2] 0' -> tuple of on/off lengths in pt (even count), or None."""
    if not s:
        return None
    m = re.match(r"\s*\[([^\]]*)\]", s)
    if not m:
        return None
    try:
        v = [float(x) for x in m.group(1).split()]
    except ValueError:
        return None
    if not v or sum(v) <= 0:
        return None
    if len(v) % 2:
        v = v * 2
    return tuple(v)


def extract_page(page, segs=12, text=True):
    """Return (primitives, view_height). Primitives are in view space (pt, Y down)."""
    m = page.rotation_matrix

    def tf(p):
        q = pymupdf.Point(p[0], p[1]) * m
        return (q.x, q.y)

    prims = []
    for d in page.get_drawings():
        stroke = to_hex(d.get("color"))
        fill = to_hex(d.get("fill"))
        typ = d.get("type", "s")
        if "s" not in typ:
            stroke = None
        if "f" not in typ:
            fill = None
        if not stroke and not fill:
            continue
        subs = _subpaths(d["items"], segs, tf)
        if not subs:
            continue
        w = d.get("width") or 0.0
        dash = parse_dash(d.get("dashes"))
        if fill:
            closed_subs = [pts for pts, _ in subs if len(pts) >= 3]
            if closed_subs:
                prims.append({"kind": "fill", "paths": closed_subs, "key": fill, "outline": not stroke})
        if stroke:
            for pts, closed in subs:
                prims.append({"kind": "poly", "pts": pts, "closed": closed, "key": stroke,
                              "width": w, "dash": dash})

    if text:
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                dx, dy = ln["dir"]
                v = pymupdf.Point(dx, dy) * pymupdf.Matrix(m.a, m.b, m.c, m.d, 0, 0)
                ang = math.degrees(math.atan2(-v.y, v.x))
                for sp in ln.get("spans", []):
                    t = sp["text"]
                    if not t.strip():
                        continue
                    prims.append({"kind": "text", "text": t, "pos": tf(sp["origin"]),
                                  "size": sp["size"], "angle": ang, "key": int_to_hex(sp["color"])})
    return prims, page.rect.height


def default_mapping(keys, prefix="C_"):
    out = {}
    for k in keys:
        out[k] = {"layer": f"{prefix}{k.lstrip('#')}", "use": k.upper() != "#FFFFFF"}
    return out


def build_dxf(pages, mapping, factor, unit, path, hatch=True, text=True, keep_widths=False,
              aci=False, lines_as_line=True, dashes=True, suffix=False):
    """pages: list of (prims, view_height). Writes `path` (first page only per file)."""
    prims, H = pages
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = UNITS[unit][1]
    msp = doc.modelspace()
    made = {}

    def ltype_for(dash):
        name = "PDF_" + "_".join(f"{round(v, 2):g}" for v in dash)
        if name not in doc.linetypes:
            pat = [sum(dash) * factor] + [(v if i % 2 == 0 else -v) * factor for i, v in enumerate(dash)]
            doc.linetypes.add(name, pattern=pat, description="from PDF dash " + " ".join(f"{v:g}" for v in dash))
        return name

    def layer_for(key, suf=""):
        m = mapping[key]
        name = clean_layer(m["layer"] + (suf if suffix else ""))
        if name not in made:
            if name not in doc.layers:
                lay = doc.layers.add(name)
                if aci:
                    lay.color = nearest_aci(key)
                else:
                    lay.rgb = hex_to_rgb(key)
            made[name] = True
        return name

    def P(p):
        return (p[0] * factor, (H - p[1]) * factor)

    counts = {"poly": 0, "line": 0, "hatch": 0, "text": 0}
    for pr in prims:
        m = mapping.get(pr["key"])
        if not m or not m.get("use", True):
            continue
        if pr["kind"] == "poly":
            pts = [P(p) for p in pr["pts"]]
            is_line = lines_as_line and len(pts) == 2 and not pr["closed"]
            attr = {"layer": layer_for(pr["key"], "_LINE" if is_line else "_PLINE")}
            if dashes and pr.get("dash"):
                attr["linetype"] = ltype_for(pr["dash"])
            if is_line:
                msp.add_line(pts[0], pts[1], dxfattribs=attr)
                counts["line"] += 1
            else:
                if keep_widths and pr["width"] > 0:
                    attr["const_width"] = pr["width"] * factor
                e = msp.add_lwpolyline(pts, close=pr["closed"], dxfattribs=attr)
                if "linetype" in attr:  # continuous pattern across vertices
                    e.dxf.flags = e.dxf.flags | LWPOLYLINE_PLINEGEN
                counts["poly"] += 1
        elif pr["kind"] == "fill":
            if hatch:
                h = msp.add_hatch(dxfattribs={"layer": layer_for(pr["key"], "_HATCH")})
                h.set_solid_fill(color=256)
                for sp in pr["paths"]:
                    h.paths.add_polyline_path([P(p) for p in sp], is_closed=True)
                counts["hatch"] += 1
            elif pr["outline"]:
                attr = {"layer": layer_for(pr["key"], "_PLINE")}
                for sp in pr["paths"]:
                    msp.add_lwpolyline([P(p) for p in sp], close=True, dxfattribs=attr)
                    counts["poly"] += 1
        elif pr["kind"] == "text" and text:
            t = msp.add_text(pr["text"], height=max(pr["size"] * 0.72 * factor, 1e-9),
                             rotation=pr["angle"], dxfattribs={"layer": layer_for(pr["key"], "_DTEXT")})
            t.dxf.insert = P(pr["pos"])
            counts["text"] += 1
    doc.saveas(path)
    return counts


def scan_colours(page_prims):
    cnt = {}
    for prims, _ in page_prims:
        for pr in prims:
            cnt[pr["key"]] = cnt.get(pr["key"], 0) + 1
    return dict(sorted(cnt.items(), key=lambda kv: -kv[1]))


def out_paths(out, idxs):
    if len(idxs) == 1:
        return {idxs[0]: out}
    base, ext = os.path.splitext(out)
    return {i: f"{base}_p{i + 1}{ext or '.dxf'}" for i in idxs}


def convert_pdf(pdf, out, mapping, factor, unit, pages="all", segs=12, **build_kw):
    """Convert one PDF (all/selected pages). Colours missing from `mapping` get default layers."""
    doc = pymupdf.open(pdf)
    res = []
    try:
        idxs = parse_pages(pages, len(doc))
        for i, p in out_paths(out, idxs).items():
            data = extract_page(doc[i], segs, build_kw.get("text", True))
            for k in {pr["key"] for pr in data[0]}:
                mapping.setdefault(k, default_mapping([k])[k])
            res.append((i + 1, p, build_dxf(data, mapping, factor, unit, p, **build_kw)))
    finally:
        doc.close()
    return res


def batch_convert(src, dst, recursive, mapping, factor, unit, log=print, **kw):
    """Convert every PDF in src (optionally recursive) into dst, mirroring sub-folders."""
    files = []
    if recursive:
        for root, _, names in os.walk(src):
            files += [os.path.join(root, n) for n in names if n.lower().endswith(".pdf")]
    else:
        files = [os.path.join(src, n) for n in os.listdir(src) if n.lower().endswith(".pdf")]
    files.sort()
    ok, errs = 0, []
    for n, f in enumerate(files, 1):
        rel = os.path.relpath(os.path.splitext(f)[0] + ".dxf", src)
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        log(f"[{n}/{len(files)}] {rel}")
        try:
            convert_pdf(f, out, mapping, factor, unit, **kw)
            ok += 1
        except Exception as ex:
            errs.append(f"{os.path.relpath(f, src)}: {ex}")
    summary = f"Batch done: {ok} of {len(files)} file(s) converted to {dst}"
    if errs:
        summary += f"\n{len(errs)} failed:\n" + "\n".join(errs[:8])
    return summary


# --------------------------------------------------------------------------- CLI
def run_cli_batch(a):
    mapping = {}
    if a.map and os.path.exists(a.map):
        mapping.update(json.load(open(a.map, encoding="utf-8")))
    if a.calib:
        sys.exit("--calib is per-drawing; use --ratio or --factor for batch")
    factor = ratio_factor(a.ratio, a.unit) if a.ratio else (a.factor or 1.0)
    print(batch_convert(a.pdf, a.out or a.pdf, a.recursive, mapping, factor, a.unit, pages=a.pages,
                        segs=a.segments, text=not a.no_text, hatch=a.hatch, keep_widths=a.widths,
                        aci=a.aci, dashes=not a.no_dashes, suffix=a.suffix))
    if a.write_map:
        json.dump(mapping, open(a.write_map, "w", encoding="utf-8"), indent=2)


def run_cli(a):
    if os.path.isdir(a.pdf):
        return run_cli_batch(a)
    doc = pymupdf.open(a.pdf)
    idxs = parse_pages(a.pages, len(doc))
    data = {i: extract_page(doc[i], a.segments, not a.no_text) for i in idxs}
    cols = scan_colours(list(data.values()))
    mapping = default_mapping(cols.keys())
    if a.map and os.path.exists(a.map):
        mapping.update(json.load(open(a.map, encoding="utf-8")))
    if a.list_colours:
        for k, n in cols.items():
            print(f"{k}  {n:6d}  -> {mapping[k]['layer']}{'' if mapping[k]['use'] else '  (skipped)'}")
        if a.write_map:
            json.dump(mapping, open(a.write_map, "w", encoding="utf-8"), indent=2)
            print("map written to", a.write_map)
        return
    if not a.out:
        sys.exit("output .dxf path required")
    if a.calib:
        x1, y1, x2, y2, dist = [float(v) for v in a.calib.split(",")]
        factor = calib_factor((x1, y1), (x2, y2), dist)
    elif a.ratio:
        factor = ratio_factor(a.ratio, a.unit)
    elif a.factor:
        factor = a.factor
    else:
        factor = 1.0
    for i, p in out_paths(a.out, idxs).items():
        c = build_dxf(data[i], mapping, factor, a.unit, p, hatch=a.hatch, text=not a.no_text,
                      keep_widths=a.widths, aci=a.aci, dashes=not a.no_dashes, suffix=a.suffix)
        print(f"page {i + 1} -> {p}  factor={factor:.6g} {a.unit}/pt  {c}")


def build_preview(rect, prims, mapping, hatch=True, text=True):
    """In-memory vector page showing only objects whose colour is enabled ('Use')."""
    doc = pymupdf.open()
    pg = doc.new_page(width=rect.width, height=rect.height)
    sh = pg.new_shape()
    for pr in prims:
        m = mapping.get(pr["key"])
        if m is not None and not m.get("use", True):
            continue
        col = tuple(v / 255 for v in hex_to_rgb(pr["key"]))
        k = pr["kind"]
        if k == "poly":
            dash = pr.get("dash")
            sh.draw_polyline(pr["pts"])
            sh.finish(color=col, fill=None, width=max(pr["width"], 0.3), closePath=pr["closed"],
                      dashes=("[%s] 0" % " ".join(f"{v:g}" for v in dash)) if dash else None)
        elif k == "fill":
            if hatch:
                for sp in pr["paths"]:
                    sh.draw_polyline(sp)
                sh.finish(color=None, fill=col, even_odd=True, closePath=True)
            elif pr["outline"]:
                for sp in pr["paths"]:
                    sh.draw_polyline(sp)
                    sh.finish(color=col, fill=None, width=0.3, closePath=True)
        elif k == "text" and text:
            try:
                pos = pymupdf.Point(*pr["pos"])
                sh.insert_text(pos, pr["text"], fontsize=pr["size"], color=col,
                               morph=(pos, pymupdf.Matrix(pr["angle"])))
            except Exception:
                pass
    sh.commit()
    return doc


# --------------------------------------------------------------------------- GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    class App:
        def __init__(self, root):
            self.root = root
            root.title("PDF -> DXF  (colour -> layer, scale calibration)")
            root.geometry("1400x860")
            self.doc = None
            self.cache = {}          # page index -> (prims, H)
            self.mapping = {}
            self.zoom = 1.0
            self.img = None
            self.calib_pts = []      # view-space points
            self.picking = False
            self.factor = 1.0
            self.swatches = {}
            self.q = queue.Queue()
            self.gen, self.pv_key, self.pv_doc = 0, None, None
            self._build()

        # ---------- layout
        def _build(self):
            r = self.root
            top = ttk.Frame(r, padding=4)
            top.pack(fill="x")
            ttk.Button(top, text="Open PDF…", command=self.open_pdf).pack(side="left")
            self.lbl_file = ttk.Label(top, text="no file")
            self.lbl_file.pack(side="left", padx=8)
            ttk.Label(top, text="Preview page").pack(side="left", padx=(20, 2))
            self.var_page = tk.IntVar(value=1)
            ttk.Spinbox(top, from_=1, to=9999, width=5, textvariable=self.var_page,
                        command=self.render).pack(side="left")
            ttk.Button(top, text="−", width=3, command=lambda: self.set_zoom(self.zoom / 1.25)).pack(side="left", padx=(14, 0))
            ttk.Button(top, text="+", width=3, command=lambda: self.set_zoom(self.zoom * 1.25)).pack(side="left")
            ttk.Button(top, text="Fit", command=self.fit).pack(side="left", padx=2)
            self.var_snap = tk.BooleanVar(value=True)
            ttk.Checkbutton(top, text="Snap to vertices", variable=self.var_snap).pack(side="left", padx=12)
            self.var_view = tk.StringVar(value="layers")
            ttk.Label(top, text="View").pack(side="left", padx=(4, 2))
            ttk.Radiobutton(top, text="Layers (Use only)", variable=self.var_view, value="layers",
                            command=self.render).pack(side="left")
            ttk.Radiobutton(top, text="Original PDF", variable=self.var_view, value="pdf",
                            command=self.render).pack(side="left")
            self.lbl_cursor = ttk.Label(top, text="")
            self.lbl_cursor.pack(side="right")

            body = ttk.PanedWindow(r, orient="horizontal")
            body.pack(fill="both", expand=True)

            left = ttk.Frame(body)
            body.add(left, weight=3)
            self.canvas = tk.Canvas(left, bg="#666", highlightthickness=0, cursor="crosshair")
            hs = ttk.Scrollbar(left, orient="horizontal", command=self.canvas.xview)
            vs = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(xscrollcommand=hs.set, yscrollcommand=vs.set)
            hs.pack(side="bottom", fill="x")
            vs.pack(side="right", fill="y")
            self.canvas.pack(fill="both", expand=True)
            self.canvas.bind("<Button-1>", self.on_click)
            self.canvas.bind("<Motion>", self.on_motion)
            self.canvas.bind("<ButtonPress-2>", lambda e: self.canvas.scan_mark(e.x, e.y))
            self.canvas.bind("<B2-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
            self.canvas.bind("<ButtonPress-3>", lambda e: self.canvas.scan_mark(e.x, e.y))
            self.canvas.bind("<B3-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
            self.canvas.bind("<MouseWheel>", self.on_wheel)
            self.canvas.bind("<Button-4>", lambda e: self.on_wheel(e, 1))
            self.canvas.bind("<Button-5>", lambda e: self.on_wheel(e, -1))

            right = ttk.Frame(body, padding=6)
            body.add(right, weight=2)

            # --- scale
            sf = ttk.LabelFrame(right, text="Scale / calibration", padding=6)
            sf.pack(fill="x")
            row = ttk.Frame(sf)
            row.pack(fill="x")
            ttk.Label(row, text="Output unit").pack(side="left")
            self.var_unit = tk.StringVar(value="m")
            cb = ttk.Combobox(row, textvariable=self.var_unit, values=list(UNITS), width=5, state="readonly")
            cb.pack(side="left", padx=4)
            cb.bind("<<ComboboxSelected>>", lambda e: self.update_factor())
            self.var_mode = tk.StringVar(value="ratio")
            r1 = ttk.Frame(sf)
            r1.pack(fill="x", pady=2)
            ttk.Radiobutton(r1, text="Printed scale  1 :", variable=self.var_mode, value="ratio",
                            command=self.update_factor).pack(side="left")
            self.var_ratio = tk.StringVar(value="100")
            e = ttk.Entry(r1, textvariable=self.var_ratio, width=8)
            e.pack(side="left", padx=4)
            e.bind("<KeyRelease>", lambda ev: self.update_factor())
            r2 = ttk.Frame(sf)
            r2.pack(fill="x", pady=2)
            ttk.Radiobutton(r2, text="Two-point calibration", variable=self.var_mode, value="calib",
                            command=self.update_factor).pack(side="left")
            ttk.Button(r2, text="Pick 2 points…", command=self.start_pick).pack(side="left", padx=6)
            self.lbl_calib = ttk.Label(sf, text="(none)", foreground="#555")
            self.lbl_calib.pack(anchor="w")
            r3 = ttk.Frame(sf)
            r3.pack(fill="x", pady=2)
            ttk.Radiobutton(r3, text="Manual: units per PDF pt", variable=self.var_mode, value="manual",
                            command=self.update_factor).pack(side="left")
            self.var_manual = tk.StringVar(value="1.0")
            e = ttk.Entry(r3, textvariable=self.var_manual, width=12)
            e.pack(side="left", padx=4)
            e.bind("<KeyRelease>", lambda ev: self.update_factor())
            self.lbl_factor = ttk.Label(sf, text="", font=("TkDefaultFont", 9, "bold"))
            self.lbl_factor.pack(anchor="w", pady=(4, 0))

            # --- colours
            cf = ttk.LabelFrame(right, text="Colour → layer  (dbl-click 'Use' to toggle, 'Layer' to rename)", padding=6)
            cf.pack(fill="both", expand=True, pady=6)
            self.tree = ttk.Treeview(cf, columns=("use", "count", "layer"), height=12, selectmode="extended")
            self.tree.heading("#0", text="Colour")
            self.tree.heading("use", text="Use")
            self.tree.heading("count", text="Objects")
            self.tree.heading("layer", text="Layer")
            self.tree.column("#0", width=110, stretch=False)
            self.tree.column("use", width=40, anchor="center", stretch=False)
            self.tree.column("count", width=65, anchor="e", stretch=False)
            self.tree.column("layer", width=200)
            ts = ttk.Scrollbar(cf, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=ts.set)
            self.tree.pack(side="left", fill="both", expand=True)
            ts.pack(side="left", fill="y")
            self.tree.bind("<Double-1>", self.on_tree_dbl)
            bf = ttk.Frame(right)
            bf.pack(fill="x")
            for txt, cmd in (("Scan colours", self.scan), ("Set layer for selected…", self.set_selected_layer),
                             ("Toggle selected", self.toggle_selected), ("Reset names", self.reset_names),
                             ("Save map…", self.save_map), ("Load map…", self.load_map)):
                ttk.Button(bf, text=txt, command=cmd).pack(side="left", padx=1)

            # --- options
            of = ttk.LabelFrame(right, text="Export options", padding=6)
            of.pack(fill="x", pady=6)
            self.var_pages = tk.StringVar(value="all")
            self.var_text = tk.BooleanVar(value=True)
            self.var_hatch = tk.BooleanVar(value=True)
            self.var_widths = tk.BooleanVar(value=False)
            self.var_aci = tk.BooleanVar(value=False)
            self.var_segs = tk.IntVar(value=12)
            self.var_dash = tk.BooleanVar(value=True)
            self.var_suffix = tk.BooleanVar(value=False)
            self.var_hatch.trace_add("write", lambda *a: self.render())
            self.var_text.trace_add("write", lambda *a: self.render())
            g = ttk.Frame(of)
            g.pack(fill="x")
            ttk.Label(g, text="Pages (e.g. 1,3-5)").grid(row=0, column=0, sticky="w")
            ttk.Entry(g, textvariable=self.var_pages, width=12).grid(row=0, column=1, sticky="w", padx=4)
            ttk.Label(g, text="Curve segments").grid(row=0, column=2, sticky="e", padx=(12, 0))
            ttk.Spinbox(g, from_=2, to=100, width=4, textvariable=self.var_segs).grid(row=0, column=3, padx=4)
            ttk.Checkbutton(of, text="Extract text as TEXT entities", variable=self.var_text).pack(anchor="w")
            ttk.Checkbutton(of, text="Filled areas as solid HATCH (else outlines only)", variable=self.var_hatch).pack(anchor="w")
            ttk.Checkbutton(of, text="Keep stroke widths (polyline constant width)", variable=self.var_widths).pack(anchor="w")
            ttk.Checkbutton(of, text="Layer colour = nearest ACI index (else true colour)", variable=self.var_aci).pack(anchor="w")
            ttk.Checkbutton(of, text="Dash patterns → linetypes (PDF_4_2 …)", variable=self.var_dash).pack(anchor="w")
            ttk.Checkbutton(of, text="Add entity type to layer name  (_LINE, _PLINE, _HATCH, _DTEXT)",
                            variable=self.var_suffix).pack(anchor="w")

            self.btn_conv = ttk.Button(right, text="Convert → DXF…", command=self.convert)
            self.btn_conv.pack(fill="x", pady=4)
            self.btn_batch = ttk.Button(right, text="Batch folder…", command=self.batch)
            self.btn_batch.pack(fill="x")
            self.status = ttk.Label(right, text="Open a PDF to begin.", wraplength=520, foreground="#333")
            self.status.pack(anchor="w")
            self.update_factor()

        # ---------- file / render
        def open_pdf(self):
            p = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf"), ("All", "*.*")])
            if not p:
                return
            try:
                self.doc = pymupdf.open(p)
            except Exception as ex:
                messagebox.showerror("Open failed", str(ex))
                return
            self.path = p
            self.cache.clear()
            self.pv_key, self.pv_doc = None, None
            self.calib_pts = []
            self.lbl_file.config(text=f"{os.path.basename(p)}  ({len(self.doc)} pages)")
            self.var_page.set(1)
            self.root.update_idletasks()
            self.scan()
            self.fit()

        def page_idx(self):
            n = len(self.doc)
            return max(0, min(n - 1, self.var_page.get() - 1))

        def get_page_data(self, i):
            if i not in self.cache:
                self.cache[i] = extract_page(self.doc[i], self.var_segs.get(), self.var_text.get())
            return self.cache[i]

        def fit(self):
            if not self.doc:
                return
            r = self.doc[self.page_idx()].rect
            cw, ch = max(self.canvas.winfo_width(), 200), max(self.canvas.winfo_height(), 200)
            self.zoom = min(cw / r.width, ch / r.height) * 0.97
            self.render()

        def set_zoom(self, z):
            self.zoom = max(0.1, min(z, 12))
            self.render()

        def render(self):
            if not self.doc:
                return
            pg = self.preview_page()
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False)
            self.img = tk.PhotoImage(data=pix.tobytes("png"))
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self.img, anchor="nw")
            self.canvas.configure(scrollregion=(0, 0, pix.width, pix.height))
            self.draw_calib()

        def preview_page(self):
            i = self.page_idx()
            if self.var_view.get() != "layers":
                return self.doc[i]
            prims, _ = self.get_page_data(i)
            used = tuple(sorted(k for k, m in self.mapping.items() if m.get("use", True)))
            key = (i, self.gen, used, self.var_hatch.get(), self.var_text.get())
            if key != self.pv_key:
                self.pv_doc = build_preview(self.doc[i].rect, prims, self.mapping,
                                            self.var_hatch.get(), self.var_text.get())
                self.pv_key = key
            return self.pv_doc[0]

        def on_wheel(self, e, d=None):
            if d is None:
                d = 1 if e.delta > 0 else -1
            if e.state & 0x4:  # ctrl
                self.set_zoom(self.zoom * (1.15 if d > 0 else 1 / 1.15))
            else:
                self.canvas.yview_scroll(-d * 3, "units")

        # ---------- picking / calibration
        def view_pt(self, e):
            cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
            return cx / self.zoom, cy / self.zoom

        def snap(self, p):
            if not self.var_snap.get() or not self.doc:
                return p
            prims, _ = self.get_page_data(self.page_idx())
            best, bd = None, (10 / self.zoom) ** 2
            for pr in prims:
                if self.var_view.get() == "layers" and not self.mapping.get(pr["key"], {}).get("use", True):
                    continue
                if pr["kind"] == "poly":
                    seq = pr["pts"]
                elif pr["kind"] == "fill":
                    seq = [q for sp in pr["paths"] for q in sp]
                else:
                    continue
                for q in seq:
                    d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
                    if d < bd:
                        best, bd = q, d
            return best or p

        def on_motion(self, e):
            if self.doc:
                x, y = self.view_pt(e)
                H = self.doc[self.page_idx()].rect.height
                self.lbl_cursor.config(text=f"pt: {x:.2f}, {H - y:.2f}   → {self.fmt_units(x)}, {self.fmt_units(H - y)}")

        def fmt_units(self, v):
            return f"{v * self.factor:.4g} {self.var_unit.get()}"

        def start_pick(self):
            if not self.doc:
                return
            self.calib_pts = []
            self.picking = True
            self.render()
            self.status.config(text="Click the FIRST point of a known distance (snaps to vertices).")

        def on_click(self, e):
            if not (self.picking and self.doc):
                return
            self.calib_pts.append(self.snap(self.view_pt(e)))
            self.draw_calib()
            if len(self.calib_pts) == 1:
                self.status.config(text="Click the SECOND point.")
                return
            self.picking = False
            pd = math.dist(*self.calib_pts)
            val = simpledialog.askfloat("Real distance",
                                        f"Real-world distance between the points\n(in {self.var_unit.get()}):",
                                        parent=self.root, minvalue=1e-12)
            if val is None:
                self.calib_pts = []
                self.render()
                self.status.config(text="Calibration cancelled.")
                return
            self.calib_factor = val / pd
            self.calib_info = f"{pd:.3f} pt = {val:g} {self.var_unit.get()}"
            self.var_mode.set("calib")
            self.update_factor()
            self.status.config(text="Calibration set.")

        def draw_calib(self):
            self.canvas.delete("calib")
            pts = [(x * self.zoom, y * self.zoom) for x, y in self.calib_pts]
            for x, y in pts:
                self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, outline="#e00", width=2, tags="calib")
            if len(pts) == 2:
                self.canvas.create_line(*pts[0], *pts[1], fill="#e00", width=2, tags="calib")

        def update_factor(self):
            unit = self.var_unit.get()
            mode = self.var_mode.get()
            f = None
            try:
                if mode == "ratio":
                    f = ratio_factor(float(self.var_ratio.get()), unit)
                elif mode == "calib":
                    f = getattr(self, "calib_factor", None)
                else:
                    f = float(self.var_manual.get())
            except ValueError:
                f = None
            self.lbl_calib.config(text=getattr(self, "calib_info", "(none)"))
            if f:
                self.factor = f
                self.lbl_factor.config(
                    text=f"1 PDF pt = {f:.6g} {unit}   (≈ 1:{ratio_from_factor(f, unit):.4g} on paper)")
            else:
                self.lbl_factor.config(text="Scale not set")

        # ---------- colour table
        def swatch(self, key):
            if key not in self.swatches:
                im = tk.PhotoImage(width=16, height=12)
                im.put("#000000", to=(0, 0, 16, 12))
                im.put(key, to=(1, 1, 15, 11))
                self.swatches[key] = im
            return self.swatches[key]

        def scan(self):
            if not self.doc:
                return
            self.root.config(cursor="watch")
            self.status.config(text="Scanning colours…")
            self.root.update_idletasks()
            try:
                self.cache.clear()
                self.gen += 1
                idxs = parse_pages(self.var_pages.get(), len(self.doc))
                data = [self.get_page_data(i) for i in idxs]
                self.counts = scan_colours(data)
            except Exception as ex:
                messagebox.showerror("Scan failed", str(ex))
                return
            finally:
                self.root.config(cursor="")
            for k, v in default_mapping(self.counts.keys()).items():
                self.mapping.setdefault(k, v)
            self.refresh_tree()
            self.status.config(text=f"{len(self.counts)} colours found on {len(idxs)} page(s).")

        def refresh_tree(self):
            self.tree.delete(*self.tree.get_children())
            for k, n in getattr(self, "counts", {}).items():
                m = self.mapping[k]
                self.tree.insert("", "end", iid=k, text=" " + k, image=self.swatch(k),
                                 values=("✔" if m["use"] else "–", n, m["layer"]))
            if self.doc:
                self.render()

        def on_tree_dbl(self, e):
            iid = self.tree.identify_row(e.y)
            col = self.tree.identify_column(e.x)
            if not iid:
                return
            if col == "#1":
                self.mapping[iid]["use"] = not self.mapping[iid]["use"]
            elif col == "#3":
                v = simpledialog.askstring("Layer name", f"Layer for {iid}:", initialvalue=self.mapping[iid]["layer"],
                                           parent=self.root)
                if v:
                    self.mapping[iid]["layer"] = clean_layer(v)
            self.refresh_tree()

        def set_selected_layer(self):
            sel = self.tree.selection()
            if not sel:
                return
            v = simpledialog.askstring("Layer name", f"Layer for {len(sel)} selected colour(s):", parent=self.root)
            if v:
                for k in sel:
                    self.mapping[k]["layer"] = clean_layer(v)
                self.refresh_tree()

        def toggle_selected(self):
            for k in self.tree.selection():
                self.mapping[k]["use"] = not self.mapping[k]["use"]
            self.refresh_tree()

        def reset_names(self):
            for k, v in default_mapping(getattr(self, "counts", {}).keys()).items():
                self.mapping[k] = v
            self.refresh_tree()

        def save_map(self):
            p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
            if p:
                json.dump(self.mapping, open(p, "w", encoding="utf-8"), indent=2)

        def load_map(self):
            p = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
            if p:
                try:
                    self.mapping.update(json.load(open(p, encoding="utf-8")))
                except Exception as ex:
                    messagebox.showerror("Load failed", str(ex))
                self.refresh_tree()

        # ---------- convert
        def convert(self):
            if not self.doc:
                return
            if self.var_mode.get() == "calib" and not hasattr(self, "calib_factor"):
                messagebox.showwarning("Scale", "Pick two calibration points first.")
                return
            self.update_factor()
            if not self.factor:
                messagebox.showwarning("Scale", "Scale is not valid.")
                return
            default = os.path.splitext(self.path)[0] + ".dxf"
            out = filedialog.asksaveasfilename(defaultextension=".dxf", initialfile=os.path.basename(default),
                                               initialdir=os.path.dirname(default), filetypes=[("DXF", "*.dxf")])
            if not out:
                return
            idxs = parse_pages(self.var_pages.get(), len(self.doc))
            data = {i: self.get_page_data(i) for i in idxs}
            args = dict(factor=self.factor, unit=self.var_unit.get(), hatch=self.var_hatch.get(),
                        text=self.var_text.get(), keep_widths=self.var_widths.get(), aci=self.var_aci.get(),
                        dashes=self.var_dash.get(), suffix=self.var_suffix.get())
            mapping = {k: dict(v) for k, v in self.mapping.items()}
            self.btn_conv.state(["disabled"])
            self.btn_batch.state(["disabled"])
            self.status.config(text="Converting…")

            def work():
                try:
                    msgs = []
                    for i, p in out_paths(out, idxs).items():
                        c = build_dxf(data[i], mapping, path=p, **args)
                        msgs.append(f"p{i + 1}: {os.path.basename(p)}  {c}")
                    self.q.put(("ok", "\n".join(msgs)))
                except Exception as ex:
                    self.q.put(("err", str(ex)))

            threading.Thread(target=work, daemon=True).start()
            self.root.after(100, self.poll)

        def batch(self):
            self.update_factor()
            if self.var_mode.get() == "calib" and not hasattr(self, "calib_factor"):
                messagebox.showwarning("Scale", "Pick two calibration points first.")
                return
            if not self.factor:
                messagebox.showwarning("Scale", "Scale is not valid.")
                return
            src = filedialog.askdirectory(title="Folder containing PDFs")
            if not src:
                return
            dst = filedialog.askdirectory(title="Output folder for DXF files", initialdir=src)
            if not dst:
                return
            rec = messagebox.askyesno("Batch", "Include sub-folders?")
            unit, factor = self.var_unit.get(), self.factor
            if not messagebox.askokcancel(
                    "Batch", f"Every PDF will use the current scale:\n1 PDF pt = {factor:.6g} {unit}\n\n"
                             f"the current colour → layer map (new colours get default names),\n"
                             f"export options and page selection ({self.var_pages.get()}).\n\nProceed?"):
                return
            kw = dict(pages=self.var_pages.get(), segs=self.var_segs.get(), text=self.var_text.get(),
                      hatch=self.var_hatch.get(), keep_widths=self.var_widths.get(), aci=self.var_aci.get(),
                      dashes=self.var_dash.get(), suffix=self.var_suffix.get())
            mapping = {k: dict(v) for k, v in self.mapping.items()}
            self.btn_conv.state(["disabled"])
            self.btn_batch.state(["disabled"])

            def work():
                try:
                    msg = batch_convert(src, dst, rec, mapping, factor, unit,
                                        log=lambda m: self.q.put(("log", m)), **kw)
                    self.q.put(("ok", msg))
                except Exception as ex:
                    self.q.put(("err", str(ex)))

            threading.Thread(target=work, daemon=True).start()
            self.root.after(100, self.poll)

        def poll(self):
            last = None
            while True:
                try:
                    kind, msg = self.q.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    last = msg
                    continue
                self.btn_conv.state(["!disabled"])
                self.btn_batch.state(["!disabled"])
                if kind == "ok":
                    self.status.config(text="Done.\n" + msg)
                else:
                    self.status.config(text="Error: " + msg)
                    messagebox.showerror("Conversion failed", msg)
                return
            if last:
                self.status.config(text=last)
            self.root.after(100, self.poll)

    root = tk.Tk()
    App(root)
    root.mainloop()


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Vector PDF -> DXF with colour->layer mapping and scale calibration")
    ap.add_argument("pdf", nargs="?", help="PDF file, or a folder for batch mode")
    ap.add_argument("out", nargs="?", help="output .dxf (or output folder in batch mode)")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--pages", default="all")
    ap.add_argument("--unit", default="m", choices=list(UNITS))
    ap.add_argument("--ratio", type=float, help="printed scale 1:N")
    ap.add_argument("--calib", help="x1,y1,x2,y2,real_distance  (PDF pt, origin top-left, in --unit)")
    ap.add_argument("--factor", type=float, help="drawing units per PDF point")
    ap.add_argument("--map", help="colour->layer JSON")
    ap.add_argument("--write-map", help="write default map JSON (with --list-colours)")
    ap.add_argument("--list-colours", action="store_true")
    ap.add_argument("--segments", type=int, default=12, help="segments per Bezier curve")
    ap.add_argument("--no-text", action="store_true")
    ap.add_argument("--hatch", action="store_true", help="fills as HATCH")
    ap.add_argument("--widths", action="store_true", help="keep stroke widths")
    ap.add_argument("--aci", action="store_true", help="layer colours as nearest ACI")
    ap.add_argument("--suffix", action="store_true", help="append _LINE/_PLINE/_HATCH/_DTEXT to layer names")
    ap.add_argument("--no-dashes", action="store_true", help="do not create linetypes from dash patterns")
    ap.add_argument("--recursive", action="store_true", help="batch: include sub-folders")
    a = ap.parse_args()
    if a.gui or not a.pdf:
        run_gui()
    else:
        run_cli(a)


if __name__ == "__main__":
    main()
