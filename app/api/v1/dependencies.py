from typing import Callable, List
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.security import decode_access_token
from app.core.firebase import firestore_users
from app.core.enums import UserRole
from app.schemas.user import UserOut

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme)) -> UserOut:
    """
    FastAPI dependency extracting and decoding the JWT bearer token,
    verifying user existence and active status.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials or token expired",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token_data = decode_access_token(token)
    if token_data is None or token_data.user_id is None:
        raise credentials_exception

    user_document = firestore_users.get_document(str(token_data.user_id))
    if user_document is None:
        raise credentials_exception

    if not user_document.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account"
        )

    return UserOut(**user_document)


def require_roles(allowed_roles: List[UserRole]) -> Callable:
    """
    Role-Based Access Control (RBAC) dependency factory.
    Restricts access to users matching the allowed roles.
    """
    def role_checker(current_user: UserOut = Depends(get_current_user)) -> UserOut:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Action prohibited for role '{current_user.role.value}'. Allowed roles: {[r.value for r in allowed_roles]}"
            )
        return current_user

    return role_checker


# Role guards
require_admin = require_roles([UserRole.ADMIN])
require_teacher = require_roles([UserRole.TEACHER, UserRole.ADMIN])
require_student = require_roles([UserRole.STUDENT, UserRole.ADMIN])
require_any_authenticated = require_roles([UserRole.ADMIN, UserRole.TEACHER, UserRole.STUDENT])
