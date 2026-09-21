"""Influence per resident, for the top and bottom turnout fifths of Denver.

The question the number answers: of the ballots cast in whatever contest elects
a person's board member, how many come from where that person lives, compared
with how many would come from there if every resident of that contest voted at
the same rate?

    influence(G) = ballots actually cast in G
                   -------------------------------------------------
                   sum over precincts p in G of  pop(p) x r(district of p)

where r(d) is district d's own ballots per resident. 1.00 means the group casts
exactly its proportional share of its own districts' ballots. Below 1.00 means
its residents are outvoted inside their own districts. For the at-large row the
district is the whole city, so the measure collapses to the group's ballots per
resident over the city's.

The group is a fifth of DENVER'S RESIDENTS, not a fifth of its precincts:
precincts are sorted by 2025 return rate (ballots cast / ballots issued) and
taken in order until they hold 20% of the 2020 population. Sorting on the rate
and sizing on population is what makes it "the fifth of Denver that lives in the
lowest-turnout precincts", which is what the page claims it is.

Run:  python3 tools/build_influence.py
"""
import json, collections, os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def load(p): return json.load(open(os.path.join(HERE, 'data', p)))

YEAR = 2025
FRACTION = 0.20

pop   = load('pop2020.json')['pop']
rets  = load('returns.json')['elections']
plans = load('plans.json')

e    = [x for x in rets if x['year'] == YEAR][0]
CAST = {n: float(c) for n, (c, i) in e['precincts'].items()}
ISS  = {n: float(i) for n, (c, i) in e['precincts'].items()}

MAPS = {'_atlarge': {n: '0' for n in pop}}
for p in plans['plans']:
    MAPS[p['key']] = {k: str(v) for k, v in p['assign'].items()}
for b in plans['baselines']:
    key = {'DPS today': 'dps5', 'City Council': 'cc11',
           'State House': 'house', 'State Senate': 'senate'}[b['label']]
    MAPS[key] = {k: str(v) for k, v in b['assign'].items()}

ids = [n for n in pop if n in CAST and ISS.get(n, 0) > 0]
rate = {n: 100.0 * CAST[n] / ISS[n] for n in ids}

def fifth(high):
    order = sorted(ids, key=lambda n: rate[n], reverse=high)
    target = FRACTION * sum(pop[n] for n in ids)
    out, run = [], 0
    for n in order:
        out.append(n); run += pop[n]
        if run >= target:
            break
    return out, run

def influence(assign, group):
    dpop, dcast = collections.Counter(), collections.Counter()
    for n, d in assign.items():
        dpop[d] += pop[n]; dcast[d] += CAST.get(n, 0.0)
    num = sum(CAST.get(n, 0.0) for n in group)
    den = sum(pop[n] * (dcast[assign[n]] / dpop[assign[n]]) for n in group)
    return num / den

ROWS = [('_atlarge', 'At-large (today)'), ('senate', 'State Senate (5)'),
        ('dps5', 'DPS today (5)'), ('k7', 'Draft (7)'),
        ('house', 'State House (9)'), ('cc11', 'City Council (11)'),
        ('k9', 'Draft (9)')]

result = {}
for high, name in ((False, 'loInf'), (True, 'hiInf')):
    grp, res = fifth(high)
    lo = min(rate[n] for n in grp); hi = max(rate[n] for n in grp)
    print("\n%s: %d precincts, %s residents (%.1f%% of the county), "
          "return rates %.1f%%-%.1f%%"
          % ('lowest fifth' if not high else 'highest fifth', len(grp),
             format(res, ','), 100.0 * res / sum(pop[n] for n in ids), lo, hi))
    for key, label in ROWS:
        v = influence(MAPS[key], grp)
        result.setdefault(key, {})[name] = round(v, 2)
        print("   %-20s %.4f  ->  %.2f" % (label, v, round(v, 2)))

print("\nvalues for the page:")
for key, _ in ROWS:
    print('  %-10s loInf %.2f   hiInf %.2f' % (key, result[key]['loInf'], result[key]['hiInf']))
json.dump(result, open(os.path.join(HERE, 'data', 'influence.json'), 'w'), indent=1)
print("\nwrote data/influence.json")
