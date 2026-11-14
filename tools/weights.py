#!/usr/bin/env python3
"""Tune the default win / pick / ban weights of the meta index against the developers' balance decisions
(meta/weights.json).

Usage:
    python3 tools/weights.py [--archive archive] [--patches meta/patches] [--base meta/judgment-base.json]
                             [--out meta/weights.json] [--report] [--reset]

Criterion ("agreement with the developers"): just before a balance patch, the heroes it nerfs should score above
the heroes it buffs. For every patch, server and role, the composite score of the meta index is standardized
within the role (mean 0, sd 1) on the stats of the day before the patch; each clearly nerfed hero adds its score,
each clearly buffed hero subtracts it, and the total is divided by the number of such heroes. Clear means every
change line of the hero points the same way, with no new or removed mechanics and no rework (tools/patches.py).

The developers balance with far more data than the public numbers (skill brackets, one-trick play, frustration),
so a weighting that ranks the heroes the way their decisions do is taken as the better measure of strength. Known
limits: they also read the public stats (partly circular), and they nerf annoying heroes, which favours ban rate.

Data: meta/judgment-base.json holds every patch since per-hero ban rates were published (Feb 2026 in China, May
2026 for Blizzard's regions) up to its "until" date; later patches come from this site's own daily archive
(archive/<server>/all.json, the day before the patch). A patch is used once PATCH_WAIT days have passed (so its
hotfixes are in), and only if it has at least MIN_HEROES clear changes.

Search: every combination in 1% steps with each weight between LO and HI. The result is the per-weight median of
the best combination and of the best ones with one patch left out at a time. It moves each weight by at most
MAX_MOVE %p per evaluation and is recomputed only when a new patch becomes usable (--reset jumps straight to it).
Back-test on the base data (Feb-Nov 2026, 11 patches, 357 hero changes): best about 35 / 31 / 34; 50 / 30 / 20
scores 0.607 against 0.634, win rate alone 0.401."""
import argparse
import json
import math
import os
import statistics as st
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERS = [("kr", "Korea"), ("asia", "Asia"), ("americas", "Americas"), ("europe", "Europe")]
ROLES = ["tank", "damage", "support"]
PICKSUM = {"tank": 100.0, "damage": 200.0, "support": 200.0}
DEFAULT = {"wr": 50, "pr": 30, "br": 20}
STEP, LO, HI = 1, 10, 70
MAX_MOVE = 10       # one evaluation moves each weight by at most this many %p
PATCH_WAIT = 9      # days after a patch before it is used (its hotfixes come within this)
MIN_HEROES = 3      # clear buffs / nerfs a patch needs


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def labels_for(patches, start, days=PATCH_WAIT):
    """Clear buffs (+1) / nerfs (-1) of the patches in [start, start + days]; a hero with any unclear change or
    changes both ways in that span is left out."""
    lab = {}
    for p in patches:
        d = date.fromisoformat(p["date"])
        if not (start <= d <= start + timedelta(days=days)):
            continue
        for h in p.get("heroes", []):
            if not h.get("id"):
                continue
            v = 1 if (h.get("clear") and h["verdict"] == "buff") else -1 if (h.get("clear") and h["verdict"] == "nerf") else 0
            if v == 0 or (h["id"] in lab and lab[h["id"]] != v):
                lab[h["id"]] = 0
            elif h["id"] not in lab:
                lab[h["id"]] = v
    return {k: v for k, v in lab.items() if v}


def archive_events(arc_dir, patches, after, today):
    """Patches after `after` from this site's archive: the four servers' all-ranks stats on the day before."""
    out = []
    starts = sorted({p["date"] for p in patches if p["date"] > after})
    used = set()
    for pd in starts:
        P = date.fromisoformat(pd)
        if (today - P).days < PATCH_WAIT or pd in used:
            continue
        lab = labels_for(patches, P)
        if len(lab) < MIN_HEROES:
            continue
        # patches inside this one's window belong to it
        used.update(x for x in starts if P <= date.fromisoformat(x) <= P + timedelta(days=PATCH_WAIT))
        for s, name in SERVERS:
            a = load(os.path.join(arc_dir, s, "all.json"))
            meta = load(os.path.join(arc_dir, s, "meta.json"), {}) or {}
            days = [x for x in (a or {}).get("days", []) if x["d"] < pd]
            if not days:
                continue
            e = days[-1]
            roles = {h: v.get("role") for h, v in (meta.get("heroes") or {}).items()}
            rows = {h: [v[1], v[2], v[3]] for h, v in e["h"].items() if "~" not in h and h in roles}
            for role in ROLES:
                hs = [h for h in rows if roles.get(h) == role]
                if len(hs) < 6 or not any(h in lab for h in hs):
                    continue
                t = sum(rows[h][0] for h in hs) or 1
                out.append({"patch": pd, "source": name, "role": role,
                            "heroes": {h: [rows[h][0] / t * PICKSUM[role], rows[h][1], rows[h][2]] for h in hs},
                            "labels": {h: lab[h] for h in hs if h in lab}})
    return out


