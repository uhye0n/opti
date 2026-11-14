#!/usr/bin/env python3
"""Tune the default win / pick / ban weights of the meta index from balance patches (meta/weights.json).

Usage:
    python3 tools/weights.py [--archive archive] [--patches meta/patches.json] [--out meta/weights.json] [--report]

Every combination of weights (5% steps, each between 10% and 70%) gets two scores:

  1. Patch response: does the index move the way the developers moved the hero? For every usable patch and server,
     the change of each hero's composite score (all ranks, all maps) from just before the patch to the end of the
     measuring window, centred within the role, is multiplied by +1 for a clear buff and -1 for a clear nerf; the
     mean over those heroes is divided by the spread of the changes of all heroes in the role (signal over noise).
  2. Agreement between servers: the same heroes should rank alike on Korea, Asia, Americas and Europe. Mean
     correlation of the composite scores between servers, per role, at the end of each patch's window.

Patch response alone favours win rate (it moves first after a patch, and it cannot see heroes whose win rate is
inflated by one-tricks, because that does not change with a patch); agreement favours pick and ban rate. The
weights with the best worse-of-the-two score (each score scaled to 0..1 over the grid) are taken, the choice is
repeated leaving out one patch at a time, and the per-weight median of those choices, back on 5% steps, is the
result. It replaces the default only once MIN_PATCHES patches are usable, moves each weight by at most MAX_MOVE %p at a
time, and is recomputed only when a new patch becomes usable, so the weights do not drift from day to day.

Measuring window, fixed in advance: before = the last daily entry before the patch day; after = the entry PATCH_END
days after it. When the official numbers were reset in between (season start), the after entry already counts only
post-patch games; otherwise the after rates are taken from the growth of the cumulative counts between the patch day
and the end of the window. Only heroes with a clear buff or nerf (every change line pointing the same way, no new or
removed mechanics, no reworks) count; a patch needs at least MIN_HEROES of them."""
import argparse
import json
import math
import os
import statistics as st
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERS = ["kr", "asia", "americas", "europe"]   # China publishes shares only: no match counts to difference
ROLES = ["tank", "damage", "support"]
DEFAULT = {"wr": 50, "pr": 30, "br": 20}
STEP, LO, HI = 5, 10, 70
PATCH_END = 10       # days after the patch day: end of the measuring window
SETTLE = 3           # days after the patch day: the window's start when the counts were not reset
MIN_HEROES = 3       # clear buffs / nerfs a patch needs to be usable
MIN_PATCHES = 4      # usable patches before the default is replaced
MAX_MOVE = 10        # one evaluation moves each weight by at most this many %p


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def d_of(s):
    return date.fromisoformat(s[:10])


def entry_at(days, day, before=False, slack=1):
    """The archive entry of `day`; failing that the nearest one within `slack` days (only earlier ones if before)."""
    best = None
    for e in days:
        dd = (d_of(e["d"]) - day).days
        if before:
            if dd < 0 and (best is None or dd > best[0]):
                best = (dd, e)
        elif abs(dd) <= slack and (best is None or abs(dd) < abs(best[0])):
            best = (dd, e)
    if before and best and best[0] < -slack - 2:
        return None
    return best[1] if best else None


def rates(e):
    return {h: v[1:4] for h, v in e["h"].items() if "~" not in h}     # [pick, win, ban]


def diff_rates(a, b):
    """Rates of the games between two cumulative entries of one counting period."""
    na, nb = a.get("n") or 0, b.get("n") or 0
    if nb <= na * 1.15 or nb - na < 2000:
        return None                     # too little growth to read (or the source did not update)
    out, dn = {}, nb - na
    for h, vb in rates(b).items():
        va = rates(a).get(h)
        if not va:
            continue
        pa, pb = va[0] * na, vb[0] * nb
        dp = pb - pa
        if dp <= 0:
            continue
        wins = (vb[1] * pb - va[1] * pa) / dp
        ban = (vb[2] * nb - va[2] * na) / dn
        if not (20 <= wins <= 80):
            continue
        out[h] = [dp / dn, wins, max(0.0, ban)]
    return out


