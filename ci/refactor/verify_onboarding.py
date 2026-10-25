"""Reproducible isolated onboarding verification; one target registry per run.

Starts only an explicitly named disposable PostgreSQL and a local Postman
simulation. Never touches existing registry containers, databases, credentials
or checkpoints. Raw simulation traffic is discarded, not committed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]


def main(domain, *, build=False):
    portable = (ROOT / f"{domain}-extension").is_dir()
    repo = ROOT if portable else ROOT / f"OAN/worktrees/{domain}-registry/develop-refactor"
    probes = ROOT / "ci/refactor" if portable else ROOT / "tools/registry_refactor"
    evidence = repo / "docs/refactor/evidence/onboarding"
    evidence.mkdir(parents=True, exist_ok=True)
    steps = []
    module = f"openg2p_registry_{domain}_extension"
    mount = ["-v", f"{repo.as_posix()}:/work:ro", "-v", f"{probes.as_posix()}:/probe:ro"]
    network = "coss-refactor-verify-" + domain
    database = network + "-db"
    simulation = None
    created_network = created_database = False
    test_container_names = []
    verification_complete = False
    traffic = {"listening":False, "requests":0, "statuses":{}, "methods":{}}

    def snapshot():
        paths = [path for path in (repo / f"{domain}-extension").rglob("*")
                 if path.is_file() and path.suffix in {".py", ".sql", ".j2", ".toml"}]
        paths += list((repo / "docker").glob("*/Dockerfile"))
        paths += [path for path in (repo / "postman").rglob("*") if path.is_file() and path.suffix in {".js", ".yaml"}]
        paths += list((repo / "ci/refactor").glob("*.py"))
        return {path.relative_to(repo).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}
    source_hashes = snapshot()

    def run(label, args, *, env=None, timeout=180, expected=0, persist=True):
        try:
            result = subprocess.run(args, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            steps.append({"check":label,"result":"FAIL","error_type":"TimeoutExpired"})
            raise
        if persist:
            for stream in ("stdout", "stderr"):
                (evidence / f"{label}.{stream}.txt").write_text(getattr(result, stream), encoding="utf-8", newline="\n")
        step = {"check":label, "exit_code":result.returncode, "expected_exit_code":expected,
                "result":"PASS" if result.returncode == expected else "FAIL"}
        steps.append(step)
        print(json.dumps({"registry":domain, **step}), flush=True)
        if result.returncode != expected:
            raise RuntimeError(label + ": " + (result.stderr or result.stdout)[-3500:])
        return result

    def docker_test(label, pattern, *, db=False, native_group=None):
        container_name = network + "-" + label
        existing = subprocess.run(["docker","container","inspect",container_name],capture_output=True)
        if existing.returncode == 0:
            raise RuntimeError("Owned test container name already exists; refusing to adopt it")
        test_container_names.append(container_name)
        options = ["--name",container_name,"--label","coss-refactor.disposable=onboarding","--network", network if db else "none", "--entrypoint", "python", *mount,
                   "-e", f"REGISTRY_EXTENSION_MODULE={module}", "-e", f"PYTHONPATH=/work/{domain}-extension/src"]
        if native_group:
            options += ["-e", "FARMER_NATIVE_GROUP=" + native_group]
        if db:
            options += ["-e", f"FARMER_TEST_DATABASE_URL=postgresql+asyncpg://postgres@{database}/refactor_tests"]
            if simulation:
                options += ["-e", "FARMER_WORKFLOW_SIMULATION_URL=http://host.docker.internal:49182/farmer"]
        return run(label, ["docker", "run", "--rm", *options, "-w", "/work", "oan/farmer-reference-tests:rc557",
                          "-m", "unittest", "discover", "-s", f"{domain}-extension/tests", "-p", pattern, "-v"])

    try:
        run("source-check", [sys.executable, str(probes / "check_port.py"), str(repo), domain])
        run("test-runtime", ["docker", "build", "--quiet", "-f", "ci/refactor/profile-tests.Dockerfile",
                             "-t", "oan/farmer-reference-tests:rc557", "."])
        docker_test("contract-tests", "test_farmer_reference_contract.py")
        docker_test("projection-tests", "test_farmer_reference_projection.py")
        docker_test("notification-contract-tests", "test_farmer_reference_notification.py")
        docker_test("ordinary-ci-discovery", "test_farmer_reference_*.py")
        for mode in ("staff", "partner", "worker", "beat"):
            role = "staff-api" if mode == "staff" else "partner-api" if mode == "partner" else "celery"
            run(f"native-{mode}-registration", ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                *mount, f"openg2p/openg2p-registry-{role}:1.2.2-rc.557", "/probe/onboarding_probe.py", "/work", domain, mode])
        run("create-network", ["docker", "network", "create", "--label", "coss-refactor.disposable=onboarding", network])
        created_network = True
        run("create-database", ["docker", "run", "--detach", "--name", database, "--network", network,
            "--label", "coss-refactor.disposable=onboarding", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e", "POSTGRES_DB=refactor_tests", "postgres:15-alpine"])
        created_database = True
        for _ in range(30):
            ready = subprocess.run(["docker", "exec", database, "pg_isready", "-U", "postgres", "-d", "refactor_tests"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Disposable database did not become ready")
        docker_test("postgres-concurrency", "test_farmer_reference_onboarding.py", db=True)
        docker_test("pending-lookup-tests", "test_farmer_reference_pending.py", db=True)
        postman = shutil.which("postman")
        if not postman:
            raise RuntimeError("Postman CLI 1.73 or later is required")
        simulation = subprocess.Popen([postman, "simulation", "run", "postman/simulations/farmer-reference.sim.yaml",
            "--port", "49182", "--output", "ndjson"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
        def consume():
            for line in simulation.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "listening":
                    traffic["listening"] = True
                if event.get("event") == "request":
                    traffic["requests"] += 1
                    for key, field in (("statuses", "statusCode"), ("methods", "method")):
                        value = str(event.get(field))
                        traffic[key][value] = traffic[key].get(value, 0) + 1
        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        deadline = time.monotonic() + 20
        while not traffic["listening"] and simulation.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if not traffic["listening"]:
            raise RuntimeError("Local Postman simulation did not listen; verify port 49182 is unused")
        docker_test("native-cr-lifecycle", "test_farmer_reference_native.py", db=True, native_group="core")
        docker_test("native-workflow-http", "test_farmer_reference_native.py", db=True, native_group="workflow")
        docker_test("native-notification-lifecycle", "test_farmer_reference_notification_native.py", db=True)
        environment = dict(os.environ, PYTHONPATH=str(repo / f"{domain}-extension/src"),
            REGISTRY_EXTENSION_MODULE=module, FARMER_SIMULATION_URL="http://127.0.0.1:49182/farmer")
        run("signed-lookup-http", ["uv", "run", "--no-project", "--python", "3.12", "--with", "httpx", "--with",
            "pyjwt[crypto]", "python", "-m", "unittest", "discover", "-s", f"{domain}-extension/tests", "-p",
            "test_farmer_reference_http.py", "-v"], env=environment)
        if traffic["requests"] < 20:
            raise RuntimeError("The HTTP test did not exercise the simulation")
        steps.append({"check":"simulation-traffic", "result":"PASS", **traffic, "raw_traffic_saved":False})
        if build:
            short = "crop" if domain == "cropsown" else domain
            for role in ("staff-api", "partner-api", "celery", "db-seed"):
                tag = f"oan/{short}-{role}:refactor-onboarding"
                run(f"build-{role}", ["docker", "build", "--quiet", "-f", f"docker/{role}/Dockerfile", "-t", tag, "."])
                result = run(f"image-{role}", ["docker", "image", "inspect", tag, "--format", "{{json .Id}}"])
                steps[-1]["image_id"] = json.loads(result.stdout)
                if role in ("staff-api", "partner-api", "celery"):
                    mode = "staff" if role == "staff-api" else "partner" if role == "partner-api" else "worker"
                    run(f"installed-{mode}-registration", ["docker", "run", "--rm", "--network", "none",
                        "--entrypoint", "python", "-v", f"{probes.as_posix()}:/probe:ro",
                        tag, "/probe/onboarding_probe.py", "/installed-wheel", domain, mode])
            run("preservation-seed-gate", ["docker", "run", "--rm", "--network", "none", f"oan/{short}-db-seed:refactor-onboarding"], expected=1)
        verification_complete = True
    finally:
        if simulation and simulation.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(simulation.pid), "/T", "/F"], capture_output=True)
            else:
                import signal
                os.killpg(simulation.pid, signal.SIGTERM)
            simulation.wait(timeout=15)
        # A timed-out Docker CLI does not stop its container. Remove only the
        # exact owned names before the disposable DB/network, preserving the
        # original failing check rather than masking it with a busy network.
        for container_name in test_container_names:
            subprocess.run(["docker","rm","--force",container_name],capture_output=True)
        if created_database:
            subprocess.run(["docker", "rm", "--force", database], capture_output=True, check=True)
        if created_network:
            subprocess.run(["docker", "network", "rm", network], capture_output=True, check=True)
        unchanged = snapshot() == source_hashes
        steps.append({"check":"source-unchanged-during-verification", "result":"PASS" if unchanged else "FAIL"})
        (repo / "docs/refactor/ONBOARDING_VERIFICATION.json").write_text(json.dumps({"registry":domain,
            "verified_at":datetime.now(timezone.utc).isoformat(), "source_sha256":source_hashes,
            "verification_complete":verification_complete and unchanged and all(step["result"]=="PASS" for step in steps),
            "registry_stacks_started":0, "existing_database_modified":False, "source_farmer_modified":False,
            "live_kafka_awe_acceptance":"NOT RUN", "steps":steps}, indent=2) + "\n", encoding="utf-8", newline="\n")
        if not unchanged:
            raise RuntimeError("Source changed during verification; rerun against a stable tree")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("domain", choices=("cropsown", "livestock"))
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    main(args.domain, build=args.build)
