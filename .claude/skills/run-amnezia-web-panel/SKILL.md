---
name: run-amnezia-web-panel
description: Build, launch and drive the Amnezia Web Panel locally - start the FastAPI app on an isolated data.json, call its REST API with a bearer token, screenshot or script its UI through Chrome DevTools, and run its test suite. Use when asked to run, start, serve, test, screenshot, click through or debug the panel.
---

# Run the Amnezia Web Panel

FastAPI app (`app.py`, ~5600 lines) that manages VPN servers over SSH. It serves
a Jinja UI and 91 JSON routes on port 5000. Everything here is driven by
`.claude/skills/run-amnezia-web-panel/driver.py`, which launches the panel,
mints a bearer token, and talks to Chrome over the DevTools Protocol so a page
can be clicked and screenshotted without a human.

Paths below are relative to the repository root. Two shortcuts used throughout:

```bash
DRV=.claude/skills/run-amnezia-web-panel/driver.py
PY=/tmp/awp-run/venv/bin/python
```

## Prerequisites

```bash
sudo apt-get install -y python3-venv        # only if `python3 -m venv` fails
```

`google-chrome` is needed for `shot` / `eval` only (verified with 150.0.7871.114).
Point `AWP_CHROME` at another binary if yours is named differently. `node` is
optional: without it `tests/test_template_js.py` skips itself.

## Setup

```bash
python3 $DRV venv        # creates /tmp/awp-run/venv, installs requirements.txt (~6 s)
```

Everything else runs with `$PY`, not the system python - the driver imports
`websockets`, which comes from the panel's own requirements.

## Run (agent path)

One command proves the whole stack - launch, API call, real UI login, screenshot:

```bash
$PY $DRV smoke           # prints SMOKE OK, leaves /tmp/awp-run/dashboard.png
```

Piece by piece:

```bash
$PY $DRV up              # start on :5000, print URL + bearer token (--port to move it)
                         #   --port writes settings.ssl.panel_port into the run
                         #   dir's data.json - app.py has no port flag of its own
$PY $DRV seed            # add a fake server so the UI is not an empty list
$PY $DRV api GET /api/exit-nodes
$PY $DRV api POST /api/settings/tokens '{"name":"scratch"}'
$PY $DRV shot /tmp/shot.png /server/0 --full --wait 5
$PY $DRV eval "MARKETPLACE_APPS.map(a=>a.proto).join(',')" /server/0
$PY $DRV logs 60
$PY $DRV down
```

The run directory is `/tmp/awp-run` (override with `AWP_RUN_DIR`): `data.json`,
`panel.log`, `token`, screenshots. **The repo stays clean** - see the first gotcha.

`shot` and `eval` log in through the actual form (`#username`, `#password`,
`#loginBtn`), so they exercise the same path a user takes. Extra flags:

- `--full` - resize the viewport to the document and capture the whole page
- `--wait SEC` - settle time before the shot (default 1.5)
- `--js "expr"` - run JS after load, before the shot: opens modals, scrolls

Opening a modal and shooting the card inside it:

```bash
$PY $DRV shot /tmp/exit-card.png /server/0 --wait 2 --js \
  "(async()=>{openMarketplaceModal(); await new Promise(r=>setTimeout(r,800));
    [...document.querySelectorAll('#marketplaceModal *')]
      .filter(e=>e.textContent.includes('Exit Node')).pop()
      .scrollIntoView({block:'center'})})()"
```

Endpoint names are guesswork otherwise - dump the real list instead:

```bash
$PY $DRV up >/dev/null; curl -s http://127.0.0.1:5000/openapi.json \
  | $PY -c "import json,sys; [print(m.upper(), p) for p,v in json.load(sys.stdin)['paths'].items() for m in v]"
```

## Direct invocation (no panel, no SSH)

Most changes here land in `managers/*.py`, and those are pure string builders -
import and call them, it is far faster than a UI round trip:

```bash
$PY -c "
from managers.awg_manager import AWGManager, EXIT_MTU
print(EXIT_MTU)
print(AWGManager._exit_conf_body({'exit_uid':'abc','exit_name':'Berlin-1',
  'transit_ip':'10.9.0.7','exit_public_key':'PUB=','psk':'PSK=',
  'endpoint_host':'203.0.113.5','endpoint_port':'55520','obfuscation':False}))
"
```

