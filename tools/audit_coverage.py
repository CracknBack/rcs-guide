#!/usr/bin/env python3
"""
Coverage audit: compares LEAGUES in resolve_v2.py against TheSportsDB's full real catalog, per sport,
and reports which real leagues are configured vs not -- the tool behind the 2026-11-10 batch that took
LEAGUES from 42 to 82 comps. Exists so expanding coverage is a repeatable "run this" process instead of
one-off ad-hoc research each time a gap gets reported.

Needs a REAL TheSportsDB key (TSDB_KEY env var or --key) -- the free/test key only returns a crippled
~10-league demo subset for all_leagues.php, useless for this. Never hardcode or commit a real key.

Usage:
    python tools/audit_coverage.py                       # full per-sport gap report
    python tools/audit_coverage.py --activity 4490 4483   # check real recent/upcoming activity for
                                                           # specific league ids before adding them
"""
import argparse, json, os, re, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every TheSportsDB strSport this file's LEAGUES actually draws from, mapped to the app's own tab name
# -- keep this in sync with resolve_v2.py's LEAGUES / tv-app's SPORT_GROUPS if a new sport is added.
SPORT_TABS = {
    'Soccer': 'soccer', 'Basketball': 'basketball', 'Ice Hockey': 'icehockey', 'Baseball': 'baseball',
    'American Football': 'amfootball', 'Motorsport': 'motorsport', 'Tennis': 'tennis', 'Golf': 'golf',
    'Fighting': 'mma/boxing', 'Rugby': 'rugby/nrl', 'Cricket': 'cricket', 'Cycling': 'cycling',
    'Australian Football': 'afl',
}


def fetch(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def configured_ids():
    txt = open(os.path.join(ROOT, 'resolve_v2.py'), encoding='utf-8').read()
    m = re.search(r'LEAGUES = \[(.*?)\n\]', txt, re.S)
    return set(re.findall(r"'tsdb_league_id': '(\d+)'", m.group(1)))


def gap_report(key):
    leagues = fetch(f'https://www.thesportsdb.com/api/v1/json/{key}/all_leagues.php')['leagues']
    if len(leagues) < 50:
        print('WARNING: only %d leagues returned -- this looks like the crippled free/test key, not a '
              'real one. Results below are not meaningful.' % len(leagues), file=sys.stderr)
    cfg = configured_ids()
    by_sport = {}
    for l in leagues:
        by_sport.setdefault(l['strSport'], []).append(l)
    for sport, tab in SPORT_TABS.items():
        rows = by_sport.get(sport, [])
        unconfigured = [r for r in rows if r['idLeague'] not in cfg]
        print('\n=== %s (app tab: %s) -- %d total, %d NOT configured ===' % (sport, tab, len(rows), len(unconfigured)))
        for r in unconfigured:
            print('  %s | %s' % (r['idLeague'], r['strLeague']))


def activity_report(key, ids):
    print('%-6s %-5s %-5s %s' % ('id', 'next', 'past', 'latest_past'))
    for lid in ids:
        nxt = fetch(f'https://www.thesportsdb.com/api/v1/json/{key}/eventsnextleague.php?id={lid}')
        pst = fetch(f'https://www.thesportsdb.com/api/v1/json/{key}/eventspastleague.php?id={lid}')
        n_next = len((nxt or {}).get('events') or [])
        p_past = (pst or {}).get('events') or []
        latest = p_past[0]['dateEvent'] if p_past else None
        print('%-6s %-5s %-5s %s' % (lid, n_next, len(p_past), latest))
        time.sleep(0.15)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--key', default=os.environ.get('TSDB_KEY', ''))
    ap.add_argument('--activity', nargs='+', metavar='LEAGUE_ID')
    args = ap.parse_args()
    if not args.key:
        sys.exit('A real TheSportsDB key is required: --key or TSDB_KEY env var (not the free/test key).')
    if args.activity:
        activity_report(args.key, args.activity)
    else:
        gap_report(args.key)
