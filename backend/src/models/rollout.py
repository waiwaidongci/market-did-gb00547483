from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class ActivationVersion(Base):
    """卸货预约启用排期的一个不可变版本。

    每次提交排期（含更正）都生成新版本；旧版本永久保留，用于追溯历史运行和
    「原统一启用记录照常可查」。当前生效版本由 :attr:`is_active` 标记。
    """

    __tablename__ = 'activation_versions'
    __table_args__ = (UniqueConstraint('dataset_id', 'version'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    version: Mapped[int]
    name: Mapped[str] = mapped_column(String(120))
    # [{"market_id": ..., "activate_date": "YYYY-MM-DD"}, ...]
    entries: Mapped[list] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(default=False, index=True)
    supersedes_id: Mapped[int | None] = mapped_column(default=None)
    note: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
