#!/usr/bin/env python3
"""Drive a local Amnezia Web Panel: launch it, call its API, screenshot its UI.

Everything lives in a run directory (default /tmp/awp-run) so the repo stays
clean and the panel never touches a real data.json - the panel would happily
create one next to app.py, and that file holds plaintext SSH credentials.

Usage:
    driver.py venv                 create /tmp/awp-run/venv and install requirements
    driver.py up [--port 5000]     start the panel, print URL + bearer token
    driver.py seed                 add a fake server so the UI is not empty
    driver.py api GET /api/settings [JSON_BODY]
    driver.py shot out.png [/path] [--full] [--wait SEC] [--js "expr"]
                                   screenshot a page, logging in through the UI;
                                   --js runs before the shot (open a modal, etc)
    driver.py eval "js" [/path]    run JS in a logged-in page, print the result
    driver.py logs [N]             tail the panel log
    driver.py down                 stop the panel
    driver.py smoke                up + seed + api + shot + down, exit 1 on failure

Requires the panel's own venv (websockets comes from requirements.txt) and
google-chrome for the screenshot commands.
"""
import asyncio
import base64
import http.cookiejar
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
RUN_DIR = os.environ.get('AWP_RUN_DIR', '/tmp/awp-run')
PORT = int(os.environ.get('AWP_PORT', '5000'))
ADMIN = ('admin', 'admin')
CHROME = os.environ.get('AWP_CHROME', 'google-chrome')


def paths():
    return {
        'data': os.path.join(RUN_DIR, 'data.json'),
        'tunnels': os.path.join(RUN_DIR, 'tunnels_state.json'),
        'log': os.path.join(RUN_DIR, 'panel.log'),
        'pid': os.path.join(RUN_DIR, 'panel.pid'),
        'token': os.path.join(RUN_DIR, 'token'),
        'chrome_profile': os.path.join(RUN_DIR, 'chrome'),
    }


def base_url():
    return f'http://127.0.0.1:{PORT}'


# ----------------------------------------------------------------- lifecycle

def cmd_venv(argv):
    """The panel's deps are not installed system-wide, and the driver itself
    needs `websockets` from requirements.txt - so both run from this venv."""
    os.makedirs(RUN_DIR, exist_ok=True)
    venv_dir = os.path.join(RUN_DIR, 'venv')
    if not os.path.exists(os.path.join(venv_dir, 'bin', 'python')):
        subprocess.check_call([sys.executable, '-m', 'venv', venv_dir])
    pip = os.path.join(venv_dir, 'bin', 'pip')
    subprocess.check_call([pip, 'install', '-q', '--upgrade', 'pip'])
    subprocess.check_call([pip, 'install', '-q', '-r',
                           os.path.join(REPO, 'requirements.txt')])
    print(f'{venv_dir}/bin/python is ready - run every other command with it')
    return 0


