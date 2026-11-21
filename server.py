"""WSGI entrypoint; unlike main.py, importing this never starts mail polling.

Local UI: python server.py (PORT provided by the service runner).
Hosted: apply migrations, then serve with gunicorn. Authentication is mandatory
when WORKSPACE_HOSTED=1 and public registration requires an owner allowlist.
"""
import os
from gmail_auto.web import create_app
from gmail_auto.envfile import load_env

load_env()
app = create_app()

@app.get('/healthz')
def health():
    return {'ok': True}

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','8765')), debug=False, threaded=True)
