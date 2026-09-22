"""B17: the deployment blueprint, the Dockerfile, the dependency lock and the verified start
command must agree, and every drift kind must be detected. The check itself lives in
scripts/check_deploy_consistency.py (also run by `make deploy-check`, the CI lint job and
scripts/image_smoke.sh inside the built image)."""

import shutil
import tempfile
from pathlib import Path

import pytest

from scripts.check_deploy_consistency import check

ROOT = Path(__file__).resolve().parents[2]
FILES = ("render.yaml", "Dockerfile", ".dockerignore", ".python-version", "requirements.txt", "pyproject.toml")


def _variant(edit) -> list[str]:
    tmp = Path(tempfile.mkdtemp())
    for name in FILES:
        shutil.copy(ROOT / name, tmp / name)
    edit(tmp)
    return check(tmp)


def _replace(name: str, old: str, new: str):
    def _edit(tmp: Path) -> None:
        text = (tmp / name).read_text()
        assert old in text, old
        (tmp / name).write_text(text.replace(old, new))
    return _edit


def test_repository_is_consistent():
    assert check(ROOT) == []


@pytest.mark.parametrize("label, edit, expected", [
    ("native runtime", _replace("render.yaml", "runtime: docker", "runtime: python"), "not 'docker'"),
    ("auto deploy on push", _replace("render.yaml", 'autoDeployTrigger: "off"', "autoDeployTrigger: commit"), "automatic deployment"),
    ("secret given a value", _replace("render.yaml", "- key: INTERNAL_API_KEY\n        sync: false", "- key: INTERNAL_API_KEY\n        value: x"), "INTERNAL_API_KEY"),
    ("gate given a value", _replace("render.yaml", "- key: ODOO_INVENTORY_URL\n        sync: false", "- key: ODOO_INVENTORY_URL\n        value: https://x"), "ODOO_INVENTORY_URL"),
    ("start command override", _replace("render.yaml", "    healthCheckPath: /health", "    dockerCommand: uvicorn app.main:app\n    healthCheckPath: /health"), "dockerCommand"),
    ("ops stage built by default", _replace("Dockerfile", "FROM runtime-base AS serve", "FROM runtime-base AS serve2"), "LAST stage"),
    ("sh swallows SIGTERM", _replace("Dockerfile", "exec uvicorn", "uvicorn"), "verified start command"),
    ("dependencies re-resolved", _replace("Dockerfile", "--require-hashes -r requirements.txt", "-r requirements.txt"), "--require-hashes"),
    ("app install resolves deps", _replace("Dockerfile", "--no-deps .", "."), "--no-deps"),
    ("root user", _replace("Dockerfile", "\nUSER app\n\nENV PORT", "\n\nENV PORT"), "non-root"),
    ("interpreter drift", lambda tmp: (tmp / ".python-version").write_text("3.12.10\n"), ".python-version"),
    ("lock drift", _replace("pyproject.toml", "dependencies = [", 'dependencies = [\n    "leftpad>=1",'), "lock drift"),
    ("unhashed pin", lambda tmp: (tmp / "requirements.txt").write_text((tmp / "requirements.txt").read_text() + "\nleftpad==1.0\n"), "no hash"),
    ("context admits tests", _replace(".dockerignore", "!migrations/", "!migrations/\n!tests/"), "admits"),
])
def test_every_drift_is_detected(label, edit, expected):
    failures = _variant(edit)
    assert failures and any(expected in f for f in failures), (label, failures)
