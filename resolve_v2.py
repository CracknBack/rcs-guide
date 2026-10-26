#!/usr/bin/env python3
"""
guide-v2: a small, resolved "which channel shows this game" file, built server-side so the TV app
never has to fetch or merge anything itself for this -- see Plan-New-Guide.md sections 3-4 for why
(the TV-side merge of a dozen-plus scraped guides was the actual source of the "multiple
interpretations" bugs, and is heavy enough to have OOM-crashed a Fire TV Stick before).

Scope for this first slice (deliberately small, per the plan's phased approach): Premier League only,
UK/US/Australia. Two sources:
  - football-data.org: the fixtures themselves (kickoff, status) -- ground truth, not a TV guide.
  - TheSportsDB (paid tier, eventstv.php): which channel/country is showing each match, by day.
    Confirmed by hand (see the session that built this) to return real, specific channel names and
    times across every country tried, not just the 5 the app's own TSDB_COUNTRIES list used to assume.
    It does NOT say LIVE vs REPLAY/HIGHLIGHTS -- there is no such field anywhere in its schema -- so
    that is inferred here, once, from listing-time-vs-kickoff, and always marked inferred. This keeps
    the "same raw fact interpreted five different ways in five different functions" bug (see the plan)
    from recurring on the server side.

Every event here is "likely", never "confirmed": this is one source, and Plan principle 2 reserves
"confirmed" for two independent sources agreeing, or the channel's own live guide agreeing at the
moment it's actually watched -- that second check only the app itself can do (L7), against the
provider's own guide, which never leaves the device. This file is an input to that check, not a
replacement for it.
"""
import json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

FD_KEY = os.environ.get('FD_KEY', '').strip()
TSDB_KEY = os.environ.get('TSDB_KEY', '').strip()
COUNTRIES = [('UK', 'United_Kingdom'), ('US', 'United_States'), ('AU', 'Australia')]
DAYS_AHEAD = 10
UA = {'User-Agent': 'red-carpet-streams-guide-v2/1'}


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def fetch_fixtures():
    if not FD_KEY:
        print('No FD_KEY set -- skipping fixtures (nothing to resolve).', file=sys.stderr)
        return []
    url = 'https://api.football-data.org/v4/competitions/PL/matches?status=SCHEDULED,TIMED,IN_PLAY,PAUSED'
    try:
        j = fetch_json(url, {'X-Auth-Token': FD_KEY})
    except Exception as e:
        print('Fixtures fetch failed:', e, file=sys.stderr)
        return []
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=DAYS_AHEAD)
    out = []
    for m in j.get('matches', []):
        kick = datetime.fromisoformat(m['utcDate'].replace('Z', '+00:00'))
        if kick > horizon:
            continue
        out.append({
            'id': 'fd:PL:%s' % m['id'],
            'comp': 'Premier League',
            'home': m['homeTeam']['name'], 'away': m['awayTeam']['name'],
            'kick': m['utcDate'], 'kick_ts': kick.timestamp(), 'status': m['status'],
        })
    return out


def norm_team(name):
    s = (name or '').lower()
    for w in ('fc', 'afc', 'cf', 'city', 'united', 'utd', 'town', 'rovers', 'wanderers', 'albion', 'athletic', 'the'):
        s = s.replace(' ' + w, '').replace(w + ' ', '')
    return ''.join(c for c in s if c.isalnum() or c == ' ').strip()


def teams_match(ev_home, ev_away, listing_title):
    t = norm_team(listing_title)
    h, a = norm_team(ev_home), norm_team(ev_away)
    if not h or not a:
        return False
    hh = h.split()[0] if h.split() else h
    aa = a.split()[0] if a.split() else a
    return hh in t and aa in t

# Tempting to filter out a channel whose name names a different sport (e.g. "Sky Sports Golf" showing
# a football match) as an obvious data error -- don't: Sky Sports genuinely repurposes its themed
# channels (Golf, Cricket, Darts, Action...) as football overflow slots during simultaneous kickoffs,
# confirmed by the owner. A static name filter would silently hide real coverage, which is worse than
# the problem it's guarding against. Leave this to "likely" confidence + the app's own tap-time check,
# same as everything else -- never a static rule based on a channel's usual name.


def fetch_tsdb_day(day, country_param):
    if not TSDB_KEY:
        return []
    url = 'https://www.thesportsdb.com/api/v1/json/%s/eventstv.php?%s' % (
        urllib.parse.quote(TSDB_KEY), urllib.parse.urlencode({'d': day, 'a': country_param, 's': 'Soccer'}))
    for attempt in range(3):
        try:
            j = fetch_json(url)
            return j.get('tvevents') or []
        except Exception as e:
            if attempt == 2:
                print('TheSportsDB fetch failed for %s %s: %s' % (country_param, day, e), file=sys.stderr)
                return []
            time.sleep(2)


