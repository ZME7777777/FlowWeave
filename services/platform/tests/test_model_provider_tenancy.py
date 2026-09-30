from __future__ import annotations

import pytest
from sqlalchemy import select

from flowweave.modules.model_providers.infrastructure.models import ModelProvider, ProviderModel
from flowweave.modules.users.application.security import (
    FLOWWEAVE_USER_ID,
    USER_USER_ID,
    tenant_user,
)


def test_model_providers_and_models_are_user_isolated(db_session_factory):
    with db_session_factory() as db:
        with tenant_user(FLOWWEAVE_USER_ID):
            provider = ModelProvider(
                name="admin-provider",
                base_url="https://models.example.com",
                auth_type="API_KEY",
            )
            db.add(provider)
            db.flush()
            model = ProviderModel(
                provider_id=provider.id,
                model_name="admin-model",
                enabled=True,
                is_default=True,
            )
            db.add(model)
            db.commit()

        with tenant_user(USER_USER_ID):
            assert db.scalars(select(ModelProvider)).all() == []
            assert db.scalars(select(ProviderModel)).all() == []
            with pytest.raises(RuntimeError, match="Cross-user record creation"):
                db.add(
                    ModelProvider(
                        name="forbidden-provider",
                        base_url="https://models.example.com",
                        auth_type="API_KEY",
                        owner_user_id=FLOWWEAVE_USER_ID,
                    )
                )
                db.flush()
            db.rollback()

        with tenant_user(FLOWWEAVE_USER_ID):
            assert [item.id for item in db.scalars(select(ModelProvider))] == [provider.id]
            assert [item.id for item in db.scalars(select(ProviderModel))] == [model.id]
