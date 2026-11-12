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
FEEDS = ['AU1', 'UK1', 'US2', 'CA2', 'NZ1', 'IN1', 'ZA1', 'IE1', 'BEIN1', 'SG1', 'MY1', 'IT1', 'FR1',
         'DE1', 'NL1', 'PT1', 'BE2', 'AT1', 'GR1', 'TR1', 'CH1', 'BR2', 'MX1']
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
    # ---- Singapore ----
    # Verified by hand 2026-11-12 against the real SG1 feed: StarHub's "Hub Sports" channels already
    # carry live FAGE WTA Finals Indian Wells coverage (the exact event this whole guide rebuild was
    # chasing) plus LPGA/DP World Tour golf and ATP Tour tennis -- real, current, not a guess.
    ('sg:hub_sports_1', 'SG', 'Hub Sports 1', r'hub\s*sports?\s*1\b'),
    ('sg:hub_sports_2', 'SG', 'Hub Sports 2', r'hub\s*sports?\s*2\b'),
    ('sg:hub_sports_3', 'SG', 'Hub Sports 3', r'hub\s*sports?\s*3\b'),
    ('sg:hub_sports_4', 'SG', 'Hub Sports 4', r'hub\s*sports?\s*4\b'),
    ('sg:hub_sports_5', 'SG', 'Hub Sports 5', r'hub\s*sports?\s*5\b'),
    ('sg:hub_sports_6', 'SG', 'Hub Sports 6', r'hub\s*sports?\s*6\b'),
    ('sg:hub_sports_7', 'SG', 'Hub Sports 7', r'hub\s*sports?\s*7\b'),
    ('sg:hub_sports_8', 'SG', 'Hub Sports 8', r'hub\s*sports?\s*8\b'),
    ('sg:mio_cricket_plus', 'SG', 'mio Cricket Plus', r'mio\s*cricket\s*plus'),
    ('sg:mio_cricket',      'SG', 'mio Cricket',      r'mio\s*cricket'),
    ('sg:mio_sports_2',     'SG', 'mio Sports 2',     r'mio\s*sports?\s*2\b'),
    ('sg:mio_sports_3',     'SG', 'mio Sports 3',     r'mio\s*sports?\s*3\b'),
    ('sg:mio_sports',       'SG', 'mio Sports',       r'mio\s*sports?\b'),
    ('sg:bein_5',        'SG', 'beIN Sports 5 (SG)', r'bein\s*sports?\s*5'),
    ('sg:bein_4',        'SG', 'beIN Sports 4 (SG)', r'bein\s*sports?\s*4'),
    ('sg:bein_3',        'SG', 'beIN Sports 3 (SG)', r'bein\s*sports?\s*3'),
    ('sg:bein_2',        'SG', 'beIN Sports 2 (SG)', r'bein\s*sports?\s*2'),
    ('sg:bein_rugby',    'SG', 'beIN Sports Rugby (SG)', r'bein\s*sports?\s*rugby'),
    ('sg:bein_1',        'SG', 'beIN Sports (SG)',   r'bein\s*sports?'),
    ('sg:premier_sports','SG', 'Premier Sports (SG)', r'premier\s*sports?\b'),
    ('sg:spotv_2',       'SG', 'SPOTV 2',          r'spotv\s*2'),
    ('sg:spotv',         'SG', 'SPOTV',            r'^spotv\b'),
    ('sg:mola_sport',    'SG', 'Mola Sport',       r'mola\s*sport'),
    ('sg:cricket_plus',  'SG', 'Cricket Plus',     r'^cricket\s*plus'),
    ('sg:fight_sports',  'SG', 'Fight Sports',     r'fight\s*sports'),
    # ---- Malaysia ----
    # Verified by hand 2026-11-12 against the real MY1 feed: real Astro sports channels (Arena,
    # Grandstand, Sports Plus/UHD, Badminton, Cricket, Football, Premier League) plus a genuine Golf
    # Channel with live LPGA/DP World Tour coverage -- not invented, checked against real programme titles.
    ('my:astro_premier_league_5', 'MY', 'Astro Premier League 5', r'astro\s*premier\s*league\s*5'),
    ('my:astro_premier_league_4', 'MY', 'Astro Premier League 4', r'astro\s*premier\s*league\s*4'),
    ('my:astro_premier_league_3', 'MY', 'Astro Premier League 3', r'astro\s*premier\s*league\s*3'),
    ('my:astro_premier_league_2', 'MY', 'Astro Premier League 2', r'astro\s*premier\s*league\s*2'),
    ('my:astro_premier_league',   'MY', 'Astro Premier League',   r'astro\s*premier\s*league'),
    ('my:astro_arena_2',     'MY', 'Astro Arena 2',     r'astro\s*arena\s*2'),
    ('my:astro_arena',       'MY', 'Astro Arena',       r'astro\s*arena'),
    ('my:astro_grandstand',  'MY', 'Astro Grandstand',  r'astro\s*grandstand'),
    ('my:astro_sports_uhd',  'MY', 'Astro Sports UHD',  r'astro\s*sports?\s*uhd'),
    ('my:astro_sports_plus', 'MY', 'Astro Sports Plus', r'astro\s*sports?\s*plus'),
    ('my:astro_badminton',   'MY', 'Astro Badminton',   r'astro\s*badminton'),
    ('my:astro_cricket',     'MY', 'Astro Cricket',     r'astro\s*cricket'),
    ('my:astro_football',    'MY', 'Astro Football',    r'astro\s*football'),
    ('my:golf_channel',      'MY', 'Golf Channel (MY)', r'golf\s*channel'),
    ('my:premier_sports_rugby', 'MY', 'Premier Sports Rugby', r'premier\s*sports?\s*rugby'),
    ('my:w_sport',           'MY', 'W-Sport',           r'w[\s-]*sport\b'),
    ('my:bein_3',            'MY', 'beIN Sports 3 (MY)', r'bein\s*sports?\s*3'),
    ('my:bein_2',            'MY', 'beIN Sports 2 (MY)', r'bein\s*sports?\s*2'),
    ('my:bein_1',            'MY', 'beIN Sports 1 (MY)', r'bein\s*sports?\s*1'),
    ('my:eurosport',         'MY', 'Eurosport (MY)',     r'eurosport'),
    # ---- Italy ----
    # Verified by hand 2026-11-12 against the real IT1 feed: DAZN 1 holds real live Serie A rights and
    # carries matches by team names alone ("Fiorentina - Juventus", "Napoli - Lazio"), no literal
    # "Serie A" in the title -- exactly the case DEDICATED exists for. Sky Sport Calcio also carries
    # Bundesliga and Premier League matches alongside Serie A, a bonus for those two gaps too, not
    # invented -- seen directly in its own real programme titles ("Mainz - FC Bayern Monaco").
    ('it:dazn_1',           'IT', 'DAZN 1 (IT)',           r'^dazn\s*1\b'),
    ('it:dazn_2',           'IT', 'DAZN 2 (IT)',           r'^dazn\s*2\b'),
    ('it:dazn_4',           'IT', 'DAZN 4 (IT)',           r'^dazn\s*4\b'),
    ('it:dazn_5',           'IT', 'DAZN 5 (IT)',           r'^dazn\s*5\b'),
    ('it:sky_calcio',       'IT', 'Sky Sport Calcio',      r'sky\s*sport\s*calcio'),
    ('it:sky_sport_f1',     'IT', 'Sky Sport F1 (IT)',     r'sky\s*sport\s*f1'),
    ('it:sky_sport_motogp', 'IT', 'Sky Sport MotoGP',      r'sky\s*sport\s*motogp'),
    ('it:sky_sport_tennis', 'IT', 'Sky Sport Tennis (IT)', r'sky\s*sport\s*tennis'),
    ('it:sky_sport_golf',   'IT', 'Sky Sport Golf (IT)',   r'sky\s*sport\s*golf'),
    ('it:sky_sport_nba',    'IT', 'Sky Sport NBA (IT)',    r'sky\s*sport\s*nba'),
    ('it:sky_sport_uno',    'IT', 'Sky Sport Uno',         r'sky\s*sport\s*uno'),
    ('it:sky_sport_24',     'IT', 'Sky Sport 24',          r'sky\s*sport\s*24'),
    ('it:sportitalia',      'IT', 'Sportitalia',           r'sportitalia'),
    # Fixed 2026-11-12: this was one bare 'eurosport' entry collapsing two real, distinct channels
    # ("Eurosport.Italia" and "Eurosport.2.Italia") into a single id -- the exact numbered-variant
    # collapse the UK section above was already careful to avoid (uk:eurosport_1/_2). Verified by hand
    # against the real feed before splitting: both carry real, different content (snooker/skiing/
    # endurance racing on 1, UCI WorldTour cycling/endurance/skiing on 2), not interchangeable.
    ('it:eurosport_2',      'IT', 'Eurosport 2 (IT)',      r'eurosport\s*2'),
    ('it:eurosport_1',      'IT', 'Eurosport (IT)',        r'eurosport'),
    # ---- France ----
    # Verified by hand 2026-11-12 against the real FR1 feed. Honest finding, not the one expected:
    # DAZN 1 (France's actual live Ligue 1 rights holder) has ZERO real programmes in this feed's
    # current window -- only a kids' magazine show and an esports league happen to share the "Ligue 1"
    # name. So this does NOT fix Ligue 1 itself yet. What it DOES give, confirmed from real titles:
    # beIN Sports France carries Bundesliga, Portugal's Primeira Liga, Turkish Super Lig and Ligue 2 --
    # a genuine bonus for three separate other gaps, found by accident while looking for Ligue 1. These
    # specific slots carry no sub-title matchup (checked), but real team names in French prose in the
    # <desc> field (e.g. "Bayer Leverkusen" / "Cologne") -- kept anyway, same as any other confirmed
    # channel+time slot without an identified matchup (same shape as NBA Extra elsewhere in this file).
    ('fr:bein_max_10',      'FR', 'beIN Sports MAX 10 (FR)', r'bein\s*sports?\s*max\s*10\b'),
    ('fr:bein_max_4',       'FR', 'beIN Sports MAX 4 (FR)', r'bein\s*sports?\s*max\s*4\b'),
    ('fr:bein_max_5',       'FR', 'beIN Sports MAX 5 (FR)', r'bein\s*sports?\s*max\s*5\b'),
    ('fr:bein_max_6',       'FR', 'beIN Sports MAX 6 (FR)', r'bein\s*sports?\s*max\s*6\b'),
    ('fr:bein_max_7',       'FR', 'beIN Sports MAX 7 (FR)', r'bein\s*sports?\s*max\s*7\b'),
    ('fr:bein_max_8',       'FR', 'beIN Sports MAX 8 (FR)', r'bein\s*sports?\s*max\s*8\b'),
    ('fr:bein_max_9',       'FR', 'beIN Sports MAX 9 (FR)', r'bein\s*sports?\s*max\s*9\b'),
    ('fr:bein_3',           'FR', 'beIN Sports 3 (FR)',    r'bein\s*sports?\s*3'),
    ('fr:bein_2',           'FR', 'beIN Sports 2 (FR)',    r'bein\s*sports?\s*2'),
    ('fr:bein_1',           'FR', 'beIN Sports (FR)',      r'bein\s*sports?'),
    ('fr:canal_sport360',   'FR', 'Canal+ Sport 360',      r'canal\+?\s*sport\s*360'),
    ('fr:canal_foot',       'FR', 'Canal+ Foot',           r'canal\+?\s*foot'),
    ('fr:canal_sport',      'FR', 'Canal+ Sport',          r'canal\+?\s*sport'),
    ('fr:rmc_sport_1',      'FR', 'RMC Sport 1',           r'rmc\s*sport\s*1'),
    ('fr:eurosport_2',      'FR', 'Eurosport 2 (FR)',      r'eurosport\s*2'),
    ('fr:eurosport_1',      'FR', 'Eurosport (FR)',        r'eurosport'),

    # Germany -- verified against real DE1 feed. Sky Sport Premier League/Bundesliga/F1/Golf/Tennis/
    # Top Event/News/Mix and DAZN 1/2 all carry real, current fixtures (confirmed: "Fussball: 2.
    # Bundesliga", "Formel 1: Grosser Preis von Mexiko", "Tennis: WTA Finals"). Sky Sport Austria 1
    # (on this German feed, not AT1) carries the actual Austrian Bundesliga ("Admiral BL").
    ('de:sky_premier_league', 'DE', 'Sky Sport Premier League (DE)', r'sky\s*sport\s*premier\s*league'),
    ('de:sky_bundesliga',   'DE', 'Sky Sport Bundesliga (DE)', r'sky\s*sport\s*bundesliga'),
    ('de:sky_austria_1',    'DE', 'Sky Sport Austria 1 (DE)', r'sky\s*sport\s*austria\s*1'),
    ('de:sky_austria',      'DE', 'Sky Sport Austria (DE)', r'sky\s*sport\s*austria'),
    ('de:sky_f1',           'DE', 'Sky Sport F1 (DE)', r'sky\s*sport\s*f1'),
    ('de:sky_golf',         'DE', 'Sky Sport Golf (DE)', r'sky\s*sport\s*golf'),
    ('de:sky_tennis',       'DE', 'Sky Sport Tennis (DE)', r'sky\s*sport\s*tennis'),
    ('de:sky_top_event',    'DE', 'Sky Sport Top Event (DE)', r'sky\s*sport\s*top\s*event'),
    ('de:sky_news',         'DE', 'Sky Sport News (DE)', r'sky\s*sport\s*news'),
    ('de:sky_mix',          'DE', 'Sky Sport Mix (DE)', r'sky\s*sport\s*mix'),
    ('de:dazn_1',           'DE', 'DAZN 1 (DE)', r'dazn\s*1\b'),
    ('de:dazn_2',           'DE', 'DAZN 2 (DE)', r'dazn\s*2\b'),
    ('de:dazn',             'DE', 'DAZN (DE)', r'\bdazn\b'),
    ('de:sportdigital_fussball', 'DE', 'sportdigital Fussball (DE)', r'sportdigital\s*fussball'),
    ('de:sport1_plus',      'DE', 'SPORT1+ (DE)', r'sport1\s*\+'),
    ('de:sport1',           'DE', 'SPORT1 (DE)', r'sport1\b'),
    ('de:eurosport_2',      'DE', 'Eurosport 2 (DE)', r'eurosport\s*2'),
    ('de:eurosport_1',      'DE', 'Eurosport (DE)', r'eurosport'),

    # Netherlands -- verified against real NL1 feed. Ziggo Sport 1-6 (+Extra/Totaal) carry genuine
    # live fixtures already in internationally-recognisable form (LaLiga, Serie A, ATP/WTA, MotoGP,
    # NASCAR), so no Dutch-language title work was needed beyond the 'voetbal' category fallback above.
    ('nl:ziggo_sport_extra', 'NL', 'Ziggo Sport Extra (NL)', r'ziggo\s*sport\s*extra'),
    ('nl:ziggo_sport_totaal', 'NL', 'Ziggo Sport Totaal (NL)', r'ziggo\s*sport\s*totaal'),
    ('nl:ziggo_sport_1',    'NL', 'Ziggo Sport 1 (NL)', r'ziggo\s*sport\s*1'),
    ('nl:ziggo_sport_2',    'NL', 'Ziggo Sport 2 (NL)', r'ziggo\s*sport\s*2'),
    ('nl:ziggo_sport_3',    'NL', 'Ziggo Sport 3 (NL)', r'ziggo\s*sport\s*3'),
    ('nl:ziggo_sport_4',    'NL', 'Ziggo Sport 4 (NL)', r'ziggo\s*sport\s*4'),
    ('nl:ziggo_sport_5',    'NL', 'Ziggo Sport 5 (NL)', r'ziggo\s*sport\s*5'),
    ('nl:ziggo_sport_6',    'NL', 'Ziggo Sport 6 (NL)', r'ziggo\s*sport\s*6'),
    ('nl:espn_2',           'NL', 'ESPN 2 (NL)', r'espn\s*2'),
    ('nl:espn_3',           'NL', 'ESPN 3 (NL)', r'espn\s*3'),
    ('nl:espn_4',           'NL', 'ESPN 4 (NL)', r'espn\s*4'),
    ('nl:espn',             'NL', 'ESPN (NL)', r'\bespn\b'),
    ('nl:eurosport_2',      'NL', 'Eurosport 2 (NL)', r'eurosport\s*2'),
    ('nl:eurosport_1',      'NL', 'Eurosport (NL)', r'eurosport'),
    ('nl:viaplay_tv_plus',  'NL', 'Viaplay TV+ (NL)', r'viaplay\s*tv\s*\+'),
    ('nl:viaplay_tv',       'NL', 'Viaplay TV (NL)', r'viaplay\s*tv'),
    ('nl:viaplay',          'NL', 'Viaplay (NL)', r'\bviaplay\b'),

    # Portugal -- verified against real PT1 feed. DAZN 3/4/5 are placeholder-only in this snapshot
    # ("Informacao Nao Disponibilizada Pelo Canal" on every sampled slot) -- only DAZN 1/2 carry real
    # fixtures. Sport TV 1-4 is the genuine Primeira Liga rights holder.
    ('pt:dazn_1',           'PT', 'DAZN 1 (PT)', r'dazn\s*1\b'),
    ('pt:dazn_2',           'PT', 'DAZN 2 (PT)', r'dazn\s*2\b'),
    ('pt:sport_tv_plus',    'PT', 'Sport TV+ (PT)', r'sport\s*tv\s*\+'),
    ('pt:sport_tv_1',       'PT', 'Sport TV1 (PT)', r'sport\s*tv\s*1\b'),
    ('pt:sport_tv_2',       'PT', 'Sport TV2 (PT)', r'sport\s*tv\s*2\b'),
    ('pt:sport_tv_3',       'PT', 'Sport TV3 (PT)', r'sport\s*tv\s*3\b'),
    ('pt:sport_tv_4',       'PT', 'Sport TV4 (PT)', r'sport\s*tv\s*4\b'),
    ('pt:sporting_tv',      'PT', 'Sporting TV (PT)', r'sporting\s*tv'),
    ('pt:eurosport_2',      'PT', 'Eurosport 2 (PT)', r'eurosport\s*2'),
    ('pt:eurosport_1',      'PT', 'Eurosport (PT)', r'eurosport'),
    ('pt:w_sport',          'PT', 'W-Sport (PT)', r'w-?sport'),

    # Belgium -- verified against real BE2 feed (a large, noisy multi-broadcaster aggregation; only
    # genuinely Belgian-rights channels with real, non-placeholder content kept). DAZN Pro League 1
    # is the real Belgian Pro League (Jupiler Pro League) rights holder; DAZN Pro League 2 and both
    # Eleven Sports slots were "Geen uitzending"/"Wordt aangekondigd" (no real content) and left out.
    # Rai Sport and other clearly foreign re-broadcasts on this feed are left unscoped rather than
    # mislabelled as Belgian.
    ('be:dazn_pro_league_1', 'BE', 'DAZN Pro League 1 (BE)', r'dazn\s*pro\s*league\s*1'),
    ('be:dazn_1',           'BE', 'DAZN 1 (BE)', r'dazn\s*1\b'),
    ('be:voosport_world_1', 'BE', 'VOOsport World 1 (BE)', r'voosport\s*world\s*1'),
    ('be:voosport_world_2', 'BE', 'VOOsport World 2 (BE)', r'voosport\s*world\s*2'),
    ('be:rmc_sport_1',      'BE', 'RMC Sport 1 (BE)', r'rmc\s*sport\s*1'),
    ('be:sport10',          'BE', 'Sport10 (BE)', r'sport\s*10'),
    ('be:eurosport_2',      'BE', 'Eurosport 2 (BE)', r'eurosport\s*2'),
    ('be:eurosport_1',      'BE', 'Eurosport (BE)', r'eurosport'),

    # Austria -- verified against real AT1 feed. Bare "ORF Sport" showed only a repeating "Silent
    # Sports +" placeholder-style title and is left out; ORF Sport1+ carries real distinct fixtures.
    ('at:sky_premier_league', 'AT', 'Sky Sport Premier League (AT)', r'sky\s*sport\s*premier\s*league'),
    ('at:sky_mix',          'AT', 'Sky Sport Mix (AT)', r'sky\s*sport\s*mix'),
    ('at:sky_news',         'AT', 'Sky Sport News (AT)', r'sky\s*sport\s*news'),
    ('at:sky_f1',           'AT', 'Sky Sport F1 (AT)', r'sky\s*sport\s*f1'),
    ('at:sky_golf',         'AT', 'Sky Sport Golf (AT)', r'sky\s*sport\s*golf'),
    ('at:sky_tennis',       'AT', 'Sky Sport Tennis (AT)', r'sky\s*sport\s*tennis'),
    ('at:orf_sport1_plus',  'AT', 'ORF Sport1+ (AT)', r'orf\s*sport\s*1\s*\+'),
    ('at:eurosport_2',      'AT', 'Eurosport 2 (AT)', r'eurosport\s*2'),
    ('at:eurosport_1',      'AT', 'Eurosport (AT)', r'eurosport'),

    # Greece -- verified against real GR1 feed. Magenta Sport channels appear in this feed too but
    # are the Austrian T-Mobile service leaking into an aggregated listing, not genuinely Greek --
    # left unscoped rather than mislabelled. Novasports and COSMOTE Sport are the two real Greek
    # sports-rights broadcasters (confirmed real Greek-script fixtures, e.g. "Fenerbahce - Liverpool").
    ('gr:novasports_premier_league', 'GR', 'Novasports Premier League (GR)', r'novasports\s*premier\s*league'),
    ('gr:novasports_prime', 'GR', 'Novasports Prime (GR)', r'novasports\s*prime'),
    ('gr:novasports_1',     'GR', 'Novasports 1 (GR)', r'novasports\s*1'),
    ('gr:novasports_2',     'GR', 'Novasports 2 (GR)', r'novasports\s*2'),
    ('gr:novasports_3',     'GR', 'Novasports 3 (GR)', r'novasports\s*3'),
    ('gr:novasports_4',     'GR', 'Novasports 4 (GR)', r'novasports\s*4'),
    ('gr:novasports_5',     'GR', 'Novasports 5 (GR)', r'novasports\s*5'),
    ('gr:novasports_6',     'GR', 'Novasports 6 (GR)', r'novasports\s*6'),
    ('gr:cosmote_sport_1',  'GR', 'COSMOTE Sport 1 (GR)', r'cosmote\s*sport\s*1'),
    ('gr:cosmote_sport_2',  'GR', 'COSMOTE Sport 2 (GR)', r'cosmote\s*sport\s*2'),
    ('gr:cosmote_sport_3',  'GR', 'COSMOTE Sport 3 (GR)', r'cosmote\s*sport\s*3'),
    ('gr:cosmote_sport_4',  'GR', 'COSMOTE Sport 4 (GR)', r'cosmote\s*sport\s*4'),
    ('gr:cosmote_sport_5',  'GR', 'COSMOTE Sport 5 (GR)', r'cosmote\s*sport\s*5'),
    ('gr:cosmote_sport_6',  'GR', 'COSMOTE Sport 6 (GR)', r'cosmote\s*sport\s*6'),
    ('gr:cosmote_sport_7',  'GR', 'COSMOTE Sport 7 (GR)', r'cosmote\s*sport\s*7'),
    ('gr:cosmote_sport_8',  'GR', 'COSMOTE Sport 8 (GR)', r'cosmote\s*sport\s*8'),
    ('gr:cosmote_sport_9',  'GR', 'COSMOTE Sport 9 (GR)', r'cosmote\s*sport\s*9'),
    ('gr:eurosport_2',      'GR', 'Eurosport 2 (GR)', r'eurosport\s*2'),
    ('gr:eurosport_1',      'GR', 'Eurosport (GR)', r'eurosport'),

    # Turkey -- verified against real TR1 feed. Every programme in this feed carries ZERO category
    # tags (confirmed empty on every sampled channel), so classification depends entirely on the
    # title -- and most real fixtures are titled as bare team names ("T. Konyaspor - Galatasaray")
    # with no sport keyword at all, unclassifiable without one. Real yield here is intentionally
    # modest: only slots that already name an internationally-recognised league (La Liga, Serie A,
    # Super Lig after the SPORT_RULES addition above) will surface.
    ('tr:beinsports_4',     'TR', 'beIN Sports 4 (TR)', r'bein\s*sports?\s*4'),
    ('tr:beinsports_3',     'TR', 'beIN Sports 3 (TR)', r'bein\s*sports?\s*3'),
    ('tr:beinsports_2',     'TR', 'beIN Sports 2 (TR)', r'bein\s*sports?\s*2'),
    ('tr:beinsports_1',     'TR', 'beIN Sports (TR)', r'bein\s*sports?'),
    ('tr:fox_sports',       'TR', 'Fox Sports (TR)', r'fox\s*sports?'),
    ('tr:trt_spor',         'TR', 'TRT Spor (TR)', r'trt\s*spor'),
    ('tr:ssport_2',         'TR', 'SSport 2 (TR)', r'ssport\s*2'),
    ('tr:ssport',           'TR', 'SSport (TR)', r'ssport'),
    ('tr:a_spor',           'TR', 'A Spor (TR)', r'\ba\s*spor\b'),
    ('tr:eurosport_1',      'TR', 'Eurosport (TR)', r'eurosport'),

    # Switzerland -- verified against real CH1 feed (very large, mostly non-Swiss re-broadcasts of
    # German/French/Italian channels already covered via DE1/AT1/FR1/IT1 -- only genuinely
    # Swiss-specific rights holders kept, to avoid duplicating those). Zero category data on every
    # sampled channel, same situation as TR1. MySports Un/Eins showed only a repeating "Slow Hockey"
    # placeholder-style title on every sample and are left out; MySports EDGE had real distinct
    # fixtures (NHL, FIA Rally-Raid) so is kept.
    ('ch:blue_sport_d_1',   'CH', 'blue Sport D 1 (CH)', r'blue\s*sport\s*d\s*1'),
    ('ch:blue_sport_d_2',   'CH', 'blue Sport D 2 (CH)', r'blue\s*sport\s*d\s*2'),
    ('ch:blue_sport_f',     'CH', 'blue Sport F (CH)', r'blue\s*sport\s*f\b'),
    ('ch:mysports_edge',    'CH', 'MySports EDGE (CH)', r'mysports\s*edge'),

    # Brazil -- used BR2 (clean, 97 channels) over BR1 (same broadcasters duplicated per-city, much
    # noisier, and its Premiere channel was empty in this snapshot where BR2's "Premiere Clubes" is
    # real -- confirmed real match reports, e.g. "VT - Bahia x Sao Paulo"). SporTV/ESPN/Band Sports
    # all carry real Brasileirao and other fixtures, already with real team names in the title.
    ('br:premiere_clubes',  'BR', 'Premiere Clubes (BR)', r'premiere\s*clubes'),
    ('br:sportv_2',         'BR', 'SporTV 2 (BR)', r'sportv\s*2'),
    ('br:sportv_3',         'BR', 'SporTV 3 (BR)', r'sportv\s*3'),
    ('br:sportv',           'BR', 'SporTV (BR)', r'sportv\b'),
    ('br:espn_2',           'BR', 'ESPN 2 (BR)', r'espn\s*2'),
    ('br:espn_3',           'BR', 'ESPN 3 (BR)', r'espn\s*3'),
    ('br:espn_4',           'BR', 'ESPN 4 (BR)', r'espn\s*4'),
    ('br:espn_5',           'BR', 'ESPN 5 (BR)', r'espn\s*5'),
    ('br:espn',             'BR', 'ESPN (BR)', r'\bespn\b'),
    ('br:band_sports',      'BR', 'Band Sports (BR)', r'band\s*sports?'),

    # Mexico -- verified against real MX1 feed. Plain "Fox Sports (Mexico)"/"Fox Sports 2" slots
    # showed only the bare channel-brand name as every programme's title with no category data, so
    # classify_sport has nothing to key off and they were left out rather than added for no real
    # yield. Azteca Deportes/ESPN/Claro Sports/TVC Deportes/Fox Sports Premium all carry real,
    # specific fixtures.
    ('mx:azteca_deportes',  'MX', 'Azteca Deportes (MX)', r'azteca\s*deportes'),
    ('mx:espn_2',           'MX', 'ESPN 2 (MX)', r'espn\s*2'),
    ('mx:espn_3',           'MX', 'ESPN 3 (MX)', r'espn\s*3'),
    ('mx:espn_5',           'MX', 'ESPN 5 (MX)', r'espn\s*5'),
    ('mx:espn',             'MX', 'ESPN (MX)', r'espn\b'),
    ('mx:claro_sports_1',   'MX', 'Claro Sports 1 (MX)', r'claro\s*sports?\s*1'),
    ('mx:tvc_deportes',     'MX', 'TVC Deportes (MX)', r'tvc\s*deportes'),
    ('mx:fox_sports_premium', 'MX', 'Fox Sports Premium (MX)', r'fox\s*sports?\s*premium'),
    ('mx:nfl_network',      'MX', 'NFL Network (MX)', r'nfl\s*network'),
    ('mx:golf_channel',     'MX', 'Golf Channel (MX)', r'golf\s*channel'),
    ('mx:aym_sports',       'MX', 'AYM Sports (MX)', r'aym\s*sports?'),
]
# Which canonical ids a given feed is allowed to resolve to.
# BEIN1 is MENA-only (confirmed by hand: all 80 of its channels are Arabic/English dubs of the same
# Qatar feed, no AU/FR/US variants) so it must never resolve to au:bein_1 and claim an Australian
# schedule -- that exact cross-region confusion was a real bug in the fixture pipeline.
# IE carries the UK sports networks for real (Sky Sports, TNT, Premier Sports are all on Irish Sky and
# Virgin), so IE1 is scoped to both. Confirmed the hard way: scoping it to 'ie:' alone silently
# dropped 3,597 programmes, Ireland being the single richest feed of the nine.
FEED_SCOPE = {'AU1': ('au:',), 'UK1': ('uk:',), 'US2': ('us:',), 'CA2': ('ca:',), 'NZ1': ('nz:',),
              'IN1': ('in:',), 'ZA1': ('za:',), 'IE1': ('ie:', 'uk:'), 'BEIN1': ('mena:',),
              'SG1': ('sg:',), 'MY1': ('my:',), 'IT1': ('it:',), 'FR1': ('fr:',),
              'DE1': ('de:',), 'NL1': ('nl:',), 'PT1': ('pt:',), 'BE2': ('be:',), 'AT1': ('at:',),
              'GR1': ('gr:',), 'TR1': ('tr:',), 'CH1': ('ch:',), 'BR2': ('br:',), 'MX1': ('mx:',)}
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
 # "Super League" bare is genuinely overloaded -- English Rugby League's own competition, but also
 # the literal official name of the FA Women's Super League (football) and the Swiss top-flight
 # football league (sponsor-branded "Brack Super League"/"Credit Suisse Super League"). Confirmed real
 # false positives against DE1/NL1/AT1/CH1: "Fussball: Women's Super League", "Women's Super League
 # LIVE: Chelsea FC - Manchester City", "Le temps additionnel - Brack Super League" -- all genuine
 # soccer matches being filed as rugby league. Excluded here rather than guessed: only the specific
 # co-occurring words actually observed in those real titles.
 # The exclusion has to check the WHOLE title, not just text after the match -- "Fussball: Women's
 # Super League" and "Le temps additionnel - Brack Super League" both put the excluding word BEFORE
 # "Super League", where a trailing (?!...) lookahead never sees it (confirmed the hard way: an
 # earlier version of this exclusion, written as a plain trailing lookahead, missed every one of
 # these real titles because of exactly this). The `^(?=(?:(?!X).)*$).*` prefix is a start-anchored,
 # end-anchored tempered-greedy-token scan of the entire string for X, independent of where the
 # eventual "super league" match itself lands.
 # "RFL Women's Super League" is real too -- the actual rugby league one, RFL (Rugby Football
 # League) being the governing body -- so the "women" exclusion above can't be unconditional. Checked
 # first and unconditionally, so it wins before the exclusion gate below even runs.
 ('rugby_league', r'\brugby\s*league\b|\bnrlw?\b|\brfl\b|'
                  r"^(?=(?:(?!fu[ß]ball|fussball|women|f[ée]minine|brack|credit\s*suisse|swiss).)*$).*"
                  r'\bsuper\s*league\b|'
                  r'\bstate\s*of\s*origin\b|\brlwc\b'),
 ('afl',          r'\baflw?\b|\baustralian\s*rules\b'),
 ('am_football',  r'\bamerican\s*football\b|\bnfl\b|\bncaa\s*football\b|\bcollege\s*football\b|\bsuper\s*bowl\b'),
 ('soccer',       r'\bfootball\b(?!.*\b(american|australian|gaelic)\b)|\bsoccer\b|\bpremier\s*league\b|'
                  r'\bchampions\s*league\b|\beuropa\s*(league|conference)\b|\bla\s*liga\b|\bserie\s*a\b|'
                  r'\bbundesliga\b|\bligue\s*1\b|\bfa\s*cup\b|\befl\b|\bmls\b|\beredivisie\b|\ba-?league\b|\bwsl\b|'
                  # German spelling, not a translation guess -- real DE1/AT1/CH1 titles say
                  # "Fussball: Servette FC - FC Zürich" with no English word anywhere in the title.
                  r'\bfu[ß]ball\b|\bfussball\b|'
                  # Turkish Süper Lig -- real TR1 titles ("Süper Lig Raporu") have no category data
                  # at all to fall back on, so this has to be a title keyword, not CATEGORY_SPORT.
                  r'\bs[uü]per\s*lig\b|'
                  # The positive side of the rugby_league exclusion above -- these need to still
                  # classify as soccer, not just stop misclassifying as rugby league.
                  r"\bwomen'?s\s*super\s*league\b|\bbrack\s*super\s*league\b|\bcredit\s*suisse\s*super\s*league\b|"
                  r'\bswiss\s*super\s*league\b'),
 ('rugby_union',  r'\brugby\s*union\b|\bsix\s*nations\b|\bsuper\s*rugby\b|\btop\s*14\b|\burc\b|'
                  r'\bpremiership\s*rugby\b|\brugby\b'),
 ('golf',         r'\bgolf\b|\bpga\b|\blpga\b|\bdp\s*world\s*tour\b|\bryder\s*cup\b'),
 ('tennis',       r'\btennis\b|\batp\b|\bwta\b|\bwimbledon\b|\broland\s*garros\b|\bdavis\s*cup\b'),
 ('motorsport',   r'\bformula\s*[12e]\b|\bf1\b|\bmotogp\b|\bmoto[23]\b|\bnascar\b|\bindycar\b|\bsupercars\b|'
                  r'\bsuperbikes?\b|\bwrc\b|\brally\b|\bgrand\s*prix\b|\bspeedway\b|\bmotocross\b'),
 ('basketball',   r'\bbasketball\b|\bw?nba\b|\beuroleague\b|\bw?nbl\b'),
 ('ice_hockey',   r'\bice\s*hockey\b|\bnhl\b|\bkhl\b|\biihf\b'),
 # "World Series" bare is also reused outside baseball (confirmed real false positives: "MMA:
 # Rajadamnern World Series Muay Thai", "UCI Mountain Bike World Series", "Jet Ski World Series") --
 # excluded by the same specific-words-actually-observed approach as the rugby_league fix above.
 # Needs the full-string check (see rugby_league's comment on this exact technique) because the
 # excluded word lands on both sides across these three real titles: before in "UCI Mountain Bike
 # World Series" and "Jet Ski World Series", after in "World Series Muay Thai".
 ('baseball',     r'\bbaseball\b|\bmlb\b|'
                  r'^(?=(?:(?!muay\s*thai|mountain\s*bike|jet\s*ski).)*$).*\bworld\s*series\b'),
 ('boxing_mma',   r'\bboxing\b|\bufc\b|\bmma\b|\bbare\s*knuckle\b|\bbkfc\b|\bfight\s*night\b|\btitle\s*fight\b'),
 ('cycling',      r'\bcycling\b|\btour\s*de\s*france\b|\bgiro\b|\bvuelta\b|\bcyclocross\b|\buci\b'),
 ('racing',       r'\bhorse\s*racing\b|\bharness\s*racing\b|\bgreyhound\b|\braceday\b'),
]
SPORT_RE = [(n, re.compile(p, re.I)) for n, p in SPORT_RULES]
# The second half of the two-job rule's own stated design ("title/category/channel decide WHICH
# SPORT") that the code never actually implemented until now. A non-English broadcaster's own
# schedule almost never puts an English sport word in the title itself -- "Fiorentina - Juventus" on
# Italy's DAZN 1 carries <category>Sport</category> and <category>Calcio</category>, genuinely live
# Serie A, silently dropped at the 'not a sport title' gate because classify_sport() only ever looked
# at the title. Verified by hand 2026-11-12 against the real IT1 feed's own category vocabulary
# (counted directly: 462 "Calcio", 157 "Tennis", 108 "Basket", 51 "Golf", 33 "Rugby", 29 "Ciclismo",
# 27 "Snooker", 396 "Motori" -- not guessed translations). Kept small and high-precision: a category
# label is a clean, single-purpose genre tag from the broadcaster's own schedule, not free text, so
# it doesn't need SPORT_RULES' word-boundary/collision care to be trustworthy.
CATEGORY_SPORT = {
    'calcio': 'soccer', 'motori': 'motorsport', 'tennis': 'tennis', 'basket': 'basketball',
    'golf': 'golf', 'rugby': 'rugby_union', 'ciclismo': 'cycling', 'snooker': 'snooker',
    # French -- verified by hand 2026-11-12 against the real FR1 feed's own category vocabulary
    # (counted directly: 216 "Football", 61 "Golf", 60 "Basket-ball", 30 "Boxe", 21 "Tennis",
    # 17 "Football américain", 12 "Rugby", 11 "Cyclisme"). 'football' here means soccer specifically
    # ('football américain' is checked first so it isn't swallowed by the bare 'football' entry).
    'football américain': 'am_football', 'football': 'soccer', 'basket-ball': 'basketball',
    'boxe': 'boxing_mma', 'cyclisme': 'cycling', 'motocyclisme': 'motorsport',
    # German -- verified against real DE1 feed categories (same words appear on AT1/CH1 titles/cats
    # too). "Formel 1"/"Großer Preis" titles don't contain the English "formula"/"grand prix" the
    # title rule looks for, so without this they were falling through to 'not a sport title'.
    'fußball': 'soccer', 'motorsport': 'motorsport', 'formel 1': 'motorsport', 'eishockey': 'ice_hockey',
    'basketball': 'basketball', 'boxen': 'boxing_mma',
    # Dutch/Portuguese -- cheap safety nets alongside titles that are mostly already in international
    # form (NL1/PT1 commonly keep league names in English, e.g. "Bundesliga 26/27 - Paderborn x E.
    # Frankfurt"), for the minority that don't.
    'voetbal': 'soccer', 'futebol': 'soccer',
    # Greek -- verified against real GR1 feed categories (counted: 620 "Ποδόσφαιρο"). Greek titles
    # are in Greek script throughout, so the English title rule never fires at all for this feed;
    # these three are the only categories confirmed unambiguous enough to trust without guessing.
    'ποδόσφαιρο': 'soccer', 'μπάσκετ': 'basketball', 'μηχανοκίνητα': 'motorsport',
}
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
                       r'ie:premier|mena:|sg:(hub_sports|mio|bein|premier_sports|spotv|mola|cricket_plus|fight_sports)|'
                       r'my:(astro|golf_channel|premier_sports_rugby|w_sport|bein)|it:(dazn|sky_|sportitalia)|'
                       r'fr:(bein|canal_|rmc_sport|eurosport)|'
                       r'de:(dazn|sky_|sportdigital|sport1|eurosport)|'
                       r'nl:(ziggo_sport|espn|eurosport|viaplay)|'
                       r'pt:(dazn|sport_tv|sporting_tv|eurosport|w_sport)|'
                       r'be:(dazn|voosport|rmc_sport|sport10|eurosport)|'
                       r'at:(sky_|orf_sport|eurosport)|'
                       r'gr:(novasports|cosmote_sport|eurosport)|'
                       r'tr:(beinsports|fox_sports|trt_spor|ssport|a_spor|eurosport)|'
                       r'ch:(blue_sport|mysports)|'
                       r'br:(premiere_clubes|sportv|espn|band_sports)|'
                       r'mx:(azteca_deportes|espn|claro_sports|tvc_deportes|fox_sports_premium|nfl_network|'
                       r'golf_channel|aym_sports))')

