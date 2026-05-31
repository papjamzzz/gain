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

@app.route('/api/status')
def status():
    return jsonify({'status': 'ok', 'project': 'gain'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5567))
    app.run(host='0.0.0.0', port=port, debug=False)
