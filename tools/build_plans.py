#!/usr/bin/env python3
"""Draw candidate DPS director-district maps from whole precincts.

The statute sets the rules. C.R.S. 22-31-109(2)(b): director districts "shall
be contiguous, compact, and composed of whole precincts". C.R.S.
22-31-109(2)(c): "as nearly equal in population as possible based upon the most
recent federal census". Evenwel v. Abbott, 578 U.S. 54, 59 (2016) treats a plan
as presumptively compliant when the maximum deviation between the largest and
smallest district is under 10%.

So the precinct is the atom, contiguity is a hard constraint rather than a
preference, and the score to beat is maximum deviation, with compactness as the
tie-break.

Usage:
    python3 tools/build_plans.py [SIZES] [OUT]

SIZES is a comma-separated list of district counts, default "7,9".
"""

import json
import math
import os
import random
import sys

from shapely.geometry import shape
from shapely.ops import unary_union

SIZES = [int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "7,9").split(",")]
OUT = sys.argv[2] if len(sys.argv) > 2 else "data/plans.json"

GEO = "data/precincts.geojson"
# Population is the only lawful measure here. Registered voters stand in only
# so the search can be exercised before the census file lands, and a plan built
# on them is never written out as a proposal.
POP_FIELD = "pop2020"
FALLBACK_FIELD = "registered"


def load():
    doc = json.load(open(GEO))
    ids, w, geoms, props = [], {}, {}, {}
    field = POP_FIELD
    if not any(POP_FIELD in f["properties"] for f in doc["features"]):
        field = FALLBACK_FIELD
    for f in doc["features"]:
        p = f["properties"]
        n = p["precinct"]
        ids.append(n)
        w[n] = p.get(field) or 0
        geoms[n] = shape(f["geometry"]).buffer(0)
        props[n] = p
    return ids, w, geoms, props, field


def adjacency(ids, geoms):
    """Who touches whom. Any shared boundary counts, a single corner included,
    which is the rule Denver's own maps use: precinct 801 in Cole reaches the
    rest of its district only through a corner."""
    adj = {n: set() for n in ids}
    boxes = {n: geoms[n].bounds for n in ids}
    for i, a in enumerate(ids):
        ax0, ay0, ax1, ay1 = boxes[a]
        for b in ids[i + 1:]:
            bx0, by0, bx1, by1 = boxes[b]
            if ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0:
                continue
            if geoms[a].intersects(geoms[b]):
                adj[a].add(b)
                adj[b].add(a)
    return adj


def connected(members, adj):
    members = set(members)
    if not members:
        return True
    start = next(iter(members))
    seen, stack = {start}, [start]
    while stack:
        for nb in adj[stack.pop()]:
            if nb in members and nb not in seen:
                seen.add(nb)
                stack.append(nb)
    return len(seen) == len(members)


def deviation(assign, w, k):
    tot = sum(w.values())
    ideal = tot / k
    sums = [0.0] * k
    for n, d in assign.items():
        sums[d] += w[n]
    return (max(sums) - min(sums)) / ideal, sums


