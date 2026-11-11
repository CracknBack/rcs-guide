#!/usr/bin/env python3
"""EPG-first guide builder.

WHY THIS EXISTS
---------------
resolve_v2.py is fixture-first: it starts from TheSportsDB/football-data fixtures and tries to glue TV
channels onto them. Measured against its own live output (2026-11-11): 44.8% of published events have
no channel at all, 15% carry a fake 00:00:00 start time, and only 5.5% ever reach "confirmed". The
fixture is the wrong spine, because a fixture is a thing that exists in the world, not a thing you can
watch.

This module inverts it. An XMLTV programme on a real broadcaster already *is* the three things the
fixture pipeline kept failing to produce: a guaranteed channel, a real start time and a real stop time.
Measured on the same nine feeds: 5,087 sports programmes in a 7-day window -> 2,834 after dedupe,
against ~296 watchable events from the fixture pipeline. Roughly ten times the content, all of it
actually on television.

WHAT IT DOES NOT DO
-------------------
It does not touch resolve_v2.py and does not write guide-v2.json. That file keeps being produced by
untouched code so the shipped TV app cannot regress while this is proven out. This module only adds
public/guides/*.json alongside it.

THE TWO-JOB RULE (the single most important thing in here)
----------------------------------------------------------
<title>/<category>/channel decide WHICH SPORT. <sub-title>/<desc> decide WHICH MATCH. Never the other
way round. A first pass that classified sport from the description produced "The Addams Family" ->
Baseball, "Better Homes And Gardens" -> Golf and "Deal Or No Deal" -> Motorsport, because descriptions
mention sports in passing. Classification must stay narrow; identity extraction can be wide, but only
once the sport is already known. Measured gain from reading sub-title/desc for identity only: matches
with an identifiable line-up go from 11% to 35%.
"""
import collections, datetime, gzip, json, os, re, sys, urllib.request
import xml.etree.ElementTree as ET

UA = {'User-Agent': 'rcs-guide-epg-first/1'}
XMLTV_BASE = 'https://epgshare01.online/epgshare01/epg_ripper_%s.xml.gz'
# Checked by hand 2026-11-11: EUROSPORT1 and OPTUSSPORTS1 both 404 on epgshare01 (the TV app has been
# requesting both for a while and silently getting nothing). IE1 and ZA1 do exist and were never used
# -- IE1 turned out to carry more sport than any other feed in the dry run.
FEEDS = ['AU1', 'UK1', 'US2', 'CA2', 'NZ1', 'IN1', 'ZA1', 'IE1', 'BEIN1']
HORIZON_DAYS = 8
INDEX_AHEAD_HOURS = 24   # how far index.json's cross-sport "All Sports" view reaches

