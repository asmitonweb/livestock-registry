"""Files that arrive inside a record instead of as a document id.

The ODK connector downloads a submission's photos from ODK Central and sends
them inline, as {"__type": "File", "name", "type", "data": <base64>}; the
intake form's 'file' widget embeds a picked file the same way. Saved as-is, that
blob would land in a text column. persist_embedded_files() uploads each one to
the documents bucket, through the same checks /documents/upload_documents
applies, and puts the resulting document_id in its place.

Every stored file, and every refused one with the reason, is written to the
ODK ingestion log (ingest_log.py), so a photo that did not make it can be found.
The same helpers serve the farmer registry (its domain_validation_utils).
"""

import base64
import io
import uuid

from openg2p_fastapi_common.context import dbengine
from openg2p_registry_core.config import Settings
from openg2p_registry_core.helpers.document import get_document_handler
from openg2p_registry_core.helpers.file_validation import validate_file_bytes
from openg2p_registry_core.helpers.file_validation_profiles import get_upload_validation_profile
from openg2p_registry_core.models import G2PRegistryDocument
from openg2p_registry_core.models.enum import DocumentBucket
from sqlalchemy.ext.asyncio import async_sessionmaker

from ...ingest_log import log_event


def is_embedded_file(value) -> bool:
    """True for the inline {"__type": "File", "data": "<base64>", ...} shape."""
    return isinstance(value, dict) and value.get("__type") == "File"


async def persist_embedded_files(record: dict, fields: tuple[str, ...], purpose: str) -> None:
    """Replace each embedded file in *fields* of *record* with its document_id.

    A plain value (an existing document_id, None) is left alone. A file the
    store refuses fails the save, as an upload from the form would."""
    for field in fields:
        value = record.get(field)
        if is_embedded_file(value):
            record[field] = await upload_embedded_file(
                value, record.get("created_by"), purpose=f"{purpose}.{field}"
            )


async def upload_embedded_file(value: dict, created_by, purpose: str = "file") -> str:
    """Upload an embedded file's bytes and return the new document_id.

    *purpose* only labels the ingestion log entry."""
    filename = value.get("name") or "upload"
    try:
        document_id = await _upload(value, created_by, filename)
    except Exception as error:
        log_event(
            "files", "file_rejected", "ERROR",
            purpose=purpose, file_name=filename, mime_type=value.get("type"),
            size_bytes=_decoded_size(value), error_type=type(error).__name__,
            error=str(error), outcome="save_refused",
        )
        raise
    log_event(
        "files", "file_stored", purpose=purpose, file_name=filename,
        mime_type=value.get("type"), size_bytes=_decoded_size(value),
        document_id=document_id,
    )
    return document_id


def _decoded_size(value: dict) -> int:
    data = value.get("data") or ""
    return max(0, len(data) * 3 // 4 - data[-2:].count("="))


async def _upload(value: dict, created_by, filename: str) -> str:
    try:
        content = base64.b64decode(value.get("data") or "", validate=True)
    except Exception as error:
        raise ValueError("uploaded file could not be decoded") from error

    content_type = value.get("type") or "application/octet-stream"
    profile = get_upload_validation_profile(DocumentBucket.DOCUMENTS, Settings.get_config(strict=False))
    if profile is not None:
        content_type = validate_file_bytes(content, profile, filename=filename).mime_type

    document_store_id = get_document_handler().upload(
        data=io.BytesIO(content),
        length=len(content),
        bucket=DocumentBucket.DOCUMENTS,
        content_type=content_type,
    )

    session_maker = async_sessionmaker(dbengine.get(), expire_on_commit=False)
    async with session_maker() as session:
        document_row = G2PRegistryDocument(
            document_id=str(uuid.uuid4()),
            document_store_id=document_store_id,
            bucket=DocumentBucket.DOCUMENTS,
            source_filename=filename,
            created_by=str(created_by or "system"),
        )
        session.add(document_row)
        await session.commit()
        return document_row.document_id
