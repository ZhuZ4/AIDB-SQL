"""Integration checks for imported data, schema routing, and real vector recall."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / '.env')


def main():
    if os.environ.get('BIRD_DEV_INDEX_LAYOUT') == 'column_dual_v1':
        import runpy
        runpy.run_path(str(ROOT / 'deploy/check-column-sample.py'), run_name='__main__')
        return
    from tools import native_sql_tools as native
    from tools.bird_dev_retriever import hybrid_retrieve

    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'schemas': {}}
    vector = create_engine(os.environ['BIRD_DEV_PG_URI'])
    data = create_engine(os.environ['DATABASE_URI'])
    imported = json.loads((ROOT / '.local-services/minidev-import-verification.json').read_text(encoding='utf-8'))
    with vector.connect() as conn:
        assert conn.execute(text('SELECT current_database()')).scalar_one() == 'AIDB-vector'
        report['pgvector_version'] = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar_one()
        rows = conn.execute(text('''
            SELECT db_id, record_type, count(*), min(vector_dims(embedding)), max(vector_dims(embedding))
            FROM bird_dev_emb_v2.bird_dev_des_emb GROUP BY db_id, record_type
        ''')).all()
        counts = {(r[0], r[1]): r[2] for r in rows}
        assert all(r[3] == r[4] == 1024 for r in rows)
        assert conn.execute(text("SELECT has_table_privilege(current_user, 'bird_dev_emb_v2.bird_dev_des_emb', 'SELECT')")).scalar_one()
        assert not conn.execute(text("SELECT has_table_privilege(current_user, 'bird_dev_emb_v2.bird_dev_des_emb', 'INSERT')")).scalar_one()
        report['vector_records'] = sum(counts.values())
    with data.connect() as conn:
        assert conn.execute(text('SELECT current_database()')).scalar_one() == 'BIRD_minidev'
        report['reader_table_permissions'] = conn.execute(text('''
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind='r' AND n.nspname = ANY(:schemas)
              AND has_table_privilege(current_user, c.oid, 'SELECT')
              AND NOT has_table_privilege(current_user, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE')
        '''), {'schemas': list(imported['schemas'])}).scalar_one()
        assert report['reader_table_permissions'] == 75
        report['public_compatibility_views'] = len(inspect(data).get_view_names(schema='public'))
        assert report['public_compatibility_views'] == 75
        report['public_query_customers'] = conn.execute(text('SELECT count(*) FROM public.customers')).scalar_one()
        assert report['public_query_customers'] == imported['schemas']['debit_card_specializing']['customers']['rows']
    native.set_database_uri(os.environ['DATABASE_URI'], session_id='deployment-minidev-check')
    assert native._infer_current_db_id() == ''  # public must not be used as a dataset ID.
    for db_id, tables in imported['schemas'].items():
        response = native.db_search(db_id)
        assert native._infer_current_db_id() == db_id, response
        database = native._get_database()
        assert set(database.get_usable_table_names()) == set(tables)
        table = next(iter(tables))
        column = inspect(database._engine).get_columns(table)[0]['name']
        with database._engine.connect() as conn:
            conn.execute(text(f'SELECT "{column}" FROM "{table}" LIMIT 1')).all()
        result = hybrid_retrieve(table + ' ' + column, db_id=native._infer_current_db_id())
        assert result.tables and result.columns
        assert all(r.table_name in tables for r in result.tables + result.columns + result.values)
        for record in result.columns:
            names = {c['name'] for c in inspect(database._engine).get_columns(record.table_name)}
            assert record.column_name in names
        assert counts[(db_id, 'table')] == len(tables)
        assert counts[(db_id, 'column')] == sum(t['columns'] for t in tables.values())
        report['schemas'][db_id] = {'tables': len(tables), 'vector_columns': counts[(db_id, 'column')],
                                     'recalled_tables': len(result.tables), 'recalled_columns': len(result.columns),
                                     'schema_routing_and_read': 'passed'}
        print(f'Passed schema routing, SQL read, and vector recall: {db_id}', flush=True)
    # Exercise the public lookup tool and the linked schema consumer together.
    native.reset_session('deployment-minidev-check')
    native.db_search('debit_card_specializing')
    lookup = native.sql_db_value_lookup('customer account currency')
    cache = native._col_descriptions.get('deployment-minidev-check', {})
    assert cache.get('customers.currency', {}).get('comment'), lookup
    native.add_schema('customers.currency')
    mschema = native.build_linked_mschema()
    assert 'currency' in mschema and 'EUR' in mschema, mschema
    report['value_lookup_and_mschema'] = 'passed'
    # Also cover URL-only schema selection and the existing SQLite behavior.
    selected_uri = make_url(os.environ['DATABASE_URI']).update_query_dict({'options': '-csearch_path=financial'})
    native.set_database_uri(selected_uri.render_as_string(hide_password=False), session_id='deployment-uri-check')
    assert native._infer_current_db_id() == 'financial'
    sqlite_like = SimpleNamespace(dialect='sqlite', _engine=SimpleNamespace(
        url=make_url('sqlite:///F:/example/california_schools.sqlite')))
    assert native._infer_current_db_id(sqlite_like) == 'california_schools'
    report['schema_from_connection_search_path'] = 'passed'
    report['sqlite_db_id_regression'] = 'passed'
    target = ROOT / '.local-services/minidev-integration-verification.json'
    target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'schemas'}, indent=2))


if __name__ == '__main__':
    main()
