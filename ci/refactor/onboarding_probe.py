"""Native registration/loader probe. No application server, broker or DB access."""
import importlib
import json
import os
from pathlib import Path
import sys

repo, domain, mode = sys.argv[1:]
source = Path(repo) / f"{domain}-extension/src"
sys.path.insert(0, str(source))
os.environ["REGISTRY_EXTENSION_MODULE"] = f"openg2p_registry_{domain}_extension"
package = importlib.import_module(os.environ["REGISTRY_EXTENSION_MODULE"])
sys.modules["openg2p_registry_extensions"] = package

if mode in {"staff", "partner"}:
    api_module = "openg2p_registry_staff_api" if mode == "staff" else "openg2p_registry_partner_api"
    importlib.import_module(api_module + ".config").Settings.get_config()
    extension_app = importlib.import_module("openg2p_registry_extensions.app")
    extension_app.Initializer()
    celery_app = None
else:
    app_module = "openg2p_registry_celery_worker" if mode == "worker" else "openg2p_registry_celery_beat"
    # A non-default queue proves the extension follows native configuration,
    # rather than accidentally executing a worker task in the beat producer.
    os.environ["REGISTRY_CELERY_WORKERS_WORKER_QUEUE"] = "owned-reference-probe-queue"
    os.environ["REGISTRY_CELERY_BEAT_WORKER_QUEUE"] = "owned-reference-probe-queue"
    celery_app = importlib.import_module(app_module + ".main").celery_app
    before = dict(celery_app.conf.beat_schedule or {})
    # Exactly the CELERY_IMPORTS path used by unmodified run_celery.py.
    extra = ["openg2p_registry_extensions.farmer_reference.worker", "openg2p_registry_extensions.tasks"]
    celery_app.conf.update(include=list(celery_app.conf.include or []) + extra)
    celery_app.loader.import_default_modules()
    assert set(before) <= set(celery_app.conf.beat_schedule)
    assert "coss.farmer_reference.deliver_workflows" in celery_app.tasks
    assert celery_app.conf.beat_schedule["coss-farmer-reference-workflows"]["options"]["queue"] == "owned-reference-probe-queue"
    assert celery_app.tasks["coss.farmer_reference.deliver_workflows"].queue == "owned-reference-probe-queue"
    tasks = importlib.import_module("openg2p_registry_extensions.tasks")
    for name, frequency in tasks.DOMAIN_SCHEDULE.items():
        assert name in celery_app.tasks
        assert celery_app.conf.beat_schedule[name] == {"task":name, "schedule":frequency}
    assert all(celery_app.conf.beat_schedule[key] == value for key, value in before.items())

runtime = importlib.import_module("openg2p_registry_extensions.farmer_reference.runtime")
from openg2p_fastapi_common.context import component_registry
for _ in range(3):
    runtime.install()
mapping = {}
for base, extension in ((runtime.G2PIntakeFormDataService, runtime.ReferenceIntakeService),
                         (runtime.G2PRegisterChangeRequestService, runtime.ReferenceChangeRequestService),
                         (runtime.G2PChangeRequestWorkerService, runtime.ReferenceIngestionService),
                         (runtime.G2PAweWebhookService, runtime.ReferenceWebhookService)):
    assert isinstance(base.get_component(), extension)
    assert sum(isinstance(item, extension) for item in component_registry) == 1
    mapping[base.__name__] = type(base.get_component()).__name__
importlib.import_module("openg2p_registry_extensions.ingestion_pipeline.enricher_services")
extension_app = importlib.import_module("openg2p_registry_extensions.app")
assert {model.__tablename__ for model in extension_app.REFERENCE_MODELS} == {
    "coss_farmer_references", "coss_farmer_reference_requests",
    "coss_farmer_submission_dependencies", "coss_farmer_workflow_outbox",
    "coss_farmer_pending_lookups",
}
print(json.dumps({"domain": domain, "mode": mode, "registered_services": mapping,
                  "extension_tables_in_migration": len(extension_app.REFERENCE_MODELS), "native_extra_imports": "PASS",
                  "duplicate_components": False, "network_accessed": False,
                  "beat_entries": len(celery_app.conf.beat_schedule) if celery_app else None}))
