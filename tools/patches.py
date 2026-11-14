#!/usr/bin/env python3
"""Collect the official Overwatch patch notes (ko-kr) into meta/patches.json, hero by hero, with every change
line marked as a buff (+1), a nerf (-1) or neither (0), and a verdict per hero.

Usage:
    python3 tools/patches.py [--out meta/patches.json] [--since 2026-09] [--months 2] [--html DIR]

The months from --since on are read once; afterwards the last --months months are read again on every run (patch
notes are sometimes corrected after release). Patches already stored for months that are not re-read are kept.
Corrections by hand go to meta/patch-overrides.json: {"<patch id>": {"<hero id>": "buff|nerf|mixed|rework|change"}}.

The marking is deliberately conservative: a line counts only when it reads "<stat> ... A에서 B(으)로 증가/감소했습니다"
(or "N% 증가/감소했습니다") for a stat whose good direction is known. New, removed and reworded mechanics count as 0,
and a hero with such lines or with lines pointing both ways is not a clear buff or nerf. Only clear verdicts are
used to tune the index weights (tools/weights.py)."""
import argparse
import html as htmllib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://overwatch.blizzard.com/ko-kr/news/patch-notes/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
KST = timezone(timedelta(hours=9))

# sections that do not change competitive play: the Stadium mode and bug fixes
SKIP_SECTION = re.compile(r"스타디움|버그 수정|아케이드|이벤트|6대6|Stadium|Bug Fix|Arcade|Event|6v6", re.I)
ROLE_SECTION = re.compile(r"^(돌격|공격|지원)( 영웅)?$|^(Tank|Damage|Support)( Heroes| Hero)?( Updates)?$", re.I)
# stats where a smaller number is better for the hero (everything else: bigger is better)
LOWER_BETTER = re.compile(
    r"재사용 대기\s?시간|대기시간|시전 시간|회복 시간|재장전 시간|장전 시간|탄 퍼짐|산탄 범위|확산|지연 시간|"
    r"충전 시간|궁극기 비용|경험치|필요한 처치|타점|받는 피해|자가 피해|전환 시간|준비 시간|이동 시간|소모량|소모율|비용|"
    r"분산|폭발 시간|변신 지속")
# words that make a line's direction unclear: it changes something for the enemy or reshapes a mechanic
UNCLEAR = re.compile(r"완화|적에게 적용|제거되었|신규|이제 |더 이상|추가되었|변경되었|대신|\(6대6\)|6대6")
CHANGE = re.compile(r"(?:에서\s*[\d.,~]+\s*[^\s]*?\s*(?:으로|로)\s*|[\d.,]+\s*%\s*)(증가|감소)했습니다")
# the English pages (some months have no Korean translation)
LOWER_BETTER_EN = re.compile(r"cooldown|cast time|recovery|spread|ultimate cost|ult cost|delay|charge time|reload time|"
                             r"hitbox|self.damage|damage taken|fuse|wind.?up|startup|\bcost\b|experience required", re.I)
UNCLEAR_EN = re.compile(r"\bnew\b|removed|\bnow\b|no longer|\badded|instead|6v6|rework|replaced|reverted", re.I)
CHANGE_EN = re.compile(r"\b(increased|decreased|reduced|lowered|raised)\b[^.]*?(?:\bfrom\s+[\d.,]+[^.]*?\bto\s+[\d.,]+|\bby\s+[\d.,]+\s*%)", re.I)


def direction(line):
    if re.search(r"[가-힣]", line):
        if UNCLEAR.search(line):
            return 0
        m = CHANGE.search(line)
        if not m:
            return 0
        up = 1 if m.group(1) == "증가" else -1
        return -up if LOWER_BETTER.search(line[:m.start()]) else up
    if UNCLEAR_EN.search(line):
        return 0
    m = CHANGE_EN.search(line)
    if not m:
        return 0
    up = 1 if m.group(1).lower() in ("increased", "raised") else -1
    return -up if LOWER_BETTER_EN.search(line[:m.start()]) else up


class Tree(HTMLParser):
    """A minimal DOM: nodes are [tag, attrs, children]; text is kept as plain strings."""
    VOID = {"img", "br", "hr", "meta", "link", "input", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ["root", {}, []]
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = [tag, dict(attrs), []]
        self.stack[-1][2].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1][2].append(data)


def cls(n):
    return (n[1].get("class") or "").split() if isinstance(n, list) else []


def text(n):
    if isinstance(n, str):
        return n
    return "".join(text(c) for c in n[2])


def norm(s):
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def find_all(n, pred, out=None):
    out = [] if out is None else out
    if isinstance(n, list):
        if pred(n):
            out.append(n)
        for c in n[2]:
            find_all(c, pred, out)
    return out


