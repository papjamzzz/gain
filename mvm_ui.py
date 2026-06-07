#!/usr/bin/env python3
"""Control — visual console UI"""

import json
import time
import os
import signal
import importlib.util
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, Response, request, jsonify, redirect, url_for
from functools import wraps
import sqlite3
import uuid
import hashlib
import hmac as _hmac

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

# ── Free tier ──────────────────────────────────────────────────────────────────
FREE_TIER_LIMIT = 3
_free_usage: dict[str, int] = {}  # ip_hash → call count (resets on deploy)

def _ip_hash() -> str:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    return hashlib.sha256(ip.encode()).hexdigest()[:16]

def _free_tier_check() -> tuple[bool, int]:
    """Returns (allowed, calls_used). Bypassed for local and logged-in users."""
    if not os.environ.get("SUPABASE_URL"):
        return True, 0
    # Check for auth token — logged-in users use the Supabase gate instead
    auth = request.headers.get("Authorization", "")
    cookie_tok = request.cookies.get("sb-access-token", "")
    if auth.startswith("Bearer ") or cookie_tok:
        return True, 0
    h = _ip_hash()
    used = _free_usage.get(h, 0)
    return used < FREE_TIER_LIMIT, used

def _free_tier_increment():
    if not os.environ.get("SUPABASE_URL"):
        return
    h = _ip_hash()
    _free_usage[h] = _free_usage.get(h, 0) + 1

MODELS_AVAILABLE = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
}
_active_model = MODEL  # mutable, changed via /model POST


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
    "mode": "", "intensity": 0.5, "depth": 0.5,
    "certainty": 0.5, "risk": 0.5, "stance": "",
    "scope": 0.5, "bandwidth": 0.5, "filter": "",
    "room": 0.5, "decay": 0.5, "voice": "",
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

STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# ── Subscriber DB ──────────────────────────────────────────────────────────────

SUBSCRIBERS_DB = Path.home() / ".streamfader" / "subscribers.db"

