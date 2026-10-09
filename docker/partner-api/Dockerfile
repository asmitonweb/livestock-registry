# Livestock Registry — Partner API image.
#
# Thin extension of the registry-platform base image (see
# docker/staff-api/Dockerfile for the full rationale). Adds only the
# livestock domain model; ENV defaults and the migrate+gunicorn CMD are inherited.
#
# Build context = repo root (so livestock-extension/ is reachable).
ARG RP_VERSION=1.2.1
FROM openg2p/openg2p-registry-partner-api:${RP_VERSION}

# Option C: select livestock's domain model (coexists with the base image's
# reference extension; env picks livestock, no uninstall needed).
ENV REGISTRY_EXTENSION_MODULE=openg2p_registry_livestock_extension

COPY livestock-extension/ /app/livestock-extension/
RUN pip install --no-cache-dir /app/livestock-extension

# Same core patches as staff-api and celery. The extension's app.py imports
# the two models these patches add (G2PAttributeValueSchedule,
# G2PAttributeValueSpeciesConfig) at module level, and partner-api loads that
# app.py for its Initializer — without the patches the process dies at boot
# with ImportError. The script skips its one staff-api-only patch when that
# package is not installed, so it is safe to reuse here as-is.
COPY docker/staff-api/core-patches/apply_patches.py /tmp/apply_patches.py
RUN python3 /tmp/apply_patches.py && rm /tmp/apply_patches.py
