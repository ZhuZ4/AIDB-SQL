"""Build all table/column vectors from SHARE, CSVs, and actual PostgreSQL metadata.

This indexes schema metadata, not every distinct cell value. Missing source
descriptions stay explicitly missing; type/keys/samples come from PostgreSQL.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.local-services' / 'minidev'
VECTOR_TABLE = 'bird_dev_emb_v2.bird_dev_des_emb'


def q(value):
    return '"' + value.replace('"', '""') + '"'


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def read_csv(path):
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp1252', 'latin1'):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    rows = list(csv.DictReader(io.StringIO(decoded)))
    return {row['original_column_name'].strip().casefold():
            {k: (v or '').strip() for k, v in row.items() if k}
            for row in rows}, encoding


def prepare_records():
    benchmark = Path(os.environ['MINIDEV_ROOT'])
    source_path = ROOT / 'column_meaning.json'
    if not source_path.exists():
        source_path = ROOT / '.local-services/research/RV-SHARE/data/bird/dev/column_meaning.json'
    meanings = json.loads(source_path.read_text(encoding='utf-8'))
    schemas = json.loads((benchmark / 'dev_tables.json').read_text(encoding='utf-8'))
    imported = json.loads((ROOT / '.local-services/minidev-import-verification.json').read_text(encoding='utf-8'))
    engine = create_engine(os.environ['BIRD_MINIDEV_ADMIN_URI'], connect_args={'connect_timeout': 10})
    inspector = inspect(engine)
    merged, column_metadata, missing, records = {}, {}, [], []
    csv_filled = 0
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = '60s'"))
        for db in schemas:
            db_id = db['db_id']
            for ti, original_table in enumerate(db['table_names_original']):
                table = original_table.lower()
                qualified = f'{q(db_id)}.{q(table)}'
                pg_columns = inspector.get_columns(table, schema=db_id)
                pg_by_name = {c['name'].casefold(): c for c in pg_columns}
                pk = set(inspector.get_pk_constraint(table, schema=db_id).get('constrained_columns', []))
                fks = inspector.get_foreign_keys(table, schema=db_id)
                csv_path = benchmark / 'dev_databases' / db_id / 'database_description' / (original_table + '.csv')
                csv_rows, encoding = read_csv(csv_path)
                table_columns = []
                for idx, original_column in db['column_names_original']:
                    if idx != ti:
                        continue
                    pg = pg_by_name[original_column.casefold()]
                    column = pg['name']
                    source_key = f'{db_id}|{original_table}|{original_column}'
                    pg_key = f'{db_id}|{table}|{column}'
                    csv_row = csv_rows.get(original_column.strip().casefold(), {})
                    meaning = (meanings.get(source_key) or '').lstrip('#').strip()
                    source = 'column_meaning.json'
                    if not meaning:
                        parts = []
                        for field in ('column_description', 'column_name', 'value_description'):
                            value = csv_row.get(field, '').strip()
                            if value and value not in parts and value != original_column:
                                parts.append(value)
                        meaning = '; '.join(parts)
                        source = 'database_description_csv' if meaning else 'missing_in_source_csv'
                        csv_filled += bool(meaning)
                    merged[source_key] = meaning
                    samples = conn.execute(text(
                        f'SELECT CAST({q(column)} AS text) FROM {qualified} '
                        f'WHERE {q(column)} IS NOT NULL LIMIT 5'
                    )).scalars().all()
                    samples = [{'value': v[:500], 'truncated': len(v) > 500}
                               for v in dict.fromkeys(samples)][:3]
                    metadata = {
                        'type': str(pg['type']), 'pk': column in pk,
                        'nullable': pg['nullable'], 'samples': samples,
                        'sample_policy': 'up to 3 distinct values from first 5 non-null rows; not exhaustive',
                        'foreign_keys': [fk for fk in fks if column in fk['constrained_columns']],
                        'description': meaning, 'description_available': bool(meaning),
                        'description_source': source, 'source_key': source_key,
                        'original_table_name': original_table, 'original_column_name': original_column,
                        'csv_path': str(csv_path), 'csv_encoding': encoding, 'csv_row': csv_row,
                    }
                    column_metadata[pg_key] = metadata
                    if not meaning:
                        missing.append({'source_key': source_key, 'postgres_key': pg_key,
                                        'csv_path': str(csv_path), 'csv_row': csv_row,
                                        'postgres_type': str(pg['type'])})
                    value_text = meaning or (
                        f'{table}.{column}: PostgreSQL type {pg["type"]}. '
                        'The original database_description CSV provides no field description.'
                    )
                    embed_text = (
                        f'Database: {db_id}. Table: {table} ({original_table}). '
                        f'Column: {column} ({original_column}). Type: {pg["type"]}. '
                        f'Description: {value_text}'
                    )
                    records.append({'db_id': db_id, 'record_type': 'column', 'table_name': table,
                                    'column_name': column, 'value_text': value_text,
                                    'embed_text': embed_text, 'metadata': metadata})
                    table_columns.append(f'{column}: {meaning[:120]}' if meaning else column)
                row_count = imported['schemas'][db_id][table]['rows']
                description = f'Database {db_id}. Table {table} ({original_table}). Columns: ' + '; '.join(table_columns)
                records.append({'db_id': db_id, 'record_type': 'table', 'table_name': table,
                                'column_name': None, 'value_text': description, 'embed_text': description,
                                'metadata': {'row_count': row_count, 'primary_keys': sorted(pk),
                                             'foreign_keys': fks, 'original_table_name': original_table,
                                             'columns': [c['name'] for c in pg_columns]}})
            print(f"Prepared {db_id}: {len(db['table_names_original'])} tables.", flush=True)
    for record in records:
        key = '|'.join([record['db_id'], record['record_type'], record['table_name'], record['column_name'] or ''])
        record['value_hash'] = digest('schema-metadata-v1|' + key)
        record['metadata']['embedding_text_sha256'] = digest(record['embed_text'])
        record['metadata']['embedding_model'] = os.environ['EMBEDDING_MODEL']
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, value in [('column_meaning.merged.json', merged), ('column_metadata.json', column_metadata),
                        ('missing_descriptions.json', missing), ('index_records.json', records)]:
        (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    summary = {'source': str(source_path), 'source_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
               'schemas': len(schemas), 'tables': sum(r['record_type'] == 'table' for r in records),
               'columns': len(column_metadata), 'share_descriptions': sum(bool(v) for v in meanings.values()),
               'csv_descriptions_filled': csv_filled, 'missing_descriptions': len(missing),
               'index_scope': 'all table and column metadata; no full distinct-cell-value index',
               'prepared_at': datetime.now(timezone.utc).isoformat()}
    (OUTPUT / 'metadata-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)
    return records


def embed_records(records):
    engine = create_engine(os.environ['BIRD_DEV_ADMIN_URI'], connect_args={'connect_timeout': 10})
    client = OpenAI(base_url=os.environ['EMBEDDING_API_URL'], api_key=os.environ['EMBEDDING_API_KEY'], timeout=120)
    model = os.environ['EMBEDDING_MODEL']
    dim = int(os.environ['EMBEDDING_DIM'])
    with engine.connect() as conn:
        if conn.execute(text('SELECT current_database()')).scalar_one() != 'AIDB-vector':
            raise RuntimeError('Refusing to populate a different vector database')
        existing = {row.value_hash: (row.sha, row.model) for row in conn.execute(text(
            f"SELECT value_hash, metadata->>'embedding_text_sha256' AS sha, "
            f"metadata->>'embedding_model' AS model FROM {VECTOR_TABLE} WHERE embedding IS NOT NULL"
        ))}
    pending = [r for r in records if existing.get(r['value_hash']) != (digest(r['embed_text']), model)]
    done = len(records) - len(pending)
    statement = text(f'''
        INSERT INTO {VECTOR_TABLE}
          (db_id, record_type, table_name, column_name, value_text, value_hash, embed_text, metadata, embedding)
        VALUES (:db_id, :record_type, :table_name, :column_name, :value_text, :value_hash,
                :embed_text, CAST(:metadata AS jsonb), CAST(:embedding AS vector))
        ON CONFLICT (db_id, record_type, table_name, (COALESCE(column_name, '')), value_hash)
        DO UPDATE SET value_text=EXCLUDED.value_text, embed_text=EXCLUDED.embed_text,
                      metadata=EXCLUDED.metadata, embedding=EXCLUDED.embedding
    ''')
    while pending:
        batch, chars = [], 0
        while pending and len(batch) < 8:
            n = len(pending[0]['embed_text'])
            if batch and chars + n > 18000:
                break
            batch.append(pending.pop(0))
            chars += n
        response = client.embeddings.create(model=model, input=[r['embed_text'] for r in batch], dimensions=dim)
        items = sorted(response.data, key=lambda row: row.index)
        if [row.index for row in items] != list(range(len(batch))):
            raise RuntimeError('Embedding response indices do not match inputs')
        payload = []
        for record, row in zip(batch, items):
            vector = row.embedding
            if len(vector) != dim or not all(math.isfinite(v) for v in vector):
                raise RuntimeError('Invalid embedding')
            if abs(sum(v * v for v in vector) - 1) > 0.01:
                raise RuntimeError('Embedding is not normalized')
            item = dict(record)
            item['metadata'] = json.dumps(record['metadata'], ensure_ascii=False, default=str)
            item['embedding'] = '[' + ','.join(map(str, vector)) + ']'
            payload.append(item)
        with engine.begin() as conn:
            conn.execute(statement, payload)
        done += len(batch)
        if done % 80 < len(batch) or not pending:
            print(f'Embedded and committed {done}/{len(records)} records.', flush=True)
    with engine.begin() as conn:
        conn.execute(text(f'ANALYZE {VECTOR_TABLE}'))
        counts = {row[0]: row[1] for row in conn.execute(text(
            f'SELECT record_type, count(*) FROM {VECTOR_TABLE} GROUP BY record_type'
        ))}
        missing = conn.execute(text(f'SELECT count(*) FROM {VECTOR_TABLE} WHERE embedding IS NULL')).scalar_one()
    if counts.get('table') != 75 or counts.get('column') != 798 or missing:
        raise RuntimeError(f'Unexpected index counts: {counts}; missing embeddings: {missing}')
    report = {'database': 'AIDB-vector', 'table': VECTOR_TABLE, 'dimensions': dim,
              'model': model, 'counts': counts, 'null_embeddings': missing,
              'completed_at': datetime.now(timezone.utc).isoformat()}
    (OUTPUT / 'index-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', action='store_true', help='Reuse prepared metadata and skip committed embeddings')
    args = parser.parse_args()
    load_dotenv(ROOT / '.env')
    if os.getenv('BIRD_DEV_INDEX_LAYOUT') == 'column_dual_v1':
        parser.error('The PG-derived concatenated index is retired. Use deploy/rebuild-column-sample.py --replace for the SQLite dual-vector sample.')
    records = (json.loads((OUTPUT / 'index_records.json').read_text(encoding='utf-8'))
               if args.resume else prepare_records())
    if not args.prepare_only:
        embed_records(records)


if __name__ == '__main__':
    main()
