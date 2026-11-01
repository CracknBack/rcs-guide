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
import json, os, re, signal, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

FD_KEY = os.environ.get('FD_KEY', '').strip()
TSDB_KEY = os.environ.get('TSDB_KEY', '').strip()
DAYS_AHEAD = 10
UA = {'User-Agent': 'red-carpet-streams-guide-v2/1'}

# comp must match the TV app's COMPS display name exactly, so the app can line up events with the
# competition tab a user picked. countries: (short code the app understands, TheSportsDB's "a=" param).
# One shared list of countries, asked for EVERY competition below, instead of a separately hand-
# researched list per league -- that doesn't scale (explicitly rejected 2026-11-01: "I'm not going to
# go on Google and research every country for every league, that's madness", and it's correct). Built
# from the union of every country already proven to carry real TheSportsDB sports-TV data somewhere in
# this file's history (not a fresh guess), plus Singapore (independently verified 2026-11-01 for a real
# Premier League match -- StarHub TV+/Singtel TV). A country with nothing to say about a given
# competition just comes back empty, same as always -- no harm in asking broadly, and the tiered cache
# below means asking is cheap after the first pass. Adding a brand-new competition below needs zero
# country research now; it just inherits this list, and gets wider automatically if this list ever
# does. A 'countries' key on an individual league entry still overrides this, for the rare case a
# competition genuinely has nothing to do with most of these (kept to NHL/MLB/NASCAR below, where it
# would just waste the call budget on markets with no realistic chance of coverage).
DEFAULT_COUNTRIES = [
    ('UK', 'United_Kingdom'), ('IE', 'Ireland'), ('US', 'United_States'), ('CA', 'Canada'),
    ('AU', 'Australia'), ('NZ', 'New_Zealand'), ('ZA', 'South_Africa'), ('IN', 'India'),
    ('MY', 'Malaysia'), ('SG', 'Singapore'), ('DE', 'Germany'), ('AT', 'Austria'), ('CH', 'Switzerland'),
    ('FR', 'France'), ('IT', 'Italy'), ('NL', 'Netherlands'), ('PT', 'Portugal'), ('BE', 'Belgium'),
    ('GR', 'Greece'), ('TR', 'Turkey'), ('BR', 'Brazil'), ('AR', 'Argentina'),
]