# ---------------------------------------------------------------------------------------------
# Canonical broadcasters
# ---------------------------------------------------------------------------------------------
# One id per real broadcaster, not per regional feed. The AU free-to-air networks publish a separate
# XMLTV channel per city ("7mate Perth", "7mate Sydney", ...), which made one Rugby League World Cup
# match appear five times in the dry run -- 45% of all rows were duplicates, and most of that was
# regional rather than cross-network. Collapsing at ingest kills it before it reaches the client.
#
# Order matters: specific before generic, so "Fox Cricket" is not swallowed by "Fox Sports".
CANONICAL = [
    # ---- Australia: Foxtel/Kayo ----
    ('au:fox_league',      'AU', 'Fox League',        r'fox\s*league'),
    ('au:fox_cricket',     'AU', 'Fox Cricket',       r'fox\s*cricket'),
    ('au:fox_footy',       'AU', 'Fox Footy',         r'fox\s*footy'),
    ('au:fox_sports_more', 'AU', 'Fox Sports More',   r'fox\s*sports?\s*more'),
    ('au:fox_sports_news', 'AU', 'Fox Sports News',   r'fox\s*sports?\s*news'),
    ('au:fox_sports_501',  'AU', 'Fox Sports 501',    r'fox\s*sports?\s*501'),
    ('au:fox_sports_502',  'AU', 'Fox Sports 502',    r'fox\s*sports?\s*502'),
    ('au:fox_sports_503',  'AU', 'Fox Sports 503',    r'fox\s*sports?\s*503'),
    ('au:fox_sports_504',  'AU', 'Fox Sports 504',    r'fox\s*sports?\s*504'),
    ('au:fox_sports_505',  'AU', 'Fox Sports 505',    r'fox\s*sports?\s*505'),
    ('au:fox_sports_506',  'AU', 'Fox Sports 506',    r'fox\s*sports?\s*506'),
    ('au:fox_sports_507',  'AU', 'Fox Sports 507',    r'fox\s*sports?\s*507'),
    ('au:kayo',            'AU', 'Kayo Sports',       r'\bkayo\b'),
    ('au:stan_sport',      'AU', 'Stan Sport',        r'stan\s*sport'),
    ('au:optus_sport',     'AU', 'Optus Sport',       r'optus\s*sport'),
    ('au:espn',            'AU', 'ESPN (AU)',         r'^espn$|^espn\s*au'),
    ('au:espn2',           'AU', 'ESPN2 (AU)',        r'^espn\s*2'),
    ('au:bein_1',          'AU', 'beIN Sports 1 (AU)', r'bein\s*sports?\s*1'),
    ('au:bein_2',          'AU', 'beIN Sports 2 (AU)', r'bein\s*sports?\s*2'),
    ('au:bein_3',          'AU', 'beIN Sports 3 (AU)', r'bein\s*sports?\s*3'),
    ('au:sky_racing',      'AU', 'Sky Racing',        r'sky\s*racing|sky\s*thoroughbred'),
    ('au:racing_com',      'AU', 'Racing.com',        r'racing\.?com'),
    # ---- Australia: free-to-air ----
    ('au:7mate',   'AU', '7mate',        r'^7\s*mate'),
    ('au:7two',    'AU', '7two',         r'^7\s*two'),
    ('au:7flix',   'AU', '7flix',        r'^7\s*flix'),
    ('au:7bravo',  'AU', '7Bravo',       r'^7\s*bravo'),
    ('au:seven',   'AU', 'Seven',        r'^(channel\s*)?7$|^seven$'),
    ('au:9gem',    'AU', '9Gem',         r'^9\s*gem'),
    ('au:9go',     'AU', '9Go!',         r'^9\s*go'),
    ('au:9rush',   'AU', '9Rush',        r'^9\s*rush'),
    ('au:nine',    'AU', 'Nine',         r'^(channel\s*)?9$|^nine$'),
    ('au:10bold',  'AU', '10 Bold',      r'^10\s*bold'),
    ('au:10peach', 'AU', '10 Peach',     r'^10\s*peach'),
    ('au:ten',     'AU', 'Ten',          r'^(channel\s*)?10$|^ten$|^network\s*ten'),
    ('au:abc',     'AU', 'ABC',          r'^abc\s*(tv)?$|^abc\s*entertains'),
    ('au:sbs',     'AU', 'SBS',          r'^sbs$|^sbs\s*viceland'),
    # ---- UK ----
    ('uk:sky_main_event',     'UK', 'Sky Sports Main Event',   r'sky\s*sp\w*\s*main'),
    ('uk:sky_premier_league', 'UK', 'Sky Sports Premier League', r'sky\s*sp\w*\s*(premier|pl\b)'),
    ('uk:sky_football',       'UK', 'Sky Sports Football',     r'sky\s*sp\w*\s*(football|fball)'),
    ('uk:sky_cricket',        'UK', 'Sky Sports Cricket',      r'sky\s*sp\w*\s*cricket'),
    ('uk:sky_golf',           'UK', 'Sky Sports Golf',         r'sky\s*sp\w*\s*golf'),
    ('uk:sky_f1',             'UK', 'Sky Sports F1',           r'sky\s*sp\w*\s*f1'),
    ('uk:sky_action',         'UK', 'Sky Sports Action',       r'sky\s*sp\w*\s*action'),
    ('uk:sky_arena',          'UK', 'Sky Sports Arena',        r'sky\s*sp\w*\s*arena'),
    ('uk:sky_tennis',         'UK', 'Sky Sports Tennis',       r'sky\s*sp\w*\s*tennis'),
    ('uk:sky_racing',         'UK', 'Sky Sports Racing',       r'sky\s*sp\w*\s*racing'),
    ('uk:sky_mix',            'UK', 'Sky Sports Mix',          r'sky\s*sp\w*\s*mix'),
    ('uk:sky_plus',           'UK', 'Sky Sports+',             r'sky\s*sp\w*\s*\+|sky\s*sports\s*plus'),
    ('uk:sky_nfl',            'UK', 'Sky Sports NFL',          r'sky\s*sp\w*\s*nfl'),
    ('uk:sky_box_office',     'UK', 'Sky Sports Box Office',   r'box\s*off'),
    ('uk:tnt_1', 'UK', 'TNT Sports 1', r'tnt\s*sports?\s*1'),
    ('uk:tnt_2', 'UK', 'TNT Sports 2', r'tnt\s*sports?\s*2'),
    ('uk:tnt_3', 'UK', 'TNT Sports 3', r'tnt\s*sports?\s*3'),
    ('uk:tnt_4', 'UK', 'TNT Sports 4', r'tnt\s*sports?\s*4'),
    ('uk:premier_sports_1', 'UK', 'Premier Sports 1', r'premier\s*sports?\s*1'),
    ('uk:premier_sports_2', 'UK', 'Premier Sports 2', r'premier\s*sports?\s*2'),
    ('uk:bbc_one',   'UK', 'BBC One',   r'^bbc\s*(one|1)\b'),
    ('uk:bbc_two',   'UK', 'BBC Two',   r'^bbc\s*(two|2)\b'),
    ('uk:bbc_three', 'UK', 'BBC Three', r'^bbc\s*(three|3)\b'),
    ('uk:bbc_four',  'UK', 'BBC Four',  r'^bbc\s*(four|4)\b'),
    ('uk:itv1', 'UK', 'ITV1', r'^itv\s*1\b|^itv$'),
    ('uk:itv2', 'UK', 'ITV2', r'^itv\s*2\b'),
    ('uk:itv3', 'UK', 'ITV3', r'^itv\s*3\b'),
    ('uk:itv4', 'UK', 'ITV4', r'^itv\s*4\b'),
    ('uk:channel4', 'UK', 'Channel 4', r'^channel\s*4|^c4\b|^e4\b'),
    ('uk:channel5', 'UK', 'Channel 5', r'^channel\s*5|^c5\b'),
    ('uk:s4c',      'UK', 'S4C',       r'^s4c'),
    ('uk:eurosport_1', 'UK', 'Eurosport 1', r'eurosport\s*1'),
    ('uk:eurosport_2', 'UK', 'Eurosport 2', r'eurosport\s*2'),
    ('uk:racing_tv',   'UK', 'Racing TV',   r'racing\s*tv'),
    ('uk:dazn',        'UK', 'DAZN',        r'^dazn'),
    ('uk:laliga_tv',   'UK', 'LaLiga TV',   r'laliga'),
    # ---- USA ----
    ('us:espn',      'US', 'ESPN',      r'^espn$|^espn\s*hd'),
    ('us:espn2',     'US', 'ESPN2',     r'^espn\s*2'),
    ('us:espnu',     'US', 'ESPNU',     r'^espn\s*u'),
    ('us:espnews',   'US', 'ESPNews',   r'^espn\s*news'),
    ('us:fs1',       'US', 'FS1',       r'^fs\s*1|fox\s*sports?\s*1\b'),
    ('us:fs2',       'US', 'FS2',       r'^fs\s*2|fox\s*sports?\s*2\b'),
    ('us:fox',       'US', 'FOX',       r'^fox$|^fox\s*broadcast'),
    ('us:cbs',       'US', 'CBS',       r'^cbs$'),
    ('us:cbs_sports','US', 'CBS Sports Network', r'cbs\s*sports'),
    ('us:nbc',       'US', 'NBC',       r'^nbc$'),
    ('us:usa',       'US', 'USA Network', r'^usa\s*network'),
    ('us:abc',       'US', 'ABC',       r'^abc$'),
    ('us:tnt',       'US', 'TNT',       r'^tnt$'),
    ('us:tbs',       'US', 'TBS',       r'^tbs$'),
    ('us:trutv',     'US', 'truTV',     r'^trutv'),
    ('us:golf',      'US', 'Golf Channel',   r'golf\s*channel'),
    ('us:tennis',    'US', 'Tennis Channel', r'tennis\s*channel'),
    ('us:mlb',       'US', 'MLB Network',    r'mlb\s*network'),
    ('us:nfl',       'US', 'NFL Network',    r'nfl\s*network'),
    ('us:nba_tv',    'US', 'NBA TV',         r'nba\s*tv'),
    ('us:nhl',       'US', 'NHL Network',    r'nhl\s*network'),
    ('us:willow',    'US', 'Willow Cricket', r'willow'),
    ('us:bein',      'US', 'beIN Sports (US)', r'bein\s*sports?'),
    ('us:racer',     'US', 'RACER Network',   r'racer'),
    ('us:sec',       'US', 'SEC Network',     r'sec\s*network'),
    ('us:acc',       'US', 'ACC Network',     r'acc\s*network'),
    ('us:big_ten',   'US', 'Big Ten Network', r'big\s*ten'),
    # ---- Canada ----
    ('ca:tsn1', 'CA', 'TSN1', r'^tsn\s*1'), ('ca:tsn2', 'CA', 'TSN2', r'^tsn\s*2'),
    ('ca:tsn3', 'CA', 'TSN3', r'^tsn\s*3'), ('ca:tsn4', 'CA', 'TSN4', r'^tsn\s*4'),
    ('ca:tsn5', 'CA', 'TSN5', r'^tsn\s*5'), ('ca:tsn',  'CA', 'TSN',  r'^tsn\b'),
    ('ca:sportsnet_one',     'CA', 'Sportsnet One',     r'sportsnet\s*one'),
    ('ca:sportsnet_360',     'CA', 'Sportsnet 360',     r'sportsnet\s*360'),
    ('ca:sportsnet_world',   'CA', 'Sportsnet World',   r'sportsnet\s*world'),
    ('ca:sportsnet',         'CA', 'Sportsnet',         r'^sportsnet'),
    ('ca:rds',       'CA', 'RDS',        r'^rds\b'),
    ('ca:tva_sports','CA', 'TVA Sports', r'tva\s*sports'),
    ('ca:cbc',       'CA', 'CBC',        r'^cbc\b'),
    ('ca:ctv',       'CA', 'CTV',        r'^ctv\b'),
    ('ca:rev_tv',    'CA', 'REV TV',     r'rev\s*tv'),
    # ---- New Zealand ----
    ('nz:sky_sport_1', 'NZ', 'Sky Sport 1 NZ', r'sky\s*sport\s*1'),
    ('nz:sky_sport_2', 'NZ', 'Sky Sport 2 NZ', r'sky\s*sport\s*2'),
    ('nz:sky_sport_3', 'NZ', 'Sky Sport 3 NZ', r'sky\s*sport\s*3'),
    ('nz:sky_sport_4', 'NZ', 'Sky Sport 4 NZ', r'sky\s*sport\s*4'),
    ('nz:sky_sport_5', 'NZ', 'Sky Sport 5 NZ', r'sky\s*sport\s*5'),
    ('nz:sky_sport_6', 'NZ', 'Sky Sport 6 NZ', r'sky\s*sport\s*6'),
    ('nz:sky_sport_7', 'NZ', 'Sky Sport 7 NZ', r'sky\s*sport\s*7'),
    ('nz:sky_sport_select', 'NZ', 'Sky Sport Select', r'sky\s*sport\s*select'),
    ('nz:sky_sport',   'NZ', 'Sky Sport NZ', r'^sky\s*sport'),
    ('nz:tvnz1', 'NZ', 'TVNZ 1', r'^tvnz\s*1'),
    ('nz:tvnz2', 'NZ', 'TVNZ 2', r'^tvnz\s*2'),
    ('nz:three', 'NZ', 'Three',  r'^three$'),
    ('nz:prime', 'NZ', 'Prime',  r'^prime'),
    # ---- India ----
    ('in:star_sports_1',      'IN', 'Star Sports 1',      r'star\s*sports?\s*1'),
    ('in:star_sports_2',      'IN', 'Star Sports 2',      r'star\s*sports?\s*2'),
    ('in:star_sports_3',      'IN', 'Star Sports 3',      r'star\s*sports?\s*3'),
    ('in:star_sports_select', 'IN', 'Star Sports Select', r'star\s*sports?\s*select'),
    ('in:star_sports',        'IN', 'Star Sports',        r'star\s*sports?'),
    ('in:sony_ten_1', 'IN', 'Sony Ten 1', r'sony\s*(sports\s*)?ten\s*1'),
    ('in:sony_ten_2', 'IN', 'Sony Ten 2', r'sony\s*(sports\s*)?ten\s*2'),
    ('in:sony_ten_3', 'IN', 'Sony Ten 3', r'sony\s*(sports\s*)?ten\s*3'),
    ('in:sony_ten_4', 'IN', 'Sony Ten 4', r'sony\s*(sports\s*)?ten\s*4'),
    ('in:sony_ten_5', 'IN', 'Sony Ten 5', r'sony\s*(sports\s*)?ten\s*5'),
    ('in:sony_liv',   'IN', 'Sony LIV',   r'sony\s*liv'),
    ('in:dd_sports',  'IN', 'DD Sports',  r'dd\s*sports'),
    # ---- South Africa ----
    ('za:supersport_grandstand', 'ZA', 'SuperSport Grandstand', r'supersport\s*grandstand'),
    ('za:supersport_football',   'ZA', 'SuperSport Football',   r'supersport\s*football'),
    ('za:supersport_rugby',      'ZA', 'SuperSport Rugby',      r'supersport\s*rugby'),
    ('za:supersport_cricket',    'ZA', 'SuperSport Cricket',    r'supersport\s*cricket'),
    ('za:supersport_action',     'ZA', 'SuperSport Action',     r'supersport\s*action'),
    ('za:supersport_variety',    'ZA', 'SuperSport Variety',    r'supersport\s*variety'),
    ('za:supersport_psl',        'ZA', 'SuperSport PSL',        r'supersport\s*(psl|premier)'),
    ('za:supersport',            'ZA', 'SuperSport',            r'^supersport'),
    ('za:sabc',                  'ZA', 'SABC',                  r'^sabc'),
    ('za:etv',                   'ZA', 'e.tv',                  r'^e\.?tv'),
    # ---- Ireland ----
    ('ie:rte1',   'IE', 'RTÉ One',  r'^rt[eé]\s*(one|1)\b'),
    ('ie:rte2',   'IE', 'RTÉ2',     r'^rt[eé]\s*(two|2)\b'),
    ('ie:virgin1','IE', 'Virgin Media One',   r'virgin\s*media\s*(one|1)\b'),
    ('ie:virgin2','IE', 'Virgin Media Two',   r'virgin\s*media\s*(two|2)\b'),
    ('ie:virgin3','IE', 'Virgin Media Three', r'virgin\s*media\s*(three|3)\b'),
    ('ie:virgin4','IE', 'Virgin Media Four',  r'virgin\s*media\s*(four|4)\b'),
    ('ie:tg4',    'IE', 'TG4',      r'^tg\s*4'),
    # ---- MENA (beIN) ----
    ('mena:bein_1', 'QA', 'beIN Sports 1', r'bein\s*sports?\s*1'),
    ('mena:bein_2', 'QA', 'beIN Sports 2', r'bein\s*sports?\s*2'),
    ('mena:bein_3', 'QA', 'beIN Sports 3', r'bein\s*sports?\s*3'),
    ('mena:bein_4', 'QA', 'beIN Sports 4', r'bein\s*sports?\s*4'),
    ('mena:bein',   'QA', 'beIN Sports',   r'bein'),
    ('mena:alkass', 'QA', 'Alkass',        r'alkass'),
]
# Which canonical ids a given feed is allowed to resolve to.
# BEIN1 is MENA-only (confirmed by hand: all 80 of its channels are Arabic/English dubs of the same
# Qatar feed, no AU/FR/US variants) so it must never resolve to au:bein_1 and claim an Australian
# schedule -- that exact cross-region confusion was a real bug in the fixture pipeline.
# IE carries the UK sports networks for real (Sky Sports, TNT, Premier Sports are all on Irish Sky and
# Virgin), so IE1 is scoped to both. Confirmed the hard way: scoping it to 'ie:' alone silently
# dropped 3,597 programmes, Ireland being the single richest feed of the nine.
FEED_SCOPE = {'AU1': ('au:',), 'UK1': ('uk:',), 'US2': ('us:',), 'CA2': ('ca:',), 'NZ1': ('nz:',),
              'IN1': ('in:',), 'ZA1': ('za:',), 'IE1': ('ie:', 'uk:'), 'BEIN1': ('mena:',)}
