from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.security import hash_password, verify_password, create_access_token
from app.core.enums import UserRole
from app.core.firebase import firestore_users
from app.schemas.auth import Token, LoginRequest
from app.schemas.user import UserCreate, UserOut
from app.api.v1.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register_user(user_in: UserCreate):
    """
    Register a new user (Student, Teacher, or Admin) and sync to Firebase.
    """
    existing_user = firestore_users.get_document_by_field("email", user_in.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email already exists"
        )

    user_id = firestore_users.get_next_numeric_id()
    user_data = {
        "full_name": user_in.full_name,
        "email": user_in.email,
        "hashed_password": hash_password(user_in.password),
        "role": user_in.role.value,
        "is_active": True,
        "created_at": datetime.utcnow().isoformat()
    }
    stored_user = firestore_users.add_document(str(user_id), user_data)
    stored_user["id"] = user_id
    return UserOut(**stored_user)


@router.post("/login", response_model=Token)
def login(login_data: LoginRequest):
    """
    Authenticate user using Email and Password.
    Returns JWT access token, user role, and user profile metadata upon success.
    """
    user_document = firestore_users.get_document_by_field("email", login_data.email)
    if not user_document or not verify_password(login_data.password, user_document.get("hashed_password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )

    if not user_document.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account"
        )

    role = UserRole(user_document["role"])
    user_id = int(user_document["id"])
    access_token = create_access_token(
        subject=user_id,
        role=role,
        email=user_document["email"]
    )

    return Token(
        access_token=access_token,
        token_type="bearer",
        role=role,
        user_id=user_id,
        full_name=user_document["full_name"]
    )


@router.post("/token", response_model=Token, include_in_schema=False)
def login_form(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    OAuth2 form login handler compatible with Swagger UI documentation authentication modal.
    """
    user_document = firestore_users.get_document_by_field("email", form_data.username)
    if not user_document or not verify_password(form_data.password, user_document.get("hashed_password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )

    role = UserRole(user_document["role"])
    user_id = int(user_document["id"])
    access_token = create_access_token(
        subject=user_id,
        role=role,
        email=user_document["email"]
    )

    return Token(
        access_token=access_token,
        token_type="bearer",
        role=role,
        user_id=user_id,
        full_name=user_document["full_name"]
    )


@router.get("/me", response_model=UserOut)
def get_current_user_profile(current_user: UserOut = Depends(get_current_user)):
    """
    Get details of the currently logged in user profile.
    """
    return current_user
