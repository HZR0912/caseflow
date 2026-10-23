"""从服务端配置解析身份；客户端仅持有不透明凭据。"""

from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from core.policy.opa_client import PolicySubject
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apps.api.settings import Settings, get_settings

bearer = HTTPBearer(auto_error=False)


def get_current_subject(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PolicySubject:
    if credentials is not None:
        digest = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
        for identity in settings.auth_credentials:
            if secrets.compare_digest(digest, identity.token_sha256):
                return PolicySubject(
                    id=identity.user_id, role=identity.role, tenant=identity.tenant
                )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid_or_missing_credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


SubjectDep = Annotated[PolicySubject, Depends(get_current_subject)]
