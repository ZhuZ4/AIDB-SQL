"""Verify separate vectors, SQLite mappings, permissions, and retrieval wiring."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / '.local-services/minidev-dual'
TABLE = 'bird_dev_emb_v2.column_embeddings'


def main():
    load_dotenv(ROOT / '.env')
    from tools import native_sql_tools as native
    from tools.bird_dev_retriever import hybrid_retrieve

    assert os.environ['BIRD_DEV_INDEX_LAYOUT'] == 'column_dual_v1'
    assert make_url(os.environ['DATABASE_URI']).drivername == 'sqlite'
    engine = create_engine(os.environ['BIRD_DEV_PG_URI'])
    with engine.connect() as conn:
        assert conn.execute(text('SELECT current_database()')).scalar_one() == 'AIDB-vector'
        assert conn.execute(text('SELECT count(*) FROM bird_dev_emb_v2.bird_dev_des_emb')).scalar_one() == 0
        rows = conn.execute(text(f'''
            SELECT id, db_id, table_name, column_name, description, description_source, metadata,
                   name_embedding::text AS name_embedding,
                   description_embedding::text AS description_embedding
            FROM {TABLE} ORDER BY id
        ''')).mappings().all()
        assert conn.execute(text("SELECT has_table_privilege(current_user, :t, 'SELECT')"), {'t': TABLE}).scalar_one()
        assert not conn.execute(text("SELECT has_table_privilege(current_user, :t, 'INSERT,UPDATE,DELETE,TRUNCATE')"), {'t': TABLE}).scalar_one()
        indexes = conn.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname='bird_dev_emb_v2' AND tablename='column_embeddings'")).scalars().all()
        assert sum('USING hnsw' in index for index in indexes) == 2
    assert len(rows) == 20
    assert len({(r['db_id'], r['table_name'], r['column_name']) for r in rows}) == 20
    audit = json.loads((OUTPUT / 'embedding-inputs.json').read_text(encoding='utf-8'))
    inputs = {(r['source_key'], r['vector_field']): r['input'] for r in audit}
    assert len(inputs) == len(audit) == 37
    checked = 0
    for r in rows:
        metadata = r['metadata']
        source_key = f"{r['db_id']}|{r['table_name']}|{r['column_name']}"
        assert metadata['source_kind'] == 'sqlite' and metadata['source_key'] == source_key
        assert inputs[(source_key, 'name_embedding')] == r['column_name']
        assert (r['description'] is None) == (r['description_embedding'] is None)
        if r['description'] is not None:
            assert inputs[(source_key, 'description_embedding')] == r['description']
        else:
            assert (source_key, 'description_embedding') not in inputs
            assert r['description_source'] == 'missing_in_source_csv'
        path = Path(metadata['sqlite_path'])
        with sqlite3.connect('file:' + path.as_posix() + '?mode=ro&immutable=1', uri=True) as sqlite:
            quoted = '"' + r['table_name'].replace('"', '""') + '"'
            columns = {col[1]: col for col in sqlite.execute('PRAGMA table_info(' + quoted + ')')}
            assert r['column_name'] in columns
            assert columns[r['column_name']][2] == metadata['type']
        for field in ('name_embedding', 'description_embedding'):
            if r[field] is None:
                continue
            vector = json.loads(r[field])
            assert len(vector) == 1024 and all(math.isfinite(v) for v in vector)
            assert abs(sum(v * v for v in vector) - 1) < 0.01
            checked += 1
    assert checked == 37
    # Re-embed actual source text and compare it to database vectors, checking
    # that we did not merely store the right labels next to a concatenated vector.
    example = next(r for r in rows if r['table_name'] == 'customers' and r['column_name'] == 'Currency')
    client = OpenAI(base_url=os.environ['EMBEDDING_API_URL'], api_key=os.environ['EMBEDDING_API_KEY'], timeout=60)
    fresh = sorted(client.embeddings.create(model=os.environ['EMBEDDING_MODEL'],
                   input=[example['column_name'], example['description']], dimensions=1024).data,
                   key=lambda item: item.index)
    for field, new in zip(('name_embedding', 'description_embedding'), fresh):
        stored = json.loads(example[field])
        assert max(abs(a - b) for a, b in zip(stored, new.embedding)) < 1e-4
    checks = {}
    for db_id in sorted({r['db_id'] for r in rows}):
        group = [r for r in rows if r['db_id'] == db_id]
        path = Path(group[0]['metadata']['sqlite_path'])
        url = URL.create('sqlite', database='file:' + path.as_posix(),
                         query={'mode': 'ro', 'immutable': '1', 'uri': 'true'})
        native.set_database_uri(url.render_as_string(), session_id='dual-check-' + db_id)
        assert native._infer_current_db_id() == db_id
        result = hybrid_retrieve(group[0]['column_name'], db_id=db_id, table_top_k=10, column_top_k=20)
        assert {r.id for r in result.columns} == {r['id'] for r in group}
        assert all(r.metadata['source_kind'] == 'sqlite' for r in result.columns)
        assert all(r.metadata['source_key'].startswith(db_id + '|') for r in result.columns)
        assert result.tables and not result.values
        checks[db_id] = {'mapped_columns': len(group), 'retrieved_columns': len(result.columns)}
        print(f'Passed SQLite mapping and two-vector recall: {db_id}', flush=True)
    # Confirm name-only rows still participate even though no description exists.
    result = hybrid_retrieve('PIC', db_id='thrombosis_prediction', table_top_k=10, column_top_k=20)
    pic = next(r for r in result.columns if r.column_name == 'PIC')
    assert 'name' in pic.metadata['retrieval_channels']
    assert 'description' not in pic.metadata['retrieval_channels']
    path = Path(example['metadata']['sqlite_path'])
    url = URL.create('sqlite', database='file:' + path.as_posix(),
                     query={'mode': 'ro', 'immutable': '1', 'uri': 'true'})
    native.set_database_uri(url.render_as_string(), session_id='dual-check-mschema')
    lookup = native.sql_db_value_lookup('customer account currency')
    cache = native._col_descriptions.get('dual-check-mschema', {})
    assert cache.get('customers.Currency', {}).get('comment'), lookup
    native.add_schema('customers.Currency')
    mschema = native.build_linked_mschema()
    assert 'Currency' in mschema and 'EUR' in mschema, mschema
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'columns': 20,
              'valid_vectors': checked, 'legacy_vector_records': 0,
              'source': 'sqlite', 'checks_by_dataset': checks,
              'independent_embedding_inputs': 'passed', 'fresh_embedding_comparison': 'passed',
              'read_only_role': 'passed', 'two_hnsw_indexes': 'passed',
              'missing_description_name_recall': 'passed', 'lookup_to_mschema': 'passed',
              'bm25_enabled': False}
    (OUTPUT / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