def _port_open(port, host='127.0.0.1'):
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def cmd_up(argv):
    global PORT
    if '--port' in argv:
        PORT = int(argv[argv.index('--port') + 1])
    p = paths()
    os.makedirs(RUN_DIR, exist_ok=True)

    if os.path.exists(p['pid']) and _port_open(PORT):
        print(f'already running on {base_url()}')
        print(f'token: {open(p["token"]).read().strip()}')
        return 0
    if _port_open(PORT):
        # Deleting the run dir orphans a running panel: the pid file goes with
        # it and `down` can no longer find the process.
        sys.exit(f'port {PORT} is taken by something else. Find it with:\n'
                 f"  ss -ltnp | awk '/:{PORT} /{{print $NF}}' "
                 f"| grep -oP 'pid=\\K[0-9]+'\n"
                 f'or start elsewhere with --port')

    env = dict(os.environ, DATA_FILE=p['data'], TUNNEL_STATE_FILE=p['tunnels'])
    log = open(p['log'], 'w')
    proc = subprocess.Popen([sys.executable, 'app.py'], cwd=REPO, env=env,
                            stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    open(p['pid'], 'w').write(str(proc.pid))

    for _ in range(120):
        if _port_open(PORT):
            break
        if proc.poll() is not None:
            print(open(p['log']).read()[-2000:], file=sys.stderr)
            sys.exit('panel died on startup, log above')
        time.sleep(0.25)
    else:
        sys.exit('panel did not open its port within 30s')

    token = _ensure_token()
    print(f'panel: {base_url()}  (admin/admin)')
    print(f'token: {token}')
    print(f'run dir: {RUN_DIR}')
    return 0


def _panel_pid():
    """The pid from the pid file, but only while it still looks like our panel.
    Pids get reused, and this file outlives crashes - SIGTERM to a whole process
    group is not something to aim at a guess."""
    pid_file = paths()['pid']
    if not os.path.exists(pid_file):
        return None
    try:
        pid = int(open(pid_file).read().strip())
        cmdline = open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\0', b' ')
    except (ValueError, OSError):
        return None
    return pid if b'app.py' in cmdline else None


def cmd_down(argv):
    p = paths()
    pid = _panel_pid()
    if pid is None:
        if os.path.exists(p['pid']):
            os.remove(p['pid'])
            print('stale pid file removed - that process is not our panel')
        else:
            print('not running')
        return 0
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(40):
        if not _port_open(PORT):
            break
        time.sleep(0.25)
    os.remove(p['pid'])
    print('stopped')
    return 0


def cmd_logs(argv):
    n = int(argv[0]) if argv else 40
    print(''.join(open(paths()['log']).readlines()[-n:]), end='')
    return 0


# ---------------------------------------------------------------------- http

def _request(method, url, body=None, headers=None, opener=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    fn = opener.open if opener else urllib.request.urlopen
    try:
        with fn(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _ensure_token():
    """Log in with the session cookie once, mint a bearer token, cache it."""
    p = paths()
    if os.path.exists(p['token']):
        tok = open(p['token']).read().strip()
        status, _ = _request('GET', f'{base_url()}/api/settings',
                             headers={'Authorization': f'Bearer {tok}'})
        if status == 200:
            return tok
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, text = _request('POST', f'{base_url()}/api/auth/login',
                            {'username': ADMIN[0], 'password': ADMIN[1]}, opener=opener)
    if status != 200:
        sys.exit(f'login failed ({status}): {text}')
    status, text = _request('POST', f'{base_url()}/api/settings/tokens',
                            {'name': 'driver'}, opener=opener)
    tok = json.loads(text).get('token')
    if not tok:
        sys.exit(f'could not mint a token ({status}): {text}')
    open(p['token'], 'w').write(tok)
    return tok


def cmd_api(argv):
    if len(argv) < 2:
        sys.exit('usage: driver.py api METHOD PATH [JSON_BODY]')
    method, path = argv[0].upper(), argv[1]
    if not path.startswith('/'):
        sys.exit(f'path must start with a slash: {path!r}')
    try:
        body = json.loads(argv[2]) if len(argv) > 2 else None
    except ValueError as exc:
        sys.exit(f'body is not valid JSON: {exc}')
    tok = _ensure_token()
    status, text = _request(method, base_url() + path, body,
                            headers={'Authorization': f'Bearer {tok}'})
    print(f'HTTP {status}')
    try:
        print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
    except ValueError:
        print(text[:4000])
    return 0 if status < 400 else 1


def cmd_seed(argv):
    """A panel with no servers renders an empty dashboard. The API refuses to
    add a server it cannot SSH into, so write the record straight into the
    run-dir data.json - load_data() re-reads the file on every request."""
    import uuid
    p = paths()['data']
    if not os.path.exists(p):
        sys.exit(f'{p} does not exist - run `driver.py up` first')
    try:
        d = json.load(open(p))
    except ValueError as exc:
        sys.exit(f'{p} is not valid JSON ({exc}) - delete it and run `up` again')
    if any(s.get('name') == 'demo-entry' for s in d.get('servers', [])):
        print('already seeded')
        return 0
    # 127.0.0.1 on purpose: nothing listens on :22 here, so check_server gets an
    # instant ECONNREFUSED and the page settles as "offline". A routable-but-dead
    # IP (198.51.100.10) makes every card spin for the whole SSH timeout instead.
    d.setdefault('servers', []).append({
        'name': 'demo-entry', 'host': '127.0.0.1', 'ssh_port': 22,
        'username': 'root', 'password': 'not-a-real-host', 'uid': uuid.uuid4().hex,
        'protocols': {'awg2': {'installed': True, 'port': '55425',
                               'subnet': '10.8.1.0/24', 'base_protocol': 'awg2',
                               'instance': 2, 'container_name': 'amnezia-awg2'}},
    })
    json.dump(d, open(p, 'w'), indent=2)
    print(f'seeded demo-entry ({len(d["servers"])} server(s))')
    return 0


# ----------------------------------------------------------------------- cdp

class CDP:
    """Minimal Chrome DevTools Protocol client over one flat websocket session."""

    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.session = None

    @staticmethod
    async def launch(profile_dir):
        import websockets
        port = 9222
        while _port_open(port):
            port += 1
        shutil.rmtree(profile_dir, ignore_errors=True)
        proc = subprocess.Popen(
            [CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
             '--hide-scrollbars', '--window-size=1280,900',
             f'--remote-debugging-port={port}', f'--user-data-dir={profile_dir}',
             'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version',
                                            timeout=1) as r:
                    ws_url = json.load(r)['webSocketDebuggerUrl']
                    break
            except Exception:
                time.sleep(0.25)
        else:
            proc.kill()
            raise RuntimeError('chrome did not expose its debugging port')
        ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024)
        return proc, CDP(ws)

    async def send(self, method, params=None, timeout=30):
        self.n += 1
        msg = {'id': self.n, 'method': method, 'params': params or {}}
        if self.session:
            msg['sessionId'] = self.session
        await self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=deadline - time.time())
            data = json.loads(raw)
            if data.get('id') == self.n:
                if 'error' in data:
                    raise RuntimeError(f'{method}: {data["error"]}')
                return data.get('result', {})
        raise TimeoutError(method)

    async def open_tab(self, url):
        target = await self.send('Target.createTarget', {'url': url})
        attached = await self.send('Target.attachToTarget',
                                   {'targetId': target['targetId'], 'flatten': True})
        self.session = attached['sessionId']
        await self.send('Page.enable')
        await self.send('Runtime.enable')

    async def js(self, expr, timeout=30):
        res = await self.send('Runtime.evaluate',
                              {'expression': expr, 'awaitPromise': True,
                               'returnByValue': True}, timeout=timeout)
        if res.get('exceptionDetails'):
            raise RuntimeError(res['exceptionDetails'].get('text', 'JS error'))
        return res.get('result', {}).get('value')

    async def wait_js(self, expr, seconds=20):
        """Poll an expression. Swallows errors on purpose: while a navigation
        is in flight the execution context is torn down and every evaluate
        raises until the new document exists."""
        for _ in range(seconds * 4):
            try:
                if await self.js(expr, timeout=5):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def login(self):
        """Real UI flow: fill the form, submit it, wait for the redirect."""
        await self.js("document.querySelector('#username').value = %r" % ADMIN[0])
        await self.js("document.querySelector('#password').value = %r" % ADMIN[1])
        await self.js("document.querySelector('#loginBtn').click()")
        if not await self.wait_js("location.pathname !== '/login'"):
            raise RuntimeError('login did not leave /login - check panel.log')

    async def screenshot(self, out, full=False):
        if full:
            m = await self.send('Page.getLayoutMetrics')
            size = m.get('cssContentSize') or m['contentSize']
            await self.send('Emulation.setDeviceMetricsOverride', {
                'width': 1280, 'height': min(int(size['height']) + 40, 8000),
                'deviceScaleFactor': 1, 'mobile': False})
            await asyncio.sleep(0.5)
        shot = await self.send('Page.captureScreenshot', {'format': 'png'})
        if full:
            await self.send('Emulation.clearDeviceMetricsOverride')
        with open(out, 'wb') as f:
            f.write(base64.b64decode(shot['data']))
        return out


async def _with_ui(path, action, settle=1.5, js=None):
    p = paths()
    proc, cdp = await CDP.launch(p['chrome_profile'])
    try:
        await cdp.open_tab(f'{base_url()}/login')
        await cdp.wait_js("!!document.querySelector('#loginBtn')")
        await cdp.login()
        if path not in ('/', ''):
            await cdp.send('Page.navigate', {'url': base_url() + path})
            if not await cdp.wait_js(
                    f"location.pathname === {json.dumps(path.split('?')[0])}"):
                raise RuntimeError(f'never landed on {path}')
        await cdp.wait_js("document.readyState === 'complete'")
        await asyncio.sleep(settle)  # let the page's own fetches paint
        if js:
            await cdp.js(js)
            await asyncio.sleep(settle)
        return await action(cdp)
    finally:
        await cdp.ws.close()
        proc.terminate()


def cmd_shot(argv):
    full = '--full' in argv
    settle, js = 1.5, None
    if '--js' in argv:
        i = argv.index('--js')
        js = argv[i + 1]
        del argv[i:i + 2]
    if '--wait' in argv:
        i = argv.index('--wait')
        settle = float(argv[i + 1])
        del argv[i:i + 2]
    argv = [a for a in argv if a != '--full']
    out = os.path.abspath(argv[0]) if argv else os.path.join(RUN_DIR, 'shot.png')
    path = argv[1] if len(argv) > 1 else '/'
    asyncio.run(_with_ui(path, lambda c: c.screenshot(out, full),
                         settle=settle, js=js))
    print(f'{out} ({os.path.getsize(out)} bytes) - page {path}'
          f'{" (full page)" if full else ""}')
    return 0


def cmd_eval(argv):
    if not argv:
        sys.exit('usage: driver.py eval "JS expression" [/path]')
    expr, path = argv[0], (argv[1] if len(argv) > 1 else '/')
    value = asyncio.run(_with_ui(path, lambda c: c.js(expr)))
    print(json.dumps(value, indent=2, ensure_ascii=False) if not isinstance(value, str) else value)
    return 0


# --------------------------------------------------------------------- smoke

def cmd_smoke(argv):
    cmd_up([])
    try:
        cmd_seed([])
        if cmd_api(['GET', '/api/exit-nodes']) != 0:
            sys.exit('exit-nodes call failed')
        out = os.path.join(RUN_DIR, 'dashboard.png')
        cmd_shot([out])
        title = asyncio.run(_with_ui('/', lambda c: c.js('document.title')))
        print(f'dashboard title: {title!r}')
        if os.path.getsize(out) < 10_000:
            sys.exit('screenshot looks empty')
        print('SMOKE OK')
        return 0
    finally:
        cmd_down([])


COMMANDS = {'venv': cmd_venv, 'up': cmd_up, 'down': cmd_down, 'api': cmd_api, 'shot': cmd_shot,
            'eval': cmd_eval, 'seed': cmd_seed, 'logs': cmd_logs, 'smoke': cmd_smoke}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