def walk(n):
    """Elements in document order."""
    if isinstance(n, list):
        yield n
        for c in n[2]:
            yield from walk(c)


def lines_of(n):
    return [norm(text(li)) for li in find_all(n, lambda x: x[0] == "li") if norm(text(li))]


def parse_hero(node, names):
    name = norm(text(find_all(node, lambda x: "PatchNotesHeroUpdate-name" in cls(x))[0]))
    groups = []
    for g in find_all(node, lambda x: "PatchNotesHeroUpdate-generalUpdates" in cls(x)):
        # "<p>label</p><ul>..</ul>" pairs; a list without a label belongs to the hero as a whole
        label = None
        for c in g[2]:
            if isinstance(c, list) and c[0] == "p":
                label = norm(text(c))
            elif isinstance(c, list) and c[0] == "ul":
                groups.append({"name": label or "일반", "lines": lines_of(c)})
                label = None
    for a in find_all(node, lambda x: "PatchNotesAbilityUpdate" in cls(x)):
        nm = find_all(a, lambda x: "PatchNotesAbilityUpdate-name" in cls(x))
        groups.append({"name": norm(text(nm[0])) if nm else "", "lines": lines_of(a)})
    out, signs = [], []
    for g in groups:
        ls = []
        for t in g["lines"]:
            if "6대6" in t or "6v6" in t.lower():
                continue                     # 6v6 only: the stats here are 5v5 role queue
            d = direction(t)
            ls.append({"t": t, "d": d})
            signs.append(d)
        if ls:
            out.append({"name": g["name"], "lines": ls})
    if not out:
        return None
    pos, neg, zero = signs.count(1), signs.count(-1), signs.count(0)
    if pos and neg:
        verdict = "mixed"
    elif pos and not zero:
        verdict = "buff"
    elif neg and not zero:
        verdict = "nerf"
    elif pos and zero <= pos / 3:
        verdict = "buff"                     # mostly clear: shown as a buff, but not used for tuning
    elif neg and zero <= neg / 3:
        verdict = "nerf"
    else:
        verdict = "change"
    clear = verdict in ("buff", "nerf") and zero == 0
    return {"name": name, "id": names.get(name) or names.get(latin_key(name)), "verdict": verdict, "clear": clear, "groups": out}


TITLE_DATE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
TITLE_DATE_EN = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})")
MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december"]


def parse_page(src, names):
    t = Tree()
    t.feed(src)
    patches = []
    for p in find_all(t.root, lambda x: "PatchNotes-patch" in cls(x)):
        anchor = find_all(p, lambda x: "anchor" in cls(x))
        pid = (anchor[0][1].get("id") or "").replace("patch-", "") if anchor else ""
        title_n = find_all(p, lambda x: "PatchNotes-patchTitle" in cls(x))
        title = norm(text(title_n[0])) if title_n else ""
        m = TITLE_DATE.search(title)
        me = TITLE_DATE_EN.search(title)
        live = (f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else
                f"{me.group(3)}-{MONTHS_EN.index(me.group(1).lower()) + 1:02d}-{int(me.group(2)):02d}" if me else pid)
        heroes, sections, section, skip, rework = [], [], "", False, False
        for el in walk(p):
            c = cls(el)
            if "PatchNotes-sectionTitle" in c:
                s = norm(text(el))
                if ROLE_SECTION.match(s):
                    section, rework = s, False    # a role heading inherits the mode (e.g. Stadium) above it
                else:
                    section, skip = s, bool(SKIP_SECTION.search(s))
                    rework = "개편" in s or "rework" in s.lower()
                if not skip and s not in sections:
                    sections.append(s)
            elif "PatchNotesHeroUpdate" in c and not skip:
                h = parse_hero(el, names)
                if h:
                    h["section"] = section
                    if rework:
                        h["verdict"], h["clear"] = "rework", False   # a rework is neither a buff nor a nerf
                    elif "타점" in section or "hitbox" in section.lower():
                        # hitbox sizes: shown, but too small to call a buff or a nerf
                        h["verdict"], h["clear"] = "hitbox", False
                        for g in h["groups"]:
                            g["name"] = "타점"
                    heroes.append(h)
        # one hero can appear in several sections (e.g. abilities and perks): merge them
        merged = {}
        for h in heroes:
            k = h["id"] or h["name"]
            if k in merged:
                m0 = merged[k]
                m0["groups"] += h["groups"]
                vs = {m0["verdict"], h["verdict"]}
                if len(vs) > 1:
                    vs.discard("hitbox")          # hitbox sizes do not change the verdict of the other changes
                m0["verdict"] = (vs.pop() if len(vs) == 1 else
                                 "rework" if "rework" in vs else
                                 "mixed" if vs & {"buff"} and vs & {"nerf"} or "mixed" in vs else
                                 ("buff" if "buff" in vs else "nerf" if "nerf" in vs else "change"))
                m0["clear"] = ((m0["clear"] or m0["verdict"] == "hitbox") and (h["clear"] or h["verdict"] == "hitbox")
                               and m0["verdict"] in ("buff", "nerf"))
                m0["section"] = m0["section"] if m0["section"] == h["section"] else m0["section"] + " · " + h["section"]
            else:
                merged[k] = h
        patches.append({"id": pid, "date": live, "title": title, "url": BASE + ("#patch-" + pid if pid else ""),
                        "heroes": list(merged.values())})
    return patches


