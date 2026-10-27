#!/usr/bin/env python3
"""
guide-v2: a small, resolved "which channel shows this game" file, built server-side so the TV app
never has to fetch or merge anything itself for this -- see Plan-New-Guide.md sections 3-4 for why
(the TV-side merge of a dozen-plus scraped guides was the actual source of the "multiple
interpretations" bugs, and is heavy enough to have OOM-crashed a Fire TV Stick before).

Leagues covered: see LEAGUES below. Two ways a league gets its fixtures (ground truth: who's playing,
when, status):
  - 'football-data': football-data.org. Used for Premier League specifically because it was already
    the app's existing fixtures source and gives a finer-grained live status than TheSportsDB's events
    endpoint does.
  - 'tsdb': TheSportsDB's own eventsnextleague.php/eventspastleague.php. Used for everything else
    (NBA, NHL, MLB, NFL) -- confirmed by hand that these return real fixtures with real timestamps,
    the same quality as the football ones.

Every league then gets its TV listings from TheSportsDB's eventstv.php, by country and day. It does
NOT say LIVE vs REPLAY/HIGHLIGHTS -- there is no such field anywhere in its schema -- so that is
inferred here, once, from listing-time-vs-kickoff, and always marked inferred.

Matching a TV listing to a fixture:
  - When the fixture ITSELF came from TheSportsDB ('tsdb' leagues): match by idEvent, exact. Confirmed
    by hand that eventstv.php's idEvent is the same id eventsnextleague.php uses for the same event --
    an exact join, not a guess, wherever TheSportsDB has both sides.
  - When the fixture came from football-data.org (no TheSportsDB id to join on): match by team name,
    same approach as before, because the two sources don't share an id space.

Every event here is "likely", never "confirmed": this is one source per claim, and Plan principle 2
reserves "confirmed" for two independent sources agreeing, or the channel's own live guide agreeing at
the moment it's actually watched -- that second check only the app itself can do (L7), against the
provider's own guide, which never leaves the device. This file is an input to that check, not a
replacement for it.
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

FD_KEY = os.environ.get('FD_KEY', '').strip()
TSDB_KEY = os.environ.get('TSDB_KEY', '').strip()
DAYS_AHEAD = 10
UA = {'User-Agent': 'red-carpet-streams-guide-v2/1'}

# comp must match the TV app's COMPS display name exactly, so the app can line up events with the
# competition tab a user picked. countries: (short code the app understands, TheSportsDB's "a=" param).
LEAGUES = [
    {'comp': 'Premier League', 'fixtures': 'football-data', 'fd_comp': 'PL', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('US', 'United_States'), ('AU', 'Australia')]},

    # English domestic cups: not on football-data.org's free plan and previously left out of this list
    # entirely, so the app never had fixture or TV data for them at all (confirmed missing, not just
    # unmatched -- a real Fleetwood Town vs Arsenal EFL Cup tie, live-checked 2026-10-27, had a genuine
    # TheSportsDB TV listing, 'BeIn Sports 3 Australia', that this app simply never fetched). TheSportsDB
    # ids confirmed by hand the same day: FA Cup 4482, EFL Cup (Carabao Cup) 4570.
    {'comp': 'FA Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '4482', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('US', 'United_States'), ('AU', 'Australia')]},
    {'comp': 'League Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '4570', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('US', 'United_States'), ('AU', 'Australia')]},
    {'comp': 'NBA', 'fixtures': 'tsdb', 'tsdb_league_id': '4387', 'tsdb_sport': 'Basketball',
     'countries': [('US', 'United_States'), ('UK', 'United_Kingdom'), ('AU', 'Australia')]},
    {'comp': 'NHL', 'fixtures': 'tsdb', 'tsdb_league_id': '4380', 'tsdb_sport': 'Ice Hockey',
     'countries': [('US', 'United_States'), ('CA', 'Canada')]},
    {'comp': 'MLB', 'fixtures': 'tsdb', 'tsdb_league_id': '4424', 'tsdb_sport': 'Baseball',
     'countries': [('US', 'United_States')]},
    {'comp': 'NFL', 'fixtures': 'tsdb', 'tsdb_league_id': '4391', 'tsdb_sport': 'American Football',
     'countries': [('US', 'United_States'), ('UK', 'United_Kingdom')]},

    # European football: football-data.org's free plan covers these 6 (confirmed via this key's own
    # /v4/competitions listing on 2026-10-26) -- same source as Premier League, more detailed status
    # than TheSportsDB's generic strStatus. The rest aren't on that plan, so fixtures come from
    # TheSportsDB itself instead, same as NBA/NHL/MLB/NFL above.
    {'comp': 'La Liga', 'fixtures': 'football-data', 'fd_comp': 'PD', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('US', 'United_States'), ('AU', 'Australia')]},
    {'comp': 'Bundesliga', 'fixtures': 'football-data', 'fd_comp': 'BL1', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('DE', 'Germany'), ('AU', 'Australia')]},
    {'comp': 'Ligue 1', 'fixtures': 'football-data', 'fd_comp': 'FL1', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('FR', 'France'), ('AU', 'Australia')]},
    {'comp': 'Serie A', 'fixtures': 'football-data', 'fd_comp': 'SA', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('IT', 'Italy'), ('AU', 'Australia')]},
    {'comp': 'Eredivisie', 'fixtures': 'football-data', 'fd_comp': 'DED', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('NL', 'Netherlands')]},
    {'comp': 'Primeira Liga', 'fixtures': 'football-data', 'fd_comp': 'PPL', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom'), ('PT', 'Portugal')]},
    {'comp': 'Belgian Pro League', 'fixtures': 'tsdb', 'tsdb_league_id': '4338', 'tsdb_sport': 'Soccer',
     'countries': [('BE', 'Belgium'), ('UK', 'United_Kingdom')]},
    {'comp': 'Scottish Premiership', 'fixtures': 'tsdb', 'tsdb_league_id': '4330', 'tsdb_sport': 'Soccer',
     'countries': [('UK', 'United_Kingdom')]},
    {'comp': 'League of Ireland', 'fixtures': 'tsdb', 'tsdb_league_id': '4643', 'tsdb_sport': 'Soccer',
     'countries': [('IE', 'Ireland'), ('UK', 'United_Kingdom')]},
    {'comp': 'Austrian Bundesliga', 'fixtures': 'tsdb', 'tsdb_league_id': '4621', 'tsdb_sport': 'Soccer',
     'countries': [('AT', 'Austria'), ('UK', 'United_Kingdom')]},
    {'comp': 'Greek Super League', 'fixtures': 'tsdb', 'tsdb_league_id': '4336', 'tsdb_sport': 'Soccer',
     'countries': [('GR', 'Greece'), ('UK', 'United_Kingdom')]},
    {'comp': 'Turkish Super Lig', 'fixtures': 'tsdb', 'tsdb_league_id': '4339', 'tsdb_sport': 'Soccer',
     'countries': [('TR', 'Turkey'), ('UK', 'United_Kingdom')]},
    {'comp': 'Swiss Super League', 'fixtures': 'tsdb', 'tsdb_league_id': '4675', 'tsdb_sport': 'Soccer',
     'countries': [('CH', 'Switzerland'), ('UK', 'United_Kingdom')]},

    # Motorsport. No World Superbike (WSBK) entry exists in TheSportsDB's catalog (checked by hand,
    # 2026-10-26) -- only domestic "British Superbike Championship", a different, smaller series, so
    # it's deliberately left out rather than silently substituted for what was actually asked for.
    {'comp': 'Formula 1', 'fixtures': 'tsdb', 'tsdb_league_id': '4370', 'tsdb_sport': 'Motorsport',
     'countries': [('UK', 'United_Kingdom'), ('US', 'United_States'), ('AU', 'Australia')]},
    {'comp': 'MotoGP', 'fixtures': 'tsdb', 'tsdb_league_id': '4407', 'tsdb_sport': 'Motorsport',
     'countries': [('UK', 'United_Kingdom'), ('AU', 'Australia')]},
    {'comp': 'IndyCar', 'fixtures': 'tsdb', 'tsdb_league_id': '4373', 'tsdb_sport': 'Motorsport',
     'countries': [('US', 'United_States'), ('UK', 'United_Kingdom')]},
    {'comp': 'NASCAR', 'fixtures': 'tsdb', 'tsdb_league_id': '4393', 'tsdb_sport': 'Motorsport',
     'countries': [('US', 'United_States')]},

    # Tennis, Golf, Fighting: individual/card sports -- TheSportsDB gives no strHomeTeam/strAwayTeam for
    # these (confirmed by hand), everything is in strEvent instead ("Alcaraz vs Lehecka", "UFC Fight
    # Night 292 Moicano vs Nolan"). fetch_tsdb_fixtures already handles that (see its own comment).
    {'comp': 'ATP Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4464', 'tsdb_sport': 'Tennis',
     'countries': [('UK', 'United_Kingdom'), ('US', 'United_States'), ('AU', 'Australia')]},
    {'comp': 'WTA Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4517', 'tsdb_sport': 'Tennis',
     'countries': [('UK', 'United_Kingdom'), ('US', 'United_States'), ('AU', 'Australia')]},
    {'comp': 'PGA Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4425', 'tsdb_sport': 'Golf',
     'countries': [('US', 'United_States'), ('UK', 'United_Kingdom')]},
    {'comp': 'European Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4426', 'tsdb_sport': 'Golf',
     'countries': [('UK', 'United_Kingdom')]},
    {'comp': 'UFC', 'fixtures': 'tsdb', 'tsdb_league_id': '4443', 'tsdb_sport': 'Fighting',
     'countries': [('US', 'United_States'), ('UK', 'United_Kingdom'), ('AU', 'Australia')]},
    {'comp': 'Boxing', 'fixtures': 'tsdb', 'tsdb_league_id': '4445', 'tsdb_sport': 'Fighting',
     'countries': [('US', 'United_States'), ('UK', 'United_Kingdom')]},
]


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


# TheSportsDB's actual plan limit is 100 requests/MINUTE (confirmed from the owner's own pricing page,
# 2026-10-26). A fixed 0.3s gap between calls -- the original design -- allows up to ~200/min, 2x over,
# which is exactly what happened on the first real 28-league run: real 429s starting a few minutes in,
# cascading into every motorsport/tennis/golf/fighting league getting 0 events because the only retry
# logic that existed (in fetch_tsdb_tv_day) slept 2s and gave up, nowhere near enough for a per-minute
# window to clear, and fetch_tsdb_fixtures had no retry at all. This replaces both: a real sliding-
# window limiter (never more than TSDB_RATE_LIMIT calls in any trailing 60s, with margin under the real
# 100), plus a 429-aware retry that actually waits out the window instead of guessing at a fixed delay.
TSDB_RATE_LIMIT = 85   # margin under the real 100/min -- this run's own 429s are the evidence the margin needs to be real, not just a round number
_tsdb_call_times = []


def tsdb_rate_limit_wait():
    now = time.time()
    while _tsdb_call_times and now - _tsdb_call_times[0] > 60:
        _tsdb_call_times.pop(0)
    if len(_tsdb_call_times) >= TSDB_RATE_LIMIT:
        wait = 60 - (now - _tsdb_call_times[0]) + 0.5
        if wait > 0:
            print('Pacing for the TheSportsDB rate limit: waiting %.1fs' % wait, file=sys.stderr)
            time.sleep(wait)
    _tsdb_call_times.append(time.time())


def fetch_tsdb_json(url, max_retries=4):
    for attempt in range(max_retries):
        tsdb_rate_limit_wait()
        try:
            return fetch_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                retry_after = e.headers.get('Retry-After') if e.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 65
                print('429 from TheSportsDB -- waiting %.0fs before retrying (attempt %d/%d)' % (wait, attempt + 1, max_retries), file=sys.stderr)
                time.sleep(wait)
                continue
            raise


def parse_ts(raw_ts, raw_date='', raw_time=''):
    s = raw_ts or ((raw_date or '') + ' ' + (raw_time or '00:00:00'))
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def fetch_football_data_fixtures(league):
    if not FD_KEY:
        print('No FD_KEY set -- skipping %s fixtures.' % league['comp'], file=sys.stderr)
        return []
    url = 'https://api.football-data.org/v4/competitions/%s/matches?status=SCHEDULED,TIMED,IN_PLAY,PAUSED' % league['fd_comp']
    try:
        j = fetch_json(url, {'X-Auth-Token': FD_KEY})
    except Exception as e:
        print('Fixtures fetch failed for %s: %s' % (league['comp'], e), file=sys.stderr)
        return []
    horizon = datetime.now(timezone.utc) + timedelta(days=DAYS_AHEAD)
    out = []
    for m in j.get('matches', []):
        kick = datetime.fromisoformat(m['utcDate'].replace('Z', '+00:00'))
        if kick > horizon:
            continue
        out.append({'id': 'fd:%s:%s' % (league['fd_comp'], m['id']), 'comp': league['comp'], 'tsdb_id': None,
                    'home': m['homeTeam']['name'], 'away': m['awayTeam']['name'],
                    'label': m['homeTeam']['name'] + ' v ' + m['awayTeam']['name'],
                    'kick': m['utcDate'], 'kick_ts': kick.timestamp(), 'status': m['status']})
    return out


FIXTURES_MAX_AGE = 3 * 3600   # schedules/results don't change minute to minute either -- same reasoning as the TV-listing cache


def fetch_tsdb_fixtures(league, cache):
    if not TSDB_KEY:
        print('No TSDB_KEY set -- skipping %s fixtures.' % league['comp'], file=sys.stderr)
        return []
    out = []
    now = datetime.now(timezone.utc)
    for endpoint in ('eventsnextleague', 'eventspastleague'):
        cache_key = 'fixtures|%s|%s' % (league['comp'], endpoint)
        entry = cache.get(cache_key)
        if entry and time.time() - entry.get('fetched_at', 0) < FIXTURES_MAX_AGE:
            events = entry.get('events') or []
        else:
            url = 'https://www.thesportsdb.com/api/v1/json/%s/%s.php?id=%s' % (
                urllib.parse.quote(TSDB_KEY), endpoint, league['tsdb_league_id'])
            try:
                j = fetch_tsdb_json(url)
            except Exception as e:
                print('%s fetch failed for %s: %s' % (endpoint, league['comp'], e), file=sys.stderr)
                events = entry.get('events') if entry else []
            else:
                events = j.get('events') or []
                cache[cache_key] = {'fetched_at': time.time(), 'events': events}
        for e in events:
            ts = parse_ts(e.get('strTimestamp'), e.get('dateEvent'), e.get('strTime'))
            if ts is None or abs(ts - now.timestamp()) > DAYS_AHEAD * 864e2:
                continue
            # Team sports (NBA, NHL...) populate strHomeTeam/strAwayTeam; individual/card sports (Golf,
            # Tennis, UFC, Boxing -- confirmed by hand) leave both None and put everything into strEvent
            # instead ("Japan Open ... Alcaraz vs Lehecka", "UFC Fight Night 292 Moicano vs Nolan").
            # Matching for every TSDB-sourced fixture goes through idEvent anyway (see resolve_league),
            # which doesn't need home/away at all -- only the cross-provider fallback (football-data
            # fixtures against TheSportsDB listings) does, and that path never reaches these leagues.
            home, away = e.get('strHomeTeam'), e.get('strAwayTeam')
            if not e.get('idEvent') or not (home or away or e.get('strEvent')):
                continue
            out.append({'id': 'tsdb:%s' % e.get('idEvent'), 'comp': league['comp'], 'tsdb_id': e.get('idEvent'),
                        'home': home, 'away': away, 'label': e.get('strEvent') or ((home or '') + ' vs ' + (away or '')),
                        'kick': datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), 'kick_ts': ts,
                        'status': e.get('strStatus') or 'NS'})
    # the next/past endpoints can both return an event close to "now"; de-dupe by TheSportsDB's own id
    seen, dedup = set(), []
    for f in out:
        if f['tsdb_id'] in seen:
            continue
        seen.add(f['tsdb_id']); dedup.append(f)
    return dedup


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


# Tiered cache: the owner's TheSportsDB plan is ~100 calls/hour. Fetching every (league, country, day)
# combination on every run -- the original design -- was already ~148 calls/hour for Premier League
# ALONE at a 15-minute schedule, before any of the other leagues below existed. A day far in the future
# essentially never changes between one run and the next; only "today" and "tomorrow" are worth
# refreshing often. NEAR_MAX_AGE/FAR_MAX_AGE control that, and the cache is carried forward between runs
# the same way guide-v2.json itself is (see mirror_published) -- a GitHub Actions run has no disk of its
# own to remember anything in otherwise.
NEAR_MAX_AGE = 25 * 60         # today/tomorrow: refetch roughly every run (schedule is every 30 min) --
                                # kept responsive deliberately: a channel confirmed an hour before kickoff
                                # should actually show up before kickoff, not on the next slow tick.
FAR_MAX_AGE = 24 * 3600         # everything else: once a day (2026-10-26, after confirming the real
                                # TheSportsDB quota is 100/minute, not /hour -- there was no longer a
                                # quota reason to keep this as tight as the original 6h; a listing 5-10
                                # days out barely changes hour to hour anyway).


def fetch_tsdb_tv_day(day, country_param, sport, cache, cache_key, day_offset):
    if not TSDB_KEY:
        return []
    now = time.time()
    max_age = NEAR_MAX_AGE if day_offset in (0, 1) else FAR_MAX_AGE
    entry = cache.get(cache_key)
    if entry and now - entry.get('fetched_at', 0) < max_age:
        return entry.get('listings') or []   # cache hit -- no API call, no need to be polite about rate either
    url = 'https://www.thesportsdb.com/api/v1/json/%s/eventstv.php?%s' % (
        urllib.parse.quote(TSDB_KEY), urllib.parse.urlencode({'d': day, 'a': country_param, 's': sport}))
    try:
        j = fetch_tsdb_json(url)
        listings = j.get('tvevents') or []
        cache[cache_key] = {'fetched_at': now, 'listings': listings}
        return listings
    except Exception as e:
        print('TheSportsDB TV fetch failed for %s/%s %s: %s' % (sport, country_param, day, e), file=sys.stderr)
        # an expired-but-present cache entry beats nothing at all if today's fetch fails
        return entry.get('listings') if entry else []


def load_cache(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_cache(path, cache):
    # unbounded growth guard: a day that's now in the past never gets looked up again, so drop entries
    # older than a day past their own cache time rather than letting this file grow forever.
    cutoff = time.time() - 2 * 864e2
    pruned = {k: v for k, v in cache.items() if v.get('fetched_at', 0) > cutoff}
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(pruned, fh)


def classify_kind(listing_ts, kick_ts):
    """Inferred, never a fact from the source itself -- see module docstring. Mirrors the TV app's own
    fixtureEvents() time windows so the two don't silently disagree (one of the plan's principles: the
    label is a guess about the data, say so, and make the guess the same guess everywhere)."""
    delta = listing_ts - kick_ts
    if -3 * 3600 <= delta <= 3600:
        return 'LIVE'
    if delta > 3600:
        return 'REPLAY'
    return 'PREVIEW'


def resolve_league(league, cache):
    fixtures = fetch_football_data_fixtures(league) if league['fixtures'] == 'football-data' else fetch_tsdb_fixtures(league, cache)
    if not fixtures:
        return []

    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date()
    for f in fixtures:
        f['channels'] = []
    by_tsdb_id = {f['tsdb_id']: f for f in fixtures if f['tsdb_id']}

    days = sorted({datetime.fromtimestamp(f['kick_ts'], tz=timezone.utc).strftime('%Y-%m-%d') for f in fixtures}
                  | {(datetime.fromtimestamp(f['kick_ts'], tz=timezone.utc) + timedelta(days=d)).strftime('%Y-%m-%d')
                     for f in fixtures for d in (-1, 0, 1)})

    for cc, param in league['countries']:
        for day in days:
            day_offset = (datetime.strptime(day, '%Y-%m-%d').date() - today).days
            cache_key = '%s|%s|%s' % (league['comp'], cc, day)
            for t in fetch_tsdb_tv_day(day, param, league['tsdb_sport'], cache, cache_key, day_offset):
                listing_ts = parse_ts(t.get('strTimeStamp'), t.get('dateEvent'), t.get('strTime'))
                if listing_ts is None:
                    continue
                matched = []
                tid = t.get('idEvent')
                if tid and tid in by_tsdb_id:
                    matched = [by_tsdb_id[tid]]
                else:
                    ev_name = t.get('strEvent') or ''
                    matched = [f for f in fixtures if abs(listing_ts - f['kick_ts']) <= 20 * 3600
                               and teams_match(f['home'], f['away'], ev_name)]
                for f in matched:
                    f['channels'].append({
                        'channel': t.get('strChannel') or '', 'country': cc,
                        'kind': classify_kind(listing_ts, f['kick_ts']), 'inferred_kind': True,
                        'start': datetime.fromtimestamp(listing_ts, tz=timezone.utc).isoformat(),
                        'confidence': 'likely', 'source': 'thesportsdb', 'checked': now_iso,
                    })

    for f in fixtures:
        del f['kick_ts']
        f.pop('tsdb_id', None)
    return fixtures


def resolve(cache):
    now_iso = datetime.now(timezone.utc).isoformat()
    events = []
    for league in LEAGUES:
        got = resolve_league(league, cache)
        print('%s: %d fixtures resolved' % (league['comp'], len(got)), file=sys.stderr)
        events.extend(got)
    return {'generated': now_iso, 'competitions': [l['comp'] for l in LEAGUES], 'events': events}


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
    for name in ('index.html', 'type.html', 'sports.xml.gz', '.nojekyll', 'tsdb-cache.json'):
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

    cache_path = os.path.join(args.out, 'tsdb-cache.json')
    cache = load_cache(cache_path)
    data = resolve(cache)
    save_cache(cache_path, cache)

    out_path = os.path.join(args.out, 'guide-v2.json')
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    n_ev = len(data['events']); n_ch = sum(len(e.get('channels', [])) for e in data['events'])
    print('Wrote %s: %d events, %d channel listings' % (out_path, n_ev, n_ch))