def _init_subscribers_db():
    SUBSCRIBERS_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(SUBSCRIBERS_DB) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                email               TEXT PRIMARY KEY,
                plan                TEXT    DEFAULT 'free',
                usage_count         INTEGER DEFAULT 0,
                monthly_limit       INTEGER DEFAULT 5,
                reset_date          TEXT,
                stripe_customer_id  TEXT
            )
        """)
        db.commit()

_init_subscribers_db()


def _get_user_info(token: str):
    """Return (user_id, email) from Supabase JWT. Returns (None, None) on failure."""
    if not token or not SUPABASE_URL:
        return None, None
    try:
        import requests as _req
        r = _req.get(f"{SUPABASE_URL}/auth/v1/user",
                     headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON},
                     timeout=4)
        if r.status_code == 200:
            data = r.json()
            return data.get("id"), data.get("email")
    except Exception:
        pass
    return None, None


def _get_plan(email: str) -> dict:
    """Return subscriber record for email, auto-resetting monthly usage if past reset_date."""
    try:
        with sqlite3.connect(SUBSCRIBERS_DB) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM subscribers WHERE email=?", (email,)).fetchone()
            if row:
                row = dict(row)
                if row["reset_date"] and row["plan"] not in ("free", "cancelled"):
                    reset = datetime.fromisoformat(row["reset_date"])
                    if datetime.utcnow() > reset:
                        new_reset = (datetime.utcnow() + timedelta(days=30)).isoformat()
                        with sqlite3.connect(SUBSCRIBERS_DB) as db2:
                            db2.execute(
                                "UPDATE subscribers SET usage_count=0, reset_date=? WHERE email=?",
                                (new_reset, email))
                            db2.commit()
                        row["usage_count"] = 0
                        row["reset_date"]  = new_reset
                return row
    except Exception:
        pass
    return {"email": email, "plan": "free", "usage_count": 0, "monthly_limit": 5, "reset_date": None}


def _check_and_increment(email: str):
    """Check plan limit and increment usage. Returns (allowed: bool, error_code: str)."""
    plan  = _get_plan(email)
    limit = plan["monthly_limit"]
    used  = plan["usage_count"]
    if limit != -1 and used >= limit:
        return False, "free_limit" if plan["plan"] == "free" else "plan_limit"
    try:
        with sqlite3.connect(SUBSCRIBERS_DB) as db:
            db.execute("""
                INSERT INTO subscribers (email, plan, usage_count, monthly_limit, reset_date)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(email) DO UPDATE SET usage_count = usage_count + 1
            """, (email, plan["plan"], limit,
                  plan.get("reset_date") or (datetime.utcnow() + timedelta(days=30)).isoformat()))
            db.commit()
    except Exception:
        pass
    return True, ""


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
    return HTML

@app.route("/")
def index():
    if SUPABASE_URL:
        return redirect("/login")
    return HTML

@app.route("/stream")
def stream():
    user_id = None  # always use local state.json — skip Supabase lookup to avoid blocking
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
    allowed, used = _free_tier_check()
    if not allowed:
        return jsonify({"error": "free_limit", "used": used, "limit": FREE_TIER_LIMIT}), 402
    data    = request.get_json() or {}
    task    = data.get("task", "").strip()
    if not task:
        return jsonify({"error": "No task provided"}), 400
    _free_tier_increment()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    user_id = None  # always use local state.json — skip Supabase lookup to avoid blocking

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
                model=_active_model,
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
    allowed, used = _free_tier_check()
    if not allowed:
        return jsonify({"error": "free_limit", "used": used, "limit": FREE_TIER_LIMIT}), 402
    _free_tier_increment()
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
                    model=_active_model,
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


COMPARE_LOG = Path.home() / ".streamfader" / "comparisons.json"

@app.route("/compare", methods=["POST"])
def compare_presets():
    # ── Free tier gate (anonymous users) ──────────────────────────────────
    allowed, used = _free_tier_check()
    if not allowed:
        return jsonify({"error": "free_limit", "used": used, "limit": FREE_TIER_LIMIT}), 402
    _free_tier_increment()
    # ── Plan gate ──────────────────────────────────────────────────────────
    if SUPABASE_URL:
        token = _token_from_request()
        _, email = _get_user_info(token)
        if not email:
            return jsonify({"error": "unauthorized"}), 401
        allowed, err = _check_and_increment(email)
        if not allowed:
            plan = _get_plan(email)
            return jsonify({
                "error": "limit_reached",
                "plan":  plan["plan"],
                "usage": plan["usage_count"],
                "limit": plan["monthly_limit"],
            }), 402
    # ──────────────────────────────────────────────────────────────────────
    data     = request.get_json() or {}
    preset_a = data.get("preset_a", "").strip()
    preset_b = data.get("preset_b", "").strip()
    prompt   = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    def _load(name):
        if name == "__current__":
            return read_state()
        f = PRESETS_DIR / f"{name}.json"
        return json.loads(f.read_text()) if f.exists() else None

    state_a = _load(preset_a)
    state_b = _load(preset_b)
    if state_a is None:
        return jsonify({"error": f"Preset '{preset_a}' not found"}), 404
    if state_b is None:
        return jsonify({"error": f"Preset '{preset_b}' not found"}), 404

    api_key = os.environ.get("ANTHROPIC_API_KEY")

    def generate():
        if not _anthropic:
            yield f"data: {json.dumps({'error': 'anthropic not installed'})}\n\n"; return
        if not api_key:
            yield f"data: {json.dumps({'error': 'ANTHROPIC_API_KEY not set'})}\n\n"; return
        client = _anthropic.Anthropic(api_key=api_key)

        # ── Run A ──────────────────────────────────────────────
        yield f"data: {json.dumps({'phase': 'a_start'})}\n\n"
        output_a = ""
        tokens_a  = 0
        try:
            with client.messages.stream(
                model=_active_model, max_tokens=2048,
                system=_build_prompt(state_a),
                messages=[{"role": "user", "content": prompt}],
            ) as s:
                for text in s.text_stream:
                    output_a += text
                    yield f"data: {json.dumps({'phase': 'a', 'text': text})}\n\n"
                try:
                    tokens_a = s.get_final_message().usage.output_tokens
                except Exception:
                    pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"; return
        words_a = len(output_a.split())
        yield f"data: {json.dumps({'phase': 'a_done', 'tokens': tokens_a, 'words': words_a})}\n\n"

        # ── Run B ──────────────────────────────────────────────
        yield f"data: {json.dumps({'phase': 'b_start'})}\n\n"
        output_b = ""
        tokens_b  = 0
        try:
            with client.messages.stream(
                model=_active_model, max_tokens=2048,
                system=_build_prompt(state_b),
                messages=[{"role": "user", "content": prompt}],
            ) as s:
                for text in s.text_stream:
                    output_b += text
                    yield f"data: {json.dumps({'phase': 'b', 'text': text})}\n\n"
                try:
                    tokens_b = s.get_final_message().usage.output_tokens
                except Exception:
                    pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"; return
        words_b = len(output_b.split())
        yield f"data: {json.dumps({'phase': 'b_done', 'tokens': tokens_b, 'words': words_b})}\n\n"

        # ── Score ──────────────────────────────────────────────
        yield f"data: {json.dumps({'phase': 'scoring'})}\n\n"
        try:
            score_prompt = (
                "You are a behavioral evaluation system for AI outputs.\n"
                "The SAME prompt was run against two different AI behavioral configurations.\n\n"
                f"PROMPT: {prompt}\n\n"
                f"OUTPUT A (Preset: {preset_a}):\n{output_a}\n\n"
                f"OUTPUT B (Preset: {preset_b}):\n{output_b}\n\n"
                f"USAGE: Output A used {tokens_a} tokens ({words_a} words). "
                f"Output B used {tokens_b} tokens ({words_b} words).\n\n"
                "Score each output on these 6 metrics (0-100):\n"
                "1. ADHERENCE — Did it follow the request precisely?\n"
                "2. DEPTH — How thoroughly did it address the topic?\n"
                "3. CLARITY — How clear and well-structured is the output?\n"
                "4. EFFICIENCY — Did it say what needed saying without waste?\n"
                "5. CONFIDENCE — How decisive and assured is the output tone?\n"
                "6. TOKEN_EFFICIENCY — Considering the token counts above, which delivered more value per token spent? Score higher for more signal per token.\n\n"
                "Return ONLY valid JSON with no text before or after:\n"
                '{"adherence":{"a":0,"b":0,"winner":"a"},'
                '"depth":{"a":0,"b":0,"winner":"a"},'
                '"clarity":{"a":0,"b":0,"winner":"a"},'
                '"efficiency":{"a":0,"b":0,"winner":"a"},'
                '"confidence":{"a":0,"b":0,"winner":"a"},'
                '"token_efficiency":{"a":0,"b":0,"winner":"a"},'
                '"overall_winner":"a","summary":"2-3 sentences on the key behavioral differences"}'
            )
            resp = client.messages.create(
                model=_active_model, max_tokens=512,
                messages=[{"role": "user", "content": score_prompt}],
            )
            raw = resp.content[0].text.strip()
            if "```" in raw:
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else parts[0]
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            scores = json.loads(raw)
            try:
                existing = json.loads(COMPARE_LOG.read_text()) if COMPARE_LOG.exists() else []
                existing.append({
                    "timestamp": datetime.utcnow().isoformat(),
                    "prompt": prompt,
                    "preset_a": preset_a,
                    "preset_b": preset_b,
                    "scores": scores,
                })
                COMPARE_LOG.parent.mkdir(parents=True, exist_ok=True)
                COMPARE_LOG.write_text(json.dumps(existing, indent=2))
            except Exception:
                pass
            yield f"data: {json.dumps({'phase': 'scores', 'scores': scores})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'phase': 'score_error', 'error': str(e)})}\n\n"

        yield f"data: {json.dumps({'phase': 'done'})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/compare/stats")
def compare_stats():
    if not COMPARE_LOG.exists():
        return jsonify({"runs": 0, "presets": {}})
    try:
        comparisons = json.loads(COMPARE_LOG.read_text())
    except Exception:
        return jsonify({"runs": 0, "presets": {}})
    metrics = ["adherence", "depth", "clarity", "efficiency", "confidence"]
    preset_stats = {}
    for comp in comparisons:
        for side, name in [("a", comp.get("preset_a")), ("b", comp.get("preset_b"))]:
            if not name or name == "__current__":
                continue
            if name not in preset_stats:
                preset_stats[name] = {m: [] for m in metrics}
                preset_stats[name]["wins"] = 0
                preset_stats[name]["runs"] = 0
            preset_stats[name]["runs"] += 1
            s = comp.get("scores", {})
            for m in metrics:
                v = s.get(m, {}).get(side)
                if v is not None:
                    preset_stats[name][m].append(v)
            if s.get("overall_winner") == side:
                preset_stats[name]["wins"] += 1
    result = {}
    for p, d in preset_stats.items():
        result[p] = {"runs": d["runs"], "wins": d["wins"]}
        for m in metrics:
            vals = d[m]
            result[p][m] = round(sum(vals) / len(vals), 1) if vals else 0
    return jsonify({"runs": len(comparisons), "presets": result})


# ── Billing ────────────────────────────────────────────────────────────────────

@app.route("/billing/status")
def billing_status():
    if not SUPABASE_URL:
        return jsonify({"plan": "local", "usage": 0, "limit": -1})
    token = _token_from_request()
    _, email = _get_user_info(token)
    if not email:
        return jsonify({"error": "unauthorized"}), 401
    plan = _get_plan(email)
    return jsonify({
        "plan":  plan["plan"],
        "usage": plan["usage_count"],
        "limit": plan["monthly_limit"],
        "email": email,
    })


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if STRIPE_WEBHOOK_SECRET:
        try:
            parts    = dict(p.split("=", 1) for p in sig_header.split(","))
            ts       = parts.get("t", "")
            v1       = parts.get("v1", "")
            signed   = ts.encode() + b"." + payload
            expected = _hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
            if not _hmac.compare_digest(expected, v1):
                return jsonify({"error": "invalid signature"}), 400
        except Exception:
            return jsonify({"error": "signature error"}), 400

    event      = request.get_json(force=True)
    event_type = (event or {}).get("type", "")
    if not event:
        return jsonify({"ok": True}), 200

    if event_type == "customer.subscription.deleted":
        sub         = event["data"]["object"]
        customer_id = sub.get("customer", "")
        try:
            with sqlite3.connect(SUBSCRIBERS_DB) as db:
                db.execute(
                    "UPDATE subscribers SET monthly_limit=0, plan='cancelled' WHERE stripe_customer_id=?",
                    (customer_id,))
                db.commit()
        except Exception as e:
            print(f"[STRIPE CANCEL] {e}")
        return jsonify({"ok": True}), 200

    if event_type != "checkout.session.completed":
        return jsonify({"ok": True}), 200

    obj         = event["data"]["object"]
    email       = obj.get("customer_details", {}).get("email", "")
    customer_id = obj.get("customer", "")
    metadata    = obj.get("metadata", {})
    plan_key    = metadata.get("plan", "base")

    if plan_key == "foundational":
        monthly_limit = -1
    else:
        monthly_limit = 100

    reset_date = (datetime.utcnow() + timedelta(days=30)).isoformat()

    try:
        with sqlite3.connect(SUBSCRIBERS_DB) as db:
            db.execute("""
                INSERT INTO subscribers (email, plan, usage_count, monthly_limit, reset_date, stripe_customer_id)
                VALUES (?, ?, 0, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    plan=excluded.plan, monthly_limit=excluded.monthly_limit,
                    reset_date=excluded.reset_date, stripe_customer_id=excluded.stripe_customer_id,
                    usage_count=0
            """, (email, plan_key, monthly_limit, reset_date, customer_id))
            db.commit()
        print(f"[STRIPE] New subscriber: {email} → {plan_key}")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True}), 200


@app.route("/proto")
def proto_view():
    return HTML


@app.route("/companion")
def companion():
    from flask import make_response
    resp = make_response(COMPANION_HTML)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/health")
def health():
    return jsonify({"ok": True, "model": _active_model,
                    "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY"))})


@app.route("/model", methods=["GET"])
def get_model():
    slug = next((k for k, v in MODELS_AVAILABLE.items() if v == _active_model), "sonnet")
    return jsonify({"model": _active_model, "slug": slug})


@app.route("/model", methods=["POST"])
def set_model():
    global _active_model
    data = request.get_json() or {}
    slug = data.get("slug", "").strip().lower()
    if slug not in MODELS_AVAILABLE:
        return jsonify({"error": f"Unknown model slug '{slug}'"}), 400
    _active_model = MODELS_AVAILABLE[slug]
    return jsonify({"ok": True, "model": _active_model, "slug": slug})


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
    <div class="brand">GAIN</div>
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

# ── MAIN HTML ─────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1">
<title>Gain</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Abril+Fatface&family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=Abril+Fatface&family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet"></noscript>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#060A0F;font-family:'Inter',sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden;color:#D8EAF8;user-select:none;}
.hdr{height:64px;display:flex;align-items:center;padding:0 24px;border-bottom:1px solid #162030;flex-shrink:0;background:#030507;position:relative;}
.brand{font-family:'Abril Fatface',serif;font-size:42px;letter-spacing:.06em;background:linear-gradient(130deg,#00E8FF,#A0C8FF,#C0A0FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;filter:drop-shadow(0 0 8px rgba(0,200,255,.5));}
.hdr-center{text-align:right;pointer-events:none;flex-shrink:0;}
.hdr-lbl{font-size:7px;font-weight:800;letter-spacing:.22em;text-transform:uppercase;color:#00DDD4;opacity:.9;}
.hdr-vals{font-size:8px;color:#D8EAF8;letter-spacing:.04em;margin-top:2px;}
.proto-tag{margin-left:auto;font-size:9px;font-weight:900;letter-spacing:.18em;text-transform:uppercase;color:rgba(217,70,239,.7);border:1px solid rgba(217,70,239,.35);padding:5px 12px;border-radius:3px;}
.theme-btn{width:36px;height:36px;border-radius:50%;border:1.5px solid rgba(0,200,192,.45);background:rgba(0,200,192,.06);color:#00DDD4;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s;margin-left:10px;flex-shrink:0;}
.theme-btn:hover{border-color:#00DDD4;background:rgba(0,200,192,.16);}
/* ── Light theme ── */
body.light{background:#EDEBE7;color:#1C2B3A;}
body.light .hdr{background:#E8E5E0;border-bottom-color:rgba(0,0,0,.1);}
body.light .brand{filter:drop-shadow(0 0 8px rgba(0,120,160,.35));}
body.light .col{border-right-color:rgba(0,0,0,.08);background:#F5F3F0;}
body.light .col-name{color:#5A7898;}
body.light .hdr-lbl{color:#007E78;}
body.light .hdr-vals{color:#0A2030;font-weight:800;}
body.light .col-flbl{color:#007E78;text-shadow:0 0 1px rgba(0,220,200,1),0 0 4px rgba(0,210,190,.95),0 0 10px rgba(0,190,170,.7);-webkit-text-fill-color:unset;}
body.light .t2 .col-flbl,body.light .t4 .col-flbl{color:#6D28D9;text-shadow:0 0 4px rgba(109,40,217,.4);}
body.light .col-fval{color:#007E78;text-shadow:0 0 8px rgba(0,158,150,.5);}
body.light .t2 .col-fval,body.light .t4 .col-fval{color:#6D28D9;text-shadow:0 0 4px rgba(109,40,217,.3);}
body.light .f-track{background:rgba(200,195,185,.4);border-color:rgba(0,120,115,.22);overflow:hidden;box-shadow:inset 0 2px 8px rgba(0,0,0,.08);}
body.light .t1 .f-fill,body.light .t3 .f-fill{background:linear-gradient(0deg,rgba(0,120,115,.88) 0%,rgba(0,150,145,.68) 45%,rgba(0,175,168,.45) 80%,rgba(0,195,188,.22) 100%);box-shadow:0 0 12px rgba(0,140,135,.25);}
body.light .t2 .f-fill,body.light .t4 .f-fill{background:linear-gradient(0deg,rgba(100,40,190,.78) 0%,rgba(125,65,215,.58) 45%,rgba(148,98,228,.38) 80%,rgba(168,128,240,.18) 100%);box-shadow:0 0 12px rgba(109,40,217,.18);}
body.light .f-thumb{background:linear-gradient(180deg,#E8E4DE 0%,#D0CCC6 30%,#BAB6B0 55%,#C8C4BE 80%,#D8D4CE 100%);border-top-color:rgba(255,255,255,.9);border-bottom-color:rgba(0,0,0,.2);box-shadow:0 3px 8px rgba(0,0,0,.2),0 1px 0 rgba(255,255,255,.8) inset;}
body.light .t1 .f-thumb,body.light .t3 .f-thumb{box-shadow:0 3px 8px rgba(0,0,0,.2),0 0 6px rgba(0,140,135,.2),0 1px 0 rgba(255,255,255,.8) inset;}
body.light .t2 .f-thumb,body.light .t4 .f-thumb{box-shadow:0 3px 8px rgba(0,0,0,.2),0 0 6px rgba(109,40,217,.15),0 1px 0 rgba(255,255,255,.8) inset;}
body.light .t1 .f-thumb .f-center::before,body.light .t1 .f-thumb .f-center::after,body.light .t3 .f-thumb .f-center::before,body.light .t3 .f-thumb .f-center::after{background:linear-gradient(90deg,transparent,rgba(0,140,135,.4),transparent);}
body.light .t2 .f-thumb .f-center::before,body.light .t2 .f-thumb .f-center::after,body.light .t4 .f-thumb .f-center::before,body.light .t4 .f-thumb .f-center::after{background:linear-gradient(90deg,transparent,rgba(109,40,217,.35),transparent);}
body.light .col-btn{color:#1A2B3A;border-color:rgba(0,80,120,.28);background:rgba(255,255,255,.75);font-weight:900;text-shadow:none;}
body.light .col-btn:hover{color:#007E78;border-color:#007E78;background:rgba(0,126,120,.1);text-shadow:none;}
body.light .col-btn.active{background:rgba(0,126,120,.18);border-color:#007E78;color:#003028;font-weight:900;text-shadow:none;}
body.light .col-btn.mute-btn{color:#8B2020;border-color:rgba(140,30,30,.38);background:rgba(180,40,40,.08);font-weight:900;}
body.light .col-btn.mute-btn.active{color:#CC1010;border-color:rgba(200,20,20,.65);background:rgba(160,20,20,.14);text-shadow:none;}
body.light .t1 .col-accent,body.light .t3 .col-accent{background:linear-gradient(90deg,#005850,#007E78);box-shadow:none;}
body.light .t2 .col-accent,body.light .t4 .col-accent{background:linear-gradient(90deg,#5020A0,#6D28D9);box-shadow:none;}
body.light .proto-tag{color:#B020C8;border-color:rgba(176,32,200,.35);}
body.light .theme-btn{border-color:rgba(0,126,120,.45);color:#007E78;background:rgba(0,126,120,.06);}
.col-knob-wrap{display:flex;flex-direction:column;align-items:center;gap:5px;padding:12px 0 10px;border-top:1px solid rgba(0,200,192,.14);flex-shrink:0;}
.col-klbl{font-size:12px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:#6A8AA8;text-align:center;}
.col-knob{width:58px;height:58px;border-radius:50%;position:relative;cursor:grab;touch-action:none;background:conic-gradient(#0A1620 0deg 225deg,#0A1620 225deg 360deg);box-shadow:0 5px 18px rgba(0,0,0,1),0 0 0 1px rgba(0,0,0,.98),0 0 0 2px rgba(255,255,255,.04),inset 0 1px 0 rgba(255,255,255,.06),0 0 20px rgba(0,196,232,.08);}
.col-knob:active{cursor:grabbing;}
.col-knob .knob-body{position:absolute;inset:5px;border-radius:50%;background:radial-gradient(circle at 36% 30%,#2C3E54 0%,#16263A 28%,#0A1828 58%,#050E1A 100%);border:1px solid rgba(0,0,0,.98);box-shadow:inset 0 3px 7px rgba(255,255,255,.13),inset 0 -3px 9px rgba(0,0,0,.96);}
.col-knob .knob-dot{position:absolute;width:3px;height:14px;background:linear-gradient(180deg,#FFFFFF,#A0F0FF,#00C8E8);border-radius:2px;top:7px;left:50%;transform-origin:50% 22px;transform:translateX(-50%) rotate(0deg);box-shadow:0 0 5px rgba(0,240,255,.85),0 0 12px rgba(0,196,232,.65);}
.col-kval{font-size:13px;font-weight:900;color:#00DDD4;font-variant-numeric:tabular-nums;text-shadow:0 0 6px rgba(0,200,192,.4);}
.t2 .col-kval,.t4 .col-kval{color:#A78BFA;text-shadow:0 0 6px rgba(167,139,250,.4);}
body.light .col-knob{background:conic-gradient(#9A9890 0deg 225deg,#9A9890 225deg 360deg);box-shadow:0 4px 12px rgba(0,0,0,.30),0 0 0 1px rgba(0,0,0,.20),0 0 0 2px rgba(255,255,255,.6);}
body.light .col-knob .knob-body{background:radial-gradient(circle at 32% 28%,#F0EDE8 0%,#D8D4CE 22%,#B8B4AC 50%,#A0A098 78%,#909088 100%);border-color:rgba(0,0,0,.25);}
body.light .col-knob .knob-dot{background:linear-gradient(180deg,#FFFFFF,#A0D0C8,#007E78);box-shadow:0 0 4px rgba(0,140,130,.6);}
body.light .col-kval{color:#007E78;text-shadow:none;}
body.light .t2 .col-kval,body.light .t4 .col-kval{color:#6D28D9;}
body.light .col-klbl{color:#5A7898;}
body.light .col-knob-wrap{border-top-color:rgba(0,126,120,.15);}
.stage{flex:1;display:flex;min-height:0;}
.col{flex:1;display:flex;flex-direction:column;border-right:1px solid #162030;padding:16px 14px 12px;min-width:0;position:relative;}
.col:last-child{border-right:none;}
.col-accent{height:3px;border-radius:2px;margin-bottom:10px;flex-shrink:0;}
.t1 .col-accent,.t3 .col-accent{background:linear-gradient(90deg,#008880,#00C8C0);box-shadow:0 0 8px rgba(0,200,192,.5);}
.t2 .col-accent,.t4 .col-accent{background:linear-gradient(90deg,#6030B0,#8B5CF6);box-shadow:0 0 8px rgba(139,92,246,.5);}
.col-name{font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:#405870;margin-bottom:8px;flex-shrink:0;}
.col-flbl{font-size:20px;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:#00DDD4;text-align:center;margin-bottom:10px;flex-shrink:0;text-shadow:0 0 12px rgba(0,220,212,.4);}
.t2 .col-flbl,.t4 .col-flbl{color:#A78BFA;text-shadow:0 0 12px rgba(167,139,250,.4);}
.col-fader{flex:1;display:flex;justify-content:center;position:relative;min-height:0;}
.f-track{width:calc(100% - 12px);height:100%;background:rgba(0,6,12,.85);border:1px solid rgba(0,180,172,.16);border-radius:5px;position:relative;cursor:ns-resize;touch-action:none;overflow:hidden;box-shadow:inset 0 4px 20px rgba(0,0,0,.8),0 0 0 1px rgba(0,0,0,.5);}
.f-fill{position:absolute;bottom:0;left:0;right:0;border-radius:0;pointer-events:none;}
.t1 .f-fill,.t3 .f-fill{background:linear-gradient(0deg,rgba(0,150,144,.92) 0%,rgba(0,185,178,.76) 35%,rgba(0,215,207,.54) 70%,rgba(0,238,230,.32) 90%,rgba(100,255,250,.14) 100%);box-shadow:0 0 24px rgba(0,200,192,.45),0 0 60px rgba(0,180,175,.15),inset 0 0 30px rgba(0,160,155,.08);}
.t2 .f-fill,.t4 .f-fill{background:linear-gradient(0deg,rgba(88,28,180,.92) 0%,rgba(120,62,222,.76) 35%,rgba(150,98,242,.54) 70%,rgba(175,135,255,.32) 90%,rgba(210,185,255,.14) 100%);box-shadow:0 0 24px rgba(139,92,246,.45),0 0 60px rgba(139,92,246,.15),inset 0 0 30px rgba(100,60,200,.08);}
.f-thumb{
  position:absolute;width:calc(100% + 4px);height:28px;
  left:-2px;transform:translateY(50%);
  cursor:ns-resize;z-index:3;touch-action:none;
  border-radius:4px;
  background:linear-gradient(180deg,#1E2E3C 0%,#14202C 30%,#0C1620 55%,#121E2A 80%,#1A2A38 100%);
  border-top:1px solid rgba(255,255,255,.14);
  border-bottom:1px solid rgba(0,0,0,.9);
  border-left:1px solid rgba(255,255,255,.06);
  border-right:1px solid rgba(255,255,255,.06);
  box-shadow:0 3px 10px rgba(0,0,0,.85),0 1px 0 rgba(255,255,255,.06) inset,0 -1px 0 rgba(0,0,0,.6) inset;
  display:flex;align-items:center;justify-content:center;
}
.t1 .f-thumb,.t3 .f-thumb{box-shadow:0 3px 10px rgba(0,0,0,.85),0 0 8px rgba(0,200,192,.18),0 1px 0 rgba(255,255,255,.06) inset,0 -1px 0 rgba(0,0,0,.6) inset;}
.t2 .f-thumb,.t4 .f-thumb{box-shadow:0 3px 10px rgba(0,0,0,.85),0 0 8px rgba(139,92,246,.18),0 1px 0 rgba(255,255,255,.06) inset,0 -1px 0 rgba(0,0,0,.6) inset;}
.f-center{
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
  width:60%;pointer-events:none;
}
.f-center::before,.f-center::after{content:'';display:block;width:100%;height:1px;}
.f-center::before{background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);}
.f-center::after{background:linear-gradient(90deg,transparent,rgba(255,255,255,.09),transparent);}
.t1 .f-thumb .f-center::before,.t1 .f-thumb .f-center::after,
.t3 .f-thumb .f-center::before,.t3 .f-thumb .f-center::after{background:linear-gradient(90deg,transparent,rgba(0,220,212,.45),transparent);}
.t2 .f-thumb .f-center::before,.t2 .f-thumb .f-center::after,
.t4 .f-thumb .f-center::before,.t4 .f-thumb .f-center::after{background:linear-gradient(90deg,transparent,rgba(167,139,250,.45),transparent);}
.col-fval{font-size:18px;font-weight:900;text-align:center;color:#00DDD4;margin-top:8px;flex-shrink:0;font-variant-numeric:tabular-nums;text-shadow:0 0 8px rgba(0,200,192,.4);}
.t2 .col-fval,.t4 .col-fval{color:#A78BFA;text-shadow:0 0 8px rgba(167,139,250,.4);}
.col-btns{display:flex;flex-direction:column;gap:5px;margin-top:10px;flex-shrink:0;}
.col-btn{height:40px;border-radius:3px;border:1px solid rgba(60,110,155,.55);background:rgba(12,22,38,.9);color:#C8E2F6;font-size:12px;font-weight:900;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;transition:all .1s;font-family:'Inter',sans-serif;}
.col-btn:hover{border-color:#00DDD4;color:#00F0E6;background:rgba(14,28,48,.95);}
.col-btn.active{background:linear-gradient(180deg,#002830,#001820);color:#20F8EE;border-color:#00DDD4;text-shadow:0 0 8px rgba(0,248,238,1),0 0 18px rgba(0,220,212,.6);box-shadow:inset 0 0 10px rgba(0,221,212,.08),0 0 6px rgba(0,221,212,.2);}
.col-btn.mute-btn{border-color:rgba(200,70,70,.35);color:#C07888;font-weight:900;}
.col-btn.mute-btn.active{background:linear-gradient(180deg,#1E0808,#120404);color:#E07878;border-color:rgba(200,60,60,.7);text-shadow:0 0 8px rgba(220,70,70,.7);}
/* ── Header action buttons ── */
.hdr-action{height:30px;padding:0 12px;border-radius:3px;border:1px solid rgba(0,200,192,.3);background:rgba(0,200,192,.06);color:rgba(0,220,212,.8);font-size:9px;font-weight:900;letter-spacing:.14em;text-transform:uppercase;cursor:pointer;transition:all .12s;font-family:'Inter',sans-serif;margin-left:6px;flex-shrink:0;}
.hdr-action:hover{border-color:#00DDD4;color:#00DDD4;background:rgba(0,200,192,.14);}
.hdr-action.cmp-trigger{border-color:rgba(217,70,239,.4);color:rgba(217,70,239,.85);}
.hdr-action.cmp-trigger:hover{border-color:#D946EF;color:#D946EF;background:rgba(217,70,239,.1);}
body.light .hdr-action{border-color:rgba(0,126,120,.3);background:rgba(0,126,120,.05);color:rgba(0,126,120,.8);}
body.light .hdr-action.cmp-trigger{border-color:rgba(176,32,200,.35);color:rgba(176,32,200,.8);}
/* ── Bottom bar ── */
.bottom-bar{flex-shrink:0;border-top:1px solid #162030;background:#030507;padding:10px 16px;display:flex;gap:14px;}
.bb-col{flex:1;display:flex;flex-direction:column;gap:5px;min-width:0;}
.bb-lbl{font-size:8px;font-weight:900;letter-spacing:.2em;text-transform:uppercase;color:rgba(0,200,192,.45);}
.bb-row{display:flex;gap:6px;}
.bb-input{flex:1;height:34px;background:rgba(4,8,14,.9);border:1px solid rgba(0,180,172,.2);border-radius:3px;color:#D8EAF8;font-size:12px;font-family:'Inter',sans-serif;padding:0 10px;outline:none;min-width:0;}
.bb-input:focus{border-color:rgba(0,220,212,.45);}
.bb-input::placeholder{color:rgba(100,140,165,.35);font-size:11px;}
.bb-btn{height:34px;padding:0 14px;border-radius:3px;border:1px solid rgba(0,200,192,.32);background:rgba(0,200,192,.07);color:#00DDD4;font-size:9px;font-weight:900;letter-spacing:.14em;text-transform:uppercase;cursor:pointer;white-space:nowrap;transition:all .12s;font-family:'Inter',sans-serif;flex-shrink:0;}
.bb-btn:hover{background:rgba(0,200,192,.16);border-color:#00DDD4;}
.bb-btn:disabled{opacity:.35;cursor:not-allowed;}
.preset-chips{display:flex;flex-wrap:wrap;gap:4px;min-height:18px;}
.preset-chip{display:flex;align-items:center;gap:4px;background:#0A141E;border:1px solid #1E2E40;border-radius:3px;padding:3px 5px 3px 9px;cursor:default;transition:border-color .1s;}
.preset-chip:hover{border-color:rgba(0,200,192,.55);}
.preset-chip-name{font-size:11px;font-weight:700;color:#A8CCDE;letter-spacing:.04em;cursor:pointer;}
.preset-chip-name:hover{color:#00DDD4;}
.preset-chip-del{background:none;border:none;color:rgba(180,80,80,.7);font-size:12px;cursor:pointer;padding:0 2px;line-height:1;font-weight:900;transition:color .1s;}
.preset-chip-del:hover{color:#FF5050;}
.preset-empty{font-size:10px;color:rgba(80,120,145,.4);font-style:italic;}
.run-status{font-size:9px;font-weight:700;letter-spacing:.1em;color:rgba(0,200,192,.5);min-height:13px;}
.resp-box{background:#040810;border:1px solid #162030;border-radius:3px;padding:9px 11px;font-size:11px;font-family:'JetBrains Mono','Inter',monospace;color:#B8D0E8;max-height:120px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;line-height:1.55;display:none;}
.resp-box.has-content{display:block;}
/* ── Compare panel ── */
.cmp-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:100;}
.cmp-overlay.open{display:block;}
.cmp-panel{position:fixed;bottom:-100%;left:0;right:0;height:72vh;background:#060C14;border-top:2px solid rgba(217,70,239,.4);z-index:101;transition:bottom .28s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;box-shadow:0 -8px 40px rgba(0,0,0,.8);}
.cmp-panel.open{bottom:0;}
.cmp-hd{padding:9px 18px;border-bottom:1px solid #162030;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;background:#030810;}
.cmp-title{font-size:10px;font-weight:900;letter-spacing:.2em;text-transform:uppercase;color:rgba(217,70,239,.85);}
.cmp-close{width:22px;height:22px;border:1px solid #1E2E40;background:transparent;cursor:pointer;font-size:11px;color:#6A8AA8;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;}
.cmp-close:hover{color:#D946EF;border-color:rgba(217,70,239,.4);}
.cmp-body{padding:14px 18px;flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;}
.cmp-save-row{display:flex;gap:6px;align-items:center;padding-bottom:10px;border-bottom:1px solid #162030;}
.cmp-save-lbl{font-size:8px;font-weight:900;letter-spacing:.16em;text-transform:uppercase;color:rgba(0,200,192,.4);flex-shrink:0;white-space:nowrap;}
.cmp-save-input{flex:1;height:30px;background:#030810;border:1px solid rgba(0,180,172,.18);border-radius:3px;color:#D8EAF8;font-size:12px;font-family:'Inter',sans-serif;padding:0 8px;outline:none;}
.cmp-save-input:focus{border-color:rgba(0,220,212,.45);}
.cmp-save-btn{height:30px;padding:0 11px;border-radius:3px;border:1px solid rgba(0,200,192,.28);background:rgba(0,200,192,.06);color:#00DDD4;font-size:8px;font-weight:900;letter-spacing:.12em;text-transform:uppercase;cursor:pointer;font-family:'Inter',sans-serif;flex-shrink:0;}
.cmp-save-btn:hover{background:rgba(0,200,192,.14);}
.cmp-save-msg{font-size:9px;font-weight:700;color:rgba(0,200,192,.6);min-height:12px;letter-spacing:.06em;}
.cmp-sel-row{display:flex;gap:10px;}
.cmp-sel-group{flex:1;display:flex;flex-direction:column;gap:4px;}
.cmp-sel-lbl{font-size:8px;font-weight:900;letter-spacing:.16em;text-transform:uppercase;color:rgba(0,200,192,.45);}
.cmp-select{height:32px;background:#030810;border:1px solid rgba(0,180,172,.22);border-radius:3px;color:#D8EAF8;font-size:12px;font-family:'Inter',sans-serif;padding:0 8px;outline:none;}
.cmp-prompt-row{display:flex;gap:8px;}
.cmp-prompt-input{flex:1;height:34px;background:#030810;border:1px solid rgba(0,180,172,.18);border-radius:3px;color:#D8EAF8;font-size:12px;font-family:'Inter',sans-serif;padding:0 10px;outline:none;}
.cmp-prompt-input:focus{border-color:rgba(0,220,212,.45);}
.cmp-run-btn{height:34px;padding:0 16px;border-radius:3px;border:1px solid rgba(217,70,239,.4);background:rgba(217,70,239,.08);color:rgba(217,70,239,.9);font-size:9px;font-weight:900;letter-spacing:.14em;text-transform:uppercase;cursor:pointer;font-family:'Inter',sans-serif;flex-shrink:0;}
.cmp-run-btn:hover{background:rgba(217,70,239,.18);border-color:#D946EF;}
.cmp-run-btn:disabled{opacity:.3;cursor:not-allowed;}
.cmp-status{font-size:9px;font-weight:700;letter-spacing:.1em;color:rgba(0,200,192,.5);min-height:14px;}
.cmp-outputs{display:flex;gap:10px;flex:1;min-height:0;}
.cmp-out{flex:1;display:flex;flex-direction:column;gap:4px;min-height:0;}
.cmp-out-lbl{font-size:8px;font-weight:900;letter-spacing:.16em;text-transform:uppercase;color:rgba(0,200,192,.5);flex-shrink:0;}
.cmp-out-box{flex:1;background:#020810;border:1px solid #162030;border-radius:3px;padding:8px 10px;font-size:11px;font-family:'JetBrains Mono','Inter',monospace;color:#A8C8E0;overflow-y:auto;white-space:pre-wrap;word-break:break-word;line-height:1.5;}
.cmp-metrics-wrap{flex-shrink:0;border-top:1px solid #162030;padding-top:10px;display:none;flex-direction:column;gap:4px;}
.cmp-metrics-wrap.visible{display:flex;}
.cmp-score-hd{font-size:8px;font-weight:900;letter-spacing:.18em;text-transform:uppercase;color:rgba(0,200,192,.4);margin-bottom:4px;}
.cmp-metric-row{display:flex;align-items:center;gap:8px;}
.cmp-metric-name{font-size:8px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:#5A7898;width:96px;flex-shrink:0;}
.cmp-bars{flex:1;display:flex;gap:3px;}
.cmp-bar{flex:1;height:10px;background:#0A141E;border-radius:2px;overflow:hidden;}
.cmp-bar-a .cmp-bar-fill{height:100%;background:linear-gradient(90deg,#007E78,#00DDD4);border-radius:2px;}
.cmp-bar-b .cmp-bar-fill{height:100%;background:linear-gradient(90deg,#5020A0,#A78BFA);border-radius:2px;}
.cmp-winner{font-size:8px;font-weight:900;color:#00DDD4;width:14px;text-align:center;flex-shrink:0;}
.cmp-summary{font-size:11px;color:#7A9AB8;line-height:1.6;padding-top:8px;border-top:1px solid #162030;margin-top:6px;flex-shrink:0;}
/* Light theme overrides */
body.light .bottom-bar{background:#E8E5E0;border-top-color:rgba(0,0,0,.1);}
body.light .bb-input{background:rgba(240,237,232,.9);border-color:rgba(0,120,115,.2);color:#1C2B3A;}
body.light .bb-btn{border-color:rgba(0,126,120,.35);background:rgba(0,126,120,.06);color:#007E78;}
body.light .bb-lbl{color:rgba(0,126,120,.55);}
body.light .preset-chip{background:#E2DED8;border-color:rgba(0,0,0,.15);}
body.light .preset-chip-name{color:#1A3050;font-weight:700;}
body.light .preset-chip-name:hover{color:#007E78;}
body.light .preset-chip-del{color:rgba(160,40,40,.65);}
body.light .preset-chip-del:hover{color:#CC1010;}
body.light .resp-box{background:#F0EDE8;border-color:rgba(0,0,0,.1);color:#1C2B3A;}
body.light .cmp-panel{background:#F0EDE8;border-top-color:rgba(176,32,200,.55);}
body.light .cmp-hd{background:#E2DED8;}
body.light .cmp-title{color:#8B10A8;font-weight:900;}
body.light .cmp-close{color:#1C2B3A;border-color:rgba(0,0,0,.25);background:rgba(0,0,0,.04);}
body.light .cmp-close:hover{color:#8B10A8;border-color:rgba(176,32,200,.5);}
body.light .cmp-save-lbl{color:#005050;font-weight:900;}
body.light .cmp-save-input{background:#fff;border-color:rgba(0,120,115,.35);color:#0A1C2A;font-weight:600;}
body.light .cmp-save-btn{border-color:rgba(0,120,115,.4);background:rgba(0,126,120,.08);color:#005858;font-weight:900;}
body.light .cmp-save-msg{color:#007E78;font-weight:800;}
body.light .cmp-sel-lbl{color:#005050;font-weight:900;}
body.light .cmp-select{background:#fff;border-color:rgba(0,120,115,.35);color:#0A1C2A;font-weight:600;}
body.light .cmp-prompt-input{background:#fff;border-color:rgba(0,120,115,.35);color:#0A1C2A;font-weight:600;}
body.light .cmp-prompt-input::placeholder{color:rgba(0,30,40,.35);}
body.light .cmp-run-btn{border-color:rgba(176,32,200,.55);background:rgba(176,32,200,.08);color:#8B10A8;font-weight:900;}
body.light .cmp-run-btn:hover{background:rgba(176,32,200,.16);border-color:#8B10A8;}
body.light .cmp-status{color:#007E78;font-weight:800;}
body.light .cmp-out-lbl{color:#005050;font-weight:900;}
body.light .cmp-out-box{background:#fff;border-color:rgba(0,0,0,.12);color:#0A1C2A;font-weight:500;}
body.light .cmp-score-hd{color:#005050;font-weight:900;}
body.light .cmp-metric-name{color:#2A4060;font-weight:800;}
body.light .cmp-winner{color:#007E78;font-weight:900;}
body.light .cmp-summary{color:#0A1C2A;font-weight:500;border-top-color:rgba(0,0,0,.1);}
/* ── Muted track — grey out fader ── */
.col.muted .f-fill{
  background:linear-gradient(0deg,rgba(55,65,75,.85) 0%,rgba(70,80,90,.65) 40%,rgba(88,98,108,.4) 75%,rgba(105,115,125,.18) 100%);
  box-shadow:none;transition:background .3s,box-shadow .3s;
}
.col.muted .f-thumb{
  background:linear-gradient(180deg,#181E24 0%,#101418 30%,#0A0E12 55%,#101418 80%,#161C22 100%);
  box-shadow:0 2px 6px rgba(0,0,0,.6);
  opacity:.45;
  transition:background .3s,box-shadow .3s,opacity .3s;
}
.col.muted .f-track{border-color:rgba(60,70,80,.3);}
.col.muted .col-flbl{color:#3A5060;text-shadow:none;transition:color .3s;}
.col.muted .col-fval{color:#3A5060;text-shadow:none;transition:color .3s;}
.col.muted .col-kval{color:#3A5060;text-shadow:none;}
/* Light theme muted */
body.light .col.muted .f-fill{background:linear-gradient(0deg,rgba(160,165,170,.75) 0%,rgba(175,180,185,.55) 45%,rgba(190,195,200,.3) 80%,rgba(205,210,215,.12) 100%);box-shadow:none;}
body.light .col.muted .f-thumb{background:linear-gradient(90deg,transparent,rgba(120,128,136,.6) 20%,rgba(150,158,166,.8) 50%,rgba(120,128,136,.6) 80%,transparent);box-shadow:0 0 4px rgba(120,128,136,.4);}
body.light .col.muted .col-flbl{color:#9AA8B8;text-shadow:none;}
body.light .col.muted .col-fval{color:#9AA8B8;text-shadow:none;}
/* ── Compare open button — flagship feature ── */
@keyframes cmp-pulse{
  0%,100%{box-shadow:0 0 10px rgba(217,70,239,.35),0 0 22px rgba(217,70,239,.18),inset 0 0 8px rgba(217,70,239,.12);}
  50%{box-shadow:0 0 22px rgba(217,70,239,.9),0 0 45px rgba(217,70,239,.55),0 0 80px rgba(217,70,239,.25),inset 0 0 18px rgba(217,70,239,.22);}
}
.cmp-open-btn{
  height:30px;padding:0 14px;border-radius:3px;
  border:2px solid rgba(217,70,239,.95);
  background:linear-gradient(180deg,rgba(145,35,190,.38) 0%,rgba(100,15,150,.52) 100%);
  color:#F0ABFF;font-size:9px;font-weight:900;letter-spacing:.18em;
  text-transform:uppercase;cursor:pointer;transition:all .15s;white-space:nowrap;
  text-shadow:0 0 10px rgba(255,180,255,1),0 0 22px rgba(217,70,239,.9);
  box-shadow:0 0 18px rgba(217,70,239,.6),0 0 40px rgba(217,70,239,.25),inset 0 0 12px rgba(217,70,239,.15);
  flex-shrink:0;touch-action:manipulation;font-family:'Inter',sans-serif;
  position:absolute;left:50%;transform:translateX(-50%);
}
.cmp-open-btn:hover{
  background:linear-gradient(180deg,rgba(180,50,220,.5) 0%,rgba(130,20,180,.6) 100%);
  border-color:#F0ABFF;
  box-shadow:0 0 30px rgba(217,70,239,1),0 0 60px rgba(217,70,239,.6),inset 0 0 24px rgba(217,70,239,.3);
}
body.light .cmp-open-btn{border-color:rgba(176,32,200,.85);background:linear-gradient(180deg,rgba(176,32,200,.18) 0%,rgba(140,10,170,.28) 100%);color:#B020C8;text-shadow:none;}
/* ── Expand / collapse ── */
.expand-btn{height:30px;width:30px;border-radius:3px;border:1px solid rgba(0,200,192,.35);background:rgba(0,200,192,.06);color:#00DDD4;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .12s;margin-left:6px;flex-shrink:0;}
.expand-btn:hover{border-color:#00DDD4;background:rgba(0,200,192,.14);}
/* ── Abort ── */
.abort-btn{height:34px;padding:0 12px;border-radius:3px;border:1px solid rgba(200,60,60,.35);background:transparent;color:rgba(200,80,80,.6);font-size:9px;font-weight:900;letter-spacing:.12em;text-transform:uppercase;cursor:pointer;transition:all .12s;font-family:'Inter',sans-serif;flex-shrink:0;}
.abort-btn:hover{border-color:#CC2020;color:#CC2020;background:rgba(200,40,40,.07);}
/* ── iOS safe area ── */
@supports(padding:env(safe-area-inset-bottom)){body{padding-bottom:env(safe-area-inset-bottom);}}
/* ── iPad / touch ── */
@media(hover:none){
  .col-btn{min-height:44px;}
  .bb-btn,.abort-btn{min-height:40px;}
  .cmp-open-btn{min-height:40px;}
  .expand-btn{width:40px;height:40px;}
}
@media(max-width:1366px) and (pointer:coarse){
  body{overscroll-behavior:none;}
  .hdr{height:56px;}
  .brand{font-size:32px;}
  .cmp-panel{width:100%;bottom:-100%;height:88vh;}
  .cmp-panel.open{bottom:0;}
}
@media(max-width:768px){.hdr-center{display:none;}}
/* ── 4K / super HD ── */
@media(min-width:2000px){
  html{font-size:125%;}
  .hdr{height:80px;}
  .brand{font-size:60px;}
}
</style>
</head>
<body>
<div class="hdr">
  <div class="brand">GAIN</div>
  <button class="hdr-action" onclick="resetDefaults()">RESET</button>
  <button class="theme-btn" onclick="toggleTheme()" title="Toggle light/dark">◐</button>
  <a href="/proto" class="expand-btn" id="expand-btn" title="Expand to full view">⊞</a>
  <button class="cmp-open-btn" onclick="openCompare()">⊕ COMPARE</button>
  <div class="hdr-center" style="margin-left:auto;">
    <div class="hdr-lbl">Current Settings</div>
    <div class="hdr-vals" id="hdr-vals">—</div>
  </div>
</div>
<div class="stage">
  <div class="col t1">
    <div class="col-accent"></div>
    <div class="col-name">Track 1 — MODE</div>
    <div class="col-flbl">EFFORT</div>
    <div class="col-fader">
      <div class="f-track" id="ft-intensity">
        <div class="f-fill" id="ff-intensity"></div>
        <div class="f-thumb" id="fth-intensity"><div class="f-center"></div></div>
      </div>
    </div>
    <div class="col-fval" id="fv-intensity">0.50</div>
    <div class="col-knob-wrap">
      <div class="col-klbl">THINKING TIME</div>
      <div class="col-knob" id="knob-depth"><div class="knob-body"></div><div class="knob-dot" id="kd-depth"></div></div>
      <div class="col-kval" id="kv-depth">0.50</div>
    </div>
    <div class="col-btns">
      <button class="col-btn" data-field="mode" data-val="EXPLORE" onclick="toggleBtn('mode','EXPLORE')">EXPLORE</button>
      <button class="col-btn mute-btn" id="mbtn-t1" onclick="toggleTrack('t1')">MUTE</button>
      <button class="col-btn" data-field="mode" data-val="BUILD" onclick="toggleBtn('mode','BUILD')">BUILD</button>
    </div>
  </div>
  <div class="col t2">
    <div class="col-accent"></div>
    <div class="col-name">Track 2 — CONFIDENCE</div>
    <div class="col-flbl">CONFIDENCE</div>
    <div class="col-fader">
      <div class="f-track" id="ft-certainty">
        <div class="f-fill" id="ff-certainty"></div>
        <div class="f-thumb" id="fth-certainty"><div class="f-center"></div></div>
      </div>
    </div>
    <div class="col-fval" id="fv-certainty">0.50</div>
    <div class="col-knob-wrap">
      <div class="col-klbl">BOLDNESS</div>
      <div class="col-knob" id="knob-risk"><div class="knob-body"></div><div class="knob-dot" id="kd-risk"></div></div>
      <div class="col-kval" id="kv-risk">0.50</div>
    </div>
    <div class="col-btns">
      <button class="col-btn" data-field="stance" data-val="LIST" onclick="toggleBtn('stance','LIST')">LIST</button>
      <button class="col-btn mute-btn" id="mbtn-t2" onclick="toggleTrack('t2')">MUTE</button>
      <button class="col-btn" data-field="stance" data-val="DECIDE" onclick="toggleBtn('stance','DECIDE')">DECIDE</button>
    </div>
  </div>
  <div class="col t3">
    <div class="col-accent"></div>
    <div class="col-name">Track 3 — SCOPE</div>
    <div class="col-flbl">ZOOM LEVEL</div>
    <div class="col-fader">
      <div class="f-track" id="ft-scope">
        <div class="f-fill" id="ff-scope"></div>
        <div class="f-thumb" id="fth-scope"><div class="f-center"></div></div>
      </div>
    </div>
    <div class="col-fval" id="fv-scope">0.50</div>
    <div class="col-knob-wrap">
      <div class="col-klbl">CONTEXT SIZE</div>
      <div class="col-knob" id="knob-bandwidth"><div class="knob-body"></div><div class="knob-dot" id="kd-bandwidth"></div></div>
      <div class="col-kval" id="kv-bandwidth">0.50</div>
    </div>
    <div class="col-btns">
      <button class="col-btn" data-field="filter" data-val="FILE" onclick="toggleBtn('filter','FILE')">FILE</button>
      <button class="col-btn mute-btn" id="mbtn-t3" onclick="toggleTrack('t3')">MUTE</button>
      <button class="col-btn" data-field="filter" data-val="PROJECT" onclick="toggleBtn('filter','PROJECT')">PROJECT</button>
    </div>
  </div>
  <div class="col t4">
    <div class="col-accent"></div>
    <div class="col-name">Track 4 — VOICE</div>
    <div class="col-flbl">VERBOSITY</div>
    <div class="col-fader">
      <div class="f-track" id="ft-room">
        <div class="f-fill" id="ff-room"></div>
        <div class="f-thumb" id="fth-room"><div class="f-center"></div></div>
      </div>
    </div>
    <div class="col-fval" id="fv-room">0.50</div>
    <div class="col-knob-wrap">
      <div class="col-klbl">MEMORY</div>
      <div class="col-knob" id="knob-decay"><div class="knob-body"></div><div class="knob-dot" id="kd-decay"></div></div>
      <div class="col-kval" id="kv-decay">0.50</div>
    </div>
    <div class="col-btns">
      <button class="col-btn" data-field="voice" data-val="DIRECT" onclick="toggleBtn('voice','DIRECT')">DIRECT</button>
      <button class="col-btn mute-btn" id="mbtn-t4" onclick="toggleTrack('t4')">MUTE</button>
      <button class="col-btn" data-field="voice" data-val="OPEN" onclick="toggleBtn('voice','OPEN')">OPEN</button>
    </div>
  </div>
</div>

<!-- BOTTOM BAR -->
<div class="bottom-bar">
  <div class="bb-col">
    <div class="bb-lbl">PRESETS</div>
    <div class="bb-row">
      <input class="bb-input" id="preset-input" type="text" placeholder="name this state and save it…" maxlength="40" onkeydown="if(event.key==='Enter')savePreset()">
      <button class="bb-btn" onclick="savePreset()">SAVE</button>
    </div>
    <div class="preset-chips" id="preset-chips"><span class="preset-empty">no presets yet</span></div>
  </div>
  <div class="bb-col">
    <div class="bb-lbl">PREVIEW RUN</div>
    <div class="bb-row">
      <input class="bb-input" id="run-input" type="text" placeholder="run a prompt with current settings…" onkeydown="if(event.key==='Enter')runTask()">
      <button class="bb-btn" id="run-btn" onclick="runTask()">RUN</button>
      <button class="abort-btn" id="abort-btn" onclick="abortTask()" title="Stop running task">■ STOP</button>
    </div>
    <div class="run-status" id="run-status"></div>
    <div class="resp-box" id="resp-box"></div>
  </div>
</div>

<!-- COMPARE PANEL -->
<div class="cmp-overlay" id="cmp-overlay" onclick="closeCompare()"></div>
<div class="cmp-panel" id="cmp-panel">
  <div class="cmp-hd">
    <span class="cmp-title">⊕ compare presets</span>
    <div style="display:flex;gap:6px;margin-left:auto;">
      <button class="cmp-tab-btn active" id="cmp-tab-run" onclick="switchCmpTab('run')">RUN</button>
      <button class="cmp-tab-btn" id="cmp-tab-stats" onclick="switchCmpTab('stats')">STATS</button>
    </div>
    <button class="cmp-close" onclick="closeCompare()" style="margin-left:10px;">✕</button>
  </div>
  <div class="cmp-body" id="cmp-body">
    <div class="cmp-save-row">
      <span class="cmp-save-lbl">SAVE CURRENT AS</span>
      <input class="cmp-save-input" id="cmp-save-input" type="text" placeholder="preset name…" maxlength="40" onkeydown="if(event.key==='Enter')savePresetFrom('cmp-save-input','cmp-save-msg')">
      <button class="cmp-save-btn" onclick="savePresetFrom('cmp-save-input','cmp-save-msg')">SAVE</button>
    </div>
    <div class="cmp-save-msg" id="cmp-save-msg"></div>
    <div class="cmp-sel-row">
      <div class="cmp-sel-group"><div class="cmp-sel-lbl">PRESET A</div><select class="cmp-select" id="cmp-select-a"></select></div>
      <div class="cmp-sel-group"><div class="cmp-sel-lbl">PRESET B</div><select class="cmp-select" id="cmp-select-b"></select></div>
    </div>
    <div class="cmp-prompt-row">
      <input class="cmp-prompt-input" id="cmp-prompt" type="text" placeholder="Enter a prompt to run on both presets…" onkeydown="if(event.key==='Enter')runCompare()">
      <button class="cmp-run-btn" id="cmp-run-btn" onclick="runCompare()">RUN</button>
    </div>
    <div class="cmp-status" id="cmp-status"></div>
    <div class="cmp-outputs">
      <div class="cmp-out"><div class="cmp-out-lbl" id="cmp-label-a">PRESET A</div><div class="cmp-out-box" id="cmp-out-a"></div></div>
      <div class="cmp-out"><div class="cmp-out-lbl" id="cmp-label-b">PRESET B</div><div class="cmp-out-box" id="cmp-out-b"></div></div>
    </div>
    <div class="cmp-metrics-wrap" id="cmp-metrics-wrap">
      <div class="cmp-score-hd">SCORECARD</div>
      <div id="cmp-metrics"></div>
      <div class="cmp-summary" id="cmp-summary"></div>
    </div>
  </div>
  <!-- STATS TAB -->
  <div class="cmp-stats-wrap" id="cmp-stats-wrap" style="display:none;">
    <div class="cmp-stats-total" id="cmp-stats-total"><span>0</span> compare runs total</div>
    <div id="cmp-stats-presets"></div>
  </div>
</div>

<script>
const _tok=localStorage.getItem('sb-access-token')||'';
function getToken(){return localStorage.getItem('sb-access-token')||'';}
const THUMB_H=14,FINE_MULT=0.25,KNOB_SENS=0.90,KNOB_DETENT=0.022;
const FADERS={
  intensity:{fill:'ff-intensity',thumb:'fth-intensity',val:'fv-intensity',track:'ft-intensity'},
  certainty:{fill:'ff-certainty',thumb:'fth-certainty',val:'fv-certainty',track:'ft-certainty'},
  scope:    {fill:'ff-scope',    thumb:'fth-scope',    val:'fv-scope',    track:'ft-scope'},
  room:     {fill:'ff-room',     thumb:'fth-room',     val:'fv-room',     track:'ft-room'},
};
const KNOBS={
  depth:    {dot:'kd-depth',    val:'kv-depth'},
  risk:     {dot:'kd-risk',     val:'kv-risk'},
  bandwidth:{dot:'kd-bandwidth',val:'kv-bandwidth'},
  decay:    {dot:'kd-decay',    val:'kv-decay'},
};
let lastState={};
const dragging=new Set();
function getR(id){const e=document.getElementById(id);return e?Math.max(20,e.offsetHeight-THUMB_H):80;}
function setFader(field,v){
  const f=FADERS[field];if(!f)return;
  const r=getR(f.track);
  document.getElementById(f.fill).style.height=(v*100)+'%';
  document.getElementById(f.thumb).style.bottom=(v*r)+'px';
  document.getElementById(f.val).textContent=v.toFixed(2);
}
function setKnob(field,v){
  const k=KNOBS[field];if(!k)return;
  const dot=document.getElementById(k.dot);
  if(dot)dot.style.transform='translateX(-50%) rotate('+((-135+v*270))+'deg)';
  const val=document.getElementById(k.val);
  if(val)val.textContent=v.toFixed(2);
  const el=document.getElementById('knob-'+field);
  if(el){
    const s=225,e=s+v*270;
    const light=document.body.classList.contains('light');
    const dark=light?'#9A9890':'#0A1620';
    const lit=light?'#007E78':'#00DDD4';
    el.style.background=e<=360
      ?'conic-gradient('+dark+' 0deg '+s+'deg,'+lit+' '+s+'deg '+e+'deg,'+dark+' '+e+'deg 360deg)'
      :'conic-gradient('+lit+' 0deg '+(e-360)+'deg,'+dark+' '+(e-360)+'deg '+s+'deg,'+lit+' '+s+'deg 360deg)';
  }
}
function applyState(s){
  lastState=s;
  Object.keys(FADERS).forEach(f=>{if(!dragging.has(f))setFader(f,s[f]??0.5);});
  Object.keys(KNOBS).forEach(f=>{if(!dragging.has(f))setKnob(f,s[f]??0.5);});
  document.querySelectorAll('.col-btn[data-field]').forEach(b=>
    b.classList.toggle('active',b.dataset.val===s[b.dataset.field]));
  ['t1','t2','t3','t4'].forEach(t=>{
    const btn=document.getElementById('mbtn-'+t);
    if(btn)btn.classList.toggle('active',s[t+'_on']===false);
    const col=document.querySelector('.col.'+t);
    if(col)col.classList.toggle('muted',s[t+'_on']===false);
  });
  const tk=(on,l,b,v)=>on===false?l+': OFF':(b?l+': '+b+' · '+v:l+': '+v);
  document.getElementById('hdr-vals').textContent=[
    tk(s.t1_on,'MODE',s.mode,(s.intensity??0.5).toFixed(2)),
    tk(s.t2_on,'CONF',s.stance,(s.certainty??0.5).toFixed(2)),
    tk(s.t3_on,'SCOPE',s.filter,(s.scope??0.5).toFixed(2)),
    tk(s.t4_on,'VOICE',s.voice,(s.room??0.5).toFixed(2)),
  ].join('  ·  ');
}
async function set(f,v){await fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({[f]:v})});}
function toggleBtn(field,val){
  const nv=lastState[field]===val?'':val;
  lastState[field]=nv;
  document.querySelectorAll('.col-btn[data-field="'+field+'"]').forEach(b=>b.classList.toggle('active',b.dataset.val===nv));
  set(field,nv);
}
function toggleTrack(t){
  const key=t+'_on',nv=lastState[key]===false?true:false;
  lastState[key]=nv;
  const btn=document.getElementById('mbtn-'+t);
  if(btn)btn.classList.toggle('active',!nv);
  const col=document.querySelector('.col.'+t);
  if(col)col.classList.toggle('muted',!nv);
  set(key,nv);
}
// Initialize after layout paints so offsetHeight is available
requestAnimationFrame(()=>{
  Object.keys(FADERS).forEach(f=>setFader(f,0.5));
  Object.keys(KNOBS).forEach(f=>setKnob(f,0.5));
});
const es=new EventSource('/stream'+(_tok?'?token='+encodeURIComponent(_tok):''));
es.onmessage=e=>applyState(JSON.parse(e.data));
Object.entries(FADERS).forEach(([field,ids])=>{
  const trackEl=document.getElementById(ids.track);
  const thumbEl=document.getElementById(ids.thumb);
  if(!trackEl||!thumbEl)return;
  const getV=()=>parseFloat(document.getElementById(ids.val).textContent);
  function onDown(e){
    const cap=e.currentTarget;
    e.preventDefault();e.stopPropagation();
    try{cap.setPointerCapture(e.pointerId);}catch(_){}
    dragging.add(field);
    const r=Math.max(20,trackEl.offsetHeight-THUMB_H);
    let prevY=e.clientY;
    function onMove(ev){
      const fine=ev.shiftKey?FINE_MULT:1;
      setFader(field,Math.max(0,Math.min(1,getV()+(-(ev.clientY-prevY)/r*fine))));
      prevY=ev.clientY;
    }
    function onUp(){
      dragging.delete(field);
      cap.removeEventListener('pointermove',onMove);
      cap.removeEventListener('pointerup',onUp);
      cap.removeEventListener('pointercancel',onUp);
      set(field,Math.round(getV()*1000)/1000);
    }
    cap.addEventListener('pointermove',onMove);
    cap.addEventListener('pointerup',onUp);
    cap.addEventListener('pointercancel',onUp);
  }
  trackEl.addEventListener('pointerdown',onDown);
  thumbEl.addEventListener('pointerdown',onDown);
  [trackEl,thumbEl].forEach(el=>el.addEventListener('dblclick',()=>{setFader(field,0.5);set(field,0.5);}));
});
Object.entries(KNOBS).forEach(([field,ids])=>{
  const knobEl=document.getElementById('knob-'+field);
  if(!knobEl)return;
  const getV=()=>parseFloat(document.getElementById(ids.val).textContent);
  function onDown(e){
    e.preventDefault();e.stopPropagation();
    try{knobEl.setPointerCapture(e.pointerId);}catch(_){}
    dragging.add(field);
    let prevY=e.clientY;
    function onMove(ev){
      const dy=ev.clientY-prevY;
      const fine=ev.shiftKey?FINE_MULT*0.8:1;
      let nv=Math.max(0,Math.min(1,getV()+(-dy/120)*KNOB_SENS*fine));
      if(!ev.shiftKey&&Math.abs(nv-0.5)<KNOB_DETENT)nv=0.5;
      setKnob(field,nv);
      prevY=ev.clientY;
    }
    function onUp(){
      dragging.delete(field);
      knobEl.removeEventListener('pointermove',onMove);
      knobEl.removeEventListener('pointerup',onUp);
      knobEl.removeEventListener('pointercancel',onUp);
      set(field,Math.round(getV()*1000)/1000);
    }
    knobEl.addEventListener('pointermove',onMove);
    knobEl.addEventListener('pointerup',onUp);
    knobEl.addEventListener('pointercancel',onUp);
  }
  knobEl.addEventListener('pointerdown',onDown);
  knobEl.addEventListener('dblclick',()=>{setKnob(field,0.5);set(field,0.5);});
});
// ── Presets ────────────────────────────────────────────────────────
async function savePreset(){
  const inp=document.getElementById('preset-input');
  const name=(inp?inp.value:'').trim();
  if(!name){if(inp)inp.focus();return;}
  await fetch('/presets/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  if(inp)inp.value='';
  loadPresets();
}
async function savePresetFrom(inputId,msgId){
  const inp=document.getElementById(inputId);
  const name=(inp?inp.value:'').trim();
  if(!name){if(inp)inp.focus();return;}
  await fetch('/presets/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  if(inp)inp.value='';
  const m=document.getElementById(msgId);
  if(m){m.textContent='Saved: '+name;setTimeout(()=>m.textContent='',2200);}
  loadPresets();
}
async function loadPreset(name){
  await fetch('/presets/load',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
}
async function deletePreset(name){
  await fetch('/presets/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  loadPresets();
}
async function loadPresets(){
  let presets=[];
  try{const r=await fetch('/presets');presets=await r.json();}catch(_){}
  const chips=document.getElementById('preset-chips');
  if(chips){
    if(!presets.length){chips.innerHTML='<span class="preset-empty">no presets yet</span>';}
    else{chips.innerHTML=presets.map(p=>`<div class="preset-chip"><span class="preset-chip-name" onclick="loadPreset('${p.name.replace(/'/g,"\\\'")}')">${p.name}</span><button class="preset-chip-del" onclick="deletePreset('${p.name.replace(/'/g,"\\\'")}')" title="delete">✕</button></div>`).join('');}
  }
  ['cmp-select-a','cmp-select-b'].forEach((id,i)=>{
    const sel=document.getElementById(id);if(!sel)return;
    const prev=sel.value;
    sel.innerHTML='<option value="__current__">— Current Settings —</option>'+presets.map(p=>`<option value="${p.name}">${p.name}</option>`).join('');
    if(prev&&[...sel.options].some(o=>o.value===prev))sel.value=prev;
    else if(i===1&&presets.length)sel.value=presets[0].name;
  });
}
function resetDefaults(){
  const n={intensity:0.5,depth:0.5,certainty:0.5,risk:0.5,scope:0.5,bandwidth:0.5,room:0.5,decay:0.5,mode:'',stance:'',filter:'',voice:'',t1_on:true,t2_on:true,t3_on:true,t4_on:true};
  fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(n)});
}
// ── Preview Run ─────────────────────────────────────────────────────
async function runTask(){
  const inp=document.getElementById('run-input');
  const task=(inp?inp.value:'').trim();
  if(!task){if(inp)inp.focus();return;}
  const btn=document.getElementById('run-btn');
  const status=document.getElementById('run-status');
  const box=document.getElementById('resp-box');
  if(btn)btn.disabled=true;
  if(status)status.textContent='● RUNNING…';
  if(box){box.textContent='';box.classList.remove('has-content');}
  try{
    const resp=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task})});
    const reader=resp.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\\n');buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data:'))continue;
        try{
          const d=JSON.parse(line.slice(5).trim());
          if(d.text&&box){box.textContent+=d.text;box.classList.add('has-content');}
          if(d.done&&status)status.textContent='';
          if(d.error&&status)status.textContent='✕ '+d.error;
        }catch(_){}
      }
    }
  }catch(e){if(status)status.textContent='✕ '+e.message;}
  if(btn)btn.disabled=false;
}
// ── Compare ─────────────────────────────────────────────────────────
function openCompare(){loadPresets();document.getElementById('cmp-panel').classList.add('open');document.getElementById('cmp-overlay').classList.add('open');}
function closeCompare(){document.getElementById('cmp-panel').classList.remove('open');document.getElementById('cmp-overlay').classList.remove('open');}

function switchCmpTab(tab) {
  const runBody  = document.getElementById('cmp-body') || document.querySelector('.cmp-body');
  const statsWrap = document.getElementById('cmp-stats-wrap');
  const tabRun   = document.getElementById('cmp-tab-run');
  const tabStats = document.getElementById('cmp-tab-stats');
  if (tab === 'stats') {
    if (runBody)   runBody.style.display = 'none';
    if (statsWrap) statsWrap.style.display = '';
    if (tabRun)    tabRun.classList.remove('active');
    if (tabStats)  tabStats.classList.add('active');
    loadCmpStats();
  } else {
    if (runBody)   runBody.style.display = '';
    if (statsWrap) statsWrap.style.display = 'none';
    if (tabRun)    tabRun.classList.add('active');
    if (tabStats)  tabStats.classList.remove('active');
  }
}

async function loadCmpStats() {
  const presetsEl = document.getElementById('cmp-stats-presets');
  const totalEl   = document.getElementById('cmp-stats-total');
  if (!presetsEl) return;
  presetsEl.innerHTML = '<div class="cmp-stats-empty">Loading…</div>';
  try {
    const r = await fetch('/compare/stats');
    const d = await r.json();
    if (totalEl) totalEl.innerHTML = '<span>' + (d.runs || 0) + '</span> compare runs total';
    const presets = Object.entries(d.presets || {});
    if (!presets.length) { presetsEl.innerHTML = '<div class="cmp-stats-empty">No compare runs yet. Run a comparison first.</div>'; return; }
    const METRICS = ['adherence','depth','clarity','efficiency','confidence'];
    const LABELS  = {adherence:'Adherence',depth:'Depth',clarity:'Clarity',efficiency:'Efficiency',confidence:'Confidence'};
    presetsEl.innerHTML = presets.sort((a,b)=>b[1].runs-a[1].runs).map(([name, s]) => {
      const winPct = s.runs ? Math.round(s.wins / s.runs * 100) : 0;
      const bars = METRICS.map(m => {
        const v = s[m] || 0;
        return '<div class="cmp-stat-row">' +
          '<span class="cmp-stat-lbl">' + LABELS[m] + '</span>' +
          '<div class="cmp-stat-bar"><div class="cmp-stat-fill" style="width:' + v + '%"></div></div>' +
          '<span class="cmp-stat-val">' + v + '</span>' +
        '</div>';
      }).join('');
      return '<div class="cmp-preset-card">' +
        '<div class="cmp-preset-hdr">' +
          '<span class="cmp-preset-name">' + esc(name) + '</span>' +
          '<span class="cmp-preset-runs">' + s.runs + ' run' + (s.runs!==1?'s':'') + '</span>' +
          '<span class="cmp-preset-win">' + winPct + '% wins</span>' +
        '</div>' +
        '<div class="cmp-preset-body">' + bars + '</div>' +
      '</div>';
    }).join('');
  } catch(e) {
    presetsEl.innerHTML = '<div class="cmp-stats-empty">Failed to load stats.</div>';
  }
}
async function runCompare(){
  const prompt=(document.getElementById('cmp-prompt').value||'').trim();
  const pa=document.getElementById('cmp-select-a').value;
  const pb=document.getElementById('cmp-select-b').value;
  if(!prompt||!pa||!pb)return;
  const btn=document.getElementById('cmp-run-btn');
  const status=document.getElementById('cmp-status');
  const outA=document.getElementById('cmp-out-a');
  const outB=document.getElementById('cmp-out-b');
  const metricsWrap=document.getElementById('cmp-metrics-wrap');
  const metrics=document.getElementById('cmp-metrics');
  const summary=document.getElementById('cmp-summary');
  if(btn)btn.disabled=true;
  if(outA)outA.textContent='';if(outB)outB.textContent='';
  if(metricsWrap)metricsWrap.classList.remove('visible');
  if(metrics)metrics.innerHTML='';if(summary)summary.textContent='';
  document.getElementById('cmp-label-a').textContent=pa==='__current__'?'CURRENT SETTINGS':pa.toUpperCase();
  document.getElementById('cmp-label-b').textContent=pb==='__current__'?'CURRENT SETTINGS':pb.toUpperCase();
  try{
    const _cmpTok=getToken();const _cmpHdrs={'Content-Type':'application/json'};
    if(_cmpTok)_cmpHdrs['Authorization']='Bearer '+_cmpTok;
    const resp=await fetch('/compare',{method:'POST',headers:_cmpHdrs,body:JSON.stringify({preset_a:pa,preset_b:pb,prompt})});
    const reader=resp.body.getReader();const dec=new TextDecoder();let buf='';
    while(true){
      const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\\n');buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data:'))continue;
        try{
          const d=JSON.parse(line.slice(5).trim());
          if(d.phase==='a_start'&&status)status.textContent='running preset A…';
          if(d.phase==='b_start'&&status)status.textContent='running preset B…';
          if(d.phase==='scoring'&&status)status.textContent='scoring…';
          if(d.phase==='a'&&d.text&&outA)outA.textContent+=d.text;
          if(d.phase==='b'&&d.text&&outB)outB.textContent+=d.text;
          if(d.phase==='scores'){
            if(status)status.textContent='';
            const s=d.scores;
            const METRICS=['adherence','depth','clarity','efficiency','confidence','token_efficiency'];
            if(metrics){
              metrics.innerHTML=METRICS.filter(m=>s[m]).map(m=>{
                const va=s[m].a,vb=s[m].b,w=s[m].winner;
                return`<div class="cmp-metric-row"><span class="cmp-metric-name">${m.replace('_',' ')}</span><div class="cmp-bars"><div class="cmp-bar cmp-bar-a"><div class="cmp-bar-fill" style="width:${va}%"></div></div><div class="cmp-bar cmp-bar-b"><div class="cmp-bar-fill" style="width:${vb}%"></div></div></div><span class="cmp-winner">${w==='a'?'A':w==='b'?'B':'—'}</span></div>`;
              }).join('');
            }
            if(s.summary&&summary)summary.textContent=s.summary;
            if(metricsWrap)metricsWrap.classList.add('visible');
          }
          if(d.phase==='done'&&status)status.textContent='';
          if(d.error&&status)status.textContent='✕ '+d.error;
        }catch(_){}
      }
    }
  }catch(e){if(status)status.textContent='✕ '+e.message;}
  if(btn)btn.disabled=false;
}
async function abortTask(){
  try{await fetch('/abort',{method:'POST'});}catch(e){}
}
function toggleCompact(){
  window.location.href='/proto';
}
loadPresets();
(function(){if(localStorage.getItem('gain_theme')==='light')document.body.classList.add('light');})();
function toggleTheme(){const l=document.body.classList.toggle('light');localStorage.setItem('gain_theme',l?'light':'dark');}
</script>
</body>
</html>"""


# ── HTML (legacy — served at /proto for rollback) ─────────────────────────────

HTML_LEGACY = r"""<!DOCTYPE html>
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
html{font-size:110%;}
body{
  background:var(--bg);
  background-image:radial-gradient(rgba(0,196,232,.04) 1px,transparent 1px);
  background-size:28px 28px;
  font-family:'Inter',sans-serif;color:var(--text);
  height:100vh;display:flex;flex-direction:column;user-select:none;overflow:hidden;
}

/* ── HEADER ─────────────────────────────────────────────── */
.hdr{
  padding:0 20px;
  border-bottom:1px solid var(--border);
  background:#030507;
  background-image:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.06) 2px,rgba(0,0,0,.06) 3px);
  display:flex;align-items:center;
  flex-shrink:0;height:96px;
  box-shadow:0 1px 0 rgba(0,200,192,.1);
  position:relative;
}
.brand{
  font-family:'Abril Fatface',serif;font-size:72px;letter-spacing:.06em;line-height:96px;
  background:linear-gradient(130deg,#00E8FF 0%,#A0C8FF 50%,#C0A0FF 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  filter:drop-shadow(0 0 6px rgba(0,200,255,.55)) drop-shadow(0 0 14px rgba(160,100,255,.3));
  flex-shrink:0;padding-right:8px;
}
.hdr-center{
  position:absolute;left:50%;transform:translateX(-50%);
  display:flex;flex-direction:column;align-items:center;gap:4px;
  pointer-events:none;
}
.hdr-settings-label{
  font-size:9px;font-weight:800;letter-spacing:.22em;text-transform:uppercase;
  color:var(--accent);opacity:.9;
  text-shadow:0 0 10px rgba(0,220,212,.35);
}
.hdr-settings{
  font-size:8px;font-weight:700;color:#C8DCEA;
  letter-spacing:.04em;font-variant-numeric:tabular-nums;
  white-space:nowrap;text-align:center;
  text-shadow:0 0 8px rgba(0,200,192,.2);
}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:8px;}
.reset-btn{
  height:32px;padding:0 14px;border-radius:4px;
  border:1px solid rgba(0,200,192,.45);background:rgba(0,200,192,.07);
  color:var(--accent2);font-size:9px;font-weight:800;letter-spacing:.12em;
  text-transform:uppercase;cursor:pointer;transition:all .15s;white-space:nowrap;
  text-shadow:0 0 8px rgba(0,220,212,.4);
  flex-shrink:0;touch-action:manipulation;
}
.reset-btn:hover{background:rgba(0,200,192,.16);border-color:var(--accent2);}
.faq-btn{
  width:32px;height:32px;border-radius:50%;
  border:1px solid rgba(0,200,192,.4);background:rgba(0,200,192,.07);
  color:var(--accent2);font-size:14px;font-weight:800;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:all .15s;line-height:1;flex-shrink:0;touch-action:manipulation;
}
.faq-btn:hover{background:rgba(0,200,192,.16);color:#fff;border-color:var(--accent2);}
.settings-btn{
  width:32px;height:32px;border-radius:50%;
  border:1px solid rgba(0,200,192,.4);background:rgba(0,200,192,.07);
  color:var(--accent2);font-size:14px;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:all .15s;line-height:1;flex-shrink:0;touch-action:manipulation;
}
.settings-btn:hover{background:rgba(0,200,192,.16);color:#fff;border-color:var(--accent2);}
/* theme-btn hidden from header, lives in settings panel only */
.theme-btn{display:none;}
/* ── Settings panel ── */
.settings-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;}
.settings-overlay.open{display:block;}
.settings-panel{position:fixed;right:-360px;top:0;bottom:0;width:320px;background:var(--panel);z-index:101;transition:right .26s cubic-bezier(.4,0,.2,1);border-left:2px solid var(--accent);display:flex;flex-direction:column;box-shadow:-8px 0 40px rgba(0,0,0,.8);}
.settings-panel.open{right:0;}
.settings-hd{padding:13px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;background:#040608;}
.settings-title{font-family:'Abril Fatface',serif;font-size:20px;color:var(--accent2);text-shadow:0 0 20px rgba(0,232,224,.4);}
.settings-close{width:26px;height:26px;border:1px solid var(--border2);background:transparent;cursor:pointer;font-size:13px;color:var(--text2);border-radius:50%;transition:background .12s;display:flex;align-items:center;justify-content:center;font-weight:700;}
.settings-close:hover{background:var(--panel2);color:var(--accent2);}
.settings-body{padding:24px 18px;flex:1;display:flex;flex-direction:column;gap:20px;}
.settings-row{display:flex;flex-direction:column;gap:8px;padding-bottom:20px;border-bottom:1px solid var(--border);}
.settings-row-label{font-size:9px;font-weight:800;letter-spacing:.2em;text-transform:uppercase;color:var(--accent2);opacity:.7;}
.settings-val{font-size:13px;color:var(--text2);}
.settings-action{height:36px;padding:0 16px;border-radius:4px;border:1.5px solid rgba(0,200,192,.4);background:rgba(0,200,192,.07);color:var(--accent2);font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;cursor:pointer;transition:all .15s;width:100%;}
.settings-action:hover{background:rgba(0,200,192,.16);border-color:var(--accent2);}
.settings-action.danger{border-color:rgba(220,60,60,.4);color:#FF6060;background:rgba(200,40,40,.06);}
.settings-action.danger:hover{background:rgba(200,40,40,.14);border-color:#FF6060;}

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
body.light .ch-hdr-row{
  background:linear-gradient(90deg,#A8C8D8,#B8D4E0);
  border-bottom-color:rgba(0,120,150,.3);
  box-shadow:0 1px 4px rgba(0,80,120,.15);
}
body.light .ch-id{color:var(--text2);}
body.light .fader-lbl{color:var(--chrome);}
body.light .fader-val{color:var(--accent);text-shadow:none;}
body.light .fader-track{
  background:rgba(200,196,188,.45);
  border-color:rgba(0,120,115,.2);
  overflow:hidden;
  box-shadow:inset 0 2px 8px rgba(0,0,0,.08);
}
body.light .fader-fill{
  background:linear-gradient(0deg,rgba(0,118,112,.88) 0%,rgba(0,148,142,.68) 45%,rgba(0,172,165,.45) 80%,rgba(0,192,185,.22) 100%);
  box-shadow:0 0 12px rgba(0,140,134,.25);
}
body.light .ch.t2 .fader-fill,body.light .ch.t4 .fader-fill{
  background:linear-gradient(0deg,rgba(98,38,188,.78) 0%,rgba(122,62,208,.58) 45%,rgba(146,94,224,.38) 80%,rgba(166,122,238,.18) 100%);
  box-shadow:0 0 12px rgba(109,40,217,.18);
}
body.light .ch.t1 .fader-thumb,body.light .ch.t3 .fader-thumb{
  background:linear-gradient(90deg,transparent,rgba(0,158,150,.8) 20%,rgba(0,196,188,1) 50%,rgba(0,158,150,.8) 80%,transparent);
  box-shadow:0 0 6px rgba(0,148,140,.7);
}
body.light .ch.t2 .fader-thumb,body.light .ch.t4 .fader-thumb{
  background:linear-gradient(90deg,transparent,rgba(128,68,208,.8) 20%,rgba(152,100,228,1) 50%,rgba(128,68,208,.8) 80%,transparent);
  box-shadow:0 0 6px rgba(109,40,217,.6);
}
body.light .knob{
  background:conic-gradient(#9A9890 0deg 225deg,#9A9890 225deg 360deg);
  box-shadow:
    0 4px 12px rgba(0,0,0,.30),
    0 0 0 1px rgba(0,0,0,.20),
    0 0 0 2px rgba(255,255,255,.6),
    inset 0 1px 0 rgba(255,255,255,.4);
}
body.light .knob-body{
  background:radial-gradient(circle at 32% 28%,#F0EDE8 0%,#D8D4CE 22%,#B8B4AC 50%,#A0A098 78%,#909088 100%);
  border-color:rgba(0,0,0,.25);
  box-shadow:
    inset 0 4px 8px rgba(255,255,255,.75),
    inset 0 -4px 10px rgba(0,0,0,.28),
    inset 0 0 20px rgba(0,0,0,.10);
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
body.light .hero-tagline{
  color:#2A1400;
  text-shadow:
    0 0 1px rgba(255,240,80,1),
    0 0 3px rgba(255,220,40,1),
    0 0 8px rgba(255,190,0,1),
    0 0 16px rgba(255,160,0,.85),
    0 0 30px rgba(255,120,0,.6),
    0 0 55px rgba(255,80,0,.3),
    0 0 90px rgba(255,60,0,.15);
  -webkit-font-smoothing:antialiased;
}
body.light .hero-brand{color:rgba(0,100,90,.06);}
body.light .copy-btn{background:rgba(240,237,232,.9);border-color:var(--border2);}
body.light .history-item{border-color:var(--border);}
body.light .hc-time,.body.light .hc-mode,.body.light .hc-peek{color:var(--text3);}
body.light .preset-empty{color:var(--text3);}
body.light .preset-name{color:var(--text);}
body.light .api-tag{background:rgba(0,126,120,.06);border-color:rgba(0,126,120,.2);color:var(--accent);}
body.light .fader-track.pickup{border-color:rgba(200,130,0,.4);box-shadow:inset 0 2px 6px rgba(0,0,0,.15),0 0 0 1px rgba(200,130,0,.15);}

/* ── LIGHT THEME — BRIGHT GLOWING TEXT ─────────────────── */
body.light .hdr-settings-label{
  color:var(--accent2);
  text-shadow:0 0 10px rgba(0,158,150,.5),0 0 20px rgba(0,158,150,.25);
}
body.light .hdr-settings{
  font-size:13px;
  color:#0A2030;
  text-shadow:0 0 6px rgba(0,120,140,.25);
  font-weight:800;
  letter-spacing:.03em;
}
body.light .bank-hd{
  color:#006860;
  text-shadow:0 0 6px rgba(0,158,150,.35);
  background:#D8D4CE;
}
body.light .ch-id{
  color:#002838;
  text-shadow:0 1px 0 rgba(255,255,255,.4);
  font-weight:900;
}
body.light .fader-lbl{
  font-size:12px;
  color:#002820;
  text-shadow:
    0 0 1px rgba(0,220,200,1),
    0 0 4px rgba(0,210,190,.95),
    0 0 10px rgba(0,190,170,.7),
    0 0 22px rgba(0,170,150,.4),
    0 0 40px rgba(0,150,130,.18);
  font-weight:900;
  letter-spacing:.18em;
  -webkit-font-smoothing:antialiased;
}
body.light .fader-val{
  font-size:14px;
  color:#001A14;
  text-shadow:
    0 0 1px rgba(0,240,220,1),
    0 0 4px rgba(0,220,200,1),
    0 0 10px rgba(0,200,180,.85),
    0 0 20px rgba(0,180,160,.55),
    0 0 38px rgba(0,160,140,.25);
  font-weight:900;
  letter-spacing:.08em;
  -webkit-font-smoothing:antialiased;
}
body.light .knob-lbl{
  font-size:10px;
  color:#002820;
  text-shadow:
    0 0 1px rgba(0,220,200,1),
    0 0 4px rgba(0,210,190,.95),
    0 0 10px rgba(0,190,170,.7),
    0 0 22px rgba(0,170,150,.4),
    0 0 40px rgba(0,150,130,.18);
  font-weight:900;
  letter-spacing:.14em;
  text-align:center;
  -webkit-font-smoothing:antialiased;
}
body.light .knob-val{
  font-size:13px;
  color:#001A14;
  text-shadow:
    0 0 1px rgba(0,240,220,1),
    0 0 4px rgba(0,220,200,1),
    0 0 10px rgba(0,200,180,.85),
    0 0 20px rgba(0,180,160,.55),
    0 0 38px rgba(0,160,140,.25);
  font-weight:900;
  letter-spacing:.08em;
  -webkit-font-smoothing:antialiased;
}
body.light .section-hd{
  color:#005058;
  text-shadow:0 0 8px rgba(0,130,150,.35);
  font-weight:900;
  letter-spacing:.2em;
}
body.light .meter-val{
  color:#007E78;
  text-shadow:0 0 8px rgba(0,158,150,.5);
  font-weight:800;
}
body.light .meter-lvl{
  color:#006860;
  text-shadow:0 0 5px rgba(0,140,130,.3);
  font-weight:800;
}
body.light .ch-btn{
  color:rgba(200,235,245,.85);
  border-color:rgba(160,210,230,.35);
  background:rgba(100,180,200,.08);
  font-weight:800;
  letter-spacing:.1em;
  text-shadow:0 0 4px rgba(160,220,240,.3);
}
body.light .ch-btn:hover{
  color:#ffffff;
  border-color:rgba(0,220,212,.6);
  text-shadow:0 0 8px rgba(0,220,212,.6);
  background:rgba(0,180,170,.15);
}
body.light .ch-btn.active{
  color:#001A14;
  border-color:var(--accent);
  background:rgba(0,126,120,.14);
  text-shadow:
    0 0 1px rgba(0,240,220,1),
    0 0 5px rgba(0,210,190,.9),
    0 0 14px rgba(0,190,170,.55),
    0 0 28px rgba(0,170,150,.25);
  font-weight:900;
}
body.light .ch-btn.mute-btn{
  color:rgba(255,160,160,.8);
  border-color:rgba(200,60,60,.35);
  background:rgba(180,30,30,.08);
}
body.light .ch-btn.mute-btn:hover{
  color:#FF8080;
  border-color:rgba(220,60,60,.65);
  background:rgba(180,30,30,.14);
  text-shadow:0 0 8px rgba(255,80,80,.5);
}
body.light .ch-btn.mute-btn.active{
  color:#FF4444;
  border-color:rgba(220,40,40,.7);
  background:rgba(160,20,20,.18);
  text-shadow:0 0 6px rgba(255,60,60,.9),0 0 16px rgba(220,40,40,.5);
}
body.light .pill{
  color:#1A3A50;
  border-color:rgba(0,100,120,.2);
  text-shadow:0 0 4px rgba(0,100,120,.15);
}
body.light .pill.active{
  color:#007E78;
  text-shadow:0 0 8px rgba(0,158,150,.5);
}
body.light .preset-name{
  color:#1A3A50;
  text-shadow:0 0 4px rgba(0,80,120,.15);
  font-weight:600;
}
body.light .hc-time{
  color:#5A7898 !important;
  text-shadow:0 0 4px rgba(0,80,120,.15);
}
body.light .brand{
  filter:drop-shadow(0 0 8px rgba(0,120,160,.35)) drop-shadow(0 0 18px rgba(0,80,160,.15));
}
body.light .reset-btn{
  color:#007E78;
  text-shadow:0 0 6px rgba(0,158,150,.4);
  font-weight:800;
}
body.light .launch-btn{
  color:#007E78;
  text-shadow:0 0 8px rgba(0,158,150,.5);
  font-weight:800;
}
body.light .abort-btn{
  text-shadow:0 0 5px rgba(180,30,30,.3);
}
body.light .panel-hd{
  color:#005058;
  text-shadow:0 0 6px rgba(0,130,150,.3);
  font-weight:900;
}

/* ── HERO ───────────────────────────────────────────────── */
.hero{position:relative;flex-shrink:0;height:108px;overflow:hidden;border-bottom:1px solid var(--border);box-shadow:0 1px 0 rgba(0,200,192,.12);}
.hero svg{position:absolute;inset:0;width:100%;height:100%;}
.hero-left{position:absolute;left:22px;top:50%;transform:translateY(-52%);pointer-events:none;z-index:1;}
.hero-brand{font-family:'Abril Fatface',serif;font-size:62px;color:rgba(0,200,192,.06);line-height:1;}
.hero-tagline{font-size:11px;font-weight:700;letter-spacing:.28em;color:#FFF8E0;text-transform:uppercase;margin-top:4px;text-shadow:0 0 3px #FFF,0 0 8px #FFE040,0 0 16px #FFB020,0 0 28px #FF5500,0 0 50px rgba(217,70,239,.4),0 0 80px rgba(139,92,246,.2);}

/* ── MAIN 3-COLUMN CONSOLE ──────────────────────────────── */
.console{flex:1;display:flex;overflow:hidden;min-height:0;}

/* ── COMPACT MODE ───────────────────────────────────────── */
.console.compact .monitor{display:none;}
.console.compact .channel-bank{width:auto;flex:1;}
.console.compact .channel-bank.right{border-left:1px solid var(--border);}

/* Fader — tank takes all available space */
.console.compact .fader-wrap{max-height:none;}
.console.compact .fader-lbl{font-size:16px;letter-spacing:.1em;font-weight:900;}
.console.compact .fader-val{font-size:16px;font-weight:900;}
.console.compact .fader-track{width:calc(100% - 16px);overflow:hidden;}
.console.compact .fader-thumb{width:100%;height:4px;left:0;transform:none;box-shadow:none;border:none;background:none;}

/* Knob — compact, secondary to fader */
.console.compact .knob-wrap{padding:4px 0 3px;border-top:1px solid rgba(0,200,192,.14);}
.console.compact .knob{width:42px;height:42px;}
.console.compact .knob-body{inset:5px;}
.console.compact .knob-dot{width:3px;height:12px;top:5px;transform-origin:50% 16px;}
.console.compact .knob-lbl{font-size:8px;letter-spacing:.08em;font-weight:800;}
.console.compact .knob-val{font-size:9px;font-weight:900;}

/* Buttons */
.console.compact .ch-btns{margin-top:4px;gap:4px;padding-top:4px;border-top:1px solid rgba(0,200,192,.1);}
.console.compact .ch-btn{height:30px;font-size:10px;letter-spacing:.1em;}

/* Track headers and misc */
.console.compact .bank-hd{font-size:12px;padding:6px 16px;}
.console.compact .ch{padding:14px 12px 10px;gap:0;}
.console.compact .ch-id{font-size:15px;letter-spacing:.08em;}
.console.compact .ch-pwr{width:28px;height:28px;font-size:13px;}

/* Collapse tab — sits between left and right bank in compact mode */
.collapse-tab{
  width:32px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;
  background:rgba(0,200,192,.06);
  border-left:1px solid rgba(0,200,192,.3);
  border-right:1px solid rgba(0,200,192,.3);
  transition:all .15s;
  position:relative;
}
.collapse-tab:hover{
  background:rgba(0,200,192,.16);
  border-color:rgba(0,220,212,.6);
}
.collapse-tab .tab-arrow{
  font-size:9px;color:var(--accent2);
  writing-mode:vertical-rl;
  letter-spacing:.22em;
  text-shadow:0 0 8px rgba(0,220,212,.8),0 0 18px rgba(0,220,212,.4);
  user-select:none;
  font-weight:900;
}
.console:not(.compact) .collapse-tab{display:none;}
.console.compact .collapse-tab{display:flex;}

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
  width:calc(100% + 12px);margin-left:-6px;margin-top:-4px;margin-bottom:6px;
  padding:3px 8px;flex-shrink:0;
  background:rgba(0,20,36,.7);
  border-bottom:1px solid rgba(0,160,180,.12);
}
.ch-id{
  font-size:9px;font-weight:900;letter-spacing:.16em;
  color:#C0D8EE;text-transform:uppercase;
  text-shadow:0 0 8px rgba(0,180,200,.25);
}
.ch-pwr{
  width:18px;height:18px;border-radius:2px;
  border:1px solid rgba(0,210,80,.5);
  background:rgba(0,180,60,.12);
  color:#00E050;
  font-size:9px;font-weight:900;letter-spacing:0;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:all .15s;flex-shrink:0;line-height:1;
  padding:0;
  box-shadow:0 0 6px rgba(0,210,80,.35),inset 0 0 4px rgba(0,210,80,.1);
  text-shadow:0 0 6px rgba(0,240,100,.9),0 0 12px rgba(0,210,80,.5);
}
.ch-pwr:hover{
  border-color:rgba(0,230,100,.8);
  color:#40FF80;
  background:rgba(0,200,70,.2);
  box-shadow:0 0 10px rgba(0,220,80,.55),inset 0 0 6px rgba(0,220,80,.15);
  text-shadow:0 0 8px rgba(0,255,100,1),0 0 18px rgba(0,220,80,.6);
}
.ch-pwr.off{
  border-color:rgba(220,40,40,.6);
  background:rgba(180,20,20,.14);
  color:#FF4040;
  box-shadow:0 0 6px rgba(220,40,40,.4),inset 0 0 4px rgba(200,30,30,.1);
  text-shadow:0 0 6px rgba(255,60,60,.9),0 0 14px rgba(220,40,40,.5);
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
  flex:1;min-height:100px;
  width:100%;gap:3px;
}
.fader-lbl{font-size:11px;color:#C8E0F0;font-weight:900;letter-spacing:.12em;text-transform:uppercase;flex-shrink:0;text-shadow:0 0 10px rgba(0,200,192,.35);}

/* the rail assembly */
.fader-rail{
  position:relative;
  width:100%;flex:1;min-height:60px;
  display:flex;justify-content:center;align-items:stretch;
}
/* tick marks — hidden, tank is the visual */
.fader-ticks{display:none;}
.tick,.tick-line{display:none;}

/* the tank — full-width liquid container */
.fader-track{
  width:calc(100% - 8px);flex:1;
  background:rgba(2,8,18,.92);
  border:1px solid rgba(0,180,172,.22);
  border-radius:5px;
  position:relative;
  cursor:ns-resize;
  touch-action:none;
  overflow:hidden;
  box-shadow:inset 0 0 0 1px rgba(0,180,172,.06),inset 0 6px 24px rgba(0,0,0,.9),0 0 0 1px rgba(0,0,0,.6);
}
.ch.t2 .fader-track,.ch.t4 .fader-track{
  border-color:rgba(139,92,246,.22);
  box-shadow:inset 0 0 0 1px rgba(139,92,246,.06),inset 0 6px 24px rgba(0,0,0,.9),0 0 0 1px rgba(0,0,0,.6);
}

/* Liquid fill — rises from bottom like fluid in a tank */
.fader-fill{
  position:absolute;bottom:0;left:0;right:0;
  border-radius:0;
  pointer-events:none;
  background:linear-gradient(0deg,
    rgba(0,148,140,.92) 0%,
    rgba(0,182,175,.76) 35%,
    rgba(0,212,204,.54) 70%,
    rgba(0,235,228,.32) 90%,
    rgba(100,255,250,.14) 100%
  );
  box-shadow:
    0 0 22px rgba(0,200,192,.42),
    0 0 55px rgba(0,180,175,.14),
    inset 0 0 28px rgba(0,160,155,.08);
}
/* T2/T4 channels: purple liquid */
.ch.t2 .fader-fill,.ch.t4 .fader-fill{
  background:linear-gradient(0deg,
    rgba(88,28,180,.92) 0%,
    rgba(118,60,220,.76) 35%,
    rgba(148,96,240,.54) 70%,
    rgba(175,132,.255,.32) 90%,
    rgba(210,182,255,.14) 100%
  );
  box-shadow:
    0 0 22px rgba(139,92,246,.42),
    0 0 55px rgba(139,92,246,.14),
    inset 0 0 28px rgba(100,58,200,.08);
}
/* Active drag: boost fill brightness slightly */
.fader-track.dragging .fader-fill,
.fader-track.value-active .fader-fill{
  box-shadow:0 0 28px rgba(0,200,192,.6),0 0 60px rgba(0,180,175,.2),inset 0 0 30px rgba(0,160,155,.1);
}
.ch.t2 .fader-track.dragging .fader-fill,
.ch.t2 .fader-track.value-active .fader-fill,
.ch.t4 .fader-track.dragging .fader-fill,
.ch.t4 .fader-track.value-active .fader-fill{
  box-shadow:0 0 28px rgba(139,92,246,.6),0 0 60px rgba(139,92,246,.2),inset 0 0 30px rgba(100,58,200,.1);
}

/* liquid surface line — the thumb is now just a glowing horizon */
.fader-thumb{
  position:absolute;
  width:100%;height:4px;
  left:0;transform:none;
  cursor:ns-resize;z-index:3;touch-action:none;
  border-radius:2px;
}
/* t1/t3 = teal surface */
.ch.t1 .fader-thumb,.ch.t3 .fader-thumb{
  background:linear-gradient(90deg,transparent,rgba(0,228,220,.65) 18%,rgba(190,255,252,.92) 50%,rgba(0,228,220,.65) 82%,transparent);
  box-shadow:0 0 10px rgba(0,220,212,1),0 0 22px rgba(0,200,192,.65);
}
/* t2/t4 = purple surface */
.ch.t2 .fader-thumb,.ch.t4 .fader-thumb{
  background:linear-gradient(90deg,transparent,rgba(158,128,250,.65) 18%,rgba(224,198,255,.92) 50%,rgba(158,128,250,.65) 82%,transparent);
  box-shadow:0 0 10px rgba(167,139,250,1),0 0 22px rgba(139,92,246,.65);
}
.fader-thumb::before,.fader-thumb::after{display:none;}
.fader-thumb .thumb-center{display:none;}

.fader-val{
  font-size:13px;font-weight:900;color:var(--accent);
  font-variant-numeric:tabular-nums;flex-shrink:0;
  text-shadow:0 0 8px rgba(0,200,192,.5);
  letter-spacing:.05em;
}

/* ── HARDWARE KNOB ──────────────────────────────────────── */
.knob-wrap{
  display:flex;flex-direction:column;align-items:center;
  gap:2px;width:100%;flex-shrink:0;
  padding:4px 0 3px;
  border-top:1px solid var(--border);
}
.knob-lbl{font-size:7px;color:#7A9AB8;font-weight:700;letter-spacing:.1em;text-transform:uppercase;}
.knob{
  width:36px;height:36px;border-radius:50%;
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
  position:absolute;inset:4px;border-radius:50%;
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
  width:2px;height:10px;
  background:linear-gradient(180deg,#FFFFFF 0%,#A0F0FF 30%,#00C8E8 100%);
  border-radius:2px;
  top:4px;left:50%;
  transform-origin:50% 14px;
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
  padding-top:4px;
  margin-top:4px;
}
.ch-btn{
  height:28px;border-radius:3px;
  border:1px solid rgba(55,100,140,.5);
  background:rgba(10,20,34,.88);
  color:#C0D8F0;
  font-size:9px;font-weight:900;letter-spacing:.1em;text-transform:uppercase;
  cursor:pointer;transition:all .1s;
  display:flex;align-items:center;justify-content:center;
}
.ch-btn:hover{background:rgba(14,28,48,.95);border-color:var(--accent);color:var(--accent2);text-shadow:0 0 8px rgba(0,200,255,.5);}
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
  flex:1;height:38px;border:1px solid var(--border2);border-radius:3px;
  background:#040608;padding:0 10px;
  font-family:'Inter',sans-serif;font-size:15px;font-weight:600;color:#FFFFFF;
  outline:none;transition:border-color .15s;user-select:text;
}
.launch-input:focus{border-color:var(--purple);box-shadow:0 0 0 2px rgba(139,92,246,.08);}
.launch-input::placeholder{color:var(--text3);}
.glow-input::placeholder{
  color:rgba(0,220,212,.75);
  animation:placeholder-pulse 2.8s ease-in-out infinite;
}
@keyframes placeholder-pulse{
  0%,100%{color:rgba(0,220,212,.65);}
  50%{color:rgba(0,220,212,.95);}
}
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
  flex:1;height:36px;border:1px solid var(--border2);border-radius:3px;
  background:#040608;padding:0 10px;
  font-family:'Inter',sans-serif;font-size:15px;font-weight:600;color:#FFFFFF;
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
  flex:1;padding:14px 16px;
  background:#030507;border:1px solid var(--border);border-radius:3px;
  font-size:15px;line-height:1.9;color:#FFFFFF;font-weight:500;
  white-space:pre-wrap;overflow-y:auto;
  font-family:'Inter',sans-serif;
  -webkit-overflow-scrolling:touch;
  letter-spacing:.01em;
}
.resp-box .err{color:#E05050;font-weight:600;}
/* MODEL DIAL */
.model-dial-wrap{display:flex;align-items:center;gap:8px;padding:8px 14px 7px;border-bottom:1px solid var(--border);flex-shrink:0;}
.model-dial-lbl{font-size:9px;font-weight:800;letter-spacing:.18em;text-transform:uppercase;color:var(--chrome);white-space:nowrap;flex-shrink:0;}
.model-dial-btns{display:flex;gap:3px;flex:1;}
.model-dial-btn{flex:1;padding:5px 0;border:1px solid rgba(0,196,232,.18);border-radius:4px;background:transparent;color:var(--chrome);font-size:9px;font-weight:800;letter-spacing:.12em;cursor:pointer;transition:all .15s;text-transform:uppercase;font-family:inherit;}
.model-dial-btn:hover{border-color:rgba(0,196,232,.5);color:var(--accent);}
.model-dial-btn.active{background:rgba(0,196,232,.1);border-color:var(--accent);color:var(--accent);box-shadow:0 0 8px rgba(0,196,232,.2);}
.model-dial-btn.active.opus{background:rgba(167,139,250,.1);border-color:#A78BFA;color:#A78BFA;box-shadow:0 0 8px rgba(167,139,250,.2);}
.model-dial-btn.active.haiku{background:rgba(94,232,138,.08);border-color:#5EE88A;color:#5EE88A;box-shadow:0 0 8px rgba(94,232,138,.15);}
body.light .model-dial-btn{color:#5A7898;border-color:rgba(0,126,120,.2);}
body.light .model-dial-btn.active{background:rgba(0,126,120,.08);border-color:var(--accent);color:var(--accent);}
/* PRESETS */
.presets-wrap{padding:6px 14px 8px;border-bottom:1px solid var(--border);flex-shrink:0;}
.presets-hd-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:5px;}
.preset-save-row{display:flex;gap:5px;align-items:center;}
.preset-save-center{display:flex;gap:8px;align-items:center;justify-content:center;padding:10px 14px 8px;}
.preset-input{height:44px;background:#040608;border:2px solid rgba(0,220,212,.35);border-radius:4px;color:#FFFFFF;font-size:15px;font-weight:600;padding:0 14px;font-family:'Inter',sans-serif;flex:1;outline:none;transition:border-color .15s;}
.preset-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(0,200,192,.1);}
.preset-input::placeholder{color:rgba(0,220,212,.4);}
.preset-save-btn{height:44px;padding:0 20px;border-radius:4px;border:2px solid var(--accent);background:rgba(0,200,192,.12);color:var(--accent2);font-size:11px;font-weight:900;letter-spacing:.12em;cursor:pointer;transition:all .12s;flex-shrink:0;text-shadow:0 0 8px rgba(0,220,212,.5);box-shadow:0 0 12px rgba(0,200,192,.2);}
.preset-save-btn:hover{background:rgba(0,200,192,.24);box-shadow:0 0 20px rgba(0,200,192,.4);}
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
  .ch-btn:hover{background:rgba(10,20,34,.88);border-color:rgba(55,100,140,.5);color:#C0D8F0;text-shadow:none;}
  .ch-btn.active:hover{background:linear-gradient(180deg,#001C22,#001018);color:var(--accent2);border-color:var(--accent);}
  /* Larger touch targets on coarse-pointer devices (iPad/iPhone) */
  .ch-btn{min-height:40px;}
  .reset-btn{min-height:40px;}
  .faq-btn{width:40px;height:40px;}
  .settings-btn{width:40px;height:40px;}
  .cmp-open-btn{min-height:40px;}
}
/* ── iPad / tablet ──────────────────────────────────────── */
@media(max-width:1366px) and (pointer:coarse){
  body{overscroll-behavior:none;-webkit-overflow-scrolling:touch;}
  .hdr{height:56px;padding:0 16px;}
  .brand{font-size:52px;line-height:56px;}
  .hdr-settings-label{font-size:8px;}
  .hdr-settings{font-size:7px;}
  /* Fader tank: easier to grab on touch */
  .fader-track{width:calc(100% - 6px);}
  /* Knob: slightly larger for finger */
  .console.compact .knob{width:48px;height:48px;}
  /* Buttons: taller tap targets */
  .ch-btn{min-height:44px;font-size:11px;}
  .console.compact .ch-btn{min-height:44px;}
  /* Safe area for home bar */
  .console{padding-bottom:env(safe-area-inset-bottom,0px);}
  /* Compare panel full-width on iPad portrait */
  .cmp-panel{width:100%;right:-100%;}
  .cmp-panel.open{right:0;}
  /* Settings panel full-width */
  .settings-panel{width:100%;right:-100%;}
  .settings-panel.open{right:0;}
  /* Separator divider hidden on small screens */
  .hdr-right > div[style]{display:none !important;}
}
@media(max-width:768px){
  .hdr-center{display:none;}
}

/* ══ HIGH-RES / 4K OPTIMIZATIONS ═══════════════════════════ */

/* Enhanced fader fill — more gradient stops, richer liquid on all screens */
.fader-fill{
  background:linear-gradient(0deg,
    rgba(0,130,124,.96) 0%,
    rgba(0,152,146,.88) 12%,
    rgba(0,175,168,.78) 28%,
    rgba(0,196,188,.65) 45%,
    rgba(0,215,207,.50) 62%,
    rgba(0,230,222,.33) 78%,
    rgba(0,242,235,.18) 91%,
    rgba(120,255,250,.08) 100%
  );
  box-shadow:0 0 22px rgba(0,200,192,.42),0 0 55px rgba(0,180,175,.14),inset 0 0 28px rgba(0,160,155,.08);
}
.ch.t2 .fader-fill,.ch.t4 .fader-fill{
  background:linear-gradient(0deg,
    rgba(82,26,175,.96) 0%,
    rgba(100,46,196,.88) 12%,
    rgba(118,64,215,.78) 28%,
    rgba(136,84,232,.65) 45%,
    rgba(153,103,245,.50) 62%,
    rgba(168,124,.255,.33) 78%,
    rgba(182,144,255,.18) 91%,
    rgba(210,185,255,.08) 100%
  );
  box-shadow:0 0 22px rgba(139,92,246,.42),0 0 55px rgba(139,92,246,.14),inset 0 0 28px rgba(100,58,200,.08);
}

/* ── 1440p / large desktop ── */
@media(min-width:1440px){
  .console.compact .fader-lbl{font-size:18px;}
  .console.compact .fader-val{font-size:18px;}
  .console.compact .knob{width:50px;height:50px;}
  .console.compact .knob-body{inset:6px;}
  .console.compact .knob-lbl{font-size:9px;}
  .console.compact .knob-val{font-size:10px;}
  .console.compact .ch-btn{height:34px;font-size:11px;}
  .ch-id{font-size:10px;}
  .fader-val{font-size:15px;}
  .knob-val{font-size:9px;}
  /* Richer surface line glow */
  .ch.t1 .fader-thumb,.ch.t3 .fader-thumb{
    box-shadow:0 0 14px rgba(0,220,212,1),0 0 32px rgba(0,200,192,.75),0 0 60px rgba(0,180,175,.3);
  }
  .ch.t2 .fader-thumb,.ch.t4 .fader-thumb{
    box-shadow:0 0 14px rgba(167,139,250,1),0 0 32px rgba(139,92,246,.75),0 0 60px rgba(120,75,230,.3);
  }
}

/* ── 4K / super HD ── */
@media(min-width:2000px){
  html{font-size:125%;}
  .hdr{height:112px;}
  .brand{font-size:88px;line-height:112px;}
  .hdr-settings-label{font-size:11px;}
  .hdr-settings{font-size:10px;}
  .console.compact .fader-lbl{font-size:22px;letter-spacing:.1em;}
  .console.compact .fader-val{font-size:22px;}
  .console.compact .knob{width:62px;height:62px;}
  .console.compact .knob-body{inset:7px;}
  .console.compact .knob-dot{width:3px;height:15px;top:6px;transform-origin:50% 24px;}
  .console.compact .knob-lbl{font-size:10px;letter-spacing:.1em;}
  .console.compact .knob-val{font-size:12px;}
  .console.compact .ch-btn{height:40px;font-size:13px;letter-spacing:.12em;}
  .ch-id{font-size:12px;letter-spacing:.18em;}
  .fader-val{font-size:18px;}
  .ch-accent{height:4px;}
  /* Wider glow spread for large screens */
  .fader-fill{box-shadow:0 0 36px rgba(0,200,192,.5),0 0 80px rgba(0,180,175,.2),inset 0 0 40px rgba(0,160,155,.1);}
  .ch.t2 .fader-fill,.ch.t4 .fader-fill{box-shadow:0 0 36px rgba(139,92,246,.5),0 0 80px rgba(139,92,246,.2),inset 0 0 40px rgba(100,58,200,.1);}
  .ch.t1 .fader-thumb,.ch.t3 .fader-thumb{
    height:5px;
    box-shadow:0 0 18px rgba(0,220,212,1),0 0 40px rgba(0,200,192,.8),0 0 80px rgba(0,180,175,.4);
  }
  .ch.t2 .fader-thumb,.ch.t4 .fader-thumb{
    height:5px;
    box-shadow:0 0 18px rgba(167,139,250,1),0 0 40px rgba(139,92,246,.8),0 0 80px rgba(120,75,230,.4);
  }
  /* Bigger dot grid */
  body{background-size:36px 36px;}
  /* Mute pulse more dramatic */
  @keyframes mute-pulse{
    0%,100%{box-shadow:0 0 12px rgba(200,60,60,.35),0 0 28px rgba(200,60,60,.16);}
    50%    {box-shadow:0 0 22px rgba(200,60,60,.65),0 0 50px rgba(200,60,60,.28);}
  }
}

/* ── High-DPI (Retina / 4K native) ── */
@media(-webkit-min-device-pixel-ratio:2),(min-resolution:192dpi){
  /* Sharper knob ring */
  .knob{box-shadow:0 4px 16px rgba(0,0,0,1),0 0 0 .5px rgba(0,0,0,.98),0 0 0 1px rgba(255,255,255,.05),0 0 18px rgba(0,196,232,.1);}
  /* Crisper fader track border */
  .fader-track{border-width:.5px;}
  /* Thinner, sharper ch-accent */
  .ch-accent{height:2px;}
  /* Sub-pixel borders on channel header */
  .ch-hdr-row{border-bottom-width:.5px;}
}


@keyframes cmp-pulse{
  0%,100%{
    box-shadow:
      0 0 14px rgba(217,70,239,.85),
      0 0 30px rgba(217,70,239,.45),
      0 0 55px rgba(217,70,239,.18),
      inset 0 0 14px rgba(217,70,239,.16);
  }
  50%{
    box-shadow:
      0 0 22px rgba(217,70,239,1),
      0 0 48px rgba(217,70,239,.72),
      0 0 85px rgba(217,70,239,.32),
      0 0 130px rgba(217,70,239,.12),
      inset 0 0 22px rgba(217,70,239,.26);
  }
}
.cmp-open-btn{
  height:36px;padding:0 20px;border-radius:4px;
  border:2px solid rgba(217,70,239,.95);
  background:linear-gradient(180deg,rgba(145,35,190,.38) 0%,rgba(100,15,150,.52) 100%);
  color:#F0ABFF;font-size:10px;font-weight:900;letter-spacing:.18em;
  text-transform:uppercase;cursor:pointer;transition:all .15s;white-space:nowrap;
  text-shadow:0 0 10px rgba(255,180,255,1),0 0 22px rgba(217,70,239,.9);
  flex-shrink:0;touch-action:manipulation;
  animation:cmp-pulse 2s ease-in-out infinite;
}
.cmp-open-btn:hover{
  background:linear-gradient(180deg,rgba(180,50,220,.5) 0%,rgba(130,20,180,.6) 100%);
  border-color:#F0ABFF;animation:none;
  box-shadow:0 0 30px rgba(217,70,239,1),0 0 60px rgba(217,70,239,.6),0 0 100px rgba(217,70,239,.25),inset 0 0 24px rgba(217,70,239,.3);
}
.cmp-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:100;}
.cmp-overlay.open{display:block;}
.cmp-panel{
  position:fixed;right:-820px;top:0;bottom:0;width:780px;
  background:var(--panel);z-index:101;
  transition:right .26s cubic-bezier(.4,0,.2,1);
  overflow-y:auto;border-left:2px solid var(--magenta);
  display:flex;flex-direction:column;box-shadow:-8px 0 40px rgba(0,0,0,.8);
}
.cmp-panel.open{right:0;}
.cmp-hd{padding:13px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;background:#040608;}
.cmp-title{font-family:'Abril Fatface',serif;font-size:20px;color:var(--magenta2);text-shadow:0 0 20px rgba(217,70,239,.4);}
.cmp-close{width:26px;height:26px;border:1px solid var(--border2);background:transparent;cursor:pointer;font-size:13px;color:var(--text2);border-radius:50%;transition:background .12s;display:flex;align-items:center;justify-content:center;font-weight:700;}
.cmp-close:hover{background:var(--panel2);color:var(--magenta2);}
.cmp-body{padding:18px;display:flex;flex-direction:column;gap:12px;flex:1;}
.cmp-selectors{display:flex;gap:10px;}
.cmp-sel-group{flex:1;display:flex;flex-direction:column;gap:5px;}
.cmp-sel-label{font-size:8px;font-weight:800;letter-spacing:.2em;text-transform:uppercase;color:var(--text3);}
.cmp-select{height:34px;background:var(--panel2);border:1px solid var(--border2);border-radius:3px;color:var(--text);font-size:12px;font-family:'Inter',sans-serif;padding:0 10px;outline:none;cursor:pointer;transition:border-color .15s;}
.cmp-select:focus{border-color:var(--magenta);}
.cmp-prompt-row{display:flex;gap:8px;}
.cmp-prompt-input{flex:1;height:40px;background:#040608;border:1px solid var(--border2);border-radius:3px;color:#FFFFFF;font-size:15px;font-weight:600;font-family:'Inter',sans-serif;padding:0 12px;outline:none;transition:border-color .15s;}
.cmp-prompt-input:focus{border-color:var(--magenta);box-shadow:0 0 0 2px rgba(217,70,239,.08);}
.cmp-prompt-input::placeholder{color:var(--text3);}
.cmp-run-btn{height:36px;padding:0 18px;background:linear-gradient(180deg,#200830,#140020);color:var(--magenta2);border:1px solid rgba(217,70,239,.5);border-radius:3px;font-family:'Inter',sans-serif;font-size:9px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;cursor:pointer;transition:all .12s;white-space:nowrap;box-shadow:0 0 10px rgba(217,70,239,.18);}
.cmp-run-btn:hover{background:linear-gradient(180deg,#2C0A40,#1E0030);box-shadow:0 0 18px rgba(217,70,239,.32);}
.cmp-run-btn:disabled{opacity:.3;cursor:not-allowed;box-shadow:none;}
.cmp-status{font-size:9px;font-weight:700;letter-spacing:.1em;text-align:center;color:var(--text3);min-height:16px;}
.cmp-status.active{color:var(--magenta2);text-shadow:0 0 8px rgba(217,70,239,.5);}
.cmp-outputs{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.cmp-out{display:flex;flex-direction:column;gap:4px;}
.cmp-out-label{font-size:8px;font-weight:800;letter-spacing:.18em;text-transform:uppercase;color:var(--text3);}
.cmp-out-box{height:260px;overflow-y:auto;background:#030507;border:1px solid var(--border);border-radius:3px;font-size:14px;line-height:1.85;color:#FFFFFF;font-weight:500;padding:12px 14px;font-family:'Inter',sans-serif;white-space:pre-wrap;transition:border-color .2s;}
.cmp-scorecard-hd{font-size:9px;font-weight:900;letter-spacing:.24em;text-transform:uppercase;color:var(--magenta2);text-shadow:0 0 8px rgba(217,70,239,.4);margin-bottom:8px;padding-top:4px;}
.cmp-raw-strip{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px;}
.cmp-raw-card{background:#030507;border:1px solid var(--border);border-radius:3px;padding:10px 12px;}
.cmp-raw-lbl{font-size:8px;font-weight:800;letter-spacing:.18em;text-transform:uppercase;color:var(--text3);margin-bottom:6px;}
.cmp-raw-vals{display:flex;gap:16px;align-items:flex-end;}
.cmp-raw-num-a{font-size:22px;font-weight:900;color:var(--accent);font-variant-numeric:tabular-nums;line-height:1;}
.cmp-raw-num-b{font-size:22px;font-weight:900;color:var(--magenta2);font-variant-numeric:tabular-nums;line-height:1;}
.cmp-raw-sub{font-size:9px;color:var(--text3);font-weight:600;letter-spacing:.06em;margin-top:2px;}
.cmp-raw-note{font-size:10px;color:var(--text2);margin-top:6px;font-weight:600;}
.cmp-out-box.a-active{border-color:rgba(0,200,192,.45);}
.cmp-out-box.b-active{border-color:rgba(217,70,239,.45);}
.cmp-metrics-section{display:flex;flex-direction:column;gap:6px;}
.cmp-metrics{display:flex;flex-direction:column;gap:5px;}
.cmp-metric{background:var(--panel2);border:1px solid var(--border);border-radius:3px;padding:7px 10px;}
.cmp-metric-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:5px;}
.cmp-metric-name{font-size:8px;font-weight:800;letter-spacing:.18em;text-transform:uppercase;color:var(--chrome2);}
.cmp-winner-chip{font-size:8px;font-weight:800;letter-spacing:.08em;padding:2px 7px;border-radius:2px;}
.cmp-winner-a{background:rgba(0,200,192,.14);color:var(--accent);border:1px solid rgba(0,200,192,.28);}
.cmp-winner-b{background:rgba(217,70,239,.1);color:var(--magenta2);border:1px solid rgba(217,70,239,.22);}
.cmp-winner-tie{background:rgba(255,255,255,.04);color:var(--text3);border:1px solid var(--border);}
.cmp-bars{display:flex;flex-direction:column;gap:3px;}
.cmp-bar-row{display:flex;align-items:center;gap:6px;}
.cmp-bar-lbl{font-size:7px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text3);width:70px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.cmp-bar-track{flex:1;height:5px;background:var(--fader-bg);border-radius:3px;overflow:hidden;}
.cmp-bar-fill-a{height:100%;background:linear-gradient(90deg,#005850,var(--accent));border-radius:3px;transition:width .6s ease;}
.cmp-bar-fill-b{height:100%;background:linear-gradient(90deg,#5010A0,var(--magenta));border-radius:3px;transition:width .6s ease;}
.cmp-bar-score{font-size:8px;font-weight:700;font-variant-numeric:tabular-nums;width:22px;text-align:right;flex-shrink:0;}
.cmp-bar-score-a{color:var(--accent);}
.cmp-bar-score-b{color:var(--magenta2);}
.cmp-summary{font-size:14px;line-height:1.8;color:#FFFFFF;font-weight:500;background:#040608;border:1px solid rgba(217,70,239,.25);border-radius:3px;padding:14px 16px;}
.cmp-no-presets{font-size:12px;color:var(--text3);text-align:center;padding:28px 0;font-style:italic;}
.cmp-tab-btn{height:26px;padding:0 12px;border-radius:2px;border:1px solid var(--border2);background:transparent;color:var(--text3);font-size:8px;font-weight:900;letter-spacing:.14em;text-transform:uppercase;cursor:pointer;font-family:'Inter',sans-serif;transition:all .15s;}
.cmp-tab-btn.active{border-color:var(--magenta);background:rgba(217,70,239,.1);color:var(--magenta2);}
.cmp-stats-wrap{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:16px;}
.cmp-stats-total{font-size:11px;font-weight:700;color:var(--text3);letter-spacing:.1em;text-transform:uppercase;}
.cmp-stats-total span{font-size:28px;font-weight:900;color:var(--magenta2);font-variant-numeric:tabular-nums;margin-right:6px;letter-spacing:-.02em;}
.cmp-preset-card{background:var(--panel2);border:1px solid var(--border);border-radius:5px;overflow:hidden;}
.cmp-preset-hdr{padding:10px 14px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);background:#040608;}
.cmp-preset-name{font-size:11px;font-weight:800;color:var(--text);letter-spacing:.02em;flex:1;}
.cmp-preset-runs{font-size:8px;font-weight:700;color:var(--text3);letter-spacing:.1em;text-transform:uppercase;}
.cmp-preset-win{font-size:8px;font-weight:900;letter-spacing:.12em;text-transform:uppercase;padding:2px 8px;border-radius:2px;border:1px solid rgba(0,221,212,.35);color:var(--accent);background:rgba(0,221,212,.08);}
.cmp-preset-body{padding:12px 14px;display:flex;flex-direction:column;gap:8px;}
.cmp-stat-row{display:flex;align-items:center;gap:8px;}
.cmp-stat-lbl{font-size:8px;font-weight:700;color:var(--text3);letter-spacing:.1em;text-transform:uppercase;width:90px;flex-shrink:0;}
.cmp-stat-bar{flex:1;height:4px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden;}
.cmp-stat-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,#005850,var(--accent));transition:width .6s ease;}
.cmp-stat-val{font-size:9px;font-weight:800;color:var(--accent);width:28px;text-align:right;font-variant-numeric:tabular-nums;}
.cmp-stats-empty{text-align:center;padding:48px 20px;color:var(--text3);font-size:12px;font-style:italic;}
</style>
</head>
<body>

<!-- ── HEADER ─────────────────────────────────────────────────── -->
<div class="hdr">
  <span class="brand">GAIN</span>
  <div class="hdr-center">
    <div class="hdr-settings-label">Current Settings</div>
    <div class="hdr-settings" id="hdr-settings">—</div>
  </div>
  <div class="hdr-right">
    <span id="user-email" style="display:none"></span>
    <button class="reset-btn" onclick="resetDefaults()">RESET</button>
    <button class="cmp-open-btn" onclick="openCompare()">⊕ COMPARE</button>
    <div style="width:1px;height:24px;background:rgba(0,200,192,.15);flex-shrink:0;"></div>
    <a href="/app" class="settings-btn" id="compact-btn" title="Collapse to compact view">⊟</a>
    <button class="settings-btn" onclick="openSettings()" title="Settings">⚙</button>
    <button class="faq-btn" onclick="openFaq()">?</button>
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
<div class="console" id="console">

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
    <div class="model-dial-wrap">
      <div class="model-dial-lbl">MODEL</div>
      <div class="model-dial-btns">
        <button class="model-dial-btn haiku" data-slug="haiku" onclick="selectModel('haiku')">HAIKU</button>
        <button class="model-dial-btn sonnet active" data-slug="sonnet" onclick="selectModel('sonnet')">SONNET</button>
        <button class="model-dial-btn opus" data-slug="opus" onclick="selectModel('opus')">OPUS</button>
      </div>
    </div>

    <div class="presets-wrap">
      <div class="preset-save-center">
        <input class="preset-input" id="preset-input" type="text" placeholder="name this state and save it…" maxlength="40">
        <button class="preset-save-btn" onclick="savePreset('preset-input')">SAVE PRESET</button>
      </div>
      <div class="presets-hd-row">
        <div class="section-hd">SAVED PRESETS</div>
        <button class="abort-btn" id="abort-btn" onclick="abortRun()">&#9632; ABORT</button>
      </div>
      <div class="preset-list" id="preset-list"><span class="preset-empty">no presets saved</span></div>
    </div>

    <!-- CTRL RUN LAUNCHER -->
    <div class="launch-wrap">
      <input class="launch-input glow-input" id="launch-input" type="text" placeholder="✦  go ahead — type something crazy and see what happens">
      <span class="launch-running" id="launch-running">● RUNNING</span>
      <button class="launch-btn" id="launch-btn">LAUNCH</button>
    </div>

    <div class="preview-wrap">
      <div class="preview-top">
        <span class="preview-hd">PREVIEW RUN</span>
        <span class="api-tag">no files touched — just ideas</span>
      </div>
      <div class="task-row">
        <input class="task-input glow-input" id="task-input" type="text" placeholder="✦  try something here first — safe to experiment">
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

  <!-- Collapse tab: only visible in compact mode -->
  <div class="collapse-tab" onclick="toggleCompact()" title="Expand (` key)">
    <span class="tab-arrow">EXPAND</span>
  </div>

</div><!-- /console -->

<!-- SETTINGS -->
<div class="settings-overlay" id="settings-overlay" onclick="closeSettings()"></div>
<div class="settings-panel" id="settings-panel">
  <div class="settings-hd">
    <span class="settings-title">settings</span>
    <button class="settings-close" onclick="closeSettings()">✕</button>
  </div>
  <div class="settings-body">
    <div class="settings-row">
      <div class="settings-row-label">Account</div>
      <div class="settings-val" id="settings-email" style="color:var(--accent2);">—</div>
    </div>
    <div class="settings-row">
      <div class="settings-row-label">Appearance</div>
      <button class="settings-action" onclick="toggleTheme()">Toggle Light / Dark Mode</button>
    </div>
    <div class="settings-row">
      <div class="settings-row-label">Board</div>
      <button class="settings-action" onclick="resetDefaults();closeSettings()">Reset All to Defaults</button>
    </div>
    <div class="settings-row" style="border:none">
      <div class="settings-row-label">Session</div>
      <button class="settings-action danger" id="settings-logout-btn" onclick="logout()" style="display:none">Sign Out</button>
    </div>
  </div>
</div>

<!-- FAQ -->
<div class="faq-overlay" id="faq-overlay" onclick="closeFaq()"></div>
<div class="faq-panel" id="faq-panel">
  <div class="faq-hd">
    <span class="faq-title">how gain works</span>
    <button class="faq-close" onclick="closeFaq()">✕</button>
  </div>
  <div class="faq-body">

    <div class="faq-s">
      <div class="faq-s-title">what is this?</div>
      <p class="faq-p">Gain is a mixing board for AI. Instead of rewriting your prompt every time, you move faders and flip switches to change <em>how</em> Claude thinks — not what you ask.</p>
      <p class="faq-p">Same question. Different settings. Completely different answer. That's the whole idea.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">⊕ compare — the main feature</div>
      <p class="faq-p">Compare is where Gain becomes a measurement tool, not just a control surface. Hit the <strong style="color:var(--magenta2)">⊕ COMPARE</strong> button in the header to open it.</p>
      <div class="faq-track" style="border-left-color:var(--magenta)">
        <div class="faq-track-name" style="color:var(--magenta2)">How it works</div>
        <div class="faq-track-desc">
          1. Pick two presets (or "Current Settings" vs a saved preset)<br>
          2. Enter a prompt<br>
          3. Hit RUN — the same prompt runs against both behavioral states<br>
          4. A reasoning model scores the outputs on 5 metrics and explains the difference
        </div>
      </div>
      <p class="faq-p" style="margin-top:8px">The 5 metrics: <strong style="color:var(--text)">Adherence, Depth, Clarity, Efficiency, Confidence.</strong> Each scored 0–100 with a winner called. The summary tells you exactly why the outputs behaved differently.</p>
      <p class="faq-p">Every comparison run is saved locally and builds a statistical database of how your presets perform over time.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">saving presets</div>
      <p class="faq-p">Dial in your faders and buttons to a state you want to keep. Type a name in the <strong style="color:var(--accent2)">SAVE PRESET</strong> bar in the center panel and hit Enter or the button.</p>
      <p class="faq-p">You can also save from inside the Compare panel — there's a save row at the top so you can lock in a state without leaving the compare workflow.</p>
      <p class="faq-p">Presets are the foundation of Compare. The more presets you save, the more you can measure.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">the two ways to run</div>
      <div class="faq-track">
        <div class="faq-track-name" style="color:#A78BFA">LAUNCH — the real thing</div>
        <div class="faq-track-desc">Claude reads your files, writes code, and gets things done. Use this when you're ready to actually build something. Every fader position shapes what it does and how it talks to you.</div>
      </div>
      <div class="faq-track" style="margin-top:8px">
        <div class="faq-track-name" style="color:var(--accent2)">PREVIEW RUN — safe to experiment</div>
        <div class="faq-track-desc">No files touched. Claude just responds in plain text. Great for trying different settings and seeing how the output changes before you commit to anything.</div>
      </div>
      <p class="faq-p" style="margin-top:8px;opacity:.6">Not sure which to use? Start with Preview Run. You can't break anything there.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">track 1 — how hard it works</div>
      <div class="faq-track">
        <div class="faq-track-name">Fader: Effort &nbsp;·&nbsp; Knob: Thinking Time</div>
        <div class="faq-track-desc">
          <strong style="color:var(--accent2)">EXPLORE</strong> — Claude looks around and tells you what it sees. No changes made. Just analysis and a recommendation.<br><br>
          <strong style="color:var(--accent2)">BUILD</strong> — Claude makes one focused change. Nothing extra.<br><br>
          <strong style="color:var(--text)">Effort fader</strong> — slide up for faster, more direct output. Slide down for more explanation and reasoning.<br>
          <strong style="color:var(--text)">Thinking Time knob</strong> — turn up for deeper analysis. Turn down to keep things quick and surface-level.
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">track 2 — how confident it acts</div>
      <div class="faq-track" style="border-left-color:#8B5CF6">
        <div class="faq-track-name">Fader: Confidence &nbsp;·&nbsp; Knob: Boldness</div>
        <div class="faq-track-desc">
          <strong style="color:#A78BFA">LIST</strong> — Claude shows you 2 or 3 options with pros and cons. Doesn't pick. You decide.<br><br>
          <strong style="color:#A78BFA">DECIDE</strong> — Claude picks one path and goes. No debate, no alternatives. Just action.<br><br>
          <strong style="color:var(--text)">Confidence fader</strong> — high means it commits fully. Low means it shows you the options.<br>
          <strong style="color:var(--text)">Boldness knob</strong> — high means it'll make bigger moves if that's what it takes. Low means it stays careful and close to what's already there.
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">track 3 — how wide it looks</div>
      <div class="faq-track" style="border-left-color:#00A8A0">
        <div class="faq-track-name">Fader: Zoom Level &nbsp;·&nbsp; Knob: Context Size</div>
        <div class="faq-track-desc">
          <strong style="color:var(--accent2)">FILE</strong> — Claude only looks at the one file you're working in. Focused and contained.<br><br>
          <strong style="color:var(--accent2)">PROJECT</strong> — Claude can see the whole codebase. Use this for bigger changes that touch multiple things.<br><br>
          <strong style="color:var(--text)">Zoom Level fader</strong> — how far out Claude searches for context.<br>
          <strong style="color:var(--text)">Context Size knob</strong> — how much related information it pulls in. High = everything nearby. Low = nothing it didn't ask for.
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">track 4 — how it talks to you</div>
      <div class="faq-track" style="border-left-color:#6040C8">
        <div class="faq-track-name">Fader: Verbosity &nbsp;·&nbsp; Knob: Memory Persistence</div>
        <div class="faq-track-desc">
          <strong style="color:#A78BFA">DIRECT</strong> — just the output. No preamble, no commentary. Claude talks less and does more.<br><br>
          <strong style="color:#A78BFA">OPEN</strong> — Claude thinks out loud with you. More collaborative, more visible reasoning.<br><br>
          <strong style="color:var(--text)">Verbosity fader</strong> — how much space Claude gives its response. High = more breathing room. Low = tight and compressed.<br>
          <strong style="color:var(--text)">Memory Persistence knob</strong> — high means ideas build on each other and expand. Low means it stays short and dense.
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">turning tracks on and off</div>
      <p class="faq-p">Every track can be completely switched off. When a track is off, it has zero influence on the output — like unplugging a channel on a real mixing board.</p>
      <p class="faq-p">Hit the <strong style="color:#D07070">MUTE</strong> button on any track to turn it off. Hit it again to bring it back. The small dot in the track header does the same thing.</p>
      <p class="faq-p">Turning off a track you're not using keeps things clean and focused. You don't have to use all four.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">the buttons on each track</div>
      <p class="faq-p">Each track has two mode buttons — one on the left side, one on the right. They set the extreme position for that track.</p>
      <p class="faq-p">Click a button to select it. Click it again to turn it off. Nothing selected means that track is fully open — no rules applied in that direction. That's different from neutral. It means Claude uses its own judgment there.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">moving the faders and knobs</div>
      <p class="faq-p"><strong style="color:var(--text)">Faders</strong> — click and drag up or down. Hold Shift while dragging for finer control. Double-click the thumb to reset it to the middle.</p>
      <p class="faq-p"><strong style="color:var(--text)">Knobs</strong> — drag up to increase, down to decrease. The arc around the knob shows its position. When it hits the center exactly, it briefly flashes white.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">saving presets</div>
      <p class="faq-p">Found a combination that works? Save it. Type a name in the Presets bar and hit Enter or Save. It appears as a chip you can click anytime to restore that exact state.</p>
      <p class="faq-p">Your presets are saved to your account and stay there across sessions.</p>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">keyboard shortcuts</div>
      <div class="faq-track">
        <div class="faq-track-desc">
          <strong style="color:var(--text)">Space</strong> — stop Claude mid-run<br>
          <strong style="color:var(--text)">1 / 2 / 3 / 4</strong> — mute or unmute each track<br>
          <strong style="color:var(--text)">R</strong> — reset everything to the middle<br>
          <strong style="color:var(--text)">T</strong> — switch between light and dark mode<br>
          <strong style="color:var(--text)">?</strong> — open this guide<br>
          <strong style="color:var(--text)">Esc</strong> — close this guide
        </div>
      </div>
    </div>

    <div class="faq-s">
      <div class="faq-s-title">try these combinations</div>
      <div class="faq-track">
        <div class="faq-track-name">Just exploring — not ready to change anything</div>
        <div class="faq-track-desc">Track 1: EXPLORE, Effort low, Thinking Time high. Track 2: LIST. Mute Tracks 3 and 4. Ask anything. Claude maps the problem and shows your options. Nothing gets touched.</div>
      </div>
      <div class="faq-track" style="margin-top:8px">
        <div class="faq-track-name">One quick fix, no extras</div>
        <div class="faq-track-desc">Track 1: BUILD, Effort high. Track 2: DECIDE, Confidence high. Track 3: FILE. Track 4: DIRECT. In, fixed, out. No commentary.</div>
      </div>
      <div class="faq-track" style="margin-top:8px">
        <div class="faq-track-name">Big swing — let it go wide</div>
        <div class="faq-track-desc">Track 1: BUILD, Thinking Time high. Track 2: DECIDE, Boldness high. Track 3: PROJECT, Context Size high. Track 4: STUDIO. Full picture, bold moves, clean output.</div>
      </div>
      <p class="faq-p" style="opacity:.3;font-style:italic;margin-top:14px">Same task. Different state. That's the machine.</p>
    </div>

  </div>
</div>

<!-- COMPARE PANEL -->
<div class="cmp-overlay" id="cmp-overlay" onclick="closeCompare()"></div>
<div class="cmp-panel" id="cmp-panel">
  <div class="cmp-hd">
    <span class="cmp-title">compare presets</span>
    <button class="cmp-close" onclick="closeCompare()">✕</button>
  </div>
  <div class="cmp-body">
    <div style="display:flex;gap:8px;align-items:center;padding-bottom:14px;border-bottom:1px solid var(--border);margin-bottom:4px;">
      <div class="cmp-sel-label" style="flex-shrink:0;white-space:nowrap;">SAVE CURRENT AS</div>
      <input class="cmp-prompt-input" id="cmp-save-input" type="text" placeholder="preset name…" maxlength="40" style="height:32px;font-size:13px;">
      <button class="cmp-run-btn" onclick="savePreset('cmp-save-input','cmp-save-msg')" style="height:32px;padding:0 14px;font-size:9px;">SAVE</button>
    </div>
    <div id="cmp-save-msg" style="font-size:10px;font-weight:700;letter-spacing:.08em;color:var(--accent);min-height:14px;margin-top:-8px;margin-bottom:6px;"></div>
    <div id="cmp-no-presets" class="cmp-no-presets" style="display:none">No presets yet — save one above.</div>
    <div id="cmp-controls">
      <div class="cmp-selectors">
        <div class="cmp-sel-group">
          <div class="cmp-sel-label">Preset A</div>
          <select class="cmp-select" id="cmp-select-a"></select>
        </div>
        <div class="cmp-sel-group">
          <div class="cmp-sel-label">Preset B</div>
          <select class="cmp-select" id="cmp-select-b"></select>
        </div>
      </div>
      <div class="cmp-prompt-row" style="margin-top:10px">
        <input class="cmp-prompt-input" id="cmp-prompt" type="text" placeholder="Enter a prompt to run on both presets…">
        <button class="cmp-run-btn" id="cmp-run-btn" onclick="runCompare()">RUN</button>
      </div>
      <div class="cmp-status" id="cmp-status"></div>
      <div class="cmp-outputs">
        <div class="cmp-out">
          <div class="cmp-out-label" id="cmp-label-a">PRESET A</div>
          <div class="cmp-out-box" id="cmp-out-a"></div>
        </div>
        <div class="cmp-out">
          <div class="cmp-out-label" id="cmp-label-b">PRESET B</div>
          <div class="cmp-out-box" id="cmp-out-b"></div>
        </div>
      </div>
      <div class="cmp-metrics-section" id="cmp-metrics-section" style="display:none">
        <div class="cmp-scorecard-hd">SCORECARD</div>
        <div class="cmp-metrics" id="cmp-metrics"></div>
        <div class="cmp-summary" id="cmp-summary"></div>
        <div class="cmp-raw-strip" id="cmp-raw-strip"></div>
      </div>
    </div>
  </div>
</div>

<script>
const THUMB_H = 4;
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
const draggingFaders = new Set();
const draggingKnobs  = new Set();
document.addEventListener('pointerup',     () => { isDragging = false; draggingFaders.clear(); draggingKnobs.clear(); }, true);
document.addEventListener('pointercancel', () => { isDragging = false; draggingFaders.clear(); draggingKnobs.clear(); }, true);
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
    const isLight = document.body.classList.contains('light');
    const dark = isLight ? '#9A9890' : '#0A1620';
    const lit  = isLight ? '#007E78' : 'var(--accent)';
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
    if (draggingFaders.has(f)) return;
    const v = s[f] ?? (FIELD_DEFAULTS[f] ?? 0.5);
    if (checkPickup(f, v)) return;
    setFader(f, v);
  });
  Object.keys(KNOBS).forEach(f => {
    if (draggingKnobs.has(f)) return;
    setKnob(f, s[f] ?? (FIELD_DEFAULTS[f] ?? 0.5));
  });
  METERS.forEach(f => setMeter(f, s[f] ?? (FIELD_DEFAULTS[f] ?? 0.5)));
  setButtons('mode',   s.mode);
  setButtons('stance', s.stance);
  setButtons('filter', s.filter);
  setButtons('voice',  s.voice);
  const tk = (label, btn, val) => btn ? `${label}: ${btn} · ${val}` : `${label}: ${val}`;
  const parts = [
    tk('MODE',       s.mode,   (s.intensity??0.5).toFixed(2)),
    tk('CONFIDENCE', s.stance, (s.certainty??0.5).toFixed(2)),
    tk('SCOPE',      s.filter, (s.scope??0.5).toFixed(2)),
    tk('VOICE',      s.voice,  (s.room??0.5).toFixed(2)),
  ];
  const settingsEl = document.getElementById('hdr-settings');
  if (settingsEl) settingsEl.textContent = parts.join('  ·  ');
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
  const newVal = lastState[field] === val ? '' : val;
  lastState[field] = newVal;
  setButtons(field, newVal);
  set(field, newVal);
}
function toggleTrack(t) {
  const key = t + '_on';
  const newVal = lastState[key] === false ? true : false;
  lastState[key] = newVal;
  const on = newVal !== false;
  const ch = document.querySelector('.ch.' + t);
  if (ch) ch.classList.toggle('ch-off', !on);
  const pwr = document.getElementById('cpwr-' + t);
  if (pwr) pwr.classList.toggle('off', !on);
  const mbtn = document.getElementById('mbtn-' + t);
  if (mbtn) mbtn.classList.toggle('active', !on);
  set(key, newVal);
}

const _tok = localStorage.getItem('sb-access-token') || '';
const es = new EventSource('/stream' + (_tok ? '?token=' + encodeURIComponent(_tok) : ''));
es.onmessage = e => { applyState(JSON.parse(e.data)); };
es.onerror   = () => {};

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

/* fader drag — direct, no physics */
Object.entries(FADERS).forEach(([field, ids]) => {
  const trackEl = document.getElementById(ids.track);
  const thumbEl = document.getElementById(ids.thumb);
  if (!trackEl || !thumbEl) return;
  const getV = () => parseFloat(document.getElementById(ids.val).textContent);

  function onDown(e) {
    const cap = e.currentTarget;
    e.preventDefault(); e.stopPropagation();
    try { cap.setPointerCapture(e.pointerId); } catch(_) {}
    isDragging = true;
    draggingFaders.add(field);
    trackEl.classList.add('dragging');
    const r = Math.max(20, trackEl.offsetHeight - THUMB_H);
    let prevY = e.clientY;
    function onMove(ev) {
      const dy   = ev.clientY - prevY;
      const fine = ev.shiftKey ? FINE_MULT : 1;
      setFader(field, Math.max(0, Math.min(1, getV() + (-dy / r * fine))));
      prevY = ev.clientY;
    }
    function onUp() {
      isDragging = false;
      draggingFaders.delete(field);
      trackEl.classList.remove('dragging');
      cap.removeEventListener('pointermove', onMove);
      cap.removeEventListener('pointerup',   onUp);
      cap.removeEventListener('pointercancel', onUp);
      set(field, Math.round(getV()*1000)/1000);
    }
    cap.addEventListener('pointermove', onMove);
    cap.addEventListener('pointerup',   onUp);
    cap.addEventListener('pointercancel', onUp);
  }

  const reset = () => {
    const d = FIELD_DEFAULTS[field]??0.5;
    setFader(field, d); set(field, d);
  };
  trackEl.addEventListener('pointerdown', onDown);
  thumbEl.addEventListener('pointerdown', onDown);
  trackEl.addEventListener('dblclick', reset);
  thumbEl.addEventListener('dblclick', reset);
});

/* knob drag — direct, no physics */
Object.entries(KNOBS).forEach(([field, ids]) => {
  const knobEl = document.getElementById('knob-'+field);
  if (!knobEl) return;
  const getV = () => parseFloat(document.getElementById(ids.val).textContent);

  function onDown(e) {
    e.preventDefault(); e.stopPropagation();
    try { knobEl.setPointerCapture(e.pointerId); } catch(_) {}
    isDragging = true;
    draggingKnobs.add(field);
    let prevY = e.clientY;
    function onMove(ev) {
      const dy    = ev.clientY - prevY;
      const fine  = ev.shiftKey ? FINE_MULT * 0.8 : 1;
      const delta = (-dy / 120) * KNOB_SENS * fine;
      let nv = Math.max(0, Math.min(1, getV() + delta));
      if (!ev.shiftKey && Math.abs(nv - 0.5) < KNOB_DETENT) {
        nv = 0.5;
        knobEl.classList.remove('at-detent');
        void knobEl.offsetWidth;
        knobEl.classList.add('at-detent');
        setTimeout(() => knobEl.classList.remove('at-detent'), 300);
      }
      setKnob(field, nv);
      prevY = ev.clientY;
    }
    function onUp() {
      isDragging = false;
      draggingKnobs.delete(field);
      knobEl.removeEventListener('pointermove', onMove);
      knobEl.removeEventListener('pointerup',   onUp);
      knobEl.removeEventListener('pointercancel', onUp);
      set(field, Math.round(getV()*1000)/1000);
    }
    knobEl.addEventListener('pointermove', onMove);
    knobEl.addEventListener('pointerup',   onUp);
    knobEl.addEventListener('pointercancel', onUp);
  }

  const reset = () => {
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
async function savePreset(inputId, msgId) {
  const inp = document.getElementById(inputId || 'preset-input');
  const name = inp ? inp.value.trim() : '';
  if (!name) return;
  try {
    const r = await fetch('/presets/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
    const d = await r.json();
    if (d.ok) {
      inp.value = '';
      await loadPresets();
      if (msgId) { const m=document.getElementById(msgId); if(m){m.textContent='Saved: '+d.name; setTimeout(()=>m.textContent='',2000);} }
      // Refresh compare dropdowns if open
      if (document.getElementById('cmp-panel').classList.contains('open')) populateCompareSelects();
    } else {
      if (msgId) { const m=document.getElementById(msgId); if(m) m.textContent='Error: '+(d.error||'failed'); }
    }
  } catch(e) {
    if (msgId) { const m=document.getElementById(msgId); if(m) m.textContent='Error: '+e.message; }
  }
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
document.getElementById('preset-input').addEventListener('keydown', e => { if (e.key === 'Enter') savePreset('preset-input'); });
loadPresets();

// ── MODEL DIAL ────────────────────────────────────────────────────
async function selectModel(slug) {
  try {
    await fetch('/model', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({slug})});
  } catch(e) {}
  document.querySelectorAll('.model-dial-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.slug === slug);
  });
}
(async function initModelDial() {
  try {
    const r = await fetch('/model');
    const d = await r.json();
    if (d.slug) {
      document.querySelectorAll('.model-dial-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.slug === d.slug);
      });
    }
  } catch(e) {}
})();

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
function openSettings() {
  document.getElementById('settings-overlay').classList.add('open');
  document.getElementById('settings-panel').classList.add('open');
  const email = document.getElementById('user-email')?.textContent || '';
  const token = getToken();
  const emailEl = document.getElementById('settings-email');
  const logoutBtn = document.getElementById('settings-logout-btn');
  if (emailEl) emailEl.textContent = email || (token ? 'Signed in' : 'Not signed in');
  if (logoutBtn) logoutBtn.style.display = token ? '' : 'none';
}
function closeSettings() { document.getElementById('settings-overlay').classList.remove('open'); document.getElementById('settings-panel').classList.remove('open'); }
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeFaq(); closeCompare(); return; }
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
    case '`': toggleCompact(); break;
    case '?': openFaq(); break;
  }
});

// ── COMPARE PANEL ─────────────────────────────────────────────────
let cmpTokensA = 0, cmpTokensB = 0, cmpWordsA = 0, cmpWordsB = 0;
function openCompare() {
  populateCompareSelects();
  document.getElementById('cmp-overlay').classList.add('open');
  document.getElementById('cmp-panel').classList.add('open');
}
function closeCompare() {
  document.getElementById('cmp-overlay').classList.remove('open');
  document.getElementById('cmp-panel').classList.remove('open');
}
function populateCompareSelects() {
  const selA  = document.getElementById('cmp-select-a');
  const selB  = document.getElementById('cmp-select-b');
  const noMsg = document.getElementById('cmp-no-presets');
  const ctrl  = document.getElementById('cmp-controls');
  if (!presets.length) {
    noMsg.style.display = ''; ctrl.style.display = 'none'; return;
  }
  noMsg.style.display = 'none'; ctrl.style.display = '';
  const cur  = '<option value="__current__">Current Settings</option>';
  const opts = presets.map(p => '<option value="' + esc(p.name) + '">' + esc(p.name) + '</option>').join('');
  selA.innerHTML = cur + opts;
  selB.innerHTML = cur + opts;
  if (presets.length >= 1) selB.selectedIndex = 1;
}
async function runCompare() {
  const presetA = document.getElementById('cmp-select-a').value;
  const presetB = document.getElementById('cmp-select-b').value;
  const prompt  = document.getElementById('cmp-prompt').value.trim();
  if (!prompt) { setCompareStatus('Enter a prompt first.'); return; }
  const runBtn = document.getElementById('cmp-run-btn');
  runBtn.disabled = true;
  document.getElementById('cmp-out-a').textContent = '';
  document.getElementById('cmp-out-b').textContent = '';
  document.getElementById('cmp-metrics-section').style.display = 'none';
  const nameA = presetA === '__current__' ? 'Current' : presetA;
  const nameB = presetB === '__current__' ? 'Current' : presetB;
  document.getElementById('cmp-label-a').textContent = nameA.toUpperCase();
  document.getElementById('cmp-label-b').textContent = nameB.toUpperCase();
  const outA = document.getElementById('cmp-out-a');
  const outB = document.getElementById('cmp-out-b');
  setCompareStatus('Initializing…', true);
  try {
    const res = await fetch('/compare', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({preset_a: presetA, preset_b: presetB, prompt})
    });
    if (res.status === 401) {
      setCompareStatus('Not logged in. Refresh and sign in again.'); runBtn.disabled = false; return;
    }
    if (res.status === 402) {
      const d = await res.json();
      const msg = d.plan === 'free'
        ? `Free limit reached (${d.usage}/${d.limit} runs). Upgrade to keep comparing.`
        : `Plan limit reached (${d.usage}/${d.limit} runs this month). Upgrade or wait for reset.`;
      setCompareStatus(msg); runBtn.disabled = false; return;
    }
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const {done, value} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const d = JSON.parse(line.slice(6));
        if (d.phase === 'a_start') { setCompareStatus('● Running ' + nameA + '…', true); outA.classList.add('a-active'); }
        if (d.phase === 'a' && d.text) { outA.textContent += d.text; outA.scrollTop = outA.scrollHeight; }
        if (d.phase === 'a_done') { cmpTokensA = d.tokens||0; cmpWordsA = d.words||0; outA.classList.remove('a-active'); setCompareStatus('● Running ' + nameB + '…', true); outB.classList.add('b-active'); }
        if (d.phase === 'b' && d.text) { outB.textContent += d.text; outB.scrollTop = outB.scrollHeight; }
        if (d.phase === 'b_done') { cmpTokensB = d.tokens||0; cmpWordsB = d.words||0; outB.classList.remove('b-active'); setCompareStatus('● Scoring outputs…', true); }
        if (d.phase === 'scores') { renderCompareScores(d.scores, nameA, nameB); setCompareStatus(''); }
        if (d.phase === 'score_error') { setCompareStatus('Scoring failed: ' + (d.error || 'unknown')); }
        if (d.error) { setCompareStatus('Error: ' + d.error); runBtn.disabled = false; }
        if (d.phase === 'done') { runBtn.disabled = false; }
      }
    }
  } catch(e) { setCompareStatus('Error: ' + e.message); runBtn.disabled = false; }
}
function setCompareStatus(msg, active) {
  const el = document.getElementById('cmp-status');
  el.textContent = msg; el.classList.toggle('active', !!active);
}
function renderCompareScores(scores, nameA, nameB) {
  const METRICS = [
    {key:'adherence',       label:'Adherence'},
    {key:'depth',           label:'Depth'},
    {key:'clarity',         label:'Clarity'},
    {key:'efficiency',      label:'Efficiency'},
    {key:'confidence',      label:'Confidence'},
    {key:'token_efficiency',label:'Token Efficiency'},
  ];
  document.getElementById('cmp-metrics').innerHTML = METRICS.map(function(m) {
    const s  = scores[m.key] || {a:0, b:0, winner:'tie'};
    const wc = s.winner==='a' ? 'cmp-winner-a' : s.winner==='b' ? 'cmp-winner-b' : 'cmp-winner-tie';
    const wl = s.winner==='a' ? esc(nameA) : s.winner==='b' ? esc(nameB) : 'TIE';
    return '<div class="cmp-metric">' +
      '<div class="cmp-metric-hd">' +
        '<span class="cmp-metric-name">' + m.label + '</span>' +
        '<span class="cmp-winner-chip ' + wc + '">' + wl + '</span>' +
      '</div>' +
      '<div class="cmp-bars">' +
        '<div class="cmp-bar-row">' +
          '<span class="cmp-bar-lbl">' + esc(nameA) + '</span>' +
          '<div class="cmp-bar-track"><div class="cmp-bar-fill-a" style="width:' + s.a + '%"></div></div>' +
          '<span class="cmp-bar-score cmp-bar-score-a">' + s.a + '</span>' +
        '</div>' +
        '<div class="cmp-bar-row">' +
          '<span class="cmp-bar-lbl">' + esc(nameB) + '</span>' +
          '<div class="cmp-bar-track"><div class="cmp-bar-fill-b" style="width:' + s.b + '%"></div></div>' +
          '<span class="cmp-bar-score cmp-bar-score-b">' + s.b + '</span>' +
        '</div>' +
      '</div>' +
    '</div>';
  }).join('');
  document.getElementById('cmp-summary').textContent = scores.summary || '';

  // Raw stats strip
  const rawEl = document.getElementById('cmp-raw-strip');
  if (rawEl) {
    const wRatio = cmpWordsA && cmpWordsB ? Math.max(cmpWordsA,cmpWordsB)/Math.max(1,Math.min(cmpWordsA,cmpWordsB)) : 0;
    const wLonger = cmpWordsA > cmpWordsB ? esc(nameA) : esc(nameB);
    const tDiff = Math.abs(cmpTokensA - cmpTokensB);
    const tCheaper = cmpTokensA < cmpTokensB ? esc(nameA) : esc(nameB);
    rawEl.innerHTML =
      '<div class="cmp-raw-card">' +
        '<div class="cmp-raw-lbl">Reply Length</div>' +
        '<div class="cmp-raw-vals">' +
          '<div><div class="cmp-raw-num-a">' + (cmpWordsA||'—') + '</div><div class="cmp-raw-sub">' + esc(nameA) + ' words</div></div>' +
          '<div><div class="cmp-raw-num-b">' + (cmpWordsB||'—') + '</div><div class="cmp-raw-sub">' + esc(nameB) + ' words</div></div>' +
        '</div>' +
        (wRatio >= 1.10 ? '<div class="cmp-raw-note">' + wLonger + ' is ' + wRatio.toFixed(1) + '× longer</div>' : wRatio ? '<div class="cmp-raw-note">Nearly identical length</div>' : '') +
      '</div>' +
      '<div class="cmp-raw-card">' +
        '<div class="cmp-raw-lbl">Tokens Used</div>' +
        '<div class="cmp-raw-vals">' +
          '<div><div class="cmp-raw-num-a">' + (cmpTokensA||'—') + '</div><div class="cmp-raw-sub">' + esc(nameA) + '</div></div>' +
          '<div><div class="cmp-raw-num-b">' + (cmpTokensB||'—') + '</div><div class="cmp-raw-sub">' + esc(nameB) + '</div></div>' +
        '</div>' +
        (tDiff > 10 ? '<div class="cmp-raw-note">' + tCheaper + ' used ' + tDiff.toLocaleString() + ' fewer tokens</div>' : tDiff >= 0 ? '<div class="cmp-raw-note">Token cost virtually identical</div>' : '') +
      '</div>';
  }

  document.getElementById('cmp-metrics-section').style.display = '';
}
document.getElementById('cmp-prompt').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') runCompare();
});
document.getElementById('cmp-save-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') savePreset('cmp-save-input', 'cmp-save-msg');
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
  const isLocal = ['localhost','127.0.0.1'].includes(window.location.hostname);
  if (isLocal) return; // bypass auth on local
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

// ── Compact mode ──────────────────────────────────────────
(function initCompact() {
  if (localStorage.getItem('gain_compact') === '1') {
    document.getElementById('console').classList.add('compact');
    const btn = document.getElementById('compact-btn');
    if (btn) btn.textContent = '⊞';
    const hero = document.querySelector('.hero');
    if (hero) hero.style.display = 'none';
  }
})();
function toggleCompact() {
  window.location.href = '/app';
}
</script>

<!-- ── ONBOARDING ── -->
<style>
#onboard-overlay {
  display:none;
  position:fixed;inset:0;z-index:9999;
  background:rgba(0,0,0,.92);
  align-items:center;justify-content:center;
  flex-direction:column;gap:0;
}
#onboard-overlay.active { display:flex; }
.ob-card {
  display:none;
  flex-direction:column;align-items:center;justify-content:center;
  text-align:center;padding:48px 40px;
  max-width:480px;width:90%;
  border:1px solid rgba(0,220,212,.2);
  border-radius:8px;background:rgba(4,8,14,.95);
  box-shadow:0 0 80px rgba(0,180,200,.1);
  animation:ob-in .4s ease forwards;
}
.ob-card.active { display:flex; }
@keyframes ob-in {
  from{opacity:0;transform:translateY(20px);}
  to{opacity:1;transform:translateY(0);}
}
.ob-num {
  font-size:10px;font-weight:800;letter-spacing:.2em;
  color:rgba(0,220,212,.4);text-transform:uppercase;margin-bottom:20px;
}
.ob-title {
  font-family:'Abril Fatface',serif;font-size:28px;
  background:linear-gradient(130deg,#00E8FF 0%,#A0C8FF 50%,#C0A0FF 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  margin-bottom:16px;line-height:1.2;
}
.ob-body {
  font-size:15px;line-height:1.7;color:rgba(200,225,240,.7);
  margin-bottom:32px;
}
.ob-next {
  height:44px;padding:0 32px;
  background:rgba(0,180,200,.2);border:1px solid rgba(0,220,212,.5);
  border-radius:4px;color:#00DDD4;font-size:11px;font-weight:800;
  letter-spacing:.14em;text-transform:uppercase;cursor:pointer;
  transition:all .15s;font-family:'Inter',sans-serif;
}
.ob-next:hover{background:rgba(0,200,212,.3);box-shadow:0 0 20px rgba(0,220,212,.3);}
#fw-canvas{position:fixed;inset:0;z-index:10000;pointer-events:none;display:none;}
</style>

<div id="onboard-overlay">
  <div class="ob-card active" id="ob-1">
    <div class="ob-num">01 of 04</div>
    <div class="ob-title">Same prompt.<br>Two states.<br>Measurable difference.</div>
    <div class="ob-body">GAIN is a behavioral mixing board for AI. Every fader changes <em>how</em> Claude thinks — not what you ask.<br><br>The same question gets completely different answers depending on where the dials are set.</div>
    <button class="ob-next" onclick="obNext(2)">How? →</button>
  </div>
  <div class="ob-card" id="ob-2">
    <div class="ob-num">02 of 04</div>
    <div class="ob-title">Dial it in.<br>Save it.</div>
    <div class="ob-body">Move the faders until the AI behaves the way you want. Name that state and save it as a preset.<br><br>That preset is now a reproducible behavioral profile — the same settings, any time, any prompt.</div>
    <button class="ob-next" onclick="obNext(3)">Then what? →</button>
  </div>
  <div class="ob-card" id="ob-3">
    <div class="ob-num">03 of 04</div>
    <div class="ob-title">Hit COMPARE.</div>
    <div class="ob-body">Pick two presets. Enter a prompt. Run it.<br><br>The same question goes through both behavioral states — and you see exactly how the outputs diverge. Side by side. No guessing.</div>
    <button class="ob-next" onclick="obNext(4)">And then? →</button>
  </div>
  <div class="ob-card" id="ob-4">
    <div class="ob-num">04 of 04</div>
    <div class="ob-title">A reasoning AI<br>scores both outputs.</div>
    <div class="ob-body">Adherence. Depth. Clarity. Efficiency. Confidence.<br><br>Each scored 0–100. A winner called. A plain-English explanation of exactly why the two states produced different results.<br><br>That's where the value is.</div>
    <button class="ob-next" onclick="obFinish()">Let's go ✦</button>
  </div>
</div>

<canvas id="fw-canvas"></canvas>

<script>
// ── Onboarding ────────────────────────────────────────────
(function initOnboard() {
  if (localStorage.getItem('gain_onboarded') === 'v2') return;
  document.getElementById('onboard-overlay').classList.add('active');
})();

function obNext(n) {
  document.querySelectorAll('.ob-card').forEach(c => c.classList.remove('active'));
  const next = document.getElementById('ob-' + n);
  if (next) { next.classList.remove('active'); void next.offsetWidth; next.classList.add('active'); }
}

function obFinish() {
  document.getElementById('onboard-overlay').classList.remove('active');
  localStorage.setItem('gain_onboarded', 'v2');
  launchFireworks();
}

// ── Fireworks ─────────────────────────────────────────────
function launchFireworks() {
  const canvas = document.getElementById('fw-canvas');
  canvas.width  = window.innerWidth;
  canvas.height = window.innerHeight;
  canvas.style.display = 'block';
  const ctx = canvas.getContext('2d');

  const COLORS = [
    '#00E8FF','#00DDD4','#A0C8FF','#C0A0FF',
    '#8B5CF6','#00C8C0','#6040C8','#FFFFFF',
    '#00FFEE','#B060FF','#40E8FF','#FF80FF'
  ];

  const particles = [];
  let startTime = null;
  const DURATION = 4000;

  function randomColor() { return COLORS[Math.floor(Math.random() * COLORS.length)]; }

  function burst(x, y, type) {
    const count = type === 'tree' ? 140 : type === 'chrysanthemum' ? 120 : 90;
    const color = randomColor();
    const color2 = randomColor();
    for (let i = 0; i < count; i++) {
      const angle = (i / count) * Math.PI * 2;
      let speed, gravity, fade, size;
      if (type === 'tree') {
        speed = (2 + Math.random() * 6) * (Math.random() < 0.3 ? 0.4 : 1);
        gravity = 0.08; fade = 0.012; size = 2.5 + Math.random() * 2;
      } else if (type === 'chrysanthemum') {
        speed = 3 + Math.random() * 5;
        gravity = 0.03; fade = 0.008; size = 1.5 + Math.random() * 2;
      } else {
        speed = 2 + Math.random() * 8;
        gravity = 0.12; fade = 0.015; size = 2 + Math.random() * 3;
      }
      particles.push({
        x, y,
        vx: Math.cos(angle) * speed + (Math.random() - 0.5),
        vy: Math.sin(angle) * speed + (Math.random() - 0.5),
        color: Math.random() < 0.3 ? color2 : color,
        alpha: 1, size, gravity, fade,
        trail: type === 'chrysanthemum',
        px: x, py: y
      });
    }
  }

  function spawnWave(t) {
    const w = canvas.width, h = canvas.height;
    const count = 6 + Math.floor(Math.random() * 5);
    for (let i = 0; i < count; i++) {
      const x = w * (0.1 + Math.random() * 0.8);
      const y = h * (0.05 + Math.random() * 0.6);
      const types = ['standard','tree','chrysanthemum'];
      burst(x, y, types[Math.floor(Math.random() * types.length)]);
    }
  }

  let lastSpawn = 0;
  const spawnInterval = 280;

  function frame(ts) {
    if (!startTime) startTime = ts;
    const elapsed = ts - startTime;
    if (elapsed > DURATION) {
      canvas.style.display = 'none';
      return;
    }

    ctx.fillStyle = 'rgba(0,0,0,0.18)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    if (ts - lastSpawn > spawnInterval) {
      spawnWave(elapsed);
      lastSpawn = ts;
    }

    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.px = p.x; p.py = p.y;
      p.x  += p.vx;
      p.y  += p.vy;
      p.vy += p.gravity;
      p.vx *= 0.98;
      p.alpha -= p.fade;
      if (p.alpha <= 0) { particles.splice(i, 1); continue; }
      ctx.save();
      ctx.globalAlpha = p.alpha;
      if (p.trail) {
        ctx.strokeStyle = p.color;
        ctx.lineWidth = p.size * 0.5;
        ctx.beginPath(); ctx.moveTo(p.px, p.py); ctx.lineTo(p.x, p.y); ctx.stroke();
      }
      ctx.fillStyle = p.color;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.size * p.alpha, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    }

    requestAnimationFrame(frame);
  }

  requestAnimationFrame(frame);
}

// ── Free tier paywall ─────────────────────────────────────────────────────────
function showPaywall() {
  document.getElementById('paywall-overlay').style.display = 'flex';
}

function _patchFetch() {
  const orig = window.fetch;
  window.fetch = async function(...args) {
    const res = await orig.apply(this, args);
    if (res.status === 402) {
      const clone = res.clone();
      clone.json().then(d => { if (d.error === 'free_limit') showPaywall(); }).catch(()=>{});
    }
    return res;
  };
}
_patchFetch();
</script>

<!-- ── PAYWALL MODAL ── -->
<div id="paywall-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:9999;align-items:center;justify-content:center;backdrop-filter:blur(8px);">
  <div style="background:#080E18;border:1px solid rgba(0,221,212,.25);border-radius:10px;width:420px;max-width:92vw;overflow:hidden;box-shadow:0 0 60px rgba(0,0,0,.8),0 0 0 1px rgba(0,221,212,.08);">
    <div style="height:3px;background:linear-gradient(90deg,#00DDD4,#8B5CF6,#D946EF);"></div>
    <div style="padding:32px 32px 28px;">
      <div style="font-family:'Abril Fatface',serif;font-size:28px;background:linear-gradient(130deg,#00E8FF,#A0C8FF,#D946EF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px;">You've used your 3 free runs.</div>
      <div style="font-size:13px;color:#6A8AA8;line-height:1.7;margin-bottom:28px;">Gain is in early access. Join the waitlist or sign in to keep going.</div>
      <div style="display:flex;flex-direction:column;gap:10px;">
        <a href="https://gain.creativekonsoles.com" target="_blank" style="display:block;height:44px;background:rgba(0,221,212,.1);border:1px solid rgba(0,221,212,.4);border-radius:4px;color:#00DDD4;font-size:10px;font-weight:900;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;display:flex;align-items:center;justify-content:center;transition:background .15s;">Join the Waitlist</a>
        <button onclick="document.getElementById('paywall-overlay').style.display='none';document.querySelector('.login-btn')&&document.querySelector('.login-btn').click();" style="height:44px;background:transparent;border:1px solid rgba(255,255,255,.1);border-radius:4px;color:#6A8AA8;font-size:10px;font-weight:900;letter-spacing:.18em;text-transform:uppercase;cursor:pointer;font-family:'Inter',sans-serif;transition:all .15s;">Sign In</button>
      </div>
      <div style="margin-top:20px;font-size:9px;color:#405870;text-align:center;letter-spacing:.06em;">gain.creativekonsoles.com</div>
    </div>
  </div>
</div>

</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

# ── Companion UI ──────────────────────────────────────────────────────────────

COMPANION_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gain</title>
<script>
// Force cache-bust: if URL has no version param, reload with one
(function(){
  var v='20260606b';
  if(window.location.search.indexOf('v='+v)===-1){
    window.location.replace('/companion?v='+v);
  }
})();
</script>
<link href="https://fonts.googleapis.com/css2?family=Abril+Fatface&family=Inter:wght@400;700;900&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
html,body{width:280px;font-family:'Inter',sans-serif;overflow:hidden;transition:background .2s,color .2s;}

[data-theme="dark"]{
  --bg:#030507;--panel:#060A0F;--border:#0d1a24;--border2:#162030;
  --text:#D8EAF8;--dim:#405870;--dim2:#1E3A50;--ftrack:#0A1018;
  --t1:#00DDD4;--t2:#D946EF;--btn-bg:#060A0F;--btn-hover:#0A1018;
  --resp-bg:#020406;--resp-text:#8AACC8;--foot-bg:transparent;
}
[data-theme="light"]{
  --bg:#C8C8C8;--panel:#BABABA;--border:#A8A8A8;--border2:#989898;
  --text:#1A1A1A;--dim:#606060;--dim2:#808080;--ftrack:#ACACAC;
  --t1:#007A74;--t2:#9B1DB5;--btn-bg:#BABABA;--btn-hover:#ADADAD;
  --resp-bg:#C0C0C0;--resp-text:#2A2A2A;--foot-bg:transparent;
}

html,body{background:var(--bg);color:var(--text);}

.hdr{padding:8px 12px 6px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border);}
.brand{font-family:'Abril Fatface',serif;font-size:16px;background:linear-gradient(130deg,#00E8FF,#A0C8FF,#D946EF);-webkit-background-clip:text;-webkit-text-fill-color:transparent;flex-shrink:0;}
.dot{width:5px;height:5px;border-radius:50%;background:#34D399;box-shadow:0 0 5px #34D399;flex-shrink:0;}
.dot.off{background:var(--dim2);box-shadow:none;}
.theme-btn{margin-left:auto;width:22px;height:22px;border-radius:4px;border:1px solid var(--border2);background:var(--panel);color:var(--dim);font-size:9px;font-weight:900;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .15s;}
.theme-btn:hover{color:var(--text);border-color:var(--dim);}

.faders{display:flex;padding:14px 24px 10px;gap:32px;justify-content:center;border-bottom:1px solid var(--border);}
.fc{display:flex;flex-direction:column;align-items:center;gap:7px;flex:1;}
.flbl{font-size:7px;font-weight:900;letter-spacing:.16em;text-transform:uppercase;color:var(--dim);}
.fval{font-size:13px;font-weight:900;font-variant-numeric:tabular-nums;}
.fval.t1{color:var(--t1);}.fval.t2{color:var(--t2);}
.ftrack{width:20px;height:110px;background:var(--ftrack);border-radius:10px;position:relative;cursor:pointer;border:1px solid var(--border2);}
.ffill{position:absolute;bottom:0;left:0;right:0;border-radius:10px;pointer-events:none;}
.t1 .ffill{background:linear-gradient(to top,var(--t1),rgba(0,221,212,.15));}
.t2 .ffill{background:linear-gradient(to top,var(--t2),rgba(217,70,239,.15));}
.fthumb{position:absolute;left:50%;transform:translate(-50%,50%);width:32px;height:11px;background:var(--panel);border-radius:6px;cursor:grab;border:1px solid rgba(0,221,212,.3);box-shadow:0 0 8px rgba(0,221,212,.1);}
.t2 .fthumb{border-color:rgba(217,70,239,.3);box-shadow:0 0 8px rgba(217,70,239,.1);}
[data-theme="light"] .fthumb{border-color:rgba(0,122,116,.4);}
[data-theme="light"] .t2 .fthumb{border-color:rgba(155,29,181,.4);}

.btns{display:flex;border-bottom:1px solid var(--border);border-top:1px solid var(--border);}
.bb{flex:1;height:44px;border:none;background:var(--btn-bg);font-size:9px;font-weight:900;letter-spacing:.18em;text-transform:uppercase;cursor:pointer;font-family:'Inter',sans-serif;transition:all .15s;border-right:1px solid var(--border);color:var(--dim);}
.bb:last-child{border-right:none;}
.bb:hover{color:var(--text);background:var(--btn-hover);}
.bb.ab{color:var(--t1);background:rgba(0,221,212,.07);box-shadow:inset 0 -3px 0 var(--t1);}
.bb.ae{color:#A78BFA;background:rgba(139,92,246,.07);box-shadow:inset 0 -3px 0 #A78BFA;}
[data-theme="light"] .bb.ab{background:rgba(0,122,116,.1);box-shadow:inset 0 -3px 0 var(--t1);}
[data-theme="light"] .bb.ae{background:rgba(139,92,246,.1);}
.bb:disabled{opacity:.4;cursor:default;}

.inp-row{display:flex;border-bottom:1px solid var(--border);}
.inp{flex:1;height:32px;background:transparent;border:none;color:var(--text);font-size:11px;font-family:'Inter',sans-serif;padding:0 10px;outline:none;}
.inp::placeholder{color:var(--dim2);}

.foot{display:flex;justify-content:space-between;align-items:center;padding:3px 10px;border-bottom:1px solid var(--border);min-height:20px;}
.ft{font-size:8px;color:var(--dim);letter-spacing:.04em;}
.tk{font-size:8px;font-weight:700;color:var(--dim2);}
.tk.lit{color:var(--t1);}

.resp{display:none;padding:10px 12px;font-size:11px;line-height:1.65;color:var(--resp-text);white-space:pre-wrap;max-height:240px;overflow-y:auto;background:var(--resp-bg);}
.resp.show{display:block;}
::-webkit-scrollbar{width:3px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px;}
</style>
</head>
<body>
<div class="hdr">
  <div class="brand">Gain</div>
  <div class="dot off" id="dot"></div>
  <button class="theme-btn" onclick="toggleTheme()" id="theme-btn">D</button>
</div>

<div class="faders">
  <div class="fc t1">
    <div class="flbl">Effort</div>
    <div class="ftrack" id="trk-intensity" onmousedown="drag(event,'intensity')">
      <div class="ffill" id="fill-intensity"></div>
      <div class="fthumb" id="thumb-intensity"></div>
    </div>
    <div class="fval t1" id="val-intensity">0.50</div>
  </div>
  <div class="fc t2">
    <div class="flbl">Verbosity</div>
    <div class="ftrack" id="trk-room" onmousedown="drag(event,'room')">
      <div class="ffill" id="fill-room"></div>
      <div class="fthumb" id="thumb-room"></div>
    </div>
    <div class="fval t2" id="val-room">0.50</div>
  </div>
</div>

<div class="btns">
  <button class="bb" id="bb" onclick="doBuild()">Build</button>
  <button class="bb" id="be" onclick="doExplore()">Explore</button>
</div>

<div class="inp-row">
  <input class="inp" id="inp" placeholder="Describe a sound or ask about the session…"
    onkeydown="if(event.key==='Enter')submit()">
</div>

<div class="foot">
  <span class="ft" id="st"></span>
  <span class="tk" id="tk">— tokens</span>
</div>
<div class="resp" id="resp"></div>

<script>
let dragging=null;
let theme=localStorage.getItem('gain-theme')||'dark';
document.documentElement.setAttribute('data-theme',theme);
document.getElementById('theme-btn').textContent=theme==='dark'?'L':'D';

function toggleTheme(){
  theme=theme==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',theme);
  document.getElementById('theme-btn').textContent=theme==='dark'?'L':'D';
  localStorage.setItem('gain-theme',theme);
}
function setFader(f,v){
  document.getElementById('fill-'+f).style.height=(v*100)+'%';
  document.getElementById('thumb-'+f).style.bottom='calc('+(v*100)+'% - 5.5px)';
  document.getElementById('val-'+f).textContent=v.toFixed(2);
}
function applyState(s){
  setFader('intensity',s.intensity??0.5);
  setFader('room',s.room??0.5);
  document.getElementById('dot').className='dot';
}
function drag(e,f){
  e.preventDefault();dragging=f;
  document.onmousemove=function(ev){
    const t=document.getElementById('trk-'+dragging);
    const r=t.getBoundingClientRect();
    let v=1-(ev.clientY-r.top)/r.height;
    v=Math.max(0,Math.min(1,v));
    setFader(dragging,v);
    fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({[dragging]:v})});
  };
  document.onmouseup=function(){dragging=null;document.onmousemove=null;document.onmouseup=null;};
}
function doBuild(){
  document.getElementById('bb').className='bb ab';
  document.getElementById('be').className='bb';
  document.getElementById('inp').focus();
  submit();
}
function armExplore(){
  document.getElementById('be').className='bb ae';
  document.getElementById('bb').className='bb';
  document.getElementById('inp').focus();
}
async function doExplore(){
  const p=document.getElementById('inp').value.trim();
  if(!p){armExplore();return;}
  document.getElementById('be').disabled=true;
  document.getElementById('resp').classList.remove('show');
  setSt('Asking Claude…');setTk(null);
  try{
    await fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'EXPLORE'})}).catch(()=>{});
    const r=await fetch('/explore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p})});
    const d=await r.json();
    if(d.error){setSt('Error: '+d.error);}
    else{showResp(d.text);setSt('Done · '+d.tokens+' tok');setTk(d.tokens);document.getElementById('inp').value='';}
  }catch(e){setSt('Error: '+e.message);}
  document.getElementById('be').disabled=false;
}
async function submit(){
  const p=document.getElementById('inp').value.trim();
  if(!p)return;
  const isExplore=document.getElementById('be').classList.contains('ae');
  if(isExplore){doExplore();return;}
  const isBuild=document.getElementById('bb').classList.contains('ab');
  document.getElementById('resp').classList.remove('show');
  setSt(isBuild?'Building — describe a sound (e.g. warm pad, punchy bass)…':'Thinking…');setTk(null);
  if(isBuild){
    let dot=0;const pulse=setInterval(()=>{dot=(dot+1)%4;setSt('Building'+'·'.repeat(dot+1));},600);
    try{
      const r=await fetch('/ableton/build',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p})});
      clearInterval(pulse);
      const d=await r.json();
      if(d.error){setSt('Error: '+d.error);}
      else{setSt('✓ Built: '+d.track_name);setTk(d.plan_tokens);document.getElementById('inp').value='';}
    }catch(e){clearInterval(pulse);setSt('Error: Gain not running');}
  }else{
    try{
      const r=await fetch('/m4l/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p})});
      const d=await r.json();
      if(d.error){setSt('Error: '+d.error);}
      else{showResp(d.text);setSt('');setTk(d.tokens);document.getElementById('inp').value='';}
    }catch(e){setSt('Gain not running');}
  }
}
function showResp(t){const r=document.getElementById('resp');r.textContent=t;r.classList.add('show');}
function setSt(t){document.getElementById('st').textContent=t;}
function setTk(n){const e=document.getElementById('tk');e.textContent=n?n+' tokens':'— tokens';e.className='tk'+(n?' lit':'');}
const es=new EventSource('/stream');
es.onmessage=e=>applyState(JSON.parse(e.data));
es.onerror=()=>{document.getElementById('dot').className='dot off';};
fetch('/m4l/state').then(r=>r.json()).then(applyState).catch(()=>{});
</script>
</body>
</html>"""


# ── Max for Live bridge ────────────────────────────────────────────────────────

@app.route("/m4l/state")
def m4l_state():
    return jsonify(read_state())

@app.route("/explore", methods=["POST"])
def explore():
    """Read Ableton session and answer a specific question about it."""
    data   = request.get_json() or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "No prompt"}), 400
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not _anthropic or not api_key:
        return jsonify({"error": "Claude not available"}), 500
    state = read_state()
    ctx   = _ableton_session_context()
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_active_model, max_tokens=1024,
            system=_build_prompt(state),
            messages=[{"role": "user", "content":
                f"[ABLETON SESSION]\n{ctx}\n\n[QUESTION]\n{prompt}"}],
        )
        return jsonify({"text": msg.content[0].text, "tokens": msg.usage.output_tokens})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/m4l/ask", methods=["POST"])
