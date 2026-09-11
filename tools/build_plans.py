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
from shapely.ops import transform, unary_union
from pyproj import Transformer

SIZES = [int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "7,9").split(",")]
OUT = sys.argv[2] if len(sys.argv) > 2 else "data/plans.json"

GEO = "data/precincts.geojson"
POP_FILE = "data/pop2020.json"
RETURNS_FILE = "data/returns.json"
TURNOUT_YEAR = 2025
# Population is the only lawful measure here. Registered voters stand in only
# so the search can be exercised before the census file lands, and a plan built
# on them is never written out as a proposal.
POP_FIELD = "pop2020"
FALLBACK_FIELD = "registered"


# ---------------------------------------------------------------- tuning ----
# Where to stop trading population equality for shape. Deliberately under the
# 4.33% the current DPS map runs: a draft less equal than the map the district
# already uses hands a critic the only number they need.
SHAPE_CAP = 0.04
HARD_DEV = 0.06          # nothing looser is worth showing

# How a finished map is judged, and what the search therefore optimises.
# W_WORST is the share of the compactness score carried by the least compact
# district rather than the average, because a map can average well and still
# contain one district nobody can defend.
# Deviation is a CONSTRAINT, not an objective. The statute asks for districts
# as nearly equal as possible and the case law draws the line at 10%; nothing
# rewards 1.5% over 3%. Weighting it like a goal made the search buy a point of
# equality with a quarter of the compactness, which is the trade that produces
# maps people call gerrymandered. So the cap enforces the limit and W_DEV only
# breaks ties between maps that are otherwise equally good.
W_SPLIT = 0.006          # cost of one split neighbourhood, applied uniformly
W_DEV = 0.2              # tie-break only
W_WORST = 0.60
# Cost of one percentage point of average within-district turnout spread,
# in the same units as compactness. Scott's concern, and the reason it is
# here: when one corner of a district votes at 60% and another at 15%, that
# corner picks the district's representative and the rest ride along. It is
# the at-large problem reproduced inside a district.
# Zero on purpose. Turnout is measured on every finished map but never drawn
# on: the reachable gain is about one point out of ten, because Denver's
# turnout varies block to block rather than district to district, and buying
# it costs compactness the statute actually asks for.
W_TURN = 0.0
NB_WEIGHT = 4.0          # scales W_SPLIT inside the search only

# Named neighbourhoods that may never be divided. Deliberately EMPTY.
#
# Singling out particular communities to protect, however good the reason,
# is a choice about those communities, and a choice is the thing an opponent
# characterises. Every one of Denver's 78 neighbourhoods is weighed the same
# way instead, by a search that has no idea who lives in any of them. Whether
# a given neighbourhood survives whole is then an outcome, not a decision,
# and the map is defended by its method rather than by its intentions.
PROTECTED = ()

ANNEAL_ITERS = 250000
HOP_ITERS = 150000
HOPS = 14
TRIES = 16


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


def turnout(ids):
    """Per-precinct return rate and the ballots it was measured on.

    Weighted by ballots issued rather than by population: the question is how
    unevenly the people who actually vote are distributed inside a district,
    so the weight has to be the electorate, not the residents.
    """
    if not os.path.exists(RETURNS_FILE):
        return None
    doc = json.load(open(RETURNS_FILE))
    year = [e for e in doc["elections"] if e["year"] == TURNOUT_YEAR]
    if not year:
        return None
    rate, wt = {}, {}
    for n, (castn, issued) in year[0]["precincts"].items():
        if issued:
            rate[n] = 100.0 * castn / issued
            wt[n] = float(issued)
    for n in ids:
        rate.setdefault(n, 0.0)
        wt.setdefault(n, 0.0)
    return rate, wt


def turn_spread(assign, k, rate, wt):
    """Average, across districts, of the ballot-weighted standard deviation of
    precinct turnout inside the district. Percentage points. Lower is flatter."""
    sw = [0.0] * k; swx = [0.0] * k; swx2 = [0.0] * k
    for n, d in assign.items():
        w_ = wt.get(n, 0.0)
        if not w_:
            continue
        x = rate[n]
        sw[d] += w_; swx[d] += w_ * x; swx2[d] += w_ * x * x
    out = []
    for i in range(k):
        if sw[i] <= 0:
            out.append(0.0); continue
        m = swx[i] / sw[i]
        out.append(math.sqrt(max(0.0, swx2[i] / sw[i] - m * m)))
    return sum(out) / k, out


