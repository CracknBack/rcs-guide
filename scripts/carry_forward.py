#!/usr/bin/env python3
"""Carry forward the published files THIS job does not build.

actions/deploy-pages replaces the whole site on every deploy, so anything a job does not place in
public/ is deleted from the live site the moment that job deploys. Three jobs publish here:

    guide.yml      builds index.html + sites/*.xml.gz   (every few hours, ~30 min)
    guide-v2.yml   builds guide-v2.json                 (every 15 minutes)
    epg-first.yml  builds guides/*.json                 (hourly)

resolve_v2.py's mirror_published already solves this for guide-v2.yml and epg-first.yml. guide.yml
had nothing equivalent: build_guide.py has no idea guide-v2.json or guides/ exist, so every run of it
deleted both. Confirmed live on 2026-11-11 -- the 06:46 run took out guide-v2.json and all 17 sport
files, and the Event Hub was running on hours-old cache by the time anyone noticed.

Deliberately narrow: it fetches ONLY the files this job does not produce. It must never pull down
index.html or sites/*.xml.gz, which the job has just rebuilt -- overwriting those with the previous
copies is how a "safety" step would quietly undo the whole build.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

UA = {'User-Agent': 'rcs-guide-carry-forward/1'}


def get(url, dest):
    """One retry: every file here is one the deploy would otherwise delete, so a transient blip is
    not a missing file, it is an outage lasting until the owning job next runs."""
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                data = r.read()
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as fh:
                fh.write(data)
            return len(data)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print('  not published yet: %s' % url)
                return 0
            last = e
        except Exception as e:
            last = e
        if attempt == 0:
            time.sleep(2)
    print('  FAILED: %s (%s)' % (url, last), file=sys.stderr)
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True, help='Pages base URL of the currently-published site')
    ap.add_argument('--out', default='public', help='the directory about to be deployed')
    args = ap.parse_args()
    base = args.base.rstrip('/')

    kept = 0
    # guide-v2.json: rebuilt every 15 minutes by guide-v2.yml, but deleting it here still blanks the
    # Event Hub for whatever is left of that window.
    if get(base + '/guide-v2.json', os.path.join(args.out, 'guide-v2.json')):
        kept += 1

    # guides/: rebuilt only hourly, so losing these costs up to an hour of the per-sport event guide.
    # The file list comes from the index rather than being hardcoded, so a sport appearing or
    # disappearing needs no change here.
    gidx = os.path.join(args.out, 'guides', 'index.json')
    if get(base + '/guides/index.json', gidx):
        kept += 1
        try:
            with open(gidx, encoding='utf-8') as fh:
                files = [s['file'] for s in json.load(fh).get('sports', []) if s.get('file')]
        except Exception as e:
            print('  guides/index.json unreadable (%s)' % e, file=sys.stderr)
            files = []
        for rel in files + ['guides/now_next.json']:
            if get('%s/%s' % (base, rel), os.path.join(args.out, *rel.split('/'))):
                kept += 1

    print('carried forward %d file(s) this job does not build' % kept)


if __name__ == '__main__':
    main()
