import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Centralized Settings Manager powered by Pydantic's BaseSettings.
    Reads, parses, and validates settings from environment variables and `.env` file.
    """
    PROJECT_NAME: str = "LMS Backend API"
    API_V1_STR: str = "/api/v1"
    DEBUG: bool = True

    # Security & JWT Authentication settings
    SECRET_KEY: str = "lms_super_secret_development_key_change_in_production_32bytes"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours duration

    # Database Configuration Settings
    DATABASE_PROVIDER: str = "FIREBASE_FIRESTORE"  # Primary database provider
    DATABASE_URL: str = "sqlite:///./lms.db"       # Secondary local relational fallback

    # Google Cloud & Firebase Integration Settings
    GCP_PROJECT_ID: str = "lms-demo-project"
    GCP_BUCKET_NAME: str = "lms-study-materials-bucket"
    GOOGLE_APPLICATION_CREDENTIALS: str = "./service_account.json"
    FIREBASE_CREDENTIALS_PATH: str = "./firebase_credentials.json"
    USE_FIREBASE_DB: bool = True
    USE_LOCAL_STORAGE: bool = False
    LOCAL_STORAGE_DIR: str = "./uploads"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


# Single shared settings instance across the application
settings = Settings()