# Studio/filler programming that is not a watchable event. Checked against the TITLE only -- the
# two-job rule (title/category decides sport, sub-title/desc decides matchup identity) holds here too,
# because a real match's own description can legitimately contain words like "goals" or "show"
# ("a five-goal thriller"), and title-only is what keeps this from rejecting real fixtures.
# Duration is a SIGNAL here, never a cutoff on its own -- a hard "< 40 minutes" rule would also
# discard genuine short sessions (qualifying, a darts session, a criterium).
SHOW_RE = re.compile(r'\bhighlights?\b|\bclassic\b|\brewind\b|\breview\b|\bmagazine?\b|\bpreviews?\b|'
                     r'press\s*conference|\bnews\b|documentary|\bbest\s*of\b|\btop\s*\d+\b|countdown|'
                     r'\banalysis\b|\bweekly\b|round\s*-?up|\bthis\s*week\b|\bbuild-?up\b|\bverdict\b|'
                     r'\bdaily\b|\bshow\b|\bextra\s*time\b|\bovertime\b|\btonight\b|\bwrap\b|\bmini\b|'
                     r'\bupcoming\b|\bcoming\s*up\b|\bgoals\b|\btalk\b|\bdebate\b|'
                     # Italian studio/filler strands -- verified live 2026-11-12 against the real IT1
                     # feed, both leaking through as apparent live matches: "Sportitalia Mercato" and
                     # "Calciomercato Live Weekend" (calciomercato = the transfer-window show, a studio
                     # strand, not a match) and "Champions League Remix Ep.4" (a highlights-remix
                     # magazine show that was being enriched with comp='Champions League' as if it were
                     # a real fixture). Same "verify against real data, don't guess the translation"
                     # bar as CATEGORY_SPORT above.
                     r'\bremix\b', re.I)