def consts(triples):
    w = [t[1] - 50 for t in triples]
    p = [math.log(max(0.1, t[0]) / max(0.3, 1 - t[2] / 100)) for t in triples]
    b = [math.log(1 + max(0.0, t[2])) for t in triples]
    return {"wr_sd": max(math.sqrt(sum(x * x for x in w) / len(w)), 0.05),
            "pr_mu": st.mean(p), "pr_sd": max(st.pstdev(p), 0.01),
            "br_mu": st.mean(b), "br_sd": max(st.pstdev(b), 0.01)}


def zs(t, C):
    """Standardized (win, pick, ban) scores, exactly as the index builds them."""
    prav = max(0.1, t[0]) / max(0.3, 1 - t[2] / 100)
    return ((t[1] - 50) / C["wr_sd"], (math.log(prav) - C["pr_mu"]) / C["pr_sd"],
            (math.log(1 + max(0.0, t[2])) - C["br_mu"]) / C["br_sd"])


def grid():
    out = []
    for wr in range(LO, HI + 1, STEP):
        for pr in range(LO, HI + 1, STEP):
            br = 100 - wr - pr
            if LO <= br <= HI:
                out.append((wr, pr, br))
    return out


def corr(x, y):
    if len(x) < 4:
        return None
    mx, my = st.mean(x), st.mean(y)
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) if sx > 0 and sy > 0 else None


def build_units(arc_dir, patches):
    """Per usable patch: the response units (server x role: z-score changes and labels) and the after snapshots."""
    archives, metas = {}, {}
    for s in SERVERS:
        a = load(os.path.join(arc_dir, s, "all.json"))
        if a and a.get("days"):
            archives[s] = a["days"]
            metas[s] = load(os.path.join(arc_dir, s, "meta.json"), {}) or {}
    today = max((d_of(d[-1]["d"]) for d in archives.values()), default=date.today())
    usable = []
    for p in sorted(patches, key=lambda x: x["date"]):
        labels = {h["id"]: (1 if h["verdict"] == "buff" else -1) for h in p.get("heroes", [])
                  if h.get("clear") and h.get("id") and h["verdict"] in ("buff", "nerf")}
        if len(labels) < MIN_HEROES:
            continue
        P = d_of(p["date"])
        if (today - P).days < PATCH_END:
            continue                                  # the measuring window has not closed yet
        units, after = [], {}
        for s, days in archives.items():
            pre = entry_at(days, P, before=True)
            end = entry_at(days, P + timedelta(days=PATCH_END))
            if not pre or not end:
                continue
            resets = [d_of(x) for x in metas[s].get("patches", [])]
            if any(P - timedelta(days=1) <= r <= P + timedelta(days=SETTLE) for r in resets):
                post = rates(end)                     # the counts restarted at the patch: all post-patch games
            elif any(d_of(pre["d"]) < r <= d_of(end["d"]) for r in resets):
                continue                              # reset somewhere else inside the window: not comparable
            else:
                start = entry_at(days, P + timedelta(days=SETTLE))
                post = diff_rates(start, end) if start else None
            if not post:
                continue
            before = rates(pre)
            roles = {h: v.get("role") for h, v in (metas[s].get("heroes") or {}).items()}
            after[s] = (rates(end), roles)
            for role in ROLES:
                hs = [h for h in before if h in post and roles.get(h) == role]
                if len(hs) < 6 or not any(h in labels for h in hs):
                    continue
                C = consts([before[h] for h in hs])
                dz = {h: [b - a for a, b in zip(zs(before[h], C), zs(post[h], C))] for h in hs}
                units.append({"server": s, "role": role, "dz": dz,
                              "labels": {h: labels[h] for h in hs if h in labels}})
        if units:
            usable.append({"id": p["id"], "date": p["date"], "units": units, "after": after,
                           "heroes": len(labels)})
    return usable


def response(units, w):
    vals = []
    for u in units:
        d = {h: w[0] * z[0] + w[1] * z[1] + w[2] * z[2] for h, z in u["dz"].items()}
        mu = st.mean(d.values())
        sd = st.pstdev(d.values()) or 1.0
        vals.append(st.mean(lab * (d[h] - mu) for h, lab in u["labels"].items()) / sd)
    return st.mean(vals) if vals else 0.0


