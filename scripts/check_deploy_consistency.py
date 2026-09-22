"""B17: the declared deployment build path, the dependency lock, the serving artifact and the
verified start command must agree. Executable (not a string grep): it parses render.yaml, the
Dockerfile stages, the lock and pyproject, and reports every disagreement.

Run: `python -m scripts.check_deploy_consistency [repo_root]`. Exit 0 = consistent. Used by
`make deploy-check`, the CI lint job, scripts/image_smoke.sh (inside the built image, so the
check runs against the very tree that was built) and tests/security/test_deploy_consistency_17.py.
"""

import re
import sys
from pathlib import Path

import yaml

# Keys whose value is a credential: a blueprint may only declare them with `sync: false`.
SECRET_KEYS = {
    "DATABASE_URL", "OPENAI_API_KEY", "ODOO_INVENTORY_API_KEY", "CUSTOMER_KEY_HASH_SALT", "INTERNAL_API_KEY",
    "SHOPIFY_API_KEY", "SHOPIFY_API_SECRET", "ABUSE_IDENTITY_HASH_KEY",
}
# Keys that switch on a destructive or external behaviour: they must not be given a literal value.
GATE_KEYS = {"SHARED_DATA_DELETION_REVIEWED", "RETENTION_EXECUTION_ENABLED", "ODOO_INVENTORY_URL", "ODOO_INVENTORY_LOCATION_SCOPE", "ODOO_INVENTORY_QUANTITY_SEMANTICS", "MANUFACTURING_MAX_OIL_ML_PER_BOTTLE"}
VERIFIED_CMD = "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log --no-server-header"


def _stages(dockerfile: str) -> list[dict]:
    """Every `FROM ... AS name` stage with its instructions, in order."""
    stages: list[dict] = []
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
        if m:
            stages.append({"base": m.group(1), "name": m.group(2), "lines": []})
        elif stages:
            stages[-1]["lines"].append(line)
    # join continuation lines ("\") for RUN instructions
    for stage in stages:
        joined, buffer = [], ""
        for line in stage["lines"]:
            if line.endswith("\\"):
                buffer += line[:-1] + " "
                continue
            joined.append(buffer + line)
            buffer = ""
        stage["lines"] = joined
    return stages