# French broadcasters' documentary marker -- sometimes a "Doc." sub-title prefix, sometimes (same
# programme, a different feed occurrence with no sub-title at all) a "Documentaire"/"Documentaire
# sportif" category instead. Verified against FR1's own 42 Doc.-subtitled titles, and against the two
# sport-themed ones that only revealed the category form ("Violences dans le football" / "Les
# acrobates du football" both appeared twice with sub="Doc. Sport" and twice more with sub=None,
# cats=['Documentaire']) -- checking sub-title alone silently let half their occurrences through.
DOC_SUB_RE = re.compile(r'^\s*doc\.?\s', re.I)
DOC_CAT_RE = re.compile(r'documentaire', re.I)

# Archive/nostalgia strands are a DIFFERENT check from the above, and deliberately read title+sub+desc
# together rather than title alone. Confirmed live and real: broadcasters routinely put the archive
# giveaway in the DESCRIPTION, not the title -- "Bundesliga Highlights" / desc "Matchday highlights
# from the latest round of fixtures from the 2026-2027 Bundesliga season", "EFL Cup Best Goals:
# 2023/24" / desc mentioning "this season's Carabao Cup". A title-only check missed all of these:
# confirmed live on 2026-11-11, 41 of 42 programmes published as "Champions League" fixtures were
# archive rewind clips ("Leeds United v Rangers from the 1992/93 season", "Chelsea v Liverpool from
# the 2008/09 season") on a single channel each, with exactly one real live match underneath them.
# This is an authenticity check, not a sport-classification one, so reading the prose here doesn't
# violate the two-job rule the same way it would for SHOW_RE or classify_sport.
ARCHIVE_RE = re.compile(r'\bflashback\b|\blook\s*back\b|\bvault\b|\bretro\b|\bgreatest\b|\blegends?\b|'
                       r'\bacademy\b|\bmasterclass\b|\bgold\b|\bencore\b|\barchive\b|\bre-?run\b|'
                       r'\bbest\s*goals\b|'
                       # these need a year (or season) attached (unlike bare "goals"/"classic" above,
                       # which a real match description can say honestly) -- a year number next to the
                       # word is what makes it unambiguous. The last alternative is the short season
                       # form broadcasters actually use ("EFL 18/19: Aston Villa v Leeds Utd") -- a
                       # full 4-digit year never appears in these, so the first two patterns missed
                       # every one of them until this was added.
                       r'\bfrom\s+(the\s+)?(19|20)\d{2}\b|\b(19|20)\d{2}\s*/\s*(19|20)?\d{2}\b|'
                       r'\b(19|20)\d{2}\s+(final|classic|season)\b|\blatest\s+round\s+of\s+fixtures\b|'
                       r'\b\d{2}/\d{2}\s*:|'
                       # A year sitting inside parentheses is the same "year signals archive" logic as
                       # the "from 19xx" check above, just a different shape broadcasters use --
                       # verified against the real IT1 feed before adding: matched exactly 4 titles,
                       # zero false positives ("Italia Mundial (Spagna 1982)", two unrelated films and
                       # an opera recording with their release year in parens, nothing live or current).
                       r'\((?:[^()]{0,30})?(19|20)\d{2}\)', re.I)
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
                         r'\b(now|tonight|central|today)\s*$|\bretention\b|\bauction\b|'
                         # Italian pre-/post-match studio segments -- verified against the real IT1
                         # feed ("Prepartita Nations League 13/11/2026", "Postpartita Nations League
                         # 14/11/2026", bare "Prepartita"/"Postpartita"), the exact same pre/post-game
                         # studio pattern the English rule above already exists for.
                         r'\bprepartita\b|\bpostpartita\b|'
                         # "Calciomercato Live Weekend" (the Italian transfer-window chat show) survived
                         # SHOW_RE's own 'mercato' catch because it also says "Live" in English, which
                         # exempts a SHOW_RE match the same way "Live: Golf Central" needed STUDIO_HARD
                         # above instead -- the literal reason this check exists at all. Belongs here,
                         # not SHOW_RE, so the live_hint rescue can never apply to it.
                         r'mercato|'
                         # Canal+'s daily French football news/transfers talk show -- verified against
                         # real FR1 desc text ("Suivez toute l'actualite foot du jour... Autour de la
                         # table, les journalistes et consultants..."), a studio panel show, not a match.
                         r'late\s*football\s*club|'
                         # Dutch Ziggo Sport strands -- verified against real NL1 desc text: "Rondo"
                         # desc literally says "Voetbaltalkshow" (football talkshow); "Kick 't Met On
                         # Tour" desc says "In a new episode of Kick 't Met On Tour: the Champions
                         # League match Feyenoord - Internazionale" -- a review show ABOUT a match,
                         # not the match; "Football Nation" is a documentary-style analysis episode.
                         r"\brondo\b|\bkick\s*'?t\s*met\s*on\s*tour\b|\bfootball\s*nation\b|"
                         # NFL Network's daily morning studio show, seen on MX1 with no sub/desc at
                         # all -- the bare word "football" in its name would otherwise let it through
                         # the soccer rule unfiltered. Left "Thursday Night Football" alone deliberately
                         # -- that one is a real weekly live NFL game, not a studio strand.
                         r'good\s*morning\s*football', re.I)

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


