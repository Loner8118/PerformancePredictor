"""
config.py
---------
Central configuration for the PerformancePredictor application.
"""

from pathlib import Path


class Config:
    """Application configuration."""

    # Base project directory
    BASE_DIR = Path(__file__).resolve().parent

    # Workspace where GitHub repositories are cloned
    WORKSPACE_DIR = BASE_DIR / "workspace"

    # Docker configuration
    DOCKER_IMAGE_NAME = "performance-image"
    DOCKER_CONTAINER_NAME = "performance-container"

    # Flask UI configuration (for the PerformancePredictor tool itself)
    HOST = "127.0.0.1"
    PORT = 5000
    DEBUG = True

    # Ensure workspace exists
    WORKSPACE_DIR.mkdir(exist_ok=True)