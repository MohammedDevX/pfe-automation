"""CV (resume) file validation utilities.

These helpers validate a CV file path before ATS submission.
They do NOT upload, parse, or transmit the file.
"""
from __future__ import annotations

import os
from pathlib import Path

# Maximum file size we accept for CV upload (10 MB)
MAX_CV_SIZE_BYTES = 10 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".pdf", ".doc", ".docx"}


class CVValidationError(ValueError):
    """Clear, user-facing error when CV validation fails."""


def validate_cv(cv_path: str | None) -> Path:
    """Validate a CV path and return a resolved Path.

    Raises CVValidationError with a human-readable message on any failure.
    Never logs the path contents or any credentials.
    """
    if not cv_path:
        raise CVValidationError(
            "Cannot prepare application: no CV path is configured.\n"
            "Set CANDIDATE_CV_PATH in your .env file to the absolute path of your CV file."
        )

    path = Path(cv_path)

    if not path.is_absolute():
        raise CVValidationError(
            f"Cannot prepare application: CV path must be absolute, got a relative path.\n"
            f"Current value starts with: '{str(path)[:40]}...'\n"
            "Set CANDIDATE_CV_PATH to the full absolute path of your CV file."
        )

    if not path.exists():
        raise CVValidationError(
            f"Cannot prepare application: CV file not found.\n"
            f"Expected file at: {path}\n"
            "Make sure the file exists and CANDIDATE_CV_PATH points to it."
        )

    if not path.is_file():
        raise CVValidationError(
            f"Cannot prepare application: CV path exists but is not a file (it may be a directory).\n"
            f"Path: {path}"
        )

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise CVValidationError(
            f"Cannot prepare application: unsupported CV file format '{ext}'.\n"
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    size = path.stat().st_size
    if size == 0:
        raise CVValidationError(
            f"Cannot prepare application: CV file is empty (0 bytes).\n"
            f"Path: {path}"
        )

    if size > MAX_CV_SIZE_BYTES:
        raise CVValidationError(
            f"Cannot prepare application: CV file is too large "
            f"({size // (1024 * 1024)} MB, limit is {MAX_CV_SIZE_BYTES // (1024 * 1024)} MB).\n"
            f"Please compress or re-export your CV."
        )

    return path