def check(root: Path) -> list[str]:
    failures: list[str] = []
    blueprint = yaml.safe_load((root / "render.yaml").read_text())
    services = blueprint.get("services") or []
    web = [s for s in services if s.get("type") == "web"]
    if len(web) != 1:
        return [f"render.yaml: expected exactly one web service, found {len(web)}"]
    svc = web[0]

    # ---- 1. the declared build path is the committed Dockerfile, built at the repo root ----
    if svc.get("runtime") != "docker":
        failures.append(f"render.yaml: runtime is {svc.get('runtime')!r}, not 'docker' (the verified artifact is the Dockerfile image)")
    if svc.get("dockerfilePath", "./Dockerfile") not in ("./Dockerfile", "Dockerfile"):
        failures.append(f"render.yaml: dockerfilePath {svc.get('dockerfilePath')!r} is not the committed Dockerfile")
    if svc.get("dockerContext", ".") != ".":
        failures.append(f"render.yaml: dockerContext {svc.get('dockerContext')!r} is not the repository root")
    if "buildCommand" in svc or "startCommand" in svc:
        failures.append("render.yaml: buildCommand/startCommand belong to the native runtime and would bypass the Dockerfile")
    if svc.get("dockerCommand") not in (None, VERIFIED_CMD, f"sh -c \"{VERIFIED_CMD}\""):
        failures.append(f"render.yaml: dockerCommand overrides the verified CMD: {svc.get('dockerCommand')!r}")
    if svc.get("healthCheckPath") != "/health":
        failures.append(f"render.yaml: healthCheckPath is {svc.get('healthCheckPath')!r}")
    # YAML 1.1 readers (PyYAML) load a bare `off` as False; the blueprint quotes it, accept both.
    if svc.get("autoDeployTrigger") not in ("off", False) and svc.get("autoDeploy") is not False:
        failures.append("render.yaml: automatic deployment on push is not switched off (autoDeployTrigger: off)")
    for var in svc.get("envVars") or []:
        key = var.get("key")
        if key in SECRET_KEYS and (var.get("sync") is not False or "value" in var):
            failures.append(f"render.yaml: {key} must be `sync: false` with no value")
        if key in GATE_KEYS and "value" in var:
            failures.append(f"render.yaml: {key} must not be given a literal value in the blueprint")
        if key == "TRUSTED_PROXY_HOPS" and str(var.get("value")) not in ("0", "1"):
            failures.append(f"render.yaml: TRUSTED_PROXY_HOPS={var.get('value')!r} trusts more than the platform's single proxy hop")

    # ---- 2. the Dockerfile: locked install, serving stage last, verified CMD, non-root ----
    stages = _stages((root / "Dockerfile").read_text())
    names = [s["name"] for s in stages]
    if not stages or stages[-1]["name"] != "serve":
        failures.append(f"Dockerfile: the LAST stage (what a platform builds) is {names[-1] if names else None!r}, not 'serve'")
    if "ops" not in names:
        failures.append("Dockerfile: the operator stage 'ops' is missing (migrations/retention have no supplied artifact)")
    build = next((s for s in stages if s["name"] == "build"), None)
    if not build:
        failures.append("Dockerfile: no 'build' stage")
    else:
        runs = " ".join(line for line in build["lines"] if line.upper().startswith("RUN"))
        if "--require-hashes -r requirements.txt" not in runs:
            failures.append("Dockerfile: the build stage does not install requirements.txt with --require-hashes")
        if not re.search(r"pip install [^&]*--no-deps \.", runs):
            failures.append("Dockerfile: the application is not installed with --no-deps (dependencies would be re-resolved)")
    serve = stages[-1] if stages else {"lines": [], "base": ""}
    cmds = [line for line in serve["lines"] if line.upper().startswith("CMD")]
    if len(cmds) != 1 or VERIFIED_CMD not in cmds[0]:
        failures.append(f"Dockerfile: serving CMD differs from the verified start command: {cmds}")
    if "exec uvicorn" not in (cmds[0] if cmds else ""):
        failures.append("Dockerfile: uvicorn must be exec'd so SIGTERM reaches it")
    runtime_base = next((s for s in stages if s["name"] == "runtime-base"), None)
    if not runtime_base or not any(line == "USER app" for line in runtime_base["lines"]):
        failures.append("Dockerfile: runtime-base does not switch to the non-root user 'app'")
    if runtime_base and any(line.upper().startswith("COPY") and "scripts" in line for line in runtime_base["lines"]):
        failures.append("Dockerfile: operator scripts must not enter the runtime base (only the ops stage)")
    bases = {s["base"] for s in stages if not s["base"].startswith(("build", "runtime-base"))}
    python_version = (root / ".python-version").read_text().strip()
    for base in bases:
        if not base.startswith(f"python:{python_version}-"):
            failures.append(f"Dockerfile: base image {base!r} does not match .python-version {python_version!r}")
    if not re.fullmatch(r"3\.12\.\d+", python_version):
        failures.append(f".python-version {python_version!r} is not a fully qualified 3.12.x version")

    # ---- 3. the lock: hashed, complete for pyproject's runtime dependencies ----
    lock = (root / "requirements.txt").read_text()
    pins = {}
    for line in lock.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", line)
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    if not pins:
        failures.append("requirements.txt: no pins")
    for name in pins:
        pattern = re.escape(name).replace("\\-", "[-_]")
        block = re.search(rf"(?ms)^{pattern}==.*?(?=^\S|\Z)", lock, re.IGNORECASE)
        if not block or "--hash=sha256:" not in block.group(0):
            failures.append(f"requirements.txt: {name} has no hash")
    pyproject = (root / "pyproject.toml").read_text()
    deps_block = re.search(r"(?s)^dependencies\s*=\s*\[(.*?)\]", pyproject, re.M)
    direct = re.findall(r'"([A-Za-z0-9_.-]+)', deps_block.group(1)) if deps_block else []
    for dep in direct:
        if dep.lower().replace("_", "-") not in pins:
            failures.append(f"pyproject dependency {dep!r} is not pinned in requirements.txt (lock drift)")
    if "requires-python" in pyproject and not re.search(r'requires-python\s*=\s*">=3\.12"', pyproject):
        failures.append("pyproject: requires-python is not >=3.12")

    # ---- 4. the context never carries development data ----
    ignore = (root / ".dockerignore").read_text().splitlines()
    if ignore[0:1] != ["*"] and "*" not in [line.strip() for line in ignore]:
        failures.append(".dockerignore is not deny-by-default")
    allowed = {line.strip()[1:] for line in ignore if line.strip().startswith("!")}
    for forbidden in (".env", "tests/", ".git", "scripts/"):
        if forbidden in allowed:
            failures.append(f".dockerignore admits {forbidden!r}")
    return failures


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    failures = check(root)
    for failure in failures:
        print(f"DEPLOY_CONSISTENCY: {failure}")
    print("deploy consistency ok" if not failures else f"deploy consistency: {len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