def unify_protected(assign, ids, w, adj, nbhd, protected=PROTECTED):
    """Pull each protected neighbourhood into a single district.

    Whichever district already holds the most of it takes the rest, provided
    the districts losing precincts stay in one piece. Runs once on the seed;
    after that the search refuses any move that would divide one again.
    """
    for g in protected:
        ms = [n for n in ids if nbhd.get(n) == g]
        if not ms:
            continue
        share = {}
        for n in ms:
            share[assign[n]] = share.get(assign[n], 0) + w[n]
        if len(share) < 2:
            continue
        home = max(share, key=share.get)
        for n in ms:
            if assign[n] == home:
                continue
            if home not in {assign[nb] for nb in adj[n]}:
                continue
            src = assign[n]
            members = {x for x in ids if assign[x] == src} - {n}
            if not members or not connected(members, adj):
                continue
            assign[n] = home
    return assign


def protected_ok(assign, ids, nbhd, protected=PROTECTED):
    for g in protected:
        ds = {assign[n] for n in ids if nbhd.get(n) == g}
        if len(ds) > 1:
            return False
    return True


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


def projected_centres(ids, geoms):
    tf = Transformer.from_crs(4326, 26954, always_xy=True)
    out = {}
    for n in ids:
        p = transform(lambda x, y, z=None: tf.transform(x, y),
                      geoms[n]).representative_point()
        out[n] = (p.x, p.y)
    return out


def voronoi_seed(ids, w, adj, C, area, k, rng, rounds=400):
    """Capacity-constrained Voronoi: compact districts by construction.

    Growing a district precinct by precinct produces ribbons, because a region
    that is cheap to reach stays attached however thin it gets, and no sequence
    of single-precinct moves can unwind it afterwards without passing through
    worse maps. A Voronoi cell cannot be a ribbon: every precinct belongs to
    the nearest centre, so the cells come out convex.

    The catch is that plain Voronoi cells are the wrong populations. Each
    centre carries a weight that grows while its district is under-populated
    and shrinks while it is over, which bends the boundaries until the cells
    balance. That is a power diagram, and it stays convex throughout.
    """
    cs = [C[rng.choice(ids)]]
    while len(cs) < k:                      # k-means++ spread, population-weighted
        d2 = [min((C[n][0] - c[0]) ** 2 + (C[n][1] - c[1]) ** 2 for c in cs)
              * max(w[n], 1) for n in ids]
        r, acc = rng.random() * sum(d2), 0.0
        for n, v in zip(ids, d2):
            acc += v
            if acc >= r:
                cs.append(C[n])
                break
    lam = [0.0] * k
    ideal = sum(w.values()) / k
    S = sum(area.values()) / k / math.pi     # a district's typical radius squared
    assign = {}
    for it in range(rounds):
        for n in ids:
            x, y = C[n]
            assign[n] = min(range(k), key=lambda j:
                            (x - cs[j][0]) ** 2 + (y - cs[j][1]) ** 2 - lam[j])
        pops = [0.0] * k
        for n, j in assign.items():
            pops[j] += w[n]
        eta = S * (0.9 if it < rounds * 0.5 else 0.25)
        for j in range(k):
            lam[j] += eta * (ideal - pops[j]) / ideal
        if it % 3 == 0:
            for j in range(k):
                ms = [n for n in ids if assign[n] == j]
                if not ms:
                    continue
                W = sum(max(w[n], 1) for n in ms)
                cs[j] = (sum(C[n][0] * max(w[n], 1) for n in ms) / W,
                         sum(C[n][1] * max(w[n], 1) for n in ms) / W)

    # A power diagram is convex in the plane, but precincts are not points, so
    # a cell can still end up with an orphan. Keep each district's largest
    # piece and hand the rest to whichever neighbour's centre is closest.
    for _ in range(80):
        strays = []
        for j in range(k):
            ms = {n for n in ids if assign[n] == j}
            if not ms:
                continue
            comps, seen = [], set()
            for s in ms:
                if s in seen:
                    continue
                stack, cur = [s], {s}
                seen.add(s)
                while stack:
                    for nb in adj[stack.pop()]:
                        if nb in ms and nb not in cur:
                            cur.add(nb); seen.add(nb); stack.append(nb)
                comps.append(cur)
            comps.sort(key=lambda c: -sum(w[n] for n in c))
            strays += comps[1:]
        if not strays:
            break
        for c in strays:
            for n in c:
                opts = [assign[nb] for nb in adj[n] if assign[nb] != assign[n]]
                if opts:
                    assign[n] = min(opts, key=lambda j:
                                    (C[n][0] - cs[j][0]) ** 2 +
                                    (C[n][1] - cs[j][1]) ** 2)
    return assign


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
            if nbhd.get(n) in PROTECTED:
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