def fetch(url):
    r = subprocess.run(["curl", "-sSL", "--compressed", "-m", "40", "-A", UA, url], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"{url}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout.decode("utf-8", errors="replace")


def latin_key(name):
    """'Soldier: 76' / 'Lúcio' / 'Junkerqueen' -> 'soldier76' / 'lucio' / 'junkerqueen' (matched against ids)."""
    import unicodedata
    t = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


def hero_names():
    """Korean hero names -> ids from the stored archives (every server, retired role entries excluded); English
    names match ids with the punctuation taken out."""
    names, roles = {}, {}
    arc = os.path.join(ROOT, "archive")
    for s in sorted(os.listdir(arc)) if os.path.isdir(arc) else []:
        try:
            meta = json.load(open(os.path.join(arc, s, "meta.json"), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for hid, h in (meta.get("heroes") or {}).items():
            if "~" in hid or not h.get("name"):
                continue
            names.setdefault(h["name"], hid)
            names.setdefault(latin_key(hid), hid)
            roles.setdefault(hid, h.get("role"))
    return names, roles


def months(since, back):
    now = datetime.now(KST)
    y, m = map(int, since.split("-"))
    out = []
    while (y, m) <= (now.year, now.month):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out, set(out[-back:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "meta", "patches.json"))
    ap.add_argument("--since", default="2026-09")
    ap.add_argument("--months", type=int, default=2)
    ap.add_argument("--html", help="read <DIR>/<yyyy>-<mm>.html instead of downloading (for tests)")
    a = ap.parse_args()
    names, roles = hero_names()
    old = {}
    try:
        old = json.load(open(a.out, encoding="utf-8"))
    except (OSError, ValueError):
        pass
    done = set(old.get("months", []))
    keep = {p["id"]: p for p in old.get("patches", [])}
    all_months, recent = months(a.since, a.months)
    read = []
    for y, m in all_months:
        key = f"{y}-{m:02d}"
        if key in done and (y, m) not in recent:
            continue
        try:
            if a.html:
                src = open(os.path.join(a.html, key + ".html"), encoding="utf-8").read()
            else:
                src = fetch(f"{BASE}live/{y}/{m}/")
                if "PatchNotes-patch" not in src:
                    # no Korean page for this month: the English one
                    src = fetch(f"{BASE.replace('/ko-kr/', '/en-us/')}live/{y}/{m}/")
        except (OSError, RuntimeError) as e:
            print(f"patch notes {key}: {e}", file=sys.stderr)
            continue
        ps = parse_page(src, names)
        ps = [p for p in ps if p["date"][:7] == key or p["id"][:7] == key]
        for p in ps:
            keep[p["id"]] = p
        read.append(key)
        done.add(key)
        print(f"patch notes {key}: {len(ps)} patches, "
              f"{sum(len(p['heroes']) for p in ps)} hero entries", file=sys.stderr)
    overrides = {}
    try:
        overrides = json.load(open(os.path.join(os.path.dirname(a.out), "patch-overrides.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    unknown = set()
    for p in keep.values():
        for h in p["heroes"]:
            if not h.get("id"):
                h["id"] = names.get(h["name"]) or names.get(latin_key(h["name"]))
            if not h.get("id"):
                unknown.add(h["name"])
            h["role"] = roles.get(h.get("id"))
            ov = (overrides.get(p["id"]) or {}).get(h.get("id") or h["name"])
            if ov:
                h["verdict"], h["clear"], h["manual"] = ov, ov in ("buff", "nerf"), True
    for n in sorted(unknown):
        print(f"patch notes: no hero id for '{n}'", file=sys.stderr)
    patches = sorted(keep.values(), key=lambda p: (p["date"], p["id"]), reverse=True)
    body = {"months": sorted(done), "patches": patches}
    if body != {k: old.get(k) for k in body}:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out + ".tmp", "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(a.out + ".tmp", a.out)
        print("patch notes: updated", file=sys.stderr)
    else:
        print("patch notes: unchanged", file=sys.stderr)


if __name__ == "__main__":
    main()