def classify_kind(listing_ts, kick_ts):
    """Inferred, never a fact from the source itself -- see module docstring. Mirrors the TV app's own
    fixtureEvents() time windows so the two don't silently disagree (one of the plan's four principles:
    the label is a guess about the data, say so, and make the guess the same guess everywhere)."""
    delta = listing_ts - kick_ts
    if -3 * 3600 <= delta <= 3600:
        return 'LIVE'
    if delta > 3600:
        return 'REPLAY'
    return 'PREVIEW'


def resolve():
    fixtures = fetch_fixtures()
    if not fixtures:
        return {'generated': datetime.now(timezone.utc).isoformat(), 'competitions': ['Premier League'],
                'countries': [c for c, _ in COUNTRIES], 'events': [], 'note': 'No fixtures resolved this run (see stderr).'}

    days = sorted({datetime.fromtimestamp(f['kick_ts'], tz=timezone.utc).strftime('%Y-%m-%d') for f in fixtures}
                  | {(datetime.fromtimestamp(f['kick_ts'], tz=timezone.utc) + timedelta(days=d)).strftime('%Y-%m-%d')
                     for f in fixtures for d in (-1, 0, 1)})

    now_iso = datetime.now(timezone.utc).isoformat()
    for f in fixtures:
        f['channels'] = []

    for cc, param in COUNTRIES:
        for day in days:
            for t in fetch_tsdb_day(day, param):
                ev_name = t.get('strEvent') or ''
                ts_raw = t.get('strTimeStamp') or (t.get('dateEvent', '') + ' ' + (t.get('strTime') or '00:00:00'))
                try:
                    listing_ts = datetime.strptime(ts_raw, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
                for f in fixtures:
                    if abs(listing_ts - f['kick_ts']) > 20 * 3600:
                        continue
                    if not teams_match(f['home'], f['away'], ev_name):
                        continue
                    f['channels'].append({
                        'channel': t.get('strChannel') or '', 'country': cc,
                        'kind': classify_kind(listing_ts, f['kick_ts']), 'inferred_kind': True,
                        'start': datetime.fromtimestamp(listing_ts, tz=timezone.utc).isoformat(),
                        'confidence': 'likely', 'source': 'thesportsdb', 'checked': now_iso,
                    })
            time.sleep(0.3)   # be polite even though the premium tier showed no 429s in testing

    for f in fixtures:
        del f['kick_ts']

    return {'generated': now_iso, 'competitions': ['Premier League'], 'countries': [c for c, _ in COUNTRIES],
            'events': fixtures}


# guide-v2 runs on its own, fast, frequent schedule (unlike the ~20min XMLTV build), and
# actions/deploy-pages replaces the WHOLE site on every deploy -- so a bare "write guide-v2.json and
# deploy" would wipe out the existing sports.xml.gz/status.json/index.html/type.html between runs of
# the main guide.yml job. Mirror whatever's actually live first (same status.json-driven site list
# cmd_fetch_prev in build_guide.py already uses, extended to the files that one doesn't fetch), then
# add guide-v2.json into that same directory, so every deploy carries the full site forward.
def mirror_published(base, dest):
    base = base.rstrip('/')
    os.makedirs(dest, exist_ok=True)

    def get(url, to):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                with open(to, 'wb') as fh:
                    fh.write(r.read())
            return True
        except Exception as e:
            print('  mirror: %s not available (%s)' % (url, e), file=sys.stderr)
            return False

    if not get(base + '/status.json', os.path.join(dest, 'status.json')):
        print('No currently-published guide found -- first run, or the main guide.yml job has not run yet.', file=sys.stderr)
        return
    for name in ('index.html', 'type.html', 'sports.xml.gz', '.nojekyll'):
        get('%s/%s' % (base, name), os.path.join(dest, name))
    try:
        with open(os.path.join(dest, 'status.json'), encoding='utf-8') as fh:
            sites = [s['site'] for s in json.load(fh).get('sites', []) if s.get('status') in ('ok', 'kept previous')]
    except Exception as e:
        print('Could not parse status.json for the per-site list: %s' % e, file=sys.stderr)
        sites = []
    if sites:
        os.makedirs(os.path.join(dest, 'sites'), exist_ok=True)
        for s in sites:
            get('%s/sites/%s.xml.gz' % (base, s), os.path.join(dest, 'sites', s + '.xml.gz'))
    print('Mirrored the currently-published guide (%d per-site files) into %s' % (len(sites), dest))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='public', help='directory to write guide-v2.json into')
    ap.add_argument('--mirror-base', default='', help='Pages base URL to mirror the existing guide from first (e.g. https://OWNER.github.io/REPO)')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    if args.mirror_base:
        mirror_published(args.mirror_base, args.out)

    data = resolve()
    out_path = os.path.join(args.out, 'guide-v2.json')
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    n_ev = len(data['events']); n_ch = sum(len(e.get('channels', [])) for e in data['events'])
    print('Wrote %s: %d events, %d channel listings' % (out_path, n_ev, n_ch))
