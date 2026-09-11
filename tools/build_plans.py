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
POP_FILE = "data/pop2020.json"
# Population is the only lawful measure here. Registered voters stand in only
# so the search can be exercised before the census file lands, and a plan built
# on them is never written out as a proposal.
POP_FIELD = "pop2020"
FALLBACK_FIELD = "registered"


def load():
    doc = json.load(open(GEO))
    ids, w, geoms, props = [], {}, {}, {}
    pop = {}
    if os.path.exists(POP_FILE):
        pop = json.load(open(POP_FILE)).get("pop", {})
    field = POP_FIELD if pop else FALLBACK_FIELD
    for f in doc["features"]:
        p = f["properties"]
        n = p["precinct"]
        ids.append(n)
        w[n] = pop.get(str(n), 0) if pop else (p.get(field) or 0)
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


def polish(assign, ids, w, adj, k, nbhd=None, nb_weight=0.0,
           target=0.04, rounds=200000):
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

    # pass two: shape and neighbourhoods, without giving back the balance
    cap = max(target, spread())
    nbhd = nbhd or {}

    # how many precincts of each neighbourhood sit in each district
    tally = {}
    for n, d in assign.items():
        g = nbhd.get(n)
        if g:
            tally.setdefault(g, {})
            tally[g][d] = tally[g].get(d, 0) + 1

    def split_delta(n, d, t):
        """Change in the neighbourhood-split count if n moves from d to t."""
        g = nbhd.get(n)
        if not g or d == t:
            return 0
        c = tally[g]
        before = sum(1 for v in c.values() if v) - 1
        after_d = c.get(d, 0) - 1
        after_t = c.get(t, 0) + 1
        parts = sum(1 for kk, v in c.items() if kk not in (d, t) and v)
        parts += (1 if after_d else 0) + (1 if after_t else 0)
        return (parts - 1) - before

    def apply_split(n, d, t):
        g = nbhd.get(n)
        if not g or d == t:
            return
        c = tally[g]
        c[d] = c.get(d, 0) - 1
        if not c[d]:
            del c[d]
        c[t] = c.get(t, 0) + 1

    for _ in range(rounds):
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
                cost = delta + nb_weight * split_delta(n, d, t)
                if cost >= 0:
                    continue
                if not can_leave(n):
                    continue
                if best is None or cost < best[0]:
                    best = (cost, n, d, t)
        if not best:
            break
        _, n, d, t = best
        apply_split(n, d, t)
        members[d].discard(n); members[t].add(n)
        sums[d] -= w[n]; sums[t] += w[n]
        assign[n] = t

    return assign


def nbhd_splits(assign, nbhd):
    """How many neighbourhoods land in more than one district.

    A precinct carries one neighbourhood label, the dominant one, because
    precincts straddle neighbourhood lines. Keeping a neighbourhood whole
    therefore means keeping every precinct that shares its label together,
    which is the most the statute's whole-precinct rule allows.
    """
    seen = {}
    for n, d in assign.items():
        g = nbhd.get(n)
        if not g:
            continue
        seen.setdefault(g, set()).add(d)
    return sum(len(v) - 1 for v in seen.values()), \
           sorted(g for g, v in seen.items() if len(v) > 1)



