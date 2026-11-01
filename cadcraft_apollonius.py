#!/usr/bin/env python3
"""
cadcraft_apollonius.py - Problem of Apollonius (CCC) for CADCraft.

Given 3 circles, find every circle tangent to all three (up to 8) and draw
the ones you choose in the running CADCraft. Reuses Client from
cadcraft_chprop.py, so keep both files in the same folder.

  1. cadcraft --control 7979
  2. Select exactly 3 circles in CADCraft.
  3. python cadcraft_apollonius.py               list the solutions
     python cadcraft_apollonius.py --draw 1,3    draw solutions 1 and 3
     python cadcraft_apollonius.py --draw all
  Other:
     --circles x y r x y r x y r   skip CADCraft, solve for these circles
     --dump                        print raw reply of the selection query
     --test                        run the ground-truth test and exit
"""
import argparse
import itertools
import math
import os
import sys

EPS = 1e-9


# ----------------------------------------------------------------------
# Solver (pure Python)
# ----------------------------------------------------------------------
def _q(a, b):
    """Quadratic form diag(1, 1, -1) on 3-vectors."""
    return a[0] * b[0] + a[1] * b[1] - a[2] * b[2]


def apollonius(c1, c2, c3, tol=1e-7):
    """c = (x, y, r). Returns a list of (x, y, r, (t1, t2, t3)) where
    ti = +1 means externally tangent to circle i, -1 internally tangent
    (solution encloses circle i or sits inside it).

    For signs s_i the tangency conditions are
        (x-xi)^2 + (y-yi)^2 = (r + s_i*ri)^2.
    Subtracting circle 1's equation from 2 and 3 gives two LINEAR equations
    in (x, y, r); the remaining one is a quadratic. Solving the linear pair
    for two unknowns in terms of the third (the best-conditioned choice)
    also handles collinear centres."""
    circles = (c1, c2, c3)
    (x1, y1, r1) = c1
    sols = []
    for s in itertools.product((1, -1), repeat=3):
        if s[0] == -1:           # (s1,s2,s3) with r<0 is (-s1,-s2,-s3) with
            continue             # r>0, so s1=+1 and both root signs cover all
        # rows: a*x + b*y + c*r = d
        rows = []
        for i in (1, 2):
            xi, yi, ri = circles[i]
            rows.append((2 * (xi - x1), 2 * (yi - y1),
                         2 * (s[i] * ri - s[0] * r1),
                         (xi * xi + yi * yi - ri * ri)
                         - (x1 * x1 + y1 * y1 - r1 * r1)))
        # pick the free variable k giving the largest 2x2 determinant
        best = None
        for k in range(3):
            j, l = [m for m in range(3) if m != k]
            det = rows[0][j] * rows[1][l] - rows[0][l] * rows[1][j]
            if best is None or abs(det) > abs(best[1]):
                best = (k, det, j, l)
        k, det, j, l = best
        if abs(det) < EPS:
            continue             # degenerate (e.g. concentric): skip
        # base point (free var = 0) and direction (free var = 1)
        def solve(rhs0, rhs1):
            vj = (rhs0 * rows[1][l] - rows[0][l] * rhs1) / det
            vl = (rows[0][j] * rhs1 - rhs0 * rows[1][j]) / det
            return vj, vl
        base = [0.0] * 3
        base[j], base[l] = solve(rows[0][3], rows[1][3])
        d = [0.0] * 3
        d[k] = 1.0
        d[j], d[l] = solve(-rows[0][k], -rows[1][k])
        e = (base[0] - x1, base[1] - y1, base[2] + s[0] * r1)
        A, B, C = _q(d, d), _q(e, d), _q(e, e)
        if abs(A) < EPS:
            ws = [-C / (2 * B)] if abs(B) > EPS else []
        else:
            disc = B * B - A * C
            if disc < -tol:
                continue
            sq = math.sqrt(max(disc, 0.0))
            ws = [(-B + sq) / A, (-B - sq) / A]
        for w in ws:
            x, y, r = (base[m] + d[m] * w for m in range(3))
            if abs(r) <= tol:
                continue
            if r < 0:            # negative radius = the opposite sign set
                r, ss = -r, tuple(-t for t in s)
            else:
                ss = s
            sols.append((x, y, r, ss))
    # verify + dedupe
    out = []
    for x, y, r, s in sols:
        ok = all(abs(math.hypot(x - ci[0], y - ci[1])
                     - abs(r + si * ci[2])) < 1e-6 * max(1.0, r)
                 for ci, si in zip(circles, s))
        if not ok:
            continue
        if any(abs(x - a) + abs(y - b) + abs(r - c) < 1e-6 * max(1.0, r)
               for a, b, c, _ in out):
            continue
        out.append((x, y, r, s))
    out.sort(key=lambda t: t[2])
    return out


def describe(sol, circles):
    x, y, r, _ = sol
    parts = []
    for n, (cx, cy, cr) in enumerate(circles, 1):
        dist = math.hypot(x - cx, y - cy)
        if abs(dist - (r + cr)) < 1e-6 * max(1.0, r):
            parts.append("C%d outside" % n)
        elif abs(dist - (r - cr)) < 1e-6 * max(1.0, r):
            parts.append("C%d inside it" % n)       # solution encloses Ci
        else:
            parts.append("C%d encloses it" % n)     # Ci encloses solution
    return ", ".join(parts)


