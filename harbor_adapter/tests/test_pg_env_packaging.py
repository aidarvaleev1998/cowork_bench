#!/usr/bin/env python3
"""Regression tests for packaged (populated) PostgreSQL env-file."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harbor_adapter"))

from validate_harbor_canonical import _validate_pg_env  # noqa: E402
from generate_harbor_canonical import _generate_pg_env_sh, _pg_env_example  # noqa: E402


DATASET_ROOT = os.environ.get("CANONICAL_DATASET_ROOT")
REGEN_ROOT = os.environ.get("CANONICAL_REGEN_ROOT")


def _dataset_or_skip(test: unittest.TestCase) -> Path:
    """Return the generated dataset root configured for integration tests."""
    if not DATASET_ROOT:
        test.skipTest("CANONICAL_DATASET_ROOT is unavailable")
    root = Path(DATASET_ROOT)
    if not root.is_dir():
        test.skipTest(f"generated dataset is unavailable: {root}")
    return root


def _fingerprint(root: Path) -> str:
    """Hash relative paths and bytes in deterministic order."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        # pg.env содержит пер-датасетный пароль: две независимые генерации
        # обязаны давать одинаковый fingerprint содержимого, кроме секрета.
        if relative == "STATIC_VALIDATION.json" or relative.endswith("environment/pg.env"):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class PgEnvPackagingTests(unittest.TestCase):
    """Validate pg.env cardinality and contents in a generated dataset."""

    def test_runtime_generator_includes_pg_aliases(self) -> None:
        """PG-backed MCPs may use either spelling for runtime credentials."""
        aliases = ("PG_USER", "PG_PASSWORD", "PG_DATABASE", "PG_HOST", "PG_PORT")
        for name, text in (
            ("script", _generate_pg_env_sh()),
            ("example", _pg_env_example()),
        ):
            with self.subTest(name=name):
                self.assertTrue(all(f"{key}=" in text for key in aliases))

    def test_validator_requires_populated_file_with_keys(self) -> None:
        """The validator reports missing/incomplete files without mutation."""
        with self.subTest("missing"), TemporaryDirectory() as td:
            path = Path(td) / "pg.env"
            self.assertEqual(
                _validate_pg_env(
                    "services:\n  db:\n    env_file:\n      - ./pg.env\n",
                    path,
                ),
                ["compose references ./pg.env but environment/pg.env is missing"],
            )
            self.assertFalse(path.exists())

        with self.subTest("empty"):
            with TemporaryDirectory() as td:
                path = Path(td) / "pg.env"
                path.write_bytes(b"")
                self.assertEqual(
                    _validate_pg_env(
                        "services:\n  db:\n    env_file:\n      - ./pg.env\n",
                        path,
                    ),
                    ["environment/pg.env is missing POSTGRES_USER",
                     "environment/pg.env is missing POSTGRES_PASSWORD",
                     "environment/pg.env is missing POSTGRES_DB",
                     "environment/pg.env is missing PGUSER",
                     "environment/pg.env is missing PGPASSWORD",
                     "environment/pg.env is missing PGDATABASE",
                     "environment/pg.env is missing PGHOST",
                     "environment/pg.env is missing PGPORT"],
                )

        with self.subTest("populated"):
            with TemporaryDirectory() as td:
                path = Path(td) / "pg.env"
                path.write_text(
                    "POSTGRES_USER=eigent\nPOSTGRES_PASSWORD=s3cret\n"
                    "POSTGRES_DB=cowork_gym\nPGUSER=eigent\nPGPASSWORD=s3cret\n"
                    "PGDATABASE=cowork_gym\nPGHOST=postgres\nPGPORT=5432\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    _validate_pg_env(
                        "services:\n  db:\n    env_file:\n      - ./pg.env\n",
                        path,
                    ),
                    [],
                )

    def test_validator_rejects_unreferenced_file(self) -> None:
        """A pg.env file is forbidden when compose does not reference it."""
        with TemporaryDirectory() as td:
            path = Path(td) / "pg.env"
            path.write_bytes(b"")
            self.assertEqual(
                _validate_pg_env("services:\n  db: {}\n", path),
                ["environment/pg.env exists without a compose reference"],
            )

    def test_pg_env_matches_compose_references(self) -> None:
        """Every DB compose reference has one zero-byte pg.env file."""
        root = _dataset_or_skip(self)
        tasks = sorted(p for p in root.iterdir() if (p / "task.toml").is_file())
        referenced = []
        unreferenced = []
        for task in tasks:
            compose = task / "environment" / "docker-compose.yaml"
            text = compose.read_text(encoding="utf-8")
            if "./pg.env" in text:
                referenced.append(task)
            else:
                unreferenced.append(task)

        self.assertEqual(len(tasks), 496)
        self.assertEqual(len(referenced), 490)
        self.assertEqual(len(unreferenced), 6)
        self.assertEqual(
            sum((task / "environment" / "pg.env").is_file() for task in referenced),
            490,
        )
        self.assertEqual(
            sum((task / "environment" / "pg.env").is_file() for task in unreferenced),
            0,
        )

    def test_pg_env_is_populated_and_consistent(self) -> None:
        """Packaged pg.env files are populated and identical across DB tasks."""
        root = _dataset_or_skip(self)
        files = sorted(root.glob("*/environment/pg.env"))
        self.assertEqual(len(files), 490)
        contents = {path.read_text(encoding="utf-8") for path in files}
        self.assertEqual(len(contents), 1, "credential sets differ across tasks")
        (text,) = contents
        data = dict(
            line.split("=", 1)
            for line in text.splitlines()
            if "=" in line and not line.startswith("#")
        )
        for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            self.assertTrue(data.get(key), f"missing {key}")
        self.assertNotEqual(data["POSTGRES_PASSWORD"], "camel")
        self.assertNotIn("/workspace/tasks", text)

    def test_pg_env_is_not_agent_visible(self) -> None:
        """The agent (main) service neither env_files nor mounts pg.env."""
        root = _dataset_or_skip(self)
        for compose in sorted(root.glob("*/environment/docker-compose.yaml")):
            main_block = compose.read_text(encoding="utf-8").split(
                "\n  workspace-prep:"
            )[0]
            self.assertNotIn("pg.env", main_block.split("\n  mcp-gateway-db:")[0])
            self.assertNotIn("PGPASSWORD", main_block.split("\n  mcp-gateway-db:")[0])

    def test_docker_compose_config_needs_no_validator_side_effect(self) -> None:
        """Compose config succeeds using packaged files only."""
        root = _dataset_or_skip(self)
        for compose in sorted(root.glob("*/environment/docker-compose.yaml")):
            before = {p.name for p in compose.parent.iterdir()}
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose), "config", "-q"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[:500])
            self.assertEqual({p.name for p in compose.parent.iterdir()}, before)

    def test_repeat_generation_fingerprint_if_supplied(self) -> None:
        """A separately generated tree is byte-identical when supplied."""
        root = _dataset_or_skip(self)
        if not REGEN_ROOT:
            self.skipTest("CANONICAL_REGEN_ROOT is unavailable")
        regenerated = Path(REGEN_ROOT)
        if not regenerated.is_dir():
            self.skipTest(f"regenerated dataset is unavailable: {regenerated}")
        self.assertEqual(_fingerprint(root), _fingerprint(regenerated))


if __name__ == "__main__":
    unittest.main()
