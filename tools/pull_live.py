#!/usr/bin/env python3
"""Download the data the live site serves now into data/ (used by the workflow once the site is deployed by
GitHub Actions and data/ is no longer kept in the repository): the refresh starts from what readers see, so a
server that is waiting keeps its published numbers and an unchanged run is recognized."""
import json
import os
import sys
import urllib.request

def _site():
    """The project site of this repository (follows a rename): https://<owner>.github.io/<repo>/."""
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"]
    owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "uhye0n/OW-TIER-LIST").partition("/")
    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/"
    return f"https://{owner.lower()}.github.io/{repo}/"


SITE = _site()
OUT = sys.argv[1] if len(sys.argv) > 1 else "data"


def get(path):
    with urllib.request.urlopen(SITE + path, timeout=30) as r:
        return r.read()


def save(path, body):
    full = os.path.join(OUT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(body)


def main():
    try:
        idx_raw = get("data/index.json")
    except Exception as e:  # noqa: BLE001
        print(f"live data not reachable ({e}); starting without it")
        return
    save("index.json", idx_raw)
    n = 1
    for sv in json.loads(idx_raw).get("servers", []):
        try:
            raw = get(f"data/{sv['id']}.json")
        except Exception as e:  # noqa: BLE001
            print(f"{sv['id']}: not reachable ({e})")
            continue
        save(f"{sv['id']}.json", raw)
        n += 1
        for rid in json.loads(raw).get("ranges", []):
            try:
                save(f"{sv['id']}/{rid}.json", get(f"data/{sv['id']}/{rid}.json"))
                n += 1
            except Exception as e:  # noqa: BLE001
                print(f"{sv['id']}/{rid}: not reachable ({e})")
    print(f"pulled {n} files from {SITE}")


if __name__ == "__main__":
    main()