def metrics(ids, geoms, adj):
    """Per-precinct area and perimeter, and the length each pair shares.

    With these three tables a district's true area and perimeter can be kept
    up to date one precinct at a time:

        area      = sum of member areas
        perimeter = sum of member perimeters - 2 x shared edges inside it

    which is what makes it affordable to optimise Polsby-Popper itself during
    the search instead of counting straddling pairs as a stand-in for it.
    Measured in metres on NAD83 Colorado Central, not degrees, or the
    ratio is distorted by latitude.
    """
    tf = Transformer.from_crs(4326, 26954, always_xy=True)
    flat = {n: transform(lambda x, y, z=None: tf.transform(x, y), geoms[n])
            for n in ids}
    area = {n: flat[n].area for n in ids}
    per = {n: flat[n].length for n in ids}
    # Shared edge from the union perimeter, not from intersecting the two
    # polygons: precincts carry ~31,000 m2 of 1-2 m simplification slivers, so
    # a shared line sometimes comes back as a sliver polygon and sometimes as a
    # line. per(a) + per(b) - per(a union b) = 2 x shared holds either way.
    shared = {n: {} for n in ids}
    for a in ids:
        for b in adj[a]:
            if b <= a:
                continue
            u = unary_union([flat[a], flat[b]])
            ln = max(0.0, (per[a] + per[b] - u.length) / 2.0)
            shared[a][b] = shared[b][a] = ln
    return area, per, shared


def pp_from(area, per):
    return 4 * math.pi * area / (per * per) if per > 0 else 0.0