def anneal(assign, ids, w, adj, k, nbhd, cap, iters=80000, seed=0,
           a_cut=1.0, a_split=4.0, a_dev=260.0, cap_end=None):
    """Simulated annealing on the whole objective at once.

    The greedy passes get stuck: each one only takes a move that improves its
    own measure right now, so a map that would need two steps to keep a
    neighbourhood whole never takes the first. Annealing accepts an occasional
    worse move early on and cools into a better answer. Population stays a hard
    constraint at the cap; shape and neighbourhoods trade against each other
    inside it.
    """
    rng = random.Random(seed)
    assign = dict(assign)
    members = [set() for _ in range(k)]
    for n, d in assign.items():
        members[d].add(n)
    sums = [sum(w[n] for n in members[i]) for i in range(k)]
    ideal = sum(w.values()) / k

    tally = {}
    for n, d in assign.items():
        g = nbhd.get(n)
        if g:
            tally.setdefault(g, {})
            tally[g][d] = tally[g].get(d, 0) + 1

    def splits_now():
        return sum(sum(1 for v in c.values() if v) - 1 for c in tally.values())

    def cuts_now():
        return sum(1 for n in assign for nb in adj[n]
                   if nb > n and assign[nb] != assign[n])

    def imbal(ss):
        return math.sqrt(sum((s / ideal - 1) ** 2 for s in ss) / k)

    cuts, splits = cuts_now(), splits_now()
    cost = a_cut * cuts + a_split * splits + a_dev * imbal(sums)
    start_dev = (max(sums) - min(sums)) / ideal
    best = (cost if start_dev <= (cap_end or cap) else float("inf"),
            dict(assign))

    cap_end = cap if cap_end is None else min(cap, cap_end)
    border = [n for n in ids if any(assign[nb] != assign[n] for nb in adj[n])]
    t0, t1 = 2.5, 0.02
    for step in range(iters):
        T = t0 * (t1 / t0) ** (step / iters)
        # squeeze the population cap down over the first half of the run.
        # While the map is still above the schedule, only moves that actually
        # reduce deviation are legal, which is what keeps this from either
        # deadlocking or never tightening at all.
        f = min(1.0, step / (0.5 * iters))
        cap_now = cap + (cap_end - cap) * f
        cur_dev = (max(sums) - min(sums)) / ideal
        if not border:
            break
        n = border[rng.randrange(len(border))]
        d = assign[n]
        opts = [assign[nb] for nb in adj[n] if assign[nb] != d]
        if not opts or len(members[d]) <= 1:
            continue
        t = opts[rng.randrange(len(opts))]

        nd, nt = sums[d] - w[n], sums[t] + w[n]
        trial = [nd if i == d else nt if i == t else sums[i] for i in range(k)]
        td = (max(trial) - min(trial)) / ideal
        if td > cap_now and td >= cur_dev:
            continue

        dcut = 0
        for nb in adj[n]:
            if assign[nb] == d:
                dcut += 1
            elif assign[nb] == t:
                dcut -= 1

        g = nbhd.get(n)
        dsplit = 0
        if g:
            c = tally[g]
            before = sum(1 for v in c.values() if v) - 1
            ad, at = c.get(d, 0) - 1, c.get(t, 0) + 1
            parts = sum(1 for kk, v in c.items() if kk not in (d, t) and v)
            parts += (1 if ad else 0) + (1 if at else 0)
            dsplit = (parts - 1) - before

        dcost = (a_cut * dcut + a_split * dsplit
                 + a_dev * (imbal(trial) - imbal(sums)))
        if dcost > 0 and rng.random() >= math.exp(-dcost / T):
            continue
        if not connected(members[d] - {n}, adj):
            continue

        members[d].discard(n); members[t].add(n)
        sums[d], sums[t] = nd, nt
        assign[n] = t
        if g:
            c = tally[g]
            c[d] = c.get(d, 0) - 1
            if not c[d]:
                del c[d]
            c[t] = c.get(t, 0) + 1
        cost += dcost
        cur_dev = td
        touched = [n] + list(adj[n])
        for x in touched:
            on = any(assign[nb] != assign[x] for nb in adj[x])
            inb = x in border
            if on and not inb:
                border.append(x)
            elif inb and not on:
                border.remove(x)
        # only snapshot maps that already meet the population target, or the
        # run happily returns a good-looking map from before the cap tightened
        if cur_dev <= cap_end + 1e-9 and cost < best[0] - 1e-9:
            best = (cost, dict(assign))
    return best[1]


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


# Where to stop trading population equality for shape. Looser caps do buy
# rounder districts and fewer split neighbourhoods, but a draft that is less
# equal than the map DPS already uses (4.33%) hands a critic the only number
# they need. So the cap sits below that, and everything else is optimised
# underneath it.
SHAPE_CAP = 0.035

