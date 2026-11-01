#!/usr/bin/env python3
"""
cadcraft_apollonius_gui.py - GUI for the Problem of Apollonius (CCC).

Needs cadcraft_apollonius.py and cadcraft_chprop.py in the same folder.

  1. cadcraft --control 7979
  2. Select exactly 3 circles in CADCraft.
  3. python cadcraft_apollonius_gui.py        (or --port 7979)
  4. Tick the solutions you want, press "Draw checked".
     "Read selection" re-reads the 3 circles after you change the selection.

  --demo    run without CADCraft on the screenshot test circles
"""
import argparse
import math
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox

from cadcraft_apollonius import (apollonius, describe, read_selected,
                                 draw_circle)

INPUT_COLOR = "#202020"
PALETTE = ["#d6336c", "#1c7ed6", "#2f9e44", "#e8590c",
           "#7048e8", "#0c8599", "#a61e4d", "#5c940d"]
SIZE = 600
MARGIN = 20


def run_gui(port, token, demo):
    root = tk.Tk()
    root.title("CADCraft - Apollonius (CCC)")

    cl = None
    if not demo:
        try:
            from cadcraft_chprop import Client, CadError
            cl = Client(port, token)
        except Exception as e:
            root.withdraw()
            messagebox.showerror("Apollonius", str(e))
            return 1

    state = {"circles": [], "sols": []}

    frm = ttk.Frame(root, padding=10)
    frm.grid()
    canvas = tk.Canvas(frm, width=SIZE, height=SIZE, bg="white",
                       highlightthickness=1, highlightbackground="#999")
    canvas.grid(row=0, column=0, rowspan=3, padx=(0, 10))

    side = ttk.Frame(frm)
    side.grid(row=0, column=1, sticky="nw")
    inp_var = tk.StringVar()
    ttk.Label(side, textvariable=inp_var, justify="left").grid(
        row=0, column=0, sticky="w", pady=(0, 8))

    list_frame = ttk.Frame(side)
    list_frame.grid(row=1, column=0, sticky="w")
    checks = []          # BooleanVars, one per solution

    fit_all = tk.BooleanVar(value=True)
    ttk.Checkbutton(side, text="Zoom to fit all solutions", variable=fit_all,
                    command=lambda: redraw()).grid(row=2, column=0,
                                                   sticky="w", pady=6)

    log_box = tk.Text(frm, width=44, height=8, state="disabled")
    log_box.grid(row=1, column=1, sticky="nw", pady=(8, 0))

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    # ---- drawing ------------------------------------------------------
    def redraw(*_):
        canvas.delete("all")
        circles, sols = state["circles"], state["sols"]
        if not circles:
            return
        shown = [(i, s) for i, s in enumerate(sols) if checks[i].get()]
        pool = list(circles)
        if fit_all.get():
            pool += [s[:3] for s in sols]
        else:
            pool += [s[:3] for _, s in shown]
        xmin = min(c[0] - c[2] for c in pool)
        xmax = max(c[0] + c[2] for c in pool)
        ymin = min(c[1] - c[2] for c in pool)
        ymax = max(c[1] + c[2] for c in pool)
        span = max(xmax - xmin, ymax - ymin, 1e-9)
        k = (SIZE - 2 * MARGIN) / span
        ox = MARGIN + ((SIZE - 2 * MARGIN) - (xmax - xmin) * k) / 2
        oy = MARGIN + ((SIZE - 2 * MARGIN) - (ymax - ymin) * k) / 2

        def px(x):           # CAD y is up, canvas y is down
            return ox + (x - xmin) * k

        def py(y):
            return SIZE - (oy + (y - ymin) * k)

        def circ(c, **kw):
            x, y, r = c
            canvas.create_oval(px(x - r), py(y + r), px(x + r), py(y - r),
                               **kw)

        for i, s in enumerate(sols):
            if checks[i].get():
                circ(s[:3], outline=PALETTE[i % 8], width=2)
                canvas.create_text(px(s[0]), py(s[1]), text=str(i + 1),
                                   fill=PALETTE[i % 8],
                                   font=("TkDefaultFont", 10, "bold"))
            else:
                circ(s[:3], outline="#bbb", dash=(3, 3))
        for n, c in enumerate(circles, 1):
            circ(c, outline=INPUT_COLOR, width=2)
            canvas.create_text(px(c[0]), py(c[1]), text="C%d" % n,
                               fill=INPUT_COLOR)

    # ---- data ---------------------------------------------------------
    def load(circles):
        state["circles"] = circles
        state["sols"] = sols = apollonius(*circles)
        inp_var.set("\n".join("C%d: x=%.5g  y=%.5g  r=%.5g" % ((n,) + c)
                              for n, c in enumerate(circles, 1)))
        for w in list_frame.winfo_children():
            w.destroy()
        checks.clear()
        for i, s in enumerate(sols):
            v = tk.BooleanVar(value=True)
            v.trace_add("write", redraw)
            checks.append(v)
            row = ttk.Frame(list_frame)
            row.grid(row=i, column=0, sticky="w")
            tk.Label(row, text="\u25a0", fg=PALETTE[i % 8]).grid(row=0,
                                                                  column=0)
            ttk.Checkbutton(
                row, variable=v,
                text="%d: r=%.5g  (%.5g, %.5g)\n    %s"
                % (i + 1, s[2], s[0], s[1], describe(s, circles))
            ).grid(row=0, column=1, sticky="w")
        if not sols:
            log("No tangent circles (degenerate input).")
        redraw()

    def read_sel():
        try:
            circles, _ = read_selected(cl)
        except Exception as e:
            log("ERROR: %s" % e)
            return
        load(circles)
        log("Read 3 circles, %d solution(s)." % len(state["sols"]))

    def draw(which):
        if cl is None:
            log("Demo mode: no CADCraft connected.")
            return
        idx = [i for i in range(len(state["sols"]))
               if which == "all" or checks[i].get()]
        if not idx:
            log("Nothing checked.")
            return
        try:
            for i in idx:
                draw_circle(cl, *state["sols"][i][:3])
                log("drew solution %d" % (i + 1))
        except Exception as e:
            log("ERROR: %s" % e)

    def set_all(val):
        for v in checks:
            v.set(val)

    btns = ttk.Frame(frm)
    btns.grid(row=2, column=1, sticky="se", pady=(8, 0))
    ttk.Button(btns, text="Read selection", command=read_sel).grid(
        row=0, column=0, padx=3)
    ttk.Button(btns, text="All", command=lambda: set_all(True)).grid(
        row=0, column=1, padx=3)
    ttk.Button(btns, text="None", command=lambda: set_all(False)).grid(
        row=0, column=2, padx=3)
    ttk.Button(btns, text="Draw checked", command=lambda: draw("checked")
               ).grid(row=0, column=3, padx=3)
    ttk.Button(btns, text="Close", command=root.destroy).grid(
        row=0, column=4, padx=3)

    if demo:
        load([(140, 140, 130), (98, 358, 42), (358, 315, 86)])
    else:
        read_sel()
    root.mainloop()
    if cl:
        cl.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="Apollonius GUI for CADCraft")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CADCRAFT_CONTROL_PORT", 7979)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    return run_gui(a.port, a.token, a.demo)


if __name__ == "__main__":
    sys.exit(main())
