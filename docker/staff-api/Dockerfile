# Livestock Registry — Staff Portal API image.
#
# Under the inverted build model the registry-platform publishes the base image
# (platform code + fastapi-common/iam-core runtime + entrypoint/CMD/ENV). This
# image only adds the livestock domain model on top: the livestock-extension is
# pip-installed under its own import name (openg2p_registry_livestock_extension)
# and selected at runtime by REGISTRY_EXTENSION_MODULE (Option C). ENV defaults
# and the migrate+gunicorn CMD are inherited from the base.
#
# RP_VERSION pins the openg2p-registry base image tag. Keep it in lockstep with
# the openg2p-registry chart dependency pinned in helm/openg2p-livestock-registry/Chart.yaml.
#
# Build context = repo root (so livestock-extension/ is reachable).
ARG RP_VERSION=1.2.1
FROM openg2p/openg2p-registry-staff-api:${RP_VERSION}

# Option C: select livestock's domain model (coexists with the base image's
# reference extension; env picks livestock, no uninstall needed).
ENV REGISTRY_EXTENSION_MODULE=openg2p_registry_livestock_extension

COPY livestock-extension/ /app/livestock-extension/
RUN pip install --no-cache-dir /app/livestock-extension

# Local fixes to the base image's installed openg2p_registry_core, pending a
# proper upstream release of the registry-platform base image. Baked in here
# (rather than patched on the running container) so they survive every
# rebuild — see core-patches/apply_patches.py for exactly what each one does
# and why it's a targeted find/replace rather than a whole-file swap.
COPY docker/staff-api/core-patches/apply_patches.py /tmp/apply_patches.py
RUN python3 /tmp/apply_patches.py && rm /tmp/apply_patches.py