# What one split neighbourhood is worth in units of "precinct pairs straddling
# a district line". Scott's call: keep neighbourhoods whole where we can, and
# spend a point or two of deviation to do it, but never make it absolute.
NB_WEIGHT = 4.0
ANNEAL_ITERS = 100000
HOPS = 30
HOP_ITERS = 70000
# Nothing above this is worth showing: it burns the safe harbour for nothing.
HARD_DEV = 0.06
TRIES = 20


def score_plan(a, w, geoms, nbhd, k):
    dev, sums = deviation(a, w, k)
    comp = compactness(a, geoms, k)
    splits, _ = nbhd_splits(a, nbhd)
    quality = sum(comp) / k - 0.02 * splits - 1.5 * dev
    return quality, dev, sums, comp, splits


def build(k, ids, w, adj, centres, geoms, nbhd, nb_weight=NB_WEIGHT,
          cap=SHAPE_CAP, tries=TRIES, seed=11):
    """Roll a batch of maps, then stop rolling and start improving.

    With two cores, more random restarts buy less than refining the best map
    already in hand. Phase one explores. Phase two perturbs the leader and
    re-anneals cool, keeping anything better, which is basin hopping: it walks
    between nearby good maps instead of starting over from nothing each time.
    """
    rng = random.Random(seed + k)
    pool = []
    for _ in range(tries):
        a = grow(ids, w, adj, centres, k, rng)
        if len(a) != len(ids):
            continue
        a = polish(dict(a), ids, w, adj, k, nbhd=nbhd,
                   nb_weight=nb_weight, target=cap)
        dev0, _ = deviation(a, w, k)
        a = anneal(a, ids, w, adj, k, nbhd, max(cap, dev0),
                   iters=ANNEAL_ITERS, seed=rng.randrange(1 << 30),
                   a_split=nb_weight, cap_end=cap)
        members = [set() for _ in range(k)]
        for n, d in a.items():
            members[d].add(n)
        if any(not connected(m, adj) or not m for m in members):
            continue
        q, dev, sums, comp, splits = score_plan(a, w, geoms, nbhd, k)
        if dev > HARD_DEV:
            continue
        pool.append((q, a))
    if not pool:
        return None
    pool.sort(key=lambda t: -t[0])
    leaders = [p[1] for p in pool[:3]]
    best_q, best_a = pool[0]

    for hop in range(HOPS):
        src_a = leaders[hop % len(leaders)] if hop < len(leaders) * 2 else best_a
        dev0, _ = deviation(src_a, w, k)
        a = anneal(dict(src_a), ids, w, adj, k, nbhd, max(cap, dev0),
                   iters=HOP_ITERS, seed=rng.randrange(1 << 30),
                   a_split=nb_weight, cap_end=cap)
        members = [set() for _ in range(k)]
        for n, d in a.items():
            members[d].add(n)
        if any(not connected(m, adj) or not m for m in members):
            continue
        q, dev, _s, _c, _sp = score_plan(a, w, geoms, nbhd, k)
        if dev <= HARD_DEV and q > best_q:
            best_q, best_a = q, a
            leaders[0] = a

    q, dev, sums, comp, splits = score_plan(best_a, w, geoms, nbhd, k)
    return (q, best_a, dev, sums, comp, splits)


def baseline(field, props, ids, w, geoms, adj, nbhd, label):
    """Score a map Denver already uses, on exactly the same yardstick."""
    groups = sorted({str(props[n].get(field)) for n in ids
                     if props[n].get(field) not in (None, "", "0", 0)})
    idx = {g: i for i, g in enumerate(groups)}
    a = {n: idx[str(props[n][field])] for n in ids
         if props[n].get(field) not in (None, "", "0", 0)}
    k = len(groups)
    if k < 2:
        return None
    dev, sums = deviation(a, w, k)
    comp = compactness(a, geoms, k)
    splits, names = nbhd_splits(a, nbhd)
    return {"label": label, "districts": k, "maxDeviation": round(100 * dev, 2),
            "meanCompactness": round(sum(comp) / k, 3), "splits": splits,
            "splitNames": names, "totals": [int(s) for s in sums],
            "groups": groups, "assign": {n: a[n] + 1 for n in a}}