CANON_RE = [(cid, cc, name, re.compile(p, re.I)) for cid, cc, name, p in CANONICAL]

# city/region and quality suffixes stripped before matching, so "7mate Perth HD" -> "7mate"
REGION_RE = re.compile(
    r'\b(perth|sydney|melbourne|brisbane|adelaide|hobart|darwin|canberra|nsw|qld|vic|wa|sa|tas|act|nt|'
    r'london|scotland|wales|north(ern)?\s*ireland|ni|east|west|north|south|central|'
    r'hd|uhd|fhd|sd|4k|8k|raw|\+1|\+2)\b', re.I)


def canonical_id(feed, raw_name):
    """Map a feed's channel name to one canonical broadcaster id, or None to drop it."""
    base = REGION_RE.sub(' ', raw_name or '')
    base = re.sub(r'[._]+', ' ', base)
    base = re.sub(r'\s+', ' ', base).strip()
    scope = FEED_SCOPE.get(feed, ())
    for cid, cc, name, rx in CANON_RE:
        if cid.startswith(scope) and rx.search(base):
            return cid, name, cc
    return None


# ---------------------------------------------------------------------------------------------
# Sport classification -- title + category + channel ONLY. See the two-job rule in the docstring.
# ---------------------------------------------------------------------------------------------
# Every token word-bounded. An unbounded `efl` previously matched "Cr(efl)o Dollar Ministries" and
# classified a religious programme as football; \bafl\b does NOT match "AFLW", so suffixed variants
# are spelled out. Order matters -- specific codes before the generic words that would swallow them.
SPORT_RULES = [
 # These three go FIRST. Their names are unambiguous, and several of them appear inside competition
 # names belonging to other sports -- "PDC ANZ Premier League Darts" was being filed under Soccer,
 # because the soccer rule's "premier league" was tested before darts was considered at all.
 ('darts',        r'\bdarts\b|\bpdc\b'),
 ('snooker',      r'\bsnooker\b'),
 ('netball',      r'\bnetball\b'),
 ('cricket',      r'\bcricket\b|\bodi\b|\bt20\b|\btest\s*(match|series)\b|\bbig\s*bash\b|\bipl\b|'
                  r'\bthe\s*hundred\b|\bsheffield\s*shield\b|\bw?bbl\b'),
 ('rugby_league', r'\brugby\s*league\b|\bnrlw?\b|\bsuper\s*league\b|\bstate\s*of\s*origin\b|\brlwc\b'),
 ('afl',          r'\baflw?\b|\baustralian\s*rules\b'),
 ('am_football',  r'\bamerican\s*football\b|\bnfl\b|\bncaa\s*football\b|\bcollege\s*football\b|\bsuper\s*bowl\b'),
 ('soccer',       r'\bfootball\b(?!.*\b(american|australian|gaelic)\b)|\bsoccer\b|\bpremier\s*league\b|'
                  r'\bchampions\s*league\b|\beuropa\s*(league|conference)\b|\bla\s*liga\b|\bserie\s*a\b|'
                  r'\bbundesliga\b|\bligue\s*1\b|\bfa\s*cup\b|\befl\b|\bmls\b|\beredivisie\b|\ba-?league\b|\bwsl\b'),
 ('rugby_union',  r'\brugby\s*union\b|\bsix\s*nations\b|\bsuper\s*rugby\b|\btop\s*14\b|\burc\b|'
                  r'\bpremiership\s*rugby\b|\brugby\b'),
 ('golf',         r'\bgolf\b|\bpga\b|\blpga\b|\bdp\s*world\s*tour\b|\bryder\s*cup\b'),
 ('tennis',       r'\btennis\b|\batp\b|\bwta\b|\bwimbledon\b|\broland\s*garros\b|\bdavis\s*cup\b'),
 ('motorsport',   r'\bformula\s*[12e]\b|\bf1\b|\bmotogp\b|\bmoto[23]\b|\bnascar\b|\bindycar\b|\bsupercars\b|'
                  r'\bsuperbikes?\b|\bwrc\b|\brally\b|\bgrand\s*prix\b|\bspeedway\b|\bmotocross\b'),
 ('basketball',   r'\bbasketball\b|\bw?nba\b|\beuroleague\b|\bw?nbl\b'),
 ('ice_hockey',   r'\bice\s*hockey\b|\bnhl\b|\bkhl\b|\biihf\b'),
 ('baseball',     r'\bbaseball\b|\bmlb\b|\bworld\s*series\b'),
 ('boxing_mma',   r'\bboxing\b|\bufc\b|\bmma\b|\bbare\s*knuckle\b|\bbkfc\b|\bfight\s*night\b|\btitle\s*fight\b'),
 ('cycling',      r'\bcycling\b|\btour\s*de\s*france\b|\bgiro\b|\bvuelta\b|\bcyclocross\b|\buci\b'),
 ('racing',       r'\bhorse\s*racing\b|\bharness\s*racing\b|\bgreyhound\b|\braceday\b'),
]
SPORT_RE = [(n, re.compile(p, re.I)) for n, p in SPORT_RULES]
SPORT_LABEL = {'cricket': 'Cricket', 'rugby_league': 'Rugby League', 'afl': 'AFL',
               'am_football': 'American Football', 'soccer': 'Soccer', 'rugby_union': 'Rugby Union',
               'golf': 'Golf', 'tennis': 'Tennis', 'motorsport': 'Motorsport',
               'basketball': 'Basketball', 'ice_hockey': 'Ice Hockey', 'baseball': 'Baseball',
               'boxing_mma': 'Boxing & MMA', 'cycling': 'Cycling', 'darts': 'Darts',
               'snooker': 'Snooker', 'netball': 'Netball', 'racing': 'Racing'}