LEAGUES = [
    {'comp': 'Premier League', 'fixtures': 'football-data', 'fd_comp': 'PL', 'tsdb_sport': 'Soccer'},

    # English domestic cups: not on football-data.org's free plan and previously left out of this list
    # entirely, so the app never had fixture or TV data for them at all (confirmed missing, not just
    # unmatched -- a real Fleetwood Town vs Arsenal EFL Cup tie, live-checked 2026-10-27, had a genuine
    # TheSportsDB TV listing, 'BeIn Sports 3 Australia', that this app simply never fetched). TheSportsDB
    # ids confirmed by hand the same day: FA Cup 4482, EFL Cup (Carabao Cup) 4570.
    {'comp': 'FA Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '4482', 'tsdb_sport': 'Soccer'},
    {'comp': 'League Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '4570', 'tsdb_sport': 'Soccer'},

    # Same gap, bigger: Champions League and Europa League were never in this list either -- same root
    # cause as the English cups above (never added, not a deliberate exclusion; the app's own COMPS list
    # has always offered both as choosable tabs with nothing behind them). Ids NOT independently verified
    # against the real TheSportsDB key the same way FA Cup/League Cup were (2026-10-28 audit: the public
    # test key only returns a crippled 5-league demo subset, useless for lookups) -- 4480/4481 are the
    # commonly documented TheSportsDB ids for these two, immediately adjacent to the already-confirmed
    # FA Cup (4482), which is itself reasonable corroborating evidence (TheSportsDB clusters English/UEFA
    # competition ids together) but is still not the same as a confirmed-by-hand check. If either comes
    # back with 0 fixtures across several real runs, the id is wrong -- check it against the real key
    # before assuming the competition itself has a genuine data gap.
    {'comp': 'Champions League', 'fixtures': 'tsdb', 'tsdb_league_id': '4480', 'tsdb_sport': 'Soccer'},
    {'comp': 'Europa League', 'fixtures': 'tsdb', 'tsdb_league_id': '4481', 'tsdb_sport': 'Soccer'},
    {'comp': 'NBA', 'fixtures': 'tsdb', 'tsdb_league_id': '4387', 'tsdb_sport': 'Basketball'},
    # Kept narrow on purpose, unlike everything else above/below: realistically nowhere outside North
    # America/the UK broadcasts NHL or MLB, so the full default list would just be wasted call budget.
    {'comp': 'NHL', 'fixtures': 'tsdb', 'tsdb_league_id': '4380', 'tsdb_sport': 'Ice Hockey',
     'countries': [('US', 'United_States'), ('CA', 'Canada')]},
    {'comp': 'MLB', 'fixtures': 'tsdb', 'tsdb_league_id': '4424', 'tsdb_sport': 'Baseball',
     'countries': [('US', 'United_States')]},
    {'comp': 'NFL', 'fixtures': 'tsdb', 'tsdb_league_id': '4391', 'tsdb_sport': 'American Football'},

    # European football: football-data.org's free plan covers these 6 (confirmed via this key's own
    # /v4/competitions listing on 2026-10-26) -- same source as Premier League, more detailed status
    # than TheSportsDB's generic strStatus. The rest aren't on that plan, so fixtures come from
    # TheSportsDB itself instead, same as NBA/NHL/MLB/NFL above.
    {'comp': 'La Liga', 'fixtures': 'football-data', 'fd_comp': 'PD', 'tsdb_sport': 'Soccer'},
    {'comp': 'Bundesliga', 'fixtures': 'football-data', 'fd_comp': 'BL1', 'tsdb_sport': 'Soccer'},
    {'comp': 'Ligue 1', 'fixtures': 'football-data', 'fd_comp': 'FL1', 'tsdb_sport': 'Soccer'},
    {'comp': 'Serie A', 'fixtures': 'football-data', 'fd_comp': 'SA', 'tsdb_sport': 'Soccer'},
    {'comp': 'Eredivisie', 'fixtures': 'football-data', 'fd_comp': 'DED', 'tsdb_sport': 'Soccer'},
    {'comp': 'Primeira Liga', 'fixtures': 'football-data', 'fd_comp': 'PPL', 'tsdb_sport': 'Soccer'},
    # Added 2026-10-31: confirmed via a live /v4/competitions call against the owner's own key that
    # these 5, plus the 6 above, are all on the free football-data.org plan right now (13 total).
    {'comp': 'Championship', 'fixtures': 'football-data', 'fd_comp': 'ELC', 'tsdb_sport': 'Soccer'},
    {'comp': 'Brazilian Serie A', 'fixtures': 'football-data', 'fd_comp': 'BSA', 'tsdb_sport': 'Soccer'},
    {'comp': 'Copa Libertadores', 'fixtures': 'football-data', 'fd_comp': 'CLI', 'tsdb_sport': 'Soccer'},
    {'comp': 'European Championship', 'fixtures': 'football-data', 'fd_comp': 'EC', 'tsdb_sport': 'Soccer'},
    {'comp': 'World Cup', 'fixtures': 'football-data', 'fd_comp': 'WC', 'tsdb_sport': 'Soccer'},
    {'comp': 'Belgian Pro League', 'fixtures': 'tsdb', 'tsdb_league_id': '4338', 'tsdb_sport': 'Soccer'},
    {'comp': 'Scottish Premiership', 'fixtures': 'tsdb', 'tsdb_league_id': '4330', 'tsdb_sport': 'Soccer'},
    {'comp': 'League of Ireland', 'fixtures': 'tsdb', 'tsdb_league_id': '4643', 'tsdb_sport': 'Soccer'},
    {'comp': 'Austrian Bundesliga', 'fixtures': 'tsdb', 'tsdb_league_id': '4621', 'tsdb_sport': 'Soccer'},
    {'comp': 'Greek Super League', 'fixtures': 'tsdb', 'tsdb_league_id': '4336', 'tsdb_sport': 'Soccer'},
    {'comp': 'Turkish Super Lig', 'fixtures': 'tsdb', 'tsdb_league_id': '4339', 'tsdb_sport': 'Soccer'},
    {'comp': 'Swiss Super League', 'fixtures': 'tsdb', 'tsdb_league_id': '4675', 'tsdb_sport': 'Soccer'},

    # A-League, AFL, NRL: all three already exist as choosable comps/sport-selector entries in the app
    # (COMPS, SPORT_GROUPS) but were never in this list, same gap as FA Cup/League Cup/Champions League/
    # Europa League above. Ids looked up directly against the real TheSportsDB catalog (2026-10-28, the
    # owner's own premium key -- the public test key only returns a crippled 5-league demo subset, no
    # use for this). AFL and NRL are both single, unambiguous leagues; no "which competition" judgement
    # call involved the way Rugby/Cricket below would need.
    {'comp': 'A-League', 'fixtures': 'tsdb', 'tsdb_league_id': '4356', 'tsdb_sport': 'Soccer'},
    {'comp': 'AFL', 'fixtures': 'tsdb', 'tsdb_league_id': '4456', 'tsdb_sport': 'Australian Football'},
    {'comp': 'NRL', 'fixtures': 'tsdb', 'tsdb_league_id': '4416', 'tsdb_sport': 'Rugby'},
    # Rugby and Cricket are NOT added here on purpose: both are a single generic comp in the app
    # (COMPS/SPORT_GROUPS just has one "Rugby" and one "Cricket", no finer split), but TheSportsDB has
    # dozens of real leagues under each -- Six Nations, United Rugby Championship, Rugby Championship,
    # Super Rugby for Rugby; international Test/ODI/T20I series, IPL, Big Bash, The Hundred for Cricket
    # -- with no single obviously-correct pick the way AFL/NRL/A-League each had exactly one real
    # answer. Needs the owner to say which competitions the app's one generic tab should actually mean
    # before guessing; multiple LEAGUES entries CAN share one 'comp' value if more than one is wanted.

    # Motorsport. No World Superbike (WSBK) entry exists in TheSportsDB's catalog (checked by hand,
    # 2026-10-26) -- only domestic "British Superbike Championship", a different, smaller series, so
    # it's deliberately left out rather than silently substituted for what was actually asked for.
    {'comp': 'Formula 1', 'fixtures': 'tsdb', 'tsdb_league_id': '4370', 'tsdb_sport': 'Motorsport'},
    {'comp': 'MotoGP', 'fixtures': 'tsdb', 'tsdb_league_id': '4407', 'tsdb_sport': 'Motorsport'},
    {'comp': 'IndyCar', 'fixtures': 'tsdb', 'tsdb_league_id': '4373', 'tsdb_sport': 'Motorsport',
     'countries': [('US', 'United_States'), ('UK', 'United_Kingdom')]},
    {'comp': 'NASCAR', 'fixtures': 'tsdb', 'tsdb_league_id': '4393', 'tsdb_sport': 'Motorsport',
     'countries': [('US', 'United_States')]},

    # Tennis, Golf, Fighting: individual/card sports -- TheSportsDB gives no strHomeTeam/strAwayTeam for
    # these (confirmed by hand), everything is in strEvent instead ("Alcaraz vs Lehecka", "UFC Fight
    # Night 292 Moicano vs Nolan"). fetch_tsdb_fixtures already handles that (see its own comment).
    # 2026-11-01: widening the country list here will NOT meaningfully raise these sports' channel
    # counts -- checked directly against the live resolved output the same day, TheSportsDB's own TV-
    # listing data is close to empty for almost every tennis/golf event and most motorsport sessions
    # outside the live moment itself (0 channels on all 15 ATP and all 14 WTA matches checked, 0 on 6/7
    # European Tour events, 19/21 MotoGP sessions), the same structural gap an earlier session already
    # flagged. Asking more countries about data that was never recorded in the first place just returns
    # more empty lists; this is a genuine source limitation, not something to keep tuning the query side
    # for. A real fix for these specific sports needs a different TV-listing source, not a wider country
    # list -- worth its own dedicated piece of work, not a quick patch here.
    {'comp': 'ATP Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4464', 'tsdb_sport': 'Tennis'},
    {'comp': 'WTA Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4517', 'tsdb_sport': 'Tennis'},
    {'comp': 'PGA Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4425', 'tsdb_sport': 'Golf'},
    {'comp': 'European Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4426', 'tsdb_sport': 'Golf'},
    {'comp': 'UFC', 'fixtures': 'tsdb', 'tsdb_league_id': '4443', 'tsdb_sport': 'Fighting'},
    {'comp': 'Boxing', 'fixtures': 'tsdb', 'tsdb_league_id': '4445', 'tsdb_sport': 'Fighting'},
]

# ESPN's public, unauthenticated Site v2 API (site.api.espn.com) -- confirmed by hand 2026-11-01 to
# return clean JSON with no Cloudflare/WAF gate at all, unlike nascar.com itself (hard-blocked even on a
# bare request with no spoofing attempted). It only ever reports US broadcasters -- checked both
# 'broadcasts' and 'geoBroadcasts' on a real event, the latter carries a 'region' field but it was 'us'
# every time, and came back empty for Eredivisie rather than naming a non-US region. So this only ever
# strengthens the US side of channels[]. Used for exactly the comps this file's own module docstring
# already flagged as having near-empty TheSportsDB TV data (PGA Tour, European Tour, NASCAR), plus
# Eredivisie, whose TV side is dead the same way despite having real football-data.org fixtures already.
#
# No AU/UK/NZ/CA equivalent: a standing-rights seed (brand names, no per-fixture data) used to fill
# that gap here, removed 2026-11-02. Tested live, not assumed: pulled the actual grabber configs from
# iptv-org/epg (the most credible maintained open-source project for exactly this) for foxsports.com.au
# and sky.com, hit their real API endpoints directly -- Fox Sports AU's tvguide API returns HTTP 200
# with an empty result (site itself 404s, the whole micro-site looks decommissioned), Sky UK's epgsky.com
# API 403s without private app auth headers. Combined with TheSportsDB's own eventstv.php (confirmed
# empty for these comps) and a separate dead third-party AU EPG aggregator checked the same session,
# that's four independent real sources confirmed dead or walled off for this specific data -- there is
# currently no working way to get real per-fixture AU/UK/NZ/CA channel numbers server-side. The real fix
# lives client-side instead (tv-app/index.html): scan the USER'S OWN already-loaded EPG -- the one place
# this data legitimately exists without scraping anything, since it's their own subscription -- and
# promote a real match straight to confirmed (see f1GuideChans, and its planned generalisation to every
# sport). A guessed brand name was worse than nothing: it claimed "likely" on zero real signal for a
# specific fixture, for a problem the client-side fix actually solves with real data when it exists.
ESPN_SOURCES = [
    # 'tournament': one multi-day event live at a time (golf) -- the DEFAULT scoreboard call (no
    # ?dates= at all) already returns the current/nearest tournament with its own startDate/endDate
    # spanning every round, confirmed by hand against the real endpoint, so this needs exactly one call,
    # not a day-by-day loop, and the match window is the whole tournament, not a single kickoff instant
    # (a Thursday-only timestamp would miss the Friday/Saturday/Sunday rounds entirely).
    {'comp': 'PGA Tour', 'espn_path': 'golf/pga', 'kind': 'tournament'},
    {'comp': 'European Tour', 'espn_path': 'golf/eur', 'kind': 'tournament'},
    # 'daily': one event a day, not a standing multi-day event (NASCAR is a single race) -- the default
    # call only returns today's slate, and a dashed date RANGE (?dates=YYYYMMDD-YYYYMMDD) is REJECTED
    # with a 400 on these scoreboards (confirmed by hand) -- the form that actually works is one plain
    # ?dates=YYYYMMDD call per day, so this loops that instead.
    {'comp': 'NASCAR', 'espn_path': 'racing/nascar-premier', 'kind': 'daily'},
    # 'teams': Eredivisie already has real fixtures from football-data.org (see LEAGUES) -- this exists
    # only to borrow ESPN's broadcasts[] for the US side, matched to one specific fixture by team name
    # (same day loop as 'daily' above, just matched per-game instead of applied to the whole day).
    {'comp': 'Eredivisie', 'espn_path': 'soccer/ned.1', 'kind': 'teams'},
]
# No MotoGP entry: ESPN's site API doesn't carry it at all (checked by hand -- no motogp/motorsport path
# exists under /apis/site/v2/sports/racing/). Its own official site (api.pulselive.motogp.com) confirms
# real fixtures but has no broadcaster endpoint either (checked by hand, several guessed paths all 400).
# MotoGP gets no server-side channel source at all now -- see the comment above ESPN_SOURCES for why
# (the standing-rights seed that used to fill this in was removed 2026-11-02).


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


#  football-data.org's free plan is 10 calls/minute (confirmed from the owner's own account). Going
# from the original 6 football-data leagues to 13 (2026-10-31) means this loop alone is 13 calls a
# run -- over the limit if fired back-to-back with no pacing, which this function previously did not
# do at all (there was no need to, at 6 calls). Same sliding-window approach as
# tsdb_rate_limit_wait, just with a lower, margin-under-10 limit, plus an actual 429 retry instead of
# silently returning no fixtures for whichever league loses the race.
FD_RATE_LIMIT = 8
_fd_call_times = []


def fd_rate_limit_wait():
    now = time.time()
    while _fd_call_times and now - _fd_call_times[0] > 60:
        _fd_call_times.pop(0)
    if len(_fd_call_times) >= FD_RATE_LIMIT:
        wait = 60 - (now - _fd_call_times[0]) + 0.5
        if wait > 0:
            print('Pacing for the football-data.org rate limit: waiting %.1fs' % wait, file=sys.stderr)
            time.sleep(wait)
    _fd_call_times.append(time.time())


def fetch_football_data_fixtures(league, max_retries=3):
    if not FD_KEY:
        print('No FD_KEY set -- skipping %s fixtures.' % league['comp'], file=sys.stderr)
        return []
    url = 'https://api.football-data.org/v4/competitions/%s/matches?status=SCHEDULED,TIMED,IN_PLAY,PAUSED' % league['fd_comp']
    j = None
    for attempt in range(max_retries):
        fd_rate_limit_wait()
        try:
            j = fetch_json(url, {'X-Auth-Token': FD_KEY})
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                retry_after = e.headers.get('Retry-After') if e.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 65
                print('429 from football-data.org -- waiting %.0fs before retrying %s (attempt %d/%d)' %
                      (wait, league['comp'], attempt + 1, max_retries), file=sys.stderr)
                time.sleep(wait)
                continue
            print('Fixtures fetch failed for %s: %s' % (league['comp'], e), file=sys.stderr)
            return []
        except Exception as e:
            print('Fixtures fetch failed for %s: %s' % (league['comp'], e), file=sys.stderr)
            return []
    if j is None:
        print('Fixtures fetch exhausted retries for %s.' % league['comp'], file=sys.stderr)
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


# Tiered cache: confirmed via TheSportsDB's own published tier docs (2026-11-01) that the Premium
# (Patreon) plan this key is on is genuinely 100 calls/MINUTE, not /hour -- an earlier pass here got that
# wrong by a factor of 60 and throttled the whole schedule (and guide-v2.yml's cron) off a budget that
# was never real. tsdb_rate_limit_wait()'s sliding window already paces every call safely against the
# true 100/min limit regardless of how often this job is triggered; the cache below exists to avoid
# WASTING calls on data that hasn't changed, not to dodge a quota that was being miscalculated. A day far
# in the future essentially never changes between one run and the next; only "today" and "tomorrow" are
# worth refreshing often. NEAR_MAX_AGE/FAR_MAX_AGE control that, and the cache is carried forward between
# runs the same way guide-v2.json itself is (see mirror_published) -- a GitHub Actions run has no disk of
# its own to remember anything in otherwise.
NEAR_MAX_AGE = 13 * 60          # today/tomorrow: refetch roughly every run (schedule is every 15 min, see
                                 # guide-v2.yml) -- kept responsive deliberately: a channel confirmed an
                                 # hour before kickoff should actually show up before kickoff, not on the
                                 # next tick.
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


def fetch_espn_json(url, max_retries=2):
    for attempt in range(max_retries):
        try:
            return fetch_json(url)
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            raise


def fetch_espn_cached(url, cache, cache_key, day_offset):
    # Same tiered-cache shape as fetch_tsdb_tv_day (NEAR_MAX_AGE/FAR_MAX_AGE), carried forward between
    # runs the same way -- no reason to hit ESPN fresh every 15-minute guide-v2 tick for a schedule that
    # barely changes hour to hour. No sliding-window rate limiter here unlike TheSportsDB/football-data.org
    # above -- those were built in response to REAL observed 429s (see their own comments); nothing here
    # has ever actually 429'd, and this file's own principle is to pace against a real, confirmed limit,
    # not an invented one.
    now = time.time()
    max_age = NEAR_MAX_AGE if day_offset in (0, 1) else FAR_MAX_AGE
    entry = cache.get(cache_key)
    if entry and now - entry.get('fetched_at', 0) < max_age:
        return entry.get('data')
    try:
        j = fetch_espn_json(url)
        cache[cache_key] = {'fetched_at': now, 'data': j}
        return j
    except Exception as e:
        print('ESPN fetch failed for %s: %s' % (url, e), file=sys.stderr)
        return entry.get('data') if entry else None


def parse_espn_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def espn_broadcast_names(competition):
    names = []
    for b in competition.get('broadcasts') or []:
        names.extend(b.get('names') or [])
    return names


def espn_channel_row(name, kick_ts, now_iso):
    return {'channel': name, 'country': 'US', 'kind': 'LIVE', 'inferred_kind': True,
            'start': datetime.fromtimestamp(kick_ts, tz=timezone.utc).isoformat(),
            'confidence': 'likely', 'source': 'espn', 'checked': now_iso}


def espn_channels_tournament(src, fixtures, cache, now_iso):
    url = 'https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard' % src['espn_path']
    j = fetch_espn_cached(url, cache, 'espn|%s|current' % src['comp'], 0)
    if not j:
        return
    for ev in j.get('events') or []:
        comp_obj = (ev.get('competitions') or [{}])[0]
        names = espn_broadcast_names(comp_obj)
        if not names:
            continue
        start = parse_espn_ts(comp_obj.get('startDate') or ev.get('date'))
        if start is None:
            continue
        end = parse_espn_ts(comp_obj.get('endDate')) or (start + 4 * 864e2)
        lo, hi = start - 12 * 3600, end + 12 * 3600
        for f in fixtures:
            if f['comp'] != src['comp'] or not (lo <= f['kick_ts'] <= hi):
                continue
            for name in names:
                f['channels'].append(espn_channel_row(name, f['kick_ts'], now_iso))


def espn_channels_daily(src, fixtures, cache, now_iso):
    today = datetime.now(timezone.utc).date()
    days = sorted({datetime.fromtimestamp(f['kick_ts'], tz=timezone.utc).strftime('%Y%m%d')
                   for f in fixtures if f['comp'] == src['comp']})
    for day in days:
        day_offset = (datetime.strptime(day, '%Y%m%d').date() - today).days
        url = 'https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard?dates=%s' % (src['espn_path'], day)
        j = fetch_espn_cached(url, cache, 'espn|%s|%s' % (src['comp'], day), day_offset)
        if not j:
            continue
        events = j.get('events') or []
        if src['kind'] == 'teams':
            for ev in events:
                comp_obj = (ev.get('competitions') or [{}])[0]
                names = espn_broadcast_names(comp_obj)
                if not names:
                    continue
                teams = [c.get('team', {}).get('displayName') or c.get('team', {}).get('name')
                         for c in (comp_obj.get('competitors') or [])]
                teams = [t for t in teams if t]
                if len(teams) < 2:
                    continue
                start = parse_espn_ts(comp_obj.get('date') or ev.get('date'))
                if start is None:
                    continue
                for f in fixtures:
                    if f['comp'] != src['comp'] or abs(f['kick_ts'] - start) > 6 * 3600:
                        continue
                    if not teams_match(f['home'], f['away'], teams[0] + ' ' + teams[1]):
                        continue
                    for name in names:
                        f['channels'].append(espn_channel_row(name, f['kick_ts'], now_iso))
        else:
            # 'daily' individual event (NASCAR): exactly one race -- if ESPN somehow lists more than one
            # for the same day, that's ambiguous and this skips it rather than guessing which one a
            # given fixture is.
            if len(events) != 1:
                continue
            comp_obj = (events[0].get('competitions') or [{}])[0]
            names = espn_broadcast_names(comp_obj)
            if not names:
                continue
            for f in fixtures:
                if f['comp'] != src['comp']:
                    continue
                if datetime.fromtimestamp(f['kick_ts'], tz=timezone.utc).strftime('%Y%m%d') != day:
                    continue
                for name in names:
                    f['channels'].append(espn_channel_row(name, f['kick_ts'], now_iso))


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

    for cc, param in league.get('countries') or DEFAULT_COUNTRIES:
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

    espn_src = next((s for s in ESPN_SOURCES if s['comp'] == league['comp']), None)
    if espn_src:
        if espn_src['kind'] == 'tournament':
            espn_channels_tournament(espn_src, fixtures, cache, now_iso)
        else:
            espn_channels_daily(espn_src, fixtures, cache, now_iso)

    for f in fixtures:
        del f['kick_ts']
        f.pop('tsdb_id', None)
    return fixtures


class Interrupted(Exception):
    """Raised from a SIGTERM handler so the resolve() loop below can unwind through its own
    try/finally instead of the process just vanishing mid-league the way the default SIGTERM
    disposition would -- GitHub Actions sends exactly this signal when timeout-minutes is hit."""


def _raise_interrupted(signum, frame):
    raise Interrupted()


def _slug(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())


# Friendly CLI names that don't slug-match their real comp string closely enough on their own
# (European Tour is branded DP World Tour on air now; everything else below already substring-
# matches via _slug, e.g. "pga" is already inside "pgatour").
_ONLY_ALIASES = {'dpwt': 'European Tour', 'dpworldtour': 'European Tour'}


def leagues_matching(only):
    """only: a list of CLI slugs (e.g. ['nascar', 'motogp', 'pga', 'dpwt', 'eredivisie']), or falsy
    for every league. Matches loosely (substring either direction on the slugged comp name) since
    this is a local debugging convenience, not the real selection logic anything ships behind."""
    if not only:
        return LEAGUES
    wanted = [w.strip().lower() for w in only if w.strip()]
    out = []
    for league in LEAGUES:
        slug = _slug(league['comp'])
        for w in wanted:
            if _ONLY_ALIASES.get(w) == league['comp'] or (_slug(w) and (_slug(w) in slug or slug in _slug(w))):
                out.append(league)
                break
    return out


def resolve(cache, only=None, checkpoint=None, deadline=None):
    """checkpoint: called after every league resolves, win or lose, so a run that DOES finish (see
    deadline below) keeps every API call it already paid TheSportsDB's rate limit for even if an
    individual league fails -- previously save_cache() only ran once resolve() returned.

    deadline: a time.time() value. The loop below stops starting NEW leagues once it's past this,
    and returns normally (exit 0) with whatever it has. This matters more than the checkpointing or
    the SIGTERM handler: guide-v2.yml's timeout-minutes is a JOB-level timeout, which CANCELS THE
    WHOLE JOB outright when it fires -- it does not behave like a failed step that a later
    `if: always()` step could react to, and it does not give this process a chance to flush anything
    to disk either. Concretely: the only way tsdb-cache.json/guide-v2.json ever reach the live site
    is checkout -> resolve_v2.py -> configure-pages -> upload-pages-artifact -> a SEPARATE deploy
    job. If the job timeout kills resolve_v2.py mid-run, upload-pages-artifact never runs, nothing
    this run checkpointed to LOCAL disk is ever published, and the next run pulls the exact same
    stale cache back down via mirror_published() -- the per-league checkpointing below would do
    nothing in that scenario. Exiting on our own, safely under the job's ceiling, is what actually
    lets a partial run's progress reach the next one. The SIGTERM handler stays as a second line of
    defence (a manual Ctrl-C locally, or a timeout that fires 1 second early for some reason), not
    the primary mechanism."""
    now_iso = datetime.now(timezone.utc).isoformat()
    events, resolved = [], []
    leagues = leagues_matching(only)
    old_handler = signal.signal(signal.SIGTERM, _raise_interrupted)
    try:
        for league in leagues:
            if deadline and time.time() > deadline:
                print('Soft deadline reached -- stopping before %s so the publish steps still run.' % league['comp'], file=sys.stderr)
                break
            try:
                got = resolve_league(league, cache)
            except Interrupted:
                print('Interrupted during %s -- keeping what already resolved.' % league['comp'], file=sys.stderr)
                break
            print('%s: %d fixtures resolved' % (league['comp'], len(got)), file=sys.stderr)
            events.extend(got)
            resolved.append(league['comp'])
            if checkpoint:
                checkpoint()
    finally:
        signal.signal(signal.SIGTERM, old_handler)
    # 'resolved' (as opposed to 'competitions', every comp this call was ASKED to cover) is what lets
    # a caller tell a genuinely finished run apart from one the deadline/an interrupt cut short -- see
    # merge_unresolved() below, which carries the previous guide-v2.json forward for exactly the comps
    # this run didn't get to, instead of a partial run silently wiping them off the live site.
    return {'generated': now_iso, 'competitions': [l['comp'] for l in leagues], 'resolved': resolved, 'events': events}


def fetch_prev_guide_v2(base):
    try:
        return fetch_json(base.rstrip('/') + '/guide-v2.json')
    except Exception as e:
        print('Could not fetch the previously-published guide-v2.json (%s) -- a partial run will not '
              'be able to carry forward any competitions it didn\'t reach.' % e, file=sys.stderr)
        return None


def merge_unresolved(data, prev):
    """actions/deploy-pages replaces the WHOLE site on every deploy (see the comment above
    mirror_published) -- so a run that stops early on --deadline-minutes or an interrupt and simply
    writes what it has would silently erase every OTHER competition from the live guide-v2.json until
    some future run happens to get far enough to re-resolve them. Carries the previous run's events
    forward for exactly the competitions this run didn't reach, never for ones it did (a fresh
    'likely' beats a stale one)."""
    missing = set(data['competitions']) - set(data['resolved'])
    if not missing or not prev:
        return data
    carried = [e for e in prev.get('events', []) if e.get('comp') in missing]
    print('Carrying forward %d event(s) from the previous guide for %d competition(s) this run '
          'didn\'t reach: %s' % (len(carried), len(missing), ', '.join(sorted(missing))), file=sys.stderr)
    data['events'] = data['events'] + carried
    return data


# guide-v2 runs on its own, fast, frequent schedule (unlike the ~20min XMLTV build), and
# actions/deploy-pages replaces the WHOLE site on every deploy -- so a bare "write guide-v2.json and
# deploy" would wipe out the existing sports.xml.gz/status.json/index.html between runs of the main
# guide.yml job. Mirror whatever's actually live first (same status.json-driven site list cmd_fetch_prev
# in build_guide.py already uses, extended to the files that one doesn't fetch), then add guide-v2.json
# into that same directory, so every deploy carries the full site forward. type.html is the one
# exception -- it's a static file checked into this repo, not a build_guide.py output, so it's copied
# straight off disk instead of mirrored from live (see below).
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
    for name in ('index.html', 'sports.xml.gz', '.nojekyll', 'tsdb-cache.json'):
        get('%s/%s' % (base, name), os.path.join(dest, name))
    # type.html is a plain static file checked into THIS repo (unlike index.html/sports.xml.gz, which
    # are build_guide.py outputs that only exist on the live site) -- mirroring it from "whatever's
    # currently live" meant every edit to it needed a full guide.yml run (~20-35 min) to ever actually
    # publish, even though the real content was sitting right here on disk the whole time. Copying the
    # repo's own copy means any run of this script -- including the fast guide-v2 one, which runs every
    # 30 minutes on its own schedule -- carries the latest committed version forward automatically.
    local_type = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'type.html')
    if os.path.exists(local_type):
        with open(local_type, 'rb') as fh:
            data = fh.read()
        with open(os.path.join(dest, 'type.html'), 'wb') as fh:
            fh.write(data)
    else:
        get(base + '/type.html', os.path.join(dest, 'type.html'))
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
    ap.add_argument('--only', default='', help='comma-separated competition slugs to resolve, e.g. '
                     '"nascar,motogp,pga,dpwt,eredivisie" -- for quick local checks, never used in CI')
    # Must stay comfortably under guide-v2.yml's job-level timeout-minutes (currently 45) -- that
    # timeout cancels the whole job outright when it fires, with no chance for this process or any
    # later step to run, so the only way a slow/throttled run's partial progress ever gets published
    # is to exit cleanly on our own first. The workflow passes its own timeout-minutes in here (minus
    # a margin) explicitly, specifically so the two numbers can't silently drift apart again.
    ap.add_argument('--deadline-minutes', type=float, default=38.0,
                     help='stop starting new leagues after this many minutes and exit cleanly')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    if args.mirror_base:
        mirror_published(args.mirror_base, args.out)

    cache_path = os.path.join(args.out, 'tsdb-cache.json')
    cache = load_cache(cache_path)
    only = [s for s in args.only.split(',') if s.strip()] if args.only else None
    deadline = time.time() + args.deadline_minutes * 60
    data = resolve(cache, only=only, checkpoint=lambda: save_cache(cache_path, cache), deadline=deadline)
    save_cache(cache_path, cache)

    if set(data['resolved']) != set(data['competitions']) and args.mirror_base:
        data = merge_unresolved(data, fetch_prev_guide_v2(args.mirror_base))

    out_path = os.path.join(args.out, 'guide-v2.json')
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    n_ev = len(data['events']); n_ch = sum(len(e.get('channels', [])) for e in data['events'])
    print('Wrote %s: %d events, %d channel listings' % (out_path, n_ev, n_ch))