def m4l_ask():
    data   = request.get_json() or {}
    prompt = data.get("prompt", "").strip()
    preset = data.get("preset")
    if not prompt:
        return jsonify({"error": "No prompt"}), 400
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not _anthropic or not api_key:
        return jsonify({"error": "Claude not available"}), 500
    state = _load_preset(preset) if preset else read_state()
    # In EXPLORE mode, prepend live Ableton session context
    user_content = prompt
    if state.get("mode", "").upper() == "EXPLORE":
        ctx = _ableton_session_context()
        user_content = f"[ABLETON SESSION]\n{ctx}\n\n[QUESTION]\n{prompt}"
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_active_model, max_tokens=1024,
            system=_build_prompt(state),
            messages=[{"role": "user", "content": user_content}],
        )
        return jsonify({
            "text":   msg.content[0].text,
            "tokens": msg.usage.output_tokens,
            "state":  state,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _load_preset(name):
    f = PRESETS_DIR / f"{name}.json"
    return json.loads(f.read_text()) if f.exists() else read_state()

@app.route("/m4l/presets")
def m4l_presets():
    names = [f.stem for f in PRESETS_DIR.glob("*.json")] if PRESETS_DIR.exists() else []
    return jsonify(names)

# ── Ableton Builder ────────────────────────────────────────────────────────────

import socket as _socket

def _ableton_session_context():
    """Pull session + track data from Ableton, capped at 2 seconds total."""
    import threading
    _result = {"ctx": ""}

    def _fetch():
        try:
            session = _ableton_send("get_session_info")
            result  = session.get("result", {})
            tempo   = result.get("tempo", "?")
            sig     = f"{result.get('signature_numerator',4)}/{result.get('signature_denominator',4)}"
            count   = result.get("track_count", 0)
            lines   = [f"SESSION: {tempo} BPM, {sig}, {count} tracks"]
            for i in range(min(count, 12)):
                t = _ableton_send("get_track_info", {"track_index": i}).get("result", {})
                if not t:
                    continue
                kind  = "MIDI" if t.get("is_midi_track") else "Audio"
                name  = t.get("name", f"Track {i+1}")
                flags = ("".join([
                    " [muted]" if t.get("mute") else "",
                    " [solo]"  if t.get("solo") else "",
                    " [armed]" if t.get("arm")  else "",
                ]))
                devs  = ", ".join(d["name"] for d in t.get("devices", []) if d.get("name"))
                clips = [c["clip"]["name"] for c in t.get("clip_slots", [])
                         if c.get("has_clip") and c.get("clip")]
                dev_s  = f" | fx: {devs}"                         if devs  else ""
                clip_s = f" | clips: {', '.join(set(clips))}"     if clips else ""
                lines.append(f"  [{kind}] {name}{flags}{dev_s}{clip_s}")
            _result["ctx"] = "\n".join(lines)
        except Exception as e:
            _result["ctx"] = f"(Could not read Ableton session: {e})"

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    t.join(timeout=2.0)
    return _result["ctx"] or "(Ableton session context timed out)"

def _ableton_send(command_type, params=None):
    """Send a command to Ableton via TCP socket on localhost:9877."""
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(("localhost", 9877))
        msg = json.dumps({"type": command_type, "params": params or {}}).encode("utf-8")
        s.sendall(msg)
        import time; time.sleep(0.1)
        chunks = []
        s.settimeout(1)
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except _socket.timeout:
            pass
        s.close()
        raw = b"".join(chunks)
        return json.loads(raw) if raw else {"ok": True}
    except Exception as e:
        return {"error": str(e)}

_ABLETON_BUILD_SYSTEM = """You are an Ableton Live sound designer. The user describes a sound they want built as a new playable MIDI instrument track.
You ALWAYS create a new MIDI track with one instrument and 1-3 effects. Never build master bus chains. Always build playable instruments.
You respond with a JSON build plan — no markdown, no explanation, just valid JSON.

Available commands:
- create_midi_track: params: {}  (creates at end, returns track index in result.track_index, 1-based)
- set_track_name: params: {track_index, name}
- load_browser_item: params: {track_index, item_uri}  (loads instrument or effect)

URI catalog (use these exactly):
INSTRUMENTS:
  Shimmer Pad (Wavetable):       query:Synths#Wavetable:Pad:FileId_22940
  Voicelike Pad (Wavetable):     query:Synths#Wavetable:Pad:FileId_7056
  Drifting Ambient Pad:          query:Synths#Wavetable:Ambient%20&%20Evolving:FileId_8424
  Spacey Ambient Pad:            query:Synths#Wavetable:Ambient%20&%20Evolving:FileId_8434
  Choiriffic (vocal pad):        query:Synths#Wavetable:Ambient%20&%20Evolving:FileId_7151
  Vapor Chimes Pad:              query:Synths#Wavetable:Pad:FileId_7051
  Stars Pad:                     query:Synths#Wavetable:Pad:FileId_7037
  Bell Pad:                      query:Synths#Wavetable:Pad:FileId_6967
  Chord Eno Pad:                 query:Synths#Wavetable:Pad:FileId_6977
  Analog Soft Pad:               query:Synths#Wavetable:Pad:FileId_8190
  Spectral Movement Pad:         query:Synths#Wavetable:Pad:FileId_7036
  Dark Swell Pad:                query:Synths#Wavetable:Pad:FileId_6987
  Detuned Square Pad:            query:Synths#Wavetable:Pad:FileId_6989
  Super Bloom Pad:               query:Synths#Wavetable:Pad:FileId_7041
  Sunrise Waves (ambient):       query:Synths#Wavetable:Ambient%20&%20Evolving:FileId_7167
  Operator (FM synth):           query:Synths#Operator
  Analog (subtractive):          query:Synths#Analog
  Wavetable (blank):             query:Synths#Wavetable

REVERB:
  Long Tail (large, washy):      query:AudioFx#Reverb:Hall:FileId_9600
  Cathedral:                     query:AudioFx#Reverb:Hall:FileId_9588
  Spacious:                      query:AudioFx#Reverb:Hall:FileId_9606
  Hall Shine:                    query:AudioFx#Reverb:Hall:FileId_9595
  Vocal Hall:                    query:AudioFx#Reverb:Hall:FileId_9607
  Dark Hall:                     query:AudioFx#Reverb:Hall:FileId_9591

CHORUS / WIDTH:
  Warm Ensemble:                 query:AudioFx#Chorus-Ensemble:FileId_10186
  Ensemble Deep:                 query:AudioFx#Chorus-Ensemble:FileId_10178
  Chorus Classic:                query:AudioFx#Chorus-Ensemble:FileId_10176
  Vibrato Spacial:               query:AudioFx#Chorus-Ensemble:FileId_10182

DELAY:
  Simple Delay (base):           query:AudioFx#Simple%20Delay
  Ping Pong Delay:               query:AudioFx#Ping%20Pong%20Delay

FILTER / DYNAMICS:
  Auto Filter:                   query:AudioFx#Auto%20Filter
  Compressor:                    query:AudioFx#Compressor
  EQ Eight:                      query:AudioFx#EQ%20Eight

Output format — return ONLY this JSON, nothing else:
{
  "track_name": "descriptive name",
  "steps": [
    {"type": "create_midi_track", "params": {}},
    {"type": "set_track_name", "params": {"track_index": "__NEW__", "name": "track_name"}},
    {"type": "load_browser_item", "params": {"track_index": "__NEW__", "item_uri": "..."}},
    {"type": "load_browser_item", "params": {"track_index": "__NEW__", "item_uri": "..."}}
  ]
}

Use __NEW__ as the track_index placeholder — it will be replaced with the real index after creation.
Choose 1 instrument + 1-3 effects that together achieve the described sound."""

@app.route("/ableton/build", methods=["POST"])
def ableton_build():
    data   = request.get_json() or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "No prompt"}), 400
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not _anthropic or not api_key:
        return jsonify({"error": "Claude not available"}), 500
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_active_model, max_tokens=1024,
            system=_ABLETON_BUILD_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        plan = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({"error": "Claude returned invalid JSON", "raw": raw}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Execute the plan against Ableton
    track_index = None
    results = []
    for step in plan.get("steps", []):
        cmd   = step["type"]
        params = dict(step.get("params", {}))
        # Substitute __NEW__ with the real track index
        params = {k: (track_index if v == "__NEW__" else v) for k, v in params.items()}
        result = _ableton_send(cmd, params)
        if cmd == "create_midi_track":
            ti = result.get("result", {}).get("index")
            if ti is not None:
                track_index = ti  # socket uses 0-based indices
        results.append({"cmd": cmd, "result": result})

    return jsonify({
        "ok": True,
        "track_name": plan.get("track_name", ""),
        "track_index": track_index,
        "steps": results,
        "plan_tokens": msg.usage.output_tokens,
    })

# ── Ableton Dashboard API ────────────────────────────────────────────────────

@app.route("/api/session")
def api_session():
    return jsonify(_ableton_send("get_session_info"))

@app.route("/api/tracks")
def api_tracks():
    info = _ableton_send("get_session_info")
    count = info.get("result", {}).get("track_count", 0)
    tracks = []
    for i in range(count):
        t = _ableton_send("get_track_info", {"track_index": i})
        tracks.append(t.get("result", {}))
    return jsonify({"tracks": tracks})

@app.route("/api/track/<int:idx>")
def api_track(idx):
    return jsonify(_ableton_send("get_track_info", {"track_index": idx}))

@app.route("/api/track/<int:idx>/volume", methods=["POST"])
def api_track_volume(idx):
    d = request.get_json() or {}
    return jsonify(_ableton_send("set_track_volume", {"track_index": idx, "volume": d.get("volume", 0.85)}))

@app.route("/api/track/<int:idx>/name", methods=["POST"])
def api_track_name(idx):
    d = request.get_json() or {}
    return jsonify(_ableton_send("set_track_name", {"track_index": idx, "name": d.get("name", "")}))

@app.route("/api/track/<int:idx>/devices")
def api_track_devices(idx):
    return jsonify(_ableton_send("get_track_info", {"track_index": idx}))

@app.route("/api/track/<int:idx>/device/<int:dev>/params")
def api_device_params(idx, dev):
    return jsonify(_ableton_send("get_device_parameters", {"track_index": idx, "device_index": dev, "show_all": True}))

@app.route("/api/track/<int:idx>/device/<int:dev>/param", methods=["POST"])
def api_set_param(idx, dev):
    d = request.get_json() or {}
    return jsonify(_ableton_send("set_device_parameter", {
        "track_index": idx, "device_index": dev,
        "parameter_name": d.get("name", ""), "value": d.get("value", 0)
    }))

@app.route("/api/track/create", methods=["POST"])
def api_create_track():
    return jsonify(_ableton_send("create_midi_track", {}))

@app.route("/api/track/<int:idx>/delete", methods=["POST"])
def api_delete_track(idx):
    return jsonify(_ableton_send("delete_track", {"track_index": idx}))

@app.route("/api/tempo", methods=["POST"])
def api_tempo():
    d = request.get_json() or {}
    return jsonify(_ableton_send("set_tempo", {"tempo": d.get("tempo", 120)}))

@app.route("/api/playback/start", methods=["POST"])
def api_play():
    return jsonify(_ableton_send("start_playback", {}))

@app.route("/api/playback/stop", methods=["POST"])
def api_stop():
    return jsonify(_ableton_send("stop_playback", {}))

@app.route("/api/clip/fire", methods=["POST"])
def api_fire_clip():
    d = request.get_json() or {}
    return jsonify(_ableton_send("fire_clip", {"track_index": d.get("track_index"), "clip_index": d.get("clip_index")}))

@app.route("/api/clip/stop", methods=["POST"])
def api_stop_clip():
    d = request.get_json() or {}
    return jsonify(_ableton_send("stop_clip", {"track_index": d.get("track_index"), "clip_index": d.get("clip_index")}))

@app.route("/api/clip/create", methods=["POST"])
def api_create_clip():
    d = request.get_json() or {}
    return jsonify(_ableton_send("create_clip", {"track_index": d.get("track_index"), "clip_index": d.get("clip_index", 1), "length": d.get("length", 4)}))

@app.route("/api/arrangement")
def api_arrangement():
    return jsonify(_ableton_send("get_arrangement_info", {}))

@app.route("/api/browser/tree")
def api_browser_tree():
    return jsonify(_ableton_send("get_browser_tree", {"category_type": "all"}))

@app.route("/api/plugins")
def api_plugins():
    return jsonify(_ableton_send("list_external_plugins", {}))

@app.route("/api/load_instrument", methods=["POST"])
def api_load_instrument():
    d = request.get_json() or {}
    return jsonify(_ableton_send("load_instrument_or_effect", {"track_index": d.get("track_index"), "uri": d.get("uri", "")}))

@app.route("/api/cue_points")
def api_cue_points():
    return jsonify(_ableton_send("get_cue_points", {}))

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gain Studio — Ableton Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
:root{
  --bg:#030507;--panel:#060A0F;--panel2:#0A1018;--border:#162030;--border2:#1E2E40;
  --text:#D8EAF8;--dim:#405870;--teal:#009690;--purple:#7B2FD4;--gold:#C8A843;
  --red:#C84030;--green:#28A060;
}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;font-size:12px;height:100vh;overflow:hidden;display:flex;flex-direction:column;}

