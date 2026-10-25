"""Extension-owned correlation/outbox tables; native CR and intake remain owners."""
from datetime import datetime

from sqlalchemy import DateTime, BigInteger, CheckConstraint, ForeignKey, JSON, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from openg2p_fastapi_common.models import BaseORMModel


class FarmerReference(BaseORMModel):
    __tablename__ = "coss_farmer_references"
    __table_args__ = (
        UniqueConstraint("source_system", "source_record_id"),
        UniqueConstraint("source_system", "farmer_id"),
        UniqueConstraint("source_system", "uid"),
        CheckConstraint("applied_version >= 0"),
    )
    reference_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    source_record_id: Mapped[str] = mapped_column(String, nullable=False)
    farmer_id: Mapped[str] = mapped_column(String, nullable=False)
    uid: Mapped[str] = mapped_column(String, nullable=False)
    local_farmer_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    applied_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    pending_add_request_id: Mapped[str | None] = mapped_column(String, nullable=True)


class FarmerReferenceRequest(BaseORMModel):
    __tablename__ = "coss_farmer_reference_requests"
    __table_args__ = (UniqueConstraint("reference_id", "source_version", "action"),)
    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    reference_id: Mapped[str] = mapped_column(ForeignKey("coss_farmer_references.reference_id"), nullable=False, index=True)
    native_cr_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    source_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    profile: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_digest: Mapped[str] = mapped_column(String, nullable=False)


class FarmerSubmissionDependency(BaseORMModel):
    __tablename__ = "coss_farmer_submission_dependencies"
    submission_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    reference_id: Mapped[str] = mapped_column(ForeignKey("coss_farmer_references.reference_id"), nullable=False, index=True)
    farmer_request_id: Mapped[str | None] = mapped_column(ForeignKey("coss_farmer_reference_requests.request_id"), nullable=True)
    identity_binding: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class FarmerWorkflowOutbox(BaseORMModel):
    __tablename__ = "coss_farmer_workflow_outbox"
    native_cr_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("coss_farmer_reference_requests.request_id"), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)


class FarmerPendingLookup(BaseORMModel):
    __tablename__ = "coss_farmer_pending_lookups"
    submission_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    identity_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)


REFERENCE_MODELS = (FarmerReference, FarmerReferenceRequest, FarmerSubmissionDependency, FarmerWorkflowOutbox, FarmerPendingLookup)