def classify_sport(title, cats=None):
    for n, rx in SPORT_RE:
        if rx.search(title):
            return n
    # Fallback only -- the title is still checked first and still wins if it names a sport itself.
    # cats is optional (every existing caller, including the self-test, keeps working unchanged).
    for c in (cats or ()):
        s = CATEGORY_SPORT.get((c or '').strip().lower())
        if s:
            return s
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
        sport = classify_sport(title, p['cats'])
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
        # French broadcasters mark real documentaries with a "Doc." sub-title prefix -- verified against
        # the real FR1 feed: 42 distinct Doc.-subtitled titles, all genuine documentaries (biographical,
        # nature, etc.), zero real matches among them. Two sport-themed ones ("Violences dans le football",
        # "Les acrobates du football") were slipping through because neither the title nor SHOW_RE's
        # English-only vocabulary caught them. Sub-title only, not desc -- same narrow, verified-first bar.
        looks_show = (bool(SHOW_RE.search(title)) or bool(DOC_SUB_RE.match(p['sub']))
                      or any(DOC_CAT_RE.search(c) for c in p['cats']))
        live_hint = bool(LIVE_HINT.search(title)) or bool(LIVE_HINT.search(p['sub']))
        # duration as a combined signal, never alone: a short programme that also looks like a studio
        # show is filler; a short programme that says LIVE is a real session.
        if STUDIO_HARD.search(title):
            rej['named studio strand'] += 1
            continue
        # Archive check reads title+sub+desc together (see ARCHIVE_RE's own comment for why) and is
        # NOT exempted by live_hint -- a "LIVE" prefix on a rebroadcast is the broadcaster's own
        # labelling error, not evidence this is actually happening now, and letting it through was
        # exactly how "Leeds United v Rangers from the 1992/93 season" reached the published guide.
        if ARCHIVE_RE.search(title) or ARCHIVE_RE.search(p['sub'] or '') or ARCHIVE_RE.search(p['desc'] or ''):
            rej['archive/nostalgia clip'] += 1
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
        # A title with no matchup and nothing distinctive in it does NOT identify one broadcast. ESPN
        # and FS1 both listing "Live: College Basketball" at 00:00 are two different games, and
        # collapsing them by title+hour produced one card claiming six broadcasters. So those stay
        # separated by broadcaster, and only the same broadcaster's own variants collapse (which is
        # what the canonical id and merge_same_channel are for). Tournament sports are exempt: there a
        # vague title genuinely is the same session, and merge_overlapping handles them deliberately.
        vague = (not ident and e['sport'] not in TOURNAMENT_SPORTS
                 and not event_signature(e))
        key = (e['sport'], ident or norm_key(e['title']),
               None if ident else e['start'].replace(minute=0, second=0, microsecond=0),
               e['bid'] if vague else None)
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