/* Header */
.hdr{display:flex;align-items:center;gap:16px;padding:0 16px;height:48px;border-bottom:1px solid var(--border);background:rgba(3,5,7,.95);flex-shrink:0;}
.brand{font-size:14px;font-weight:900;letter-spacing:3px;color:var(--teal);}
.sep{width:1px;height:20px;background:var(--border2);}
.transport{display:flex;align-items:center;gap:6px;}
.tbtn{width:30px;height:28px;border-radius:4px;border:1px solid var(--border2);background:var(--panel2);color:var(--text);cursor:pointer;font-size:11px;display:flex;align-items:center;justify-content:center;transition:all .15s;}
.tbtn:hover{border-color:var(--teal);color:var(--teal);}
.tbtn.active{background:var(--teal);color:#000;border-color:var(--teal);}
.tempo-box{display:flex;align-items:center;gap:6px;}
.tempo-box label{font-size:9px;font-weight:800;letter-spacing:.15em;color:var(--dim);text-transform:uppercase;}
.tempo-input{width:56px;height:28px;background:var(--panel2);border:1px solid var(--border2);border-radius:4px;color:var(--teal);font-size:13px;font-weight:700;text-align:center;font-family:inherit;}
.session-info{margin-left:auto;display:flex;gap:16px;align-items:center;}
.stat{font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--dim);}
.stat span{color:var(--text);}

