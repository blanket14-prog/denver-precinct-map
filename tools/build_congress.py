"""Colorado's U.S. Representatives -> the "congress" block of data/legislators.json,
plus their official portraits in static/congress/.

Source: the unitedstates/congress-legislators dataset, which tracks every
sitting member of Congress with party, district and term dates:

    curl -O https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-current.yaml

Portraits are the official House photographs, which are works of the federal
government and in the public domain, so unlike the state legislature's
portraits they are kept here rather than hotlinked. They come from the
unitedstates/images project, resized to 150 px wide.

"In this seat since" is the first day of the member's unbroken run in the
same district number. Lauren Boebert therefore reads January 2025: she
represented the 3rd district before moving to the 4th.

Run:  python3 tools/build_congress.py legislators-current.yaml
"""
import io, json, os, sys, urllib.request
import yaml
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "legislators.json")
PHOTOS = os.path.join(ROOT, "static", "congress")
IMG = "https://raw.githubusercontent.com/unitedstates/images/gh-pages/congress/225x275/%s.jpg"
PARTY = {"Democrat": "D", "Republican": "R", "Independent": "I", "Libertarian": "L"}


def main(src):
    people = yaml.safe_load(open(src))
    members, since, photos = {}, {}, {}
    os.makedirs(PHOTOS, exist_ok=True)
    for p in people:
        t = p["terms"][-1]
        if t.get("state") != "CO" or t["type"] != "rep":
            continue
        d = str(t["district"])
        start = t["start"]
        for prev in reversed(p["terms"][:-1]):
            if prev.get("state") == "CO" and prev["type"] == "rep" and prev.get("district") == t.get("district"):
                start = prev["start"]
            else:
                break
        name = p["name"].get("official_full") or "%s %s" % (p["name"]["first"], p["name"]["last"])
        members[d] = [name, PARTY.get(t["party"], t["party"][:1])]
        since[d] = start
        bio = p["id"]["bioguide"]
        out = os.path.join(PHOTOS, bio + ".jpg")
        if not os.path.exists(out):
            raw = urllib.request.urlopen(IMG % bio, timeout=30).read()
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im = im.resize((150, round(150 * im.height / im.width)), Image.LANCZOS)
            im.save(out, "JPEG", quality=84, optimize=True)
        photos[d] = "/static/congress/%s.jpg" % bio

    got = sorted(members, key=int)
    assert got == [str(i) for i in range(1, 9)], got

    doc = json.load(open(PATH))
    doc["congress"] = members
    doc.setdefault("since", {})["congress"] = since
    doc.setdefault("photos", {})["congress"] = photos
    doc["congress_source"] = ("U.S. House members via the unitedstates/congress-legislators "
                              "dataset; official House portraits (public domain)")
    with open(PATH, "w") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    for d in got:
        print("CD%s  %-20s %s  since %s" % (d, members[d][0], members[d][1], since[d]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "legislators-current.yaml")
