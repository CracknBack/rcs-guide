#!/usr/bin/env python3
"""Offline test of the builder, using a stand-in grabber. Run:  python3 scripts/test_build.py"""
import gzip, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
import xml.etree.ElementTree as ET
ROOT = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp())
(T / 'epg/sites/fake.one').mkdir(parents=True); (T / 'epg/sites/fake.two').mkdir(parents=True)
(T / 'epg/sites/fake.one/fake.one_uk.channels.xml').write_text('''<?xml version="1.0"?><channels>
<channel site="fake.one" lang="en" xmltv_id="SkySportsMainEvent.uk" site_id="a1">Sky Sports Main Event</channel>
<channel site="fake.one" lang="en" xmltv_id="TNTSports1.uk" site_id="a2">TNT Sports 1</channel>
<channel site="fake.one" lang="en" xmltv_id="BBCOne.uk" site_id="b1">BBC One</channel>
<channel site="fake.one" lang="en" xmltv_id="CookingTV.uk" site_id="c1">Cooking Channel</channel>
<channel site="fake.one" lang="en" xmltv_id="" site_id="d1">Sky Sports Unmapped</channel>
</channels>''')
(T / 'epg/sites/fake.two/fake.two_uk.channels.xml').write_text('''<?xml version="1.0"?><channels>
<channel site="fake.two" lang="en" xmltv_id="SkySportsMainEvent.uk" site_id="z1">Sky Sports Main Event</channel>
<channel site="fake.two" lang="en" xmltv_id="StanSport.au" site_id="z2">Stan Sport</channel>
</channels>''')
(T / 'fake_grab.py').write_text('''
import sys, os, re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
a = dict(x[2:].split('=', 1) for x in sys.argv[1:] if x.startswith('--') and '=' in x)
site = os.path.basename(a['output']).replace('.xml', '')
if os.environ.get('FAKE_FAIL') == site: sys.exit(1)
days = int(os.environ.get('FAKE_DAYS_' + site.replace('.', '_'), a['days']))
ch = ET.parse(a['channels']).getroot()
tv = ET.Element('tv'); now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
f = lambda d: d.strftime('%Y%m%d%H%M%S +0000')
for c in ch.iter('channel'):
    e = ET.SubElement(tv, 'channel', {'id': c.get('xmltv_id')}); ET.SubElement(e, 'display-name').text = c.text
    for i in range(days * 8):
        t = now + timedelta(hours=3 * i)
        p = ET.SubElement(tv, 'programme', {'start': f(t), 'stop': f(t + timedelta(hours=3)), 'channel': c.get('xmltv_id')})
        ET.SubElement(p, 'title').text = 'Premier League: Arsenal v Chelsea' if i == 1 else ('EastEnders' if 'BBC' in c.text else 'Filler %d' % i)
ET.ElementTree(tv).write(a['output'], encoding='utf-8')
''')
cfg = json.loads((ROOT / 'config/sites.json').read_text())
cfg['sites'] = [{'site': 'fake.one', 'label': 'Fake one'}, {'site': 'fake.two', 'label': 'Fake two'}, {'site': 'not.there', 'label': 'Missing'}]
cfg['days'] = 2
shutil.copytree(ROOT, T / 'proj', ignore=shutil.ignore_patterns('.git', '__pycache__'))
(T / 'proj/config/sites.json').write_text(json.dumps(cfg))
env = dict(os.environ, GRAB_CMD='%s %s' % (sys.executable, T / 'fake_grab.py'))
def build(extra_env=None, prev=None, only=''):
    e = dict(env, **(extra_env or {}))
    return subprocess.run([sys.executable, str(T / 'proj/scripts/build_guide.py'), 'build', '--epg', str(T / 'epg'), '--out', str(T / 'out'), '--work', str(T / 'work'),
                           '--prev', str(prev or T / 'noprev'), '--only', only], env=e, capture_output=True, text=True)
def load(path): return ET.fromstring(gzip.decompress(Path(path).read_bytes()))
ok = True
def check(name, cond, extra=''):
    global ok; ok &= bool(cond); print(('PASS' if cond else 'FAIL'), name, extra)

# 1. normal run
r = build(); st = json.loads((T / 'out/status.json').read_text()); by = {s['site']: s for s in st['sites']}
check('run 1 succeeds', r.returncode == 0, r.stderr[-200:])
check('unmapped / non-sport channels are left out', {c.get('id') for c in load(T / 'out/sports.xml.gz').findall('channel')} == {'SkySportsMainEvent.uk', 'TNTSports1.uk', 'BBCOne.uk', 'StanSport.au'})
check('a missing site is skipped, not fatal', by['not.there']['status'] == 'skipped')
tv = load(T / 'out/sports.xml.gz')
bbc = [p for p in tv.findall('programme') if p.get('channel') == 'BBCOne.uk']
check('free-to-air channel keeps only its sport', bbc and all('Arsenal' in p.find('title').text for p in bbc), '%d programmes' % len(bbc))
sky = [p for p in tv.findall('programme') if p.get('channel') == 'SkySportsMainEvent.uk']
check('a duplicated channel appears once', len(sky) == len({p.get('start') for p in sky}), '%d programmes' % len(sky))

# 2. the other site reaches further, so it should win the duplicated channel
shutil.rmtree(T / 'out'); r = build({'FAKE_DAYS_fake_two': '5'})
check('the site reaching furthest ahead wins a shared channel', len([p for p in load(T / 'out/sports.xml.gz').findall('programme') if p.get('channel') == 'SkySportsMainEvent.uk']) == 5 * 8)

# 3. a failed site keeps its last good data
shutil.rmtree(T / 'out'); build()
prev = T / 'prev'; shutil.copytree(T / 'out/sites', prev); shutil.rmtree(T / 'out')
r = build({'FAKE_FAIL': 'fake.two'}, prev=prev); st = json.loads((T / 'out/status.json').read_text()); by = {s['site']: s for s in st['sites']}
check('a failed site keeps its previous data', by['fake.two']['status'] == 'kept previous', by['fake.two']['note'][:60])
check('...and its channel is still in the guide', 'StanSport.au' in {c.get('id') for c in load(T / 'out/sports.xml.gz').findall('channel')})

# 4. everything failing must not publish an empty guide
shutil.rmtree(T / 'out'); r = build({'FAKE_FAIL': 'fake.one'}, only='fake.one')
check('nothing usable -> the build fails (live guide untouched)', r.returncode == 1 and not (T / 'out/sports.xml.gz').exists())

# 5. the page and status are written
shutil.rmtree(T / 'out'); build()
check('status page and status.json are written', (T / 'out/index.html').exists() and (T / 'out/status.json').exists() and (T / 'out/.nojekyll').exists())
print('\nALL PASSED' if ok else '\nSOME FAILED'); sys.exit(0 if ok else 1)