/* Layout */
.main{display:flex;flex:1;overflow:hidden;}

/* Tracks panel */
.tracks-panel{width:220px;border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;}
.panel-hdr{padding:8px 12px;font-size:9px;font-weight:800;letter-spacing:.2em;color:var(--dim);text-transform:uppercase;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;}
.add-btn{width:18px;height:18px;border-radius:3px;border:1px solid var(--border2);background:transparent;color:var(--dim);cursor:pointer;font-size:13px;line-height:1;display:flex;align-items:center;justify-content:center;transition:all .15s;}
.add-btn:hover{border-color:var(--teal);color:var(--teal);}
.track-list{flex:1;overflow-y:auto;}
.track-item{padding:6px 10px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s;display:flex;align-items:center;gap:8px;}
.track-item:hover{background:var(--panel2);}
.track-item.selected{background:rgba(0,150,144,.08);border-left:2px solid var(--teal);}
.track-num{font-size:9px;font-weight:700;color:var(--dim);width:16px;flex-shrink:0;}
.track-name{flex:1;font-size:11px;font-weight:600;truncate;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.track-midi{font-size:8px;padding:1px 4px;border-radius:2px;background:rgba(0,150,144,.15);color:var(--teal);font-weight:700;flex-shrink:0;}
.track-audio{font-size:8px;padding:1px 4px;border-radius:2px;background:rgba(123,47,212,.15);color:var(--purple);font-weight:700;flex-shrink:0;}
.vol-bar{width:40px;height:4px;background:var(--border2);border-radius:2px;flex-shrink:0;}
.vol-fill{height:100%;background:var(--teal);border-radius:2px;transition:width .2s;}

/* Inspector */
.inspector{flex:1;display:flex;flex-direction:column;border-right:1px solid var(--border);overflow:hidden;}
.inspector-body{flex:1;overflow-y:auto;padding:14px;}
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--dim);gap:8px;}
.empty-icon{font-size:32px;opacity:.3;}
.section-title{font-size:9px;font-weight:800;letter-spacing:.2em;color:var(--dim);text-transform:uppercase;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border);}
.track-detail-name{font-size:18px;font-weight:900;color:var(--text);margin-bottom:4px;}
.track-detail-type{font-size:10px;color:var(--dim);margin-bottom:14px;}
.vol-row{display:flex;align-items:center;gap:10px;margin-bottom:16px;}
.vol-label{font-size:9px;font-weight:700;color:var(--dim);width:40px;}
.vol-slider{flex:1;-webkit-appearance:none;height:4px;background:var(--border2);border-radius:2px;outline:none;}
.vol-slider::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--teal);cursor:pointer;}
.vol-val{font-size:10px;font-weight:700;color:var(--teal);width:36px;text-align:right;}
.device-card{background:var(--panel2);border:1px solid var(--border2);border-radius:6px;padding:10px 12px;margin-bottom:8px;cursor:pointer;transition:border-color .15s;}
.device-card:hover{border-color:var(--teal);}
.device-card.open{border-color:var(--teal);}
.device-name{font-size:11px;font-weight:700;margin-bottom:2px;}
.device-class{font-size:9px;color:var(--dim);}
.params-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px;}
.param-row{background:var(--panel);border-radius:4px;padding:6px 8px;}
.param-name{font-size:9px;color:var(--dim);margin-bottom:3px;truncate;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.param-slider{width:100%;-webkit-appearance:none;height:3px;background:var(--border2);border-radius:2px;outline:none;margin-bottom:2px;}
.param-slider::-webkit-slider-thumb{-webkit-appearance:none;width:10px;height:10px;border-radius:50%;background:var(--purple);cursor:pointer;}
.param-val{font-size:9px;color:var(--purple);font-weight:700;}

/* AI Panel */
.ai-panel{width:280px;display:flex;flex-direction:column;flex-shrink:0;}
.mode-tabs{display:flex;border-bottom:1px solid var(--border);}
.mode-tab{flex:1;padding:8px;text-align:center;font-size:9px;font-weight:800;letter-spacing:.15em;color:var(--dim);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;text-transform:uppercase;}
.mode-tab.active{color:var(--teal);border-bottom-color:var(--teal);}
.ai-body{flex:1;display:flex;flex-direction:column;padding:12px;gap:10px;overflow:hidden;min-height:0;}
.prompt-input{width:100%;background:var(--panel2);border:1px solid var(--border2);border-radius:6px;padding:8px 10px;color:var(--text);font-size:11px;font-family:inherit;resize:none;height:60px;transition:border-color .15s;}
.prompt-input:focus{outline:none;border-color:var(--teal);}
.action-row{display:flex;gap:6px;}
.action-btn{flex:1;padding:9px;border-radius:6px;border:none;font-size:10px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;transition:all .2s;}
.build-btn{background:var(--teal);color:#000;}
.build-btn:hover{opacity:.85;}
.explore-btn{background:var(--panel2);border:1px solid var(--border2);color:var(--text);}
.explore-btn:hover{border-color:var(--purple);color:var(--purple);}
.ai-status{font-size:10px;color:var(--dim);min-height:16px;}
.ai-response{flex:1;overflow-y:auto;background:var(--panel2);border-radius:6px;padding:10px;font-size:11px;line-height:1.6;color:var(--text);white-space:pre-wrap;}

/* Tools bar */
.tools-bar{padding:8px 12px;border-top:1px solid var(--border);display:flex;gap:6px;flex-wrap:wrap;flex-shrink:0;background:var(--panel);}
.tool-btn{padding:5px 10px;border-radius:4px;border:1px solid var(--border2);background:var(--panel2);color:var(--dim);font-size:9px;font-weight:700;letter-spacing:.1em;cursor:pointer;transition:all .15s;text-transform:uppercase;}
.tool-btn:hover{border-color:var(--teal);color:var(--teal);}

/* Tools catalog */
.tools-catalog{flex:1;overflow-y:auto;display:none;flex-direction:column;gap:0;min-height:0;}
.tools-catalog.active{display:flex;}
.tool-group{margin-bottom:0;}
.tool-group-hdr{padding:7px 12px;font-size:9px;font-weight:900;letter-spacing:.2em;color:var(--teal);text-transform:uppercase;background:rgba(0,150,144,.06);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:1;}
.tool-entry{padding:7px 12px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:2px;cursor:default;transition:background .1s;}
.tool-entry:hover{background:var(--panel2);}
.tool-name{font-size:10px;font-weight:800;color:var(--text);font-family:'SF Mono',monospace;letter-spacing:-.01em;}
.tool-desc{font-size:9px;color:var(--dim);line-height:1.4;}

/* Scrollbars */
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px;}

/* Toast */
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--teal);color:#000;padding:8px 16px;border-radius:6px;font-size:11px;font-weight:800;opacity:0;transition:opacity .2s;pointer-events:none;z-index:999;}
.toast.show{opacity:1;}

