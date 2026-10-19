#!/usr/bin/env python3
"""Red Carpet guide builder.

Runs the open iptv-org/epg grabbers for the sites in config/sites.json, keeps only sports channels
(plus sport programmes on a few free-to-air channels), and publishes ONE merged XMLTV file.

  build_guide.py fetch-prev --base https://OWNER.github.io/REPO --dir prev
  build_guide.py build --epg epg --out public [--prev prev] [--only a.com,b.com]

Design rules
  * A site that is missing, slow or broken never stops the others.
  * If a site returns far less than last time, its last good data is kept (and the page says so).
  * If a channel is offered by two sites, the one reaching furthest ahead wins (no duplicates).
  * If NOTHING usable came out, the build fails, so the live guide is never replaced by an empty one.

Environment
  GRAB_CMD   how to run the grabber. Default tries "npm run grab ---" and then "npm run grab --".
"""
import argparse, glob, gzip, html, json, os, re, shlex, subprocess, sys, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_cfg():
    with open(ROOT / 'config' / 'sites.json', encoding='utf-8') as f:
        return json.load(f)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- time and files
def xmltv_dt(s):
    m = re.match(r'\s*(\d{14})\s*([+-]\d{4})?', s or '')
    if not m:
        return None
    dt = datetime.strptime(m.group(1), '%Y%m%d%H%M%S')
    off = m.group(2) or '+0000'
    delta = timedelta(hours=int(off[1:3]), minutes=int(off[3:5])) * (1 if off[0] == '+' else -1)
    return (dt - delta).replace(tzinfo=timezone.utc)


def read_xml(path):
    data = Path(path).read_bytes()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return ET.fromstring(data)


