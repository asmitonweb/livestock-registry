# The Master Data -> ODK Central location sync, run as a Kubernetes CronJob.
# Build from this directory:  docker build -t odk-geo-sync tools/odk-geo-sync
FROM python:3.12-slim
RUN pip install --no-cache-dir "httpx>=0.27,<1"
COPY odk_geo_sync.py /app/odk_geo_sync.py
USER 65534
ENTRYPOINT ["python", "/app/odk_geo_sync.py"]