/* Modal */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:100;}
.modal-overlay.show{display:flex;}
.modal{background:var(--panel);border:1px solid var(--border2);border-radius:10px;padding:20px;width:320px;display:flex;flex-direction:column;gap:12px;}
.modal-title{font-size:13px;font-weight:900;letter-spacing:.1em;}
.modal-input{width:100%;background:var(--panel2);border:1px solid var(--border2);border-radius:6px;padding:8px 10px;color:var(--text);font-size:12px;font-family:inherit;}
.modal-input:focus{outline:none;border-color:var(--teal);}
.modal-btns{display:flex;gap:8px;justify-content:flex-end;}
.modal-btn{padding:7px 16px;border-radius:5px;border:none;font-size:10px;font-weight:800;cursor:pointer;}
.modal-cancel{background:var(--panel2);color:var(--dim);}
.modal-ok{background:var(--teal);color:#000;}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <div class="brand">GAIN STUDIO</div>
  <div class="sep"></div>
  <div class="transport">
    <button class="tbtn" id="play-btn" onclick="play()" title="Play">▶</button>
    <button class="tbtn" id="stop-btn" onclick="stop()" title="Stop">■</button>
  </div>
  <div class="sep"></div>
  <div class="tempo-box">
    <label>BPM</label>
    <input class="tempo-input" id="tempo" type="number" min="20" max="999" value="120" onchange="setTempo(this.value)">
  </div>
  <div class="sep"></div>
  <div class="session-info">
    <div class="stat">TRACKS <span id="track-count">—</span></div>
    <div class="stat">TIME SIG <span id="time-sig">—</span></div>
  </div>
</div>

<!-- Main -->
<div class="main">

  <!-- Track List -->
  <div class="tracks-panel">
    <div class="panel-hdr">
      Tracks
      <button class="add-btn" onclick="createTrack()" title="New MIDI Track">+</button>
    </div>
    <div class="track-list" id="track-list">
      <div class="empty-state" style="height:80px;"><div class="stat">Loading...</div></div>
    </div>
  </div>

  <!-- Inspector -->
  <div class="inspector">
    <div class="panel-hdr">Inspector</div>
    <div class="inspector-body" id="inspector-body">
      <div class="empty-state">
        <div class="empty-icon">🎛</div>
        <div class="stat">Select a track</div>
      </div>
    </div>
  </div>

  <!-- AI Panel -->
  <div class="ai-panel">
    <div class="mode-tabs">
      <div class="mode-tab active" id="tab-build" onclick="setMode('BUILD')">Build</div>
      <div class="mode-tab" id="tab-explore" onclick="setMode('EXPLORE')">Explore</div>
      <div class="mode-tab" id="tab-tools" onclick="setMode('TOOLS')">Tools</div>
    </div>
    <div class="ai-body">
      <textarea class="prompt-input" id="prompt" placeholder="Describe a sound or ask about your session..."></textarea>
      <div class="action-row">
        <button class="action-btn build-btn" id="build-btn" onclick="runBuild()">Build</button>
        <button class="action-btn explore-btn" id="explore-btn" onclick="runExplore()">Explore</button>
      </div>
      <div class="ai-status" id="ai-status"></div>
      <div class="ai-response" id="ai-response"></div>
      <div class="tools-catalog" id="tools-catalog">

        <div class="tool-group">
          <div class="tool-group-hdr">Session &amp; Info</div>
          <div class="tool-entry"><div class="tool-name">get_session_info</div><div class="tool-desc">Tempo, time sig, track count, playback state</div></div>
          <div class="tool-entry"><div class="tool-name">get_track_info</div><div class="tool-desc">Name, type, armed, volume, devices, clips for one track</div></div>
          <div class="tool-entry"><div class="tool-name">get_track_volume</div><div class="tool-desc">Current volume level of a track</div></div>
          <div class="tool-entry"><div class="tool-name">get_arrangement_info</div><div class="tool-desc">All clips, length, and markers in arrangement view</div></div>
          <div class="tool-entry"><div class="tool-name">get_cue_points</div><div class="tool-desc">All cue markers with names and bar positions</div></div>
          <div class="tool-entry"><div class="tool-name">get_track_deletion_status</div><div class="tool-desc">Whether a pending track delete has completed</div></div>
        </div>

        <div class="tool-group">
          <div class="tool-group-hdr">Tracks</div>
          <div class="tool-entry"><div class="tool-name">create_midi_track</div><div class="tool-desc">Add a new MIDI track to the session</div></div>
          <div class="tool-entry"><div class="tool-name">delete_track</div><div class="tool-desc">Remove a track by index</div></div>
          <div class="tool-entry"><div class="tool-name">set_track_name</div><div class="tool-desc">Rename a track</div></div>
          <div class="tool-entry"><div class="tool-name">set_track_volume</div><div class="tool-desc">Set volume level (0.0 – 1.0)</div></div>
          <div class="tool-entry"><div class="tool-name">set_track_panning</div><div class="tool-desc">Set pan position (-1 left → 0 center → 1 right)</div></div>
        </div>

        <div class="tool-group">
          <div class="tool-group-hdr">Clips</div>
          <div class="tool-entry"><div class="tool-name">create_clip</div><div class="tool-desc">New empty MIDI clip in session view slot</div></div>
          <div class="tool-entry"><div class="tool-name">fire_clip</div><div class="tool-desc">Launch a clip in session view</div></div>
          <div class="tool-entry"><div class="tool-name">stop_clip</div><div class="tool-desc">Stop a playing clip</div></div>
          <div class="tool-entry"><div class="tool-name">set_clip_name</div><div class="tool-desc">Rename a clip</div></div>
          <div class="tool-entry"><div class="tool-name">add_notes_to_clip</div><div class="tool-desc">Write MIDI notes into an existing clip (pitch, time, duration, velocity)</div></div>
          <div class="tool-entry"><div class="tool-name">manage_clip_automation</div><div class="tool-desc">Set automation envelope data inside a clip</div></div>
          <div class="tool-entry"><div class="tool-name">duplicate_clip_to_arrangement</div><div class="tool-desc">Copy a session-view clip into the arrangement timeline</div></div>
        </div>

        <div class="tool-group">
          <div class="tool-group-hdr">Arrangement</div>
          <div class="tool-entry"><div class="tool-name">create_arrangement_midi_clip</div><div class="tool-desc">Place a MIDI clip at a specific timeline position and length</div></div>
          <div class="tool-entry"><div class="tool-name">create_arrangement_audio_clip</div><div class="tool-desc">Place an audio clip at a specific timeline position</div></div>
          <div class="tool-entry"><div class="tool-name">delete_arrangement_clip</div><div class="tool-desc">Remove a clip from the arrangement view</div></div>
          <div class="tool-entry"><div class="tool-name">set_arrangement_clip_property</div><div class="tool-desc">Change loop, position, length, or warp settings on an arrangement clip</div></div>
          <div class="tool-entry"><div class="tool-name">set_arrangement_loop</div><div class="tool-desc">Set the global loop start and end points</div></div>
          <div class="tool-entry"><div class="tool-name">control_arrangement_view</div><div class="tool-desc">Scroll or zoom the arrangement timeline view</div></div>
          <div class="tool-entry"><div class="tool-name">set_song_time</div><div class="tool-desc">Jump the playhead to a specific beat/bar position</div></div>
        </div>

        <div class="tool-group">
          <div class="tool-group-hdr">Devices</div>
          <div class="tool-entry"><div class="tool-name">get_device_parameters</div><div class="tool-desc">List all parameters (name, value, min, max) for a device</div></div>
          <div class="tool-entry"><div class="tool-name">set_device_parameter</div><div class="tool-desc">Set a device parameter by name and value</div></div>
          <div class="tool-entry"><div class="tool-name">delete_device</div><div class="tool-desc">Remove a device from a track's chain</div></div>
          <div class="tool-entry"><div class="tool-name">enable_device</div><div class="tool-desc">Turn a device on</div></div>
          <div class="tool-entry"><div class="tool-name">disable_device</div><div class="tool-desc">Turn a device off (bypass)</div></div>
          <div class="tool-entry"><div class="tool-name">navigate_device_preset</div><div class="tool-desc">Cycle forward or backward through a device's presets</div></div>
          <div class="tool-entry"><div class="tool-name">get_chain_info</div><div class="tool-desc">Devices and routing inside a rack chain</div></div>
          <div class="tool-entry"><div class="tool-name">get_drum_pad_info</div><div class="tool-desc">Drum rack pad assignments, notes, and choke groups</div></div>
        </div>

        <div class="tool-group">
          <div class="tool-group-hdr">Instruments &amp; Browser</div>
          <div class="tool-entry"><div class="tool-name">load_instrument_or_effect</div><div class="tool-desc">Load any instrument or effect by its browser URI</div></div>
          <div class="tool-entry"><div class="tool-name">load_external_plugin</div><div class="tool-desc">Load a specific VST/AU plugin by name</div></div>
          <div class="tool-entry"><div class="tool-name">load_drum_kit</div><div class="tool-desc">Load a drum kit preset into a Drum Rack</div></div>
          <div class="tool-entry"><div class="tool-name">list_external_plugins</div><div class="tool-desc">All installed VST/AU plugins Ableton can see</div></div>
          <div class="tool-entry"><div class="tool-name">get_browser_tree</div><div class="tool-desc">Top-level Ableton library categories (Instruments, Samples, etc.)</div></div>
          <div class="tool-entry"><div class="tool-name">get_browser_items_at_path</div><div class="tool-desc">Items inside a specific browser folder path</div></div>
        </div>

        <div class="tool-group">
          <div class="tool-group-hdr">Transport &amp; View</div>
          <div class="tool-entry"><div class="tool-name">start_playback</div><div class="tool-desc">Press Play</div></div>
          <div class="tool-entry"><div class="tool-name">stop_playback</div><div class="tool-desc">Press Stop</div></div>
          <div class="tool-entry"><div class="tool-name">set_tempo</div><div class="tool-desc">Change the session BPM</div></div>
          <div class="tool-entry"><div class="tool-name">set_ableton_view</div><div class="tool-desc">Switch to Session, Arrangement, Detail, or Browser view</div></div>
          <div class="tool-entry"><div class="tool-name">jump_to_cue_point</div><div class="tool-desc">Move playhead to a named cue marker</div></div>
          <div class="tool-entry"><div class="tool-name">create_cue_point</div><div class="tool-desc">Add a cue marker at the current playhead position</div></div>
          <div class="tool-entry"><div class="tool-name">delete_cue_point</div><div class="tool-desc">Remove a cue marker by name or index</div></div>
        </div>

        <div class="tool-group">
          <div class="tool-group-hdr">Ableton Knowledge</div>
          <div class="tool-entry"><div class="tool-name">search_live_manual</div><div class="tool-desc">Search the official Ableton Live manual</div></div>
          <div class="tool-entry"><div class="tool-name">search_knowledge_base</div><div class="tool-desc">General Ableton tips and techniques database</div></div>
          <div class="tool-entry"><div class="tool-name">search_transcripts</div><div class="tool-desc">Search tutorial video transcripts</div></div>
          <div class="tool-entry"><div class="tool-name">search_videos</div><div class="tool-desc">Find relevant Ableton tutorial videos</div></div>
          <div class="tool-entry"><div class="tool-name">search_push_manual</div><div class="tool-desc">Search the Push hardware manual</div></div>
          <div class="tool-entry"><div class="tool-name">search_note_manual</div><div class="tool-desc">Search the Note app manual</div></div>
        </div>

      </div>
    </div>
  </div>
</div>

<!-- Tools bar -->
<div class="tools-bar">
  <button class="tool-btn" onclick="showTempoModal()">Set Tempo</button>
  <button class="tool-btn" onclick="createTrack()">+ MIDI Track</button>
  <button class="tool-btn" onclick="loadSession()">Refresh</button>
  <button class="tool-btn" onclick="showPlugins()">List Plugins</button>
  <button class="tool-btn" onclick="showArrangement()">Arrangement</button>
  <button class="tool-btn" onclick="showCuePoints()">Cue Points</button>
  <button class="tool-btn" onclick="deleteSelectedTrack()">Delete Track</button>
  <button class="tool-btn" onclick="window.open('http://127.0.0.1:5570','_blank')">Open Gain →</button>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<!-- Tempo Modal -->
<div class="modal-overlay" id="tempo-modal">
  <div class="modal">
    <div class="modal-title">SET TEMPO</div>
    <input class="modal-input" id="tempo-val" type="number" min="20" max="999" value="120" placeholder="BPM">
    <div class="modal-btns">
      <button class="modal-btn modal-cancel" onclick="closeModal('tempo-modal')">Cancel</button>
      <button class="modal-btn modal-ok" onclick="applyTempo()">Set</button>
    </div>
  </div>
</div>

<!-- Generic Output Modal -->
<div class="modal-overlay" id="output-modal">
  <div class="modal" style="width:480px;max-height:70vh;">
    <div class="modal-title" id="output-title">Output</div>
    <pre id="output-body" style="font-size:10px;color:var(--dim);overflow-y:auto;max-height:400px;white-space:pre-wrap;"></pre>
    <div class="modal-btns">
      <button class="modal-btn modal-ok" onclick="closeModal('output-modal')">Close</button>
    </div>
  </div>
</div>

<script>
let selectedTrack = null;
let currentMode = 'BUILD';
let openDevices = {};

// ── Session ──────────────────────────────────────────────────────────────────

async function loadSession() {
  try {
    const r = await fetch('/api/session');
    const d = await r.json();
    const s = d.result || {};
    document.getElementById('tempo').value = Math.round(s.tempo || 120);
    document.getElementById('track-count').textContent = s.track_count || '—';
    document.getElementById('time-sig').textContent = s.signature_numerator + '/' + s.signature_denominator || '—';
    loadTracks();
  } catch(e) { toast('Cannot reach Ableton', true); }
}

async function loadTracks() {
  try {
    const r = await fetch('/api/tracks');
    const d = await r.json();
    const tracks = d.tracks || [];
    const list = document.getElementById('track-list');
    list.innerHTML = '';
    tracks.forEach((t, i) => {
      const vol = t.volume || 0.85;
      const volPct = Math.round((vol / 1.0) * 100);
      const isMidi = t.is_midi_track;
      const div = document.createElement('div');
      div.className = 'track-item' + (selectedTrack === i ? ' selected' : '');
      div.onclick = () => selectTrack(i, t);
      div.innerHTML = `
        <span class="track-num">${i+1}</span>
        <span class="track-name" title="${t.name || ''}">${t.name || 'Track ${i+1}'}</span>
        <span class="${isMidi ? 'track-midi' : 'track-audio'}">${isMidi ? 'M' : 'A'}</span>
        <div class="vol-bar"><div class="vol-fill" style="width:${volPct}%"></div></div>
      `;
      list.appendChild(div);
    });
  } catch(e) {}
}

// ── Track Selection ───────────────────────────────────────────────────────────

async function selectTrack(idx, trackData) {
  selectedTrack = idx;
  loadTracks();
  const body = document.getElementById('inspector-body');
  body.innerHTML = '<div class="empty-state"><div class="stat">Loading...</div></div>';

  // Full track info
  const r = await fetch(`/api/track/${idx}`);
  const d = await r.json();
  const t = d.result || {};

  const vol = t.volume || 0.85;
  const pan = t.panning || 0;

  let devHtml = '';
  (t.devices || []).forEach((dev, di) => {
    devHtml += `
      <div class="device-card" id="dev-${di}" onclick="toggleDevice(${idx}, ${di+1}, '${(dev.name||'').replace(/'/g,"\\\\'")}')">
        <div class="device-name">${dev.name || 'Device ' + (di+1)}</div>
        <div class="device-class">${dev.class_name || ''}</div>
        <div id="params-${di}"></div>
      </div>`;
  });
  if (!devHtml) devHtml = '<div class="stat" style="color:var(--dim);margin-top:6px;">No devices</div>';

  body.innerHTML = `
    <div class="track-detail-name">${t.name || 'Track ' + (idx+1)}</div>
    <div class="track-detail-type">${t.is_midi_track ? 'MIDI Track' : 'Audio Track'} · Track ${idx+1}</div>
    <div class="vol-row">
      <span class="vol-label">VOL</span>
      <input type="range" class="vol-slider" min="0" max="1" step="0.01" value="${vol}"
        oninput="updateVol(${idx}, this.value)" onchange="setVol(${idx}, this.value)">
      <span class="vol-val" id="vol-val-${idx}">${Math.round(vol*100)}%</span>
    </div>
    <div class="vol-row">
      <span class="vol-label">PAN</span>
      <input type="range" class="vol-slider" min="-1" max="1" step="0.01" value="${pan}"
        onchange="setPan(${idx}, this.value)">
      <span class="vol-val">${pan > 0 ? 'R' : pan < 0 ? 'L' : 'C'}</span>
    </div>
    <div class="section-title" style="margin-top:14px;">Devices</div>
    ${devHtml}
  `;
}

function updateVol(idx, v) {
  document.getElementById('vol-val-' + idx).textContent = Math.round(v*100) + '%';
}

async function setVol(idx, v) {
  await fetch(`/api/track/${idx}/volume`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({volume: parseFloat(v)})});
  toast('Volume set');
}

