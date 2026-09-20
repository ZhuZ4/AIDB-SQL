"""Exercise the real Windows -> WSL and embedding -> pgvector connections."""
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, text


def main():
    load_dotenv(ROOT / '.env')
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'checks': {}}
    checks = report['checks']
    dual_columns = os.environ.get('BIRD_DEV_INDEX_LAYOUT') == 'column_dual_v1'
    vector_table = ('bird_dev_emb_v2.column_embeddings' if dual_columns
                    else 'bird_dev_emb_v2.bird_dev_des_emb')
    from google.adk.skills import load_skill_from_dir
    from google.adk.tools.skill_toolset import SkillToolset
    from google.adk.agents.run_config import RunConfig, StreamingMode
    from google.adk.models.lite_llm import LiteLlm
    from langchain_community.utilities import SQLDatabase
    checks['python_dependencies'] = {
        name: importlib.metadata.version(name)
        for name in ('google-adk', 'litellm', 'openai', 'SQLAlchemy', 'psycopg2-binary')
    }
    engine = create_engine(os.environ['BIRD_DEV_PG_URI'], connect_args={'connect_timeout': 8})
    with engine.connect() as conn:
        checks['postgres_version'] = conn.execute(text('SHOW server_version')).scalar_one()
        checks['pgvector_version'] = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar_one()
        checks['database_user'] = conn.execute(text('SELECT current_user')).scalar_one()
        checks['vector_table'] = vector_table
        checks['vector_records'] = conn.execute(text(f'SELECT count(*) FROM {vector_table}')).scalar_one()
        checks['reader_has_select'] = conn.execute(text("SELECT has_table_privilege(current_user, :table, 'SELECT')"), {'table': vector_table}).scalar_one()
        checks['reader_has_insert'] = conn.execute(text("SELECT has_table_privilege(current_user, :table, 'INSERT')"), {'table': vector_table}).scalar_one()
        assert checks['reader_has_select'] and not checks['reader_has_insert']
    client = OpenAI(base_url=os.environ['EMBEDDING_API_URL'], api_key=os.environ['EMBEDDING_API_KEY'], timeout=60)
    embeddings = client.embeddings.create(
        model=os.environ['EMBEDDING_MODEL'],
        input=['California school names', 'Schools located in California'],
        dimensions=int(os.environ['EMBEDDING_DIM']),
    )
    assert len(embeddings.data) == 2
    vectors = [row.embedding for row in embeddings.data]
    assert all(len(v) == 1024 and all(math.isfinite(n) for n in v) for v in vectors)
    assert all(abs(sum(n * n for n in v) - 1) < 0.01 for v in vectors)
    checks['embedding_dimensions'] = [len(v) for v in vectors]
    checks['embedding_normalized'] = True
    # Use the project's actual embedding client as an integration check.
    from tools.bird_dev_retriever import _generate_embedding
    project_vector = _generate_embedding('California school names')
    assert project_vector is not None and len(project_vector) == 1024
    checks['project_embedding_client'] = 'passed'
    admin = create_engine(os.environ['BIRD_DEV_ADMIN_URI'], connect_args={'connect_timeout': 8})
    vector_text = '[' + ','.join(map(str, project_vector)) + ']'
    with admin.connect() as conn:
        txn = conn.begin()
        try:
            if dual_columns:
                probe_id = conn.execute(text(f'''
                    INSERT INTO {vector_table}
                        (db_id, table_name, column_name, description_source, name_embedding, embedding_model)
                    VALUES ('__deployment_probe__', 'probe', 'probe', 'probe', CAST(:vector AS vector), :model)
                    RETURNING id
                '''), {'vector': vector_text, 'model': os.environ['EMBEDDING_MODEL']}).scalar_one()
                vector_column = 'name_embedding'
            else:
                probe_id = conn.execute(text('''
                INSERT INTO bird_dev_emb_v2.bird_dev_des_emb
                    (db_id, record_type, table_name, value_text, embedding)
                VALUES ('__deployment_probe__', 'table', 'probe', 'California school names', CAST(:vector AS vector))
                RETURNING id
                '''), {'vector': vector_text}).scalar_one()
                vector_column = 'embedding'
            similarity = conn.execute(text(f'''
                SELECT 1 - ({vector_column} <=> CAST(:vector AS vector))
                FROM {vector_table} WHERE id = :id
            '''), {'vector': vector_text, 'id': probe_id}).scalar_one()
            assert abs(similarity - 1) < 1e-5
            checks['pgvector_round_trip_similarity'] = float(similarity)
        finally:
            txn.rollback()
    checks['probe_transaction_rolled_back'] = True
    benchmark = ROOT.parent / 'minidev' / 'MINIDEV'
    questions = json.loads((benchmark / 'mini_dev_sqlite.json').read_text(encoding='utf-8'))
    ids = sorted({row['db_id'] for row in questions})
    assert len(questions) == 500 and len(ids) == 11
    assert all((benchmark / 'dev_databases' / name / (name + '.sqlite')).is_file() for name in ids)
    checks['benchmark_files'] = {'questions': len(questions), 'databases': len(ids)}
    checks['llm_api_configured'] = all(os.environ.get(k) for k in ('LITE_LLM_API_KEY', 'LITE_LLM_BASE_URL', 'LITE_LLM_MODEL_NAME'))
    checks['missing_pipeline_skills'] = [name for name in ('data-link', 'database-query-helper', 'correct', 'schema-exploration') if not (ROOT / 'skills' / name / 'SKILL.md').is_file()]
    output = ROOT / '.local-services' / 'verification.json'
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