def anneal(assign, ids, w, adj, k, nbhd, cap, area, per, shared,
           iters=80000, seed=0, nb_weight=NB_WEIGHT, a_comp=900.0,
           w_worst=W_WORST, cap_end=None, turn=None, w_turn=W_TURN):
    """Simulated annealing on population, compactness and neighbourhoods.

    Compactness here is the real Polsby-Popper of each district, kept current
    through the incremental area and perimeter tables, with the least compact
    district carrying its own weight. Optimising the average alone produces
    maps that score well and still contain one district nobody can defend.

    Population stays a hard constraint: the cap tightens on a schedule, and
    while the map sits above it only moves that reduce deviation are legal,
    which keeps the search from either deadlocking or never tightening.
    """
    # The search minimises exactly what score_plan maximises, scaled up so the
    # annealing temperature has sensible units. Keeping the two in step matters:
    # when they disagree the search optimises one thing and the selection picks
    # on another, and good maps get thrown away.
    a_split = a_comp * W_SPLIT * (nb_weight / NB_WEIGHT)
    a_dev = a_comp * W_DEV * 3.0     # imbal is roughly a third of max deviation
    a_turn = a_comp * w_turn
    rate, twt = turn if turn else ({}, {})

    rng = random.Random(seed)
    assign = dict(assign)
    members = [set() for _ in range(k)]
    for n, d in assign.items():
        members[d].add(n)
    sums = [sum(w[n] for n in members[i]) for i in range(k)]
    ideal = sum(w.values()) / k

    A = [sum(area[n] for n in members[i]) for i in range(k)]
    P = []
    for i in range(k):
        p = sum(per[n] for n in members[i])
        for n in members[i]:
            for nb, ln in shared[n].items():
                if nb in members[i] and nb > n:
                    p -= 2 * ln
        P.append(p)

    SW = [0.0] * k; SX = [0.0] * k; SX2 = [0.0] * k
    for n, d in assign.items():
        t_ = twt.get(n, 0.0)
        if t_:
            SW[d] += t_; SX[d] += t_ * rate[n]; SX2[d] += t_ * rate[n] * rate[n]

    def spread_of(sw, sx, sx2):
        tot = 0.0
        for i in range(k):
            if sw[i] <= 0:
                continue
            m = sx[i] / sw[i]
            tot += math.sqrt(max(0.0, sx2[i] / sw[i] - m * m))
        return tot / k

    tally = {}
    for n, d in assign.items():
        g = nbhd.get(n)
        if g:
            tally.setdefault(g, {})
            tally[g][d] = tally[g].get(d, 0) + 1

    def splits_now():
        return sum(sum(1 for v in c.values() if v) - 1 for c in tally.values())

    def comp_score(AA, PP_):
        vals = [pp_from(AA[i], PP_[i]) for i in range(k)]
        return (1 - w_worst) * (sum(vals) / k) + w_worst * min(vals)

    def imbal(ss):
        return math.sqrt(sum((s / ideal - 1) ** 2 for s in ss) / k)

    splits = splits_now()
    cost = (a_split * splits + a_dev * imbal(sums)
            - a_comp * comp_score(A, P)
            + a_turn * spread_of(SW, SX, SX2))
    cap_end = cap if cap_end is None else min(cap, cap_end)
    start_dev = (max(sums) - min(sums)) / ideal
    best = (cost if start_dev <= cap_end else float("inf"), dict(assign))

    border = [n for n in ids if any(assign[nb] != assign[n] for nb in adj[n])]
    # Temperature has to match the size of a single move's cost change, not
    # the size of the total cost. A typical one-precinct move shifts the
    # objective by a fraction of a unit; starting at 22 made every move look
    # free and the walk destroyed more structure than the cooling could rebuild.
    t0, t1 = 1.5, 0.01
    for step in range(iters):
        T = t0 * (t1 / t0) ** (step / iters)
        f = min(1.0, step / (0.5 * iters))
        cap_now = cap + (cap_end - cap) * f
        cur_dev = (max(sums) - min(sums)) / ideal
        if not border:
            break
        n = border[rng.randrange(len(border))]
        d = assign[n]
        if len(members[d]) <= 1:
            continue
        if nbhd.get(n) in PROTECTED:
            continue                     # protected neighbourhoods move whole or not at all
        opts = [assign[nb] for nb in adj[n] if assign[nb] != d]
        if not opts:
            continue
        t = opts[rng.randrange(len(opts))]

        nd, nt = sums[d] - w[n], sums[t] + w[n]
        trial = [nd if i == d else nt if i == t else sums[i] for i in range(k)]
        td = (max(trial) - min(trial)) / ideal
        if td > cap_now and td >= cur_dev:
            continue

        sd = sum(ln for nb, ln in shared[n].items() if assign.get(nb) == d)
        st = sum(ln for nb, ln in shared[n].items() if assign.get(nb) == t)
        nA = list(A); nP = list(P)
        nA[d] -= area[n]; nA[t] += area[n]
        nP[d] = P[d] - per[n] + 2 * sd
        nP[t] = P[t] + per[n] - 2 * st

        g = nbhd.get(n)
        dsplit = 0
        if g:
            c = tally[g]
            before = sum(1 for v in c.values() if v) - 1
            ad, at = c.get(d, 0) - 1, c.get(t, 0) + 1
            parts = sum(1 for kk, v in c.items() if kk not in (d, t) and v)
            parts += (1 if ad else 0) + (1 if at else 0)
            dsplit = (parts - 1) - before

        dturn = 0.0
        nSW = nSX = nSX2 = None
        if a_turn:
            t_ = twt.get(n, 0.0)
            nSW, nSX, nSX2 = list(SW), list(SX), list(SX2)
            if t_:
                x = rate[n]
                nSW[d] -= t_; nSX[d] -= t_ * x; nSX2[d] -= t_ * x * x
                nSW[t] += t_; nSX[t] += t_ * x; nSX2[t] += t_ * x * x
            dturn = spread_of(nSW, nSX, nSX2) - spread_of(SW, SX, SX2)

        dcost = (a_split * dsplit
                 + a_dev * (imbal(trial) - imbal(sums))
                 - a_comp * (comp_score(nA, nP) - comp_score(A, P))
                 + a_turn * dturn)
        if dcost > 0 and rng.random() >= math.exp(-dcost / T):
            continue
        if not connected(members[d] - {n}, adj):
            continue

        members[d].discard(n); members[t].add(n)
        sums[d], sums[t] = nd, nt
        A, P = nA, nP
        if nSW is not None:
            SW, SX, SX2 = nSW, nSX, nSX2
        assign[n] = t
        if g:
            c = tally[g]
            c[d] = c.get(d, 0) - 1
            if not c[d]:
                del c[d]
            c[t] = c.get(t, 0) + 1
        cost += dcost
        cur_dev = td
        for x in [n] + list(adj[n]):
            on = any(assign[nb] != assign[x] for nb in adj[x])
            inb = x in border
            if on and not inb:
                border.append(x)
            elif inb and not on:
                border.remove(x)
        if cur_dev <= cap_end + 1e-9 and cost < best[0] - 1e-9:
            best = (cost, dict(assign))
    return best[1]


