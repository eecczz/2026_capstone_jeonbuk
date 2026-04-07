import logging
import time
from typing import Optional
import uuid

from sqlalchemy.orm import Session
from open_webui.internal.db import Base, get_db

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Boolean, Column, String, Text, Integer, JSON

log = logging.getLogger(__name__)

####################
# CrawlTarget DB Schema
####################


class CrawlTarget(Base):
    __tablename__ = "crawl_target"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text)  # who registered this target

    label = Column(Text)  # e.g. "전북특별자치도청", "농업기술원"
    url = Column(Text, unique=True)  # base URL to crawl
    description = Column(Text, nullable=True)

    # crawl settings
    max_depth = Column(Integer, default=2)  # link follow depth
    crawl_interval_hours = Column(Integer, default=24)  # how often to crawl
    is_active = Column(Boolean, default=True)

    # status tracking
    last_crawl_at = Column(BigInteger, nullable=True)  # last successful crawl timestamp
    last_crawl_status = Column(Text, nullable=True)  # "success", "failed", "in_progress"
    last_crawl_page_count = Column(Integer, nullable=True)  # pages crawled last time
    collection_name = Column(Text, nullable=True)  # vector DB collection name

    meta = Column(JSON, nullable=True)  # extra metadata

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


####################
# Pydantic Models
####################


class CrawlTargetModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    label: str
    url: str
    description: Optional[str] = None
    max_depth: int = 2
    crawl_interval_hours: int = 24
    is_active: bool = True
    last_crawl_at: Optional[int] = None
    last_crawl_status: Optional[str] = None
    last_crawl_page_count: Optional[int] = None
    collection_name: Optional[str] = None
    meta: Optional[dict] = None
    created_at: int
    updated_at: int


class CrawlTargetForm(BaseModel):
    label: str
    url: str
    description: Optional[str] = None
    max_depth: int = 2
    crawl_interval_hours: int = 24
    is_active: bool = True


class CrawlTargetUpdateForm(BaseModel):
    label: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    max_depth: Optional[int] = None
    crawl_interval_hours: Optional[int] = None
    is_active: Optional[bool] = None


####################
# Data Access Class
####################


class CrawlTargets:

    @staticmethod
    def insert_new_target(
        user_id: str,
        form_data: CrawlTargetForm,
    ) -> Optional[CrawlTargetModel]:
        with get_db() as db:
            now = int(time.time())
            target_id = str(uuid.uuid4())
            collection_name = f"crawl-{target_id}"

            target = CrawlTarget(
                id=target_id,
                user_id=user_id,
                label=form_data.label,
                url=form_data.url.rstrip("/"),
                description=form_data.description,
                max_depth=form_data.max_depth,
                crawl_interval_hours=form_data.crawl_interval_hours,
                is_active=form_data.is_active,
                collection_name=collection_name,
                created_at=now,
                updated_at=now,
            )
            db.add(target)
            db.commit()
            db.refresh(target)
            return CrawlTargetModel.model_validate(target)

    @staticmethod
    def get_targets() -> list[CrawlTargetModel]:
        with get_db() as db:
            targets = db.query(CrawlTarget).order_by(CrawlTarget.created_at.desc()).all()
            return [CrawlTargetModel.model_validate(t) for t in targets]

    @staticmethod
    def get_active_targets() -> list[CrawlTargetModel]:
        with get_db() as db:
            targets = (
                db.query(CrawlTarget)
                .filter(CrawlTarget.is_active == True)
                .order_by(CrawlTarget.created_at.desc())
                .all()
            )
            return [CrawlTargetModel.model_validate(t) for t in targets]

    @staticmethod
    def get_target_by_id(target_id: str) -> Optional[CrawlTargetModel]:
        with get_db() as db:
            target = db.query(CrawlTarget).filter(CrawlTarget.id == target_id).first()
            if target:
                return CrawlTargetModel.model_validate(target)
            return None

    @staticmethod
    def update_target_by_id(
        target_id: str, form_data: CrawlTargetUpdateForm
    ) -> Optional[CrawlTargetModel]:
        with get_db() as db:
            target = db.query(CrawlTarget).filter(CrawlTarget.id == target_id).first()
            if not target:
                return None

            update_data = form_data.model_dump(exclude_none=True)
            if "url" in update_data:
                update_data["url"] = update_data["url"].rstrip("/")
            update_data["updated_at"] = int(time.time())

            for key, value in update_data.items():
                setattr(target, key, value)

            db.commit()
            db.refresh(target)
            return CrawlTargetModel.model_validate(target)

    @staticmethod
    def update_crawl_status(
        target_id: str,
        status: str,
        page_count: Optional[int] = None,
    ) -> Optional[CrawlTargetModel]:
        with get_db() as db:
            target = db.query(CrawlTarget).filter(CrawlTarget.id == target_id).first()
            if not target:
                return None

            target.last_crawl_status = status
            target.updated_at = int(time.time())

            if status == "success":
                target.last_crawl_at = int(time.time())
            if page_count is not None:
                target.last_crawl_page_count = page_count

            db.commit()
            db.refresh(target)
            return CrawlTargetModel.model_validate(target)

    @staticmethod
    def delete_target_by_id(target_id: str) -> bool:
        with get_db() as db:
            target = db.query(CrawlTarget).filter(CrawlTarget.id == target_id).first()
            if not target:
                return False
            db.delete(target)
            db.commit()
            return True

    @staticmethod
    def get_targets_due_for_crawl() -> list[CrawlTargetModel]:
        """Get active targets that are due for their next crawl."""
        with get_db() as db:
            now = int(time.time())
            targets = (
                db.query(CrawlTarget)
                .filter(CrawlTarget.is_active == True)
                .all()
            )
            due_targets = []
            for t in targets:
                if t.last_crawl_at is None:
                    due_targets.append(CrawlTargetModel.model_validate(t))
                elif (now - t.last_crawl_at) >= (t.crawl_interval_hours * 3600):
                    due_targets.append(CrawlTargetModel.model_validate(t))
            return due_targets