def grow(ids, w, adj, centres, k, rng):
    """Seed k precincts far apart, then let the hungriest district eat next.

    Always feeding the smallest district keeps the result close to balanced
    before local search starts, and taking the neighbour nearest the district's
    centre of mass keeps the shapes from wandering.
    """
    seeds = [rng.choice(ids)]
    while len(seeds) < k:
        far, best = None, -1
        for n in ids:
            d = min((centres[n][0] - centres[s][0]) ** 2 +
                    (centres[n][1] - centres[s][1]) ** 2 for s in seeds)
            if d > best:
                best, far = d, n
        seeds.append(far)

    assign = {}
    members = [set() for _ in range(k)]
    sums = [0.0] * k
    for i, s in enumerate(seeds):
        assign[s] = i
        members[i].add(s)
        sums[i] = w[s]

    frontier = [set(nb for nb in adj[s] if nb not in assign) for s in seeds]
    while len(assign) < len(ids):
        order = sorted(range(k), key=lambda i: sums[i])
        moved = False
        for i in order:
            cand = [n for n in frontier[i] if n not in assign]
            if not cand:
                continue
            cx = sum(centres[n][0] for n in members[i]) / len(members[i])
            cy = sum(centres[n][1] for n in members[i]) / len(members[i])
            pick = min(cand, key=lambda n: (centres[n][0] - cx) ** 2 +
                                           (centres[n][1] - cy) ** 2)
            assign[pick] = i
            members[i].add(pick)
            sums[i] += w[pick]
            frontier[i].update(nb for nb in adj[pick] if nb not in assign)
            moved = True
            break
        if not moved:
            # an unassigned pocket no district can reach: give it to the
            # smallest neighbouring district and carry on
            left = [n for n in ids if n not in assign]
            for n in left:
                touching = [assign[nb] for nb in adj[n] if nb in assign]
                if touching:
                    i = min(touching, key=lambda j: sums[j])
                    assign[n] = i
                    members[i].add(n)
                    sums[i] += w[n]
                    frontier[i].update(x for x in adj[n] if x not in assign)
                    break
            else:
                break
    return assign


def polish(assign, ids, w, adj, k, target=0.04, rounds=200000):
    """Balance first, then tidy the shapes, never breaking a district apart.

    Two passes. The first minimises the sum of squared distances from the ideal
    population, which unlike chasing the largest-to-smallest gap can climb out
    of the case where one district is boxed in and starving. The second trims
    the number of precinct pairs that straddle a district line, a cheap stand-in
    for compactness, and refuses any move that pushes the deviation back over
    the target it inherited.
    """
    members = [set() for _ in range(k)]
    for n, d in assign.items():
        members[d].add(n)
    sums = [sum(w[n] for n in members[i]) for i in range(k)]
    ideal = sum(w.values()) / k

    def spread():
        return (max(sums) - min(sums)) / ideal

    def border():
        """Precincts with a neighbour in another district: the movable set."""
        out = []
        for n in assign:
            for nb in adj[n]:
                if assign[nb] != assign[n]:
                    out.append(n)
                    break
        return out

    def imbalance():
        return sum((s - ideal) ** 2 for s in sums)

    def can_leave(n):
        return connected(members[assign[n]] - {n}, adj)

    # pass one: population
    for _ in range(rounds):
        best, gain = None, 1e-9
        cur = imbalance()
        for n in border():
            d = assign[n]
            if len(members[d]) <= 1:
                continue
            for t in {assign[nb] for nb in adj[n]} - {d}:
                after = cur - (sums[d] - ideal) ** 2 - (sums[t] - ideal) ** 2 \
                        + (sums[d] - w[n] - ideal) ** 2 + (sums[t] + w[n] - ideal) ** 2
                g = cur - after
                if g > gain and can_leave(n):
                    best, gain = (n, d, t), g
        if not best:
            break
        n, d, t = best
        members[d].discard(n); members[t].add(n)
        sums[d] -= w[n]; sums[t] += w[n]
        assign[n] = t

    # pass two: shape, without giving back the balance
    cap = max(target, spread())
    cuts = lambda: sum(1 for n in assign for nb in adj[n]
                       if nb > n and assign[nb] != assign[n])
    for _ in range(rounds):
        cur = cuts()
        best = None
        for n in border():
            d = assign[n]
            if len(members[d]) <= 1:
                continue
            for t in {assign[nb] for nb in adj[n]} - {d}:
                nd, nt = sums[d] - w[n], sums[t] + w[n]
                trial = [nd if i == d else nt if i == t else sums[i]
                         for i in range(k)]
                if (max(trial) - min(trial)) / ideal > cap:
                    continue
                delta = 0
                for nb in adj[n]:
                    if assign[nb] == d:
                        delta += 1
                    elif assign[nb] == t:
                        delta -= 1
                if delta >= 0:
                    continue
                if not can_leave(n):
                    continue
                if best is None or delta < best[0]:
                    best = (delta, n, d, t)
        if not best:
            break
        _, n, d, t = best
        members[d].discard(n); members[t].add(n)
        sums[d] -= w[n]; sums[t] += w[n]
        assign[n] = t

    return assign