def agreement(snaps, w):
    vals = []
    for after in snaps:
        comp = {}
        for s, (r, roles) in after.items():
            for role in ROLES:
                hs = [h for h in r if roles.get(h) == role]
                if len(hs) < 6:
                    continue
                C = consts([r[h] for h in hs])
                comp[(s, role)] = {h: sum(a * b for a, b in zip(w, zs(r[h], C))) for h in hs}
        ss = sorted(after)
        for role in ROLES:
            for i in range(len(ss)):
                for j in range(i + 1, len(ss)):
                    a, b = comp.get((ss[i], role)), comp.get((ss[j], role))
                    if not a or not b:
                        continue
                    common = sorted(set(a) & set(b))
                    c = corr([a[h] for h in common], [b[h] for h in common])
                    if c is not None:
                        vals.append(c)
    return st.mean(vals) if vals else 0.0


def choose(usable, prefer):
    G = grid()
    units = [u for p in usable for u in p["units"]]
    snaps = [p["after"] for p in usable]
    s1 = {g: response(units, [x / 100 for x in g]) for g in G}
    s2 = {g: agreement(snaps, [x / 100 for x in g]) for g in G}

    def scaled(s):
        lo, hi = min(s.values()), max(s.values())
        return {g: (v - lo) / (hi - lo) if hi > lo else 1.0 for g, v in s.items()}
    r1, r2 = scaled(s1), scaled(s2)
    best = max(G, key=lambda g: (round(min(r1[g], r2[g]), 6),
                                 -sum(abs(a - b) for a, b in zip(g, prefer))))
    return best, s1, s2


def snap5(ws):
    """Per-weight values back onto 5% steps that add up to 100 (largest remainders get the rounding)."""
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
    ap.add_argument("--patches", default=os.path.join(ROOT, "meta", "patches.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "meta", "weights.json"))
    ap.add_argument("--report", action="store_true", help="print the scores even when nothing changes")
    a = ap.parse_args()
    old = load(a.out, {}) or {}
    cur = old.get("weights") or DEFAULT
    patches = (load(a.patches, {}) or {}).get("patches", [])
    usable = build_units(a.archive, patches)
    ids = [p["id"] for p in usable]
    print(f"weights: usable patches {len(usable)} ({', '.join(ids) or 'none'}), need {MIN_PATCHES}", file=sys.stderr)
    if ids == old.get("patchesUsed") and not a.report:
        print("weights: no new patch to learn from, unchanged", file=sys.stderr)
        return
    out = {"weights": cur, "status": old.get("status", "collecting"), "patchesUsed": ids,
           "need": MIN_PATCHES, "history": old.get("history", []), "evaluatedAt": datetime.now().astimezone().isoformat(timespec="minutes")}
    if usable:
        prefer = (cur["wr"], cur["pr"], cur["br"])
        best, s1, s2 = choose(usable, prefer)
        picks = [best]
        if len(usable) >= 3:
            for i in range(len(usable)):
                picks.append(choose(usable[:i] + usable[i + 1:], prefer)[0])
        final = snap5([st.median(p[k] for p in picks) for k in range(3)])
        spread = max(max(p[k] for p in picks) - min(p[k] for p in picks) for k in range(3))
        dflt = (DEFAULT["wr"], DEFAULT["pr"], DEFAULT["br"])
        out["candidate"] = {"wr": final[0], "pr": final[1], "br": final[2], "leaveOneOutSpread": spread,
                            "response": round(response([u for p in usable for u in p["units"]], [x / 100 for x in final]), 3),
                            "agreement": round(agreement([p["after"] for p in usable], [x / 100 for x in final]), 3),
                            "defaultResponse": round(s1[dflt], 3), "defaultAgreement": round(s2[dflt], 3)}
        print(f"weights: candidate {final} (leave-one-out spread {spread}%p), response {out['candidate']['response']} "
              f"vs default {s1[dflt]:.3f}, agreement {out['candidate']['agreement']} vs default {s2[dflt]:.3f}",
              file=sys.stderr)
        if len(usable) >= MIN_PATCHES:
            new = dict(zip(("wr", "pr", "br"), step_towards([cur["wr"], cur["pr"], cur["br"]], final)))
            out["status"] = "tuned"
            if new != cur:
                out["weights"] = new
                out["history"].append({"at": out["evaluatedAt"], "patch": ids[-1], "weights": new})
                print(f"weights: default changed to {new}", file=sys.stderr)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(a.out + ".tmp", a.out)


if __name__ == "__main__":
    main()
