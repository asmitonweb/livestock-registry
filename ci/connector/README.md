# ODK connector for the livestock registry

The connector polls ODK Central and posts each submission to the registry's
Partner API, which queues it for the celery workers to transform and save as an
intake. See `docs/odk/README.md` for the form, the photos and the ingestion logs.

| Piece | Where |
| --- | --- |
| Connector service and UI | `openg2p-connector-service/`, `openg2p-connector-ui/` |
| Helm chart | `openg2p-connector-service/deploy/charts/openg2p-connector` |
| Values for dev (`live`) | `ci/connector/values-live.yaml` |
| Deploy | Jenkins job from `ci/connector/Jenkinsfile`, which runs `ci/connector/deploy.sh` |

## Deploying

The registry pipeline builds the `connector-service` and `connector-ui` images
on every build, with the build's tag, but never deploys them: the connector is
its own helm release (`livestock-connector`), and a merged connector change is not live
until this step runs for a build that has it. Check what runs with

```sh
kubectl -n live get deploy livestock-connector-worker -o jsonpath='{..image}'
```

Run the standalone Jenkins job set up from `ci/connector/Jenkinsfile` ("Pipeline
script from SCM", no triggers): **Build with Parameters**, TAG the build's tag,
CONFIRM ticked. DRY_RUN (ticked by default) renders the release and lists its
images; untick it to apply. By hand, with a kubeconfig:

```sh
DRY_RUN=1 ENVIRONMENT=live ./ci/connector/deploy.sh <image tag>
ENVIRONMENT=live ./ci/connector/deploy.sh <image tag>
```

`deploy.sh` keeps the release's own values (the pipelines and ODK logins live in
the connector's database and are edited in the connector UI) and changes only
the image tags. It also applies `connector-api-alias.yaml`, the service name the
UI image's nginx expects. Roll back with `helm rollback livestock-connector <revision> -n live`.

## A fresh environment

1. Deploy as above. The chart creates the `livestock_connector` database and its user.
2. Register the partner the connector posts as, in the `master_data` database:

   ```sql
   insert into g2p_partners (partner_id, partner_mnemonic, keymanager_reference_id, is_active)
   values ('livestock-partner', 'livestock-partner', 'livestock-partner-key-ref', true)
   on conflict (partner_id) do nothing;
   ```

3. Publish the form in `docs/odk/` on ODK Central with their media.
4. Create the pipeline in the connector UI (form `livestock_registry`, data model `MY_DATA_MODEL`, partner header `livestock-partner`, `resolve_nav_links` and `embed_attachments` on). db-seed already
   loads the registry side (`zz_livestock_odk_ingestion.sql`: data model `MY_DATA_MODEL`,
   its routing and the transform's catalogue row).
