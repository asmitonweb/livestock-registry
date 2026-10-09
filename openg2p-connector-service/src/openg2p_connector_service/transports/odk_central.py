"""ODK Central REST poller transport.

Reads ``base_url`` and auth from per-connector configuration via the
auth strategy layer. Supports pagination (``max_pages`` in
``source_config``) and exponential backoff on transient errors.

Incremental polling
-------------------
The default incremental strategy uses
``__system/submissionDate`` (verified server-side support: ``$filter``
and ``$orderby`` both return 200 on the live ODK server, while
``$filter`` / ``$orderby`` against ``__id`` return 501). The transport
emits a structured :class:`~..checkpoints.Checkpoint` per record,
mode ``TIMESTAMP``, with ``boundary_ids`` carrying the submission ids
seen at the latest watermark so equal-timestamp ties don't get
re-processed on the next poll.

Configuration knobs (``source_config_json``)
--------------------------------------------
``incremental_mode``
    ``"timestamp"`` (default), ``"updated_at"``, ``"sequence"``, or
    ``"full_scan"``. ``sequence`` is the legacy ``__id``-based mode
    kept for servers that do support it; ``full_scan`` opts out of
    incremental entirely.
``incremental_field``
    Override the OData field used for filter/order. Defaults to
    ``__system/submissionDate`` for ``timestamp`` mode and
    ``__system/updatedAt`` for ``updated_at`` mode.
``strict_incremental``
    When true (the global default per
    ``CONNECTOR_STRICT_INCREMENTAL``), a ``501`` from the configured
    incremental field is treated as a hard error rather than silently
    falling back to a full scan. Set this per-connector to ``false``
    to allow the legacy fallback for low-volume / dev forms.
``http_timeout_seconds``
    OData client timeout in seconds (default: ``CONNECTOR_ODK_HTTP_TIMEOUT_SECONDS``,
    typically 60). Clamped to 5–300. Raise this if ODK Central is slow and you see
    ``ReadTimeout`` on large forms.
"""

import asyncio
import base64
import logging
import mimetypes
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from ..auth import get_auth_strategy
from ..config import get_settings
from .. import metrics as connector_metrics
from ..ingest_log import log_event
from ..models import ConnectorDefinition
from .base import BaseTransport, SourceRecord
from .checkpoints import (
    CHECKPOINT_STATE_KEY,
    Checkpoint,
    CheckpointMode,
    TransportCapability,
)
from .registry import register_transport

_logger = logging.getLogger("connector.transport.odk")


def _cfg_bool(cfg: dict[str, Any], key: str, *, default: bool) -> bool:
    """Parse booleans from JSON (bool) or legacy string configs.

    ``bool("false")`` in Python is True — never use bare ``bool(str)``.
    """
    if key not in cfg:
        return default
    v = cfg[key]
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _cfg_nav_link_filter(cfg: dict[str, Any]) -> set[str] | None:
    """Optional whitelist of nav-link names to resolve per submission.

    When ``resolve_nav_links`` is true and this is set, only navigation
    link entries whose name matches one of these is fetched.
    """
    raw = cfg.get("nav_link_names") or cfg.get("expand_paths")
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = {p.strip() for p in raw.split(",") if p and p.strip()}
        return parts or None
    if isinstance(raw, (list, tuple)):
        parts = {str(p).strip() for p in raw if str(p).strip()}
        return parts or None
    return None


# Default OData field per incremental mode. Verified live on the
# configured ODK server (https://odk.mowsa.openg2p.org):
#   - __system/submissionDate -> $filter + $orderby return 200
#   - __system/updatedAt      -> Edm.DateTimeOffset (works similarly)
#   - __id                    -> $filter + $orderby return 501.5
_DEFAULT_FIELD_FOR_MODE = {
    "timestamp": "__system/submissionDate",
    "updated_at": "__system/updatedAt",
    "sequence": "__id",
}


class StrictIncrementalUnsupported(RuntimeError):
    """Raised when the configured incremental field is not supported and
    the connector / global config disallow falling back to a full scan."""


