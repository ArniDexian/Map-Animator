#!/usr/bin/env python3
"""Check every installed Python package (direct + automatic) against public sources.

For each package: who publishes it, how old it is, how widely it is used, whether its GitHub repository is alive,
whether the installed version has known vulnerabilities (OSV database), and whether the downloaded wheel is
byte-for-byte the file PyPI publishes. Run it with the project's environment:

    .venv/bin/python tools/audit_dependencies.py

Needs internet access. Nothing is installed or changed.
"""
import hashlib
import importlib.metadata as md
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

SKIP = {"pip", "setuptools", "wheel"}


def get(url, body=None):
    headers = {"User-Agent": "map-animator-dependency-audit"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, headers)
    for attempt in (1, 2):                      # one retry: the public APIs occasionally drop a request
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - a missing answer must not stop the audit
            err = str(e)[:80]
            time.sleep(1.5)
    return {"_error": err}


def main():
    pkgs = sorted((d.metadata["Name"], d.version) for d in md.distributions() if d.metadata["Name"].lower() not in SKIP)
    now = datetime.now(timezone.utc)
    print(f"{'package':19s} {'version':12s} {'age':>5s} {'downloads/month':>16s}  {'GitHub repository':34s} {'stars':>6s} {'last push':10s} {'known vulns':>11s}  wheel==PyPI")
    problems = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, ver in pkgs:
            meta = get(f"https://pypi.org/pypi/{name}/{ver}/json")
            allrel = get(f"https://pypi.org/pypi/{name}/json")
            if "_error" in meta or "_error" in allrel:
                print(f"{name:19s} {ver:12s} PyPI did not answer"); problems += 1; continue
            first = min(datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00")) for fs in allrel["releases"].values() for f in fs)
            urls = list((meta["info"].get("project_urls") or {}).values()) + [meta["info"].get("home_page") or ""]
            m = next((re.search(r"github\.com/([^/]+/[^/#?]+)", u) for u in urls if u and re.search(r"github\.com/([^/]+/[^/#?]+)", u)), None)
            repo = m.group(1).removesuffix(".git") if m else None
            gh = get(f"https://api.github.com/repos/{repo}") if repo else {}
            dl = (get(f"https://pypistats.org/api/packages/{name}/recent").get("data") or {}).get("last_month")
            osv = get("https://api.osv.dev/v1/query", {"package": {"name": name, "ecosystem": "PyPI"}, "version": ver})
            vulns = len(osv.get("vulns", []))
            # is the wheel pip downloads exactly the file PyPI lists?
            published = {f["filename"]: f["digests"]["sha256"] for f in meta["urls"]}
            subprocess.run([sys.executable, "-m", "pip", "download", f"{name}=={ver}", "--no-deps", "--only-binary=:all:", "-d", tmp, "-q",
                            "--disable-pip-version-check"], capture_output=True)
            import glob, os
            match = "?"
            for f in glob.glob(tmp + "/*"):
                fn = os.path.basename(f)
                if fn in published:
                    match = "yes" if hashlib.sha256(open(f, "rb").read()).hexdigest() == published[fn] else "NO"
                    os.remove(f)
            problems += (vulns > 0) + (match == "NO")
            print(f"{name:19s} {ver:12s} {round((now - first).days / 365.25, 1):>4}y {(f'{dl:,}' if dl else 'n/a'):>16}  {str(repo):34s} {gh.get('stargazers_count', '?'):>6} {(gh.get('pushed_at') or '')[:10]:10s} {vulns or 'none':>11}  {match}")
    print("\nProblems found:", problems or "none")


if __name__ == "__main__":
    main()
