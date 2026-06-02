#!/usr/bin/env python3
"""Control — visual console UI"""

import json
import time
import os
import signal
import importlib.util
from pathlib import Path
from flask import Flask, Response, request, jsonify, redirect, url_for
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

STATE_FILE   = Path.home() / ".streamfader" / "state.json"
PRESETS_DIR  = Path.home() / ".streamfader" / "presets"
PID_FILE     = Path.home() / ".streamfader" / "ctrl.pid"
PORT  = int(os.environ.get("PORT", 5570))
MODEL = os.environ.get("CTRL_MODEL", "claude-sonnet-4-6")


# Load build_system_prompt from ctrl script without importing the whole CLI
import importlib.machinery
_ctrl_path = Path(__file__).resolve().parent / "ctrl"
if _ctrl_path.exists():
    _loader = importlib.machinery.SourceFileLoader("_ctrl", str(_ctrl_path))
    _spec   = importlib.util.spec_from_loader("_ctrl", _loader)
    _ctrl   = importlib.util.module_from_spec(_spec)
    _loader.exec_module(_ctrl)
    _build_prompt = _ctrl.build_system_prompt
else:
    _build_prompt = lambda s: "You are a helpful AI assistant."


app = Flask(__name__)

# ── Agentic tools ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories at a path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command and return stdout + stderr.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def _execute_tool(name: str, inputs: dict) -> str:
    import subprocess as _sp
    try:
        if name == "read_file":
            p = Path(inputs["path"]).expanduser()
            if not p.exists():
                return f"Error: not found: {p}"
            if p.stat().st_size > 500_000:
                return f"Error: file too large (>{500_000} bytes)"
            text = p.read_text(errors="replace")
            limit = 6_000
            if len(text) > limit:
                return text[:limit] + f"\n\n[... truncated — {len(text) - limit} chars omitted. Ask for a specific section if you need more.]"
            return text

        elif name == "write_file":
            p = Path(inputs["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(inputs["content"])
            return f"Wrote {len(inputs['content'])} chars to {p}"

        elif name == "list_directory":
            p = Path(inputs["path"]).expanduser()
            if not p.exists():
                return f"Error: not found: {p}"
            lines = []
            for item in sorted(p.iterdir()):
                lines.append(f"{'[dir] ' if item.is_dir() else '[file]'} {item.name}")
            return "\n".join(lines) if lines else "(empty)"

        elif name == "run_command":
            r = _sp.run(inputs["command"], shell=True, capture_output=True, text=True, timeout=60)
            out = (r.stdout + r.stderr).strip()
            return out[:20_000] if out else "(no output)"

        return f"Unknown tool: {name}"
    except Exception as e:
        return f"Error: {e}"


# ── State ─────────────────────────────────────────────────────────────────────

DEFAULT_STATE = {
    "mode": "BUILD", "intensity": 0.5, "depth": 0.5,
    "certainty": 0.5, "risk": 0.5, "stance": "GUIDE",
    "scope": 0.5, "bandwidth": 0.5, "filter": "MODULE",
    "room": 0.5, "decay": 0.5, "voice": "STUDIO",
    "t1_on": True, "t2_on": True, "t3_on": True, "t4_on": True,
}

def _sb_headers(token=None):
    h = {"apikey": SUPABASE_SERVICE, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    else:
        h["Authorization"] = f"Bearer {SUPABASE_SERVICE}"
    return h

def _get_user_id(token: str):
    """Validate token and return user_id."""
    if not token or not SUPABASE_URL:
        return None
    try:
        import requests as _req
        r = _req.get(f"{SUPABASE_URL}/auth/v1/user",
                     headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON},
                     timeout=4)
        if r.status_code == 200:
            return r.json().get("id")
    except Exception:
        pass
    return None

def _token_from_request() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""

def read_state(user_id: str = None) -> dict:
    """Read state from Supabase if user_id given, else fall back to local file."""
    if user_id and SUPABASE_URL:
        try:
            import requests as _req
            r = _req.get(
                f"{SUPABASE_URL}/rest/v1/user_state?user_id=eq.{user_id}&select=state",
                headers=_sb_headers(), timeout=4)
            if r.status_code == 200:
                rows = r.json()
                if rows:
                    return {**DEFAULT_STATE, **rows[0]["state"]}
        except Exception:
            pass
    # Local fallback
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return DEFAULT_STATE.copy()

def write_state(state: dict, user_id: str = None) -> None:
    """Write state to Supabase if user_id given, else write local file."""
    if user_id and SUPABASE_URL:
        try:
            import requests as _req
            _req.post(
                f"{SUPABASE_URL}/rest/v1/user_state",
                headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
                json={"user_id": user_id, "state": state, "updated_at": "now()"},
                timeout=4)
        except Exception:
            pass
    # Always write local file as backup
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(STATE_FILE)
    except Exception:
        pass


# ── Routes ────────────────────────────────────────────────────────────────────

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON    = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_KEY", "")

def require_auth(f):
    """Validate Supabase JWT from Authorization header or cookie."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Skip auth in local dev if no Supabase keys configured
        if not SUPABASE_URL:
            return f(*args, **kwargs)
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token:
            token = request.cookies.get("sb-access-token")
        if not token:
            # Browser request — redirect to login
            if request.headers.get("Accept", "").startswith("text/html"):
                return redirect("/login")
            return jsonify({"error": "unauthorized"}), 401
        try:
            from supabase import create_client
            sb = create_client(SUPABASE_URL, SUPABASE_SERVICE)
            user = sb.auth.get_user(token)
            if not user or not user.user:
                raise Exception("invalid token")
        except Exception:
            if request.headers.get("Accept", "").startswith("text/html"):
                return redirect("/login")
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route("/login")
def login():
    return LOGIN_HTML

@app.route("/auth/callback")
def auth_callback():
    # Parse token from hash fragment client-side, store in localStorage, redirect to /app
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{background:#000;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;font-size:13px;letter-spacing:.1em}}</style>
</head><body>SIGNING IN…
<script>
(function(){{
  var hash = window.location.hash.substring(1);
  var params = {{}};
  hash.split('&').forEach(function(p){{ var kv=p.split('='); params[kv[0]]=decodeURIComponent(kv[1]||''); }});
  if (params.access_token) {{
    localStorage.setItem('sb-access-token', params.access_token);
    localStorage.setItem('sb-refresh-token', params.refresh_token || '');
    window.location.href = '/app';
  }} else {{
    window.location.href = '/login';
  }}
}})();
</script></body></html>"""

@app.route("/app")
def app_view():
    # Auth is validated client-side via JS + token in localStorage
    # API routes (/set, /run, /stream etc.) enforce server-side auth
    return HTML

@app.route("/")
def index():
    if SUPABASE_URL:
        return redirect("/login")
    return HTML

@app.route("/stream")
def stream():
    token   = request.args.get("token", "")
    user_id = _get_user_id(token) if token else None
    def generate():
        last = {}
        while True:
            state = read_state(user_id)
            if state != last:
                last = state.copy()
                yield f"data: {json.dumps(state)}\n\n"
            time.sleep(0.3)
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/set", methods=["POST"])
def set_state():
    token   = _token_from_request()
    user_id = _get_user_id(token) if token else None
    data    = request.get_json()
    state   = read_state(user_id)
    state.update(data)
    write_state(state, user_id)
    return jsonify({"ok": True})

@app.route("/run", methods=["POST"])
def run_task():
    data    = request.get_json() or {}
    task    = data.get("task", "").strip()
    if not task:
        return jsonify({"error": "No task provided"}), 400
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    token   = _token_from_request()
    user_id = _get_user_id(token) if token else None

    def generate():
        if not _anthropic:
            yield f"data: {json.dumps({'error': 'anthropic not installed'})}\n\n"
            return
        if not api_key:
            yield f"data: {json.dumps({'error': 'ANTHROPIC_API_KEY not set'})}\n\n"
            return
        state  = read_state(user_id)
        system = _build_prompt(state)
        try:
            client = _anthropic.Anthropic(api_key=api_key)
            with client.messages.stream(
                model=MODEL,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": task}],
            ) as s:
                for text in s.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/exec", methods=["POST"])
def exec_task():
    """Agentic loop — Claude with file tools, runs entirely in Flask, no CLI needed."""
    data = request.get_json() or {}
    task = data.get("task", "").strip()
    if not task:
        return jsonify({"error": "No task provided"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not _anthropic:
        return jsonify({"error": "anthropic package not installed"}), 500
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    state  = read_state()
    system = _build_prompt(state)
    cwd    = str(Path.home())

    def generate():
        client   = _anthropic.Anthropic(api_key=api_key)
        messages = [{"role": "user", "content": f"[Working directory: {cwd}]\n\n{task}"}]

        try:
            Path.home().joinpath(".streamfader", "last_task.txt").write_text(task)
        except Exception:
            pass

        for _turn in range(20):  # hard cap on agentic turns
            collected = []
            try:
                with client.messages.stream(
                    model=MODEL,
                    max_tokens=8096,
                    system=system,
                    tools=TOOLS,
                    messages=messages,
                ) as stream:
                    for event in stream:
                        t = getattr(event, "type", "")
                        if t == "content_block_delta":
                            txt = getattr(getattr(event, "delta", None), "text", None)
                            if txt:
                                yield f"data: {json.dumps({'text': txt})}\n\n"
                    final = stream.get_final_message()
                    collected = final.content
                    stop     = final.stop_reason
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                return

            if stop != "tool_use":
                yield f"data: {json.dumps({'done': True})}\n\n"
                return

            tool_results = []
            for blk in collected:
                if getattr(blk, "type", "") == "tool_use":
                    yield f"data: {json.dumps({'tool': blk.name, 'input': blk.input})}\n\n"
                    result = _execute_tool(blk.name, blk.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": blk.id,
                        "content": result,
                    })

            messages.append({"role": "assistant", "content": collected})
            messages.append({"role": "user",      "content": tool_results})

        yield f"data: {json.dumps({'done': True})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/presets", methods=["GET"])
def list_presets():
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(PRESETS_DIR.glob("*.json")):
        try:
            out.append({"name": f.stem, "state": json.loads(f.read_text())})
        except Exception:
            pass
    return jsonify(out)

@app.route("/presets/save", methods=["POST"])
def save_preset():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    safe = "".join(c for c in name if c.isalnum() or c in " -_").strip()[:40]
    if not safe:
        return jsonify({"error": "Invalid name"}), 400
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    (PRESETS_DIR / f"{safe}.json").write_text(json.dumps(read_state(), indent=2) + "\n")
    return jsonify({"ok": True, "name": safe})

@app.route("/abort", methods=["POST"])
def abort_run():
    try:
        pid = int(PID_FILE.read_text().strip())
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        return jsonify({"ok": True, "killed": pid})
    except (FileNotFoundError, ValueError):
        return jsonify({"ok": False, "reason": "no process running"})
    except (ProcessLookupError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return jsonify({"ok": False, "reason": "process already gone"})

@app.route("/presets/load", methods=["POST"])
def load_preset():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    f = PRESETS_DIR / f"{name}.json"
    if not f.exists():
        return jsonify({"error": "Not found"}), 404
    state = json.loads(f.read_text())
    write_state(state)
    return jsonify({"ok": True, "state": state})

@app.route("/presets/delete", methods=["POST"])
def delete_preset():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    f = PRESETS_DIR / f"{name}.json"
    if f.exists():
        f.unlink()
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({"ok": True, "model": MODEL,
                    "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY"))})


# ── LOGIN HTML ────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Gain — Sign In</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Abril+Fatface&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #000; font-family: 'Inter', sans-serif; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
    .bg-grid { position: fixed; inset: 0; z-index: 0; background-image: linear-gradient(rgba(0,220,212,.04) 1px, transparent 1px), linear-gradient(90deg, rgba(0,220,212,.04) 1px, transparent 1px); background-size: 40px 40px; }
    .card { position: relative; z-index: 10; width: 100%; max-width: 380px; background: rgba(6,10,18,.92); border: 1px solid rgba(0,220,212,.15); border-radius: 6px; padding: 40px 36px; box-shadow: 0 0 80px rgba(0,180,200,.06); }
    .brand { font-family: 'Abril Fatface', serif; font-size: 32px; letter-spacing: .06em; background: linear-gradient(130deg,#00E8FF 0%,#A0C8FF 50%,#C0A0FF 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; filter: drop-shadow(0 0 8px rgba(0,200,255,.4)); margin-bottom: 6px; }
    .tagline { font-size: 11px; font-weight: 500; letter-spacing: .12em; text-transform: uppercase; color: rgba(0,220,212,.5); margin-bottom: 36px; }
    .section-label { font-size: 9px; font-weight: 700; letter-spacing: .18em; text-transform: uppercase; color: rgba(180,210,230,.3); margin-bottom: 10px; }
    .btn-google { width: 100%; height: 44px; background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.12); border-radius: 4px; color: #fff; font-size: 13px; font-weight: 600; font-family: 'Inter', sans-serif; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 10px; transition: all .15s; margin-bottom: 24px; }
    .btn-google:hover { background: rgba(255,255,255,.09); border-color: rgba(255,255,255,.22); }
    .divider { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
    .divider-line { flex: 1; height: 1px; background: rgba(255,255,255,.07); }
    .divider-text { font-size: 10px; color: rgba(255,255,255,.2); letter-spacing: .1em; text-transform: uppercase; }
    .input { width: 100%; height: 42px; background: rgba(6,10,15,.8); border: 1px solid rgba(0,220,212,.18); border-radius: 3px; color: #D8EAF8; font-size: 16px; font-family: 'Inter', sans-serif; padding: 0 13px; outline: none; transition: border-color .2s; margin-bottom: 10px; }
    .input::placeholder { color: rgba(130,170,190,.35); }
    .input:focus { border-color: rgba(0,220,212,.5); }
    .btn-primary { width: 100%; height: 42px; background: rgba(0,180,200,.2); border: 1px solid rgba(0,220,212,.4); border-radius: 3px; color: #00DDD4; font-size: 10px; font-weight: 800; font-family: 'Inter', sans-serif; letter-spacing: .14em; text-transform: uppercase; cursor: pointer; transition: all .15s; margin-top: 4px; }
    .btn-primary:hover { background: rgba(0,200,212,.28); border-color: rgba(0,220,212,.7); }
    .btn-primary:disabled { opacity: .4; cursor: not-allowed; }
    .tab-row { display: flex; margin-bottom: 14px; border-bottom: 1px solid rgba(255,255,255,.06); }
    .tab { flex: 1; padding: 8px 0; font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: rgba(255,255,255,.25); background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer; font-family: 'Inter', sans-serif; transition: all .15s; margin-bottom: -1px; }
    .tab.active { color: #00DDD4; border-bottom-color: #00DDD4; }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .status { font-size: 11px; font-weight: 600; letter-spacing: .06em; min-height: 18px; margin-top: 10px; text-align: center; }
    .status.ok { color: #00DDD4; } .status.err { color: #FF5050; }
    .footer { margin-top: 28px; text-align: center; font-size: 10px; color: rgba(255,255,255,.15); letter-spacing: .06em; }
    .footer a { color: rgba(0,220,212,.4); text-decoration: none; }
  </style>
</head>
<body>
  <div class="bg-grid"></div>
  <div class="card">
    <div class="brand">gain</div>
    <div class="tagline">The AI Behavioral Mixing Board</div>
    <div class="section-label">Continue with</div>
    <button class="btn-google" onclick="signInGoogle()">
      <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z"/><path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 6.29C4.672 4.163 6.656 3.58 9 3.58z"/></svg>
      Continue with Google
    </button>
    <div class="divider"><div class="divider-line"></div><div class="divider-text">or</div><div class="divider-line"></div></div>
    <div class="tab-row">
      <button class="tab active" onclick="switchTab('password')">Password</button>
      <button class="tab" onclick="switchTab('magic')">Magic Link</button>
    </div>
    <div class="tab-panel active" id="panel-password">
      <input class="input" id="pw-email" type="email" placeholder="your@email.com" autocomplete="email"/>
      <input class="input" id="pw-password" type="password" placeholder="password" autocomplete="current-password"/>
      <button class="btn-primary" id="pw-btn" onclick="signInPassword()">Sign In</button>
      <div style="text-align:center;margin-top:10px">
        <a href="#" onclick="switchMode()" id="mode-link" style="font-size:11px;color:rgba(0,220,212,.45);text-decoration:none">No account? Create one</a>
      </div>
    </div>
    <div class="tab-panel" id="panel-magic">
      <input class="input" id="magic-email" type="email" placeholder="your@email.com" autocomplete="email"/>
      <button class="btn-primary" id="magic-btn" onclick="sendMagicLink()">Send Magic Link</button>
    </div>
    <div class="status" id="status"></div>
    <div class="footer"><a href="https://gain.creativekonsoles.com">← Back to gain.creativekonsoles.com</a></div>
  </div>
  <script>
    const SB_URL = '__SUPABASE_URL__';
    const SB_KEY = '__SUPABASE_KEY__';
    const CALLBACK = window.location.origin + '/auth/callback';
    let isSignUp = false;
    function switchMode() { isSignUp=!isSignUp; document.getElementById('pw-btn').textContent=isSignUp?'CREATE ACCOUNT':'SIGN IN'; document.getElementById('mode-link').textContent=isSignUp?'Have an account? Sign in':'No account? Create one'; setStatus(''); }
    function switchTab(tab) { document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',(i===0&&tab==='password')||(i===1&&tab==='magic'))); document.getElementById('panel-password').classList.toggle('active',tab==='password'); document.getElementById('panel-magic').classList.toggle('active',tab==='magic'); setStatus(''); }
    function setStatus(msg,type='') { const el=document.getElementById('status'); el.textContent=msg; el.className='status'+(type?' '+type:''); }
    function signInGoogle() { window.location.href=SB_URL+'/auth/v1/authorize?provider=google&redirect_to='+encodeURIComponent(CALLBACK); }
    async function signInPassword() {
      const email=document.getElementById('pw-email').value.trim(), password=document.getElementById('pw-password').value;
      if(!email||!password){setStatus('Enter email and password.','err');return;}
      const btn=document.getElementById('pw-btn'); btn.disabled=true;
      setStatus(isSignUp?'Creating account…':'Signing in…');
      const endpoint=isSignUp?SB_URL+'/auth/v1/signup':SB_URL+'/auth/v1/token?grant_type=password';
      try {
        const res=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json','apikey':SB_KEY},body:JSON.stringify({email,password})});
        const data=await res.json();
        if(data.error||data.error_description){setStatus(data.error_description||data.error,'err');}
        else if(data.access_token){localStorage.setItem('sb-access-token',data.access_token);localStorage.setItem('sb-refresh-token',data.refresh_token||'');window.location.href='/app';}
        else{setStatus('Check your email to confirm your account.','ok');}
      } catch(e){setStatus('Network error. Try again.','err');}
      btn.disabled=false;
    }
    async function sendMagicLink() {
      const email=document.getElementById('magic-email').value.trim();
      if(!email){setStatus('Enter your email.','err');return;}
      const btn=document.getElementById('magic-btn'); btn.disabled=true; setStatus('Sending…');
      try {
        const res=await fetch(SB_URL+'/auth/v1/otp',{method:'POST',headers:{'Content-Type':'application/json','apikey':SB_KEY},body:JSON.stringify({email,options:{emailRedirectTo:CALLBACK}})});
        const data=await res.json();
        if(data.error){setStatus(data.error,'err');}else{setStatus('Magic link sent. Check your inbox.','ok');}
      } catch(e){setStatus('Network error. Try again.','err');}
      btn.disabled=false;
    }
  </script>
</body>
</html>""".replace('__SUPABASE_URL__', SUPABASE_URL).replace('__SUPABASE_KEY__', SUPABASE_ANON)

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1">
<title>Gain</title>
<link href="https://fonts.googleapis.com/css2?family=Abril+Fatface&family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:        #060A0F;
  --panel:     #0B1018;
  --panel2:    #101820;
  --border:    #162030;
  --border2:   #1E2E40;
  --accent:    #00DDD4;
  --accent2:   #10F2E8;
  --purple:    #8B5CF6;
  --purple2:   #A78BFA;
  --green:     #00DDD4;
  --text:      #D8EAF8;
  --text2:     #6A8AA8;
  --text3:     #405870;
  --chrome:    #6A90A8;
  --chrome2:   #A8D0E0;
  --fader-bg:  #040608;
  --fader-trk: #020406;
  --thumb-hi:  #A8C4D8;
  --thumb-lo:  #1C2E40;
  --magenta:   #D946EF;
  --magenta2:  #F0ABFF;
}
*{margin:0;padding:0;box-sizing:border-box;}
body{
  background:var(--bg);
  background-image:radial-gradient(rgba(0,196,232,.04) 1px,transparent 1px);
  background-size:28px 28px;
  font-family:'Inter',sans-serif;color:var(--text);
  height:100vh;display:flex;flex-direction:column;user-select:none;overflow:hidden;
}

/* ── HEADER ─────────────────────────────────────────────── */
.hdr{
  padding:0 16px;
  border-bottom:1px solid var(--border);
  background:#030507;
  background-image:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.06) 2px,rgba(0,0,0,.06) 3px);
  display:flex;align-items:center;
  flex-shrink:0;height:82px;
  box-shadow:0 1px 0 rgba(0,200,192,.1);
  position:relative;
}
.brand{
  font-family:'Abril Fatface',serif;font-size:41px;letter-spacing:.06em;line-height:82px;
  background:linear-gradient(130deg,#00E8FF 0%,#A0C8FF 50%,#C0A0FF 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  filter:drop-shadow(0 0 6px rgba(0,200,255,.55)) drop-shadow(0 0 14px rgba(160,100,255,.3));
  flex-shrink:0;
}
.hdr-center{
  position:absolute;left:50%;transform:translateX(-50%);
  display:flex;flex-direction:column;align-items:center;gap:3px;
  pointer-events:none;
}
.hdr-settings-label{
  font-size:8.5px;font-weight:800;letter-spacing:.22em;text-transform:uppercase;
  color:var(--accent);opacity:.7;
}
.hdr-settings{
  font-size:9px;font-weight:700;color:#C8DCEA;
  letter-spacing:.04em;font-variant-numeric:tabular-nums;
  white-space:nowrap;text-align:center;
  text-shadow:0 0 12px rgba(0,200,192,.25);
}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:10px;}
.reset-btn{
  height:27px;padding:0 11px;border-radius:3px;
  border:1px solid rgba(0,200,192,.3);background:rgba(0,200,192,.06);
  color:var(--accent);font-size:8px;font-weight:800;letter-spacing:.12em;
  text-transform:uppercase;cursor:pointer;transition:all .15s;white-space:nowrap;
  box-shadow:0 0 8px rgba(0,200,192,.1);
  flex-shrink:0;
}
.reset-btn:hover{background:rgba(0,200,192,.16);border-color:var(--accent);box-shadow:0 0 16px rgba(0,200,192,.28);}
.faq-btn{
  width:44px;height:44px;border-radius:50%;
  border:1px solid var(--accent);background:rgba(0,200,192,.07);
  color:var(--accent);font-size:20px;font-weight:800;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:all .15s;line-height:1;
  box-shadow:0 0 10px rgba(0,200,192,.28),0 0 20px rgba(0,200,192,.12);
  text-shadow:0 0 8px rgba(0,232,224,.7);
  flex-shrink:0;
}
.faq-btn:hover{background:rgba(0,200,192,.16);color:var(--accent2);border-color:var(--accent2);box-shadow:0 0 16px rgba(0,200,192,.48),0 0 32px rgba(0,200,192,.22);}
.theme-btn{
  width:28px;height:28px;border-radius:50%;
  border:1px solid var(--border2);background:transparent;
  color:var(--text3);font-size:13px;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:all .2s;line-height:1;flex-shrink:0;
}
.theme-btn:hover{border-color:var(--chrome);color:var(--text2);}

/* ══ LIGHT THEME ════════════════════════════════════════════ */
body.light{
  --bg:        #EDEBE7;
  --panel:     #F5F3F0;
  --panel2:    #E4E1DC;
  --border:    rgba(0,0,0,.10);
  --border2:   rgba(0,0,0,.16);
  --accent:    #007E78;
  --accent2:   #009E96;
  --purple:    #6D28D9;
  --purple2:   #7C3AED;
  --text:      #1C2B3A;
  --text2:     #3D5570;
  --text3:     #7A96B0;
  --chrome:    #5A7898;
  --chrome2:   #3A5878;
  --fader-bg:  #D8D4CE;
  --fader-trk: #C8C4BE;
  --thumb-hi:  #F8F6F2;
  --thumb-lo:  #888078;
  --magenta:   #B020C8;
  --magenta2:  #C026D3;
  background-image:radial-gradient(rgba(0,80,80,.05) 1px,transparent 1px);
  background-size:28px 28px;
}
body.light .hdr{
  background:#E8E5E0;
  background-image:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.03) 2px,rgba(0,0,0,.03) 3px);
  box-shadow:0 1px 0 rgba(0,130,120,.12);
}
body.light .brand{
  background:linear-gradient(130deg,#006E80 0%,#004E70 50%,#5030A0 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  filter:none;
}
body.light .hdr-settings{color:#2A3E52;}
body.light .reset-btn{
  border-color:rgba(0,126,120,.35);background:rgba(0,126,120,.06);
  box-shadow:none;
}
body.light .reset-btn:hover{background:rgba(0,126,120,.14);border-color:var(--accent);}
body.light .faq-btn{
  border-color:var(--accent);background:rgba(0,126,120,.06);
  box-shadow:none;text-shadow:none;
}
body.light .channel-bank{
  background:linear-gradient(180deg,#EAE7E2 0%,#E2DED8 100%);
  box-shadow:inset -1px 0 0 rgba(0,0,0,.06);
}
body.light .bank-hd{background:#E2DED8;}
body.light .section-hd{color:var(--chrome);}
body.light .ch-hdr-row{background:#DDD9D3;border-color:var(--border);}
body.light .ch-id{color:var(--text2);}
body.light .fader-lbl{color:var(--chrome);}
body.light .fader-val{color:var(--accent);text-shadow:none;}
body.light .fader-track{
  background:linear-gradient(180deg,#C8C4BC 0%,#D4D0C8 50%,#C8C4BC 100%);
  border-color:#B8B4AC;
  border-left-color:#C0BDB4;
  border-right-color:#C0BDB4;
  box-shadow:
    inset 0 2px 6px rgba(0,0,0,.18),
    inset 1px 0 3px rgba(0,0,0,.10),
    inset -1px 0 3px rgba(0,0,0,.10),
    inset 0 1px 0 rgba(255,255,255,.5),
    0 0 0 1px rgba(0,100,120,.04);
}
body.light .fader-fill{
  background:linear-gradient(0deg,
    rgba(0,100,100,.06) 0%,
    rgba(0,130,125,.18) 35%,
    rgba(0,155,148,.34) 65%,
    rgba(0,175,168,.52) 85%,
    rgba(0,185,178,.68) 100%
  );
  box-shadow:0 0 6px rgba(0,130,120,.22),inset 0 0 4px rgba(0,150,145,.10);
}
body.light .ch.t2 .fader-fill,body.light .ch.t4 .fader-fill{
  background:linear-gradient(0deg,
    rgba(90,30,160,.05) 0%,
    rgba(100,40,180,.14) 35%,
    rgba(115,60,200,.28) 65%,
    rgba(130,80,220,.44) 85%,
    rgba(140,95,235,.58) 100%
  );
  box-shadow:0 0 6px rgba(109,40,217,.20),inset 0 0 4px rgba(120,60,210,.08);
}
body.light .fader-thumb{
  background:linear-gradient(180deg,
    #FAFAF8 0%,
    #EAE8E4 4%,
    #C8C4BC 16%,
    #A8A49C 34%,
    #8C887E 47%,
    #888078 50%,
    #8C887E 53%,
    #A8A49C 66%,
    #C8C4BC 84%,
    #EAE8E4 96%,
    #FAFAF8 100%
  );
  border-color:#A0A098;
  border-top-color:#FEFEFE;
  border-bottom-color:#888078;
  box-shadow:
    0 2px 8px rgba(0,0,0,.28),
    0 0 0 1px rgba(0,0,0,.10),
    inset 0 2px 0 rgba(255,255,255,.7),
    inset 0 -1px 0 rgba(0,0,0,.15);
}
body.light .fader-thumb::before,body.light .fader-thumb::after{
  background:linear-gradient(90deg,transparent,rgba(0,0,0,.18) 25%,rgba(0,0,0,.18) 75%,transparent);
}
body.light .fader-thumb .thumb-center{
  background:linear-gradient(90deg,transparent,rgba(0,140,135,.85) 12%,rgba(0,160,155,1) 50%,rgba(0,140,135,.85) 88%,transparent);
  box-shadow:0 0 4px rgba(0,140,130,.6),0 0 10px rgba(0,130,120,.25);
}
body.light .fader-track.dragging .fader-thumb,
body.light .fader-track.value-active .fader-thumb{
  box-shadow:
    0 2px 10px rgba(0,0,0,.35),
    0 0 16px rgba(0,160,150,.45),
    0 0 0 1px rgba(0,180,170,.28),
    inset 0 2px 0 rgba(255,255,255,.8);
  border-top-color:#FFFFFF;
}
body.light .knob{
  background:conic-gradient(#C8C4BC 0deg 225deg,#C8C4BC 225deg 360deg);
  box-shadow:
    0 3px 10px rgba(0,0,0,.22),
    0 0 0 1px rgba(0,0,0,.14),
    0 0 0 2px rgba(255,255,255,.55),
    inset 0 1px 0 rgba(255,255,255,.5);
}
body.light .knob-body{
  background:radial-gradient(circle at 36% 30%,#DEDAD4 0%,#C8C4BE 28%,#B0ACA5 58%,#A8A49C 100%);
  border-color:rgba(0,0,0,.18);
  box-shadow:
    inset 0 3px 7px rgba(255,255,255,.7),
    inset 0 -3px 9px rgba(0,0,0,.20),
    inset 0 0 18px rgba(0,0,0,.08);
}
body.light .knob-val{text-shadow:none;}
body.light .knob-lbl{color:var(--chrome);}
body.light .ch-btn{
  border-color:var(--border2);background:rgba(0,0,0,.03);color:var(--text2);
}
body.light .ch-btn:hover{background:rgba(0,126,120,.08);border-color:var(--accent);color:var(--accent);}
body.light .ch-btn.active{background:rgba(0,126,120,.14);border-color:var(--accent);color:var(--accent);}
body.light .meter-track{background:#C8C4BC;}
body.light .meter-fill{
  background:linear-gradient(90deg,#005850 0%,var(--accent) 65%,var(--accent2) 100%);
  box-shadow:none;
}
body.light .meter-val{text-shadow:none;}
body.light .meter-lvl{color:var(--text3);}
body.light .pill{border-color:var(--border2);background:rgba(0,0,0,.04);color:var(--text2);}
body.light .pill.active{background:rgba(0,126,120,.12);border-color:var(--accent);color:var(--accent);}
body.light .right-col{background:linear-gradient(180deg,#EAE7E2 0%,#E2DED8 100%);}
body.light .meters-wrap,.body.light .pills-wrap,.body.light .presets-wrap{border-color:var(--border);}
body.light .section-bg{background:#E2DED8;}
body.light .output-wrap{background:#F0EDE8;border-color:var(--border);}
body.light .output-box{background:#F8F6F2;color:var(--text);border-color:var(--border);}
body.light .launch-btn{border-color:rgba(0,126,120,.4);background:rgba(0,126,120,.06);color:var(--accent);}
body.light .launch-btn:hover{background:rgba(0,126,120,.14);}
body.light .preset-input{background:#F0EDE8;border-color:var(--border2);color:var(--text);}
body.light .preset-save-btn{border-color:rgba(0,126,120,.4);background:rgba(0,126,120,.06);color:var(--accent);}
body.light .abort-btn{border-color:rgba(200,40,40,.3);color:rgba(180,40,40,.55);}
body.light .abort-btn:hover{border-color:#CC2020;color:#CC2020;background:rgba(200,40,40,.07);}
body.light .hero-tagline{color:#4A3000;text-shadow:0 0 3px rgba(255,220,100,.8),0 0 8px rgba(255,160,20,.5),0 0 16px rgba(255,100,0,.3);}
body.light .hero-brand{color:rgba(0,100,90,.06);}
body.light .copy-btn{background:rgba(240,237,232,.9);border-color:var(--border2);}
body.light .history-item{border-color:var(--border);}
body.light .hc-time,.body.light .hc-mode,.body.light .hc-peek{color:var(--text3);}
body.light .preset-empty{color:var(--text3);}
body.light .preset-name{color:var(--text);}
body.light .api-tag{background:rgba(0,126,120,.06);border-color:rgba(0,126,120,.2);color:var(--accent);}
body.light .fader-track.pickup{border-color:rgba(200,130,0,.4);box-shadow:inset 0 2px 6px rgba(0,0,0,.15),0 0 0 1px rgba(200,130,0,.15);}

/* ── HERO ───────────────────────────────────────────────── */
.hero{position:relative;flex-shrink:0;height:108px;overflow:hidden;border-bottom:1px solid var(--border);box-shadow:0 1px 0 rgba(0,200,192,.12);}
.hero svg{position:absolute;inset:0;width:100%;height:100%;}
.hero-left{position:absolute;left:22px;top:50%;transform:translateY(-52%);pointer-events:none;z-index:1;}
.hero-brand{font-family:'Abril Fatface',serif;font-size:62px;color:rgba(0,200,192,.06);line-height:1;}
.hero-tagline{font-size:11px;font-weight:700;letter-spacing:.28em;color:#FFF8E0;text-transform:uppercase;margin-top:4px;text-shadow:0 0 3px #FFF,0 0 8px #FFE040,0 0 16px #FFB020,0 0 28px #FF5500,0 0 50px rgba(217,70,239,.4),0 0 80px rgba(139,92,246,.2);}

/* ── MAIN 3-COLUMN CONSOLE ──────────────────────────────── */
.console{flex:1;display:flex;overflow:hidden;min-height:0;}

/* ── CHANNEL STRIP (shared L/R) ─────────────────────────── */
.channel-bank{
  width:148px;flex-shrink:0;
  display:flex;flex-direction:column;
  background:linear-gradient(180deg,#0E1520 0%,#090F1C 100%);
  border-right:1px solid var(--border);
  overflow:hidden;
  box-shadow:inset -1px 0 0 rgba(0,200,192,.05);
}
.channel-bank.right{border-right:none;border-left:1px solid var(--border);}
.bank-hd{
  padding:4px 10px;
  font-size:8px;font-weight:800;letter-spacing:.22em;
  color:var(--text3);text-transform:uppercase;
  border-bottom:1px solid var(--border);
  background:#030507;flex-shrink:0;
}

/* each individual channel strip */
.ch{
  flex:1;display:flex;flex-direction:column;align-items:center;
  border-bottom:1px solid var(--border);
  padding:10px 6px 8px;gap:0;min-height:0;overflow:hidden;
  background:linear-gradient(180deg,#141D26 0%,#0D1620 50%,#0A1220 100%);
  position:relative;
  box-shadow:inset 1px 0 0 rgba(0,196,232,.03),inset -1px 0 0 rgba(0,196,232,.03);
}
.ch:last-child{border-bottom:none;}

/* colored top accent per channel */
.ch-accent{position:absolute;top:0;left:0;right:0;height:3px;}
.ch.t1 .ch-accent{background:linear-gradient(90deg,#008880,#00C8C0);box-shadow:0 0 8px rgba(0,200,192,.5);}
.ch.t2 .ch-accent{background:linear-gradient(90deg,#6030B0,#8B5CF6);box-shadow:0 0 8px rgba(139,92,246,.5);}
.ch.t3 .ch-accent{background:linear-gradient(90deg,#00A0A8,#00D8E0);box-shadow:0 0 8px rgba(0,200,192,.4);}
.ch.t4 .ch-accent{background:linear-gradient(90deg,#5020A8,#7844E0);box-shadow:0 0 8px rgba(120,68,224,.4);}

.ch-hdr-row{
  display:flex;align-items:center;justify-content:space-between;
  width:100%;margin-top:6px;margin-bottom:10px;flex-shrink:0;
}
.ch-id{
  font-size:8px;font-weight:800;letter-spacing:.16em;
  color:#7A9AB8;text-transform:uppercase;
}
.ch-pwr{
  width:18px;height:18px;border-radius:2px;
  border:1px solid rgba(0,196,192,.2);
  background:rgba(0,196,192,.06);
  color:rgba(0,196,192,.55);
  font-size:9px;font-weight:900;letter-spacing:0;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:all .15s;flex-shrink:0;line-height:1;
  padding:0;
}
.ch-pwr:hover{border-color:rgba(0,196,192,.5);color:rgba(0,196,192,.95);background:rgba(0,196,192,.14);}
.ch-pwr.off{
  border-color:rgba(210,60,60,.3);
  background:rgba(180,30,30,.1);
  color:rgba(210,80,80,.6);
  box-shadow:0 0 5px rgba(200,40,40,.12);
}
/* ── PICKUP MODE (soft takeover) ────────────────────────── */
.fader-ghost{
  position:absolute;
  width:44px;height:4px;
  left:50%;transform:translateX(-50%);
  background:rgba(255,210,60,.55);
  border-radius:2px;
  pointer-events:none;
  display:none;z-index:4;
  box-shadow:0 0 6px rgba(255,200,40,.5);
}
.fader-ghost.active{display:block;}
.pickup-label{
  position:absolute;top:2px;left:50%;transform:translateX(-50%);
  font-size:6px;font-weight:900;letter-spacing:.1em;color:rgba(255,210,60,.9);
  background:rgba(0,0,0,.7);padding:1px 4px;border-radius:2px;
  pointer-events:none;z-index:5;white-space:nowrap;display:none;
}
.pickup-label.active{display:block;}
.fader-track.pickup{border-color:rgba(255,200,40,.35);box-shadow:inset 0 8px 16px rgba(0,0,0,1),inset 2px 0 6px rgba(0,0,0,.85),inset -2px 0 6px rgba(0,0,0,.85),0 0 0 1px rgba(255,200,40,.12);}

/* dim fader+knob when muted; keep buttons accessible */
.ch.ch-off .fader-wrap,
.ch.ch-off .knob-wrap{
  opacity:.15;
  pointer-events:none;
  filter:grayscale(.9) brightness(.5);
  transition:opacity .2s,filter .2s;
}
.ch.ch-off .ch-btns{
  opacity:.4;
  filter:grayscale(.6) brightness(.65);
  transition:opacity .2s,filter .2s;
}
/* mute button stays fully live even when channel is off */
.ch.ch-off .ch-btn.mute-btn{
  opacity:1;
  pointer-events:auto;
  filter:none;
}

/* MUTE button styling */
.ch-btn.mute-btn{
  border-color:rgba(200,70,70,.18);
  color:rgba(190,90,90,.45);
}
.ch-btn.mute-btn:hover{
  border-color:rgba(210,70,70,.5);
  color:rgba(220,100,100,.9);
  background:rgba(160,30,30,.14);
  text-shadow:none;
}
@keyframes mute-pulse{
  0%,100%{box-shadow:0 0 8px rgba(200,60,60,.3),0 0 18px rgba(200,60,60,.12),0 0 0 1px rgba(200,60,60,.1);}
  50%    {box-shadow:0 0 16px rgba(200,60,60,.55),0 0 32px rgba(200,60,60,.22),0 0 0 1px rgba(200,60,60,.2);}
}
.ch-btn.mute-btn.active{
  background:linear-gradient(180deg,#1E0808,#120404);
  color:#D07070;
  border-color:rgba(200,60,60,.55);
  text-shadow:0 0 8px rgba(220,70,70,.7),0 0 16px rgba(200,60,60,.4);
  animation:mute-pulse 2.4s ease-in-out infinite;
}

/* ── HARDWARE FADER ─────────────────────────────────────── */
.fader-wrap{
  display:flex;flex-direction:column;align-items:center;
  flex:1;min-height:80px;max-height:240px;
  width:100%;gap:4px;
}
.fader-lbl{font-size:8px;color:#7A9AB8;font-weight:700;letter-spacing:.1em;text-transform:uppercase;flex-shrink:0;}

/* the rail assembly */
.fader-rail{
  position:relative;
  width:100%;flex:1;min-height:60px;
  display:flex;justify-content:center;align-items:stretch;
}
/* tick marks column — labels removed, lines only */
.fader-ticks{
  position:absolute;right:calc(50% + 7px);top:0;bottom:0;
  width:8px;display:flex;flex-direction:column;
  justify-content:space-between;padding:0;
  pointer-events:none;
}
.tick{
  display:flex;align-items:center;justify-content:flex-end;
  height:1px;
}
.tick-line{height:1px;background:var(--border2);flex-shrink:0;}
.tick-line.major{background:var(--chrome);height:1px;}

/* the groove/track — deep carved console slot */
.fader-track{
  width:10px;flex:1;
  background:linear-gradient(180deg,#010204 0%,#020406 50%,#010204 100%);
  border:1px solid #080F18;
  border-left-color:#050C14;
  border-right-color:#050C14;
  border-radius:2px;
  position:relative;
  cursor:ns-resize;
  touch-action:none;
  box-shadow:
    inset 0 8px 16px rgba(0,0,0,1),
    inset 2px 0 6px rgba(0,0,0,.85),
    inset -2px 0 6px rgba(0,0,0,.85),
    inset 0 1px 0 rgba(255,255,255,.015),
    0 0 0 1px rgba(0,196,232,.025);
}

/* Level Halo — atmospheric glow pool, light from the thumb cascading down */
.fader-fill{
  position:absolute;bottom:0;left:0;right:0;
  border-radius:1px;
  background:linear-gradient(0deg,
    rgba(0,160,152,.06) 0%,
    rgba(0,190,182,.15) 35%,
    rgba(0,210,200,.30) 65%,
    rgba(0,235,222,.50) 85%,
    rgba(0,255,245,.64) 100%
  );
  pointer-events:none;
  box-shadow:
    0 0 8px rgba(0,196,192,.28),
    inset 0 0 5px rgba(0,220,210,.12),
    0 0 18px rgba(0,180,175,.08);
  transition:box-shadow .38s ease-out, background .38s ease-out;
}
/* T2/T4 channels: glow purple instead of teal */
.ch.t2 .fader-fill,.ch.t4 .fader-fill{
  background:linear-gradient(0deg,
    rgba(80,30,180,.06) 0%,
    rgba(110,50,210,.15) 35%,
    rgba(135,75,235,.30) 65%,
    rgba(158,100,250,.50) 85%,
    rgba(175,120,255,.64) 100%
  );
  box-shadow:
    0 0 8px rgba(139,92,246,.26),
    inset 0 0 5px rgba(160,110,255,.10),
    0 0 18px rgba(139,92,246,.07);
}
.fader-track.dragging .fader-fill,
.fader-track.value-active .fader-fill{
  background:linear-gradient(0deg,
    rgba(0,160,152,.10) 0%,
    rgba(0,195,185,.24) 30%,
    rgba(0,218,205,.42) 60%,
    rgba(0,242,228,.66) 82%,
    rgba(0,255,248,.82) 100%
  );
  box-shadow:
    0 0 14px rgba(0,196,192,.55),
    inset 0 0 7px rgba(0,230,218,.22),
    0 0 32px rgba(0,180,175,.18),
    0 0 55px rgba(217,70,239,.07);
  transition:box-shadow .04s ease-in, background .04s ease-in;
}
.ch.t2 .fader-track.dragging .fader-fill,
.ch.t2 .fader-track.value-active .fader-fill,
.ch.t4 .fader-track.dragging .fader-fill,
.ch.t4 .fader-track.value-active .fader-fill{
  background:linear-gradient(0deg,
    rgba(80,30,180,.10) 0%,
    rgba(115,55,215,.24) 30%,
    rgba(140,80,240,.42) 60%,
    rgba(162,105,252,.66) 82%,
    rgba(180,128,255,.82) 100%
  );
  box-shadow:
    0 0 14px rgba(139,92,246,.55),
    inset 0 0 7px rgba(165,115,255,.22),
    0 0 32px rgba(139,92,246,.18),
    0 0 55px rgba(217,70,239,.10);
  transition:box-shadow .04s ease-in, background .04s ease-in;
}
.fader-track.dragging .fader-thumb,
.fader-track.value-active .fader-thumb{
  box-shadow:
    0 2px 12px rgba(0,0,0,.98),
    0 0 22px rgba(0,196,232,.65),
    0 0 44px rgba(0,196,232,.22),
    0 0 0 1px rgba(0,220,255,.28),
    inset 0 2px 0 rgba(255,255,255,.35);
  border-top-color:#F0F8FF;
  transition:box-shadow .04s ease-in, border-top-color .04s ease-in;
}

/* the hardware thumb cap — cold chrome console fader */
.fader-thumb{
  position:absolute;
  width:44px;height:28px;
  left:50%;transform:translateX(-50%);
  cursor:ns-resize;z-index:3;touch-action:none;
  border-radius:3px;
  background:linear-gradient(180deg,
    #EEF6FF 0%,
    #C8DCEE 4%,
    #587890 16%,
    #283E50 34%,
    #0E1C28 47%,
    #0A1620 50%,
    #0E1C28 53%,
    #283E50 66%,
    #587890 84%,
    #C8DCEE 96%,
    #EEF6FF 100%
  );
  border:1px solid #060E18;
  border-top-color:#D8EEFF;
  border-bottom-color:#040C14;
  box-shadow:
    0 3px 12px rgba(0,0,0,.95),
    0 0 0 1px rgba(0,196,232,.10),
    inset 0 2px 0 rgba(255,255,255,.22),
    0 0 10px rgba(0,200,192,.10);
}
/* hairline grip serrations */
.fader-thumb::before,.fader-thumb::after{
  content:'';position:absolute;
  left:14%;right:14%;height:1px;
  background:linear-gradient(90deg,transparent,rgba(0,0,0,.4) 25%,rgba(0,0,0,.4) 75%,transparent);
}
.fader-thumb::before{top:calc(50% - 5px);}
.fader-thumb::after {top:calc(50% + 5px);}
/* teal center stripe — the level marker, reads as a position cursor */
.fader-thumb .thumb-center{
  position:absolute;left:10%;right:10%;top:50%;
  height:2px;transform:translateY(-50%);
  background:linear-gradient(90deg,transparent,rgba(0,215,255,.82) 12%,rgba(0,250,255,1) 50%,rgba(0,215,255,.82) 88%,transparent);
  box-shadow:0 0 6px rgba(0,210,255,.82),0 0 16px rgba(0,200,255,.45),0 0 26px rgba(0,196,232,.20);
  border-radius:1px;
}

.fader-val{
  font-size:8px;font-weight:700;color:var(--accent);
  font-variant-numeric:tabular-nums;flex-shrink:0;
  text-shadow:0 0 8px rgba(0,200,192,.5);
}

/* ── HARDWARE KNOB ──────────────────────────────────────── */
.knob-wrap{
  display:flex;flex-direction:column;align-items:center;
  gap:4px;width:100%;flex-shrink:0;
  padding:8px 0 6px;
  border-top:1px solid var(--border);
}
.knob-lbl{font-size:8px;color:#7A9AB8;font-weight:700;letter-spacing:.1em;text-transform:uppercase;}
.knob{
  width:46px;height:46px;border-radius:50%;
  position:relative;cursor:grab;touch-action:none;
  background:conic-gradient(#0A1620 0deg 225deg, #0A1620 225deg 360deg);
  box-shadow:
    0 5px 18px rgba(0,0,0,1),
    0 0 0 1px rgba(0,0,0,.98),
    0 0 0 2px rgba(255,255,255,.04),
    inset 0 1px 0 rgba(255,255,255,.06),
    0 0 20px rgba(0,196,232,.08),
    0 0 40px rgba(0,196,232,.04);
  transition:box-shadow .15s;
}
.knob:active{cursor:grabbing;box-shadow:0 5px 18px rgba(0,0,0,1),0 0 0 1px rgba(0,0,0,.98),0 0 0 2px rgba(255,255,255,.04),inset 0 1px 0 rgba(255,255,255,.06),0 0 28px rgba(0,196,232,.28),0 0 52px rgba(0,196,232,.10);}
@keyframes detent-flash{
  0%  {filter:brightness(1);}
  35% {filter:brightness(2.5) drop-shadow(0 0 6px rgba(0,240,255,.9));}
  100%{filter:brightness(1);}
}
.knob.at-detent{animation:detent-flash .28s ease-out;}
/* matte inner body — hardware console knob */
.knob-body{
  position:absolute;inset:5px;border-radius:50%;
  background:radial-gradient(circle at 36% 30%,#2C3E54 0%,#16263A 28%,#0A1828 58%,#050E1A 100%);
  border:1px solid rgba(0,0,0,.98);
  box-shadow:
    inset 0 3px 7px rgba(255,255,255,.13),
    inset 0 -3px 9px rgba(0,0,0,.96),
    inset 1px 0 4px rgba(255,255,255,.05),
    inset -1px 0 4px rgba(0,0,0,.7),
    inset 0 0 18px rgba(0,0,0,.55),
    0 0 0 1px rgba(255,255,255,.05);
}
/* indicator pointer */
.knob-dot{
  position:absolute;
  width:3px;height:13px;
  background:linear-gradient(180deg,#FFFFFF 0%,#A0F0FF 30%,#00C8E8 100%);
  border-radius:2px;
  top:6px;left:50%;
  transform-origin:50% 17px;
  transform:translateX(-50%) rotate(0deg);
  box-shadow:0 0 5px rgba(0,240,255,.85),0 0 12px rgba(0,196,232,.65),0 0 22px rgba(0,196,232,.3);
  transition:box-shadow .38s ease-out;
}
.knob.value-active .knob-dot{
  box-shadow:0 0 8px rgba(0,255,255,1),0 0 18px rgba(0,220,255,.9),0 0 34px rgba(0,196,232,.65),0 0 52px rgba(0,196,232,.3);
  transition:box-shadow .04s ease-in;
}
@keyframes btn-pulse{
  0%,100%{box-shadow:0 0 8px rgba(0,196,232,.35),0 0 18px rgba(0,196,232,.15),0 0 0 1px rgba(217,70,239,.12),inset 0 1px 0 rgba(0,232,255,.1),inset 0 0 8px rgba(0,196,232,.04);}
  50%    {box-shadow:0 0 14px rgba(0,196,232,.55),0 0 30px rgba(0,196,232,.25),0 0 0 1px rgba(217,70,239,.22),inset 0 1px 0 rgba(0,232,255,.18),inset 0 0 14px rgba(0,196,232,.08);}
}
.knob-val{font-size:8px;font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums;text-shadow:0 0 8px rgba(0,200,192,.5);}

/* ── CHANNEL BUTTONS ────────────────────────────────────── */
.ch-btns{
  display:flex;flex-direction:column;
  gap:3px;width:100%;flex-shrink:0;
  border-top:1px solid var(--border);
  padding-top:7px;
}
.ch-btn{
  height:26px;border-radius:3px;
  border:1px solid var(--border2);
  border-top-color:#080E18;
  background:linear-gradient(180deg,#04080E 0%,#060C14 100%);
  color:var(--text2);
  font-size:9px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;
  cursor:pointer;transition:all .1s;
  display:flex;align-items:center;justify-content:center;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.7),inset 0 2px 6px rgba(0,0,0,.4),0 1px 0 rgba(255,255,255,.025);
}
.ch-btn:hover{background:var(--panel2);border-color:var(--accent);color:var(--accent2);text-shadow:0 0 8px rgba(0,200,255,.5);}
.ch-btn.active{
  background:linear-gradient(180deg,#002830,#001820);
  color:#20F8EE;border-color:#00DDD4;
  text-shadow:0 0 8px rgba(0,248,238,1),0 0 18px rgba(0,220,212,.6);
  box-shadow:inset 0 0 10px rgba(0,221,212,.08),0 0 6px rgba(0,221,212,.2);
  animation:btn-pulse 2.4s ease-in-out infinite;
}

/* ── CENTER MONITORING PANEL ───────────────────────────── */
.monitor{
  flex:1;display:flex;flex-direction:column;
  overflow:hidden;background:#080C10;min-width:0;
}
.panel-hd{
  padding:4px 14px;font-size:9px;font-weight:800;
  letter-spacing:.24em;color:var(--chrome);text-transform:uppercase;
  border-bottom:1px solid var(--border);background:#040608;flex-shrink:0;
}

/* METERS */
.meters-wrap{padding:8px 14px 7px;border-bottom:1px solid var(--border);flex-shrink:0;}
.section-hd{font-size:9px;font-weight:800;letter-spacing:.22em;color:var(--chrome2);text-transform:uppercase;margin-bottom:6px;}
.meters-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;}
.meter-row{display:flex;align-items:center;gap:5px;}
.meter-lbl{font-size:9px;color:var(--text);font-weight:700;letter-spacing:.04em;text-transform:uppercase;width:84px;flex-shrink:0;}
.meter-track{flex:1;height:4px;background:#030506;border-radius:2px;overflow:hidden;border:1px solid var(--border);}
.meter-fill{height:100%;background:linear-gradient(90deg,#005850 0%,var(--accent) 65%,var(--accent2) 100%);transition:width .12s ease;border-radius:1px;box-shadow:0 0 6px rgba(0,200,192,.3);}
.meter-val{font-size:10px;font-weight:700;color:var(--accent2);font-variant-numeric:tabular-nums;width:28px;text-align:right;flex-shrink:0;text-shadow:0 0 6px rgba(0,200,192,.4);}
.meter-lvl{font-size:8px;font-weight:800;letter-spacing:.04em;width:26px;flex-shrink:0;}
.lvl-low{color:#206860;}.lvl-med{color:var(--accent);}.lvl-high{color:var(--purple2);}

/* PILLS */
.pills-wrap{padding:5px 14px;border-bottom:1px solid var(--border);display:flex;gap:6px;align-items:center;flex-shrink:0;flex-wrap:wrap;}
.pill-group{display:flex;align-items:center;gap:4px;}
.pill-lbl{font-size:8px;color:var(--chrome);font-weight:700;letter-spacing:.12em;text-transform:uppercase;}
.pill{padding:2px 8px;border-radius:2px;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;transition:all .2s;}

/* PREVIEW RUN */
.preview-wrap{padding:8px 14px 7px;border-bottom:1px solid var(--border);flex:1;display:flex;flex-direction:column;min-height:0;}
.preview-top{display:flex;align-items:center;gap:8px;margin-bottom:5px;flex-shrink:0;}
.preview-hd{font-size:8px;font-weight:800;letter-spacing:.2em;color:var(--text3);text-transform:uppercase;}
.api-tag{font-size:8px;font-weight:700;padding:2px 6px;border-radius:2px;background:rgba(0,200,192,.08);color:#00A8A0;letter-spacing:.04em;border:1px solid rgba(0,200,192,.2);}
.task-row{display:flex;gap:6px;align-items:center;flex-shrink:0;}
.launch-wrap{
  padding:10px 14px 8px;border-bottom:1px solid var(--border);
  display:flex;gap:7px;align-items:center;flex-shrink:0;
}
.launch-input{
  flex:1;height:32px;border:1px solid var(--border2);border-radius:3px;
  background:#040608;padding:0 10px;
  font-family:'Inter',sans-serif;font-size:12px;color:var(--text);
  outline:none;transition:border-color .15s;user-select:text;
}
.launch-input:focus{border-color:var(--purple);box-shadow:0 0 0 2px rgba(139,92,246,.08);}
.launch-input::placeholder{color:var(--text3);}
.launch-btn{
  height:32px;padding:0 16px;
  background:linear-gradient(180deg,#100828,#0A0518);
  color:var(--purple2);
  border:1px solid rgba(139,92,246,.4);
  border-radius:3px;
  font-family:'Inter',sans-serif;font-size:9px;font-weight:800;
  letter-spacing:.14em;text-transform:uppercase;
  cursor:pointer;transition:all .12s;white-space:nowrap;
  box-shadow:0 0 10px rgba(139,92,246,.15);
}
.launch-btn:hover{background:linear-gradient(180deg,#180A38,#120620);box-shadow:0 0 18px rgba(139,92,246,.3);}
.launch-btn:disabled{opacity:.3;cursor:not-allowed;box-shadow:none;}
.launch-running{font-size:9px;color:var(--purple2);letter-spacing:.06em;display:none;text-shadow:0 0 8px rgba(167,139,250,.6);}
.launch-running.show{display:block;}
.task-input{
  flex:1;height:30px;border:1px solid var(--border2);border-radius:3px;
  background:#040608;padding:0 10px;
  font-family:'Inter',sans-serif;font-size:12px;color:var(--text);
  outline:none;transition:border-color .15s;user-select:text;
}
.task-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,200,192,.08);}
.task-input::placeholder{color:var(--text3);}
.run-btn{
  height:30px;padding:0 14px;
  background:linear-gradient(180deg,#001A18,#001010);
  color:var(--accent2);
  border:1px solid var(--accent);
  border-radius:3px;
  font-family:'Inter',sans-serif;font-size:9px;font-weight:800;
  letter-spacing:.14em;text-transform:uppercase;
  cursor:pointer;transition:all .12s;white-space:nowrap;
  box-shadow:0 0 8px rgba(0,200,192,.15);
}
.run-btn:hover{background:linear-gradient(180deg,#002420,#001A18);box-shadow:0 0 14px rgba(0,200,192,.28);}
.run-btn:disabled{opacity:.3;cursor:not-allowed;box-shadow:none;}
.resp-wrap{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden;margin-top:6px;position:relative;}
.resp-wrap.open{}
.resp-box{
  flex:1;padding:10px 12px;
  background:#030507;border:1px solid var(--border);border-radius:3px;
  font-size:12px;line-height:1.7;color:var(--text);
  white-space:pre-wrap;overflow-y:auto;
  font-family:'JetBrains Mono','Fira Code','Cascadia Code','SF Mono','Menlo',monospace;
  -webkit-overflow-scrolling:touch;
  letter-spacing:.01em;
}
.resp-box .err{color:#E05050;font-weight:600;}
/* PRESETS */
.presets-wrap{padding:6px 14px 8px;border-bottom:1px solid var(--border);flex-shrink:0;}
.presets-hd-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:5px;}
.preset-save-row{display:flex;gap:5px;align-items:center;}
.preset-input{height:22px;background:#030507;border:1px solid var(--border2);border-radius:2px;color:var(--text);font-size:10px;padding:0 7px;font-family:inherit;width:130px;}
.preset-input:focus{outline:none;border-color:var(--accent);}
.preset-save-btn{height:22px;padding:0 10px;border-radius:2px;border:1px solid var(--accent);background:rgba(0,200,192,.07);color:var(--accent);font-size:9px;font-weight:800;letter-spacing:.08em;cursor:pointer;transition:all .1s;flex-shrink:0;}
.preset-save-btn:hover{background:rgba(0,200,192,.18);}
.abort-btn{display:block;margin-top:4px;padding:2px 7px;height:16px;border-radius:2px;border:1px solid rgba(255,59,59,.4);background:transparent;color:rgba(255,80,80,.6);font-size:7px;font-weight:800;letter-spacing:.1em;cursor:pointer;transition:all .1s;text-transform:uppercase;line-height:1;}
.abort-btn:hover{border-color:#FF3B3B;color:#FF3B3B;background:rgba(255,59,59,.08);}
.abort-btn.firing{border-color:#FF6060;color:#FF6060;background:rgba(255,59,59,.2);}
.preset-list{display:flex;flex-wrap:wrap;gap:4px;min-height:16px;}
.preset-item{display:flex;align-items:center;gap:3px;background:#0A141E;border:1px solid var(--border2);border-radius:2px;padding:2px 4px 2px 8px;transition:border-color .1s;}
.preset-item:hover{border-color:var(--accent);}
.preset-name{font-size:10px;font-weight:700;color:var(--text);letter-spacing:.04em;cursor:pointer;white-space:nowrap;}
.preset-load{font-size:8px;padding:1px 5px;border-radius:2px;border:1px solid var(--border2);background:transparent;color:var(--text3);cursor:pointer;font-weight:700;letter-spacing:.06em;flex-shrink:0;}
.preset-load:hover{border-color:var(--accent);color:var(--accent);}
.preset-del{width:16px;height:16px;border:none;background:transparent;color:var(--text3);font-size:11px;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;line-height:1;flex-shrink:0;}
.preset-del:hover{color:#E05050;}
.preset-empty{font-size:9px;color:var(--text3);letter-spacing:.04em;}
.copy-btn{position:absolute;bottom:10px;right:10px;width:22px;height:22px;border-radius:2px;border:1px solid var(--border2);background:rgba(11,16,24,.85);color:var(--text3);font-size:12px;cursor:pointer;display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .15s;line-height:1;}
.resp-wrap:hover .copy-btn{opacity:1;}
.copy-btn:hover{color:var(--accent);border-color:var(--accent);background:rgba(0,200,192,.08);}
.copy-btn.copied{color:#50C878;border-color:#50C878;opacity:1;}



/* HISTORY */
.history-wrap{flex-shrink:0;min-height:120px;max-height:220px;overflow-y:auto;padding:6px 14px 8px;border-top:1px solid var(--border);-webkit-overflow-scrolling:touch;}
.history-empty{font-size:11px;color:var(--text3);font-style:italic;text-align:center;padding:18px 0;}
.history-hd-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;}
.clear-btn{height:20px;padding:0 8px;border-radius:2px;border:1px solid var(--border);background:transparent;color:var(--text3);font-size:8px;font-weight:700;cursor:pointer;letter-spacing:.06em;text-transform:uppercase;transition:all .1s;}
.clear-btn:hover{background:#4A1010;color:#F08080;border-color:#7F1D1D;}
.hc{background:var(--panel);border:1px solid var(--border);border-radius:3px;padding:6px 9px;margin-bottom:4px;cursor:pointer;transition:border-color .1s;}
.hc:hover{border-color:var(--border2);}
.hc.open{border-color:var(--accent);box-shadow:0 0 12px rgba(0,200,192,.08);}
.hc-top{display:flex;gap:6px;align-items:center;margin-bottom:3px;}
.hc-time{font-size:9px;color:var(--text3);font-variant-numeric:tabular-nums;font-weight:600;}
.hc-mode{font-size:9px;font-weight:800;padding:1px 5px;border-radius:2px;background:var(--panel2);color:var(--accent2);letter-spacing:.08em;border:1px solid var(--border2);}
.hc-peek{font-size:9px;color:var(--text3);font-weight:600;margin-left:auto;font-variant-numeric:tabular-nums;}
.hc-chevron{font-size:8px;color:var(--text3);transition:transform .15s;flex-shrink:0;}
.hc.open .hc-chevron{transform:rotate(180deg);}
.hc-task{font-size:12px;font-weight:700;color:var(--text);margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.hc-preview{font-size:10px;color:var(--text3);line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.hc-body{display:none;margin-top:7px;padding-top:7px;border-top:1px solid var(--border);}
.hc.open .hc-body{display:block;}
.hc-state-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:3px;margin-bottom:7px;}
.hc-si{background:var(--fader-bg);border-radius:2px;padding:4px 5px;border:1px solid var(--border);}
.hc-si-lbl{font-size:7px;color:var(--text3);font-weight:700;text-transform:uppercase;letter-spacing:.06em;}
.hc-si-val{font-size:11px;color:var(--accent2);font-weight:800;margin-top:1px;font-variant-numeric:tabular-nums;}
.hc-full{font-size:11px;line-height:1.6;color:var(--text);white-space:pre-wrap;background:#040608;border:1px solid var(--border);border-radius:3px;padding:8px 10px;max-height:180px;overflow-y:auto;margin-bottom:6px;font-family:'Inter',sans-serif;}
.hc-actions{display:flex;gap:5px;justify-content:flex-end;}
.hc-copy{height:24px;padding:0 10px;border-radius:2px;border:1px solid var(--border2);background:var(--panel);color:var(--text2);font-size:9px;font-weight:700;cursor:pointer;transition:all .1s;letter-spacing:.06em;text-transform:uppercase;}
.hc-copy:hover{background:var(--panel2);color:var(--accent2);border-color:var(--accent);}

/* FAQ PANEL */
.faq-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:100;}
.faq-overlay.open{display:block;}
.faq-panel{position:fixed;right:-500px;top:0;bottom:0;width:480px;background:var(--panel);z-index:101;transition:right .26s cubic-bezier(.4,0,.2,1);overflow-y:auto;border-left:2px solid var(--accent);display:flex;flex-direction:column;box-shadow:-8px 0 40px rgba(0,0,0,.8),-2px 0 0 rgba(0,200,192,.15);}
.faq-panel.open{right:0;}
.faq-hd{padding:13px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;background:#040608;}
.faq-title{font-family:'Abril Fatface',serif;font-size:20px;color:var(--accent2);text-shadow:0 0 20px rgba(0,232,224,.4);}
.faq-close{width:26px;height:26px;border:1px solid var(--border2);background:transparent;cursor:pointer;font-size:13px;color:var(--text2);border-radius:50%;transition:background .12s;display:flex;align-items:center;justify-content:center;font-weight:700;}
.faq-close:hover{background:var(--panel2);color:var(--accent2);border-color:var(--accent);}
.faq-body{padding:18px;flex:1;}
.faq-s{margin-bottom:20px;}
.faq-s-title{font-size:8px;font-weight:800;letter-spacing:.24em;text-transform:uppercase;color:var(--accent2);margin-bottom:7px;text-shadow:0 0 8px rgba(0,200,192,.3);}
.faq-p{font-size:12px;line-height:1.7;color:var(--text2);margin-bottom:7px;}
.faq-track{margin-bottom:8px;padding:9px 12px;background:#060A0F;border-radius:3px;border-left:3px solid var(--accent);}
.faq-track-name{font-size:10px;font-weight:800;color:var(--accent2);margin-bottom:3px;letter-spacing:.06em;}
.faq-track-desc{font-size:11px;color:var(--text2);line-height:1.6;}
.faq-code{font-family:'JetBrains Mono','Courier New',monospace;font-size:11px;background:#040608;padding:7px 10px;border-radius:3px;color:var(--accent2);margin:5px 0;display:block;border:1px solid var(--border);}

/* ── iPAD / iOS ────────────────────────────────────────── */
@supports(padding: env(safe-area-inset-bottom)){
  body{ padding-bottom: env(safe-area-inset-bottom); }
}
.ch-btn,.ch-pwr,.run-btn,.launch-btn,.faq-btn,.clear-btn{
  -webkit-tap-highlight-color:transparent;
  touch-action:manipulation;
}
@media(hover:none){
  .ch-btn:hover{background:linear-gradient(180deg,#04080E 0%,#060C14 100%);border-color:var(--border2);color:var(--text2);text-shadow:none;}
  .ch-btn.active:hover{background:linear-gradient(180deg,#001C22,#001018);color:var(--accent2);border-color:var(--accent);}
}
@media(max-width:1024px){
  .hdr{height:48px;padding:0 12px;}
  .fader-track{width:14px;}
  .fader-thumb{width:48px;height:32px;}
  .knob{width:42px;height:42px;}
  .ch-btn{height:30px;}
}
</style>
</head>
<body>

<!-- ── HEADER ─────────────────────────────────────────────────── -->
<div class="hdr">
  <span class="brand">gain</span>
  <div class="hdr-center">
    <div class="hdr-settings-label">Current Settings</div>
    <div class="hdr-settings" id="hdr-settings">—</div>
  </div>
  <div class="hdr-right">
    <span id="user-email" style="font-size:9px;letter-spacing:.08em;color:var(--text3);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></span>
    <button class="theme-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle light/dark">◐</button>
    <button class="reset-btn" onclick="resetDefaults()">Reset to Defaults</button>
    <button class="faq-btn" onclick="openFaq()">?</button>
    <button class="reset-btn" id="logout-btn" onclick="logout()" style="display:none">Sign Out</button>
  </div>
</div>

<!-- ── HERO BANNER ─────────────────────────────────────────────── -->
<div class="hero">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 108" preserveAspectRatio="xMidYMid slice">
    <defs>
      <linearGradient id="bg-grad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#060A0F"/>
        <stop offset="100%" stop-color="#040608"/>
      </linearGradient>
      <linearGradient id="scan-h" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="#00C8C0" stop-opacity="0"/>
        <stop offset="40%" stop-color="#00C8C0" stop-opacity="0.12"/>
        <stop offset="60%" stop-color="#00C8C0" stop-opacity="0.12"/>
        <stop offset="100%" stop-color="#00C8C0" stop-opacity="0"/>
      </linearGradient>
      <linearGradient id="teal-fade" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="#00C8C0" stop-opacity="0.5"/>
        <stop offset="100%" stop-color="#00C8C0" stop-opacity="0"/>
      </linearGradient>
      <filter id="glow-teal" x="-8%" y="-80%" width="116%" height="260%">
        <feGaussianBlur in="SourceGraphic" stdDeviation="2.5" result="blur"/>
        <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="glow-purple" x="-4%" y="-60%" width="108%" height="220%">
        <feGaussianBlur in="SourceGraphic" stdDeviation="2" result="blur"/>
        <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
    </defs>
    <!-- Background -->
    <rect width="1200" height="108" fill="url(#bg-grad)"/>
    <!-- Grid — horizontal -->
    <line x1="0" y1="18" x2="1200" y2="18" stroke="#162030" stroke-width="0.5"/>
    <line x1="0" y1="36" x2="1200" y2="36" stroke="#162030" stroke-width="0.5"/>
    <line x1="0" y1="54" x2="1200" y2="54" stroke="#1E2E40" stroke-width="0.8"/>
    <line x1="0" y1="72" x2="1200" y2="72" stroke="#162030" stroke-width="0.5"/>
    <line x1="0" y1="90" x2="1200" y2="90" stroke="#162030" stroke-width="0.5"/>
    <!-- Grid — vertical -->
    <line x1="150" y1="0" x2="150" y2="108" stroke="#162030" stroke-width="0.5"/>
    <line x1="300" y1="0" x2="300" y2="108" stroke="#162030" stroke-width="0.5"/>
    <line x1="450" y1="0" x2="450" y2="108" stroke="#162030" stroke-width="0.5"/>
    <line x1="600" y1="0" x2="600" y2="108" stroke="#1E2E40" stroke-width="0.8"/>
    <line x1="750" y1="0" x2="750" y2="108" stroke="#162030" stroke-width="0.5"/>
    <line x1="900" y1="0" x2="900" y2="108" stroke="#162030" stroke-width="0.5"/>
    <line x1="1050" y1="0" x2="1050" y2="108" stroke="#162030" stroke-width="0.5"/>
    <!-- Oscilloscope waveform (teal) — snapped to grid y=18..90 -->
    <polyline points="0,54 30,54 38,29 46,79 54,18 62,90 70,38 78,70 86,54 120,54" stroke="#00DDD4" stroke-width="1.5" fill="none" opacity="0.7" filter="url(#glow-teal)"/>
    <!-- Sine wave (purple) — full width, fits within y=18..90 -->
    <path d="M120,54 C165,54 180,18 225,18 C270,18 285,90 330,90 C375,90 390,18 435,18 C480,18 495,90 540,90 C585,90 600,54 660,54 C720,54 735,18 780,18 C825,18 840,90 885,90 C930,90 945,18 990,18 C1035,18 1050,90 1095,90 C1140,90 1155,54 1200,54" stroke="#8B5CF6" stroke-width="1.8" fill="none" opacity="0.5" filter="url(#glow-purple)"/>
    <!-- Scan line glow -->
    <rect x="0" y="51" width="1200" height="6" fill="url(#scan-h)"/>
    <!-- Intersection accent dots -->
    <circle cx="120" cy="54" r="2.5" fill="#00C8C0" opacity="0.6"/>
    <circle cx="600" cy="54" r="2" fill="#8B5CF6" opacity="0.5"/>
    <circle cx="1080" cy="54" r="1.5" fill="#00C8C0" opacity="0.3"/>
    <!-- Bottom accent line -->
    <rect x="0" y="106" width="1200" height="2" fill="#00C8C0" opacity="0.15"/>
    <!-- Left origin marker -->
    <line x1="0" y1="0" x2="0" y2="108" stroke="#00C8C0" stroke-width="2" opacity="0.3"/>
  </svg>
  <div class="hero-left">
    <div class="hero-tagline">dial it in.</div>
  </div>
</div>

<!-- ── 3-COLUMN CONSOLE ────────────────────────────────────────── -->
<div class="console">

  <!-- LEFT BANK: T1 + T2 -->
  <div class="channel-bank">

    <!-- CHANNEL T1: MODE / DRIVE -->
    <div class="ch t1">
      <div class="ch-accent"></div>
      <div class="ch-hdr-row"><span class="ch-id">Track 1 — MODE</span><button class="ch-pwr" id="cpwr-t1" onclick="toggleTrack('t1')" title="Enable / mute track">◉</button></div>

      <div class="fader-wrap">
        <div class="fader-lbl">EFFORT</div>
        <div class="fader-rail">
          <!-- tick marks -->
          <div class="fader-ticks" id="ticks-intensity"></div>
          <div class="fader-track" id="ft-intensity">
            <div class="fader-fill" id="ff-intensity"></div>
            <div class="fader-ghost" id="fg-intensity"></div>
            <div class="pickup-label" id="pl-intensity">MOVE TO SYNC</div>
            <div class="fader-thumb" id="fth-intensity"><div class="thumb-center"></div></div>
          </div>
        </div>
        <div class="fader-val" id="fv-intensity">0.50</div>
      </div>

      <div class="knob-wrap">
        <div class="knob-lbl">THINKING TIME</div>
        <div class="knob" id="knob-depth">
          <div class="knob-body"></div>
          <div class="knob-dot" id="kd-depth"></div>
        </div>
        <div class="knob-val" id="kv-depth">0.50</div>
      </div>

      <div class="ch-btns">
        <div class="ch-btn" data-field="mode" data-val="EXPLORE" onclick="toggleBtn('mode','EXPLORE')">EXPLORE</div>
        <div class="ch-btn mute-btn" id="mbtn-t1" onclick="toggleTrack('t1')">MUTE</div>
        <div class="ch-btn" data-field="mode" data-val="BUILD"   onclick="toggleBtn('mode','BUILD')">BUILD</div>
      </div>
    </div>

    <!-- CHANNEL T2: CONFIDENCE -->
    <div class="ch t2">
      <div class="ch-accent"></div>
      <div class="ch-hdr-row"><span class="ch-id">Track 2 — CONFIDENCE</span><button class="ch-pwr" id="cpwr-t2" onclick="toggleTrack('t2')" title="Enable / mute track">◉</button></div>

      <div class="fader-wrap">
        <div class="fader-lbl">CONFIDENCE</div>
        <div class="fader-rail">
          <div class="fader-ticks" id="ticks-certainty"></div>
          <div class="fader-track" id="ft-certainty">
            <div class="fader-fill" id="ff-certainty"></div>
            <div class="fader-ghost" id="fg-certainty"></div>
            <div class="pickup-label" id="pl-certainty">MOVE TO SYNC</div>
            <div class="fader-thumb" id="fth-certainty"><div class="thumb-center"></div></div>
          </div>
        </div>
        <div class="fader-val" id="fv-certainty">0.50</div>
      </div>

      <div class="knob-wrap">
        <div class="knob-lbl">BOLDNESS</div>
        <div class="knob" id="knob-risk">
          <div class="knob-body"></div>
          <div class="knob-dot" id="kd-risk"></div>
        </div>
        <div class="knob-val" id="kv-risk">0.50</div>
      </div>

      <div class="ch-btns">
        <div class="ch-btn" data-field="stance" data-val="LIST"   onclick="toggleBtn('stance','LIST')">LIST</div>
        <div class="ch-btn mute-btn" id="mbtn-t2" onclick="toggleTrack('t2')">MUTE</div>
        <div class="ch-btn" data-field="stance" data-val="DECIDE" onclick="toggleBtn('stance','DECIDE')">DECIDE</div>
      </div>
    </div>

  </div><!-- /left bank -->

  <!-- CENTER: MONITORING -->
  <div class="monitor">
    <div class="panel-hd">MONITORING</div>

    <div class="meters-wrap">
      <div class="section-hd">PARAMETER LEVELS</div>
      <div class="meters-grid">
        <div class="meter-row"><span class="meter-lbl">EFFORT</span><div class="meter-track"><div class="meter-fill" id="m-intensity" style="width:50%"></div></div><span class="meter-val" id="mv-intensity">0.50</span><span class="meter-lvl lvl-med" id="ml-intensity">MED</span></div>
        <div class="meter-row"><span class="meter-lbl">CONFIDENCE</span><div class="meter-track"><div class="meter-fill" id="m-certainty" style="width:50%"></div></div><span class="meter-val" id="mv-certainty">0.50</span><span class="meter-lvl lvl-med" id="ml-certainty">MED</span></div>
        <div class="meter-row"><span class="meter-lbl">THINK TIME</span><div class="meter-track"><div class="meter-fill" id="m-depth" style="width:50%"></div></div><span class="meter-val" id="mv-depth">0.50</span><span class="meter-lvl lvl-med" id="ml-depth">MED</span></div>
        <div class="meter-row"><span class="meter-lbl">BOLDNESS</span><div class="meter-track"><div class="meter-fill" id="m-risk" style="width:50%"></div></div><span class="meter-val" id="mv-risk">0.50</span><span class="meter-lvl lvl-med" id="ml-risk">MED</span></div>
        <div class="meter-row"><span class="meter-lbl">ZOOM LEVEL</span><div class="meter-track"><div class="meter-fill" id="m-scope" style="width:50%"></div></div><span class="meter-val" id="mv-scope">0.50</span><span class="meter-lvl lvl-med" id="ml-scope">MED</span></div>
        <div class="meter-row"><span class="meter-lbl">VERBOSITY</span><div class="meter-track"><div class="meter-fill" id="m-room" style="width:30%"></div></div><span class="meter-val" id="mv-room">0.30</span><span class="meter-lvl lvl-low" id="ml-room">LOW</span></div>
        <div class="meter-row"><span class="meter-lbl">CONTEXT SIZE</span><div class="meter-track"><div class="meter-fill" id="m-bandwidth" style="width:50%"></div></div><span class="meter-val" id="mv-bandwidth">0.50</span><span class="meter-lvl lvl-med" id="ml-bandwidth">MED</span></div>
        <div class="meter-row"><span class="meter-lbl">MEMORY</span><div class="meter-track"><div class="meter-fill" id="m-decay" style="width:30%"></div></div><span class="meter-val" id="mv-decay">0.30</span><span class="meter-lvl lvl-low" id="ml-decay">LOW</span></div>
      </div>
    </div>

    <div class="pills-wrap">
      <div class="pill-group"><span class="pill-lbl">MODE</span><div class="pill" id="pill-mode">—</div></div>
      <div class="pill-group"><span class="pill-lbl">STANCE</span><div class="pill" id="pill-stance">—</div></div>
      <div class="pill-group"><span class="pill-lbl">FILTER</span><div class="pill" id="pill-filter">—</div></div>
      <div class="pill-group"><span class="pill-lbl">VOICE</span><div class="pill" id="pill-voice">—</div></div>
    </div>

    <!-- PRESETS -->
    <div class="presets-wrap">
      <div class="presets-hd-row">
        <div class="section-hd">PRESETS</div>
        <div class="preset-save-row">
          <input class="preset-input" id="preset-input" type="text" placeholder="name this state…" maxlength="40">
          <button class="preset-save-btn" onclick="savePreset()">SAVE</button>
        </div>
        <button class="abort-btn" id="abort-btn" onclick="abortRun()">&#9632; ABORT</button>
      </div>
      <div class="preset-list" id="preset-list"><span class="preset-empty">no presets saved</span></div>
    </div>

    <!-- CTRL RUN LAUNCHER -->
    <div class="launch-wrap">
      <input class="launch-input" id="launch-input" type="text" placeholder="type a task · Enter to run — reads and writes files, no terminal needed">
      <span class="launch-running" id="launch-running">● RUNNING</span>
      <button class="launch-btn" id="launch-btn">LAUNCH</button>
    </div>

    <div class="preview-wrap">
      <div class="preview-top">
        <span class="preview-hd">PREVIEW RUN</span>
        <span class="api-tag">Claude API · no file access</span>
      </div>
      <div class="task-row">
        <input class="task-input" id="task-input" type="text" placeholder="type a task to preview parameter effects…">
        <button class="run-btn" id="run-btn">RUN</button>
      </div>
      <div class="resp-wrap" id="resp-wrap">
        <div class="resp-box" id="resp-box"></div>
        <button class="copy-btn" id="copy-btn" onclick="copyResp()" title="Copy output">⎘</button>
      </div>
    </div>

    <!-- RUN LOG -->
    <div class="history-wrap">
      <div class="history-hd-row">
        <div class="section-hd">RUN LOG</div>
        <button class="clear-btn" onclick="clearHistory()">Clear</button>
      </div>
      <div id="history"><div class="history-empty">no runs yet</div></div>
    </div>
  </div>

  <!-- RIGHT BANK: T3 + T4 -->
  <div class="channel-bank right">

    <!-- CHANNEL T3: SCOPE -->
    <div class="ch t3">
      <div class="ch-accent"></div>
      <div class="ch-hdr-row"><span class="ch-id">Track 3 — SCOPE</span><button class="ch-pwr" id="cpwr-t3" onclick="toggleTrack('t3')" title="Enable / mute track">◉</button></div>

      <div class="fader-wrap">
        <div class="fader-lbl">ZOOM LEVEL</div>
        <div class="fader-rail">
          <div class="fader-ticks" id="ticks-scope"></div>
          <div class="fader-track" id="ft-scope">
            <div class="fader-fill" id="ff-scope"></div>
            <div class="fader-ghost" id="fg-scope"></div>
            <div class="pickup-label" id="pl-scope">MOVE TO SYNC</div>
            <div class="fader-thumb" id="fth-scope"><div class="thumb-center"></div></div>
          </div>
        </div>
        <div class="fader-val" id="fv-scope">0.50</div>
      </div>

      <div class="knob-wrap">
        <div class="knob-lbl">CONTEXT SIZE</div>
        <div class="knob" id="knob-bandwidth">
          <div class="knob-body"></div>
          <div class="knob-dot" id="kd-bandwidth"></div>
        </div>
        <div class="knob-val" id="kv-bandwidth">0.50</div>
      </div>

      <div class="ch-btns">
        <div class="ch-btn" data-field="filter" data-val="FILE"    onclick="toggleBtn('filter','FILE')">FILE</div>
        <div class="ch-btn mute-btn" id="mbtn-t3" onclick="toggleTrack('t3')">MUTE</div>
        <div class="ch-btn" data-field="filter" data-val="PROJECT" onclick="toggleBtn('filter','PROJECT')">PROJECT</div>
      </div>
    </div>

    <!-- CHANNEL T4: VOICE -->
    <div class="ch t4">
      <div class="ch-accent"></div>
      <div class="ch-hdr-row"><span class="ch-id">Track 4 — VOICE</span><button class="ch-pwr" id="cpwr-t4" onclick="toggleTrack('t4')" title="Enable / mute track">◉</button></div>

      <div class="fader-wrap">
        <div class="fader-lbl">VERBOSITY</div>
        <div class="fader-rail">
          <div class="fader-ticks" id="ticks-room"></div>
          <div class="fader-track" id="ft-room">
            <div class="fader-fill" id="ff-room"></div>
            <div class="fader-ghost" id="fg-room"></div>
            <div class="pickup-label" id="pl-room">MOVE TO SYNC</div>
            <div class="fader-thumb" id="fth-room"><div class="thumb-center"></div></div>
          </div>
        </div>
        <div class="fader-val" id="fv-room">0.30</div>
      </div>

      <div class="knob-wrap">
        <div class="knob-lbl">MEMORY PERSISTENCE</div>
        <div class="knob" id="knob-decay">
          <div class="knob-body"></div>
          <div class="knob-dot" id="kd-decay"></div>
        </div>
        <div class="knob-val" id="kv-decay">0.30</div>
      </div>

      <div class="ch-btns">
        <div class="ch-btn" data-field="voice" data-val="DIRECT" onclick="toggleBtn('voice','DIRECT')">DIRECT</div>
        <div class="ch-btn mute-btn" id="mbtn-t4" onclick="toggleTrack('t4')">MUTE</div>
        <div class="ch-btn" data-field="voice" data-val="OPEN"   onclick="toggleBtn('voice','OPEN')">OPEN</div>
      </div>
    </div>

  </div><!-- /right bank -->

</div><!-- /console -->

<!-- FAQ -->
<div class="faq-overlay" id="faq-overlay" onclick="closeFaq()"></div>
<div class="faq-panel" id="faq-panel">
  <div class="faq-hd">
    <span class="faq-title">control</span>
    <button class="faq-close" onclick="closeFaq()">✕</button>
  </div>
  <div class="faq-body">

    <div class="faq-s">
      <div class="faq-s-title">what is gain?</div>
      <p class="faq-p">Gain is a behavioral mixing board for AI coding agents. Every fader, knob, and button writes to a system prompt in real time. Same task, different state — measurably different output. You perform instead of prompt.</p>
      <p class="faq-p">Four tracks. Each one controls a different dimension of how Claude thinks. All parameters flow through <code style="background:#040608;padding:1px 4px;border-radius:2px;font-size:10px">~/.streamfader/state.json</code>, which the CLI reads on every run.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">T1 — MODE</div>
      <div class="faq-track">
        <div class="faq-track-name">Fader: Effort &nbsp;·&nbsp; Knob: Thinking Time</div>
        <div class="faq-track-desc">Sets what Claude is allowed to do and how it reasons.</div>
        <div class="faq-track-desc" style="margin-top:6px">
          <strong style="color:var(--accent2)">EXPLORE</strong> — analysis only. No code changes. Ends with a single decision point.<br>
          <strong style="color:var(--accent2)">BUILD</strong> — one atomic change only. No refactoring unless required.<br>
          <em style="opacity:.5">Nothing selected = track off, mode rules skipped.</em>
        </div>
        <div class="faq-track-desc" style="margin-top:6px">
          <strong style="color:var(--text)">Effort</strong> — HIGH compresses output to direct execution. LOW opens verbose exploratory reasoning.<br>
          <strong style="color:var(--text)">Thinking Time</strong> — HIGH allows deep diagnostic chains. LOW keeps reasoning at the surface.
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">T2 — CONFIDENCE</div>
      <div class="faq-track" style="border-left-color:#8B5CF6">
        <div class="faq-track-name">Fader: Confidence &nbsp;·&nbsp; Knob: Boldness</div>
        <div class="faq-track-desc">Controls how committed Claude is to its choices and how bold the changes are.</div>
        <div class="faq-track-desc" style="margin-top:6px">
          <strong style="color:#A78BFA">LIST</strong> — shows 2–3 alternatives with pros/cons. Does not pick.<br>
          <strong style="color:#A78BFA">DECIDE</strong> — picks one approach and implements it. Zero explanation of alternatives.
        </div>
        <div class="faq-track-desc" style="margin-top:6px">
          <strong style="color:var(--text)">Confidence</strong> — HIGH commits to one solution, no hedging. LOW shows options.<br>
          <strong style="color:var(--text)">Boldness</strong> — HIGH pursues the best solution even if it requires large changes. LOW stays close to existing patterns.
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">T3 — SCOPE</div>
      <div class="faq-track" style="border-left-color:#00A8A0">
        <div class="faq-track-name">Fader: Zoom Level &nbsp;·&nbsp; Knob: Context Size</div>
        <div class="faq-track-desc">Controls how wide Claude looks when building context.</div>
        <div class="faq-track-desc" style="margin-top:6px">
          <strong style="color:var(--accent2)">FILE</strong> — strict local scope. This file only, no cross-module pulls.<br>
          <strong style="color:var(--accent2)">PROJECT</strong> — full codebase context allowed, global awareness.
        </div>
        <div class="faq-track-desc" style="margin-top:6px">
          <strong style="color:var(--text)">Zoom Level</strong> — how far out Claude searches for relevant code and context.<br>
          <strong style="color:var(--text)">Context Size</strong> — HIGH pulls in adjacent concerns freely. LOW is surgical, touches nothing adjacent.
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">T4 — VOICE</div>
      <div class="faq-track" style="border-left-color:#6040C8">
        <div class="faq-track-name">Fader: Verbosity &nbsp;·&nbsp; Knob: Memory Persistence</div>
        <div class="faq-track-desc">Controls how output feels — the texture of Claude's responses.</div>
        <div class="faq-track-desc" style="margin-top:6px">
          <strong style="color:#A78BFA">DIRECT</strong> — dead room. Output only, zero commentary or preamble.<br>
          <strong style="color:#A78BFA">OPEN</strong> — full resonance. Thinks out loud with you, collaborative tone.
        </div>
        <div class="faq-track-desc" style="margin-top:6px">
          <strong style="color:var(--text)">Verbosity</strong> — WET gives breathing room and space. DRY is close-mic'd, just the output.<br>
          <strong style="color:var(--text)">Memory Persistence</strong> — LONG lets ideas echo and build. SHORT compresses to tight, every-word-counts density.
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">muting tracks</div>
      <p class="faq-p">Every track can be turned off independently. A muted track contributes nothing to the system prompt — those rules are completely skipped.</p>
      <p class="faq-p">Two ways to mute a track:</p>
      <p class="faq-p"><strong style="color:var(--accent2)">◉ button</strong> in the channel header — top right of each strip. Click to toggle off/on. Glows red when muted.</p>
      <p class="faq-p"><strong style="color:#D07070">MUTE button</strong> in the button row — middle button on each track. Same toggle, different physical location. Pulses red when active to make muted state visible at a glance.</p>
      <p class="faq-p">On a physical nanoKONTROL2, the <strong style="color:var(--text)">M buttons</strong> (row 2 on the controller) mute the corresponding track.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">button toggle behavior</div>
      <p class="faq-p">All mode buttons (EXPLORE, BUILD, LIST, DECIDE, FILE, PROJECT, DIRECT, OPEN) are deselectable. Click an active button again to turn it off entirely. When nothing is selected, that parameter is absent from the system prompt — Claude uses its default behavior for that dimension.</p>
      <p class="faq-p">This is different from picking a middle ground. Leaving a parameter empty means <em>no rule applied</em>, not a neutral setting.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">faders</div>
      <p class="faq-p"><strong style="color:var(--text)">Drag</strong> — click and drag vertically anywhere on the track. The Level Halo (atmospheric glow fill) rises with the value — the higher the setting, the more light pools in the channel.</p>
      <p class="faq-p"><strong style="color:var(--text)">Fine mode</strong> — hold <kbd style="background:#040608;padding:1px 5px;border-radius:2px;font-size:10px;border:1px solid var(--border2)">Shift</kbd> while dragging for 5× precision. Useful for dialing exact values.</p>
      <p class="faq-p"><strong style="color:var(--text)">Reset</strong> — double-click the thumb to reset to 0.50.</p>
      <p class="faq-p">The fader glows teal (T1/T3) or purple (T2/T4) during both mouse drag and physical controller movement.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">knobs</div>
      <p class="faq-p"><strong style="color:var(--text)">Drag vertically</strong> on the knob — up increases, down decreases. The arc fills clockwise to show position.</p>
      <p class="faq-p">The knob glow scales with the value — at 0 it barely glows; at 1.0 it burns bright. You can read rough position from the glow intensity without looking at the number.</p>
      <p class="faq-p"><strong style="color:var(--text)">Center flash</strong> — when you hit 0.50 exactly, the dot briefly flashes white as a detent cue.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">LAUNCH vs PREVIEW RUN</div>
      <p class="faq-p"><strong style="color:#A78BFA">LAUNCH</strong> (purple) — full agentic loop. Claude reads files, writes code, runs shell commands — up to 20 tool turns. This is the real thing. Every move on the board shapes what it does and how it responds.</p>
      <p class="faq-p"><strong style="color:var(--accent2)">PREVIEW RUN</strong> (teal) — API only, no file access. Shows you exactly what your current state produces in plain text. Good for dialing in behavior before committing to a full run.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">physical controller</div>
      <p class="faq-p">Korg nanoKONTROL2. Start the MIDI bridge:</p>
      <code class="faq-code">ctrl nano --start</code>
      <p class="faq-p">Controller layout:</p>
      <div class="faq-track">
        <div class="faq-track-desc">
          <strong style="color:var(--text)">Faders 1–4</strong> — Effort, Confidence, Zoom Level, Verbosity<br>
          <strong style="color:var(--text)">Knobs 1–4</strong> — Thinking Time, Boldness, Context Size, Memory Persistence<br>
          <strong style="color:var(--text)">S buttons</strong> (row 1) — left-extreme mode per track (EXPLORE / LIST / FILE / DIRECT)<br>
          <strong style="color:var(--text)">M buttons</strong> (row 2) — mute T1 / T2 / T3 / T4<br>
          <strong style="color:var(--text)">R buttons</strong> (row 3) — right-extreme mode per track (BUILD / DECIDE / PROJECT / OPEN)<br>
          <strong style="color:var(--text)">PLAY</strong> — replay last task<br>
          <strong style="color:var(--text)">STOP</strong> — kill running Claude process
        </div>
      </div>
      <p class="faq-p">M button LEDs light up when that track is muted. Everything syncs back to the UI in real time via SSE — no refresh needed.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">presets</div>
      <p class="faq-p">Save any board state as a named preset. Type a name in the PRESETS bar and hit Enter or SAVE. Click a preset chip to load it instantly — the board snaps to that state in real time.</p>
      <p class="faq-p">Presets are saved to <code style="background:#040608;padding:1px 4px;border-radius:2px;font-size:10px">~/.streamfader/presets/</code> as JSON files. They persist across restarts and sync with the CLI and MIDI bridge automatically.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">keyboard shortcuts</div>
      <div class="faq-track">
        <div class="faq-track-desc">
          <strong style="color:var(--text)">Space</strong> — Abort running Claude process<br>
          <strong style="color:var(--text)">1 / 2 / 3 / 4</strong> — Mute / unmute Track 1 – 4<br>
          <strong style="color:var(--text)">R</strong> — Reset all faders and knobs to 0.50<br>
          <strong style="color:var(--text)">T</strong> — Toggle light / dark theme<br>
          <strong style="color:var(--text)">?</strong> — Open this panel<br>
          <strong style="color:var(--text)">Esc</strong> — Close this panel
        </div>
      </div>
      <p class="faq-p" style="margin-top:8px">Shortcuts are suppressed when the preset name field is focused.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">practical combos</div>
      <div class="faq-track">
        <div class="faq-track-name">Analyze before touching code</div>
        <div class="faq-track-desc">T1: EXPLORE, Effort LOW, Thinking Time HIGH. T2: LIST. Mute T3 + T4. Ask anything. No files touched, full options shown.</div>
      </div>
      <div class="faq-track">
        <div class="faq-track-name">Fast surgical fix</div>
        <div class="faq-track-desc">T1: BUILD, Effort HIGH. T2: DECIDE, Confidence HIGH, Boldness LOW. T3: FILE. T4: DIRECT. One file, one fix, no commentary.</div>
      </div>
      <div class="faq-track">
        <div class="faq-track-name">Risky refactor</div>
        <div class="faq-track-desc">T1: BUILD, Thinking Time HIGH. T2: DECIDE, Boldness HIGH. T3: PROJECT, Context Size HIGH. T4: STUDIO. Full codebase access, bold changes, clean output.</div>
      </div>
      <p class="faq-p" style="opacity:.3;font-style:italic;margin-top:12px">Same task. Different state. That's the machine.</p>
    </div>

  </div>
</div>

<script>
const THUMB_H = 28;
const FADERS = {
  intensity: {fill:'ff-intensity', thumb:'fth-intensity', val:'fv-intensity', track:'ft-intensity'},
  certainty: {fill:'ff-certainty', thumb:'fth-certainty', val:'fv-certainty', track:'ft-certainty'},
  scope:     {fill:'ff-scope',     thumb:'fth-scope',     val:'fv-scope',     track:'ft-scope'},
  room:      {fill:'ff-room',      thumb:'fth-room',      val:'fv-room',      track:'ft-room'},
};
const KNOBS = {
  depth:     {dot:'kd-depth',     val:'kv-depth'},
  risk:      {dot:'kd-risk',      val:'kv-risk'},
  bandwidth: {dot:'kd-bandwidth', val:'kv-bandwidth'},
  decay:     {dot:'kd-decay',     val:'kv-decay'},
};
const FIELD_DEFAULTS = {};
const METERS   = ['intensity','depth','certainty','risk','scope','bandwidth','room','decay'];
const BADGE_C  = {EXPLORE:'#00A8A0', FIX:'#8B5CF6', BUILD:'#00C8C0'};
const PILL_C   = ['#00C8C0','#8B5CF6','#00A0A8','#6040C8'];

let isDragging  = false;
const activeTimers = {};
const prevVals  = {};  // tracks last seen value per field to detect real changes

const INFO = {
  intensity:  ['EFFORT',            'How hard Claude pushes. HIGH = minimal output, direct execution. LOW = verbose reasoning, exploratory tone.'],
  depth:      ['THINKING TIME',     'How deep Claude diagnoses. HIGH = full root cause analysis. LOW = surface-level reasoning only.'],
  certainty:  ['CONFIDENCE',        'Commitment to one answer. HIGH = single solution, no alternatives. LOW = 2-3 approaches with pros/cons.'],
  risk:       ['BOLDNESS',          'How bold the changes. HIGH = best solution even if significant refactor. LOW = stay close to existing patterns.'],
  scope:      ['ZOOM LEVEL',        'How wide Claude looks. HIGH = full codebase. MED = module + dependencies. LOW = this file only.'],
  bandwidth:  ['CONTEXT SIZE',      'Adjacent context. HIGH = pull in everything related. LOW = surgical, touch nothing adjacent.'],
  room:       ['VERBOSITY',         'Space in output. WET = thinks out loud, breathing room. DRY = close-mic\'d, just the result.'],
  decay:      ['MEMORY PERSISTENCE','Language density. LONG = ideas echo and build. SHORT = tight and compressed, every word counts.'],
  EXPLORE:    ['EXPLORE',    'Analysis only. No code changes. Claude maps the problem and ends with a single decision point.'],
  FIX:        ['FIX',        'One root cause, one fix. No secondary issues touched. Scalpel, not a sledgehammer.'],
  BUILD:      ['BUILD',      'One atomic change implemented. No refactoring unless explicitly required.'],
  LIST:       ['LIST',       'Present alternatives only. Nothing gets implemented. Use before committing to a direction.'],
  GUIDE:      ['GUIDE',      'Recommend then implement. Claude gives brief reasoning for its choice, then acts.'],
  DECIDE:     ['DECIDE',     'Pick one and ship it. Zero explanation of alternatives. Maximum commitment.'],
  FILE:       ['FILE',       'This file only. No cross-module context pulled. Strictest local scope.'],
  MODULE:     ['MODULE',     'This module and its direct dependencies. Selective, shaped context.'],
  PROJECT:    ['PROJECT',    'Full codebase in scope. Global context available. Pair with high SCOPE.'],
  DIRECT:     ['DIRECT',     'Dead room. Output only — zero commentary or preamble. Just the result.'],
  STUDIO:     ['STUDIO',     'Professional and measured. Clean response with minimal framing. Default voice.'],
  OPEN:       ['OPEN',       'Collaborative. Claude thinks out loud with you, full reasoning visible.'],
  MODE:       ['MODE',       'What Claude is allowed to do. EXPLORE = read only. FIX = one bug. BUILD = one change.'],
  STANCE:     ['STANCE',     'How Claude presents its work. LIST = options only. GUIDE = recommends + acts. DECIDE = just ships.'],
  FILTER:     ['FILTER',     'Context boundary. FILE = this file. MODULE = this module. PROJECT = full codebase.'],
  VOICE:      ['VOICE',      'Output style. DIRECT = no commentary. STUDIO = clean + measured. OPEN = thinks out loud.'],
};

function showInfo(key) {}
function clearInfo()   {}
let lastState = {};

function buildPromptPreview(s) {
  const i=s.intensity??0.5, d=s.depth??0.5, c=s.certainty??0.5, r=s.risk??0.5;
  const sc=s.scope??0.5, bw=s.bandwidth??0.5, ro=s.room??0.3, dc=s.decay??0.3;
  const t1=s.t1_on!==false, t2=s.t2_on!==false, t3=s.t3_on!==false, t4=s.t4_on!==false;
  const lines = [];
  const kv = (k,v) => `<span class="pp-key">${k}:</span> <span class="pp-val">${v}</span>`;
  const off = (t) => `<span class="pp-key" style="opacity:.3">${t}:</span> <span class="pp-val" style="opacity:.3;font-style:italic">muted — excluded from prompt</span>`;
  if (!t1) { lines.push(off('T1 MODE')); }
  else {
    if (s.mode==='EXPLORE') lines.push(kv('MODE','EXPLORE — analysis only, no code changes'));
    else if (s.mode==='FIX') lines.push(kv('MODE','FIX — one root cause, one fix'));
    else if (s.mode==='BUILD') lines.push(kv('MODE','BUILD — one atomic change'));
    else lines.push(kv('MODE','— none selected'));
    lines.push(kv('INTENSITY', i>=0.7?'HIGH — minimal output, direct execution':i>=0.4?'MED — concise reasoning':'LOW — verbose reasoning, exploratory'));
    lines.push(kv('DEPTH',     d>=0.7?'HIGH — deeper diagnostic reasoning':d>=0.4?'MED — moderate analysis':'LOW — surface-level only'));
  }
  if (!t2) { lines.push(off('T2 CONF')); }
  else {
    lines.push(kv('CERTAINTY', c>=0.7?'HIGH — one solution, no alternatives':c>=0.4?'MED — recommendation + brief reasoning':'LOW — show 2-3 approaches, do not pick'));
    lines.push(kv('RISK',      r>=0.7?'HIGH — best solution, even if significant changes':r>=0.4?'MED — prefer existing patterns where reasonable':'LOW — stay close to existing, minimal disruption'));
    if (s.stance==='LIST')        lines.push(kv('STANCE','LIST — present alternatives only, do not implement'));
    else if (s.stance==='DECIDE') lines.push(kv('STANCE','DECIDE — pick one, implement it, zero explanation'));
    else if (s.stance==='GUIDE')  lines.push(kv('STANCE','GUIDE — recommend with brief reasoning, then implement'));
    else lines.push(kv('STANCE','— none selected'));
  }
  if (!t3) { lines.push(off('T3 SCOPE')); }
  else {
    lines.push(kv('SCOPE',     sc>=0.7?'WIDE — full codebase':sc>=0.4?'MED — module + dependencies':'NARROW — immediate file or function only'));
    lines.push(kv('BANDWIDTH', bw>=0.7?'WIDE — pull in adjacent concerns freely':bw>=0.4?'MED — related things welcome if relevant':'NARROW — surgical, touch nothing adjacent'));
    if (s.filter==='FILE')         lines.push(kv('FILTER','FILE — this file only, no cross-module context'));
    else if (s.filter==='PROJECT') lines.push(kv('FILTER','PROJECT — full project scope, global context'));
    else if (s.filter==='MODULE')  lines.push(kv('FILTER','MODULE — shaped band around the module'));
    else lines.push(kv('FILTER','— none selected'));
  }
  if (!t4) { lines.push(off('T4 VOICE')); }
  else {
    lines.push(kv('ROOM',  ro>=0.7?'WET — open space, think out loud':ro>=0.4?'MED — some space, conversational':'DRY — close-mic\'d, no space, just output'));
    lines.push(kv('DECAY', dc>=0.7?'LONG — ideas echo and build':dc>=0.4?'MED — moderate density':'SHORT — tight, every word counts'));
    if (s.voice==='DIRECT')      lines.push(kv('VOICE','DIRECT — dead room, output only, zero preamble'));
    else if (s.voice==='OPEN')   lines.push(kv('VOICE','OPEN — collaborative, thinks out loud'));
    else if (s.voice==='STUDIO') lines.push(kv('VOICE','STUDIO — professional, measured, clean'));
    else lines.push(kv('VOICE','— none selected'));
  }
  return lines.join('\n');
}

/* build tick marks for a fader */
function buildTicks(containerId) {
  const c = document.getElementById(containerId); if (!c) return;
  const ticks = [
    {v:1.0, lbl:'100', major:true},
    {v:0.9, lbl:'',    major:false},
    {v:0.8, lbl:'80',  major:false},
    {v:0.7, lbl:'',    major:false},
    {v:0.6, lbl:'60',  major:false},
    {v:0.5, lbl:'50',  major:true},
    {v:0.4, lbl:'40',  major:false},
    {v:0.3, lbl:'',    major:false},
    {v:0.2, lbl:'20',  major:false},
    {v:0.1, lbl:'',    major:false},
    {v:0.0, lbl:'0',   major:true},
  ];
  c.innerHTML = ticks.map(t =>
    `<div class="tick">
      <div class="tick-line${t.major?' major':''}" style="width:${t.major?6:4}px"></div>
    </div>`
  ).join('');
}

function getRange(trackId) {
  const el = document.getElementById(trackId);
  return el ? Math.max(20, el.offsetHeight - THUMB_H) : 80;
}
function setFader(field, v) {
  const f = FADERS[field]; if (!f) return;
  const r = getRange(f.track);
  document.getElementById(f.fill).style.height  = (v*100)+'%';
  document.getElementById(f.thumb).style.bottom = (v*r)+'px';
  document.getElementById(f.val).textContent    = v.toFixed(2);
  // Buzz: only fire glow when this specific fader's value actually changed
  const prev = prevVals['f_'+field];
  prevVals['f_'+field] = v;
  if (prev !== undefined && Math.abs(v - prev) > 0.001) {
    const trackEl = document.getElementById(f.track);
    if (trackEl && !trackEl.classList.contains('dragging')) {
      trackEl.classList.add('value-active');
      clearTimeout(activeTimers['f_'+field]);
      activeTimers['f_'+field] = setTimeout(() => trackEl.classList.remove('value-active'), 460);
    }
  }
}
function setKnob(field, v) {
  const k = KNOBS[field]; if (!k) return;
  const dot = document.getElementById(k.dot);
  if (dot) dot.style.transform = `translateX(-50%) rotate(${-135+v*270}deg)`;
  const val = document.getElementById(k.val);
  if (val) val.textContent = v.toFixed(2);
  const knobEl = document.getElementById('knob-'+field);
  if (knobEl) {
    // Live conic arc: sweep from 225° to current position
    const s = 225, e = s + v * 270;
    const dark = '#0A1620', lit = 'var(--accent)';
    knobEl.style.background = e <= 360
      ? `conic-gradient(${dark} 0deg ${s}deg,${lit} ${s}deg ${e}deg,${dark} ${e}deg 360deg)`
      : `conic-gradient(${lit} 0deg ${e-360}deg,${dark} ${e-360}deg ${s}deg,${lit} ${s}deg 360deg)`;
    // Outer glow scales continuously with value — knob "glows hotter" as you push it up
    const gi = (0.06 + v * 0.24).toFixed(3);
    const gs = Math.round(16 + v * 26);
    knobEl.style.boxShadow = `0 4px 16px rgba(0,0,0,1),0 0 0 1px rgba(0,0,0,.95),0 0 ${gs}px rgba(0,196,232,${gi}),0 0 ${gs*2}px rgba(0,196,232,${(parseFloat(gi)*0.42).toFixed(3)})`;
    // Buzz: only fire indicator flash when this knob's value actually changed
    const kprev = prevVals['k_'+field];
    prevVals['k_'+field] = v;
    if (kprev !== undefined && Math.abs(v - kprev) > 0.001) {
      knobEl.classList.add('value-active');
      clearTimeout(activeTimers['k_'+field]);
      activeTimers['k_'+field] = setTimeout(() => knobEl.classList.remove('value-active'), 460);
    }
  }
}
function setMeter(field, v) {
  const f   = document.getElementById('m-'+field);
  const val = document.getElementById('mv-'+field);
  const lvl = document.getElementById('ml-'+field);
  if (f)   f.style.width = (v*100)+'%';
  if (val) val.textContent = v.toFixed(2);
  if (lvl) {
    lvl.className = 'meter-lvl '+(v>=0.7?'lvl-high':v>=0.4?'lvl-med':'lvl-low');
    lvl.textContent = v>=0.7?'HIGH':v>=0.4?'MED':'LOW';
  }
}
function setButtons(field, active) {
  document.querySelectorAll(`.ch-btn[data-field="${field}"]`).forEach(b =>
    b.classList.toggle('active', b.dataset.val === active));
}
function setPill(id, text, color) {
  const el = document.getElementById(id); if (!el) return;
  el.textContent = text;
  el.style.background = color+'28';
  el.style.color = color;
}
function applyState(s) {
  lastState = s;
  Object.keys(FADERS).forEach(f => {
    const v = s[f] ?? (FIELD_DEFAULTS[f] ?? 0.5);
    if (checkPickup(f, v)) return; // blocked — physical hasn't reached software position yet
    setFader(f, v);
  });
  Object.keys(KNOBS).forEach(f  => setKnob(f,  s[f] ?? (FIELD_DEFAULTS[f] ?? 0.5)));
  METERS.forEach(f => setMeter(f, s[f] ?? (FIELD_DEFAULTS[f] ?? 0.5)));
  setButtons('mode',   s.mode);
  setButtons('stance', s.stance);
  setButtons('filter', s.filter);
  setButtons('voice',  s.voice);
  const parts = [];
  if (s.mode)   parts.push(`Mode: ${s.mode}`);
  if (s.stance) parts.push(`Stance: ${s.stance}`);
  if (s.filter) parts.push(`Filter: ${s.filter}`);
  if (s.voice)  parts.push(`Voice: ${s.voice}`);
  parts.push(`Effort: ${(s.intensity??0.5).toFixed(2)}`);
  parts.push(`Confidence: ${(s.certainty??0.5).toFixed(2)}`);
  parts.push(`Scope: ${(s.scope??0.5).toFixed(2)}`);
  parts.push(`Verbosity: ${(s.room??0.5).toFixed(2)}`);
  const settingsEl = document.getElementById('hdr-settings');
  if (settingsEl) settingsEl.textContent = parts.length ? parts.join('  ·  ') : 'All defaults — baseline';
  setPill('pill-mode',   s.mode  ||'—', PILL_C[0]);
  setPill('pill-stance', s.stance||'—', PILL_C[1]);
  setPill('pill-filter', s.filter||'—', PILL_C[2]);
  setPill('pill-voice',  s.voice ||'—', PILL_C[3]);
  // Apply per-track mute state
  ['t1','t2','t3','t4'].forEach(t => {
    const on = s[t+'_on'] !== false;
    const ch = document.querySelector('.ch.'+t);
    if (ch) ch.classList.toggle('ch-off', !on);
    const pwr = document.getElementById('cpwr-'+t);
    if (pwr) pwr.classList.toggle('off', !on);
    const mbtn = document.getElementById('mbtn-'+t);
    if (mbtn) mbtn.classList.toggle('active', !on);
  });
  const pp = document.getElementById('prompt-preview'); if (pp) pp.innerHTML = buildPromptPreview(s);
}

async function set(field, value) {
  await fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({[field]:value})});
}
function toggleBtn(field, val) {
  set(field, lastState[field] === val ? '' : val);
}
function toggleTrack(t) {
  const key = t + '_on';
  set(key, lastState[key] === false ? true : false);
}

const _tok = localStorage.getItem('sb-access-token') || '';
const es = new EventSource('/stream' + (_tok ? '?token=' + encodeURIComponent(_tok) : ''));
es.onmessage = e => { if (!isDragging) applyState(JSON.parse(e.data)); };
es.onerror   = () => { document.getElementById('hdr-vals').textContent = 'reconnecting…'; };

function bindDrag(el, getV, onMove, onDrop) {
  el.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    try { el.setPointerCapture(e.pointerId); } catch(_) {}
    isDragging = true;
    const track = el.classList.contains('fader-track') ? el : el.closest('.fader-track');
    if (track) track.classList.add('dragging');
    const sy = e.clientY, sv = getV();
    function move(e2) { onMove(sy, e2.clientY, sv); }
    function up() {
      isDragging = false;
      if (track) track.classList.remove('dragging');
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      onDrop();
    }
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  });
}

// ── PHYSICS CONSTANTS ────────────────────────────────────────────
const FADER_INERTIA = 0.22;  // smoothing blend during drag
const FADER_DAMPING = 0.72;  // velocity decay per frame after release
const KNOB_INERTIA  = 0.15;
const KNOB_DAMPING  = 0.80;
const FINE_MULT     = 0.25;  // Shift key precision multiplier
const KNOB_SENS     = 0.90;  // knob travel sensitivity
const KNOB_DETENT   = 0.022; // center snap zone (±2.2% around 0.5)

/* fader physics drag */
Object.entries(FADERS).forEach(([field, ids]) => {
  const trackEl = document.getElementById(ids.track);
  const thumbEl = document.getElementById(ids.thumb);
  if (!trackEl || !thumbEl) return;
  const getV = () => parseFloat(document.getElementById(ids.val).textContent);
  let vel = 0, rafId = null;

  function coast() {
    vel *= FADER_DAMPING;
    const cur = getV(), next = Math.max(0, Math.min(1, cur + vel));
    if (Math.abs(vel) < 0.0003 || next === cur) { set(field, Math.round(next*1000)/1000); return; }
    setFader(field, next);
    rafId = requestAnimationFrame(coast);
  }

  function onDown(e) {
    const cap = e.currentTarget;
    e.preventDefault(); e.stopPropagation();
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    try { cap.setPointerCapture(e.pointerId); } catch(_) {}
    isDragging = true;
    trackEl.classList.add('dragging');
    let prevY = e.clientY, prevT = performance.now();
    vel = 0;
    function onMove(ev) {
      const now = performance.now(), dt = Math.max(8, now - prevT);
      const dy  = ev.clientY - prevY;
      const r   = getRange(ids.track);
      const fine = ev.shiftKey ? FINE_MULT : 1;
      const raw  = dy / r * fine;
      // Exponential ease-out: precise when slow, accelerates when fast
      const sign   = raw < 0 ? -1 : 1;
      const curved = sign * Math.pow(Math.abs(raw), 0.78);
      vel = vel * FADER_INERTIA + (dy / r * fine) * (16 / dt) * (1 - FADER_INERTIA);
      setFader(field, Math.max(0, Math.min(1, getV() + curved)));
      prevY = ev.clientY; prevT = now;
    }
    function onUp() {
      isDragging = false;
      trackEl.classList.remove('dragging');
      cap.removeEventListener('pointermove', onMove);
      cap.removeEventListener('pointerup',   onUp);
      cap.removeEventListener('pointercancel', onUp);
      if (Math.abs(vel) > 0.005) rafId = requestAnimationFrame(coast);
      else set(field, Math.round(getV()*1000)/1000);
    }
    cap.addEventListener('pointermove', onMove);
    cap.addEventListener('pointerup',   onUp);
    cap.addEventListener('pointercancel', onUp);
  }

  const reset = () => {
    if (rafId) cancelAnimationFrame(rafId);
    vel = 0;
    const d = FIELD_DEFAULTS[field]??0.5;
    setFader(field, d); set(field, d);
  };
  trackEl.addEventListener('pointerdown', onDown);
  thumbEl.addEventListener('pointerdown', onDown);
  trackEl.addEventListener('dblclick', reset);
  thumbEl.addEventListener('dblclick', reset);
});

/* knob physics drag */
Object.entries(KNOBS).forEach(([field, ids]) => {
  const knobEl = document.getElementById('knob-'+field);
  if (!knobEl) return;
  const getV = () => parseFloat(document.getElementById(ids.val).textContent);
  let vel = 0, rafId = null;

  function coast() {
    vel *= KNOB_DAMPING;
    const cur = getV(), next = Math.max(0, Math.min(1, cur + vel));
    if (Math.abs(vel) < 0.0002 || next === cur) { set(field, Math.round(next*1000)/1000); return; }
    setKnob(field, next);
    rafId = requestAnimationFrame(coast);
  }

  function onDown(e) {
    e.preventDefault(); e.stopPropagation();
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    try { knobEl.setPointerCapture(e.pointerId); } catch(_) {}
    isDragging = true;
    let prevY = e.clientY, prevT = performance.now();
    vel = 0;
    function onMove(ev) {
      const now  = performance.now(), dt = Math.max(8, now - prevT);
      const dy   = ev.clientY - prevY;
      const fine = ev.shiftKey ? FINE_MULT * 0.8 : 1;
      const delta = (-dy / 120) * KNOB_SENS * fine;
      vel = vel * KNOB_INERTIA + delta * (16 / dt) * (1 - KNOB_INERTIA);
      let nv = Math.max(0, Math.min(1, getV() + delta));
      // Center detent: visual snap to 0.5 (hold Shift to bypass)
      if (!ev.shiftKey && Math.abs(nv - 0.5) < KNOB_DETENT) {
        nv = 0.5; vel = 0;
        knobEl.classList.remove('at-detent');
        void knobEl.offsetWidth; // force reflow so re-adding retriggers animation
        knobEl.classList.add('at-detent');
        setTimeout(() => knobEl.classList.remove('at-detent'), 300);
      }
      setKnob(field, nv);
      prevY = ev.clientY; prevT = now;
    }
    function onUp() {
      isDragging = false;
      knobEl.removeEventListener('pointermove', onMove);
      knobEl.removeEventListener('pointerup',   onUp);
      knobEl.removeEventListener('pointercancel', onUp);
      if (Math.abs(vel) > 0.002) rafId = requestAnimationFrame(coast);
      else set(field, Math.round(getV()*1000)/1000);
    }
    knobEl.addEventListener('pointermove', onMove);
    knobEl.addEventListener('pointerup',   onUp);
    knobEl.addEventListener('pointercancel', onUp);
  }

  const reset = () => {
    if (rafId) cancelAnimationFrame(rafId);
    vel = 0;
    const d = FIELD_DEFAULTS[field]??0.5;
    setKnob(field, d); set(field, d);
  };
  knobEl.addEventListener('pointerdown', onDown);
  knobEl.addEventListener('dblclick', reset);
});

/* info box hover — attach to all labeled controls */
document.querySelectorAll('.fader-lbl,.knob-lbl').forEach(el => {
  const field = el.textContent.trim().toLowerCase();
  el.addEventListener('mouseenter', () => showInfo(field));
  el.addEventListener('mouseleave', clearInfo);
});
document.querySelectorAll('.ch-btn').forEach(el => {
  const key = el.dataset.val;
  el.addEventListener('mouseenter', () => showInfo(key));
  el.addEventListener('mouseleave', clearInfo);
});
document.querySelectorAll('.pill-lbl').forEach(el => {
  const key = el.textContent.trim();
  el.addEventListener('mouseenter', () => showInfo(key));
  el.addEventListener('mouseleave', clearInfo);
});
document.querySelectorAll('.meter-lbl').forEach(el => {
  const field = el.textContent.trim().toLowerCase();
  el.addEventListener('mouseenter', () => showInfo(field));
  el.addEventListener('mouseleave', clearInfo);
});
document.querySelectorAll('.fader-track,.fader-thumb,.knob').forEach(el => {
  el.addEventListener('mouseenter', () => {
    const field = el.id?.replace('knob-','').replace('ft-','').replace('fth-','');
    if (field) showInfo(field);
  });
  el.addEventListener('mouseleave', clearInfo);
});

/* build tick marks after layout */
['intensity','certainty','scope','room'].forEach(f => buildTicks('ticks-'+f));

// ── RUN LOG ───────────────────────────────────────────────────────
const history = [];
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function saveHistory()  { try { localStorage.setItem('ctrl_log', JSON.stringify(history.slice(0,30))); } catch(_) {} }
function loadHistory()  { try { const s=localStorage.getItem('ctrl_log'); if(s){JSON.parse(s).forEach(r=>history.push(r)); renderHistory();} } catch(_) {} }
function clearHistory() {
  if (!confirm('Clear the run log?')) return;
  history.length = 0;
  try { localStorage.removeItem('ctrl_log'); } catch(_) {}
  renderHistory();
}
function toggleCard(i) { const el=document.getElementById('hc-'+i); if(el) el.classList.toggle('open'); }
function copyResp(i, e) {
  e.stopPropagation();
  const r = history[i]; if (!r) return;
  navigator.clipboard.writeText(
    'TASK: '+r.task+'\n\nSTATE:\nMODE='+r.mode+' STANCE='+r.stance+' FILTER='+r.filter+' VOICE='+r.voice+
    '\nI='+r.intensity+' D='+r.depth+' C='+r.certainty+' R='+r.risk+
    ' SCOPE='+r.scope+' BW='+r.bandwidth+' ROOM='+r.room+' DECAY='+r.decay+
    '\n\nRESPONSE:\n'+r.resp
  ).then(() => { const btn=e.target; btn.textContent='Copied!'; setTimeout(()=>btn.textContent='Copy',1500); });
}
function renderHistory() {
  const el = document.getElementById('history');
  if (!history.length) { el.innerHTML='<div class="history-empty">no runs yet</div>'; return; }
  el.innerHTML = history.slice(0,5).map((r,i) => {
    const modeTag = r.mode ? `<span class="hc-mode">${esc(r.mode)}</span>` : '';
    const stanceTag = r.stance ? `<span class="hc-mode" style="background:transparent;color:var(--text3);border-color:var(--border2)">${esc(r.stance)}</span>` : '';
    return `
    <div class="hc" id="hc-${i}" onclick="toggleCard(${i})">
      <div class="hc-top">
        <span class="hc-time">${esc(r.t)}</span>
        ${modeTag}${stanceTag}
        <span class="hc-chevron">▼</span>
      </div>
      <div class="hc-task">${esc(r.task)}</div>
      <div class="hc-body">
        <div class="hc-state-grid">
          <div class="hc-si"><div class="hc-si-lbl">Mode</div><div class="hc-si-val">${esc(r.mode)||'—'}</div></div>
          <div class="hc-si"><div class="hc-si-lbl">Stance</div><div class="hc-si-val">${esc(r.stance)||'—'}</div></div>
          <div class="hc-si"><div class="hc-si-lbl">Filter</div><div class="hc-si-val">${esc(r.filter)||'—'}</div></div>
          <div class="hc-si"><div class="hc-si-lbl">Voice</div><div class="hc-si-val">${esc(r.voice)||'—'}</div></div>
          <div class="hc-si"><div class="hc-si-lbl">Effort</div><div class="hc-si-val">${r.intensity}</div></div>
          <div class="hc-si"><div class="hc-si-lbl">Think Time</div><div class="hc-si-val">${r.depth}</div></div>
          <div class="hc-si"><div class="hc-si-lbl">Confidence</div><div class="hc-si-val">${r.certainty}</div></div>
          <div class="hc-si"><div class="hc-si-lbl">Boldness</div><div class="hc-si-val">${r.risk}</div></div>
        </div>
        <div class="hc-full">${esc(r.resp)}</div>
        <div class="hc-actions"><button class="hc-copy" onclick="copyResp(${i},event)">Copy</button></div>
      </div>
    </div>`;
  }).join('');
}

// ── PRESETS ───────────────────────────────────────────────────────
let presets = [];
async function loadPresets() {
  try {
    const r = await fetch('/presets');
    presets = await r.json();
    renderPresets();
  } catch(e) {}
}
function renderPresets() {
  const el = document.getElementById('preset-list');
  if (!presets.length) { el.innerHTML = '<span class="preset-empty">no presets saved</span>'; return; }
  el.innerHTML = presets.map(p => `<div class="preset-item">
    <span class="preset-name" onclick="applyPreset('${esc(p.name)}')">${esc(p.name)}</span>
    <button class="preset-load" onclick="applyPreset('${esc(p.name)}')">LOAD</button>
    <button class="preset-del" onclick="deletePreset('${esc(p.name)}')" title="Delete">×</button>
  </div>`).join('');
}
async function savePreset() {
  const name = document.getElementById('preset-input').value.trim();
  if (!name) return;
  const r = await fetch('/presets/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  const d = await r.json();
  document.getElementById('preset-input').value = '';
  loadPresets();
}
async function abortRun() {
  const btn = document.getElementById('abort-btn');
  btn.classList.add('firing');
  btn.textContent = '■ ABORTING…';
  await fetch('/abort', {method:'POST'});
  setTimeout(() => {
    btn.classList.remove('firing');
    btn.innerHTML = '&#9632; ABORT';
  }, 600);
}
async function applyPreset(name) {
  await fetch('/presets/load', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  // SSE stream will push the updated state to the UI automatically
}
async function deletePreset(name) {
  await fetch('/presets/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  loadPresets();
}
document.getElementById('preset-input').addEventListener('keydown', e => { if (e.key === 'Enter') savePreset(); });
loadPresets();

// ── COPY ──────────────────────────────────────────────────────────
function copyResp() {
  const txt = document.getElementById('resp-box').textContent;
  if (!txt) return;
  navigator.clipboard.writeText(txt).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.classList.add('copied'); btn.textContent = '✓';
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = '⎘'; }, 1500);
  });
}

// ── PREVIEW RUN ───────────────────────────────────────────────────
const taskInput = document.getElementById('task-input');
const runBtn    = document.getElementById('run-btn');
const respWrap  = document.getElementById('resp-wrap');
const respBox   = document.getElementById('resp-box');

async function runTask() {
  const task = taskInput.value.trim();
  if (!task || runBtn.disabled) return;
  runBtn.disabled = true; runBtn.textContent = '···';
  respBox.textContent = ''; respWrap.classList.add('open'); respBox.scrollTop = 0;
  let full = '';
  const snap = {
    mode:      lastState.mode      || '—',
    stance:    lastState.stance    || '—',
    filter:    lastState.filter    || '—',
    voice:     lastState.voice     || '—',
    intensity: (lastState.intensity ?? 0).toFixed(2),
    depth:     (lastState.depth     ?? 0).toFixed(2),
    certainty: (lastState.certainty ?? 0).toFixed(2),
    risk:      (lastState.risk      ?? 0).toFixed(2),
    scope:     (lastState.scope     ?? 0).toFixed(2),
    bandwidth: (lastState.bandwidth ?? 0).toFixed(2),
    room:      (lastState.room      ?? 0).toFixed(2),
    decay:     (lastState.decay     ?? 0).toFixed(2),
  };
  try {
    const res = await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task})});
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const {done,value} = await reader.read(); if (done) break;
      buf += dec.decode(value,{stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const d = JSON.parse(line.slice(6));
        if (d.text)  { full+=d.text; respBox.textContent=full; respBox.scrollTop=respBox.scrollHeight; }
        if (d.error) { respBox.innerHTML='<span class="err">'+esc(d.error)+'</span>'; }
        if (d.done)  {
          runBtn.disabled=false; runBtn.textContent='RUN';
          if (full) {
            history.unshift({t:new Date().toLocaleTimeString(), task, resp:full, ...snap});
            if (history.length>30) history.pop();
            saveHistory();
            renderHistory();
          }
        }
      }
    }
  } catch(e) {
    respBox.innerHTML='<span class="err">'+esc(e.message)+'</span>';
  } finally { runBtn.disabled=false; runBtn.textContent='RUN'; }
}
runBtn.addEventListener('click', runTask);
taskInput.addEventListener('keydown', e => { if ((e.metaKey||e.ctrlKey)&&e.key==='Enter') runTask(); });
loadHistory();

// ── CTRL RUN LAUNCHER ────────────────────────────────────────────
const launchInput   = document.getElementById('launch-input');
const launchBtn     = document.getElementById('launch-btn');
const launchRunning = document.getElementById('launch-running');

const TOOL_ICONS = {read_file:'📖',write_file:'✏️',list_directory:'📂',run_command:'⚡'};

async function launchTask() {
  const task = launchInput.value.trim();
  if (!task || launchBtn.disabled) return;
  launchBtn.disabled = true; launchBtn.textContent = '···';
  launchRunning.classList.add('show');
  respBox.textContent = ''; respWrap.classList.add('open'); respBox.scrollTop = 0;
  const snap = {
    mode:      lastState.mode      || '—',
    stance:    lastState.stance    || '—',
    filter:    lastState.filter    || '—',
    voice:     lastState.voice     || '—',
    intensity: (lastState.intensity ?? 0).toFixed(2),
    depth:     (lastState.depth     ?? 0).toFixed(2),
    certainty: (lastState.certainty ?? 0).toFixed(2),
    risk:      (lastState.risk      ?? 0).toFixed(2),
    scope:     (lastState.scope     ?? 0).toFixed(2),
    bandwidth: (lastState.bandwidth ?? 0).toFixed(2),
    room:      (lastState.room      ?? 0).toFixed(2),
    decay:     (lastState.decay     ?? 0).toFixed(2),
  };
  let textOutput = ''; let fullLog = '';
  const finish = () => {
    launchBtn.disabled = false; launchBtn.textContent = 'LAUNCH';
    launchRunning.classList.remove('show');
  };
  try {
    const res = await fetch('/exec', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task})});
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const {done: rd, value} = await reader.read(); if (rd) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const d = JSON.parse(line.slice(6));
        if (d.text) {
          textOutput += d.text;
          fullLog    += d.text;
          respBox.textContent = fullLog; respBox.scrollTop = respBox.scrollHeight;
        }
        if (d.tool) {
          const icon  = TOOL_ICONS[d.tool] || '🔧';
          const label = d.tool === 'read_file'  ? d.input.path  :
                        d.tool === 'write_file' ? d.input.path  :
                        d.tool === 'list_directory' ? d.input.path :
                        d.input.command || '';
          const tag = `\n[${icon} ${d.tool}: ${label}]\n`;
          fullLog += tag;
          respBox.textContent = fullLog; respBox.scrollTop = respBox.scrollHeight;
        }
        if (d.error) {
          fullLog += `\n[error: ${d.error}]`;
          respBox.innerHTML = '<span class="err">'+esc(d.error)+'</span>';
          finish();
        }
        if (d.done) {
          finish();
          if (fullLog) {
            history.unshift({t:new Date().toLocaleTimeString(), task, resp:fullLog, ...snap});
            if (history.length > 30) history.pop();
            saveHistory(); renderHistory();
            launchInput.value = '';
          }
        }
      }
    }
  } catch(e) { finish(); }
}
launchBtn.addEventListener('click', launchTask);
launchInput.addEventListener('keydown', e => { if (e.key === 'Enter') launchTask(); });

// ── PICKUP MODE (soft takeover) ───────────────────────────────────
// pickupMode[field] = { targetVal, physicalVal, direction }
const pickupMode = {};
const PICKUP_TARGET = 0.5;
const PICKUP_THRESHOLD = 0.025; // within 2.5% = locked in

function setPickupGhost(field, physVal) {
  const trackEl = document.getElementById('ft-'+field);
  const ghostEl = document.getElementById('fg-'+field);
  const labelEl = document.getElementById('pl-'+field);
  if (!trackEl || !ghostEl) return;
  const r = getRange('ft-'+field);
  ghostEl.style.bottom = (physVal * r) + 'px';
  ghostEl.classList.add('active');
  trackEl.classList.add('pickup');
  if (labelEl) labelEl.classList.add('active');
}

function clearPickup(field) {
  const trackEl = document.getElementById('ft-'+field);
  const ghostEl = document.getElementById('fg-'+field);
  const labelEl = document.getElementById('pl-'+field);
  if (ghostEl) ghostEl.classList.remove('active');
  if (trackEl) trackEl.classList.remove('pickup');
  if (labelEl) labelEl.classList.remove('active');
  delete pickupMode[field];
}

function checkPickup(field, incomingVal) {
  const pm = pickupMode[field];
  if (!pm) return false; // not in pickup mode
  // Update ghost to show physical position
  setPickupGhost(field, incomingVal);
  // Check if physical has crossed or reached target
  const crossed = pm.direction === 'down'
    ? incomingVal <= pm.targetVal + PICKUP_THRESHOLD
    : incomingVal >= pm.targetVal - PICKUP_THRESHOLD;
  if (crossed) {
    clearPickup(field);
    return false; // allow value through — it's now in sync
  }
  return true; // still in pickup, block this value
}

// ── RESET TO DEFAULTS ─────────────────────────────────────────────
async function resetDefaults() {
  const TARGET = PICKUP_TARGET;
  // Capture physical positions before reset & set up pickup mode
  Object.keys(FADERS).forEach(field => {
    const curVal = parseFloat(document.getElementById(FADERS[field].val).textContent) || 0.5;
    if (Math.abs(curVal - TARGET) > PICKUP_THRESHOLD) {
      pickupMode[field] = {
        targetVal: TARGET,
        physicalVal: curVal,
        direction: curVal > TARGET ? 'down' : 'up'
      };
      setPickupGhost(field, curVal);
    }
  });
  // Reset software state immediately
  const defaults = {
    intensity:TARGET, depth:TARGET, certainty:TARGET, risk:TARGET,
    scope:TARGET, bandwidth:TARGET, room:TARGET, decay:TARGET,
    mode:'', stance:'', filter:'', voice:'',
    t1_on:true, t2_on:true, t3_on:true, t4_on:true
  };
  await fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(defaults)});
  // Snap software faders to target immediately
  Object.keys(FADERS).forEach(field => {
    if (pickupMode[field]) setFader(field, TARGET);
  });
}

// ── FAQ ───────────────────────────────────────────────────────────
function openFaq()  { document.getElementById('faq-overlay').classList.add('open'); document.getElementById('faq-panel').classList.add('open'); }
function closeFaq() { document.getElementById('faq-overlay').classList.remove('open'); document.getElementById('faq-panel').classList.remove('open'); }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeFaq(); return; }
  // Suppress shortcuts when typing in an input
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  switch (e.key) {
    case ' ':
      e.preventDefault();
      abortRun();
      break;
    case '1': e.preventDefault(); toggleTrack('t1'); break;
    case '2': e.preventDefault(); toggleTrack('t2'); break;
    case '3': e.preventDefault(); toggleTrack('t3'); break;
    case '4': e.preventDefault(); toggleTrack('t4'); break;
    case 'r': case 'R': resetDefaults(); break;
    case 't': case 'T': toggleTheme(); break;
    case '?': openFaq(); break;
  }
});

// ── Auth ──────────────────────────────────────────────────
const SB_URL = '""" + SUPABASE_URL + """';
const SB_KEY = '""" + SUPABASE_ANON + """';

function getToken() { return localStorage.getItem('sb-access-token') || ''; }

function logout() {
  localStorage.removeItem('sb-access-token');
  localStorage.removeItem('sb-refresh-token');
  window.location.href = '/login';
}

(async function initAuth() {
  if (!SB_URL) return; // local dev, no auth
  const token = getToken();
  if (!token) { window.location.href = '/login'; return; }
  try {
    const res = await fetch(SB_URL + '/auth/v1/user', {
      headers: { 'Authorization': 'Bearer ' + token, 'apikey': SB_KEY }
    });
    if (!res.ok) { window.location.href = '/login'; return; }
    const user = await res.json();
    const emailEl = document.getElementById('user-email');
    const logoutEl = document.getElementById('logout-btn');
    if (emailEl) emailEl.textContent = user.email || '';
    if (logoutEl) logoutEl.style.display = '';
  } catch(e) { /* network error — allow through in dev */ }
})();

// Patch fetch to include auth token on local API calls
const _origFetch = window.fetch.bind(window);
window.fetch = function(url, opts) {
  const token = getToken();
  if (token && typeof url === 'string' && (url.startsWith('/') || url.startsWith(window.location.origin))) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers, { 'Authorization': 'Bearer ' + token });
  }
  return _origFetch(url, opts);
};

// ── Theme toggle ──────────────────────────────────────────
(function initTheme() {
  const saved = localStorage.getItem('gain_theme');
  if (saved === 'light') document.body.classList.add('light');
})();
function toggleTheme() {
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('gain_theme', isLight ? 'light' : 'dark');
}
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"┌──────────────────────────────────────────────┐")
    print(f"│   Control  ·  visual console                 │")
    print(f"│   http://127.0.0.1:{PORT}                      │")
    print(f"│   Open on iPad: http://<your-mac-ip>:{PORT}   │")
    print(f"│   Ctrl+C to stop                             │")
    print(f"└──────────────────────────────────────────────┘")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
