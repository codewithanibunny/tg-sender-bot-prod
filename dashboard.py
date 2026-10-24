from flask import Flask, render_template, jsonify
import json
from pathlib import Path

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
SESSIONS_DIR = Path('sessions')
LOGS_FILE = Path('dashboard_logs.jsonl')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/users')
def get_users():
    users = []
    if SESSIONS_DIR.exists():
        for f in SESSIONS_DIR.glob('user_*.json'):
            try:
                cfg = json.loads(f.read_text(encoding='utf-8'))
                users.append({
                    'user_id': cfg.get('user_id', f.stem.replace('user_', '')),
                    'is_polling_active': cfg.get('is_polling_active', False),
                    'firebase_url': cfg.get('firebase_url', ''),
                    'selected_device_id': cfg.get('selected_device_id', 'Not Set'),
                    'stats_real_sms': cfg.get('stats_real_sms', 0)
                })
            except Exception:
                pass
    return jsonify(users)

GLOBAL_FB_PATH = SESSIONS_DIR / 'global_firebases.json'

@app.route('/api/firebases')
def get_firebases():
    firebases = []
    if GLOBAL_FB_PATH.exists():
        try:
            with open(GLOBAL_FB_PATH, 'r', encoding='utf-8') as f:
                firebases = json.load(f)
        except Exception:
            pass
    return jsonify(firebases)

@app.route('/api/logs')
def get_logs():
    logs = []
    if LOGS_FILE.exists():
        try:
            with open(LOGS_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                # Get the last 1000 logs reversed (newest first)
                for line in reversed(lines[-1000:]):
                    if line.strip():
                        logs.append(json.loads(line))
        except Exception as e:
            print("Error reading logs:", e)
    return jsonify(logs)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