def smooth(assign, ids, w, adj, k, nbhd, cap, area, per, shared,
           w_worst=W_WORST, rounds=4000):
    """Greedy cleanup on real compactness, after the annealing has finished.

    Annealing ends warm enough to leave single precincts poking into the wrong
    district. Those are what a reader points at. This takes the single best
    compactness-improving move available, over and over, and stops when none
    is left, refusing anything that breaks contiguity, pushes population past
    the cap, or splits another neighbourhood.
    """
    assign = dict(assign)
    members = [set() for _ in range(k)]
    for n, d in assign.items():
        members[d].add(n)
    sums = [sum(w[n] for n in members[i]) for i in range(k)]
    ideal = sum(w.values()) / k
    A = [sum(area[n] for n in members[i]) for i in range(k)]
    P = []
    for i in range(k):
        p = sum(per[n] for n in members[i])
        for n in members[i]:
            for nb, ln in shared[n].items():
                if nb in members[i] and nb > n:
                    p -= 2 * ln
        P.append(p)

    tally = {}
    for n, d in assign.items():
        g = nbhd.get(n)
        if g:
            tally.setdefault(g, {})
            tally[g][d] = tally[g].get(d, 0) + 1

    def blend(AA, PP_):
        v = [pp_from(AA[i], PP_[i]) for i in range(k)]
        return (1 - w_worst) * (sum(v) / k) + w_worst * min(v)

    for _ in range(rounds):
        base = blend(A, P)
        best = None
        for n in ids:
            d = assign[n]
            if len(members[d]) <= 1:
                continue
            if nbhd.get(n) in PROTECTED:
                continue
            if all(assign[nb] == d for nb in adj[n]):
                continue
            for t in {assign[nb] for nb in adj[n]} - {d}:
                nd, nt = sums[d] - w[n], sums[t] + w[n]
                trial = [nd if i == d else nt if i == t else sums[i]
                         for i in range(k)]
                if (max(trial) - min(trial)) / ideal > cap:
                    continue
                g = nbhd.get(n)
                if g:
                    c = tally[g]
                    before = sum(1 for v in c.values() if v) - 1
                    ad, at = c.get(d, 0) - 1, c.get(t, 0) + 1
                    parts = sum(1 for kk, v in c.items() if kk not in (d, t) and v)
                    parts += (1 if ad else 0) + (1 if at else 0)
                    if (parts - 1) > before:
                        continue
                sd = sum(ln for nb, ln in shared[n].items() if assign.get(nb) == d)
                st = sum(ln for nb, ln in shared[n].items() if assign.get(nb) == t)
                nA = list(A); nP = list(P)
                nA[d] -= area[n]; nA[t] += area[n]
                nP[d] = P[d] - per[n] + 2 * sd
                nP[t] = P[t] + per[n] - 2 * st
                gain = blend(nA, nP) - base
                if gain <= 1e-7:
                    continue
                if best and gain <= best[0]:
                    continue
                if not connected(members[d] - {n}, adj):
                    continue
                best = (gain, n, d, t, nA, nP)
        if not best:
            break
        _g, n, d, t, nA, nP = best
        gg = nbhd.get(n)
        if gg:
            c = tally[gg]
            c[d] = c.get(d, 0) - 1
            if not c[d]:
                del c[d]
            c[t] = c.get(t, 0) + 1
        members[d].discard(n); members[t].add(n)
        sums[d] -= w[n]; sums[t] += w[n]
        A, P = nA, nP
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