# shape is informational for the client (EPG already gives a real start and stop, so nothing depends
# on it for duration) -- it describes what one event IS, for display and grouping.
SPORT_SHAPE = {'golf': 'multiday', 'cricket': 'session', 'motorsport': 'weekend_session',
               'boxing_mma': 'card', 'cycling': 'stage', 'tennis': 'session', 'racing': 'meeting'}

SPORTS_CAT = re.compile(r'sport', re.I)
# A dedicated sports channel makes a title keyword trustworthy on its own. A general/FTA channel does
# not, so those additionally need the feed's own <category>Sport</category>.
DEDICATED = re.compile(r'^(au:(fox|kayo|stan|optus|espn|bein|sky_racing|racing)|uk:(sky|tnt|premier|eurosport|racing|dazn|laliga)|'
                       r'us:(espn|fs\d|cbs_sports|golf|tennis|mlb|nfl|nba|nhl|willow|bein|racer|sec|acc|big_ten)|'
                       r'ca:(tsn|sportsnet|rds|tva|rev)|nz:sky|in:(star|sony|dd)|za:supersport|'
                       r'ie:premier|mena:)')

# Studio/filler programming that is not a watchable event. Duration is a SIGNAL here, never a cutoff
# on its own -- a hard "< 40 minutes" rule would also discard genuine short sessions (qualifying,
# a darts session, a criterium).
SHOW_RE = re.compile(r'\bhighlights?\b|\bclassic\b|\brewind\b|\breview\b|\bmagazine\b|\bpreviews?\b|'
                     r'press\s*conference|\bnews\b|documentary|\bbest\s*of\b|\btop\s*\d+\b|countdown|'
                     r'\banalysis\b|\bweekly\b|round\s*-?up|\bthis\s*week\b|\bbuild-?up\b|\bverdict\b|'
                     r'\bdaily\b|\bshow\b|\bextra\s*time\b|\bovertime\b|\btonight\b|\bwrap\b|\bmini\b|'
                     r'\bupcoming\b|\bcoming\s*up\b|\bgoals\b|\btalk\b|\bdebate\b|'
                     # archive/nostalgia strands. <previously-shown> only catches what the broadcaster
                     # bothered to tag, and these are not tagged: confirmed real leaks were "UFC Fight
                     # Flashback", "Darts Gold: Priestley v Wilson" (a 1990s match), "Legends of Darts",
                     # "2000's Greatest Games" and "Golf Academy" (instructional, not an event).
                     r'\bflashback\b|\blook\s*back\b|\bvault\b|\bretro\b|\bgreatest\b|\blegends?\b|'
                     r'\bacademy\b|\bmasterclass\b|\bgold\b|\bencore\b|\barchive\b|\bre-?run\b|'
                     r'\bfrom\s+(19|20)\d{2}\b|\b(19|20)\d{2}\s+(final|classic)\b', re.I)
LIVE_HINT = re.compile(r'^\s*(live|lve)\b|\blive\b', re.I)

# Named studio strands that a "LIVE" prefix must NOT rescue. The live_hint exemption above exists so a
# genuine short session ("LIVE: Champion of Champions - D2 NS") survives the filler filter, but US
# networks brand their studio output as live too: "Live: Golf Central", "Live: MLB Now", "Live: MLB
# Tonight", "Live: NHL Post-Game on TNT", "NFL Tailgate", "NHL on TNT Face Off", "NFL Films Presents",
# "PGA Tour Champions Learning Center - Episode 2". These were surviving as events AND, once the
# overlap merge landed, accumulating broadcasters -- a studio show on four channels looks exactly like
# a well-covered fixture.
STUDIO_HARD = re.compile(r'golf\s*central|learning\s*cent(er|re)|\bepisode\b|\bthe\s*cut\b|'
                         r'how\s*good\s*is|\btailgate\b|\bface\s*-?off\b|\bfilms\s*presents\b|'
                         r'\b(post|pre)\s*-?\s*(game|match|show)\b|\bthe\s*point\b|\bheaven\b|'
                         r'\b(now|tonight|central|today)\s*$', re.I)

# identity: "A v B" / "A vs B", optionally prefixed by a round ("Semi Final: Australia v Samoa")
IDENTITY_RE = re.compile(r'^(?:(?P<round>[^:]{3,40}):\s*)?(?P<a>[^:]{2,48}?)\s+(?:v|vs\.?|versus)\s+(?P<b>[^:(]{2,48})\s*$', re.I)
# North American feeds never write "v"/"vs" -- they write "Away at Home" or "Away @ Home"
# ("Kansas City Chiefs at Buffalo Bills"), which is why US2/CA2 produced zero matchups.
# "at" is a real English word, so it is only trusted under three guards together: a US/CA row, one of
# the team sports that actually uses the convention, and both sides starting capitalised. Without the
# sport guard "Golf at Pebble Beach" and "NASCAR at Phoenix" parse as matchups; without the
# capitalisation guard, so does "Live at the Apollo".
AT_IDENTITY_RE = re.compile(r'^(?:(?P<round>[^:]{3,40}):\s*)?(?P<a>[^:@]{2,48}?)\s+at\s+(?P<b>[^:(@]{2,48})\s*$', re.I)
AMP_IDENTITY_RE = re.compile(r'^(?:(?P<round>[^:]{3,40}):\s*)?(?P<a>[^:@]{2,48}?)\s*@\s*(?P<b>[^:(@]{2,48})\s*$')
# Soccer is deliberately NOT here: it never uses the "at" notation, and including it would let
# "Soccer at Wembley" through both other guards.
AT_SPORTS = {'am_football', 'basketball', 'ice_hockey', 'baseball'}
AT_COUNTRIES = {'US', 'CA'}
SIDE_CAPS = re.compile(r'^[A-Z0-9]')
# a side that is just a sport or a filler noun is a venue/billing line, not a team: "Baseball at
# Fenway", "Live at the Apollo", "Hockey at the Olympics"
SIDE_GENERIC = re.compile(r'^(golf|tennis|soccer|football|baseball|basketball|hockey|ice\s*hockey|'
                          r'nascar|racing|cricket|rugby|boxing|darts|live|highlights|action|'
                          r'coverage|game|match)$', re.I)