def write_gz(root, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    raw = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    with gzip.GzipFile(path, 'wb', compresslevel=9, mtime=0) as g:
        g.write(raw)


# ---------------------------------------------------------------- channel selection
def filter_channels(files, cfg):
    sports = re.compile(cfg['sports_regex'], re.I)
    always = re.compile(cfg['always_regex'], re.I)
    found = {}
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for ch in root.iter('channel'):
            xid = (ch.get('xmltv_id') or '').strip()
            sid = (ch.get('site_id') or '').strip()
            name = (ch.text or '').strip()
            if not xid or not sid or not name:
                continue                       # a channel the project has not mapped to an id is no use to us
            if sports.search(name) or sports.search(xid):
                kind = 'sports'
            elif always.search(name):
                kind = 'always'
            else:
                continue
            found.setdefault((sid, xid), {'site': ch.get('site') or '', 'site_id': sid, 'xmltv_id': xid,
                                          'lang': ch.get('lang') or 'en', 'name': name, 'kind': kind})
    chans = sorted(found.values(), key=lambda c: (c['kind'] != 'sports', c['name'].lower()))
    return chans[: int(cfg.get('max_channels_per_site', 180))]


def write_channels(path, chans):
    root = ET.Element('channels')
    for c in chans:
        e = ET.SubElement(root, 'channel', {'site': c['site'], 'lang': c['lang'], 'xmltv_id': c['xmltv_id'], 'site_id': c['site_id']})
        e.text = c['name']
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


# ---------------------------------------------------------------- running the grabber
def run_grab(epg_dir, channels_xml, out_xml, days, max_conn, timeout_s):
    custom = shlex.split(os.environ.get('GRAB_CMD', ''))
    styles = [custom] if custom else [['npm', 'run', 'grab', '---'], ['npm', 'run', 'grab', '--']]
    args = ['--channels=' + str(Path(channels_xml).resolve()), '--output=' + str(Path(out_xml).resolve()),
            '--days=' + str(days), '--maxConnections=' + str(max_conn)]
    tail = ''
    for style in styles:
        try:
            r = subprocess.run(style + args, cwd=epg_dir, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False, 'timed out after %d minutes' % (timeout_s // 60)
        except FileNotFoundError as e:
            return False, 'could not start the grabber (%s)' % e
        out = (r.stdout or '') + (r.stderr or '')
        tail = out[-500:].strip()
        if r.returncode == 0 and Path(out_xml).exists():
            return True, tail
        if not re.search(r'unknown (option|argument)|error: unknown', out, re.I):
            break                              # a real failure, not just the wrong way of passing options
    return False, tail


# ---------------------------------------------------------------- tidy one site's output
def prog_text(p):
    return ' '.join((e.text or '') for e in p if e.tag in ('title', 'sub-title', 'desc', 'category'))


def tidy(root, kinds, sport_re):
    """Free-to-air style channels keep only their sport; sports channels keep everything."""
    for p in list(root.findall('programme')):
        if kinds.get(p.get('channel')) == 'always' and not sport_re.search(prog_text(p)):
            root.remove(p)
    used = {p.get('channel') for p in root.findall('programme')}
    for c in list(root.findall('channel')):
        if c.get('id') not in used:
            root.remove(c)
    return root


def stats(root):
    progs = root.findall('programme')
    stops = [xmltv_dt(p.get('stop') or p.get('start')) for p in progs]
    stops = [s for s in stops if s]
    return len(progs), len({p.get('channel') for p in progs}), (max(stops) if stops else None)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ') if dt else None


def process_site(site_cfg, cfg, args, work):
    site = site_cfg['site']
    st = {'site': site, 'label': site_cfg.get('label', site), 'status': 'skipped', 'channels': 0, 'programmes': 0,
          'horizon': None, 'note': '', 'seconds': 0}
    t0 = time.time()
    files = sorted(glob.glob(str(Path(args.epg) / 'sites' / site / '*.channels.xml')))
    if not files:
        st['note'] = 'not offered by the grabber project (skipped)'
        return st, None
    chans = filter_channels(files, cfg)
    if not chans:
        st['note'] = 'no sports channels found in its channel list'
        return st, None
    kinds = {c['xmltv_id']: c['kind'] for c in chans}
    ch_xml, out_xml = work / (site + '.channels.xml'), work / (site + '.xml')
    write_channels(ch_xml, chans)
    log('::group::%s: %d channels' % (site, len(chans)))
    ok, tail = run_grab(args.epg, ch_xml, out_xml, cfg['days'], cfg['max_connections'], int(cfg['site_timeout_minutes']) * 60)
    log(tail)
    log('::endgroup::')
    new_root = None
    if ok:
        try:
            new_root = tidy(ET.parse(out_xml).getroot(), kinds, re.compile(cfg['sport_programme_regex'], re.I))
        except ET.ParseError as e:
            ok, tail = False, 'its output could not be read (%s)' % e
    n_new = stats(new_root)[0] if new_root is not None else 0
    prev_root, n_prev = None, 0
    pf = Path(args.prev) / (site + '.xml.gz') if args.prev else None
    if pf and pf.exists():
        try:
            prev_root = read_xml(pf)
            n_prev = stats(prev_root)[0]
        except Exception:
            prev_root = None
    keep_prev = prev_root is not None and (n_new == 0 or n_new < cfg.get('keep_previous_if_below', 0.4) * n_prev)
    if keep_prev:
        root, st['status'] = prev_root, 'kept previous'
        st['note'] = ('this run returned %d programmes (last good run had %d)%s' % (n_new, n_prev, '' if ok else '; ' + tail[-160:])).strip()
    elif n_new > 0:
        root, st['status'] = new_root, 'ok'
    else:
        st['status'], st['note'] = 'failed', (tail[-200:] or 'no programmes came back')
        st['seconds'] = round(time.time() - t0)
        log('::warning::%s: %s' % (site, st['note']))
        return st, None
    n, c, hz = stats(root)
    st.update(channels=c, programmes=n, horizon=iso(hz), seconds=round(time.time() - t0))
    if st['status'] == 'failed' or st['status'] == 'kept previous':
        log('::warning::%s: %s' % (site, st['note']))
    return st, root


# ---------------------------------------------------------------- merge
def merge(site_roots):
    """One entry per channel id: the site that reaches furthest ahead (then has most programmes) wins."""
    best = {}
    for site, root in site_roots:
        chs = {c.get('id'): c for c in root.findall('channel')}
        by = {}
        for p in root.findall('programme'):
            by.setdefault(p.get('channel'), []).append(p)
        for cid, plist in by.items():
            stops = [xmltv_dt(p.get('stop') or p.get('start')) for p in plist]
            stops = [s for s in stops if s]
            score = ((max(stops).replace(minute=0, second=0) if stops else datetime.min.replace(tzinfo=timezone.utc)), len(plist))
            if cid not in best or score > best[cid][0]:
                best[cid] = (score, site, chs.get(cid), plist)
    tv = ET.Element('tv', {'generator-info-name': 'Red Carpet Guide', 'generator-info-url': 'https://github.com/'})
    for cid in sorted(best):
        _, _, ch, plist = best[cid]
        if ch is None:
            ch = ET.Element('channel', {'id': cid})
            ET.SubElement(ch, 'display-name').text = cid
        tv.append(ch)
    for cid in sorted(best):
        for p in sorted(best[cid][3], key=lambda p: p.get('start') or ''):
            tv.append(p)
    return tv, {cid: best[cid][1] for cid in best}


# ---------------------------------------------------------------- the status page
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Red Carpet Guide</title><style>
:root{color-scheme:light dark;--bg:#fbf6ee;--fg:#2a1a14;--mut:#7a6558;--ok:#1f7a3d;--warn:#b26a00;--bad:#b3261e;--line:#e6d9c8}
@media (prefers-color-scheme:dark){:root{--bg:#1a0d10;--fg:#f4e6d6;--mut:#b39a86;--ok:#5fd08a;--warn:#f0b04a;--bad:#ff8a80;--line:#3a2226}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:1.2rem;max-width:56rem;margin-inline:auto}
h1{margin:.2rem 0}p{color:var(--mut)}table{width:100%;border-collapse:collapse;margin-top:1rem}th,td{text-align:left;padding:.5rem .4rem;border-bottom:1px solid var(--line);vertical-align:top}
.ok{color:var(--ok)}.kept{color:var(--warn)}.failed{color:var(--bad)}.skipped{color:var(--mut)}code{background:var(--line);padding:.1rem .35rem;border-radius:.3rem}
.wrap{overflow-x:auto}</style></head><body>
<h1>Red Carpet Guide</h1><p>Sports TV listings built from the broadcasters' own guides. Updated __GEN__ UTC.</p>
<p>Guide address for the app: <code>sports.xml.gz</code> in this same folder.<br>__TOT__</p>
<div class="wrap"><table><tr><th>Source</th><th>Status</th><th>Channels</th><th>Programmes</th><th>Reaches</th></tr>__ROWS__</table></div>
<p>Anything marked "kept previous" returned too little this time, so its last good data is still being served.</p></body></html>"""


def write_page(path, status):
    rows = []
    for s in status['sites']:
        cls = {'ok': 'ok', 'kept previous': 'kept', 'failed': 'failed'}.get(s['status'], 'skipped')
        note = ('<br><small>%s</small>' % html.escape(s['note'])) if s['note'] else ''
        rows.append('<tr><td>%s<br><small>%s</small></td><td class="%s">%s%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            html.escape(s['label']), html.escape(s['site']), cls, html.escape(s['status']), note, s['channels'] or '', s['programmes'] or '',
            (s['horizon'] or '')[:16].replace('T', ' ')))
    tot = '%d channels · %d programmes · reaches %s UTC' % (status['channels'], status['programmes'], (status['horizon'] or '?')[:16].replace('T', ' '))
    Path(path).write_text(PAGE.replace('__GEN__', status['generated'][:16].replace('T', ' ')).replace('__TOT__', tot).replace('__ROWS__', '\n'.join(rows)), encoding='utf-8')


# ---------------------------------------------------------------- commands
def cmd_build(args):
    cfg = load_cfg()
    out, work = Path(args.out), Path(args.work)
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    only = set(filter(None, (args.only or '').split(',')))
    statuses, roots = [], []
    for sc in cfg['sites']:
        if only and sc['site'] not in only:
            continue
        st, root = process_site(sc, cfg, args, work)
        statuses.append(st)
        log('%-20s %-14s %4s channels %6s programmes  %s' % (st['site'], st['status'], st['channels'], st['programmes'], st['note'][:90]))
        if root is not None:
            roots.append((sc['site'], root))
            write_gz(root, out / 'sites' / (sc['site'] + '.xml.gz'))
    if not roots:
        log('::error::No site produced any programmes, so the live guide was left as it was.')
        return 1
    tv, owner = merge(roots)
    write_gz(tv, out / 'sports.xml.gz')
    n, c, hz = stats(tv)
    status = {'generated': iso(datetime.now(timezone.utc)), 'channels': c, 'programmes': n, 'horizon': iso(hz), 'sites': statuses}
    (out / 'status.json').write_text(json.dumps(status, indent=1), encoding='utf-8')
    write_page(out / 'index.html', status)
    (out / '.nojekyll').write_text('')
    log('Merged guide: %d channels, %d programmes, reaches %s' % (c, n, iso(hz)))
    return 0


def cmd_fetch_prev(args):
    dest = Path(args.dir)
    dest.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip('/')

    def get(url, to):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'rcs-guide'}), timeout=60) as r:
                Path(to).write_bytes(r.read())
            return True
        except Exception as e:
            log('  (not available: %s)' % e)
            return False

    if not get(base + '/status.json', dest / 'status.json'):
        log('No earlier guide found, which is normal on the first run.')
        return 0
    try:
        sites = [s['site'] for s in json.loads((dest / 'status.json').read_text())['sites'] if s['status'] in ('ok', 'kept previous')]
    except Exception:
        return 0
    for s in sites:
        get('%s/sites/%s.xml.gz' % (base, s), dest / (s + '.xml.gz'))
    log('Fetched the previous data for %d sites.' % len(list(dest.glob('*.xml.gz'))))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--epg', default='epg')
    b.add_argument('--out', default='public')
    b.add_argument('--work', default='work')
    b.add_argument('--prev', default='prev')
    b.add_argument('--only', default='')
    f = sub.add_parser('fetch-prev')
    f.add_argument('--base', required=True)
    f.add_argument('--dir', default='prev')
    a = ap.parse_args()
    sys.exit(cmd_build(a) if a.cmd == 'build' else cmd_fetch_prev(a))


if __name__ == '__main__':
    main()