# What one split neighbourhood is worth in units of "precinct pairs straddling
# a district line". Scott's call: keep neighbourhoods whole where we can, and
# spend a point or two of deviation to do it, but never make it absolute.
# Nothing above this is worth showing: it burns the safe harbour for nothing.


def score_plan(a, w, geoms, nbhd, k, w_split=W_SPLIT, w_dev=W_DEV,
               w_worst=W_WORST, turn=None, w_turn=W_TURN):
    """One number for a whole map.

    The mean alone hides a bad district: a map can average 0.126 while one
    of its seven seats is a 0.056 tendril, and that one district is what
    someone points at when they say the map looks drawn. So the worst
    district carries real weight alongside the mean.
    """
    dev, sums = deviation(a, w, k)
    comp = compactness(a, geoms, k)
    splits, _ = nbhd_splits(a, nbhd)
    quality = ((1 - w_worst) * (sum(comp) / k) + w_worst * min(comp)
               - w_split * splits - w_dev * dev)
    spread = None
    if turn:
        spread, _per = turn_spread(a, k, *turn)
        quality -= w_turn * spread
    return quality, dev, sums, comp, splits, spread


def build(k, ids, w, adj, centres, geoms, nbhd, mets, nb_weight=NB_WEIGHT,
          cap=SHAPE_CAP, tries=TRIES, seed=11, turn=None, w_turn=W_TURN):
    """Roll a batch of maps, then stop rolling and start improving.

    With two cores, more random restarts buy less than refining the best map
    already in hand. Phase one explores. Phase two perturbs the leader and
    re-anneals cool, keeping anything better, which is basin hopping: it walks
    between nearby good maps instead of starting over from nothing each time.
    """
    rng = random.Random(seed + k)
    area, per, shared = mets
    C = projected_centres(ids, geoms)
    pool = []
    for attempt in range(tries):
        # Voronoi for most starts, grown maps for a few: the grower sometimes
        # finds a good map in a corner the Voronoi never reaches.
        if attempt % 4 == 3:
            a = grow(ids, w, adj, centres, k, rng)
            if len(a) != len(ids):
                continue
            a = polish(dict(a), ids, w, adj, k, nbhd=nbhd,
                       nb_weight=nb_weight, target=cap)
        else:
            a = voronoi_seed(ids, w, adj, C, area, k, rng)
            if len(set(a.values())) != k:
                continue
        a = unify_protected(a, ids, w, adj, nbhd)
        dev0, _ = deviation(a, w, k)
        a = anneal(a, ids, w, adj, k, nbhd, max(cap, dev0), *mets,
                   iters=ANNEAL_ITERS, seed=rng.randrange(1 << 30),
                   nb_weight=nb_weight, cap_end=cap,
                   turn=turn, w_turn=w_turn)
        a = smooth(a, ids, w, adj, k, nbhd, cap, *mets)
        members = [set() for _ in range(k)]
        for n, d in a.items():
            members[d].add(n)
        if any(not connected(m, adj) or not m for m in members):
            continue
        q, dev, sums, comp, splits, spr = score_plan(a, w, geoms, nbhd, k,
                                                    turn=turn, w_turn=w_turn)
        if dev > HARD_DEV or not protected_ok(a, ids, nbhd):
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
        a = anneal(dict(src_a), ids, w, adj, k, nbhd, max(cap, dev0), *mets,
                   iters=HOP_ITERS, seed=rng.randrange(1 << 30),
                   nb_weight=nb_weight, cap_end=cap,
                   turn=turn, w_turn=w_turn)
        a = smooth(a, ids, w, adj, k, nbhd, cap, *mets)
        members = [set() for _ in range(k)]
        for n, d in a.items():
            members[d].add(n)
        if any(not connected(m, adj) or not m for m in members):
            continue
        q, dev, _s, _c, _sp, _sr = score_plan(a, w, geoms, nbhd, k,
                                              turn=turn, w_turn=w_turn)
        if dev <= HARD_DEV and protected_ok(a, ids, nbhd) and q > best_q:
            best_q, best_a = q, a
            leaders[0] = a

    q, dev, sums, comp, splits, spr = score_plan(best_a, w, geoms, nbhd, k,
                                                turn=turn, w_turn=w_turn)
    return (q, best_a, dev, sums, comp, splits, spr)