def compactness(assign, geoms, k):
    """Polsby-Popper, 4*pi*area over perimeter squared. 1 is a circle."""
    out = []
    for i in range(k):
        part = unary_union([geoms[n] for n, d in assign.items() if d == i])
        if part.is_empty or part.length == 0:
            out.append(0.0)
            continue
        out.append(4 * math.pi * part.area / (part.length ** 2))
    return out


# Where to stop trading balance for shape. Past about 4% the districts stop
# getting rounder and the deviation just climbs toward the 10% line, which
# hands a critic a number for nothing. Below it the shape pass has no room.
SHAPE_CAP = 0.04


def build(k, ids, w, adj, centres, geoms, tries=24, seed=11):
    rng = random.Random(seed + k)
    best = None
    for _ in range(tries):
        a = grow(ids, w, adj, centres, k, rng)
        if len(a) != len(ids):
            continue
        a = polish(dict(a), ids, w, adj, k, target=SHAPE_CAP)
        members = [set() for _ in range(k)]
        for n, d in a.items():
            members[d].add(n)
        if any(not connected(m, adj) or not m for m in members):
            continue
        dev, sums = deviation(a, w, k)
        comp = compactness(a, geoms, k)
        # inside the safe harbour, prefer the rounder map; outside it, prefer
        # the better balanced one
        score = (dev > SHAPE_CAP * 1.5, -sum(comp) / k, dev)
        if best is None or score < best[0]:
            best = (score, a, dev, sums, comp)
    return best


def main():
    ids, w, geoms, props, field = load()
    print("precincts: %d, balancing on %s (total %s)"
          % (len(ids), field, format(int(sum(w.values())), ",")))
    if field != POP_FIELD:
        print("  ! census population not loaded yet, so these are a rehearsal,")
        print("    not a proposal. Add pop2020 to precincts.geojson and rerun.")

    centres = {n: (geoms[n].representative_point().x,
                   geoms[n].representative_point().y) for n in ids}
    adj = adjacency(ids, geoms)
    print("  adjacency: %d precinct pairs touch"
          % (sum(len(v) for v in adj.values()) // 2))

    plans = []
    for k in SIZES:
        got = build(k, ids, w, adj, centres, geoms)
        if not got:
            print("  %d districts: no contiguous plan found" % k)
            continue
        _score, a, dev, sums, comp = got
        print("\n  %d districts   max deviation %.2f%%   %s   mean compactness %.3f"
              % (k, 100 * dev,
                 "within the 10% safe harbour" if dev < .10 else "OVER 10%",
                 sum(comp) / k))
        ideal = sum(w.values()) / k
        for i in range(k):
            n = sum(1 for x in a.values() if x == i)
            print("     district %-2d %3d precincts %9s %+6.2f%%  compactness %.3f"
                  % (i + 1, n, format(int(sums[i]), ","),
                     100 * (sums[i] - ideal) / ideal, comp[i]))
        plans.append({
            "key": "k%d" % k,
            "districts": k,
            "label": "%d districts" % k,
            "basis": field,
            "maxDeviation": round(100 * dev, 2),
            "compactness": [round(c, 3) for c in comp],
            "totals": [int(s) for s in sums],
            "assign": {n: a[n] + 1 for n in ids},
        })

    if field != POP_FIELD:
        print("\nnothing written: rerun once pop2020 is in precincts.geojson")
        return
    with open(OUT, "w") as fh:
        json.dump({"basis": field, "plans": plans,
                   "adjacency": {n: sorted(adj[n]) for n in ids}},
                  fh, separators=(",", ":"))
    print("\nwrote %s (%d KB)" % (OUT, os.path.getsize(OUT) // 1024))


if __name__ == "__main__":
    main()
