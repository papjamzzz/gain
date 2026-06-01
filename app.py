from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
import os, json, requests
from pathlib import Path
from datetime import datetime

load_dotenv()
app = Flask(__name__)

WAITLIST_FILE = Path(__file__).parent / 'data' / 'waitlist.json'

def load_waitlist():
    if WAITLIST_FILE.exists():
        return json.loads(WAITLIST_FILE.read_text())
    return []

def save_waitlist(entries):
    WAITLIST_FILE.parent.mkdir(exist_ok=True)
    WAITLIST_FILE.write_text(json.dumps(entries, indent=2))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/waitlist', methods=['POST'])
def waitlist():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'error': 'invalid email'}), 400
    entries = load_waitlist()
    if any(e['email'] == email for e in entries):
        return jsonify({'status': 'already_registered'})
    entries.append({'email': email, 'joined': datetime.utcnow().isoformat()})
    save_waitlist(entries)
    # Forward to your inbox via Resend
    resend_key = os.getenv('RESEND_API_KEY', '')
    if resend_key:
        try:
            # Add to Resend Audience
            requests.post('https://api.resend.com/audiences/7ed2a7d8-e897-454f-937b-9279f67fd845/contacts', headers={
                'Authorization': f'Bearer {resend_key}',
                'Content-Type': 'application/json'
            }, json={'email': email, 'unsubscribed': False}, timeout=5)
            # Notify inbox
            requests.post('https://api.resend.com/emails', headers={
                'Authorization': f'Bearer {resend_key}',
                'Content-Type': 'application/json'
            }, json={
                'from': 'Gain <noreply@creativekonsoles.com>',
                'to': ['jeremiahstephensmith@gmail.com'],
                'subject': f'Gain waitlist: {email}',
                'html': f'<p>New signup: <strong>{email}</strong></p><p>{datetime.utcnow().isoformat()}</p>'
            }, timeout=5)
        except Exception:
            pass
    return jsonify({'status': 'ok'})

@app.route('/waitlist/view')
def waitlist_view():
    pw = request.args.get('pw', '')
    if pw != os.getenv('ADMIN_PW', 'gain2026'):
        return '<h2 style="font-family:sans-serif;padding:40px">Access denied. Add ?pw=yourpassword to the URL.</h2>', 403
    entries = load_waitlist()
    rows = ''.join(
        f'<tr><td style="padding:8px 16px;border-bottom:1px solid #eee">{i+1}</td>'
        f'<td style="padding:8px 16px;border-bottom:1px solid #eee">{e["email"]}</td>'
        f'<td style="padding:8px 16px;border-bottom:1px solid #eee;color:#888">{e.get("joined","—")[:19].replace("T"," ")}</td></tr>'
        for i, e in enumerate(entries)
    )
    return f'''<!DOCTYPE html><html><head><title>Gain Waitlist</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:60px auto;padding:0 20px">
<h2 style="color:#00DDD4">Gain Waitlist — {len(entries)} signup{"s" if len(entries)!=1 else ""}</h2>
<table style="width:100%;border-collapse:collapse;margin-top:20px">
<thead><tr>
  <th style="text-align:left;padding:8px 16px;background:#f5f5f5">#</th>
  <th style="text-align:left;padding:8px 16px;background:#f5f5f5">Email</th>
  <th style="text-align:left;padding:8px 16px;background:#f5f5f5">Joined</th>
</tr></thead><tbody>{rows or "<tr><td colspan=3 style='padding:20px 16px;color:#888'>No signups yet.</td></tr>"}</tbody>
</table></body></html>'''

@app.route('/api/status')
def status():
    return jsonify({'status': 'ok', 'project': 'gain'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5567))
    app.run(host='0.0.0.0', port=port, debug=False)
