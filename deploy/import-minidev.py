"""Provision the two local databases and import the official PostgreSQL dump.

The source dump and the old bird_dev database are never modified. All dump SQL
runs as aidb_owner in one transaction; an existing populated target is refused.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

from dotenv import set_key
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL

from bootstrap_database import ROOT, STATE, credentials, psql, wsl

VECTOR_DB = 'AIDB-vector'
DATA_DB = 'BIRD_minidev'
BENCHMARK = ROOT.parent / 'minidev' / 'MINIDEV'
DUMP = ROOT.parent / 'minidev' / 'MINIDEV_postgresql' / 'BIRD_dev.sql'
IMPORT_DIR = STATE / 'import'


def quote_ident(value):
    return '"' + value.replace('"', '""') + '"'


def uri(database, role='aidb_owner'):
    return URL.create('postgresql+psycopg2', username=role,
                      password=credentials[role], host='127.0.0.1',
                      port=55432, database=database).render_as_string(hide_password=False)


def provision():
    wsl('systemctl', 'start', 'postgresql@16-main')
    for database in (VECTOR_DB, DATA_DB):
        owner = psql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '" + database + "';")
        if owner and owner != 'aidb_owner':
            raise RuntimeError(f'{database} already exists with a different owner')
        if not owner:
            psql(f'CREATE DATABASE {quote_ident(database)} OWNER aidb_owner;')
        psql(f'GRANT CONNECT ON DATABASE {quote_ident(database)} TO aidb_reader;')
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
CREATE UNIQUE INDEX IF NOT EXISTS bird_dev_des_emb_record_key_idx
    ON bird_dev_emb_v2.bird_dev_des_emb (db_id, record_type, table_name,
                                        COALESCE(column_name, ''), value_hash);
GRANT USAGE ON SCHEMA bird_dev_emb_v2 TO aidb_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA bird_dev_emb_v2 TO aidb_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA bird_dev_emb_v2 GRANT SELECT ON TABLES TO aidb_reader;
''', VECTOR_DB)
    print('Databases and vector table ready.', flush=True)


def prepare_dump():
    """Only rewrite owner directives, outside COPY data; audit psql commands."""
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = IMPORT_DIR / 'BIRD_minidev.owner.sql'
    digest = hashlib.sha256()
    copy_table = None
    row_counts = {}
    owners_rewritten = 0
    command_counts = {}
    with DUMP.open('rb') as src, target.open('wb') as dst:
        for line in src:
            digest.update(line)
            if copy_table is not None:
                if line.rstrip(b'\r\n') == b'\\.':
                    copy_table = None
                else:
                    row_counts[copy_table] += 1
            else:
                stripped = line.strip()
                if stripped.startswith(b'COPY '):
                    match = re.fullmatch(rb'COPY public\.("[^"]+"|\w+) \(.+\) FROM stdin;', stripped)
                    if not match:
                        raise ValueError('Unexpected COPY command')
                    copy_table = match[1].decode('utf-8').strip('"')
                    row_counts[copy_table] = 0
                elif stripped.startswith((b'\\', b'DROP ', b'CREATE FUNCTION', b'CREATE PROCEDURE',
                                          b'CREATE TRIGGER', b'CREATE EXTENSION', b'GRANT ', b'REVOKE ')):
                    raise ValueError('Unexpected executable directive in the dump')
                if stripped and not stripped.startswith(b'--') and not line.startswith(b' '):
                    command = stripped.split(b' ', 1)[0].decode('utf-8')
                    command_counts[command] = command_counts.get(command, 0) + 1
                if re.fullmatch(rb'ALTER (TABLE|SEQUENCE) .+ OWNER TO xiaolongli;', stripped):
                    line = line.replace(b' OWNER TO xiaolongli;', b' OWNER TO aidb_owner;')
                    owners_rewritten += 1
            dst.write(line)
    if copy_table is not None or len(row_counts) != 75:
        raise ValueError('Incomplete or unexpected dump')
    manifest = {'source': str(DUMP), 'source_sha256': digest.hexdigest(),
                'source_bytes': DUMP.stat().st_size, 'table_rows': row_counts,
                'table_count': len(row_counts), 'row_count': sum(row_counts.values()),
                'owners_rewritten': owners_rewritten, 'commands': command_counts}
    (IMPORT_DIR / 'import-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f"Prepared {manifest['table_count']} tables, {manifest['row_count']} rows.", flush=True)
    return target, manifest


def import_dump(target):
    count = psql("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                 "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
                 "AND c.relkind IN ('r','v','m','p');", DATA_DB)
    if count != '0':
        raise RuntimeError('BIRD_minidev is populated; refusing to replay the dump. Use --finalize-only after a successful import.')
    linux_path = '/mnt/' + target.drive[0].lower() + target.as_posix()[2:]
    log = IMPORT_DIR / 'restore.log'
    with log.open('w', encoding='utf-8') as output:
        result = subprocess.run(['wsl', '-d', 'Ubuntu-22.04', '-u', 'root', '--',
                                 'runuser', '-u', 'postgres', '--', 'psql', '-X', '-q',
                                 '-p', '55432', '-d', DATA_DB, '-v', 'ON_ERROR_STOP=1',
                                 '--single-transaction', '-c', 'SET ROLE aidb_owner;',
                                 '-f', linux_path], stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'Import rolled back. Inspect {log}')
    print('Dump transaction committed.', flush=True)


def finalize(manifest):
    schemas = json.loads((BENCHMARK / 'dev_tables.json').read_text(encoding='utf-8'))
    engine = create_engine(uri(DATA_DB), connect_args={'connect_timeout': 10})
    before = inspect(engine)
    public_tables = set(before.get_table_names(schema='public'))
    imported_tables = {table for db in schemas for table in before.get_table_names(schema=db['db_id'])}
    expected = set(manifest['table_rows'])
    if public_tables | imported_tables != expected:
        raise RuntimeError('Imported tables do not match the audited dump')
    with engine.begin() as conn:
        for db in schemas:
            schema = quote_ident(db['db_id'])
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION aidb_owner'))
            for original in db['table_names_original']:
                table = original.lower()
                if table not in expected:
                    raise RuntimeError(f'Missing PostgreSQL table for {original}')
                quoted = quote_ident(table)
                if table in public_tables:
                    conn.execute(text(f'ALTER TABLE public.{quoted} SET SCHEMA {schema}'))
                # Preserve the original dump's unqualified/public query interface.
                conn.execute(text(f'CREATE OR REPLACE VIEW public.{quoted} AS SELECT * FROM {schema}.{quoted}'))
            conn.execute(text(f'GRANT USAGE ON SCHEMA {schema} TO aidb_reader'))
            conn.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO aidb_reader'))
            conn.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO aidb_reader'))
        conn.execute(text('GRANT USAGE ON SCHEMA public TO aidb_reader'))
        conn.execute(text('GRANT SELECT ON ALL TABLES IN SCHEMA public TO aidb_reader'))
    inspector = inspect(engine)
    report = {'database': DATA_DB, 'vector_database': VECTOR_DB,
              'source_sha256': manifest['source_sha256'], 'schemas': {}, 'total_rows': 0,
              'total_columns': 0, 'total_tables': 0,
              'checked_at': datetime.now(timezone.utc).isoformat()}
    with engine.connect() as conn:
        for db in schemas:
            db_id = db['db_id']
            tables = {}
            for original in db['table_names_original']:
                table = original.lower()
                qtable = f'{quote_ident(db_id)}.{quote_ident(table)}'
                count = conn.execute(text(f'SELECT count(*) FROM {qtable}')).scalar_one()
                if count != manifest['table_rows'][table]:
                    raise RuntimeError(f'Row count mismatch for {db_id}.{table}')
                columns = inspector.get_columns(table, schema=db_id)
                tables[table] = {'rows': count, 'columns': len(columns)}
                report['total_rows'] += count
                report['total_columns'] += len(columns)
                report['total_tables'] += 1
            report['schemas'][db_id] = tables
    # Publish connection settings only after all tables have passed verification.
    settings = {'BIRD_DEV_PG_URI': uri(VECTOR_DB, 'aidb_reader'),
                'BIRD_DEV_ADMIN_URI': uri(VECTOR_DB),
                'BIRD_DEV_SCHEMA': 'bird_dev_emb_v2',
                'DATABASE_URI': uri(DATA_DB, 'aidb_reader'),
                'BIRD_MINIDEV_ADMIN_URI': uri(DATA_DB),
                'BIRD_DEV_DB_ID': '', 'DESCRIPTION_DIR': '',
                'MINIDEV_ROOT': BENCHMARK.as_posix()}
    for key, value in settings.items():
        set_key(str(ROOT / '.env'), key, value, quote_mode='always')
    (STATE / 'minidev-import-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'schemas'}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--finalize-only', action='store_true')
    args = parser.parse_args()
    provision()
    if args.finalize_only:
        manifest = json.loads((IMPORT_DIR / 'import-manifest.json').read_text(encoding='utf-8'))
    else:
        target, manifest = prepare_dump()
        import_dump(target)
    finalize(manifest)


if __name__ == '__main__':
    main()