def unit_stats(ev):
    """Per (patch, server, role): a = sum over labelled heroes of -label x (z - mean z), S = covariance of z.
    The standardized composite score of weights w then gives sum(-label x score) = (w . a) / sqrt(w S w)."""
    hs = list(ev["heroes"])
    Z = []
    for h in hs:
        p, w, b = ev["heroes"][h]
        w = 50 + (w - 50) / (1 - min(max(p, 0) / 100, 0.9))          # without mirror matches, as the index
        Z.append([w - 50, math.log(max(0.1, p) / max(0.3, 1 - b / 100)), math.log1p(max(0.0, b))])
    n = len(Z)
    wsd = max(math.sqrt(sum(z[0] ** 2 for z in Z) / n), 0.05)
    mu = [st.mean(z[k] for z in Z) for k in (1, 2)]
    sd = [max(st.pstdev([z[k] for z in Z]), 0.01) for k in (1, 2)]
    Z = [[z[0] / wsd, (z[1] - mu[0]) / sd[0], (z[2] - mu[1]) / sd[1]] for z in Z]
    m = [st.mean(z[k] for z in Z) for k in range(3)]
    S = [[st.mean((z[i] - m[i]) * (z[j] - m[j]) for z in Z) for j in range(3)] for i in range(3)]
    a = [0.0, 0.0, 0.0]
    for h, z in zip(hs, Z):
        L = ev["labels"].get(h, 0)
        for k in range(3):
            a[k] += -L * (z[k] - m[k])
    return {"patch": ev["patch"], "a": a, "S": S, "n": len(ev["labels"])}


def grid():
    return [(wr, pr, 100 - wr - pr) for wr in range(LO, HI + 1, STEP) for pr in range(LO, HI + 1, STEP)
            if LO <= 100 - wr - pr <= HI]


def score(units, g):
    w = [x / 100 for x in g]
    tot, cnt = 0.0, 0
    for u in units:
        S = u["S"]
        var = sum(w[i] * S[i][j] * w[j] for i in range(3) for j in range(3))
        if var > 1e-12:
            tot += sum(w[k] * u["a"][k] for k in range(3)) / math.sqrt(var)
        cnt += u["n"]
    return tot / max(cnt, 1)


def best(units, G, prefer):
    return max(G, key=lambda g: (round(score(units, g), 6), -sum(abs(a - b) for a, b in zip(g, prefer))))


def snap(ws):
    """Per-weight values back onto whole percent that add up to 100 (largest remainders get the rounding)."""
    t = sum(ws) or 1
    raw = [x * 100 / t / STEP for x in ws]
    base = [math.floor(x) for x in raw]
    for i in sorted(range(3), key=lambda i: raw[i] - base[i], reverse=True)[:int(round(100 / STEP - sum(base)))]:
        base[i] += 1
    return [b * STEP for b in base]


def step_towards(cur, target):
    """Move from the current weights towards the target by at most MAX_MOVE %p per weight, keeping the sum at 100."""
    new = [c + max(-MAX_MOVE, min(MAX_MOVE, t - c)) for c, t in zip(cur, target)]
    while sum(new) != 100:
        d = STEP if sum(new) < 100 else -STEP
        ok = [k for k in range(3) if abs(new[k] + d - cur[k]) <= MAX_MOVE and LO <= new[k] + d <= HI] or [0, 1, 2]
        i = max(ok, key=lambda k: (target[k] - new[k]) * d)
        new[i] += d
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=os.path.join(ROOT, "archive"))
    ap.add_argument("--patches", default=os.path.join(ROOT, "meta", "patches"))
    ap.add_argument("--base", default=os.path.join(ROOT, "meta", "judgment-base.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "meta", "weights.json"))
    ap.add_argument("--report", action="store_true", help="recompute even when no new patch is usable")
    ap.add_argument("--reset", action="store_true", help="adopt the result without the per-evaluation move limit")
    a = ap.parse_args()
    old = load(a.out, {}) or {}
    cur = old.get("weights") or DEFAULT
    base = load(a.base, {}) or {}
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from patches import load_patches
    patches = load_patches(a.patches)
    events = list(base.get("events", []))
    newer = [p for p in patches if p["date"] > base.get("until", "")]
    events += archive_events(a.archive, newer, base.get("until", ""), date.today())
    units = [unit_stats(e) for e in events if e.get("labels")]
    ids = sorted({u["patch"] for u in units})
    nlab = sum(u["n"] for u in units)
    print(f"weights: {len(ids)} patches, {nlab} labelled hero changes", file=sys.stderr)
    if ids == old.get("patchesUsed") and old.get("criterion") == "developers" and not (a.report or a.reset):
        print("weights: no new patch to learn from, unchanged", file=sys.stderr)
        return
    if not units:
        print("weights: no usable patches", file=sys.stderr)
        return
    G = grid()
    prefer = (cur["wr"], cur["pr"], cur["br"])
    picks = [best(units, G, prefer)]
    if len(ids) >= 3:
        for pid in ids:
            picks.append(best([u for u in units if u["patch"] != pid], G, prefer))
    target = snap([st.median(p[k] for p in picks) for k in range(3)])
    spread = max(max(p[k] for p in picks) - min(p[k] for p in picks) for k in range(3))
    new = target if a.reset else step_towards([cur["wr"], cur["pr"], cur["br"]], target)
    new = dict(zip(("wr", "pr", "br"), new))
    out = {"weights": new, "status": "tuned", "criterion": "developers", "patchesUsed": ids, "labels": nlab,
           "target": dict(zip(("wr", "pr", "br"), target)), "leaveOneOutSpread": spread,
           "score": round(score(units, tuple(new.values())), 3),
           "scoreTarget": round(score(units, tuple(target)), 3),
           "scoreOldDefault": round(score(units, (50, 30, 20)), 3),
           "history": old.get("history", []), "evaluatedAt": datetime.now().astimezone().isoformat(timespec="minutes")}
    if new != cur:
        out["history"].append({"at": out["evaluatedAt"], "patch": ids[-1], "weights": new})
        print(f"weights: default changed to {new}", file=sys.stderr)
    print(f"weights: target {target} (leave-one-out spread {spread}%p), score {out['scoreTarget']} "
          f"vs 50/30/20 {out['scoreOldDefault']}", file=sys.stderr)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(a.out + ".tmp", a.out)


if __name__ == "__main__":
    main()