@register_transport("odk_central")
class OdkCentralTransport(BaseTransport):
    capability = TransportCapability(
        modes=(
            CheckpointMode.TIMESTAMP,
            CheckpointMode.SEQUENCE,
            CheckpointMode.FULL_SCAN,
        ),
        default_mode=CheckpointMode.TIMESTAMP,
        supports_strict_incremental=True,
        notes=(
            "ODK Central OData. Verified: $filter/$orderby on "
            "__system/submissionDate returns 200; on __id returns 501."
        ),
    )

    async def fetch(
        self, connector: ConnectorDefinition
    ) -> AsyncIterator[SourceRecord]:
        cfg = connector.get_source_config()
        project_id = cfg.get("project_id")
        form_id = cfg.get("form_id")
        if not project_id or not form_id:
            raise ValueError(
                "ODK connector requires source_config_json with project_id and form_id"
            )

        base = self._resolve_base_url(cfg)

        auth_strategy = get_auth_strategy(connector.auth_type)
        auth_ctx = await auth_strategy.get_auth_context(connector)
        headers = dict(auth_ctx.headers)
        if auth_ctx.base_url:
            base = auth_ctx.base_url.rstrip("/")

        page_size = cfg.get("page_size", 100)
        max_pages = cfg.get("max_pages", 50)
        use_draft = _cfg_bool(cfg, "use_draft", default=False)
        draft_fallback = _cfg_bool(cfg, "draft_odata_fallback_on_404", default=True)
        use_draft_effective = use_draft
        # ODK Central rejects $expand combined with $filter/$orderby on
        # __system/submissionDate (returns 501). Repeats are exposed as
        # OData navigation links on each entry; when resolve_nav_links is
        # set, we GET each *@odata.navigationLink per submission and
        # merge the results back into the parent entry.
        resolve_nav_links = _cfg_bool(cfg, "resolve_nav_links", default=False)
        nav_link_filter = _cfg_nav_link_filter(cfg)
        nav_link_max_depth = int(cfg.get("nav_link_max_depth", 4))
        # OData carries only an attachment's file name (a photo, a scanned
        # certificate); the bytes are a separate download. With
        # embed_attachments set, each submission's attachments are fetched
        # and inlined where their name appears, as the
        # {"__type": "File", "name", "type", "data": <base64>} value the
        # registry's file fields accept. On by default so existing pipelines
        # pick it up without a config change; set false to skip the extra
        # calls (each file is held in memory, capped by attachment_max_bytes).
        embed_attachments = _cfg_bool(cfg, "embed_attachments", default=True)
        attachment_max_bytes = int(cfg.get("attachment_max_bytes", 10 * 1024 * 1024))

        mode_str = str(cfg.get("incremental_mode") or "timestamp").strip().lower()
        if mode_str not in ("timestamp", "updated_at", "sequence", "full_scan"):
            _logger.warning(
                "Unknown incremental_mode=%r on connector %s; defaulting to 'timestamp'",
                mode_str, connector.connector_id,
            )
            mode_str = "timestamp"

        # Strict mode policy: enterprise default is "fail closed". A
        # per-connector override wins, otherwise the global setting
        # (CONNECTOR_STRICT_INCREMENTAL) decides.
        settings = get_settings()
        strict = _cfg_bool(
            cfg, "strict_incremental", default=settings.strict_incremental
        )
        allow_full_scan_fallback = _cfg_bool(
            cfg, "full_scan_on_incremental_unsupported",
            default=settings.full_scan_on_incremental_unsupported,
        )

        prev_state = connector.get_poll_state()
        prev_checkpoint = Checkpoint.from_dict(prev_state.get(CHECKPOINT_STATE_KEY))

        # ---- assemble incremental query parameters ----
        incremental_field: str | None = None
        filter_expr: str | None = None
        orderby_expr: str | None = None

        if mode_str == "full_scan":
            _logger.info(
                "ODK connector %s configured for FULL_SCAN; no incremental filter applied.",
                connector.connector_id,
            )
        else:
            incremental_field = (
                cfg.get("incremental_field")
                or _DEFAULT_FIELD_FOR_MODE[mode_str]
            )
            orderby_expr = f"{incremental_field} asc"
            if mode_str in ("timestamp", "updated_at"):
                # Boundary-safe replay: filter with `ge` against the
                # watermark and dedupe ties locally via boundary_ids.
                if (
                    prev_checkpoint
                    and prev_checkpoint.mode is CheckpointMode.TIMESTAMP
                    and prev_checkpoint.value
                ):
                    filter_expr = f"{incremental_field} ge {prev_checkpoint.value}"
            else:  # sequence
                if (
                    prev_checkpoint
                    and prev_checkpoint.mode is CheckpointMode.SEQUENCE
                    and prev_checkpoint.value
                ):
                    filter_expr = f"{incremental_field} gt '{prev_checkpoint.value}'"
                # Also honor the legacy poll_state key for backwards compat
                # so existing connectors don't lose their cursor on upgrade.
                elif cfg.get("last_submission_id"):
                    filter_expr = f"{incremental_field} gt '{cfg['last_submission_id']}'"

        global_timeout = float(get_settings().odk_http_timeout_seconds)
        poll_timeout = float(cfg.get("http_timeout_seconds", global_timeout))
        poll_timeout = max(5.0, min(poll_timeout, 300.0))

        async with httpx.AsyncClient(timeout=poll_timeout) as client:
            skip = 0
            for _page in range(max_pages):
                url = self._odata_submissions_url(
                    base, project_id, form_id, use_draft=use_draft_effective
                )
                params: dict[str, Any] = {
                    "$top": page_size,
                    "$skip": skip,
                }
                if orderby_expr:
                    params["$orderby"] = orderby_expr
                if filter_expr:
                    params["$filter"] = filter_expr

                ok = []
                if use_draft_effective and draft_fallback:
                    ok.append(404)
                if incremental_field is not None:
                    # Always tolerate 501 long enough to apply the
                    # strict/opt-in policy below, then either raise or
                    # downgrade to full-scan with a loud warning.
                    ok.append(501)

                resp = await self._request_with_backoff(
                    client, url, headers, params, ok_if=tuple(ok)
                )
                if resp.status_code == 404 and use_draft_effective and draft_fallback:
                    _logger.warning(
                        "ODK draft OData returned 404 (%s). The form is likely "
                        "published now; retrying published OData. Set "
                        "use_draft=false in source_config_json to avoid this.",
                        url,
                    )
                    use_draft_effective = False
                    url = self._odata_submissions_url(
                        base, project_id, form_id, use_draft=False
                    )
                    resp = await self._request_with_backoff(
                        client, url, headers, params, ok_if=tuple(ok)
                    )

                if resp.status_code == 501 and incremental_field is not None:
                    self._handle_incremental_unsupported(
                        connector_id=connector.connector_id,
                        url=url,
                        field=incremental_field,
                        strict=strict,
                        allow_full_scan_fallback=allow_full_scan_fallback,
                    )
                    connector_metrics.poll_incremental_fallback_total.labels(
                        connector_id=connector.connector_id,
                        transport="odk_central",
                        reason="server_501",
                    ).inc()
                    # Fallback path: drop incremental query and re-issue.
                    incremental_field = None
                    filter_expr = None
                    orderby_expr = None
                    params.pop("$filter", None)
                    params.pop("$orderby", None)
                    resp = await self._request_with_backoff(
                        client, url, headers, params
                    )

                body = resp.json()
                entries = body.get("value", [])

                # ODK nav links are relative to the .svc/ service root,
                # which we already constructed for the listing URL. Strip
                # the trailing /Submissions to get the root for expansion.
                service_root = url
                if service_root.endswith("/Submissions"):
                    service_root = service_root[: -len("/Submissions")]

                for entry in entries:
                    instance_id = entry.get("__id", "")
                    if resolve_nav_links:
                        await self._resolve_navigation_links(
                            client=client,
                            service_root=service_root,
                            headers=headers,
                            entry=entry,
                            connector_id=connector.connector_id,
                            allowed_names=nav_link_filter,
                            max_depth=nav_link_max_depth,
                        )
                    record_checkpoint = self._build_record_checkpoint(
                        mode_str=mode_str,
                        entry=entry,
                        instance_id=instance_id,
                        incremental_field=incremental_field,
                    )
                    # Boundary dedupe: at exactly the watermark, ignore
                    # ids we already processed last poll.
                    if (
                        record_checkpoint is not None
                        and record_checkpoint.mode is CheckpointMode.TIMESTAMP
                        and prev_checkpoint is not None
                        and prev_checkpoint.mode is CheckpointMode.TIMESTAMP
                        and str(record_checkpoint.value)
                        == str(prev_checkpoint.value)
                        and instance_id in prev_checkpoint.boundary_ids
                    ):
                        connector_metrics.poll_boundary_replay_skipped_total.labels(
                            connector_id=connector.connector_id,
                            transport="odk_central",
                        ).inc()
                        continue

                    if embed_attachments and instance_id:
                        await self._embed_attachments(
                            client=client,
                            base=base,
                            project_id=project_id,
                            form_id=form_id,
                            instance_id=instance_id,
                            headers=headers,
                            entry=entry,
                            use_draft=use_draft_effective,
                            max_bytes=attachment_max_bytes,
                        )
                    elif instance_id:
                        log_event(
                            "attachments", "attachments_not_embedded",
                            source_event_id=f"{form_id}:{instance_id}",
                            reason="embed_attachments is off; files go as names only",
                        )

                    yield SourceRecord(
                        source_event_id=f"{form_id}:{instance_id}",
                        data=entry,
                        cursor_key="last_submission_id",
                        cursor_value=instance_id or None,
                        checkpoint=record_checkpoint,
                    )

                if len(entries) < page_size:
                    break
                skip += page_size

    @classmethod
    async def _resolve_navigation_links(
        cls,
        *,
        client: httpx.AsyncClient,
        service_root: str,
        headers: dict,
        entry: dict[str, Any],
        connector_id: str,
        allowed_names: set[str] | None,
        max_depth: int,
    ) -> None:
        """Walk an ODK OData entry and dereference repeat navigation links.

        ODK Central exposes repeat groups as separate OData entity sets and
        decorates each parent with ``"<name>@odata.navigationLink"`` whose
        value is relative to the form's ``.svc/`` service root. This
        helper GETs every such link, replaces ``entry[<name>]`` with the
        materialized array, then recurses into nested repeats. Failures
        are logged but do not abort the poll — a partially expanded row
        is more useful than dropping it entirely.
        """
        if max_depth <= 0:
            return
        await cls._resolve_nav_links_in_obj(
            client=client,
            service_root=service_root,
            headers=headers,
            obj=entry,
            connector_id=connector_id,
            allowed_names=allowed_names,
            depth=max_depth,
        )

    @classmethod
    async def _resolve_nav_links_in_obj(
        cls,
        *,
        client: httpx.AsyncClient,
        service_root: str,
        headers: dict,
        obj: Any,
        connector_id: str,
        allowed_names: set[str] | None,
        depth: int,
    ) -> None:
        if depth <= 0:
            return
        if isinstance(obj, dict):
            link_keys = [k for k in list(obj.keys()) if k.endswith("@odata.navigationLink")]
            for link_key in link_keys:
                base_key = link_key[: -len("@odata.navigationLink")]
                if allowed_names is not None and base_key not in allowed_names:
                    continue
                rel = obj.get(link_key)
                if not isinstance(rel, str) or not rel:
                    continue
                try:
                    materialized = await cls._fetch_nav_link(
                        client=client,
                        service_root=service_root,
                        headers=headers,
                        link=rel,
                    )
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "ODK nav link expansion failed for %r on connector %s: %s",
                        rel, connector_id, exc,
                    )
                    continue
                obj[base_key] = materialized
                await cls._resolve_nav_links_in_obj(
                    client=client,
                    service_root=service_root,
                    headers=headers,
                    obj=materialized,
                    connector_id=connector_id,
                    allowed_names=allowed_names,
                    depth=depth - 1,
                )
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    await cls._resolve_nav_links_in_obj(
                        client=client,
                        service_root=service_root,
                        headers=headers,
                        obj=v,
                        connector_id=connector_id,
                        allowed_names=allowed_names,
                        depth=depth - 1,
                    )
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    await cls._resolve_nav_links_in_obj(
                        client=client,
                        service_root=service_root,
                        headers=headers,
                        obj=item,
                        connector_id=connector_id,
                        allowed_names=allowed_names,
                        depth=depth - 1,
                    )

    @staticmethod
    async def _fetch_nav_link(
        *,
        client: httpx.AsyncClient,
        service_root: str,
        headers: dict,
        link: str,
    ) -> list[dict[str, Any]]:
        """GET an OData navigation link and return its ``value`` array.

        ``link`` may be absolute (rare) or relative to the form's OData
        service root (``.../forms/<form>.svc``). Pagination is followed
        via ``@odata.nextLink``; empty / missing payloads return ``[]``.
        """
        items: list[dict[str, Any]] = []
        next_url: str | None = link
        root = service_root.rstrip("/")
        while next_url:
            if next_url.lower().startswith(("http://", "https://")):
                url = next_url
            else:
                url = f"{root}/{next_url.lstrip('/')}"
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            body = resp.json()
            chunk = body.get("value") or []
            if isinstance(chunk, list):
                items.extend(chunk)
            next_url = body.get("@odata.nextLink")
        return items

    @staticmethod
    def _build_record_checkpoint(
        *,
        mode_str: str,
        entry: dict[str, Any],
        instance_id: str,
        incremental_field: str | None,
    ) -> Checkpoint | None:
        """Map an ODK entry to a structured Checkpoint."""
        if mode_str == "full_scan" or incremental_field is None:
            return None
        if mode_str in ("timestamp", "updated_at"):
            sys_block = entry.get("__system") or {}
            field_name = (
                "submissionDate" if mode_str == "timestamp" else "updatedAt"
            )
            ts = sys_block.get(field_name)
            if ts is None:
                return None
            return Checkpoint(
                mode=CheckpointMode.TIMESTAMP,
                value=ts,
                boundary_ids=[instance_id] if instance_id else [],
                extra={"field": incremental_field},
            )
        if mode_str == "sequence":
            return Checkpoint(
                mode=CheckpointMode.SEQUENCE,
                value=instance_id or None,
                extra={"field": incremental_field},
            )
        return None

    @staticmethod
    def _handle_incremental_unsupported(
        *,
        connector_id: str,
        url: str,
        field: str,
        strict: bool,
        allow_full_scan_fallback: bool,
    ) -> None:
        """Apply the strict / opt-in fallback policy uniformly."""
        if strict and not allow_full_scan_fallback:
            raise StrictIncrementalUnsupported(
                f"ODK server at {url} does not support incremental "
                f"$filter/$orderby on {field!r}. Connector "
                f"{connector_id!r} is in strict_incremental=True mode "
                "and full_scan_on_incremental_unsupported=False, so "
                "falling back to a full scan is not allowed. Choose a "
                "supported incremental_field (e.g. "
                "__system/submissionDate) or explicitly opt into "
                "full-scan mode."
            )
        _logger.error(
            "ODK %s returned 501 for incremental field=%r; falling back to "
            "FULL SCAN. This re-downloads every submission and is "
            "expensive — set incremental_mode=timestamp + "
            "incremental_field=__system/submissionDate to use server-side "
            "filtering. (connector_id=%s)",
            url, field, connector_id,
        )

    @staticmethod
    def _odata_submissions_url(
        base: str, project_id: Any, form_id: str, *, use_draft: bool
    ) -> str:
        """OData Submissions list URL for a form (draft vs published)."""
        if use_draft:
            return (
                f"{base}/v1/projects/{project_id}/forms/{form_id}/draft.svc/Submissions"
            )
        return f"{base}/v1/projects/{project_id}/forms/{form_id}.svc/Submissions"

    @classmethod
    async def _embed_attachments(
        cls,
        *,
        client: httpx.AsyncClient,
        base: str,
        project_id: Any,
        form_id: str,
        instance_id: str,
        headers: dict,
        entry: dict,
        use_draft: bool,
        max_bytes: int,
    ) -> None:
        """Replace each attachment's file name in *entry* with its content.

        Lists the submission's attachments, downloads the ones Central holds
        (``exists``), and swaps every string equal to an attachment's name,
        at any depth (repeat groups included), for the inline file value. An
        attachment that is missing, larger than *max_bytes* or fails to
        download keeps its file name, with a warning: the record is still
        sent, without that file.
        """
        root = f"{base}/v1/projects/{project_id}/forms/{quote(str(form_id), safe='')}"
        if use_draft:
            root += "/draft"
        listing_url = f"{root}/submissions/{quote(instance_id, safe='')}/attachments"
        # source_event_id is the id the connector run and the registry ingest use,
        # so these events join with theirs.
        ids = {"source_event_id": f"{form_id}:{instance_id}", "form_id": form_id}
        try:
            resp = await cls._request_with_backoff(client, listing_url, headers, {})
            attachments = resp.json() or []
        except (httpx.HTTPError, ValueError) as exc:
            _logger.warning("ODK attachments listing failed for %s: %s", instance_id, exc)
            log_event(
                "attachments", "attachment_listing_failed", "WARNING", **ids,
                error=f"{type(exc).__name__}: {exc}",
                http_status=getattr(getattr(exc, "response", None), "status_code", None),
                outcome="sent_without_attachments",
            )
            return

        files: dict[str, dict] = {}
        counts = {"listed": len(attachments), "embedded": 0, "not_uploaded": 0,
                  "too_large": 0, "download_failed": 0}
        for attachment in attachments:
            name = attachment.get("name")
            if not name:
                continue
            if not attachment.get("exists", True):
                counts["not_uploaded"] += 1
                log_event(
                    "attachments", "attachment_not_uploaded", "WARNING", **ids,
                    file_name=name,
                    reason="listed by ODK Central but the file was never uploaded",
                    outcome="sent_with_file_name_only",
                )
                continue
            url = f"{listing_url}/{quote(name, safe='')}"
            try:
                download = await cls._request_with_backoff(client, url, headers, {})
            except httpx.HTTPError as exc:
                _logger.warning("ODK attachment %s of %s not downloaded: %s", name, instance_id, exc)
                counts["download_failed"] += 1
                log_event(
                    "attachments", "attachment_download_failed", "WARNING", **ids,
                    file_name=name, error=f"{type(exc).__name__}: {exc}",
                    http_status=getattr(getattr(exc, "response", None), "status_code", None),
                    outcome="sent_with_file_name_only",
                )
                continue
            content = download.content
            if len(content) > max_bytes:
                _logger.warning(
                    "ODK attachment %s of %s is %d bytes, over attachment_max_bytes=%d; "
                    "sent without it",
                    name, instance_id, len(content), max_bytes,
                )
                counts["too_large"] += 1
                log_event(
                    "attachments", "attachment_too_large", "WARNING", **ids,
                    file_name=name, size_bytes=len(content), limit_bytes=max_bytes,
                    outcome="sent_with_file_name_only",
                )
                continue
            content_type = (
                (download.headers.get("content-type") or "").split(";")[0].strip()
                or mimetypes.guess_type(name)[0]
                or "application/octet-stream"
            )
            files[name] = {
                "__type": "File",
                "name": name,
                "type": content_type,
                "data": base64.b64encode(content).decode("ascii"),
            }
            counts["embedded"] += 1
            log_event(
                "attachments", "attachment_embedded", **ids,
                file_name=name, size_bytes=len(content), mime_type=content_type,
            )

        if files:
            cls._replace_file_names(entry, files)

        problems = counts["not_uploaded"] + counts["too_large"] + counts["download_failed"]
        log_event(
            "attachments", "attachments_summary", "WARNING" if problems else "INFO",
            **ids, **counts,
        )

    @classmethod
    def _replace_file_names(cls, obj: Any, files: dict[str, dict]) -> Any:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and value in files:
                    obj[key] = dict(files[value])
                else:
                    cls._replace_file_names(value, files)
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                if isinstance(value, str) and value in files:
                    obj[index] = dict(files[value])
                else:
                    cls._replace_file_names(value, files)
        return obj

    @staticmethod
    async def _request_with_backoff(
        client: httpx.AsyncClient,
        url: str,
        headers: dict,
        params: dict,
        *,
        max_retries: int = 3,
        ok_if: tuple[int, ...] = (),
    ) -> httpx.Response:
        """GET with exponential backoff on 429/5xx and Retry-After support."""
        backoff = 1.0
        for attempt in range(max_retries + 1):
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code in ok_if:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == max_retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                _logger.warning(
                    "ODK %s returned %d, retrying in %.1fs (attempt %d/%d)",
                    url, resp.status_code, wait, attempt + 1, max_retries,
                )
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue
            resp.raise_for_status()
            return resp
        return resp  # unreachable but satisfies type checker

    @staticmethod
    def _resolve_base_url(cfg: dict) -> str:
        base = cfg.get("base_url", "").rstrip("/")
        if not base:
            settings = get_settings()
            base = settings.odk_central_base_url.rstrip("/")
            if base:
                _logger.warning(
                    "Using deprecated CONNECTOR_ODK_CENTRAL_BASE_URL; "
                    "migrate to source_config_json.base_url"
                )
        if not base:
            raise ValueError("No base_url in source_config_json or env")
        return base
