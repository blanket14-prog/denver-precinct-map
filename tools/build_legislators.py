"""Attach portrait URLs to data/legislators.json.

The roster of record is data/legislators.json itself, which was taken from the
Colorado General Assembly directory by hand. This script does not rewrite the
names; it adds a photo URL for each seat and reports anywhere a second source
disagrees, so a bad name or party cannot slip in unnoticed.

The photos come from the Open States people dataset, which records the portrait
each chamber publishes:

    git clone --depth 1 --filter=blob:none --sparse \\
        https://github.com/openstates/people.git
    cd people && git sparse-checkout set data/co

The URLs point at leg.colorado.gov, and the map hotlinks them rather than
keeping copies. That is deliberate. They are permanent Rails ActiveStorage
blob links -- the signed payload carries "exp":null -- that redirect to a
short-lived S3 URL generated per request, so the browser always gets a current
one. Mirroring 100 official portraits onto a hobby Render service would mean
re-hosting someone else's images, going stale the day a member is sworn in,
and adding about 2 MB to a free-tier repo, for no gain.

Not every seat has a portrait. A member appointed to a vacancy is often seated
for months before the chamber photographs them; those seats are left without a
URL and the map draws initials instead.

Run:  python3 tools/build_legislators.py [path to the openstates clone]
"""
import datetime, glob, json, os, sys

import yaml

CLONE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-0/people"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "legislators.json")
TODAY = datetime.date.today()


def as_date(v):
    if not v:
        return None
    return v if isinstance(v, datetime.date) else datetime.date.fromisoformat(v)


def current_role(person):
    """The seat this person holds today, not every seat they have held."""
    for r in person.get("roles", []):
        if r.get("type") not in ("upper", "lower"):
            continue
        start, end = as_date(r.get("start_date")), as_date(r.get("end_date"))
        if start and start > TODAY:
            continue
        if end and end < TODAY:
            continue
        return r
    return None


def read_people(clone):
    out = {}
    pattern = os.path.join(clone, "data", "co", "legislature", "*.yml")
    files = sorted(glob.glob(pattern))
    if not files:
        sys.exit("no legislator files under %s -- is the clone sparse-checked out?"
                 % os.path.join(clone, "data", "co", "legislature"))
    for f in files:
        p = yaml.safe_load(open(f))
        role = current_role(p)
        if not role:
            continue
        chamber = "house" if role["type"] == "lower" else "senate"
        party = (p.get("party") or [{}])[0].get("name", "")
        out[(chamber, str(int(role["district"])))] = {
            "name": p["name"],
            "party": party[:1] or "?",
            "image": p.get("image") or "",
        }
    return out


def main():
    doc = json.load(open(PATH))
    people = read_people(CLONE)

    photos = {"house": {}, "senate": {}}
    missing, disagree, unmatched = [], [], []

    for chamber in ("house", "senate"):
        for district, row in sorted(doc[chamber].items(), key=lambda kv: int(kv[0])):
            other = people.get((chamber, district))
            if not other:
                unmatched.append("%s %s (%s)" % (chamber, district, row[0]))
                continue
            # A nickname is not a disagreement; a different surname or party is.
            same_person = (other["name"].split()[-1].lower()
                           == row[0].split()[-1].lower())
            if not same_person or other["party"] != row[1]:
                disagree.append("%s %s: roster has %s (%s), Open States has %s (%s)"
                                % (chamber, district, row[0], row[1],
                                   other["name"], other["party"]))
            if other["image"]:
                photos[chamber][district] = other["image"]
            else:
                missing.append("%s %s %s" % (chamber, district, row[0]))

    doc["photos"] = photos
    doc["photo_source"] = ("Portraits published by the Colorado General Assembly, "
                           "linked from leg.colorado.gov; URLs via the Open States "
                           "people dataset")
    with open(PATH, "w") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=False)

    have = len(photos["house"]) + len(photos["senate"])
    print("portraits: %d of %d seats" % (have, len(doc["house"]) + len(doc["senate"])))
    if missing:
        print("  no portrait published yet (%d):" % len(missing))
        for m in missing:
            print("    " + m)
    if unmatched:
        print("  not in Open States (%d):" % len(unmatched))
        for u in unmatched:
            print("    " + u)
    if disagree:
        print("  DISAGREEMENT -- check before shipping (%d):" % len(disagree))
        for d in disagree:
            print("    " + d)
    else:
        print("  every matched seat agrees on surname and party")


if __name__ == "__main__":
    main()
