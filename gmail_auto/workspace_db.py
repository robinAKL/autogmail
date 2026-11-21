"""Transactional persistence for matters, follow-ups and review decisions.

Existing mailbox credentials remain in the existing provider store. Records belong to the
signed-in SaaS user, or to the installation's owner on a private single-owner install.
Run python -m gmail_auto.workspace_db to apply versioned SQL migrations.
"""
import os
from contextlib import contextmanager
from contextvars import ContextVar
import threading

_CURRENT = ContextVar('workspace_transaction', default=None)
_POOL = None
_POOL_URL = None
_POOL_LOCK = threading.Lock()
from pathlib import Path

from gmail_auto.errors import ConfigError


def available():
    return bool(os.getenv('DATABASE_URL')) and os.getenv('WORKSPACE_DISABLE_DB') != '1'


def scope():
    """Whose records these are. In SaaS mode each signed-in user has their own scope; with no
    signed-in user there is no scope at all, so one user's matters can never reach another."""
    from gmail_auto.tenant import current_user_id, saas_enabled

    if saas_enabled():
        user_id = current_user_id()
        if not user_id:
            raise ConfigError('Sign in to access your workspace.')
        return 'user:' + user_id
    # A private single-owner install.
    return os.getenv('WORKSPACE_OWNER', 'private-workspace').strip().lower()


def connect():
    if not available():
        raise ConfigError('Matters need DATABASE_URL. Set up the database and run the migration first.')
    import psycopg
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10)


def connection_pool():
    global _POOL, _POOL_URL
    if not available():
        raise ConfigError('Matters need DATABASE_URL. Set up the database and run the migration first.')
    with _POOL_LOCK:
        url = os.environ['DATABASE_URL']
        if _POOL is not None and _POOL_URL != url:
            _POOL.close()
            _POOL = None
        if _POOL is None:
            from psycopg_pool import ConnectionPool
            _POOL = ConnectionPool(url, min_size=1, max_size=4, timeout=30, max_idle=300, kwargs={'connect_timeout':20}, check=ConnectionPool.check_connection, open=True)
            _POOL_URL = url
            import atexit
            atexit.register(_POOL.close)
    return _POOL


@contextmanager
def transaction():
    current = _CURRENT.get()
    if current and current[0] == scope():
        yield current[1]
        return
    try:
        with connection_pool().connection() as db:
            # Also serializes webhook / cron / browser changes across instances.
            db.execute("SET LOCAL statement_timeout = '20s'")
            db.execute("SET LOCAL lock_timeout = '10s'")
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('workspace:' + scope(),))
            token = _CURRENT.set((scope(), db))
            try:
                yield db
            finally:
                _CURRENT.reset(token)
    except ConfigError:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('Workspace transaction failed')
        raise ConfigError('Matters could not be saved right now. Please try again later.') from exc


def all_records(db, kind):
    rows = db.execute('SELECT id,version,data FROM workspace_records WHERE scope=%s AND kind=%s ORDER BY updated_at DESC', (scope(), kind)).fetchall()
    return [{**r[2], 'id': r[0], 'version': r[1]} for r in rows]


def get(db, kind, ident):
    row = db.execute('SELECT version,data FROM workspace_records WHERE scope=%s AND kind=%s AND id=%s', (scope(), kind, str(ident))).fetchone()
    return {**row[1], 'id': str(ident), 'version': row[0]} if row else None


def put(db, kind, record):
    from psycopg.types.json import Jsonb
    data = {k:v for k,v in record.items() if k != 'version'}
    row = db.execute('INSERT INTO workspace_records(scope,kind,id,data) VALUES (%s,%s,%s,%s) ON CONFLICT(scope,kind,id) DO UPDATE SET data=EXCLUDED.data,version=workspace_records.version+1,updated_at=now() RETURNING version', (scope(), kind, str(record['id']), Jsonb(data))).fetchone()
    return {**data, 'version': row[0]}


def migrate():
    with connect() as db:
        for path in sorted((Path(__file__).resolve().parent.parent / 'migrations').glob('*.sql')):
            db.execute(path.read_text(encoding='utf-8'))
    print('Workspace migrations applied.')


if __name__ == '__main__':
    migrate()