# Where US matchups ACTUALLY live. Measured on US2+CA2: of 426 rows naming NFL/NBA/NHL/MLB, only 40
# have a <sub-title> at all and just 7 of those hold a matchup -- but 425 have a <desc>, and ~87 state
# the fixture in prose: "The Buffalo Sabres host the Tampa Bay Lightning in NHL regular-season play".
# Anchored at the start of the first sentence on purpose: unanchored, "Ant and Dec host Saturday Night
# Takeaway" parses as a fixture. No re.I -- the capitalisation IS the team-name signal.
_TEAM = r"[A-Z][\w.&'-]*(?:\s+(?:of\s+|de\s+)?[A-Z][\w.&'-]*){0,3}"
DESC_PROSE = [
    # "A hosts B" -> A is at home
    (re.compile(r"^(?:[Tt]he\s+)?(" + _TEAM + r")\s+(?:hosts?|welcomes?|entertains?)\s+"
                r"(?:the\s+)?(" + _TEAM + r")"), False),
    # "A visits B" -> B is at home, so the sides swap
    (re.compile(r"^(?:[Tt]he\s+)?(" + _TEAM + r")\s+(?:visits?|travels?\s+to|heads?\s+to)\s+"
                r"(?:the\s+)?(" + _TEAM + r")"), True),
    # neutral verbs carry no venue information; keep the stated order
    (re.compile(r"^(?:[Tt]he\s+)?(" + _TEAM + r")\s+(?:takes?\s+on|faces?|meets?|battles?)\s+"
                r"(?:the\s+)?(" + _TEAM + r")"), False),
]
JUNK_SIDE = re.compile(r'^(tba|tbc|tbd|the|a|\d+)$', re.I)


def classify_sport(title):
    for n, rx in SPORT_RE:
        if rx.search(title):
            return n
    return None


def extract_identity(sub, desc, sport=None, country=None):
    """Teams/line-up from <sub-title>, then the first sentence of <desc>. Only ever called after the
    sport is already decided -- see the two-job rule."""
    # (regex, enforce capitalised/non-generic sides, swap sides so home comes first)
    forms = [(IDENTITY_RE, False, False)]
    if sport in AT_SPORTS:
        # "Away @ Home" / "Away at Home": the SECOND side is the home team, so these swap.
        forms.append((AMP_IDENTITY_RE, True, True))
        if country in AT_COUNTRIES:
            forms.append((AT_IDENTITY_RE, True, True))
    for n, (text, is_desc) in enumerate(((sub or '', False), ((desc or '').split('.')[0], True))):
        t = re.sub(r'\s+', ' ', text).strip()
        if not t:
            continue
        trials = list(forms)
        if is_desc and sport not in TOURNAMENT_SPORTS:
            trials += [(rx, True, swap) for rx, swap in DESC_PROSE]
        for rx, strict, swap in trials:
            m = rx.match(t)
            if not m:
                continue
            g = m.groupdict()
            a = (g.get('a') or m.group(1)).strip()
            b = (g.get('b') or m.group(2)).strip()
            if JUNK_SIDE.match(a) or JUNK_SIDE.match(b):
                continue
            if strict and not (SIDE_CAPS.match(a) and SIDE_CAPS.match(b)):
                continue
            if strict and (SIDE_GENERIC.match(a) or SIDE_GENERIC.match(b)):
                continue
            if swap:
                a, b = b, a
            return a, b, (g.get('round') or '').strip() or None
    return None, None, None


def parse_ts(s):
    if not s or len(s) < 14:
        return None
    try:
        dt = datetime.datetime.strptime(s[:14], '%Y%m%d%H%M%S')
        off = s[14:].strip()
        if off and len(off) >= 5:
            sign = 1 if off[0] == '+' else -1
            dt -= sign * datetime.timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
        return dt.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def fetch_feed(code, cache_dir):
    path = os.path.join(cache_dir, code + '.xml')
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    req = urllib.request.Request(XMLTV_BASE % code, headers=UA)
    with urllib.request.urlopen(req, timeout=240) as r:
        raw = r.read()
    with open(path, 'wb') as f:
        f.write(gzip.decompress(raw))
    return path


def read_feed(code, cache_dir, now, horizon):
    """-> (programmes, canonical channels seen). One pass, streaming, memory-flat."""
    try:
        path = fetch_feed(code, cache_dir)
    except Exception as e:
        print('%s: download failed (%s)' % (code, e), file=sys.stderr)
        return [], {}
    chan, seen = {}, {}
    out = []
    for _, el in ET.iterparse(path, events=('end',)):
        if el.tag == 'channel':
            cid = el.get('id') or ''
            names = [d.text or '' for d in el.findall('display-name')]
            hit = None
            for n in names + [cid]:
                hit = canonical_id(code, n)
                if hit:
                    break
            if hit:
                chan[cid] = hit
                seen[hit[0]] = {'id': hit[0], 'name': hit[1], 'country': hit[2]}
            el.clear()
        elif el.tag == 'programme':
            hit = chan.get(el.get('channel') or '')
            if hit:
                st, sp = parse_ts(el.get('start')), parse_ts(el.get('stop'))
                if st and sp and sp > now and st < horizon and sp > st:
                    out.append({
                        'bid': hit[0], 'country': hit[2],
                        'title': (el.findtext('title') or '').strip(),
                        'sub': (el.findtext('sub-title') or '').strip(),
                        'desc': (el.findtext('desc') or '').strip(),
                        'cats': [c.text or '' for c in el.findall('category')],
                        'replay': el.find('previously-shown') is not None,
                        'start': st, 'stop': sp,
                    })
            el.clear()
    return out, seen


def build_events(raw):
    """Classify, filter out studio/filler, extract identity. Returns kept events + reject counters."""
    kept, rej = [], collections.Counter()
    for p in raw:
        title = p['title']
        if not title:
            rej['no title'] += 1
            continue
        sport = classify_sport(title)
        if not sport:
            rej['not a sport title'] += 1
            continue
        is_sport_cat = any(SPORTS_CAT.search(c) for c in p['cats'])
        if not (DEDICATED.match(p['bid']) or is_sport_cat):
            rej['general channel, no sport category'] += 1
            continue
        if p['replay']:
            rej['marked previously-shown'] += 1
            continue
        mins = (p['stop'] - p['start']).total_seconds() / 60
        looks_show = bool(SHOW_RE.search(title))
        live_hint = bool(LIVE_HINT.search(title)) or bool(LIVE_HINT.search(p['sub']))
        # duration as a combined signal, never alone: a short programme that also looks like a studio
        # show is filler; a short programme that says LIVE is a real session.
        if STUDIO_HARD.search(title):
            rej['named studio strand'] += 1
            continue
        if looks_show and not live_hint:
            rej['studio/filler title'] += 1
            continue
        if mins < 40 and looks_show:
            rej['short filler'] += 1
            continue
        a, b, rnd = extract_identity(p['sub'], p['desc'], sport, p['country'])
        kept.append({
            'sport': sport, 'title': title, 'sub': p['sub'] or None,
            'home': a, 'away': b, 'round': rnd,
            'start': p['start'], 'stop': p['stop'],
            'bid': p['bid'], 'country': p['country'], 'live_hint': live_hint,
        })
    return kept, rej


def norm_key(s):
    return re.sub(r'[^a-z0-9 ]', '', re.sub(r'\s+', ' ', (s or '').lower())).strip()


def dedupe(events):
    """One real-world event broadcast on several channels becomes one event with several broadcasters.
    45% of raw rows were duplicates in the dry run, mostly the AU free-to-air networks publishing a
    separate channel per city."""
    buckets = collections.defaultdict(list)
    for e in events:
        ident = norm_key('%s %s' % (e['home'], e['away'])) if e['home'] and e['away'] else None
        key = (e['sport'], ident or norm_key(e['title']),
               None if ident else e['start'].replace(minute=0, second=0, microsecond=0))
        buckets[key].append(e)
    out = []
    for key, group in buckets.items():
        group.sort(key=lambda x: x['start'])
        merged = []
        for e in group:
            # same key within 6h of an existing cluster = the same broadcast event
            for m in merged:
                if abs((e['start'] - m['start']).total_seconds()) <= 6 * 3600:
                    m['_all'].append(e)
                    if e['start'] < m['start']:
                        m['start'] = e['start']
                    if e['stop'] > m['stop']:
                        m['stop'] = e['stop']
                    break
            else:
                e = dict(e); e['_all'] = [e]
                merged.append(e)
        out.extend(merged)
    return merge_same_channel(out)