## Test

```bash
$PY -m unittest discover -s tests        # 209 tests, ~0.5 s, no network
```

A `TypeError: _dispatch_callback_with_service...` line scrolls past mid-run.
It is a deliberately provoked failure inside a test, not a broken suite - trust
the `OK` on the last line.

## Run (human path)

```bash
DATA_FILE=/tmp/awp-run/data.json python3 app.py     # http://127.0.0.1:5000, admin/admin
```

Blocks in the foreground; Ctrl-C to stop. It gives you nothing the driver does
not, and it is easy to forget `DATA_FILE`.

## Gotchas

- **Never launch without `DATA_FILE`.** The panel writes `data.json` next to
  `app.py`, and that file stores SSH passwords of real servers in plaintext. The
  driver always points it at the run directory; the human path above must too.
- **`pkill -f app.py` kills the shell that runs it.** The pattern matches the
  command's own command line. Use `$PY $DRV down`, or kill by port:
  `ss -ltnp | awk '/:5000 /{print $NF}' | grep -oP 'pid=\K[0-9]+'`.
- **Seed with `127.0.0.1`, not a routable dead IP.** Nothing listens on :22
  locally, so `check_server` fails instantly and the page settles on
  `CONNECTION ERROR`. With something like `198.51.100.10` every protocol card
  spins on "Checking server services..." for the full SSH timeout.
- **An unreachable server hides its protocol cards.** `/server/0` renders "No
  installed applications yet" even though `data.json` lists `awg2`. To look at
  protocol UI without a real host, open the Templates modal instead
  (`openMarketplaceModal()`).
- **`location.href = ...` breaks the CDP session.** The execution context is
  torn down mid-navigation and `Runtime.evaluate` fails with a bare `Uncaught`.
  The driver navigates via `Page.navigate` and polls tolerantly; keep that
  pattern if you extend it.
- **`--js` fires as soon as the page settles.** Modals render asynchronously, so
  wrap anything that touches modal contents in an async IIFE with its own
  `setTimeout` - the driver awaits the promise.
- **Card text is not in a leaf node.** Titles carry an emoji and a `NEW` badge,
  so `e.children.length === 0` matches nothing. Filter by `textContent` and take
  `.pop()` for the deepest hit.
- **A seeded server makes some endpoints return 500, by design.** `panel.log`
  fills with `ConnectionError: SSH to 127.0.0.1 recently failed, backing off 30s`
  and `POST /api/servers/0/stats` answers 500. That is the panel reporting an
  unreachable host, not a bug - anything that needs live SSH needs a real one.
- **There is no `/api/servers`.** Servers are addressed by array index:
  `/server/0`, `/api/servers/0/check`. Read `/openapi.json` before guessing.
- **First start creates `admin` / `admin`** and captcha defaults to off, so the
  scripted login works on a fresh run directory.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `port 5000 is taken by something else` | Another panel or an old run. `$PY $DRV down`, or `$PY $DRV up --port 5001`. |
| `stale pid file removed - that process is not our panel` | The pid file outlived its panel (a crash, or the run dir was recreated). `down` refuses to signal a pid it cannot confirm, so nothing was killed; just `up` again. |
| `panel died on startup, log above` | Deps missing or `DATA_FILE` unwritable. Re-run `python3 $DRV venv`, check `$PY $DRV logs`. |
| `AttributeError: module 'urllib.request' has no attribute 'open'` | Stale driver copy - `urlopen` is the correct call; re-pull this file. |
| `chrome did not expose its debugging port` | `google-chrome` missing or sandboxed. Install it, or set `AWP_CHROME`. |
| `login did not leave /login` | Wrong credentials in the run dir, or captcha got enabled in `data.json`. Delete `/tmp/awp-run/data.json` and `up` again. |
| `never landed on /path` | The path 404s or redirects. Confirm it exists in `/openapi.json`. |
| `RuntimeError: Uncaught` from `--js` / `eval` | Your JS threw. Test it with `eval` first - it prints the same error without wasting a screenshot. |
