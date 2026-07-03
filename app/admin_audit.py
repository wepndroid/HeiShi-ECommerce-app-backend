"""Admin audit log helper."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import AdminAuditLog


def log_admin_action(
    db: Session,
    *,
    admin_id: str,
    action_type: str,
    target_type: str,
    target_id: str,
    before: Any = None,
    after: Any = None,
) -> None:
    db.add(
        AdminAuditLog(
            admin_id=admin_id,
            action_type=action_type,
            target_type=target_type,
            target_id=str(target_id),
            before_json=json.dumps(before) if before is not None else None,
            after_json=json.dumps(after) if after is not None else None,
        )
    )