async function setPan(idx, v) {
  await fetch(`/api/track/${idx}/panning`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({panning: parseFloat(v)})}).catch(()=>{});
}

async function toggleDevice(trackIdx, devIdx, name) {
  const key = trackIdx + '-' + devIdx;
  const card = document.getElementById('dev-' + (devIdx-1));
  const paramsEl = document.getElementById('params-' + (devIdx-1));
  if (openDevices[key]) {
    openDevices[key] = false;
    card.classList.remove('open');
    paramsEl.innerHTML = '';
    return;
  }
  openDevices[key] = true;
  card.classList.add('open');
  paramsEl.innerHTML = '<div class="stat" style="margin-top:8px;">Loading params...</div>';
  const r = await fetch(`/api/track/${trackIdx}/device/${devIdx}/params`);
  const d = await r.json();
  const params = d.result?.parameters || [];
  if (!params.length) { paramsEl.innerHTML = '<div class="stat" style="margin-top:8px;color:var(--dim);">No parameters</div>'; return; }
  let html = '<div class="params-grid">';
  params.slice(0,12).forEach((p, pi) => {
    const norm = p.value != null ? ((p.value - (p.min||0)) / ((p.max||1) - (p.min||0))) : 0.5;
    html += `<div class="param-row">
      <div class="param-name" title="${p.name}">${p.name}</div>
      <input type="range" class="param-slider" min="0" max="1" step="0.001" value="${norm}"
        onchange="setParam(${trackIdx},${devIdx},'${(p.name||'').replace(/'/g,"\\\\'")}',this.value,${p.min||0},${p.max||1})">
      <div class="param-val">${p.display_value || (p.value != null ? p.value.toFixed(2) : '—')}</div>
    </div>`;
  });
  html += '</div>';
  paramsEl.innerHTML = html;
}