def merge_same_channel(events):
    """Second pass on a hard invariant: one channel cannot broadcast two things at once. After the
    canonical collapse folds HD/SD/regional variants into a single id, two entries on the SAME
    canonical broadcaster whose times overlap must be the same broadcast described differently.
    Confirmed real: Sky Sports Cricket listed the same match as "WBBL Hobart.H v Adelaide.S" on one
    variant and "Women's Big Bash" on another -- different enough that identity-based grouping above
    left them as two events. Keeps whichever description is richest."""
    by_bid = collections.defaultdict(list)
    for e in events:
        for bid in {x['bid'] for x in e['_all']}:
            by_bid[bid].append(e)
    absorbed = set()
    for bid, evs in by_bid.items():
        evs.sort(key=lambda e: e['start'])
        for i, a in enumerate(evs):
            if id(a) in absorbed:
                continue
            for b in evs[i + 1:]:
                if id(b) in absorbed or b['sport'] != a['sport']:
                    continue
                if b['start'] >= a['stop']:
                    break
                a['_all'].extend(b['_all'])
                a['stop'] = max(a['stop'], b['stop'])
                # keep the more informative description of the two
                if not (a['home'] and a['away']) and b['home'] and b['away']:
                    a['home'], a['away'], a['round'] = b['home'], b['away'], b.get('round')
                if len(b['title']) > len(a['title']) and not (a['home'] and a['away']):
                    a['title'] = b['title']
                absorbed.add(id(b))
    return [e for e in events if id(e) not in absorbed]


# Sports whose events are sessions rather than head-to-head fixtures. Two networks covering the same
# round start at different minutes (Sky Sports Golf at 08:00, Fox Sports 503 at 08:30), so exact-hour
# dedupe leaves two cards for one tournament.
TOURNAMENT_SPORTS = {'golf', 'motorsport', 'tennis', 'cycling', 'snooker', 'darts', 'racing'}
OVERLAP_MIN = 0.5

# Words that say nothing about WHICH tournament this is. Everything left after stripping them is the
# distinctive signature: "DP World Tour" -> {dp, world}, "LIVE: DP World Tour - Tour Championship
# Round 1" -> {dp, world, championship}, "Irish Open Final Round" -> {irish, open}, "LPGA" -> {lpga}.
# Tour identifiers (dp, world, lpga, atp, wta) are deliberately NOT generic -- they are the only thing
# separating one simultaneous tour from another.
GENERIC_TOUR_TOKENS = frozenset(
    'live lve golf tennis cycling motorsport snooker darts racing tour tours round rounds day days '
    'session sessions morning afternoon evening night final finals semi quarter highlights coverage '
    'the and a of r1 r2 r3 r4 d1 d2 d3 d4 mens womens womans'.split())


def tour_signature(e):
    toks = set(norm_key(e['title']).split())
    if e.get('comp'):
        toks |= set(norm_key(e['comp']).split())
    return frozenset(t for t in toks - GENERIC_TOUR_TOKENS if not t.isdigit())


def _overlap_frac(a, b):
    """Overlap as a fraction of the SHORTER window. Measuring against the shorter one is what makes a
    30-minute offset on a 4-hour broadcast read as the same event (0.88) while a 4-hour golf round and
    the 28-hour span of the whole tournament do not (a naive min(start)-max(end) rule welds a Thursday
    round to a Friday one -- that is why this is a threshold and not a span union)."""
    lo, hi = max(a['start'], b['start']), min(a['stop'], b['stop'])
    ov = (hi - lo).total_seconds()
    if ov <= 0:
        return 0.0
    shortest = min((a['stop'] - a['start']).total_seconds(), (b['stop'] - b['start']).total_seconds())
    return 0.0 if shortest <= 0 else ov / shortest


def _absorb(a, b):
    a['_all'].extend(b['_all'])
    a['start'] = min(a['start'], b['start'])
    a['stop'] = max(a['stop'], b['stop'])
    if not (a['home'] and a['away']) and b['home'] and b['away']:
        a['home'], a['away'], a['round'] = b['home'], b['away'], b.get('round')
    if not a.get('comp') and b.get('comp'):
        a['comp'] = b['comp']
    if len(b['title']) > len(a['title']) and not (a['home'] and a['away']):
        a['title'] = b['title']


def merge_overlapping(events):
    """Third pass, across DIFFERENT broadcasters: entries in the same sport and competition/session
    whose windows overlap by more than half the shorter one are one event. Grouped narrowly on
    purpose -- a tournament sport keys on its competition name (or its own title when unenriched) so
    two different tours running simultaneously on opposite sides of the world stay separate, and a
    team sport keys on the matchup so only genuinely identical fixtures with offset pre-show padding
    collapse."""
    groups = collections.defaultdict(list)
    for e in events:
        if e['home'] and e['away']:
            ha, aa = norm_key(e['home']).split(), norm_key(e['away']).split()
            if ha and aa:
                groups[(e['sport'], frozenset([ha[0], aa[0]]))].append(e)
        elif e['sport'] in TOURNAMENT_SPORTS:
            # one bucket per sport, because the titles are exactly what disagree -- the same round is
            # "DP World Tour" on Sky Sport NZ, "DP World Tour Golf" on Sony Ten and "LIVE: DP World
            # Tour - Tour Championship Round 1" on Fox 505. The signature test below is what keeps
            # two different tours running at the same hour apart.
            groups[(e['sport'],)].append(e)
    absorbed = set()
    for key, g in groups.items():
        g.sort(key=lambda e: e['start'])
        tour = len(key) == 1
        sig = {id(e): tour_signature(e) for e in g} if tour else {}
        for i, a in enumerate(g):
            if id(a) in absorbed:
                continue
            for b in g[i + 1:]:
                if id(b) in absorbed:
                    continue
                if _overlap_frac(a, b) <= OVERLAP_MIN:
                    continue
                if tour:
                    sa, sb = sig[id(a)], sig[id(b)]
                    # a vaguer title folding into a more specific one is the same broadcast; two
                    # titles with competing specifics ({irish,open} vs {abu,dhabi}) are not.
                    if not (sa <= sb or sb <= sa):
                        continue
                _absorb(a, b)
                if tour:
                    sig[id(a)] = sig[id(a)] | sig[id(b)]
                absorbed.add(id(b))
    return [e for e in events if id(e) not in absorbed]


