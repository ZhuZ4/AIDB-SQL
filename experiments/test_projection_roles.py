"""Offline policy boundaries; real ADK skill parsing, no provider requests."""
import asyncio
from contextlib import ExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

from experiments import worker
from skill_variants import resolve_data_link_policy


ROOT = Path(__file__).resolve().parents[1]
VARIANT = "explicit_projection_v1"
_SOCKET_CONNECT = socket.socket.connect


def _local_socketpair_only(sock, address):
    # Windows asyncio uses a loopback socket pair to wake its event loop.
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return _SOCKET_CONNECT(sock, address)
    raise AssertionError("offline test forbids external connections")


class ProjectionRolePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing agent creates an ADK root Agent, but never a network client
        # request. Use a model identifier and deny socket connections as well.
        with patch.object(socket.socket, "connect", new=_local_socketpair_only):
            import utils
            with patch.object(utils, "create_model", return_value="gemini-2.0-flash"), \
                    patch.dict(os.environ, {"DATABASE_URI": ""}):
                cls.agent = importlib.import_module("agent")
            cls.agent.create_model = utils.create_model

    def setUp(self):
        self.enterContext(patch.object(socket.socket, "connect", new=_local_socketpair_only))

    def test_baseline_uses_original_bytes_and_ignores_environment_policy(self):
        original = ROOT / "skills/data-link/SKILL.md"
        with patch.dict(os.environ, {"DATA_LINK_POLICY": VARIANT}):
            selection = resolve_data_link_policy()
            agent = self.agent.AdkAgent()
        self.assertEqual(selection.skill_path, original)
        self.assertEqual(selection.sha256, hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertEqual(agent.skill_policy_metadata, selection.metadata())
        from google.adk.skills import load_skill_from_dir
        expected = load_skill_from_dir(original.parent)
        actual = next(skill for skill in agent._skills if skill.name == "data-link")
        self.assertEqual(actual.model_dump(), expected.model_dump())

    def test_only_data_link_changes_and_default_instances_do_not_leak_policy(self):
        before = self.agent.AdkAgent()
        selected = self.agent.AdkAgent(data_link_policy=VARIANT)
        after = self.agent.AdkAgent()
        baseline = {skill.name: skill.model_dump() for skill in before._skills}
        variant = {skill.name: skill.model_dump() for skill in selected._skills}
        self.assertEqual(set(baseline), set(variant))
        self.assertEqual([name for name in baseline if baseline[name] != variant[name]], ["data-link"])
        self.assertEqual(before.get_available_skills(), selected.get_available_skills())
        self.assertEqual(baseline, {skill.name: skill.model_dump() for skill in after._skills})
        self.assertEqual(after.skill_policy_metadata["data_link_policy"], "baseline")
        self.assertEqual(selected.skill_policy_metadata, resolve_data_link_policy(VARIANT).metadata())
        # Metadata consumers cannot mutate another instance's selection.
        selected.skill_policy_metadata["data_link_policy"] = "baseline"
        self.assertEqual(selected.skill_policy_metadata["data_link_policy"], VARIANT)

    def test_unknown_policy_is_rejected_in_agent_service_and_worker_input(self):
        for value in ("typo", "BASELINE", None, {}, [VARIANT]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Unknown data_link_policy"):
                    self.agent.AdkAgent(data_link_policy=value)
                with self.assertRaisesRegex(ValueError, "Unknown data_link_policy"):
                    self.agent.AgentService(data_link_policy=value)
                with self.assertRaisesRegex(ValueError, "Unknown data_link_policy"):
                    worker.validate_input({"question": "Count rows", "db_id": "sample",
                                           "config": {"data_link_policy": value}})

    def test_missing_variant_stops_worker_before_environment_or_api_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(worker, "ROOT", root), \
                    patch("dotenv.load_dotenv", side_effect=AssertionError("must fail before env loading")):
                result = asyncio.run(worker.predict({"question": "Count rows", "db_id": "sample",
                                                     "config": {"data_link_policy": VARIANT}}))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["llm_calls"], 0)
            self.assertEqual(result["error"]["type"], "FileNotFoundError")
            self.assertFalse(result["retryable"])

    def test_malformed_selected_skill_cannot_be_swallowed_or_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "skills", root / "skills")
            target = root / "skill_variants/projection_roles/data-link/SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("Missing required frontmatter\n", encoding="utf-8")
            with patch.object(self.agent, "current_dir", str(root)):
                with self.assertRaises(ValueError):
                    self.agent.AdkAgent(data_link_policy=VARIANT)

    def test_missing_baseline_anchor_rejects_agent_and_service_with_valid_variant(self):
        anchor = ROOT / "skills/data-link/SKILL.md"
        original_exists = Path.exists

        def exists_without_anchor(path):
            return False if path == anchor else original_exists(path)

        # Keep actual variant bytes and ADK parsing; hide only the baseline
        # iteration anchor, which must never silently omit the selected skill.
        self.assertTrue(resolve_data_link_policy(VARIANT).skill_path.is_file())
        for constructor in (self.agent.AdkAgent, self.agent.AgentService):
            with self.subTest(constructor=constructor.__name__), \
                    patch.object(Path, "exists", new=exists_without_anchor), \
                    self.assertRaisesRegex(ValueError, "Exactly one selected data-link"):
                constructor(data_link_policy=VARIANT)

    def test_service_explicitly_forwards_policy_and_default_remains_baseline(self):
        with patch.object(self.agent.AgentService, "_build_agent", return_value=object()):
            variant = self.agent.AgentService(data_link_policy=VARIANT)
            baseline = self.agent.AgentService()
        self.assertEqual(variant.skill_policy_metadata, resolve_data_link_policy(VARIANT).metadata())
        self.assertEqual(baseline.skill_policy_metadata, resolve_data_link_policy().metadata())

    def test_gold_is_rejected_even_with_a_valid_policy(self):
        for key in ("SQL", "gold", "gold_sql", "difficulty"):
            payload = {"question": "Count rows", "db_id": "sample", key: "untrusted answer",
                       "config": {"data_link_policy": VARIANT}}
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.validate_input(payload)
            payload.pop(key)
            payload["config"][key] = "untrusted answer"
            with self.assertRaises(ValueError):
                worker.validate_input(payload)

    def test_worker_passes_policy_and_records_the_loaded_skill_hash(self):
        from tools import native_sql_tools
        observed = []

        class OfflineService:
            def __init__(self, *, experiment_profile, data_link_policy):
                observed.append({"policy": data_link_policy, "profile": experiment_profile})
                self.skill_policy_metadata = resolve_data_link_policy(data_link_policy).metadata()
                self._adk = self
                self.last_run_diagnostics = {}

            def get_available_skills(self):
                return [{"name": name} for name in
                        ("data-link", "database-query-helper", "correct", "schema-exploration")]

            async def run_query(self, **kwargs):
                observed[-1]["run_kwargs"] = kwargs
                return {"sql_execution_trace": []}

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            env = root / "offline.env"
            env.write_text("LITE_LLM_MODEL_NAME=deepseek-v4.1-flash\n"
                           "LITE_LLM_API_KEY=offline-unused\n"
                           "LITE_LLM_BASE_URL=https://offline.invalid/v1\n", encoding="utf-8")
            database = root / "sample/sample.sqlite"
            database.parent.mkdir()
            database.touch()  # URI validation only; fake service never opens it.
            stack.enter_context(patch.dict(os.environ, {}))
            stack.enter_context(patch.object(self.agent, "AgentService", OfflineService))
            stack.enter_context(patch("experiments.sqlite_runtime.bootstrap_sqlite_runtime",
                                      return_value={"version": "offline-test"}))
            stack.enter_context(patch.object(native_sql_tools, "get_final_sql", return_value={"sql": "SELECT 1"}))
            for name in ("get_sql_execution_trace", "get_correction_events", "get_linked_schema", "get_linked_schema_snapshot"):
                stack.enter_context(patch.object(native_sql_tools, name, return_value=[]))
            for policy in (VARIANT, "baseline"):
                config = {"env_file": str(env), "db_root": str(root)}
                if policy == VARIANT:
                    config["data_link_policy"] = policy
                result = asyncio.run(worker.predict({"question_id": 1, "db_id": "sample",
                    "question": "Count rows", "evidence": "Use all rows", "config": config}))
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["llm_calls"], 0)
                for key, value in resolve_data_link_policy(policy).metadata().items():
                    self.assertEqual(result["metadata"][key], value)
                self.assertEqual(observed[-1]["policy"], policy)
                self.assertEqual(observed[-1]["run_kwargs"]["question"], "Count rows")
                self.assertEqual(observed[-1]["run_kwargs"]["evidence"], "Use all rows")
                self.assertNotIn("config", observed[-1]["run_kwargs"])


