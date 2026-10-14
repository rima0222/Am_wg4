# Standalone user portal (optional)

Deploy this folder on GitHub Pages so users always have one stable link to check
their usage and grab their config, even if the VPN server's IP address changes
or the panel's own address gets blocked.

## Setup

1. Edit `mirrors.json` and list every server address that runs the panel
   (add a new one whenever you spin up a replacement server):
   ```json
   { "mirrors": ["https://1.2.3.4:8787", "https://5.6.7.8:8787"] }
   ```
2. Push this folder to a GitHub repo (can be the same repo, this folder, or
   a separate one).
3. In the repo settings, enable **GitHub Pages** for this folder
   (Settings → Pages → deploy from branch → folder: `/portal-standalone`).
4. Share the resulting `https://<user>.github.io/<repo>/` link with your users
   once — that link doesn't change even when the backend server does.

## How it fails gracefully

The page tries, in order: the last server that worked for it, the current
page's own origin, then every address in `mirrors.json` — using the first one
that responds. If a server becomes unreachable, it automatically tries the
next one on the next login/reload.

**Limits, to be upfront about:** this does not make the VPN itself unblockable.
It only means you don't have to redistribute a new link to every user each
time a server changes — you just add the new address to `mirrors.json`. If
GitHub Pages itself is blocked in a region, this specific portal page won't
be reachable either.
