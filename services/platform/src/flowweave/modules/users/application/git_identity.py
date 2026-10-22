"""Current authenticated user profile as a validated Git identity."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from flowweave.modules.users.application.security import FLOWWEAVE_USER_ID, current_user_id
from flowweave.modules.users.infrastructure.models import User


def profile_identity(db: Session) -> tuple[str, str] | None:
    user = db.get(User, current_user_id(default=FLOWWEAVE_USER_ID))
    if user is None or not isinstance(user.display_name, str) or not isinstance(user.email, str):
        return None
    name, email = user.display_name.strip(), user.email.strip()
    if not name or len(name) > 200 or re.search(r"[\x00-\x1f\x7f<>]", name):
        return None
    if len(email) > 320 or not re.fullmatch(
        r"[^\x00-\x1f\x7f\s@<>]+@[^\x00-\x1f\x7f\s@<>]+", email
    ):
        return None
    return name, email