def show(label, k, dev, sums, comp, splits, ideal):
    print("\n  %s: max deviation %.2f%%   %s   mean compactness %.3f   "
          "neighbourhoods split %d"
          % (label, 100 * dev,
             "within the 10%% safe harbour" if dev < .10 else "OVER 10%%",
             sum(comp) / k, splits))
    for i in range(k):
        print("     district %-2d %9s %+6.2f%%  compactness %.3f"
              % (i + 1, format(int(sums[i]), ","),
                 100 * (sums[i] - ideal) / ideal, comp[i]))


def main():
    ids, w, geoms, props, field = load()
    print("precincts: %d, balancing on %s (total %s)"
          % (len(ids), field, format(int(sum(w.values())), ",")))
    if field != POP_FIELD:
        print("  ! census population not loaded, so this is a rehearsal.")

    nbhd = {n: (props[n].get("neighborhood") or "").strip() for n in ids}
    print("  neighbourhoods represented: %d"
          % len({g for g in nbhd.values() if g}))

    centres = {n: (geoms[n].representative_point().x,
                   geoms[n].representative_point().y) for n in ids}
    adj = adjacency(ids, geoms)
    print("  adjacency: %d precinct pairs touch"
          % (sum(len(v) for v in adj.values()) // 2))

    bases = []
    for f, lab in (("school_board", "DPS today"), ("council", "City Council")):
        b = baseline(f, props, ids, w, geoms, adj, nbhd, lab)
        if b:
            bases.append(b)
            print("\n  %s (%d districts, existing map): deviation %.2f%%   "
                  "compactness %.3f   neighbourhoods split %d"
                  % (lab, b["districts"], b["maxDeviation"],
                     b["meanCompactness"], b["splits"]))

    prior = {}
    if os.path.exists(OUT):
        try:
            for p in json.load(open(OUT)).get("plans", []):
                prior[p["key"]] = p
        except Exception:
            pass

    plans = []
    for k in SIZES:
        got = build(k, ids, w, adj, centres, geoms, nbhd)
        if not got:
            print("  %d districts: no contiguous plan found" % k)
            continue
        _q, a, dev, sums, comp, splits = got
        old = prior.get("k%d" % k)
        if old and "quality" in old:
            oa = {n: old["assign"][n] - 1 for n in ids if n in old["assign"]}
            if len(oa) == len(ids):
                oq, odev, osums, ocomp, osplits = score_plan(oa, w, geoms, nbhd, k)
                if oq > _q:
                    print("  %d districts: keeping the better map from an "
                          "earlier run (quality %.4f vs %.4f)" % (k, oq, _q))
                    a, dev, sums, comp, splits, _q = oa, odev, osums, ocomp, osplits, oq
        ideal = sum(w.values()) / k
        show("%d districts" % k, k, dev, sums, comp, splits, ideal)
        plans.append({
            "key": "k%d" % k,
            "districts": k,
            "label": "%d districts" % k,
            "basis": field,
            "maxDeviation": round(100 * dev, 2),
            "meanCompactness": round(sum(comp) / k, 3),
            "quality": round(_q, 5),
            "compactness": [round(c, 3) for c in comp],
            "splits": splits,
            "splitNames": nbhd_splits(a, nbhd)[1],
            "totals": [int(s) for s in sums],
            "assign": {n: a[n] + 1 for n in ids},
        })

    if field != POP_FIELD:
        print("\nnothing written: census population missing")
        return
    with open(OUT, "w") as fh:
        json.dump({"basis": field, "total": int(sum(w.values())),
                   "plans": plans, "baselines": bases,
                   "adjacency": {n: sorted(adj[n]) for n in ids}},
                  fh, separators=(",", ":"))
    print("\nwrote %s (%d KB)" % (OUT, os.path.getsize(OUT) // 1024))


if __name__ == "__main__":
    main()