class ProjectionRoleManifestTests(unittest.TestCase):
    def test_example_changes_only_experiment_and_policy(self):
        baseline = json.loads((ROOT / "experiments/experiment.example.json").read_text(encoding="utf-8"))
        variant = json.loads((ROOT / "experiments/projection_roles.example.json").read_text(encoding="utf-8"))
        self.assertEqual(variant.pop("data_link_policy"), VARIANT)
        self.assertNotEqual(variant["experiment_id"], baseline["experiment_id"])
        variant["experiment_id"] = baseline["experiment_id"]
        self.assertEqual(variant, baseline)

    def test_code_snapshot_covers_policy_module_and_raw_variant_changes(self):
        from experiments import run_batch
        current = run_batch.code_snapshot()
        for relative in ("skill_variants/__init__.py", "skill_variants/projection_roles/data-link/SKILL.md"):
            self.assertEqual(current[relative], hashlib.sha256((ROOT / relative).read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("agent.py", "utils.py", "AGENTS.md"):
                (root / name).write_text("offline fixture", encoding="utf-8")
            target = root / "skill_variants/projection_roles/data-link/SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"first version\r\n")
            with patch.object(run_batch, "ROOT", root):
                before = run_batch.code_snapshot()
                target.write_bytes(b"first version\n")
                after = run_batch.code_snapshot()
            key = "skill_variants/projection_roles/data-link/SKILL.md"
            self.assertNotEqual(before[key], after[key])
            self.assertEqual({k: v for k, v in before.items() if k != key},
                             {k: v for k, v in after.items() if k != key})


if __name__ == "__main__":
    unittest.main()
