# Red Carpet Guide

A free sports TV guide for the Red Carpet Streams app. It runs the open **iptv-org/epg** grabbers three times a day,
keeps only sports channels, and publishes one file the app reads:

    https://YOUR-GITHUB-NAME.github.io/YOUR-REPO-NAME/sports.xml.gz

and a status page at the same address without the file name, showing every source, how many programmes it returned and
how far ahead it reaches.

## Set it up (about 5 minutes, once)

1. On github.com press **+** (top right) → **New repository**. Name it `rcs-guide`, choose **Public** (free Pages and free
   Actions minutes need a public repository; the guide holds only public TV listings), then **Create repository**.
2. On the new repository's page choose **uploading an existing file**, drag in **everything inside this folder**
   (including the hidden `.github` folder), and press **Commit changes**.
3. **Settings → Pages → Source: GitHub Actions.**
4. **Actions** tab → **Build the Red Carpet guide** → **Run workflow**. The first run takes a while (up to about 1–2 hours,
   because it reads each broadcaster's guide page by page). Later runs are faster.
5. When it finishes, open `https://YOUR-GITHUB-NAME.github.io/rcs-guide/` — you should see the status page.
6. In the app: **Settings → Red Carpet sports guide**, and paste
   `https://YOUR-GITHUB-NAME.github.io/rcs-guide/sports.xml.gz`

## What to know

* **A source can fail on any run** (a broadcaster changes its site, or blocks the grabber). That never stops the others, and
  a source that returns far less than last time keeps its last good data. The status page shows which.
* **If every source fails, nothing is published**, so the live guide is never replaced by an empty one.
* **Edit `config/sites.json`** to add or remove sources, change how many days ahead (`days`), or change which channels count
  as sports (`sports_regex`). Sources must exist in the iptv-org/epg project (its `sites` folder).
* **If the workflow turns red,** open the failed run, copy the red lines of the log, and send them over; the usual cause is
  the grabber's command-line options changing, which is a one-line fix in `scripts/build_guide.py`.
* **Test the builder offline:** `python3 scripts/test_build.py`
