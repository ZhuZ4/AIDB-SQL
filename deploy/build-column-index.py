"""Build and verify a versioned, full SQLite column-name/description index.

Business SQLite files are always opened read-only. Writes are restricted to
new metadata tables in AIDB-vector and local index artifacts. Existing index
tables are never replaced. Use --activate to pin .env to the verified version.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import sys

from dotenv import load_dotenv, set_key
from openai import OpenAI
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.local-services' / 'column-index'
POLICY = {
    'layout': 'column_dual_v1', 'revision': 1,
    'name_input': 'exact SQLite column name; no concatenation',
    'description_input': 'JSON exact identity, strip leading # and whitespace; CSV fallback',
    'csv_fallback': ['column_description', 'column_name', 'value_description'],
    'missing_description': 'NULL description and NULL description vector; retain name vector',
    'samples': 'up to 3 distinct strings from first 5 non-null rows, truncated to 500 chars',
    'fusion': 'independent name/description cosine ranks; existing RRF k=60',
    'bm25_enabled': False,
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def file_hash(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(canonical(value) + '\n', encoding='utf-8')
    temporary.replace(path)


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def read_csv(path):
    if not path.exists():
        return {}, None
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp1252', 'latin1'):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    rows = {}
    for row in csv.DictReader(io.StringIO(decoded)):
        key = row['original_column_name'].strip().casefold()
        clean = {k: (v or '').strip() for k, v in row.items() if k}
        if key in rows and rows[key] != clean:
            raise ValueError(f'Ambiguous CSV column identity: {path.name}: {key}')
        rows[key] = clean
    return rows, hashlib.sha256(raw).hexdigest()


def prepare(benchmark):
    """Enumerate schema directly; never load questions or gold SQL."""
    meanings_path = ROOT / 'column_meaning.json'
    meanings = json.loads(meanings_path.read_text(encoding='utf-8'))
    sources = {'column_meaning.json': file_hash(meanings_path)}
    records = []
    for path in sorted((benchmark / 'dev_databases').glob('*/*.sqlite')):
        db_id = path.parent.name
        if path.stem != db_id:
            raise ValueError(f'Unexpected SQLite filename for dataset {db_id}')
        sources[path.relative_to(benchmark).as_posix()] = file_hash(path)
        # as_uri escapes spaces, # and ? in paths; mode=ro is enforced by SQLite.
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True) as conn:
            conn.execute('PRAGMA query_only = ON')
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for table in tables:
                csv_path = path.parent / 'database_description' / (table + '.csv')
                csv_records, csv_hash = read_csv(csv_path)
                sources[csv_path.relative_to(benchmark).as_posix()] = csv_hash
                foreign_keys = [dict(zip(('id', 'seq', 'table', 'from', 'to', 'on_update', 'on_delete', 'match'), row))
                                for row in conn.execute('PRAGMA foreign_key_list(' + quote(table) + ')')]
                for info in conn.execute('PRAGMA table_info(' + quote(table) + ')').fetchall():
                    column = info[1]
                    key = f'{db_id}|{table}|{column}'
                    raw_meaning = meanings.get(key)
                    description = (raw_meaning or '').lstrip('#').strip() or None
                    description_source = 'column_meaning.json'
                    csv_row = csv_records.get(column.strip().casefold(), {})
                    if description is None:
                        parts = []
                        for field in POLICY['csv_fallback']:
                            part = csv_row.get(field, '')
                            if part and part != column and part not in parts:
                                parts.append(part)
                        description = '; '.join(parts) or None
                        description_source = 'database_description_csv' if description else 'missing_in_source_csv'
                    values = conn.execute(f'SELECT {quote(column)} FROM {quote(table)} '
                                          f'WHERE {quote(column)} IS NOT NULL LIMIT 5').fetchall()
                    samples = list(dict.fromkeys(str(row[0]) for row in values))[:3]
                    records.append({
                        'id': len(records) + 1, 'db_id': db_id, 'table_name': table, 'column_name': column,
                        'description': description, 'description_source': description_source,
                        'metadata': {
                            'source_kind': 'sqlite', 'source_key': key,
                            'sqlite_path': path.relative_to(benchmark).as_posix(),
                            'csv_path': csv_path.relative_to(benchmark).as_posix(), 'csv_row': csv_row,
                            'type': info[2], 'pk': bool(info[5]), 'primary_key_position': info[5],
                            'declared_not_null': bool(info[3]), 'default': info[4],
                            'foreign_keys': [fk for fk in foreign_keys if fk['from'] == column],
                            'samples': [{'value': v[:500], 'truncated': len(v) > 500} for v in samples],
                            'raw_meaning': raw_meaning, 'description_available': description is not None,
                            'name_input_sha256': hashlib.sha256(column.encode('utf-8')).hexdigest(),
                            'description_input_sha256': hashlib.sha256(description.encode('utf-8')).hexdigest() if description else None,
                        },
                    })
        print(f'Prepared {db_id}: {sum(r["db_id"] == db_id for r in records)} columns', flush=True)
    counts = {
        'databases': len({r['db_id'] for r in records}),
        'tables': len({(r['db_id'], r['table_name']) for r in records}),
        'columns': len(records), 'name_vectors': len(records),
        'description_vectors': sum(r['description'] is not None for r in records),
        'missing_descriptions': sum(r['description'] is None for r in records),
    }
    if (counts['databases'], counts['tables'], counts['columns']) != (11, 75, 798):
        raise ValueError(f'Unexpected mini-dev schema: {counts}')
    if len({r['metadata']['source_key'] for r in records}) != len(records):
        raise ValueError('Duplicate column identity')
    return records, sources, counts


def model_provenance(model_file):
    if not model_file.is_file():
        raise FileNotFoundError('Local embedding model file is required for reproducibility')
    runtime = ROOT / '.local-services' / 'llama'
    binaries = {p.name: file_hash(p) for p in sorted(runtime.glob('*'))
                if p.is_file() and p.suffix.lower() in ('.exe', '.dll')}
    return {
        'model': os.environ['EMBEDDING_MODEL'], 'dimensions': int(os.environ['EMBEDDING_DIM']),
        'model_file': model_file.name, 'model_sha256': file_hash(model_file),
        'runtime_files_sha256': binaries,
        'service_start_script_sha256': file_hash(ROOT / 'deploy/start-services.ps1'),
        'packages': {name: importlib.metadata.version(name) for name in ('openai', 'SQLAlchemy', 'psycopg2-binary')},
    }


def normalized_vector(vector, dimensions):
    if len(vector) != dimensions or not all(math.isfinite(n) for n in vector):
        raise ValueError('Embedding has an invalid dimension or non-finite value')
    result = [struct.unpack('<f', struct.pack('<f', n))[0] for n in vector]
    if abs(sum(n * n for n in result) - 1) > 0.01:
        raise ValueError('Expected normalized embedding')
    return result


def generate_vectors(records, provenance, output, batch_size):
    client = OpenAI(base_url=os.environ['EMBEDDING_API_URL'], api_key=os.environ['EMBEDDING_API_KEY'],
                    timeout=120, max_retries=3)
    available = [m.id for m in client.models.list().data]
    if provenance['model'] not in available:
        raise ValueError('Embedding endpoint does not advertise the configured model')
    inputs = []
    for record in records:
        for field, input_text in [('name_embedding', record['column_name']),
                                  ('description_embedding', record['description'])]:
            if input_text is not None:
                inputs.append({'source_key': record['metadata']['source_key'], 'vector_field': field,
                               'input': input_text, 'input_sha256': hashlib.sha256(input_text.encode('utf-8')).hexdigest()})
    write_json(output / 'embedding-inputs.json', inputs)
    texts = dict.fromkeys(item['input'] for item in inputs)
    cache_dir = OUTPUT / 'embedding-cache' / digest(provenance)
    cache_dir.mkdir(parents=True, exist_ok=True)
    vectors = {}
    for input_text in texts:
        path = cache_dir / (hashlib.sha256(input_text.encode('utf-8')).hexdigest() + '.json')
        if path.exists():
            cached = json.loads(path.read_text(encoding='utf-8'))
            if cached['input'] != input_text or cached['provenance_sha256'] != digest(provenance):
                raise ValueError('Embedding cache identity mismatch')
            vectors[input_text] = normalized_vector(cached['vector'], provenance['dimensions'])
    pending = [value for value in texts if value not in vectors]
    print(f'Embedding {len(inputs)} independent inputs ({len(texts)} unique; {len(vectors)} cached)', flush=True)
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        response = client.embeddings.create(model=provenance['model'], input=batch, dimensions=provenance['dimensions'])
        items = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(len(batch))):
            raise ValueError('Embedding response indices do not match the batch')
        for input_text, item in zip(batch, items):
            vector = normalized_vector(item.embedding, provenance['dimensions'])
            vectors[input_text] = vector
            path = cache_dir / (hashlib.sha256(input_text.encode('utf-8')).hexdigest() + '.json')
            write_json(path, {'input': input_text, 'vector': vector, 'provenance_sha256': digest(provenance)})
        if start == 0 or (start + batch_size) % 80 == 0 or start + batch_size >= len(pending):
            print(f'Embedded {min(start + batch_size, len(pending))}/{len(pending)} uncached inputs', flush=True)
    for record in records:
        record['embedding_model'] = provenance['model']
        record['name_embedding'] = vectors[record['column_name']]
        record['description_embedding'] = vectors.get(record['description'])
    return records


def persist(records, manifest):
    schema, table = manifest['schema'], manifest['table']
    qualified = quote(schema) + '.' + quote(table)
    dimensions = manifest['embedding']['dimensions']
    engine = create_engine(os.environ['BIRD_DEV_ADMIN_URI'], connect_args={'connect_timeout': 10})
    with engine.begin() as conn:
        if conn.execute(text('SELECT current_database()')).scalar_one() != 'AIDB-vector':
            raise RuntimeError('Metadata writes are restricted to AIDB-vector')
        conn.execute(text("SET LOCAL lock_timeout = '15s'"))
        conn.execute(text('SELECT pg_advisory_xact_lock(:key)'),
                     {'key': int(manifest['content_sha256'][:15], 16)})
        existing = conn.execute(text('SELECT to_regclass(:table)'), {'table': qualified}).scalar_one()
        if existing:
            print('Existing version table retained; verifying exact contents.', flush=True)
            return
        conn.execute(text(f'''CREATE TABLE {qualified} (
            id BIGINT PRIMARY KEY, db_id TEXT NOT NULL, table_name TEXT NOT NULL,
            column_name TEXT NOT NULL, description TEXT, description_source TEXT NOT NULL,
            name_embedding vector({dimensions}) NOT NULL, description_embedding vector({dimensions}),
            embedding_model TEXT NOT NULL, metadata JSONB NOT NULL,
            UNIQUE (db_id, table_name, column_name),
            CHECK ((description IS NULL AND description_embedding IS NULL) OR
                   (description IS NOT NULL AND btrim(description) <> '' AND description_embedding IS NOT NULL))
        )'''))
        statement = text(f'''INSERT INTO {qualified}
            (id, db_id, table_name, column_name, description, description_source,
             name_embedding, description_embedding, embedding_model, metadata)
            VALUES (:id, :db_id, :table_name, :column_name, :description, :description_source,
                    CAST(:name_embedding AS vector), CAST(:description_embedding AS vector),
                    :embedding_model, CAST(:metadata AS jsonb))''')
        for start in range(0, len(records), 40):
            payload = []
            for record in records[start:start + 40]:
                item = dict(record)
                item['metadata'] = canonical(item['metadata'])
                for field in ('name_embedding', 'description_embedding'):
                    item[field] = canonical(item[field]) if item[field] is not None else None
                payload.append(item)
            conn.execute(statement, payload)
        for field, suffix in [('name_embedding', 'name_hnsw'), ('description_embedding', 'desc_hnsw')]:
            where = ' WHERE description_embedding IS NOT NULL' if field == 'description_embedding' else ''
            conn.execute(text(f'CREATE INDEX {quote(table + "_" + suffix)} ON {qualified} '
                              f'USING hnsw ({field} vector_cosine_ops)' + where))
        conn.execute(text(f'CREATE INDEX {quote(table + "_db")} ON {qualified} (db_id)'))
        conn.execute(text(f'GRANT SELECT ON {qualified} TO aidb_reader'))
        conn.execute(text(f'ANALYZE {qualified}'))
    engine.dispose()


def vector_hash(records):
    """Hash float32 bytes, avoiding differences in PostgreSQL decimal rendering."""
    result = hashlib.sha256()
    for record in records:
        result.update(record['metadata']['source_key'].encode('utf-8'))
        for field in ('name_embedding', 'description_embedding'):
            vector = record[field]
            result.update(field.encode('ascii'))
            if vector is None:
                result.update(b'NULL')
            else:
                result.update(struct.pack('<' + 'f' * len(vector), *vector))
    return result.hexdigest()


def verify(records, manifest):
    sys.path.insert(0, str(ROOT))
    from tools.bird_dev_retriever import RetrievalResult, _retrieve_dual_columns
    qualified = quote(manifest['schema']) + '.' + quote(manifest['table'])
    engine = create_engine(os.environ['BIRD_DEV_PG_URI'], connect_args={'connect_timeout': 10})
    with engine.connect() as conn:
        if conn.execute(text('SELECT current_database()')).scalar_one() != 'AIDB-vector':
            raise ValueError('Reader must connect to AIDB-vector')
        if not conn.execute(text("SELECT has_table_privilege(current_user, :t, 'SELECT')"), {'t': qualified}).scalar_one():
            raise ValueError('Reader cannot read the index')
        if conn.execute(text("SELECT has_table_privilege(current_user, :t, 'INSERT,UPDATE,DELETE,TRUNCATE')"), {'t': qualified}).scalar_one():
            raise ValueError('Index reader unexpectedly has write access')
        rows = [dict(row) for row in conn.execute(text(f'''SELECT id, db_id, table_name, column_name,
            description, description_source, embedding_model, metadata,
            name_embedding::text AS name_embedding, description_embedding::text AS description_embedding
            FROM {qualified} ORDER BY id''')).mappings()]
        indexes = conn.execute(text('SELECT indexdef FROM pg_indexes WHERE schemaname=:s AND tablename=:t'),
                               {'s': manifest['schema'], 't': manifest['table']}).scalars().all()
    if len(rows) != len(records) or sum('USING hnsw' in i for i in indexes) != 2:
        raise ValueError('Row count or independent HNSW indexes do not match')
    dimensions = manifest['embedding']['dimensions']
    for expected, row in zip(records, rows):
        for key in ('id', 'db_id', 'table_name', 'column_name', 'description', 'description_source', 'embedding_model', 'metadata'):
            if row[key] != expected[key]:
                raise ValueError(f'Index record mismatch: {key}: {expected["metadata"]["source_key"]}')
        for field in ('name_embedding', 'description_embedding'):
            row[field] = json.loads(row[field]) if row[field] is not None else None
            if (row[field] is None) != (expected[field] is None):
                raise ValueError('Missing description/vector mismatch')
            if row[field] is not None:
                row[field] = normalized_vector(row[field], dimensions)
    if vector_hash(rows) != manifest['vectors_sha256']:
        raise ValueError('PostgreSQL vectors differ from the frozen snapshot')
    settings = {'BIRD_DEV_SCHEMA': manifest['schema'], 'BIRD_DEV_COLUMN_TABLE': manifest['table'],
                'BIRD_DEV_INDEX_VERSION': manifest['index_version']}
    previous = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    isolation = {}
    try:
        for db_id in sorted({r['db_id'] for r in rows}):
            group = [r for r in rows if r['db_id'] == db_id]
            probe = next((r for r in group if r['description'] is None), group[0])
            result = _retrieve_dual_columns(engine, probe['name_embedding'], RetrievalResult(db_id=db_id), 75, 798)
            keys = {r.metadata['source_key'] for r in result.columns}
            expected_keys = {r['metadata']['source_key'] for r in group}
            if keys != expected_keys or any(not k.startswith(db_id + '|') for k in keys):
                raise ValueError(f'Incomplete or cross-database retrieval: {db_id}')
            if probe['description'] is None:
                hit = next(r for r in result.columns if r.id == probe['id'])
                if set(hit.metadata['retrieval_channels']) != {'name'}:
                    raise ValueError('Missing description must use only the name channel')
            isolation[db_id] = len(keys)
        for invalid in ('', '__unknown_database__'):
            result = _retrieve_dual_columns(engine, rows[0]['name_embedding'], RetrievalResult(db_id=invalid), 5, 15)
            if result.tables or result.columns:
                raise ValueError('Unscoped database query returned records')
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        engine.dispose()
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'counts': manifest['counts'],
            'index_version': manifest['index_version'], 'content_sha256': manifest['content_sha256'],
            'vectors_sha256': manifest['vectors_sha256'], 'database_isolation': isolation,
            'sqlite_column_identities': 'passed', 'snapshot_vector_round_trip': 'passed',
            'two_independent_hnsw_indexes': 'passed', 'reader_is_read_only': 'passed',
            'empty_and_unknown_db_isolation': 'passed', 'missing_description_name_channel': 'passed',
            'bm25_enabled': False}


def validate_snapshot(records, manifest):
    inputs = []
    for record in records:
        item = {key: value for key, value in record.items()
                if key not in ('name_embedding', 'description_embedding', 'embedding_model')}
        item['metadata'] = {key: value for key, value in record['metadata'].items() if key != 'index_version'}
        inputs.append(item)
    content_hash = digest({'records': inputs, 'sources': manifest['source_files_sha256'],
                           'policy': manifest['policy'], 'embedding': manifest['embedding']})
    if content_hash != manifest['content_sha256'] or manifest['index_version'] != 'column_dual_v1_' + content_hash[:20]:
        raise ValueError('Local frozen content snapshot hash mismatch')
    if vector_hash(records) != manifest['vectors_sha256']:
        raise ValueError('Local frozen vector snapshot hash mismatch')
    benchmark = Path(os.environ['MINIDEV_ROOT'])
    for relative, expected in manifest['source_files_sha256'].items():
        path = ROOT / relative if relative == 'column_meaning.json' else benchmark / relative
        actual = file_hash(path) if path.exists() else None
        if actual != expected:
            raise ValueError(f'Source file changed since the frozen index build: {relative}')


def activate(manifest):
    for key, value in {'BIRD_DEV_INDEX_LAYOUT': 'column_dual_v1', 'BIRD_DEV_SCHEMA': manifest['schema'],
                       'BIRD_DEV_COLUMN_TABLE': manifest['table'], 'BIRD_DEV_INDEX_VERSION': manifest['index_version']}.items():
        set_key(str(ROOT / '.env'), key, value, quote_mode='always')
    write_json(OUTPUT / 'active.json', {'index_version': manifest['index_version'], 'schema': manifest['schema'],
                                      'table': manifest['table'], 'content_sha256': manifest['content_sha256'],
                                      'vectors_sha256': manifest['vectors_sha256'],
                                      'manifest': str(OUTPUT / manifest['index_version'] / 'manifest.json')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate', action='store_true', help='Pin .env after successful verification')
    parser.add_argument('--prepare-only', action='store_true', help='Read schema and save reproducible inputs, without embedding or PG writes')
    parser.add_argument('--verify-only', metavar='VERSION', help='Verify an existing frozen snapshot and version table')
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--model-file', type=Path, default=ROOT / '.local-services/models/jina-embeddings-v3-Q8_0.gguf')
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 64:
        parser.error('--batch-size must be between 1 and 64')
    if args.prepare_only and (args.activate or args.verify_only):
        parser.error('--prepare-only cannot be combined with --activate or --verify-only')
    load_dotenv(ROOT / '.env')
    if args.verify_only:
        if not args.verify_only.startswith('column_dual_v1_') or not all(c.isalnum() or c == '_' for c in args.verify_only):
            parser.error('Invalid index version')
        output = OUTPUT / args.verify_only
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        records = json.loads((output / 'records-full.json').read_text(encoding='utf-8'))
        validate_snapshot(records, manifest)
    else:
        benchmark = Path(os.environ['MINIDEV_ROOT'])
        records, sources, counts = prepare(benchmark)
        provenance = model_provenance(args.model_file)
        content_hash = digest({'records': records, 'sources': sources, 'policy': POLICY, 'embedding': provenance})
        version = 'column_dual_v1_' + content_hash[:20]
        output = OUTPUT / version
        manifest = {
            'index_version': version, 'schema': os.environ.get('BIRD_DEV_SCHEMA', 'bird_dev_emb_v2'),
            'table': 'columns_' + content_hash[:20], 'content_sha256': content_hash,
            'counts': counts, 'counts_by_database': dict(sorted(Counter(r['db_id'] for r in records).items())),
            'source_files_sha256': sources, 'embedding': provenance, 'policy': POLICY,
            'builder_sha256': file_hash(Path(__file__)), 'created_at': datetime.now(timezone.utc).isoformat(),
        }
        write_json(output / 'records-inputs.json', records)
        if args.prepare_only:
            write_json(output / 'prepared-manifest.json', manifest)
            print(json.dumps({'index_version': version, 'counts': counts, 'output': str(output)}, indent=2))
            return
        records = generate_vectors(records, provenance, output, args.batch_size)
        for record in records:
            record['metadata']['index_version'] = version
        manifest['vectors_sha256'] = vector_hash(records)
        if (output / 'manifest.json').exists():
            previous = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
            if (previous['content_sha256'], previous['vectors_sha256']) != (content_hash, manifest['vectors_sha256']):
                raise ValueError('Refusing to replace an existing version with different content or vectors')
            manifest = previous
        # Preserve the snapshot before the database transaction for restart recovery.
        write_json(output / 'records-full.json', records)
        write_json(output / 'manifest.json', manifest)
        persist(records, manifest)
    report = verify(records, manifest)
    write_json(output / 'verification.json', report)
    if args.activate:
        activate(manifest)
    print(json.dumps({'status': 'verified', 'activated': args.activate, 'output': str(output), **report}, indent=2), flush=True)


if __name__ == '__main__':
    main()