async function setParam(trackIdx, devIdx, name, normVal, min, max) {
  const val = min + normVal * (max - min);
  await fetch(`/api/track/${trackIdx}/device/${devIdx}/param`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, value: val})
  });
  toast('Parameter set');
}

// ── Transport ─────────────────────────────────────────────────────────────────

async function play() {
  await fetch('/api/playback/start', {method:'POST'});
  document.getElementById('play-btn').classList.add('active');
  document.getElementById('stop-btn').classList.remove('active');
  toast('Playing');
}

async function stop() {
  await fetch('/api/playback/stop', {method:'POST'});
  document.getElementById('stop-btn').classList.add('active');
  document.getElementById('play-btn').classList.remove('active');
  toast('Stopped');
}

async function setTempo(bpm) {
  await fetch('/api/tempo', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tempo: parseFloat(bpm)})});
  toast('Tempo → ' + bpm + ' BPM');
}

// ── Track Actions ─────────────────────────────────────────────────────────────

async function createTrack() {
  const r = await fetch('/api/track/create', {method:'POST'});
  toast('MIDI track created');
  loadSession();
}

async function deleteSelectedTrack() {
  if (selectedTrack === null) { toast('Select a track first', true); return; }
  if (!confirm('Delete track ' + (selectedTrack+1) + '?')) return;
  await fetch(`/api/track/${selectedTrack}/delete`, {method:'POST'});
  selectedTrack = null;
  document.getElementById('inspector-body').innerHTML = '<div class="empty-state"><div class="empty-icon">🎛</div><div class="stat">Select a track</div></div>';
  toast('Track deleted');
  loadSession();
}

// ── AI Panel ──────────────────────────────────────────────────────────────────

function setMode(mode) {
  currentMode = mode;
  document.getElementById('tab-build').classList.toggle('active', mode==='BUILD');
  document.getElementById('tab-explore').classList.toggle('active', mode==='EXPLORE');
  document.getElementById('tab-tools').classList.toggle('active', mode==='TOOLS');

  const isTools = mode === 'TOOLS';
  const isBuild = mode === 'BUILD';
  const isExplore = mode === 'EXPLORE';

  // Show/hide panel sections
  document.getElementById('prompt').style.display = isTools ? 'none' : '';
  document.querySelector('.action-row').style.display = isTools ? 'none' : '';
  document.getElementById('ai-status').style.display = isTools ? 'none' : '';
  document.getElementById('ai-response').style.display = isTools ? 'none' : '';
  document.getElementById('tools-catalog').classList.toggle('active', isTools);

  // Visual feedback for Build vs Explore
  if (!isTools) {
    const buildBtn = document.getElementById('build-btn');
    const exploreBtn = document.getElementById('explore-btn');
    const prompt = document.getElementById('prompt');
    buildBtn.style.opacity = isBuild ? '1' : '0.35';
    exploreBtn.style.opacity = isExplore ? '1' : '0.35';
    prompt.placeholder = isBuild
      ? 'Describe a sound to build in Ableton...'
      : 'Ask anything about your session or sound design...';
  }
}

async function runBuild() {
  const p = document.getElementById('prompt').value.trim();
  if (!p) return;
  const btn = document.getElementById('build-btn');
  btn.disabled = true;
  document.getElementById('ai-status').textContent = 'Building...';
  document.getElementById('ai-response').textContent = '';
  try {
    const r = await fetch('/ableton/build', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt:p})});
    const d = await r.json();
    if (d.error) {
      document.getElementById('ai-status').textContent = 'Error: ' + d.error;
    } else {
      document.getElementById('ai-status').textContent = d.steps.length + ' steps · ' + d.plan_tokens + ' tok';
      document.getElementById('ai-response').textContent = 'Built: ' + d.track_name;
      document.getElementById('prompt').value = '';
      loadSession();
    }
  } catch(e) { document.getElementById('ai-status').textContent = 'Error: Gain not running'; }
  btn.disabled = false;
}

async function runExplore() {
  const p = document.getElementById('prompt').value.trim();
  if (!p) return;
  const btn = document.getElementById('explore-btn');
  btn.disabled = true;
  document.getElementById('ai-status').textContent = 'Asking Claude...';
  document.getElementById('ai-response').textContent = '';
  try {
    const r = await fetch('/explore', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt:p})});
    const d = await r.json();
    if (d.error) {
      document.getElementById('ai-status').textContent = 'Error: ' + d.error;
    } else {
      document.getElementById('ai-status').textContent = d.tokens + ' tok';
      document.getElementById('ai-response').textContent = d.text;
      document.getElementById('prompt').value = '';
    }
  } catch(e) { document.getElementById('ai-status').textContent = 'Error'; }
  btn.disabled = false;
}

document.getElementById('prompt').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); currentMode === 'BUILD' ? runBuild() : runExplore(); }
});

// ── Tool Buttons ──────────────────────────────────────────────────────────────

function showTempoModal() {
  document.getElementById('tempo-val').value = document.getElementById('tempo').value;
  document.getElementById('tempo-modal').classList.add('show');
}

async function applyTempo() {
  const v = document.getElementById('tempo-val').value;
  await setTempo(v);
  document.getElementById('tempo').value = v;
  closeModal('tempo-modal');
}

async function showPlugins() {
  const r = await fetch('/api/plugins');
  const d = await r.json();
  const plugins = d.result?.plugins || [];
  showOutput('External Plugins (' + plugins.length + ')', plugins.map(p => p.name || p).join('\\n') || 'None found');
}

async function showArrangement() {
  const r = await fetch('/api/arrangement');
  const d = await r.json();
  showOutput('Arrangement', JSON.stringify(d.result || d, null, 2));
}

async function showCuePoints() {
  const r = await fetch('/api/cue_points');
  const d = await r.json();
  const pts = d.result?.cue_points || [];
  showOutput('Cue Points', pts.length ? pts.map(p => 'Bar ' + p.bar + ': ' + p.name).join('\\n') : 'No cue points');
}

function showOutput(title, body) {
  document.getElementById('output-title').textContent = title;
  document.getElementById('output-body').textContent = body;
  document.getElementById('output-modal').classList.add('show');
}

function closeModal(id) { document.getElementById(id).classList.remove('show'); }

// ── Toast ─────────────────────────────────────────────────────────────────────

let toastTimer;
function toast(msg, err=false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = err ? 'var(--red)' : 'var(--teal)';
  el.style.color = err ? '#fff' : '#000';
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2000);
}

// ── Init ──────────────────────────────────────────────────────────────────────

loadSession();
setInterval(loadSession, 5000);
</script>
</body>
</html>"""

@app.route("/dashboard")
def dashboard():
    return DASHBOARD_HTML

# Always open clean — reset to neutral state on every server start
write_state(DEFAULT_STATE.copy())

if __name__ == "__main__":
    print(f"┌──────────────────────────────────────────────┐")
    print(f"│   Control  ·  visual console                 │")
    print(f"│   http://127.0.0.1:{PORT}                      │")
    print(f"│   Open on iPad: http://<your-mac-ip>:{PORT}   │")
    print(f"│   Ctrl+C to stop                             │")
    print(f"└──────────────────────────────────────────────┘")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
