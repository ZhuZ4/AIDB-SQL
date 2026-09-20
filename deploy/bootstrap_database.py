"""Initialize only the new AIDB database; keep credentials out of command lines."""
import json
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.local-services'
DISTRO = 'Ubuntu-22.04'
PORT = '55432'
VECTOR_DATABASE = 'AIDB-vector'
STATE.mkdir(exist_ok=True)
secret_file = STATE / 'db-credentials.json'
if secret_file.exists():
    credentials = json.loads(secret_file.read_text(encoding='utf-8'))
else:
    credentials = {name: secrets.token_urlsafe(32) for name in ('aidb_owner', 'aidb_reader')}
    secret_file.write_text(json.dumps(credentials, indent=2), encoding='utf-8')


def wsl(*args, sql=None):
    result = subprocess.run(['wsl', '-d', DISTRO, '-u', 'root', '--', *args],
                            input=sql, text=True, encoding='utf-8', errors='replace',
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if result.returncode:
        error = result.stderr
        for password in credentials.values():
            error = error.replace(password, '[REDACTED]')
        raise RuntimeError(error)
    return result.stdout.strip()


def psql(sql, database='postgres'):
    return wsl('runuser', '-u', 'postgres', '--', 'psql', '-X', '-q', '-A', '-t',
               '-v', 'ON_ERROR_STOP=1', '-p', PORT, '-d', database, sql=sql)


if __name__ == '__main__':
    wsl('pg_conftool', '16', 'main', 'set', 'port', PORT)
    wsl('systemctl', 'restart', 'postgresql@16-main')
    for username, password in credentials.items():
        if psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{username}';") != '1':
            psql(f"CREATE ROLE {username} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE;")
    if psql(f"SELECT 1 FROM pg_database WHERE datname = '{VECTOR_DATABASE}';") != '1':
        psql(f'CREATE DATABASE "{VECTOR_DATABASE}" OWNER aidb_owner;')
    owner = psql(f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '{VECTOR_DATABASE}';")
    if owner != 'aidb_owner':
        raise RuntimeError('Refusing to configure an existing database owned by another role')
    psql('''
CREATE EXTENSION IF NOT EXISTS vector;
SET ROLE aidb_owner;
CREATE SCHEMA IF NOT EXISTS bird_dev_emb_v2 AUTHORIZATION aidb_owner;
CREATE TABLE IF NOT EXISTS bird_dev_emb_v2.bird_dev_des_emb (
    id BIGSERIAL PRIMARY KEY,
    db_id TEXT NOT NULL,
    record_type TEXT NOT NULL CHECK (record_type IN ('table', 'column', 'value')),
    table_name TEXT,
    column_name TEXT,
    value_text TEXT NOT NULL,
    value_hash TEXT,
    embed_text TEXT,
    freq BIGINT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(1024)
);
CREATE INDEX IF NOT EXISTS bird_dev_des_emb_lookup_idx
    ON bird_dev_emb_v2.bird_dev_des_emb (db_id, record_type, table_name, column_name);
GRANT CONNECT ON DATABASE "AIDB-vector" TO aidb_reader;
GRANT USAGE ON SCHEMA bird_dev_emb_v2 TO aidb_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA bird_dev_emb_v2 TO aidb_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA bird_dev_emb_v2 GRANT SELECT ON TABLES TO aidb_reader;
''', VECTOR_DATABASE)
    benchmark = ROOT.parent / 'minidev' / 'MINIDEV' / 'dev_databases' / 'california_schools'
    settings = {
        'BIRD_DEV_PG_URI': f"postgresql+psycopg2://aidb_reader:{credentials['aidb_reader']}@127.0.0.1:{PORT}/{VECTOR_DATABASE}",
        'BIRD_DEV_ADMIN_URI': f"postgresql+psycopg2://aidb_owner:{credentials['aidb_owner']}@127.0.0.1:{PORT}/{VECTOR_DATABASE}",
        'BIRD_DEV_SCHEMA': 'bird_dev_emb_v2',
        'EMBEDDING_API_URL': 'http://127.0.0.1:8080/v1',
        'EMBEDDING_API_KEY': 'no-key',
        'EMBEDDING_MODEL': 'jina-embeddings-v3-Q8_0.gguf',
        'EMBEDDING_DIM': '1024',
        'DATABASE_URI': 'sqlite:///' + (benchmark / 'california_schools.sqlite').as_posix(),
        'DESCRIPTION_DIR': (benchmark / 'database_description').as_posix(),
    }
    env_path = ROOT / '.env'
    lines = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
    keys = {line.split('=', 1)[0].strip() for line in lines if '=' in line and not line.lstrip().startswith('#')}
    additions = [f'{key}={value}' for key, value in settings.items() if key not in keys]
    if additions:
        with env_path.open('a', encoding='utf-8', newline='\n') as dst:
            dst.write('\n# Local services, provisioned by deploy/bootstrap_database.py\n')
            dst.write('\n'.join(additions) + '\n')
    print(f'PostgreSQL configured on localhost:55432; database={VECTOR_DATABASE}; reader and owner credentials saved locally.')
    print('pgvector=' + psql("SELECT extversion FROM pg_extension WHERE extname='vector';", VECTOR_DATABASE))
    print('Vector table ready. Benchmark vectors have not been populated by this bootstrap.')