# For team sports the league name is not distinguishing either -- "Live NFL" and "Live: College
# Basketball" name a league, not a game. Anything left after stripping these is something that can
# actually identify one broadcast ("Rugby League World Cup", "IIHF Women's World Championship").
GENERIC_EVENT_TOKENS = GENERIC_TOUR_TOKENS | frozenset(
    'nfl nba wnba nhl mlb nrl nrlw afl aflw bbl wbbl ipl epl mls nbl wnbl ncaa college university '
    'football basketball hockey ice baseball soccer cricket rugby league union netball darts snooker '
    'match matches matchday game games fixture week weeks regular season preseason postseason '
    'twenty t20 t20i odi oneday odis test tests internationals international friendly '
    'friendlies'.split())


def event_signature(e):
    toks = set(norm_key(e['title']).split())
    return frozenset(t for t in toks - GENERIC_EVENT_TOKENS if not t.isdigit())


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


# Confirmed live and wrong: "UEFA Women's Champions League - OL Lyonnes vs. Juventus", a real fixture
# on a real channel (au:espn2), was being tagged comp='Champions League' -- the MEN'S competition --
# because "Champions League" is literally a substring of "Women's Champions League" and \b word
# boundaries don't stop a substring match, only a whole-extra-word one. LEAGUES has zero women's
# competition entries at all (confirmed: `grep -i women resolve_v2.py` on the comp list is empty), so
# there is nothing CORRECT to attach here yet -- leaving the event uncategorised is honest; relabelling
# it as the men's competition is not. comp_name_index() itself can't fix this (it only sees one league
# name at a time, not the broadcast's own title), so the guard lives where the title is actually
# checked, in enrich() below.
WOMENS_MARKER = re.compile(r"\bwomen'?s\b|\bladies\b|\bwnrl\b|\bwbbl\b|\baflw\b|\bwsl\b", re.I)