def load_fixture_enrichment(guide_v2_path):
    """Fixtures demoted to an enrichment layer: they supply canonical competition names that EPG
    titles lack ("Constellation Cup" never says netball). Keyed by normalised team pair."""
    idx = {}
    try:
        with open(guide_v2_path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return idx
    for ev in data.get('events', []):
        h, a = ev.get('home'), ev.get('away')
        if not (h and a):
            continue
        k = frozenset([norm_key(h).split()[0] if norm_key(h) else '',
                       norm_key(a).split()[0] if norm_key(a) else ''])
        if '' in k or len(k) < 2:
            continue
        try:
            kick = datetime.datetime.fromisoformat(ev['kick'])
        except Exception:
            continue
        idx.setdefault(k, []).append({'comp': ev.get('comp'), 'kick': kick})
    return idx


# Competition names straight from resolve_v2's own LEAGUES list, matched against the programme title.
# Team-pair matching alone turned out to enrich only 6 of 1,700 events -- EPG and fixture feeds rarely
# describe the same match in the same window. But EPG titles very often name the competition outright
# ("UEFA Europa League Football", "Super League Rugby", "NRL - R17 Knights v Wests Tigers"), which the
# fixture list can confirm and canonicalise. Gated on the sport agreeing, so soccer's "Premier League"
# can never attach itself to "PDC ANZ Premier League Darts".
TSDB_SPORT_TO_ID = {
    'Soccer': {'soccer'}, 'Basketball': {'basketball'}, 'Ice Hockey': {'ice_hockey'},
    'Baseball': {'baseball'}, 'American Football': {'am_football'}, 'Motorsport': {'motorsport'},
    'Tennis': {'tennis'}, 'Golf': {'golf'}, 'Fighting': {'boxing_mma'}, 'Cricket': {'cricket'},
    'Cycling': {'cycling'}, 'Australian Football': {'afl'},
    'Rugby': {'rugby_league', 'rugby_union'},   # LEAGUES does not split the two codes
}


def comp_name_index():
    try:
        from resolve_v2 import LEAGUES
    except Exception:
        return []
    out = []
    for l in LEAGUES:
        sports = TSDB_SPORT_TO_ID.get(l.get('tsdb_sport'), set())
        if not sports:
            continue
        out.append((re.compile(r'\b' + re.escape(l['comp']) + r'\b', re.I), l['comp'], sports))
    # longest name first, so "Rugby League Super League" wins over "Super League"
    out.sort(key=lambda x: -len(x[1]))
    return out


def enrich(events, fixtures):
    hits = 0
    for comp_rx, comp, sports in comp_name_index():
        for e in events:
            if not e.get('comp') and e['sport'] in sports and comp_rx.search(e['title']):
                e['comp'] = comp
                hits += 1
    for e in events:
        if e.get('comp') or not (e['home'] and e['away']):
            continue
        ha, aa = norm_key(e['home']).split(), norm_key(e['away']).split()
        if not ha or not aa:
            continue
        for f in fixtures.get(frozenset([ha[0], aa[0]]), []):
            # same teams can meet in several competitions in one window (an ODI and a Test). Only
            # trust the competition name when the fixture's own kickoff is near this broadcast.
            if abs((f['kick'] - e['start']).total_seconds()) <= 12 * 3600:
                e['comp'] = f['comp']
                hits += 1
                break
    return hits


# Fixture channel rows carry a plain name plus a country ("Sky Sports NFL" / "UK"), so they can be put
# through the same canonical collapse the EPG side uses and compared as ids.
COUNTRY_SCOPE = {'UK': ('uk:',), 'US': ('us:',), 'AU': ('au:',), 'CA': ('ca:',), 'NZ': ('nz:',),
                 'IN': ('in:',), 'ZA': ('za:',), 'IE': ('ie:', 'uk:'), 'QA': ('mena:',),
                 'AE': ('mena:',), 'SA': ('mena:',)}
JOIN_WINDOW_S = 20 * 60


def canonical_from_fixture(name, country):
    base = re.sub(r'\s+', ' ', re.sub(r'[._]+', ' ', REGION_RE.sub(' ', name or ''))).strip()
    scope = COUNTRY_SCOPE.get((country or '').upper())
    if not scope:
        return None
    for cid, cc, cname, rx in CANON_RE:
        if cid.startswith(scope) and rx.search(base):
            return cid
    return None


def load_fixture_slots(guide_v2_path):
    """Fixtures indexed by competition and kickoff, each carrying the canonical broadcaster ids it is
    listed on. Used to put a matchup on EPG rows whose title is just the league name ("NFL")."""
    slots = collections.defaultdict(list)
    try:
        with open(guide_v2_path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return slots
    for ev in data.get('events', []):
        comp, h, a = ev.get('comp'), ev.get('home'), ev.get('away')
        if not (comp and h and a):
            continue
        try:
            kick = datetime.datetime.fromisoformat(ev['kick'])
        except Exception:
            continue
        bids = {b for b in (canonical_from_fixture(c.get('channel'), c.get('country'))
                            for c in ev.get('channels') or []) if b}
        slots[comp].append({'home': h, 'away': a, 'kick': kick,
                            'round': ev.get('round'), 'bids': bids})
    return slots


def join_fixture_matchups(events, slots):
    """Put a matchup on an EPG row that has a competition but no teams, from the fixture kicking off in
    the same slot. Only when it is unambiguous, which is the whole point: 70% of NFL fixtures and
    around half of NHL/NBA share a +/-20 minute window with another game, so "the fixture in that
    slot" is usually four fixtures. Taking the first would confidently mislabel the majority of US
    sport -- worse than leaving the title as plain "NFL". So: accept a lone fixture in the window, or
    disambiguate a crowded window by which fixture is listed on THIS broadcaster."""
    hits = ambiguous = 0
    for e in events:
        if (e['home'] and e['away']) or not e.get('comp'):
            continue
        cands = [f for f in slots.get(e['comp'], ())
                 if abs((f['kick'] - e['start']).total_seconds()) <= JOIN_WINDOW_S]
        if not cands:
            continue
        pick = cands[0] if len(cands) == 1 else None
        if pick is None:
            bids = {x['bid'] for x in e['_all']}
            on_air = [f for f in cands if f['bids'] & bids]
            if len(on_air) == 1:
                pick = on_air[0]
        if pick is None:
            ambiguous += 1
            continue
        e['home'], e['away'] = pick['home'], pick['away']
        if not e.get('round') and pick.get('round'):
            e['round'] = pick['round']
        hits += 1
    return hits, ambiguous


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def write_outputs(out_dir, events, channels, now):
    gdir = os.path.join(out_dir, 'guides')
    os.makedirs(gdir, exist_ok=True)
    gen = iso(now)
    by_sport = collections.defaultdict(list)
    for e in events:
        by_sport[e['sport']].append(e)

    # index.json is the "All Sports" landing view, not just a table of contents: it carries every
    # currently-live event plus the next 24 hours across all sports, so opening the Event Hub renders
    # from one request instead of downloading 16 sport files. Measured on this data set: 550 events,
    # 110.7 KB raw but 9.8 KB gzipped (Pages serves gzip), against 261 KB for the four biggest sport
    # files alone. Selecting a sport chip still lazily fetches that sport's full 8-day file.
    cut = now + datetime.timedelta(hours=INDEX_AHEAD_HOURS)
    index = {'generated': gen, 'horizon_days': HORIZON_DAYS, 'index_ahead_hours': INDEX_AHEAD_HOURS,
             'sports': [], 'live': [], 'events': []}
    for sport, evs in sorted(by_sport.items(), key=lambda kv: -len(kv[1])):
        evs.sort(key=lambda e: e['start'])
        live = [e for e in evs if e['start'] <= now <= e['stop']]
        payload = {'generated': gen, 'sport': sport, 'label': SPORT_LABEL.get(sport, sport),
                   'shape': SPORT_SHAPE.get(sport, 'session'), 'events': []}
        for i, e in enumerate(evs):
            eid = '%s:%d' % (sport, i)
            if e['start'] <= cut and e['stop'] > now:
                index['events'].append({
                    'id': eid, 'sport': sport, 'title': e['title'],
                    'home': e['home'], 'away': e['away'], 'comp': e.get('comp'),
                    'start': iso(e['start']), 'end': iso(e['stop']),
                    'shape': SPORT_SHAPE.get(sport, 'session'),
                    'broadcaster_ids': sorted({x['bid'] for x in e['_all']}),
                })
            payload['events'].append({
                'id': eid,
                'title': e['title'], 'sub': e['sub'],
                'home': e['home'], 'away': e['away'], 'round': e.get('round'),
                'comp': e.get('comp'),
                'start': iso(e['start']), 'end': iso(e['stop']), 'timed': True,
                'shape': SPORT_SHAPE.get(sport, 'session'),
                'broadcaster_ids': sorted({x['bid'] for x in e['_all']}),
                'countries': sorted({x['country'] for x in e['_all']}),
            })
        with open(os.path.join(gdir, '%s.json' % sport), 'w', encoding='utf-8') as f:
            json.dump(payload, f, separators=(',', ':'))
        index['sports'].append({'id': sport, 'label': SPORT_LABEL.get(sport, sport),
                                'file': 'guides/%s.json' % sport,
                                'events': len(evs), 'live_now': len(live)})
        for e in live[:3]:
            index['live'].append({
                'sport': sport, 'title': e['title'],
                'home': e['home'], 'away': e['away'], 'comp': e.get('comp'),
                'start': iso(e['start']), 'end': iso(e['stop']),
                'broadcaster_ids': sorted({x['bid'] for x in e['_all']})})

    index['broadcasters'] = sorted(channels.values(), key=lambda c: c['id'])
    with open(os.path.join(gdir, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, separators=(',', ':'))
    return index, by_sport


def write_now_next(out_dir, raw_by_bid, channels, now):
    """Restores the Live TV browser's "Now:" line for the canonical broadcasters without the client
    downloading or parsing a single byte of XML (the 6,000-item cap in loadExtraGuides exists because
    that parsing was OOM-crashing the Fire Stick)."""
    out = {'generated': iso(now), 'channels': {}}
    for bid, progs in raw_by_bid.items():
        progs.sort(key=lambda p: p['start'])
        cur = next((p for p in progs if p['start'] <= now <= p['stop']), None)
        nxt = next((p for p in progs if p['start'] > now), None)
        if not cur and not nxt:
            continue
        out['channels'][bid] = {
            'name': channels.get(bid, {}).get('name', bid),
            'now': None if not cur else {'title': cur['title'], 'sub': cur['sub'] or None,
                                         'start': iso(cur['start']), 'end': iso(cur['stop'])},
            'next': None if not nxt else {'title': nxt['title'], 'sub': nxt['sub'] or None,
                                          'start': iso(nxt['start']), 'end': iso(nxt['stop'])},
        }
    gdir = os.path.join(out_dir, 'guides')
    os.makedirs(gdir, exist_ok=True)
    with open(os.path.join(gdir, 'now_next.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'))
    return out


def selftest():
    cases = [('Creflo Dollar Ministries', None), ('Better Homes And Gardens', None),
             ('The Addams Family', None), ('Deal Or No Deal', None),
             ('AFLW - North Melbourne v Carlton', 'afl'),
             ('LIVE: NHL - Kraken vs. Hurricanes', 'ice_hockey'),
             ('Rugby League World Cup 2026', 'rugby_league'),
             ('UFC Fight Week - UFC 334', 'boxing_mma'),
             ('LIVE: ODI - AUS v ENG G1', 'cricket'),
             ('NFL', 'am_football'), ('DP World Tour Golf', 'golf')]
    bad = [(t, classify_sport(t), e) for t, e in cases if classify_sport(t) != e]
    # a LIVE prefix must not rescue a named studio strand, and must still rescue a real short session
    studio = [('Live: Golf Central', True), ('Live: MLB Now', True), ('Live: MLB Tonight', True),
              ('Live: NHL Post-Game on TNT', True), ('NFL Tailgate', True),
              ('NHL on TNT Face Off', True), ('NFL Films Presents', True),
              ('PGA Tour Champions Learning Center - Episode 2', True),
              ('LIVE: Champion of Champions - D2 NS', False),
              ('LIVE: DP World Tour - Tour Championship Round 1', False),
              ('LIVE: NHL - Kraken vs. Hurricanes', False)]
    for t, exp in studio:
        if bool(STUDIO_HARD.search(t)) != exp:
            bad.append(('STUDIO_HARD', t, not exp))
    idc = [(('Melbourne Stars v Brisbane Heat', ''), ('Melbourne Stars', 'Brisbane Heat')),
           (('Semi Final: Australia v Samoa', ''), ('Australia', 'Samoa')),
           (('', 'The Stars challenge the Heat. More text'), (None, None)),
           (('Wheelchair Final: TBA v TBA', ''), (None, None))]
    for (sub, desc), exp in idc:
        a, b, _ = extract_identity(sub, desc)
        if (a, b) != exp:
            bad.append(((sub, desc), (a, b), exp))
    # the "at"/"@" form, plus the false positives its three guards exist to stop
    # "Away at Home" -> the home side is returned first, hence the reversed expectations
    atc = [('Kansas City Chiefs at Buffalo Bills', 'am_football', 'US',
            ('Buffalo Bills', 'Kansas City Chiefs')),
           ('Boston Celtics @ Miami Heat', 'basketball', 'US', ('Miami Heat', 'Boston Celtics')),
           ('Toronto Maple Leafs at Montreal Canadiens', 'ice_hockey', 'CA',
            ('Montreal Canadiens', 'Toronto Maple Leafs')),
           ('Golf at Pebble Beach', 'golf', 'US', (None, None)),          # wrong sport
           ('NASCAR at Phoenix', 'motorsport', 'US', (None, None)),       # wrong sport
           ('Live at the Apollo', 'basketball', 'US', (None, None)),      # side not capitalised
           ('Baseball at Fenway', 'baseball', 'US', (None, None)),        # generic side
           ('Soccer at Wembley', 'soccer', 'UK', (None, None)),           # soccer excluded outright
           ('Kansas City Chiefs at Buffalo Bills', 'am_football', 'AU', (None, None))]  # "at" is US/CA only
    for sub, sport, cc, exp in atc:
        a, b, _ = extract_identity(sub, None, sport, cc)
        if (a, b) != exp:
            bad.append((sub, sport, cc, (a, b), exp))
    # prose matchups in <desc>, which is where US/CA feeds actually put them
    dsc = [('The Buffalo Sabres host the Tampa Bay Lightning in NHL regular-season play at KeyBank Center.',
            'ice_hockey', ('Buffalo Sabres', 'Tampa Bay Lightning')),
           ('The Chicago Bulls visit the New York Knicks.', 'basketball',
            ('New York Knicks', 'Chicago Bulls')),                  # visitor named first -> swapped
           ('Liverpool face Arsenal in the league.', 'soccer', ('Liverpool', 'Arsenal')),
           ('From T-Mobile Arena in Las Vegas.', 'ice_hockey', (None, None)),
           ('Ant and Dec host Saturday Night Takeaway.', 'soccer', (None, None)),   # not sentence-initial
           ('Anson Carter and Liam McHugh present pregame coverage.', 'ice_hockey', (None, None)),
           ('Rory McIlroy meets the field at Pebble Beach.', 'golf', (None, None))]  # tournament sport
    for desc, sport, exp in dsc:
        a, b, _ = extract_identity(None, desc, sport, 'US')
        if (a, b) != exp:
            bad.append((desc[:40], sport, (a, b), exp))
    # tournament signatures: vaguer titles must be a subset of the specific one they belong to, and
    # two tours running the same hour must not be comparable in either direction
    sig = lambda t: tour_signature({'title': t, 'comp': None})
    dpw, dpwg = sig('DP World Tour'), sig('DP World Tour Golf')
    tc = sig('LIVE: DP World Tour - Tour Championship Round 1')
    abu = sig('DP World Tour - Abu Dhabi Championship Final Round')
    iri, lpga = sig('DP World Tour - Irish Open Final Round'), sig('LPGA')
    for label, cond in [('generic folds into specific', dpw <= tc and dpwg <= tc),
                        ('cross-feed generics agree', dpw == dpwg),
                        ('rival tournaments stay apart', not (abu <= iri or iri <= abu)),
                        ('rival tours stay apart', not (lpga <= dpw or dpw <= lpga))]:
        if not cond:
            bad.append(('tour_signature', label))
    cid = canonical_id('AU1', '7mate Perth')
    if not cid or cid[0] != 'au:7mate':
        bad.append(('7mate Perth', cid, 'au:7mate'))
    cid = canonical_id('BEIN1', 'beIN Sports 1')
    if not cid or not cid[0].startswith('mena:'):
        bad.append(('BEIN1 beIN Sports 1', cid, 'mena:*'))
    for b in bad:
        print('  SELFTEST FAIL', b)
    print('selftest: %d checks, %d failures'
          % (len(cases) + len(idc) + len(atc) + len(dsc) + len(studio) + 6, len(bad)))
    return not bad


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='public')
    ap.add_argument('--cache', default='.epgcache')
    ap.add_argument('--guide-v2', default=None, help='guide-v2.json to use as the enrichment layer')
    args = ap.parse_args()

    if not selftest():
        sys.exit('classifier/extractor self-test failed -- refusing to publish')

    os.makedirs(args.cache, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(days=HORIZON_DAYS)

    raw_all, channels = [], {}
    for code in FEEDS:
        progs, seen = read_feed(code, args.cache, now, horizon)
        raw_all.extend(progs)
        channels.update(seen)
        print('%-6s %4d canonical channels, %5d programmes in window' % (code, len(seen), len(progs)), flush=True)

    kept, rej = build_events(raw_all)
    before = len(kept)
    events = dedupe(kept)
    gv2 = args.guide_v2 or os.path.join(args.out, 'guide-v2.json')
    fixtures = load_fixture_enrichment(gv2)
    hits = enrich(events, fixtures)

    # competition names must be settled before the overlap merge (it groups on them) and before the
    # slot join (it looks the competition up in the fixture list).
    after_enrich = len(events)
    events = merge_overlapping(events)
    slots = load_fixture_slots(gv2)
    joined, ambiguous = join_fixture_matchups(events, slots)
    # second pass: rows that only just acquired a matchup can now collapse against each other
    events = merge_overlapping(events)

    raw_by_bid = collections.defaultdict(list)
    for p in raw_all:
        raw_by_bid[p['bid']].append(p)

    index, by_sport = write_outputs(args.out, events, channels, now)
    nn = write_now_next(args.out, raw_by_bid, channels, now)

    print('\nrejected:', dict(rej))
    print('kept %d -> %d after dedupe (%d%% were duplicate broadcasts)'
          % (before, len(events), 0 if not before else 100 - 100 * len(events) // before))
    print('enriched with a fixture competition name: %d' % hits)
    print('overlapping broadcasts merged across networks: %d (%d -> %d)'
          % (after_enrich - len(events), after_enrich, len(events)))
    print('matchups joined from fixture slots: %d   left alone as ambiguous: %d' % (joined, ambiguous))
    with_matchup = sum(1 for e in events if e['home'] and e['away'])
    print('events with a matchup: %d/%d (%d%%)'
          % (with_matchup, len(events), 100 * with_matchup // max(1, len(events))))
    print('canonical broadcasters seen: %d   now_next entries: %d' % (len(channels), len(nn['channels'])))


if __name__ == '__main__':
    main()
