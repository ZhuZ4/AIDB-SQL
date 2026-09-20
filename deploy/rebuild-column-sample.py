"""Replace mini-dev's old concatenated vectors with 20 SQLite column examples.

Each column gets an embedding of its exact name and a separate embedding of
its description. No business PostgreSQL database is read. The replacement is
committed only after all embeddings have been generated and validated.
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
import sqlite3

from dotenv import load_dotenv, set_key
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.local-services' / 'minidev-dual'
TABLE = 'bird_dev_emb_v2.column_embeddings'
LEGACY = 'bird_dev_emb_v2.bird_dev_des_emb'
SELECTION = [
    ('debit_card_specializing', 'customers', ['CustomerID', 'Segment', 'Currency']),
    ('california_schools', 'schools', ['CDSCode', 'School', 'City']),
    ('california_schools', 'frpm', ['Enrollment (K-12)', 'FRPM Count (K-12)', 'Percent (%) Eligible FRPM (K-12)']),
    ('formula_1', 'drivers', ['driverId', 'forename', 'surname', 'nationality']),
    ('card_games', 'cards', ['name', 'manaCost', 'convertedManaCost']),
    ('thrombosis_prediction', 'Laboratory', ['ID', 'PIC', 'TAT', 'TAT2']),
]
DDL = f'''
CREATE TABLE IF NOT EXISTS {TABLE} (
    id BIGSERIAL PRIMARY KEY,
    db_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    description TEXT,
    description_source TEXT NOT NULL,
    name_embedding vector(1024) NOT NULL,
    description_embedding vector(1024),
    embedding_model TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (db_id, table_name, column_name),
    CHECK (
        (description IS NULL AND description_embedding IS NULL) OR
        (description IS NOT NULL AND btrim(description) <> '' AND description_embedding IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS column_embeddings_name_hnsw
    ON {TABLE} USING hnsw (name_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS column_embeddings_description_hnsw
    ON {TABLE} USING hnsw (description_embedding vector_cosine_ops)
    WHERE description_embedding IS NOT NULL;
GRANT SELECT ON {TABLE} TO aidb_reader;
'''


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def sqlite_uri(path):
    return URL.create('sqlite', database='file:' + path.as_posix(),
                      query={'mode': 'ro', 'immutable': '1', 'uri': 'true'}).render_as_string()


def csv_rows(path):
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp1252', 'latin1'):
        try:
            value = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    return {row['original_column_name'].strip().casefold():
            {k: (v or '').strip() for k, v in row.items() if k}
            for row in csv.DictReader(io.StringIO(value))}


def prepare(benchmark):
    source = ROOT / 'column_meaning.json'
    meanings = json.loads(source.read_text(encoding='utf-8'))
    records = []
    for db_id, table, columns in SELECTION:
        path = benchmark / 'dev_databases' / db_id / (db_id + '.sqlite')
        csv_path = path.parent / 'database_description' / (table + '.csv')
        descriptions = csv_rows(csv_path)
        with sqlite3.connect('file:' + path.as_posix() + '?mode=ro&immutable=1', uri=True) as con:
            con.execute('PRAGMA query_only = ON')
            schema = {r[1]: r for r in con.execute('PRAGMA table_info(' + quote(table) + ')')}
            foreign_keys = [dict(zip(('id', 'seq', 'table', 'from', 'to', 'on_update', 'on_delete', 'match'), row))
                            for row in con.execute('PRAGMA foreign_key_list(' + quote(table) + ')')]
            for column in columns:
                info = schema[column]  # Exact spelling/case must exist in SQLite.
                key = f'{db_id}|{table}|{column}'
                csv_row = descriptions.get(column.strip().casefold(), {})
                raw_meaning = meanings.get(key)
                description = (raw_meaning or '').lstrip('#').strip() or None
                description_source = 'column_meaning.json'
                if description is None:
                    parts = []
                    for field in ('column_description', 'column_name', 'value_description'):
                        part = csv_row.get(field, '').strip()
                        if part and part != column and part not in parts:
                            parts.append(part)
                    description = '; '.join(parts) or None
                    description_source = 'database_description_csv' if description else 'missing_in_source_csv'
                values = con.execute(f'SELECT {quote(column)} FROM {quote(table)} '
                                     f'WHERE {quote(column)} IS NOT NULL LIMIT 5').fetchall()
                samples = list(dict.fromkeys(str(row[0]) for row in values))[:3]
                metadata = {
                    'source_kind': 'sqlite', 'sqlite_path': str(path), 'source_key': key,
                    'type': info[2], 'pk': bool(info[5]), 'primary_key_position': info[5],
                    'declared_not_null': bool(info[3]), 'default': info[4],
                    'foreign_keys': [fk for fk in foreign_keys if fk['from'] == column],
                    'samples': [{'value': v[:500], 'truncated': len(v) > 500} for v in samples],
                    'sample_policy': 'up to 3 distinct values from first 5 non-null rows',
                    'csv_path': str(csv_path), 'csv_row': csv_row,
                    'raw_meaning': raw_meaning, 'description_available': description is not None,
                    'name_input_sha256': sha(column),
                    'description_input_sha256': sha(description) if description else None,
                    'embedding_input_policy': 'exact column_name; description without leading #; no concatenation',
                }
                records.append({'db_id': db_id, 'table_name': table, 'column_name': column,
                                'description': description, 'description_source': description_source,
                                'metadata': metadata})
    assert len(records) == len({r['metadata']['source_key'] for r in records}) == 20
    return records


def generate_vectors(records):
    model = os.environ['EMBEDDING_MODEL']
    if int(os.environ['EMBEDDING_DIM']) != 1024:
        raise ValueError('This sample schema requires 1024-dimensional embeddings')
    client = OpenAI(base_url=os.environ['EMBEDDING_API_URL'],
                    api_key=os.environ['EMBEDDING_API_KEY'], timeout=120)
    jobs = []
    for record in records:
        record['embedding_model'] = model
        record['description_embedding'] = None
        jobs.append((record, 'name_embedding', record['column_name']))
        if record['description']:
            jobs.append((record, 'description_embedding', record['description']))
    audit = []
    for start in range(0, len(jobs), 8):
        batch = jobs[start:start + 8]
        result = client.embeddings.create(model=model, input=[j[2] for j in batch], dimensions=1024)
        items = sorted(result.data, key=lambda row: row.index)
        if [row.index for row in items] != list(range(len(batch))):
            raise RuntimeError('Embedding response does not match request indices')
        for (record, field, input_text), item in zip(batch, items):
            vector = item.embedding
            if len(vector) != 1024 or not all(math.isfinite(v) for v in vector):
                raise RuntimeError('Invalid embedding')
            if abs(sum(v * v for v in vector) - 1) > 0.01:
                raise RuntimeError('Expected normalized vectors')
            record[field] = vector
            audit.append({'source_key': record['metadata']['source_key'],
                          'vector_field': field, 'input': input_text,
                          'sha256': sha(input_text), 'dimensions': len(vector)})
    (OUTPUT / 'embedding-inputs.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Validated {len(jobs)} separate embeddings for 20 columns.', flush=True)


def replace(records, benchmark):
    ids = sorted({r['db_id'] for r in json.loads((benchmark / 'mini_dev_sqlite.json').read_text(encoding='utf-8'))})
    engine = create_engine(os.environ['BIRD_DEV_ADMIN_URI'], connect_args={'connect_timeout': 10})
    with engine.begin() as conn:
        if conn.execute(text('SELECT current_database()')).scalar_one() != 'AIDB-vector':
            raise RuntimeError('Replacement is restricted to AIDB-vector')
        conn.execute(text("SET LOCAL lock_timeout = '15s'"))
        conn.execute(text(f'LOCK TABLE {LEGACY} IN EXCLUSIVE MODE'))
        legacy_count = conn.execute(text(f'SELECT count(*) FROM {LEGACY} WHERE db_id = ANY(:ids)'), {'ids': ids}).scalar_one()
        conn.execute(text(DDL))
        conn.execute(text(f'LOCK TABLE {TABLE} IN EXCLUSIVE MODE'))
        conn.execute(text(f'DELETE FROM {TABLE} WHERE db_id = ANY(:ids)'), {'ids': ids})
        statement = text(f'''
            INSERT INTO {TABLE} (db_id, table_name, column_name, description, description_source,
                name_embedding, description_embedding, embedding_model, metadata)
            VALUES (:db_id, :table_name, :column_name, :description, :description_source,
                CAST(:name_embedding AS vector), CAST(:description_embedding AS vector),
                :embedding_model, CAST(:metadata AS jsonb))
        ''')
        payload = []
        for record in records:
            item = dict(record)
            item['metadata'] = json.dumps(item['metadata'], ensure_ascii=False)
            for field in ('name_embedding', 'description_embedding'):
                item[field] = json.dumps(item[field]) if item[field] is not None else None
            payload.append(item)
        conn.execute(statement, payload)
        deleted = conn.execute(text(f'DELETE FROM {LEGACY} WHERE db_id = ANY(:ids)'), {'ids': ids}).rowcount
        assert deleted == legacy_count
        count = conn.execute(text(f'SELECT count(*) FROM {TABLE} WHERE db_id = ANY(:ids)'), {'ids': ids}).scalar_one()
        assert count == 20
        assert conn.execute(text(f'SELECT count(*) FROM {LEGACY} WHERE db_id = ANY(:ids)'), {'ids': ids}).scalar_one() == 0
    with engine.begin() as conn:
        conn.execute(text(f'ANALYZE {TABLE}'))
    path = benchmark / 'dev_databases/california_schools/california_schools.sqlite'
    for key, value in {'DATABASE_URI': sqlite_uri(path), 'BIRD_DEV_DB_ID': '', 'DESCRIPTION_DIR': '',
                       'BIRD_DEV_SCHEMA': 'bird_dev_emb_v2', 'BIRD_DEV_COLUMN_TABLE': 'column_embeddings',
                       'BIRD_DEV_INDEX_LAYOUT': 'column_dual_v1', 'MINIDEV_ROOT': benchmark.as_posix()}.items():
        set_key(str(ROOT / '.env'), key, value, quote_mode='always')
    print(f'Committed 20 new columns; deleted {deleted} legacy vector records.', flush=True)
    return engine, deleted


def export_preview(engine, deleted):
    with engine.connect() as conn:
        rows = conn.execute(text(f'''
            SELECT id, db_id, table_name, column_name, description, description_source,
                name_embedding::text AS name_embedding,
                description_embedding::text AS description_embedding, embedding_model, metadata
            FROM {TABLE} ORDER BY id
        ''')).mappings().all()
    full, preview = [], []
    for row in rows:
        record = dict(row)
        for field in ('name_embedding', 'description_embedding'):
            record[field] = json.loads(record[field]) if record[field] else None
        full.append(record)
        compact = {k: v for k, v in record.items() if k not in ('name_embedding', 'description_embedding')}
        for field in ('name_embedding', 'description_embedding'):
            v = record[field]
            compact[field] = {'dimensions': len(v), 'first_8': v[:8],
                              'l2_norm': math.sqrt(sum(n * n for n in v))} if v else None
        preview.append(compact)
    summary = {'database': 'AIDB-vector', 'table': TABLE, 'columns': len(full),
               'datasets': len({r['db_id'] for r in full}),
               'source_tables': len({(r['db_id'], r['table_name']) for r in full}),
               'name_vectors': sum(r['name_embedding'] is not None for r in full),
               'description_vectors': sum(r['description_embedding'] is not None for r in full),
               'missing_descriptions': sum(r['description'] is None for r in full),
               'deleted_legacy_records': deleted, 'business_source': 'sqlite',
               'bm25_enabled': False, 'created_at': datetime.now(timezone.utc).isoformat()}
    for name, value in [('records-full.json', full), ('records-preview.json', preview), ('summary.json', summary)]:
        (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# mini-dev 字段双向量样例', '',
             f'数据库：`AIDB-vector`；表：`{TABLE}`。业务来源仅使用原始 SQLite。', '',
             f'实际写入 {len(full)} 个字段：20 个列名向量、17 个说明向量，均为 1024 维。删除旧嵌入 {deleted} 条。', '',
             '`name_embedding = embed(column_name)`；`description_embedding = embed(description)`。不额外拼接库名、表名、类型或样例。', '',
             '说明缺失时 description 和 description_embedding 均为 NULL；列名向量仍保留。BM25 索引尚未建立。', '',
             '| ID | 数据集 | 表 | 原始列名 / 列名嵌入输入 | SQLite 类型 | 列名向量 | 说明向量 |',
             '|---|---|---|---|---|---|---|']
    for r in full:
        lines.append(f"| {r['id']} | {r['db_id']} | {r['table_name']} | {r['column_name']} | {r['metadata']['type']} | 1024 维 | "
                     + ('1024 维 |' if r['description'] else 'NULL |'))
    lines.extend(['', '## 每个字段的实际说明输入', ''])
    for r in full:
        lines.extend([f"### {r['id']}. {r['db_id']} / {r['table_name']} / {r['column_name']}", '',
                      r['description'] or 'NULL：SHARE 字典没有该项，原始 CSV 对应行的说明为空。', ''])
    lines.extend(['## 查看向量库', '', '```sql', f'SELECT id, db_id, table_name, column_name, description,',
                  '       vector_dims(name_embedding) AS name_dimensions,',
                  '       vector_dims(description_embedding) AS description_dimensions',
                  f'FROM {TABLE}', 'ORDER BY id;', '```', '', '## 字段与索引定义', '', '```sql', DDL.strip(), '```', ''])
    (OUTPUT / 'preview.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true', help='Delete the old mini-dev vectors and install the 20 examples atomically')
    args = parser.parse_args()
    if not args.replace:
        parser.error('--replace is required; it removes the old mini-dev embeddings')
    load_dotenv(ROOT / '.env')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    benchmark = Path(os.environ['MINIDEV_ROOT'])
    records = prepare(benchmark)
    generate_vectors(records)
    engine, deleted = replace(records, benchmark)
    export_preview(engine, deleted)


if __name__ == '__main__':
    main()