# ----------------------------------------------------------------------
# Ground-truth test: circles measured off the screenshot (pixels, +-1)
# and the magenta solution drawn there.
# ----------------------------------------------------------------------
def self_test():
    circles = [(140, 140, 130), (98, 358, 42), (358, 315, 86)]
    want = (313, 393, 176)
    sols = apollonius(*circles)
    print("test: %d solutions" % len(sols))
    for n, s in enumerate(sols, 1):
        print("  %d: x=%.2f y=%.2f r=%.2f  %s" % (n, s[0], s[1], s[2],
                                                  describe(s, circles)))
    hit = [s for s in sols
           if max(abs(s[0] - want[0]), abs(s[1] - want[1]),
                  abs(s[2] - want[2])) < 3]
    print("magenta circle %s %s" % (want, "FOUND" if hit else "MISSING"))
    # exact synthetic case: unit circles at the corners of an equilateral
    # triangle -> inner Soddy circle r = 2/sqrt(3) - 1
    h = math.sqrt(3)
    tri = [(0, 0, 1), (2, 0, 1), (1, h, 1)]
    inner = 2 / h - 1
    sols2 = apollonius(*tri)
    ok2 = any(abs(s[0] - 1) < 1e-9 and abs(s[1] - h / 3) < 1e-9
              and abs(s[2] - inner) < 1e-9 for s in sols2)
    print("equilateral inner Soddy circle (r=%.6f) %s" %
          (inner, "FOUND" if ok2 else "MISSING"))
    # collinear centres must not crash
    print("collinear centres: %d solutions" %
          len(apollonius((0, 0, 1), (4, 0, 1), (8, 0, 2))))
    return 0 if (hit and ok2) else 1


# ----------------------------------------------------------------------
# CADCraft side
# ----------------------------------------------------------------------
def _num(d, keys):
    for k in keys:
        if isinstance(d.get(k), (int, float)):
            return float(d[k])
    return None


def _center(d):
    for k in ("center", "centre", "c", "origin"):
        v = d.get(k)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            return float(v[0]), float(v[1])
        if isinstance(v, dict):
            x, y = _num(v, ("x", "X")), _num(v, ("y", "Y"))
            if x is not None and y is not None:
                return x, y
    x, y = _num(d, ("cx", "x", "X")), _num(d, ("cy", "y", "Y"))
    if x is not None and y is not None:
        return x, y
    return None


def find_circles(obj, out=None):
    """Walk any reply and collect (x, y, r) from dicts that look like circles."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        kind = str(obj.get("type", obj.get("kind", obj.get("entity", "")))
                   ).upper()
        r = _num(obj, ("radius", "r", "R"))
        c = _center(obj)
        if "CIRCLE" in kind and r is not None and c is not None:
            out.append((c[0], c[1], r))
        else:
            for v in obj.values():
                find_circles(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_circles(v, out)
    return out


def read_selected(cl):
    from cadcraft_chprop import CadError
    reply = cl.execute("entities", {"selected": True, "limit": 50})
    circles = find_circles(reply)
    if len(circles) != 3:
        raise CadError(
            "Expected exactly 3 selected circles, found %d. Run with --dump "
            "to see what CADCraft returned." % len(circles))
    return circles, reply


def draw_circle(cl, x, y, r):
    # no spaces: a space acts as Enter at the CADCraft command line
    cl.type("CIRCLE")
    cl.type("%.10g,%.10g" % (x, y))
    cl.type("%.10g" % r)


def main():
    ap = argparse.ArgumentParser(description="Apollonius CCC for CADCraft")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CADCRAFT_CONTROL_PORT", 7979)))
    ap.add_argument("--token", default=None)
    ap.add_argument("--draw", default=None,
                    help="comma list of solution numbers, or 'all'")
    ap.add_argument("--circles", type=float, nargs=9, metavar="V")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if args.test:
        return self_test()

    cl = None
    try:
        if args.circles:
            v = args.circles
            circles = [tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9])]
        else:
            from cadcraft_chprop import Client
            cl = Client(args.port, args.token)
            if args.dump:
                print(cl.execute("entities", {"selected": True, "limit": 50}))
                return 0
            circles, _ = read_selected(cl)

        for n, c in enumerate(circles, 1):
            print("C%d: x=%.6g y=%.6g r=%.6g" % ((n,) + c))
        sols = apollonius(*circles)
        if not sols:
            print("No tangent circles (degenerate input, e.g. concentric).")
            return 1
        print("%d solution(s):" % len(sols))
        for n, s in enumerate(sols, 1):
            print("  %d: x=%.6g y=%.6g r=%.6g   %s"
                  % (n, s[0], s[1], s[2], describe(s, circles)))

        if args.draw:
            if cl is None:
                print("--draw needs CADCraft (omit --circles).")
                return 1
            pick = (range(1, len(sols) + 1) if args.draw.lower() == "all"
                    else [int(t) for t in args.draw.split(",")])
            for n in pick:
                draw_circle(cl, *sols[n - 1][:3])
                print("drew solution %d" % n)
        return 0
    except Exception as e:                       # CadError, bad --draw, ...
        print("ERROR:", e)
        return 1
    finally:
        if cl:
            cl.close()


if __name__ == "__main__":
    sys.exit(main())
