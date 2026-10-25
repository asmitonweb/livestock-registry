"""Run one registry's metadata source through native DDL in a disposable DB."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import re
import time
import os

ROOT = Path(__file__).resolve().parents[2]

def main(domain, pipeline=False):
    portable = (ROOT / f"{domain}-extension").is_dir()
    repo = ROOT if portable else ROOT / f"OAN/worktrees/{domain}-registry/develop-refactor"
    probes = ROOT / "ci/refactor" if portable else ROOT / "tools/registry_refactor"
    network = "coss-refactor-metadata-" + domain
    database = network + "-db"
    created_network = created_database = False
    def run(*args):
        result = subprocess.run(args,capture_output=True,text=True,encoding="utf-8",errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr[-1800:])
        return result
    try:
        run("docker","network","create","--label","coss-refactor.disposable=metadata",network)
        created_network = True
        run("docker","run","--detach","--name",database,"--network",network,"--label","coss-refactor.disposable=metadata",
            "-e","POSTGRES_HOST_AUTH_METHOD=trust","-e","POSTGRES_DB=refactor_metadata","postgres:15-alpine")
        created_database = True
        for _ in range(40):
            ready = subprocess.run(["docker","exec",database,"pg_isready","-U","postgres"],capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Disposable database did not become ready")
        def probe(mode):
            result = run("docker","run","--rm","--network",network,"--entrypoint","python",
                "-v",f"{repo.as_posix()}:/work","-v",f"{probes.as_posix()}:/probe:ro",
                os.environ.get("COSS_REFACTOR_TEST_IMAGE","oan/farmer-reference-tests:rc557"),"/probe/metadata_probe.py","/work",domain,mode)
            print(result.stdout.splitlines()[-1],flush=True)
        probe("ddl")
        package = repo / f"{domain}-extension/src/openg2p_registry_{domain}_extension"
        files = sorted((package / "meta_data").rglob("*.sql"))
        steps = []
        for attempt in (1,2):
            for path in files:
                # Use native psql parsing/autocommit/ON_ERROR_STOP behavior,
                # preserving literal percent signs and DO/dollar-quoted blocks.
                raw = path.read_bytes()
                result = subprocess.run(["docker","exec","-i",database,"psql","-X","-U","postgres",
                    "-d","refactor_metadata","-v","ON_ERROR_STOP=0","-v","VERBOSITY=sqlstate"],
                    input=raw,capture_output=True)
                errors = re.findall(rb"ERROR:\s+([0-9A-Z]{5})",result.stderr)
                steps.append({"pass":attempt,"path":path.relative_to(repo).as_posix(),
                    "sha256":hashlib.sha256(raw).hexdigest(),"sqlstate_errors":[code.decode() for code in errors],
                    "psql_exit_code":result.returncode})
            probe(str(attempt))
        if pipeline:
            result = run("docker","run","--rm","--network",network,"--entrypoint","python",
                "-v",f"{repo.as_posix()}:/work","-v",f"{probes.as_posix()}:/probe:ro",
                "openg2p/openg2p-registry-celery:1.2.2-rc.557","/probe/notification_pipeline_probe.py","/work",domain)
            print(result.stdout.splitlines()[-1],flush=True)
        output = repo / "docs/refactor/new-dev"
        first = json.loads((output / "target-pass-1.json").read_text(encoding="utf-8"))
        second = json.loads((output / "target-pass-2.json").read_text(encoding="utf-8"))
        proof = {"registry":domain,"native_psql_semantics":True,"steps":steps,
                 "repeat_metadata_identical":first["metadata"] == second["metadata"],
                 "existing_environment_modified":False,"disposable_database_removed":True}
        (output / "seed-rehearsal.json").write_text(json.dumps(proof,indent=2)+"\n",encoding="utf-8",newline="\n")
        print(json.dumps({"registry":domain,"repeat_metadata_identical":proof["repeat_metadata_identical"],
            "files_with_errors":len([step for step in steps if step["sqlstate_errors"]])}),flush=True)
        if not proof["repeat_metadata_identical"] or any(step["sqlstate_errors"] or step["psql_exit_code"] for step in steps):
            raise RuntimeError("Metadata rehearsal failed: SQL errors or non-identical repeated configuration; inspect seed-rehearsal.json")
    finally:
        if created_database:
            run("docker","rm","--force",database)
        if created_network:
            run("docker","network","rm",network)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("domain",choices=("cropsown","livestock"))
    parser.add_argument("--pipeline",action="store_true")
    args=parser.parse_args()
    main(args.domain,args.pipeline)
