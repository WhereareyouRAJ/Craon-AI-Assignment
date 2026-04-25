"""
config.py — Central place for all settings.
Reads values from your .env file automatically.
"""
 
from pydantic_settings import BaseSettings
 
 
class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "jobqueue"
    app_env: str = "development"
 
    class Config:
        env_file = ".env"          # reads from .env file in project root
        env_file_encoding = "utf-8"
 
 
# Single shared instance used across the whole app
settings = Settings()