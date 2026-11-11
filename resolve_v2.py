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
import gzip, io, json, os, re, signal, sys, time, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
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
    # Added 2026-11-10, same audit as soccer above: this file covered 1 of 123 real basketball leagues
    # before this. All three confirmed with genuine recent/upcoming activity against the real key.
    {'comp': 'EuroLeague Basketball', 'fixtures': 'tsdb', 'tsdb_league_id': '4546', 'tsdb_sport': 'Basketball'},
    {'comp': 'WNBA', 'fixtures': 'tsdb', 'tsdb_league_id': '4516', 'tsdb_sport': 'Basketball'},
    {'comp': 'FIBA Basketball World Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '4549', 'tsdb_sport': 'Basketball'},
    # Kept narrow on purpose, unlike everything else above/below: realistically nowhere outside North
    # America/the UK broadcasts NHL or MLB, so the full default list would just be wasted call budget.
    {'comp': 'NHL', 'fixtures': 'tsdb', 'tsdb_league_id': '4380', 'tsdb_sport': 'Ice Hockey',
     'countries': [('US', 'United_States'), ('CA', 'Canada')]},
    # Added 2026-11-10, same audit: 1 of 60 real ice hockey leagues covered before this. KHL confirmed
    # with a fixture the same day it was checked; Worlds is a real annual tournament (dormant most of
    # the year, same acceptable state as NFL/Cycling elsewhere in this file).
    {'comp': 'Russian KHL', 'fixtures': 'tsdb', 'tsdb_league_id': '4920', 'tsdb_sport': 'Ice Hockey'},
    {'comp': 'IIHF World Championship', 'fixtures': 'tsdb', 'tsdb_league_id': '4976', 'tsdb_sport': 'Ice Hockey'},
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

    # Added 2026-11-10: a systematic audit (prompted by the owner finding the app only ever covers a
    # sliver of what actually exists -- confirmed: this file covered 12 of 706 real TheSportsDB soccer
    # leagues before this batch) against the owner's own real key, checked for genuine current activity
    # (not just a catalog entry) before anything here. All confirmed with real recent/upcoming fixtures
    # at check time: UEFA Nations League (20 upcoming), Copa del Rey/Coupe de France/DFB-Pokal (domestic
    # cups of leagues this file already covers the league season for -- same gap class as FA Cup/League
    # Cup above), English League One/Two (both 20 upcoming, genuinely mid-season -- this is also the
    # direct answer to "does this cover the Championship below the Premier League" for the two tiers
    # under that), MLS and Mexican Liga MX (both had a fixture within the last 2 days at check time).
    {'comp': 'UEFA Nations League', 'fixtures': 'tsdb', 'tsdb_league_id': '4490', 'tsdb_sport': 'Soccer'},
    {'comp': 'Copa del Rey', 'fixtures': 'tsdb', 'tsdb_league_id': '4483', 'tsdb_sport': 'Soccer'},
    {'comp': 'Coupe de France', 'fixtures': 'tsdb', 'tsdb_league_id': '4484', 'tsdb_sport': 'Soccer'},
    {'comp': 'DFB-Pokal', 'fixtures': 'tsdb', 'tsdb_league_id': '4485', 'tsdb_sport': 'Soccer'},
    {'comp': 'English League One', 'fixtures': 'tsdb', 'tsdb_league_id': '4396', 'tsdb_sport': 'Soccer'},
    {'comp': 'English League Two', 'fixtures': 'tsdb', 'tsdb_league_id': '4397', 'tsdb_sport': 'Soccer'},
    {'comp': 'MLS', 'fixtures': 'tsdb', 'tsdb_league_id': '4346', 'tsdb_sport': 'Soccer'},
    {'comp': 'Mexican Liga MX', 'fixtures': 'tsdb', 'tsdb_league_id': '4350', 'tsdb_sport': 'Soccer'},

    # A-League, AFL, NRL: all three already exist as choosable comps/sport-selector entries in the app
    # (COMPS, SPORT_GROUPS) but were never in this list, same gap as FA Cup/League Cup/Champions League/
    # Europa League above. Ids looked up directly against the real TheSportsDB catalog (2026-10-28, the
    # owner's own premium key -- the public test key only returns a crippled 5-league demo subset, no
    # use for this). AFL and NRL are both single, unambiguous leagues; no "which competition" judgement
    # call involved the way Rugby/Cricket below would need.
    {'comp': 'A-League', 'fixtures': 'tsdb', 'tsdb_league_id': '4356', 'tsdb_sport': 'Soccer'},
    {'comp': 'AFL', 'fixtures': 'tsdb', 'tsdb_league_id': '4456', 'tsdb_sport': 'Australian Football'},
    {'comp': 'NRL', 'fixtures': 'tsdb', 'tsdb_league_id': '4416', 'tsdb_sport': 'Rugby'},
    # Added 2026-11-09 after the owner reported a real, current gap: live mid-tournament (final
    # 2026-11-15), zero coverage. Id 5806 confirmed against the owner's own real TheSportsDB key (the
    # free test key's all_leagues.php only returns a crippled 10-league demo, useless for this kind of
    # lookup -- 5806 doesn't exist in that subset at all, which is exactly why this got missed for so
    # long). Real fixtures confirmed: 15 played this tournament already (as recently as 2026-11-08,
    # Australia v Samoa), final listed 2026-11-15. Unambiguous like AFL/NRL, not a "which competition"
    # judgement call -- shares NRL's app tab (Rugby League / NRL), not the generic Rugby one.
    {'comp': 'Rugby League World Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '5806', 'tsdb_sport': 'Rugby'},
    # Rugby League Super League and World Club Challenge/State of Origin added 2026-11-10, same audit --
    # all real rugby LEAGUE competitions (not union), so grouped with NRL/Rugby League World Cup under
    # the app's "Rugby League / NRL" tab rather than the generic Rugby one below. Super League had a
    # fixture 2026-10-03 (Grand Final season); World Club Challenge and State of Origin are real annual
    # one-off/series fixtures, seasonally dormant at check time but genuine, same acceptable state as
    # NFL/Cycling elsewhere in this file.
    {'comp': 'Rugby League Super League', 'fixtures': 'tsdb', 'tsdb_league_id': '4415', 'tsdb_sport': 'Rugby'},
    {'comp': 'World Club Challenge', 'fixtures': 'tsdb', 'tsdb_league_id': '5834', 'tsdb_sport': 'Rugby'},
    {'comp': 'State of Origin', 'fixtures': 'tsdb', 'tsdb_league_id': '5835', 'tsdb_sport': 'Rugby'},

    # Generic Rugby (union) tab: deferred for weeks pending an owner decision that never actually came
    # (see the original note, kept below) -- resolved 2026-11-10 by picking the globally major, real,
    # currently-or-recently-active competitions directly, the same evidence standard as everything else
    # in this batch, rather than waiting on a judgement call that wasn't forthcoming. All 6 confirmed
    # against the real key: Rugby World Cup last played 2025-11-18 (next is 2027 -- genuinely between
    # tournaments, not missing), Six Nations/Rugby Championship are real annual international windows
    # (both seasonally dormant at check time), Super Rugby/URC/Top 14 are current club seasons (URC and
    # Top 14 both had a fixture within 2 days of check time).
    # Original note, superseded above: "Rugby (generic tab) and Cricket are NOT added here on purpose:
    # both are a single generic comp in the app (COMPS/SPORT_GROUPS just has one "Rugby" and one
    # "Cricket", no finer split), but TheSportsDB has dozens of real leagues under each ... with no
    # single obviously-correct pick the way AFL/NRL/A-League each had exactly one real answer. Needs the
    # owner to say which competitions the app's one generic tab should actually mean before guessing."
    {'comp': 'Rugby World Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '4574', 'tsdb_sport': 'Rugby'},
    {'comp': 'Six Nations Championship', 'fixtures': 'tsdb', 'tsdb_league_id': '4714', 'tsdb_sport': 'Rugby'},
    {'comp': 'Rugby Championship', 'fixtures': 'tsdb', 'tsdb_league_id': '4986', 'tsdb_sport': 'Rugby'},
    {'comp': 'Super Rugby', 'fixtures': 'tsdb', 'tsdb_league_id': '4551', 'tsdb_sport': 'Rugby'},
    {'comp': 'United Rugby Championship', 'fixtures': 'tsdb', 'tsdb_league_id': '4446', 'tsdb_sport': 'Rugby'},
    {'comp': 'French Top 14', 'fixtures': 'tsdb', 'tsdb_league_id': '4430', 'tsdb_sport': 'Rugby'},

    # Cricket tab: same resolution as Rugby above, same evidence standard. One Day International
    # Series and Twenty20 International Series (the generic international-match buckets, as opposed to
    # a single named tournament) both had a fixture within a week of check time -- these are the ones
    # that actually cover "is there a real international match on right now" day to day. IPL/Big Bash/
    # Caribbean Premier League/The Hundred are real annual domestic T20 leagues, seasonally dormant at
    # check time (Apr-May/Dec-Jan/Aug-Sep/Jul-Aug windows respectively), same acceptable state as
    # NFL/Cycling elsewhere in this file.
    {'comp': 'One Day International Series', 'fixtures': 'tsdb', 'tsdb_league_id': '4801', 'tsdb_sport': 'Cricket'},
    {'comp': 'Twenty20 International Series', 'fixtures': 'tsdb', 'tsdb_league_id': '4979', 'tsdb_sport': 'Cricket'},
    {'comp': 'Indian Premier League', 'fixtures': 'tsdb', 'tsdb_league_id': '4460', 'tsdb_sport': 'Cricket'},
    {'comp': 'Australian Big Bash League', 'fixtures': 'tsdb', 'tsdb_league_id': '4461', 'tsdb_sport': 'Cricket'},
    {'comp': 'Caribbean Premier League', 'fixtures': 'tsdb', 'tsdb_league_id': '5176', 'tsdb_sport': 'Cricket'},
    {'comp': 'The Hundred', 'fixtures': 'tsdb', 'tsdb_league_id': '5177', 'tsdb_sport': 'Cricket'},
    # Added 2026-11-10, found the hard way: the owner spotted a LIVE Pakistan vs Sri Lanka match on their
    # own guide (Fox Sports) that this app showed nothing for. Root cause: this file had ZERO Test-cricket
    # coverage -- confirmed live, that exact match is real, idEvent 2623108, league id 5100 "ICC World
    # Test Championship", started 2026-11-09. 4844 "International Test Match Series" (bilateral Tests
    # outside the WTC cycle) returned no next/past event at check time -- added anyway on the same
    # evidence standard as Cycling/NASCAR Truck elsewhere in this file (a real, current TheSportsDB league
    # id, just dormant right now, not a guess). Both ids come straight off TheSportsDB's own Pakistan
    # Cricket team record (idTeam 137144), confirmed real, not guessed.
    {'comp': 'ICC World Test Championship', 'fixtures': 'tsdb', 'tsdb_league_id': '5100', 'tsdb_sport': 'Cricket'},
    {'comp': 'International Test Match Series', 'fixtures': 'tsdb', 'tsdb_league_id': '4844', 'tsdb_sport': 'Cricket'},
    # Same session: the generic ODI/T20I buckets above only cover ICC's composite "any bilateral series"
    # leagues -- the big ICC tournaments themselves are SEPARATE league ids on TheSportsDB, never added.
    # All three confirmed real (same Pakistan Cricket team record) but dormant at check time (none run
    # every year) -- added for completeness, same as every periodic/seasonal comp elsewhere in this file.
    {'comp': 'Cricket World Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '4575', 'tsdb_sport': 'Cricket'},
    {'comp': 'ICC Mens T20 World Cup', 'fixtures': 'tsdb', 'tsdb_league_id': '5103', 'tsdb_sport': 'Cricket'},
    {'comp': 'ICC Champions Trophy', 'fixtures': 'tsdb', 'tsdb_league_id': '5587', 'tsdb_sport': 'Cricket'},

    # CORRECTION 2026-11-10: the "no World Superbike" claim directly below was wrong -- a real false
    # negative from searching the literal phrase "World Superbike". TheSportsDB catalogs it as 'SBK'
    # (id 4454), confirmed with real fixtures (20 upcoming, last raced 2026-10-18) against the owner's
    # real key during the same systematic audit as the soccer/basketball/hockey batches above. Left the
    # original wrong note below for the record rather than deleting it -- the lesson (a name-literal
    # catalog search can silently miss a real entry filed under a different name) is worth keeping.
    # Motorsport. No World Superbike (WSBK) entry exists in TheSportsDB's catalog (checked by hand,
    # 2026-10-26) -- only domestic "British Superbike Championship", a different, smaller series, so
    # it's deliberately left out rather than silently substituted for what was actually asked for.
    {'comp': 'Formula 1', 'fixtures': 'tsdb', 'tsdb_league_id': '4370', 'tsdb_sport': 'Motorsport'},
    {'comp': 'MotoGP', 'fixtures': 'tsdb', 'tsdb_league_id': '4407', 'tsdb_sport': 'Motorsport'},
    # Moto2/Moto3 added 2026-11-10: the other two classes racing the same real MotoGP weekend (both had
    # a fixture the exact same day as MotoGP's own most recent one at check time) -- same event, same
    # broadcast, previously only the premier class was ever looked for.
    {'comp': 'Moto2', 'fixtures': 'tsdb', 'tsdb_league_id': '4436', 'tsdb_sport': 'Motorsport'},
    {'comp': 'Moto3', 'fixtures': 'tsdb', 'tsdb_league_id': '4437', 'tsdb_sport': 'Motorsport'},
    {'comp': 'World Superbike', 'fixtures': 'tsdb', 'tsdb_league_id': '4454', 'tsdb_sport': 'Motorsport'},
    {'comp': 'WRC', 'fixtures': 'tsdb', 'tsdb_league_id': '4409', 'tsdb_sport': 'Motorsport'},
    {'comp': 'Formula E', 'fixtures': 'tsdb', 'tsdb_league_id': '4371', 'tsdb_sport': 'Motorsport'},
    {'comp': 'Formula 2', 'fixtures': 'tsdb', 'tsdb_league_id': '4486', 'tsdb_sport': 'Motorsport'},
    {'comp': 'NASCAR Truck Series', 'fixtures': 'tsdb', 'tsdb_league_id': '5093', 'tsdb_sport': 'Motorsport'},
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
    # Added 2026-11-10, same audit: men's tours only before this. LPGA and PGA Tour Champions both had
    # a fixture within days of check time; LIV Golf confirmed real (and controversial but genuinely
    # broadcast) despite being seasonally quiet at check time -- same acceptable dormant-season state as
    # NFL/Cycling elsewhere in this file, not excluded for being out of season.
    {'comp': 'LPGA Tour', 'fixtures': 'tsdb', 'tsdb_league_id': '4553', 'tsdb_sport': 'Golf'},
    {'comp': 'PGA Tour Champions', 'fixtures': 'tsdb', 'tsdb_league_id': '4864', 'tsdb_sport': 'Golf'},
    {'comp': 'LIV Golf', 'fixtures': 'tsdb', 'tsdb_league_id': '5329', 'tsdb_sport': 'Golf'},
    {'comp': 'UFC', 'fixtures': 'tsdb', 'tsdb_league_id': '4443', 'tsdb_sport': 'Fighting'},
    {'comp': 'Boxing', 'fixtures': 'tsdb', 'tsdb_league_id': '4445', 'tsdb_sport': 'Fighting'},
    # Added 2026-11-10, same audit: both real, active promotions (ONE had a fixture 2 days before check
    # time, BKFC the same). Bellator deliberately excluded -- checked, its own catalog entry's most
    # recent event was 2025-01-25, consistent with its real-world merger into PFL that year; a catalog
    # entry existing is not the same as the promotion still operating.
    {'comp': 'ONE Championship', 'fixtures': 'tsdb', 'tsdb_league_id': '4495', 'tsdb_sport': 'Fighting'},
    {'comp': 'BKFC', 'fixtures': 'tsdb', 'tsdb_league_id': '4567', 'tsdb_sport': 'Fighting'},

    # Cycling, added 2026-11-09 at the owner's request. TheSportsDB's catalog has several cycling
    # leagues (checked by hand against the real key); 'UCI World Tour' (id 4465) is the one that's
    # actually the men's elite road calendar -- Tour de France, Giro, Vuelta, the Classics, 28 events a
    # season -- not 'UCI Road World Championships' (one race a year) or the Olympics/defunct-teams
    # entries also in that catalog. No strHomeTeam/strAwayTeam on its events (confirmed: a real past
    # event, "Tour of Guangxi Stage 6", had both null) -- same individual/stage-race shape as
    # Tennis/Golf/Motorsport above, so the existing strEvent-based fixture parsing already handles it
    # with no special-casing. Right now (Nov) is the off-season -- eventsnextleague.php returns nothing
    # until the calendar resumes in the new year -- so the TV-listing side below is configured on real
    # corroborating evidence, not yet confirmed against an actual live race.
    {'comp': 'Cycling', 'fixtures': 'tsdb', 'tsdb_league_id': '4465', 'tsdb_sport': 'Cycling'},
]

# ---- How long an event actually lasts -------------------------------------------------------------
# Until now the published event carried a start and nothing else, so the TV app had to guess a length,
# and guessed a flat 2.25h for everything with one hardcoded two-name exception for Test cricket. That
# is wrong for most of this file: a golf tournament runs four days, a Test five, a rally a weekend, a
# fight card an evening. The visible symptom (owner-reported, and the reason this table exists): a
# tournament showed as "live" for 2h15m, flipped to finished while it was still on day one, then
# dropped out of every list entirely 12h after that -- so "there was not a single golf event".
#
# Resolution order per event, applied in resolve_league:
#   1. a REAL end from the source (ESPN tournament endDate -- see espn_channels_tournament, which has
#      always parsed it and until now only used it as a local matching bound, then discarded it)
#   2. EVENT_HOURS_BY_COMP -- competitions whose shape differs from their sport's norm
#   3. UNTIMED_EVENT_HOURS -- no real start time known (TheSportsDB sent 00:00:00), so the honest
#      window is the whole calendar day rather than a precise-looking couple of hours
#   4. EVENT_HOURS_BY_SPORT -- the ordinary case
# Deliberately generous rather than tight: ending an event early makes it vanish mid-play, which is the
# failure being fixed; ending it late only leaves it listed a bit longer than needed.
EVENT_HOURS_BY_SPORT = {
    'Soccer': 2.5, 'Basketball': 3.0, 'Ice Hockey': 3.0, 'Baseball': 3.5, 'American Football': 3.5,
    'Motorsport': 2.5, 'Tennis': 3.0, 'Golf': 9.0, 'Fighting': 5.0, 'Rugby': 2.5, 'Cricket': 9.0,
    'Cycling': 6.0, 'Australian Football': 3.0,
}
EVENT_HOURS_BY_COMP = {
    # Five scheduled playing days. Replaces the client-side MULTIDAY_COMPS set, which lived in the TV
    # app as two literal strings -- the kind of blanket client-side rule this file should own instead.
    'ICC World Test Championship': 5 * 24,
    'International Test Match Series': 5 * 24,
    # One published event per whole rally/tournament rather than per day, so the window is the event.
    'WRC': 3 * 24,
}
UNTIMED_EVENT_HOURS = 24
DEFAULT_EVENT_HOURS = 2.5


def event_end_ts(fixture, sport):
    """Resolution order documented above. `fixture['end_ts']` is set only where a source gave a real
    end (currently ESPN tournaments); everything else is derived."""
    real = fixture.get('end_ts')
    if real and real > fixture['kick_ts']:
        return real
    hours = (EVENT_HOURS_BY_COMP.get(fixture['comp'])
             or (UNTIMED_EVENT_HOURS if not fixture.get('timed', True) else None)
             or EVENT_HOURS_BY_SPORT.get(sport, DEFAULT_EVENT_HOURS))
    return fixture['kick_ts'] + hours * 3600


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
    # IndyCar and UFC added 2026-11-02, same 'daily' shape as NASCAR (one event a day, loop real
    # TheSportsDB-sourced fixture days against ?dates=YYYYMMDD). Both confirmed live by hand: IndyCar's
    # racing/irl scoreboard returns real broadcasts (FOX); mma/ufc returns real broadcasts (Paramount+,
    # UFC's current US rights holder). Same US-only caveat as every other ESPN_SOURCES entry.
    {'comp': 'IndyCar', 'espn_path': 'racing/irl', 'kind': 'daily'},
    {'comp': 'UFC', 'espn_path': 'mma/ufc', 'kind': 'daily'},
    # Added 2026-11-10, part of the systematic coverage audit's TV-listing follow-up: checked TheSportsDB
    # eventstv.php for all 40 newly-added LEAGUES comps against the owner's real key -- every single one
    # confirmed empty (34 of 40 checked cleanly before hitting the real key's per-minute rate limit; the
    # other 6 are untested, not confirmed either way, flagged below). 'teams' shape like Eredivisie above
    # (multiple team-vs-team games a day, matched by name) -- not 'daily' (one single event a day). MLS
    # confirmed live by hand: real broadcasts, 'Apple TV' -- correct, MLS's actual real-world US rights
    # holder since 2023. WNBA confirmed live by hand too, but the real endpoint had nothing to show at
    # check time (its 2026 season just ended, 2026-11-05) -- same acceptable just-finished-season state
    # as NFL/Cycling elsewhere in this file, not a broken path.
    {'comp': 'MLS', 'espn_path': 'soccer/usa.1', 'kind': 'teams'},
    {'comp': 'WNBA', 'espn_path': 'basketball/wnba', 'kind': 'teams'},
    # NBA added 2026-11-10, found while the owner asked to specifically trace NBA end-to-end as a real
    # test case: despite being one of the four original flagship comps in this file (alongside NHL/MLB/
    # NFL), NBA was NEVER in ESPN_SOURCES at all -- confirmed TheSportsDB's own eventstv.php is empty for
    # it too, same as everything else, so without this it had genuinely zero TV-listing support this
    # whole time. Confirmed live by hand: real per-game broadcasts.php data, regional sports networks
    # (Spectrum Sports Net, NBCSN, MavsTV, and several 'DAZN <city>' entries -- matches real-world NBA
    # local broadcast deals, several teams moved to DAZN-powered regional networks in 2025-26). US-only,
    # same caveat as every entry above; UK/AU XMLTV feeds checked by hand for NBA programme text, found
    # nothing at the moment of checking -- inconclusive (NBA's actual UK/AU coverage is mostly via its
    # own League Pass app, not a clean linear channel, so this may never populate much outside the US).
    {'comp': 'NBA', 'espn_path': 'basketball/nba', 'kind': 'teams'},
    # NHL/MLB/NFL: same discovery as NBA just above, checked the same session once the pattern showed up
    # on the first one. All three are among the four ORIGINAL comps in this entire file (next to NBA) and
    # had the exact same gap the whole time. All three confirmed live by hand with real broadcasts.php
    # data matching real-world rights: NHL -- ESPN+, plus real regional networks (MSG, MSGSN, NBC Sports
    # Philadelphia/Bay Area); MLB -- Fox (real regular-season national rights holder, even returned a real
    # result despite being the off-season); NFL -- Prime Video, NFL Network, Fox (all real, current NFL
    # broadcast partners). Same US-only caveat as every entry above.
    {'comp': 'NHL', 'espn_path': 'hockey/nhl', 'kind': 'teams'},
    {'comp': 'MLB', 'espn_path': 'baseball/mlb', 'kind': 'teams'},
    {'comp': 'NFL', 'espn_path': 'football/nfl', 'kind': 'teams'},
    # Added 2026-11-10, part of the same research pass that found the cricket/Test-match gap. Checked
    # against ESPN's own authoritative league list (sports.core.api.espn.com/v2/sports/racing/leagues
    # and .../golf/leagues -- the real enumerated slugs, not guessed ones), then confirmed live:
    # 'racing/nascar-truck' -- real race, real broadcasts ("FS1"), same 'daily' pattern as NASCAR/IndyCar.
    {'comp': 'NASCAR Truck Series', 'espn_path': 'racing/nascar-truck', 'kind': 'daily'},
    # 'golf/lpga', 'golf/champions-tour' -- both real tournaments, real broadcasts ("Golf Chnl"), same
    # 'tournament' pattern as PGA/European Tour (one call, no date, spans the whole event).
    {'comp': 'LPGA Tour', 'espn_path': 'golf/lpga', 'kind': 'tournament'},
    {'comp': 'PGA Tour Champions', 'espn_path': 'golf/champions-tour', 'kind': 'tournament'},
    # 'golf/liv' -- real league, but the undated default call can return a canceled/empty event (checked
    # by hand); confirmed real broadcasts only against specific past 2026-season dates ("FOX","FS1","FS2").
    # Current (2025+) US rights holder is Fox, NOT The CW -- the CW deal was 2024-only and already ended;
    # don't add a CW XMLTV pattern for this, it would chase a dead deal. 'tournament' kind still correct
    # (LIV events are multi-day, one call covers the whole event) -- the undated-call caveat is the same
    # risk every 'tournament' entry already accepts (PGA/European Tour), not something new here.
    {'comp': 'LIV Golf', 'espn_path': 'golf/liv', 'kind': 'tournament'},
    # 'basketball/fiba' -- found via ESPN's authoritative basketball/leagues list (which has no
    # 'euroleague' slug at all -- confirmed dead end for that comp, don't add one). This is the single
    # best find of the whole 2026-11-10 research pass: a free, no-auth ESPN endpoint with a genuinely
    # non-empty broadcasts[] for a comp that had zero channel signal anywhere before (real data:
    # "TNT","truTV","HBO Max" against real FIBA Basketball World Cup fixtures). 'teams' kind: country-vs-
    # country games, several a day, matched by team name like WNBA/MLS/NBA above -- not a single standing
    # event like NASCAR/golf.
    {'comp': 'FIBA Basketball World Cup', 'espn_path': 'basketball/fiba', 'kind': 'teams'},
]
# UPDATE 2026-11-10 (second pass, same day): the TV-listing follow-up list below this comment used to
# name everything as "still needed" -- a full research pass (4 parallel agents, real fetches, not
# guesses) closed almost all of it. Resolved real sources went into LEAGUES/ESPN_SOURCES/XMLTV_SOURCES
# above with their own comments. What's left, CONFIRMED DEAD (checked by hand, not just "not found yet"
# -- don't re-research these without new evidence):
# - Boxing: see the dedicated comment below. No change.
# - ONE Championship: ESPN has no slug (confirmed against the real ~25-slug MMA list). No free XMLTV/API
#   broadcaster source either -- onefc.com's own site only has ad hoc "How To Watch" mentions (e.g. Prime
#   Video for one event), nothing structured/bulk. Fixtures-only via Wikipedia if ever needed.
# - BKFC: same ESPN dead end as ONE. bkfc.com/events has real fixtures + "BKFC+/Fubo" branding (HTML
#   only, no API). ufcalendar.com's free-tier API (1,000 req/mo, no card) explicitly covers BKFC
#   broadcasts and is worth actually signing up for if this comp becomes worth the effort -- unverified
#   beyond its own docs, no key was requested.
# - Russian KHL: zero signal anywhere -- not in ESPN's hockey/leagues list at all, zero genuine XMLTV
#   hits in any of the 5 feeds (checked with a \bkhl\b word-boundary, not just substring). Matches the
#   real-world picture (KHL has almost no Western broadcast footprint post-2022).
# - EuroLeague Basketball: same -- no ESPN slug, zero genuine XMLTV hits (its real rights are split
#   across EuroLeague TV/FanDuel TV/L'Équipe/MagentaSport, none of which land in these linear EPG dumps).
# - Formula E, Formula 2: no ESPN slug for either (confirmed against the real 5-slug racing/leagues
#   list: f1/irl/nascar-premier/nascar-secondary/nascar-truck only). Every XMLTV hit investigated for
#   both was a false positive (a DHL-logistics documentary namedropping Formula E; a Schumacher-career
#   retrospective namedropping F2) -- not real event coverage. No free source exists for either.
# Resolved this pass, sources now wired above: NASCAR Truck Series, LPGA Tour, PGA Tour Champions, LIV
# Golf, FIBA Basketball World Cup (ESPN); Moto2/Moto3/World Superbike (already covered by existing
# UK1/AU1 TNT/Fox Sports patterns, WSBK's US gap closed via a new 'racer' pattern); WRC (new CA2 'rev tv'
# pattern, highlights-level, WRC+ still the only full-coverage source); AFL (new AU1 'fox footy'
# pattern); Rugby Union -- all six comps (Six Nations/Rugby Championship/Super Rugby/URC/Top 14/Rugby
# World Cup) already covered by existing UK1 Premier Sports/TNT and NZ1 Sky Sport patterns, live-verified
# against real "Top 14 Rugby Union"/"PREM Rugby Cup"/All Blacks programme titles -- no code change
# needed, ESPN itself has real fixtures for all six but always-empty broadcasts[] for every one; IIHF
# World Championship -- already covered by existing CA2 TSN pattern, live-verified; Cricket (every comp)
# -- see the dedicated Test-cricket/ODI/T20I/Hundred/CPL comments above.
# No MotoGP entry: ESPN's site API doesn't carry it at all (checked by hand -- no motogp/motorsport path
# exists under /apis/site/v2/sports/racing/). Its own official site (api.pulselive.motogp.com) confirms
# real fixtures but has no broadcaster endpoint either (checked by hand, several guessed paths all 400).
# MotoGP gets its channels from XMLTV_SOURCES below instead (real per-fixture programme data covers
# it -- AU1/UK1/CA2 all had genuine MotoGP race-weekend listings when checked by hand 2026-11-02).
#
# No Boxing entry, and none planned: checked by hand 2026-11-02, ESPN's site API doesn't carry boxing
# at all -- /apis/site/v2/sports/boxing/scoreboard 404s, and the core API's own sport list rejects
# "boxing" outright ({"error":{"message":"Invalid sport (boxing)."}}), unlike every other sport here
# which at least resolves to a real (if sometimes wrong) league slug. XMLTV_SOURCES was checked too --
# no DAZN/PPV/boxing-branded channel exists in any of the AU/UK/NZ/CA/US feeds. This isn't a parsing
# gap, it's that boxing's real-world broadcast rights are split across PPV/DAZN/promoter-specific deals
# with no single body or standing channel the way UFC/PGA/NASCAR have -- there is currently no free,
# legitimate source for this one. Boxing still gets whatever TheSportsDB's eventstv.php happens to
# carry (same as everything else), just nothing on top of it.
#
# No Cycling entry: checked by hand 2026-11-09, /apis/site/v2/sports/cycling/scoreboard 404s the same
# way boxing's does -- ESPN's site API has no cycling sport at all. Doesn't need an XMLTV_SOURCES entry
# either, unlike MotoGP/boxing above: TheSportsDB's own league metadata for UCI World Tour (id 4465)
# names 'TNT Sports' as the real TV-rights holder, and UK1's channel_re already matches 'tnt\s*sports?'
# (added for football's TNT Sports channels, well before cycling existed in this file) -- confirmed by
# hand the same day that UK1 genuinely carries 11 separate TNT Sports channels. No Eurosport channel
# exists in any of AU1/UK1/NZ1/CA2/US2 (checked by hand; TNT absorbed Eurosport's UK sports rights a
# few years back, consistent with TheSportsDB's own claim) -- don't add an 'eurosport' pattern, there's
# nothing in these feeds for it to match. Cycling is in the Northern Hemisphere off-season right now
# (eventsnextleague.php for 4465 returns nothing), so there's no live race to confirm an actual
# programme-level TNT Sports listing against yet -- this is configured on real corroborating evidence
# (the league's own rights-holder field, the channel's real existence in UK1), not a live-verified
# match the way MotoGP/IndyCar/UFC above are. Worth a real check once the season resumes in the new year.

# Real per-country TV guide XMLTV feeds, checked by hand 2026-11-02 after the raw Foxtel/Sky backend
# APIs that power them turned out dead/walled (tvguide.foxsports.com.au returns HTTP 200 with a fully
# empty result; epgsky.com 403s without private app auth) -- these are PRE-COMPILED, publicly hosted
# outputs of that same kind of scraping, already run and republished daily by epgshare01.online, a
# well-known community aggregator used widely in IPTV/Jellyfin/Plex setups. Verified by hand, not
# assumed: downloaded all five, decompressed them, and grep'd for real programme text -- genuine
# MotoGP/NASCAR/PGA Tour/European Tour hits in the dozens to low hundreds per file, e.g. a real
# "SkySp.Golf.uk" entry titled "PGA Tour Champions Highlights" with a real date and description, not
# placeholder junk. Eredivisie is absent from AU/UK/NZ/US (0 hits) but present in CA2 (20 hits) --
# genuinely thin international coverage, not a parsing gap; TrillerTV (the actual rights holder,
# streaming-only, no linear channel) is still the better answer for that one specifically.
#
# channel_re matches candidate channel ids/display-names up front during parsing (see
# fetch_xmltv_programmes) so the multi-hundred-thousand-line files below don't have to keep every
# channel's programmes in memory -- only the sports-adjacent slice. Kept deliberately generous (e.g.
# bare "fox" on the US feed also catches Fox News/Fox Business) since over-matching a channel here only
# costs a little parse time; under-matching one silently drops a real channel, the actual risk.
XMLTV_SOURCES = [
    {'country': 'AU', 'url': 'https://epgshare01.online/epgshare01/epg_ripper_AU1.xml.gz',
     # 'fox\s*league' added 2026-11-09 for Rugby League World Cup (see LEAGUES): a real 'Fox League'
     # channel exists in this feed (confirmed by hand), the actual Australian rugby league rights
     # holder in real life. No programme-level confirmation yet against a specific RLWC match -- this
     # feed's current window didn't have one at the moment of checking -- so treat this the same as
     # Cycling/TNT: real corroborating evidence (the channel itself, genuinely exists), not a live-
     # verified match yet.
     # 'fox\s*cricket' added 2026-11-10 for Australian Big Bash League (see LEAGUES): a real 'Fox
     # Cricket' channel exists in this feed (confirmed by hand), the actual Australian cricket rights
     # holder in real life. Same corroborating-evidence standard as Fox League above -- channel
     # confirmed real, no specific-match programme-level confirmation yet (Big Bash is seasonally
     # dormant at check time, Dec-Jan window). UPDATE 2026-11-10: Fox Cricket now also programme-level
     # confirmed for the generic ODI Series comp (not just channel-existence) -- real current titles
     # "LIVE: ODI - AUS v ENG G1", "ODI Mini - NZL v IND G2" found in this feed at check time.
     # 'fox\s*footy' added 2026-11-10 for AFL: a real 'Fox Footy' channel exists in this feed (confirmed
     # by hand, real AFL/AFLW programming) -- distinct id from 'Fox Sports', so the existing
     # fox\s*sports pattern never caught it. ESPN's own AFL endpoint returns real fixtures but an always-
     # empty broadcasts[] (checked by hand), so this XMLTV channel is the only real signal for AFL.
     'channel_re': re.compile(r'fox\s*sports|fox8|fox\s*league|fox\s*cricket|fox\s*footy|espn|bein|kayo', re.I)},
    {'country': 'UK', 'url': 'https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz',
     # 'bbc' added 2026-11-10 for The Hundred (see LEAGUES): real-world rights are split Sky Sports
     # (all 68 matches, already caught by 'sky\s*sp' -- SkySpCricket.HD.uk confirmed in this feed with a
     # real "The Hundred Highlights" title right now) + BBC One/Two (~16 free-to-air matches including
     # the final, confirmed by hand against thehundred.com/Yahoo Sports). Closes what was previously a
     # total-zero gap for this comp. Deliberately broad ('bbc' alone also matches BBC News/Parliament/
     # etc.) -- same tradeoff this file already accepts elsewhere: a little wasted parse time costs far
     # less than silently dropping a real free-to-air match.
     'channel_re': re.compile(r'sky\s*sp|tnt\s*sports?|premier\s*sports|bein|bbc', re.I)},
    {'country': 'NZ', 'url': 'https://epgshare01.online/epgshare01/epg_ripper_NZ1.xml.gz',
     'channel_re': re.compile(r'sky\s*sport|espn', re.I)},
    {'country': 'CA', 'url': 'https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz',
     # 'rev\s*tv' added 2026-11-10 for WRC: 'REV TV Canada' is a real dedicated motorsport channel in
     # this feed (confirmed by hand, real "FIA World Rally Championship" coverage+analysis airing
     # repeatedly) -- ESPN has no WRC endpoint at all (confirmed against its authoritative racing/leagues
     # list: only f1/irl/nascar-premier/nascar-secondary/nascar-truck exist). WRC+ is still the only
     # source for full live stage coverage; this is real linear highlights/review coverage, not that.
     'channel_re': re.compile(r'tsn|sportsnet|fox\s*sports|rev\s*tv', re.I)},
    # IN (India) added 2026-11-10 for Indian Premier League (see LEAGUES): a real 'Star Sports' channel
    # family exists in this feed (confirmed by hand -- Star Sports 2 Hindi/Tamil/Telugu, Star Sports
    # Khel), the actual Indian cricket rights holder in real life. No programme-level confirmation yet
    # -- IPL is seasonally dormant at check time (Apr-May window) -- same corroborating-evidence
    # standard as Fox League/Fox Cricket above.
    {'country': 'IN', 'url': 'https://epgshare01.online/epgshare01/epg_ripper_IN1.xml.gz',
     'channel_re': re.compile(r'star\s*sports', re.I)},
    # epgshare01's "US_SPORTS1" is NOT this (checked by hand -- it's Minor League Baseball team
    # channels, zero real network content); "US2" is the real one with ESPN/FOX/CBS Sports/etc.
    # "tennis" added 2026-11-02: Tennis.Channel.HD.us2 is a real channel in this feed with real ATP/
    # WTA programme text ("Center Court", sub-title naming the actual tour/city) -- just never matched
    # by the old regex, nothing to do with the sport lacking coverage here.
    # "willow" added 2026-11-10: Willow.Cricket.HD.us2 and Willow.2.Xtra.us2 are real channels in this
    # feed (confirmed by hand), the actual US/Canada cricket rights holder for ICC international cricket
    # AND the Caribbean Premier League. Programme-level confirmed live, not just channel-existence: real
    # sub-titles currently in this feed read "New Zealand vs India: 3rd ODI", "England vs Sri Lanka: 1st
    # T20I", "India vs West Indies - 2nd ODI" -- an exact match to the generic ODI/T20I Series fixtures
    # this file already tracks (see LEAGUES ids 4801/4979), closing what was previously a total-zero gap
    # for those two comps.
    # 'racer' added 2026-11-10 for World Superbike: 'RACER Network' is a real dedicated US motorsports
    # cable channel in this feed (confirmed by hand, real "2026 FIM World Superbike Championship
    # Highlights" title) -- ESPN has no WSBK endpoint at all (same authoritative racing/leagues list
    # check as WRC above). UK1/AU1 already catch WSBK via their existing tnt\s*sports?/fox\s*sports
    # patterns -- this closes the US gap specifically.
    {'country': 'US', 'url': 'https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz',
     'channel_re': re.compile(r'espn|fox|cbs\s*sports|nbc|usa|golf|tennis|willow|racer', re.I)},
    # CA1 (the obvious sibling to CA2) 404s -- removed, not guessed at; CA2 alone already covers
    # TSN/Sportsnet/Fox Sports Racing, the channels that actually matter here.
]

XMLTV_MAX_AGE = 2 * 3600   # these are daily community rippers, not a live feed -- no reason to
                           # redownload and reparse several MB of XML every 15-minute guide-v2 tick.


def parse_xmltv_ts(raw):
    """XMLTV's "YYYYMMDDHHMMSS +HHMM" format -- confirmed by hand these feeds use real, VARIED offsets
    (+0800, +0100, -0400, -0500, +0000 all seen across AU1/UK1/CA2), not always +0000, so the offset
    has to actually be parsed and subtracted, not assumed away."""
    if not raw or len(raw) < 14:
        return None
    try:
        dt = datetime.strptime(raw[:14], '%Y%m%d%H%M%S')
        off = raw[14:].strip()
        if off:
            sign = 1 if off[0] == '+' else -1
            dt -= sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, IndexError):
        return None


def fetch_xmltv_programmes(source, cache, horizon_days=DAYS_AHEAD):
    """Streams the feed with ET.iterparse instead of loading the whole DOM -- these decompress to
    hundreds of thousands of lines (CA2 alone is ~90MB uncompressed) -- and clears every element after
    reading it so memory stays bounded by "how many programmes are in the sports-channel/time-window
    slice", never by the file's real total size. Returns (programmes, channel_names); channel_names
    maps a raw id ("FoxSports505.au") to its real <display-name> ("Fox Sports 505") so guide-v2.json
    doesn't publish the raw XMLTV id as the channel name."""
    cache_key = 'xmltv|%s' % source['country']
    entry = cache.get(cache_key)
    if entry and time.time() - entry.get('fetched_at', 0) < XMLTV_MAX_AGE:
        return entry.get('programmes') or [], entry.get('names') or {}
    try:
        req = urllib.request.Request(source['url'], headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = gzip.decompress(r.read())
    except Exception as e:
        print('XMLTV fetch failed for %s: %s' % (source['country'], e), file=sys.stderr)
        return (entry.get('programmes'), entry.get('names')) if entry else ([], {})

    now = time.time()
    lo, hi = now - 864e2, now + horizon_days * 864e2
    wanted_ids, names, programmes = set(), {}, []
    for _, elem in ET.iterparse(io.BytesIO(raw), events=('end',)):
        if elem.tag == 'channel':
            cid = elem.get('id') or ''
            dn = elem.findtext('display-name') or ''
            if source['channel_re'].search(cid) or source['channel_re'].search(dn):
                wanted_ids.add(cid)
                names[cid] = dn.strip() or cid
            elem.clear()
        elif elem.tag == 'programme':
            cid = elem.get('channel') or ''
            if cid in wanted_ids:
                start = parse_xmltv_ts(elem.get('start'))
                if start is not None and lo <= start <= hi:
                    text = ' '.join(filter(None, [elem.findtext('title'), elem.findtext('sub-title'), elem.findtext('desc')]))
                    stop = parse_xmltv_ts(elem.get('stop')) or (start + 3600)
                    programmes.append({'channel': cid, 'start': start, 'stop': stop, 'text': text})
            elem.clear()
    cache[cache_key] = {'fetched_at': now, 'programmes': programmes, 'names': names}
    print('XMLTV %s: %d sports-channel programmes in the next %d days' % (source['country'], len(programmes), horizon_days), file=sys.stderr)
    return programmes, names


def load_xmltv(cache):
    """Fetches every country once per resolve() run (not once per league -- these are expensive, multi-
    MB downloads) and hands every league the same shared index."""
    out = {}
    for source in XMLTV_SOURCES:
        programmes, names = fetch_xmltv_programmes(source, cache)
        out[source['country']] = {'programmes': programmes, 'names': names}
    return out


def norm_text(s):
    """Same normalisation as the TV app's own norm() (accent-strip, lowercase, punctuation to spaces)
    -- kept separate from norm_team below, which additionally strips club-name filler words ("FC",
    "United"...) that would be wrong to strip from a tournament/event label."""
    import unicodedata
    s = unicodedata.normalize('NFD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'[^a-z0-9]+', ' ', s.lower().replace('&', ' and ').replace("'", ''))
    return s.strip()


# Known on-air phrasings that don't literally contain the comp name as a substring -- without these,
# requiring the sport identifier below would silently never match correctly-worded real listings
# (confirmed real cases: EPGs commonly abbreviate "Formula 1" to "F1"; the European Tour has been
# branded "DP World Tour" on air for years; neither phrase contains the comp string at all. Same
# pattern confirmed 2026-11-02 for tennis: real listings on SkySp.Tennis.HD.uk/Tennis.Channel.HD.us2
# say "WTA Tokyo Final" or "Vienna-ATP" -- the bare tour abbreviation, never the literal "ATP Tour"/
# "WTA Tour" phrase -- so without these aliases xmltv_text_matches silently failed every real tennis
# listing despite the right channels already being in scope).
COMP_TEXT_ALIASES = {
    'Formula 1': ['f1'],
    'European Tour': ['dp world tour'],
    'ATP Tour': ['atp'],
    'WTA Tour': ['wta'],
}


def xmltv_text_matches(text, fixture):
    """Team sports: the existing team-pair match (unaffected by the bug this fixes). Non-team sports
    MUST match the sport/series identifier itself (the comp name, or a known on-air alias above) -- a
    bare distinctive LABEL word is no longer enough on its own. Confirmed real false positive this
    was built to fix: "Sky Sports Golf" matched a MotoGP fixture purely because its listing happened
    to mention "Malaysia" (the race's location that week), even though Sky Sports doesn't hold
    MotoGP's UK rights at all -- TNT Sports does. A city/country name isn't sport-specific; two
    completely unrelated sports' events can share one in the same week. The sport's own name is the
    one thing in the label that's actually diagnostic."""
    if fixture.get('home') and fixture.get('away'):
        return teams_match(fixture['home'], fixture['away'], text)
    t = ' ' + norm_text(text) + ' '
    comp = fixture.get('comp') or ''
    names = [comp] + COMP_TEXT_ALIASES.get(comp, [])
    return any(n and (' ' + norm_text(n) + ' ') in t for n in names)


def xmltv_channels_for_fixture(fixture, xmltv, now_iso):
    # Deduped by (normalised channel name, country), keeping the earliest match -- a single channel's
    # multiple programme blocks around a fixture (e.g. quali and the race both falling inside this
    # function's own time window) used to each produce their own channel row, so one real channel
    # could show up 5-7 times for one event. The earliest start is the one closest to kickoff, same
    # "keep whichever's nearer" reasoning the TV app's own v2Events() already uses client-side.
    best = {}
    for cc, idx in xmltv.items():
        for p in idx['programmes']:
            if p['stop'] < fixture['kick_ts'] - 3 * 3600 or p['start'] > fixture['kick_ts'] + 6 * 3600:
                continue
            if not xmltv_text_matches(p['text'], fixture):
                continue
            name = idx['names'].get(p['channel'], p['channel'])
            key = (norm_text(name), cc)
            if key not in best or p['start'] < best[key]['start']:
                best[key] = {'name': name, 'country': cc, 'start': p['start']}
    return [{
        'channel': v['name'], 'country': v['country'],
        'kind': classify_kind(v['start'], fixture['kick_ts']), 'inferred_kind': True,
        'start': datetime.fromtimestamp(v['start'], tz=timezone.utc).isoformat(),
        'confidence': 'likely', 'source': 'xmltv', 'checked': now_iso,
    } for v in best.values()]


# ---- ausportguide.com (AU, multi-sport TV guide, free/no-login, HTML scrape) ----
# Found 2026-11-10 by the owner directly, mid-investigation of the Pakistan vs Sri Lanka Test match
# that started tonight's whole cricket audit -- they spotted it had their exact match with their exact
# real channel numbers ("Fox Sports 503, Fox Sports 505, Fox Sports More"), confirmed by hand against
# the live page. Genuinely the best single find of the session: one page per sport covering nearly
# everything this file has thin coverage for (cricket generally) or a confirmed hard dead end on every
# other source (boxing/MMA -- real DAZN entries confirmed present). No public API, no robots.txt --
# this is a straight HTML scrape of a small commercial site (it has its own iOS/Android apps), more
# fragile than the documented ESPN/XMLTV sources above: their markup can change and silently break
# this with no warning. Treat it as a bonus signal, not load-bearing the way ESPN_SOURCES is.
#
# Each event block in the page HTML looks like:
#   <div title="Pakistan - Sri Lanka - Test Series" class="list-group-item  d-flex gap-3 shadow-sm schedule-event">
#     ...<span class="schedule-live-status" data-start="1794198600" hidden></span>...
#     <span class="schedule-station stationImg">Fox Sports 503</span><span class="schedule-station stationImg">Fox Sports 505</span>...
#   </div>
# The title attribute already gives team names (or a single event name) plus the league/competition,
# space-separated by " - " -- fed straight into the existing teams_match()/xmltv_text_matches() logic
# below rather than re-implementing team-name extraction, so this gets the exact same matching standard
# (and the exact same false-positive guards) as the XMLTV path already has.
AUSPORTGUIDE_BASE = 'https://ausportguide.com/live-sports-tv-guide/'
# Keyed by tsdb_sport (like AUSPORTGUIDE_RUGBY_LEAGUE_COMPS below, one fetch covers every comp sharing
# that sport, same dedup reasoning as the TSDB TV-day cache). Checked by hand 2026-11-10: this site has
# no separate Rugby Union page at all (its own nav only lists "Rugby league"), so 'Rugby' can't be a
# blanket entry here the way the others are -- Six Nations/Super Rugby/URC/etc. get nothing from this
# source, same honest gap as everywhere else tonight, not a bug.
AUSPORTGUIDE_PATHS = {
    'Cricket': 'cricket', 'Fighting': 'boxing-mma', 'Australian Football': 'aussie-rules',
    'Basketball': 'basketball', 'Baseball': 'baseball', 'Tennis': 'tennis',
    'Motorsport': 'motorsport', 'Soccer': 'football',
}
AUSPORTGUIDE_RUGBY_LEAGUE_PATH = 'rugby-league'
AUSPORTGUIDE_RUGBY_LEAGUE_COMPS = {'NRL', 'Rugby League World Cup', 'Rugby League Super League',
                                    'World Club Challenge', 'State of Origin'}
AUSPORTGUIDE_MAX_AGE = 2 * 3600   # same reasoning as XMLTV_MAX_AGE -- not a live feed, no reason to re-scrape every 15-minute tick

_AUSPORTGUIDE_EVENT_RE = re.compile(r'<div title="([^"]*)" class="[^"]*\bschedule-event\b[^"]*">')
# NOT a literal-class match (used to be "list-group-item\s+d-flex gap-3 shadow-sm schedule-event",
# word for word) -- confirmed by hand against a real downloaded page that PAST/already-aired events get
# an extra "opacity-50" class spliced into the middle of that exact string for the greyed-out styling,
# which the literal version silently failed to match. That didn't just drop those events -- their own
# station spans then bled into whichever NEXT matched event's chunk (re.split's chunk boundaries are
# "from this title to the next matched title", so an unmatched event in between gets absorbed into its
# neighbour), producing real, confirmed duplicate/wrong-event channel assignments. Caught by testing
# this parser against real saved pages before trusting it, not by assumption.
_AUSPORTGUIDE_START_RE = re.compile(r'data-start="(\d+)"')
_AUSPORTGUIDE_STATION_RE = re.compile(r'class="schedule-station[^"]*"[^>]*>([^<]*)<')


def fetch_ausportguide_page(path, cache):
    cache_key = 'ausportguide|' + path
    c = cache.get(cache_key)
    if c and time.time() - c['ts'] < AUSPORTGUIDE_MAX_AGE:
        return c['events']
    try:
        req = urllib.request.Request(AUSPORTGUIDE_BASE + path, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode('utf-8', 'replace')
    except Exception:
        return (c or {}).get('events', [])   # additive source: a failed scrape just leaves whatever's cached, never breaks the rest of the guide
    # re.split with one capture group yields [pre, title1, chunk1, title2, chunk2, ...] -- chunk_i runs
    # up to the NEXT event's own title marker, so a findall inside it only ever sees this one event's
    # own station spans, never leaks into the next event's.
    parts = _AUSPORTGUIDE_EVENT_RE.split(html)
    events = []
    for i in range(1, len(parts) - 1, 2):
        title, chunk = parts[i], parts[i + 1]
        m = _AUSPORTGUIDE_START_RE.search(chunk)
        if not m:
            continue
        stations = [s.strip() for s in _AUSPORTGUIDE_STATION_RE.findall(chunk)]
        stations = [s for s in stations if s]
        if not stations:
            continue
        events.append({'title': title, 'ts': int(m.group(1)), 'stations': stations})
    cache[cache_key] = {'ts': time.time(), 'events': events}
    return events


def ausportguide_channels_for_fixture(fixture, events, now_iso):
    out = []
    for e in events:
        if abs(e['ts'] - fixture['kick_ts']) > 20 * 3600 or not xmltv_text_matches(e['title'], fixture):
            continue
        for st in e['stations']:
            out.append({
                'channel': st, 'country': 'AU',
                'kind': classify_kind(e['ts'], fixture['kick_ts']), 'inferred_kind': True,
                'start': datetime.fromtimestamp(e['ts'], tz=timezone.utc).isoformat(),
                'confidence': 'likely', 'source': 'ausportguide', 'checked': now_iso,
            })
    return out


# ---- tvsports.in (India, multi-sport TV guide, free/no-login) ----
# Found 2026-11-10 in the same research pass that found ausportguide.com -- directly closes this file's
# single biggest remaining gap: real Star Sports/SONY Ten/SONY LIV channel-level data for Indian cricket
# (ODI/T20I/IPL all needed exactly this granularity and had nothing before). Verified by hand: the
# cricket page's real embedded fixture data has "New Zealand - India" (One Day International) on real
# channels SONY LIV/SONY Ten 1/3/4/5, and the football page has the same shape with real UEFA Nations
# League data. robots.txt explicitly allows crawling (only /go? and /app/news disallowed).
#
# Structurally nicer than ausportguide.com's regex scrape: this is a Next.js App Router site, which
# streams its page data as escaped JSON strings inside `self.__next_f.push([1,"..."])` calls rather than
# rendering everything into plain HTML -- the chunk containing "oldFixtures" IS a real JSON string
# literal (quotes and all), so json.loads-ing it directly recovers the real underlying JSON text, and a
# plain brace-match from its "data": key extracts the fixtures object with no HTML parsing at all. Still
# an unofficial, undocumented structure (no public API) -- could break on a redesign -- but far less
# fragile than matching literal CSS classes the way the AU scrape has to.
TVSPORTS_BASE = 'https://www.tvsports.in/sport/'
TVSPORTS_PATHS = {'Cricket': 'cricket', 'Soccer': 'football', 'Basketball': 'basketball',
                   'Tennis': 'tennis', 'Golf': 'golf'}   # 'motorsport' checked by hand, 404s -- not a real path here
TVSPORTS_MAX_AGE = 2 * 3600
_TVSPORTS_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,(".*?")\]\)', re.S)


def _parse_tvsports_html(html):
    chunks = _TVSPORTS_CHUNK_RE.findall(html)
    target = next((c for c in chunks if 'oldFixtures' in c), None)
    if not target:
        return []
    try:
        real_text = json.loads(target)
        idx = real_text.find('"data":{')
        if idx < 0:
            return []
        start = idx + len('"data":')
        depth, i = 0, start
        while i < len(real_text):
            if real_text[i] == '{':
                depth += 1
            elif real_text[i] == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        data = json.loads(real_text[start:i + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for day in data.values():
        for f in (day.get('newFixtures') or []) + (day.get('oldFixtures') or []):
            if not f.get('channels'):
                continue
            # "2026-11-10T02:00:00.000Z" -> "2026-11-10T02:00:00" (parse_ts's format has no fractional
            # seconds or a trailing Z -- both of those are always UTC already here, same as everywhere
            # else in this file, so simply truncating before the dot is exact, not an approximation).
            ts = parse_ts((f.get('date') or '').split('.')[0])
            if ts is None:
                continue
            out.append({
                'title': ' '.join(filter(None, [f.get('home_team'), f.get('visiting_team'), f.get('league')])),
                'ts': ts,
                'stations': [c['name'] for c in f['channels'] if c.get('name')],
            })
    return out


def fetch_tvsports_page(path, cache):
    cache_key = 'tvsports|' + path
    c = cache.get(cache_key)
    if c and time.time() - c['ts'] < TVSPORTS_MAX_AGE:
        return c['events']
    try:
        req = urllib.request.Request(TVSPORTS_BASE + path, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode('utf-8', 'replace')
        events = _parse_tvsports_html(html)
    except Exception:
        return (c or {}).get('events', [])   # additive source: a failed fetch just leaves whatever's cached, never breaks the rest of the guide
    cache[cache_key] = {'ts': time.time(), 'events': events}
    return events


def tvsports_channels_for_fixture(fixture, events, now_iso):
    out = []
    for e in events:
        if abs(e['ts'] - fixture['kick_ts']) > 20 * 3600 or not xmltv_text_matches(e['title'], fixture):
            continue
        for st in e['stations']:
            out.append({
                'channel': st, 'country': 'IN',
                'kind': classify_kind(e['ts'], fixture['kick_ts']), 'inferred_kind': True,
                'start': datetime.fromtimestamp(e['ts'], tz=timezone.utc).isoformat(),
                'confidence': 'likely', 'source': 'tvsports', 'checked': now_iso,
            })
    return out


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


# TheSportsDB's published free/test keys -- only ever used for local debugging; the real CI run
# always gets the premium key from secrets.TSDB_KEY, so this never affects production. The free
# key's own rate limiter hands back a real Retry-After of 120s (confirmed by hand 2026-11-02 -- that
# number is the server's, not a constant this file chose). Honouring that during a quick local smoke
# test just means sitting through minutes of sleep for data the free key was never going to give
# cleanly anyway (it's throttled far harder than the real plan). Both callers of this function already
# catch and degrade gracefully on failure (empty fixtures/listings), so failing fast here is safe.
TSDB_FREE_KEYS = {'3', '123'}


def fetch_tsdb_json(url, max_retries=4):
    is_free_key = TSDB_KEY in TSDB_FREE_KEYS
    for attempt in range(1 if is_free_key else max_retries):
        tsdb_rate_limit_wait()
        try:
            return fetch_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 429 and not is_free_key and attempt < max_retries - 1:
                retry_after = e.headers.get('Retry-After') if e.headers else None
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 65
                print('429 from TheSportsDB -- waiting %.0fs before retrying (attempt %d/%d)' % (wait, attempt + 1, max_retries), file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 429 and is_free_key:
                print('429 from TheSportsDB (free test key -- not retrying, expected locally)', file=sys.stderr)
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
        # 'round': football-data.org's own 'stage' field is a real text value ("REGULAR_SEASON",
        # "FINAL", "SEMI_FINALS", "QUARTER_FINALS", "ROUND_16", "GROUP_STAGE", ...) -- a cleaner,
        # directly-readable knockout signal than TheSportsDB's numeric intRound codes below, but still
        # not a universal shape across both sources, so the client treats this the same way: a
        # knockout-stage signal to check for keywords against, never assumed to be one consistent type.
        out.append({'id': 'fd:%s:%s' % (league['fd_comp'], m['id']), 'comp': league['comp'], 'tsdb_id': None,
                    'home': m['homeTeam']['name'], 'away': m['awayTeam']['name'],
                    'label': m['homeTeam']['name'] + ' v ' + m['awayTeam']['name'],
                    'kick': m['utcDate'], 'kick_ts': kick.timestamp(), 'timed': True,
                    'status': m['status'], 'round': m.get('stage')})   # football-data always sends a real utcDate, never a placeholder
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
            # 'round': raw intRound, kept as TheSportsDB sends it (a plain integer string for most
            # leagues' regular season, but special non-linear codes for knockout stages on some comps --
            # e.g. confirmed on the Rugby League World Cup final, intRound="200" -- so this is NOT safe
            # to rank by as a plain number across competitions; the client-side significance scoring
            # (Event Hub plan item 3) treats it as a knockout-stage signal via a threshold/keyword check,
            # never a linear "higher round = more important" comparison. Passed through as-is, string or
            # None, no parsing here -- added 2026-11-10, first-ever capture of this field in this file.
            # 'timed': whether that kick-off is a REAL start time or a placeholder. TheSportsDB sends
            # strTime "00:00:00" to mean "we don't know when" -- confirmed by hand against the live API
            # for golf ("Good Good Championship", dateEvent 2026-11-12, strTime 00:00:00) and the same
            # holds for tennis, boxing, Moto2/Moto3 and WRC. That is 84 of 543 published events (15%).
            # Taken literally it reads as midnight UTC, and the app then draws a confident "11:00am"
            # (AEDT) that is pure fiction, and treats the event as over 2h15m later. The app already has
            # an honest presentation for this case -- a `timed` flag, an "Event channels (time not
            # known)" heading and an EVENT badge -- built for its own channel-name parsing and never
            # connected to server data. This connects it.
            raw_time = (e.get('strTime') or '').strip()
            timed = bool(raw_time) and not raw_time.startswith('00:00:00')
            out.append({'id': 'tsdb:%s' % e.get('idEvent'), 'comp': league['comp'], 'tsdb_id': e.get('idEvent'),
                        'home': home, 'away': away, 'label': e.get('strEvent') or ((home or '') + ' vs ' + (away or '')),
                        'kick': datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), 'kick_ts': ts,
                        'timed': timed,
                        'status': e.get('strStatus') or 'NS', 'round': e.get('intRound')})
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
    # isinstance guard: a bare non-dict value (e.g. a raw float) in cache crashed this with
    # AttributeError on the very first real run after order_by_staleness started stamping
    # last_resolved|<comp> (confirmed 2026-11-02, fixed by giving that entry the same {'fetched_at':
    # ...} shape as everything else) -- kept here too so the SAME mistake in a future addition to
    # this dict fails to prune that one entry, not the whole run.
    pruned = {k: v for k, v in cache.items() if isinstance(v, dict) and v.get('fetched_at', 0) > cutoff}
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
            # The real tournament end, kept rather than thrown away. `end` was already being parsed
            # here (it bounds the fixture match just above) but never left this function, so the app
            # had no idea a four-day tournament wasn't a two-hour one. This is the single most
            # authoritative duration available anywhere in this file -- ESPN publishes the actual
            # startDate/endDate spanning every round. See event_end_ts for how it's prioritised.
            if end and end > f['kick_ts']:
                f['end_ts'] = max(f.get('end_ts') or 0, end)
            # Owner-reported 2026-11-11: TheSportsDB's own strEvent (this fixture's 'label') had this
            # exact tournament as "Good Good Championship" -- ESPN's real live scoreboard, confirmed by
            # hand against the same date, calls it "Austin Championship pres. by Compliance Solutions".
            # Sponsor-renamed tournaments are common on the PGA/European/LIV tours and TheSportsDB's
            # crowd-sourced name lags behind; ESPN's name is coming from the same live call already being
            # made for its broadcasts[] data, so this is free -- no extra request, no extra source to
            # trust, just using the fresher of two names already in hand for exactly the one event
            # already matched above. Never applied for 'daily'/'teams' kinds (NASCAR, NBA...) -- those
            # already have real per-fixture home/away team names, nothing to improve.
            #
            # The round suffix is preserved, not dropped: unlike PGA Tour (one fixture per whole
            # tournament), European Tour arrives from TheSportsDB as one fixture PER ROUND ("DP WORLD
            # TOUR CHAMPIONSHIP DUBAI Round 1", "...Round 2"...), and ESPN's own event name here is for
            # the whole tournament, no round in it -- a bare overwrite would make all four rounds
            # identical, losing real information rather than fixing a stale name.
            if ev.get('name'):
                m = re.search(r'\b(Round\s*\d+|Final Round)\s*$', f['label'] or '', re.I)
                f['label'] = ev['name'] + (' ' + m.group(1) if m else '')


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


def resolve_league(league, cache, xmltv=None):
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
            # Keyed by SPORT, not comp -- eventstv.php's own URL (see fetch_tsdb_tv_day) is built only
            # from (day, country, sport); it never includes which competition is asking. A comp-keyed
            # cache meant every one of the 24 competitions sharing tsdb_sport='Soccer' independently
            # refetched the identical country x day grid, 24 times over, for a call that was already
            # byte-for-byte identical across all of them. Confirmed by hand 2026-11-02: the URL build
            # genuinely has no comp in it at all. Pure dedup, zero change to what gets matched -- the
            # listings pool content is the same regardless of which comp's run happens to fetch it
            # first; only the FETCH is shared now, not the per-comp matching against it below.
            cache_key = '%s|%s|%s' % (league['tsdb_sport'], cc, day)
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

    # Applies to every league, not just the five TheSportsDB's eventstv.php has nothing for -- this is
    # real per-fixture programme data (a genuine text match against this specific event, not a standing-
    # rights guess), so it's just as valid corroborating evidence for Premier League or the NBA as it is
    # for NASCAR. Still tagged 'likely', same as everything else here.
    if xmltv:
        for f in fixtures:
            f['channels'].extend(xmltv_channels_for_fixture(f, xmltv, now_iso))

    # ausportguide.com -- see its own comment block above fetch_ausportguide_page for what/why.
    ag_path = AUSPORTGUIDE_PATHS.get(league['tsdb_sport']) or \
        (AUSPORTGUIDE_RUGBY_LEAGUE_PATH if league['comp'] in AUSPORTGUIDE_RUGBY_LEAGUE_COMPS else None)
    if ag_path:
        ag_events = fetch_ausportguide_page(ag_path, cache)
        for f in fixtures:
            f['channels'].extend(ausportguide_channels_for_fixture(f, ag_events, now_iso))

    # tvsports.in -- see its own comment block above fetch_tvsports_page for what/why.
    tvs_path = TVSPORTS_PATHS.get(league['tsdb_sport'])
    if tvs_path:
        tvs_events = fetch_tvsports_page(tvs_path, cache)
        for f in fixtures:
            f['channels'].extend(tvsports_channels_for_fixture(f, tvs_events, now_iso))

    # Confidence tiers, added 2026-11-10 (Event Hub plan item 1): every source above already tags its
    # own channel rows 'likely' and names its own source ('thesportsdb'/'espn'/'xmltv'), but until now
    # nothing ever checked whether two of them independently landed on the SAME channel for the SAME
    # fixture -- that agreement is real corroborating evidence (this file's own module docstring has
    # always described "confirmed" as needing exactly that: two independent sources agreeing), it just
    # was never actually computed. Grouped by a normalised channel-name slug (reusing _slug(), already
    # used for comp-name matching below) rather than the raw name, since the same channel can arrive
    # from two sources with slightly different formatting ("Sky Sports Main Event" vs "SKY SPORTS MAIN
    # EVENT HD"); deliberately exact-slug rather than fuzzy, so this only upgrades confidence on a real,
    # tight match, never guesses two differently-named channels are "the same" to manufacture agreement.
    for f in fixtures:
        by_slug = {}
        for c in f['channels']:
            by_slug.setdefault(_slug(c['channel']), []).append(c)
        for rows in by_slug.values():
            if len({c['source'] for c in rows}) >= 2:
                for c in rows:
                    c['confidence'] = 'confirmed'

    # 'end' is published for every event, so the app never has to guess a duration again (see
    # EVENT_HOURS_BY_SPORT). Computed last, after espn_channels_tournament has had its chance to
    # attach a real end_ts.
    for f in fixtures:
        f['end'] = datetime.fromtimestamp(event_end_ts(f, league['tsdb_sport']), tz=timezone.utc).isoformat()
        f.setdefault('timed', True)
        del f['kick_ts']
        f.pop('end_ts', None)
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


def order_by_staleness(leagues, cache):
    """Rotates by staleness instead of a fixed order: whichever competition was least recently
    resolved goes first, so a deadline/rate-limit cutoff never permanently starves whatever happens to
    sit near the back of the list -- the thing that got cut off THIS run has the oldest timestamp and
    sorts first NEXT run, so every competition gets a turn across consecutive runs. (A 2026-11-02 fix
    attempt hardcoded NASCAR/MotoGP/PGA Tour/European Tour/Eredivisie at the front of LEAGUES instead
    -- reverted before it even shipped: that would only have pushed the exact same starvation onto
    whatever now sits at the back.) A competition with no cache entry yet (brand new, or the cache was
    just reset) sorts first of all, so it gets its first real run immediately rather than waiting
    behind everything else. Stamped in resolve() below via the same cache dict the checkpoint already
    persists, so this needs no separate storage. Stored as {'fetched_at': ...}, same shape as every
    other cache entry -- save_cache()'s own pruning assumes every value is dict-shaped and calls
    v.get('fetched_at', 0) on all of them; a bare float here crashed that with AttributeError on the
    very first real run (confirmed 2026-11-02)."""
    return sorted(leagues, key=lambda league: cache.get('last_resolved|' + league['comp'], {}).get('fetched_at', 0))


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
    leagues = order_by_staleness(leagues_matching(only), cache)
    # Loaded once here, not inside resolve_league() -- these are multi-MB downloads; fetching them
    # once per resolve() run and sharing the result across every league is the whole point (and why
    # fetch_xmltv_programmes' own cache keys by country, not by league).
    xmltv = load_xmltv(cache)
    old_handler = signal.signal(signal.SIGTERM, _raise_interrupted)
    try:
        for league in leagues:
            if deadline and time.time() > deadline:
                print('Soft deadline reached -- stopping before %s so the publish steps still run.' % league['comp'], file=sys.stderr)
                break
            try:
                got = resolve_league(league, cache, xmltv)
            except Interrupted:
                print('Interrupted during %s -- keeping what already resolved.' % league['comp'], file=sys.stderr)
                break
            print('%s: %d fixtures resolved' % (league['comp'], len(got)), file=sys.stderr)
            events.extend(got)
            resolved.append(league['comp'])
            # Stamped regardless of whether got is empty -- fairness is about "was this league
            # attempted this run" (it already spent its share of the TheSportsDB/ESPN budget either
            # way), not "did it happen to find anything". Dict-shaped, not a bare float -- see
            # order_by_staleness's own comment on why that matters to save_cache().
            cache['last_resolved|' + league['comp']] = {'fetched_at': time.time()}
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