TURN = None


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
    spread = turn_spread(a, k, *TURN)[0] if TURN else None
    return {"label": label, "turnSpread": (round(spread, 2) if spread else None), "districts": k, "maxDeviation": round(100 * dev, 2),
            "meanCompactness": round(sum(comp) / k, 3),
            "worstCompactness": round(min(comp), 3), "splits": splits,
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
    print("  neighbourhoods represented: %d   protected: %s"
          % (len({g for g in nbhd.values() if g}), ", ".join(PROTECTED)))

    centres = {n: (geoms[n].representative_point().x,
                   geoms[n].representative_point().y) for n in ids}
    adj = adjacency(ids, geoms)
    print("  adjacency: %d precinct pairs touch"
          % (sum(len(v) for v in adj.values()) // 2))
    mets = metrics(ids, geoms, adj)
    global TURN
    TURN = turnout(ids)
    if TURN:
        print("  turnout: %d precinct return rates from the %d election"
              % (sum(1 for n in ids if TURN[1].get(n)), TURNOUT_YEAR))

    bases = []
    for f, lab in (("school_board", "DPS today"), ("council", "City Council"),
                   ("house", "State House"), ("senate", "State Senate")):
        b = baseline(f, props, ids, w, geoms, adj, nbhd, lab)
        if b:
            bases.append(b)
            print("\n  %s (%d districts, existing map): deviation %.2f%%   "
                  "compactness %.3f (worst %.3f)   split %d   turnout sd %s"
                  % (lab, b["districts"], b["maxDeviation"],
                     b["meanCompactness"], b["worstCompactness"], b["splits"],
                     b["turnSpread"]))

    prior = {}
    if os.path.exists(OUT):
        try:
            for p in json.load(open(OUT)).get("plans", []):
                prior[p["key"]] = p
        except Exception:
            pass

    plans = []
    for k in SIZES:
        got = build(k, ids, w, adj, centres, geoms, nbhd, mets, turn=TURN)
        if not got:
            print("  %d districts: no contiguous plan found" % k)
            continue
        _q, a, dev, sums, comp, splits, spr = got
        old = prior.get("k%d" % k)
        if old and "quality" in old:
            oa = {n: old["assign"][n] - 1 for n in ids if n in old["assign"]}
            if len(oa) == len(ids):
                oq, odev, osums, ocomp, osplits, ospr = score_plan(
                    oa, w, geoms, nbhd, k, turn=TURN)
                if oq > _q:
                    print("  %d districts: keeping the better map from an "
                          "earlier run (quality %.4f vs %.4f)" % (k, oq, _q))
                    a, dev, sums, comp, splits, spr, _q = (
                        oa, odev, osums, ocomp, osplits, ospr, oq)
        ideal = sum(w.values()) / k
        show("%d districts" % k, k, dev, sums, comp, splits, ideal)
        if spr:
            print("     within-district turnout spread %.2fpp" % spr)
        plans.append({
            "key": "k%d" % k,
            "districts": k,
            "label": "%d districts" % k,
            "basis": field,
            "maxDeviation": round(100 * dev, 2),
            "meanCompactness": round(sum(comp) / k, 3),
            "worstCompactness": round(min(comp), 3),
            "quality": round(_q, 5),
            "turnSpread": (round(spr, 2) if spr else None),
            "compactness": [round(c, 3) for c in comp],
            "splits": splits,
            "splitNames": nbhd_splits(a, nbhd)[1],
            "totals": [int(s) for s in sums],
            "assign": {n: a[n] + 1 for n in ids},
        })

    if field != POP_FIELD:
        print("\nnothing written: census population missing")
        return
    for key, p in prior.items():
        if not any(q["key"] == key for q in plans):
            plans.append(p)          # a run for one size must not drop the others
    plans.sort(key=lambda p: p["districts"])
    with open(OUT, "w") as fh:
        json.dump({"basis": field, "total": int(sum(w.values())),
                   "plans": plans, "baselines": bases,
                   "adjacency": {n: sorted(adj[n]) for n in ids}},
                  fh, separators=(",", ":"))
    print("\nwrote %s (%d KB)" % (OUT, os.path.getsize(OUT) // 1024))


if __name__ == "__main__":
    main()