# How far a channel's own programme start can sit from a KNOWN real fixture's kickoff and still
# plausibly BE that fixture's broadcast. Tight on purpose: unlike the independent EPG-first pipeline,
# this is not trying to decide what the programme IS, only whether its timing can only be this one
# already-real, already-identified match.
FIXTURE_FILL_WINDOW_HOURS = 1.5


def comp_to_sports():
    """Competition display name (as guide-v2.json's own `comp` field spells it) -> the epg_first sport
    id(s) it belongs to, straight from the same LEAGUES list comp_name_index() already uses."""
    try:
        from resolve_v2 import LEAGUES
    except Exception:
        return {}
    out = {}
    for l in LEAGUES:
        sports = TSDB_SPORT_TO_ID.get(l.get('tsdb_sport'), set())
        if sports:
            out[l['comp']] = sports
    return out


def fill_fixture_channels(raw_all, channels, guide_v2_path, out_path):
    """The actual fix for guide-v2.json's real gap: 231 of 539 fixtures (42%, measured live on
    2026-11-11) are real matches -- real teams, real competition, real kickoff, from
    TheSportsDB/football-data -- with zero channels. Never invent a new event here. The fixture
    already IS the event; the EPG's only job is answering "is a channel broadcasting something of the
    right sport at this exact, already-known kickoff". That sidesteps the entire class of bug the
    independent EPG-first pipeline (build_events/enrich below) has to defend against -- archive clips,
    stale ground truth, gender mislabelling -- none of those are possible when a real fixture, not a
    parsed EPG title, is the source of truth for who's playing and when. Writes channels back into
    guide-v2.json's own existing schema, so v2Events() on the client needs no changes at all to use
    them -- this is additive to the fixture-matching path that already works, not a parallel one."""
    try:
        with open(guide_v2_path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print('fill_fixture_channels: could not read %s (%s)' % (guide_v2_path, e), file=sys.stderr)
        return 0, 0

    c2s = comp_to_sports()
    need = collections.defaultdict(list)       # sport -> [(kick_dt, fixture_dict), ...]
    candidates = 0
    for ev in data.get('events', []):
        if ev.get('channels'):
            continue
        sports = c2s.get(ev.get('comp'))
        if not sports:
            continue
        try:
            kick = datetime.datetime.fromisoformat(ev['kick'])
        except Exception:
            continue
        candidates += 1
        for sp in sports:
            need[sp].append((kick, ev))

    if not need:
        return 0, candidates

    window = datetime.timedelta(hours=FIXTURE_FILL_WINDOW_HOURS)
    checked_iso = iso(datetime.datetime.now(datetime.timezone.utc))
    found = set()   # id(ev), so one fixture is only counted once even if several channels match it
    for p in raw_all:
        title = p['title']
        if not title:
            continue
        sport = classify_sport(title, p['cats'])
        bucket = need.get(sport)
        if not bucket:
            continue
        # same evidence bar as the main pipeline's filters, for the same reasons -- a studio/archive
        # programme is not proof of a real broadcast slot, and a general channel needs the category
        # flag the same way build_events() requires it.
        if STUDIO_HARD.search(title) or ARCHIVE_RE.search(title) or ARCHIVE_RE.search(p['sub'] or '') or ARCHIVE_RE.search(p['desc'] or ''):
            continue
        is_sport_cat = any(SPORTS_CAT.search(c) for c in p['cats'])
        if not (DEDICATED.match(p['bid']) or is_sport_cat):
            continue
        for kick, ev in bucket:
            if abs((p['start'] - kick).total_seconds()) > window.total_seconds():
                continue
            ch = channels.get(p['bid'])
            if not ch:
                continue
            row = {'channel': ch['name'], 'country': ch['country'], 'kind': 'LIVE', 'inferred_kind': True,
                   'start': iso(kick), 'confidence': 'likely', 'source': 'epg_timeslot', 'checked': checked_iso}
            existing = ev.setdefault('channels', [])
            if not any(c.get('channel') == row['channel'] for c in existing):
                existing.append(row)
                found.add(id(ev))

    if found:
        with open(guide_v2_path, encoding='utf-8') as f:
            pass  # already-loaded `data` holds the mutated fixtures directly (ev dicts are shared refs)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, separators=(',', ':'))
    return len(found), candidates


def match_title_comp(title, sport):
    """The actual decision used by enrich()'s title-name route, pulled out so the self-test exercises
    the REAL logic rather than a hand-copied approximation of it that could silently drift out of sync
    with a future edit here."""
    title_is_womens = bool(WOMENS_MARKER.search(title))
    for comp_rx, comp, sports in comp_name_index():
        if sport not in sports or not comp_rx.search(title):
            continue
        if title_is_womens and not WOMENS_MARKER.search(comp):
            continue
        return comp
    return None


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


# How far from a real fixture's kickoff a broadcast can sit and still plausibly BE that match --
# pre-match build-up, the match itself, post-match analysis, and ordinary EPG/timezone slop. Wide
# enough to cover genuine same-day coverage, nowhere near wide enough to cover "from the 1992/93
# season": that is hours or days off, this window is not.
COMP_GATE_WINDOW_HOURS = 4

# A comp only counts as having ground truth to gate against when BOTH hold:
#  - at least one tracked kickoff is still in the future (a comp whose only data is last week's
#    finished matches is exactly as blind to "is anything on right now" as having zero data -- and
#    gating on it is actively worse than not gating, because it looks like evidence and isn't).
#    Confirmed live: FA Cup had 15 kickoffs, all of them past, Bundesliga/La Liga/Ligue 1 had exactly
#    1 each -- gating on that is what collapsed soccer's real count, not archive clips.
#  - at least this many FUTURE kickoffs (3), so a sport whose fixture coverage is a handful of
#    marquee dates (UFC: 2 tracked future cards; Boxing: 2; BKFC: 0) doesn't get treated as a
#    complete schedule. Below this bar, "no fixture nearby" is far more likely our own coverage gap
#    than evidence the broadcast is fake.
COMP_GATE_MIN_FUTURE = 3

# Sports whose own events don't carry the same kind of kickoff: a tournament's `kick` is one round's
# tee time or session start, and real EPG coverage of a DIFFERENT round of the SAME live tournament can
# legitimately sit many hours away from it. Combat sports are excluded for a different reason -- our
# own fixture tracking there is a small curated set of marquee cards (ausportguide is the only real
# source), never comprehensive, so "no tracked card nearby" is routinely just a real regional fight
# night we don't have in the database, not evidence of anything fake.
COMP_GATE_EXEMPT_SPORTS = TOURNAMENT_SPORTS | {'boxing_mma'}


def load_comp_kickoffs(guide_v2_path):
    """Every real kickoff time guide-v2.json holds for each competition, regardless of team names --
    this is the ground truth the title-match enrichment below is checked against. Deliberately
    comp-keyed rather than team-keyed: the point is "is ANYTHING real happening in this competition
    anywhere near this broadcast", not "is this the exact match"."""
    idx = collections.defaultdict(list)
    try:
        with open(guide_v2_path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return idx
    for ev in data.get('events', []):
        comp = ev.get('comp')
        if not comp:
            continue
        try:
            idx[comp].append(datetime.datetime.fromisoformat(ev['kick']))
        except Exception:
            continue
    return idx


def comp_has_ground_truth(kicks, now):
    """Whether a comp's tracked kickoffs are actually usable evidence -- see COMP_GATE_MIN_FUTURE's
    comment for why "has some entries" isn't the real bar."""
    future = [k for k in kicks if k > now]
    return len(future) >= COMP_GATE_MIN_FUTURE


def enrich(events, fixtures, comp_kickoffs, now):
    """Two ways a competition name reaches an event, two very different levels of trust.

    The team-pair route (second loop) is evidence-backed by construction: it only fires when a real
    fixture's own kickoff is already within 12h of this broadcast AND the two team names match. No
    further check needed -- the proof is built into how the match was found.

    The title-name route (first loop) is just a word match ("Bundesliga" appears in this programme's
    title) and nothing more. That is how 41 of 42 "Champions League" programmes published on
    2026-11-11 turned out to be archive rewind clips: the title named a real competition, nothing
    tied the BROADCAST to any actual scheduled match in it. Fixed here rather than relying on the
    archive-phrase filter alone (that is defense in depth, not the real fix) -- when a competition has
    real, CURRENT tracked fixtures (see comp_has_ground_truth) and this broadcast's time doesn't sit
    near any of them, the event is dropped outright, not just left uncategorised.

    Getting "do we have ground truth" right matters as much as the gate itself: a first version of
    this checked only "is the kickoff list non-empty" and it was actively harmful, not just
    incomplete. FA Cup had 15 tracked kickoffs -- all of them already finished -- and Bundesliga/La
    Liga/Ligue 1 had exactly one each; gating real current soccer coverage against stale or
    one-data-point "evidence" is what collapsed the soccer count on the first attempt, nothing to do
    with archive clips. comp_has_ground_truth and COMP_GATE_EXEMPT_SPORTS exist because of that.
    """
    hits = gated = 0
    for e in events:
        if e.get('comp') or not (e['home'] and e['away']):
            continue
        ha, aa = norm_key(e['home']).split(), norm_key(e['away']).split()
        if not ha or not aa:
            continue
        # Same WOMENS_MARKER guard as the title-name route below, for the same reason: keying on
        # just the first word of each team name ("Arsenal Women" -> "arsenal") means a women's
        # fixture's team pair can collide with a men's fixture carrying the same club surnames.
        e_womens = bool(WOMENS_MARKER.search(e['title']) or WOMENS_MARKER.search(str(e['home'])) or WOMENS_MARKER.search(str(e['away'])))
        for f in fixtures.get(frozenset([ha[0], aa[0]]), []):
            if e_womens and not WOMENS_MARKER.search(str(f['comp'] or '')):
                continue
            # same teams can meet in several competitions in one window (an ODI and a Test). Only
            # trust the competition name when the fixture's own kickoff is near this broadcast.
            if abs((f['kick'] - e['start']).total_seconds()) <= 12 * 3600:
                e['comp'] = f['comp']
                hits += 1
                break
    keep = []
    gate_s = COMP_GATE_WINDOW_HOURS * 3600
    for e in events:
        if e.get('comp'):
            keep.append(e)
            continue
        matched = match_title_comp(e['title'], e['sport'])
        if not matched:
            keep.append(e)
            continue
        kicks = comp_kickoffs.get(matched) or []
        if e['sport'] in COMP_GATE_EXEMPT_SPORTS or not comp_has_ground_truth(kicks, now):
            # no USABLE ground truth for this comp (none tracked, or what's tracked is stale/thin) --
            # can't confirm OR deny, so don't reject on a check that has nothing real to check against
            e['comp'] = matched
            hits += 1
            keep.append(e)
            continue
        if any(abs((k - e['start']).total_seconds()) <= gate_s for k in kicks):
            e['comp'] = matched
            hits += 1
            keep.append(e)
        else:
            gated += 1
    events[:] = keep
    return hits, gated


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
              ('TATA IPL 2027 Player Retention', True),
              # canary: catches a lost \b in the line above. Written as an escape by a non-raw string
              # it becomes a literal backspace byte, the regex still compiles, and "now$" silently
              # starts matching anything ending in -now. This has cost this project time three times.
              ('Let It Snow', False),
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
    # titles that name only a league must not collapse across broadcasters; titles that name a real
    # competition must
    esig = lambda t: event_signature({'title': t})
    for label, cond in [('league-only title is vague', not esig('Live: College Basketball')
                         and not esig('Live NFL') and not esig('Live: NHL Hockey')),
                        ('competition title is specific', esig('Rugby League World Cup 2026')
                         and esig("Live: 2026 IIHF Women's World Championship")),
                        ('generic folds into specific', dpw <= tc and dpwg <= tc),
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
    # confirmed live on 2026-11-11: 41 of 42 "Champions League" programmes were archive clips, every
    # one of them with the giveaway in <sub-title>/<desc>, not <title> -- these are the exact rows, so
    # a regression here reopens the hole that caused it.
    archive = [('Leeds United v Rangers', '', 'from the 1992/93 season', True),
               ('Chelsea v Liverpool', '', 'from the 2008/09 season', True),
               ('Bundesliga Highlights', '', 'Matchday highlights from the latest round of fixtures '
                'from the 2026-2027 Bundesliga season', True),
               ('EFL Cup Best Goals: 2023/24', '', "this season's Carabao Cup has thrown up a host", True),
               ('The Best of The WBBL', '', 'A look back at a memorable encounter from the 2022 season', True),
               ('EFL 18/19: Aston Villa v Leeds Utd', '', '', True),   # short season form, no 4-digit year
               ('EFL 18/19:Leeds Utd v Derby County', '', '', True),   # no space after the colon either
               # must NOT reject a real match recap -- "goals" and "classic" alone, with no year
               # attached, are ordinary commentary language, not an archive marker.
               ('LIVE: Premier League', 'Arsenal v Chelsea',
                'A five-goal thriller as both sides traded classic late goals', False),
               ('NRL - Panthers v Broncos', '', 'Round 14 clash at Accor Stadium', False)]
    for title, sub, desc, expect_archive in archive:
        got = bool(ARCHIVE_RE.search(title) or ARCHIVE_RE.search(sub) or ARCHIVE_RE.search(desc))
        if got != expect_archive:
            bad.append(('ARCHIVE_RE', title, sub, desc, got, expect_archive))

    # fixture-kickoff gate: a competition we DO track, WITH ENOUGH FUTURE DATA TO TRUST, must prove a
    # real kickoff is nearby; thin, stale or absent data must never reject (confirmed real: FA Cup's
    # 15 kickoffs were all past, Bundesliga/La Liga/Ligue 1 had exactly 1 each -- all of this must
    # come out "no verdict", not "reject", or soccer collapses again the way the first cut did).
    gnow = datetime.datetime(2026, 11, 11, 10, 0, tzinfo=datetime.timezone.utc)
    base_kick = datetime.datetime(2026, 11, 4, 20, 0, tzinfo=datetime.timezone.utc)
    future_kick = gnow + datetime.timedelta(days=3)
    gatecases = [
        # (sport, kicks-around-"now", offset of THIS broadcast from base_kick, expect_kept)
        ('soccer', [base_kick] * 5 + [future_kick] * 3, datetime.timedelta(hours=0), True),    # the match
        ('soccer', [base_kick] * 5 + [future_kick] * 3, datetime.timedelta(hours=3), True),    # post-match
        ('soccer', [base_kick] * 5 + [future_kick] * 3, datetime.timedelta(days=7, hours=14), False),  # archive clip
        ('soccer', [], datetime.timedelta(days=7), True),                      # zero data: don't guess
        ('soccer', [base_kick] * 15, datetime.timedelta(hours=0), True),       # 15 kickoffs, ALL past: FA Cup's case
        ('soccer', [future_kick], datetime.timedelta(days=7), True),           # exactly 1 future: still too thin
        ('boxing_mma', [future_kick] * 10, datetime.timedelta(days=7), True),  # dense data, EXEMPT sport anyway
        ('golf', [future_kick] * 10, datetime.timedelta(days=7), True),       # dense data, EXEMPT (tournament)
    ]
    for sport, kicks, offset, expect_kept in gatecases:
        start = base_kick + offset
        gate_s = COMP_GATE_WINDOW_HOURS * 3600
        exempt = sport in COMP_GATE_EXEMPT_SPORTS or not comp_has_ground_truth(kicks, gnow)
        got_kept = exempt or any(abs((k - start).total_seconds()) <= gate_s for k in kicks)
        if got_kept != expect_kept:
            bad.append(('comp_gate', sport, len(kicks), offset, got_kept, expect_kept))

    # confirmed live and wrong on 2026-11-11: a real women's fixture tagged as the MEN'S competition,
    # because "Champions League" is a substring of "Women's Champions League". Must now come out
    # uncategorised (LEAGUES has no women's entry to attach instead), never relabelled as the men's comp.
    womens_cases = [
        ("Live: UEFA Women's Champions League Soccer", 'soccer', None),
        ("LIVE: UEFA Women's Champions League - OL Lyonnes vs. Juventus", 'soccer', None),
        ("UEFA Women's Champions League - Arsenal vs. Barcelona", 'soccer', None),
        # a real men's fixture must still attach normally -- the guard must not swallow everything
        ('UEFA Champions League Football', 'soccer', 'Champions League'),
    ]
    for title, sport, expect in womens_cases:
        got = match_title_comp(title, sport)
        if got != expect:
            bad.append(('match_title_comp', title, got, expect))

    for b in bad:
        print('  SELFTEST FAIL', b)
    print('selftest: %d checks, %d failures'
          % (len(cases) + len(idc) + len(atc) + len(dsc) + len(studio) + len(archive) + len(gatecases)
             + len(womens_cases) + 8, len(bad)))
    return not bad


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='public')
    ap.add_argument('--cache', default='.epgcache')
    ap.add_argument('--guide-v2', default=None, help='guide-v2.json to use as the enrichment layer')
    # Same reason resolve_v2.py has one: actions/deploy-pages replaces the WHOLE site, so a job that
    # writes only guides/ and deploys would erase guide-v2.json, the XMLTV guide and every per-site
    # file. Mirror what is live first, add guides/ to it, then deploy the lot.
    ap.add_argument('--mirror-base', default='',
                    help='Pages base URL to mirror the currently-published site from first')
    args = ap.parse_args()

    if not selftest():
        sys.exit('classifier/extractor self-test failed -- refusing to publish')

    if args.mirror_base:
        from resolve_v2 import mirror_published
        mirror_published(args.mirror_base, args.out)

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
    comp_kickoffs = load_comp_kickoffs(gv2)
    before_gate = len(events)
    hits, gated = enrich(events, fixtures, comp_kickoffs, now)

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
    print('rejected by the fixture-kickoff gate (named a real comp, no real kickoff nearby): %d/%d'
          % (gated, before_gate))
    print('overlapping broadcasts merged across networks: %d (%d -> %d)'
          % (after_enrich - len(events), after_enrich, len(events)))
    print('matchups joined from fixture slots: %d   left alone as ambiguous: %d' % (joined, ambiguous))
    with_matchup = sum(1 for e in events if e['home'] and e['away'])
    print('events with a matchup: %d/%d (%d%%)'
          % (with_matchup, len(events), 100 * with_matchup // max(1, len(events))))
    print('canonical broadcasters seen: %d   now_next entries: %d' % (len(channels), len(nn['channels'])))


if __name__ == '__main__':
    main()
