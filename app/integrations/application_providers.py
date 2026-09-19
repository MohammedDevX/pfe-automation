"""Application Providers and ATS Integration."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, HttpUrl


# ---------------------------------------------------------------------------
# Core Datastructures
# ---------------------------------------------------------------------------


class CandidateProfile(BaseModel):
    """Structured candidate profile for automated submission."""
    first_name: str
    last_name: str
    email: str
    phone: str
    location: str | None = None
    education: str | None = None
    degree: str | None = None
    specialization: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    cv_path: str | None = None
    cover_letter: str | None = None


@dataclass
class ApplicationQuestion:
    """A question required by the ATS."""
    id: str
    label: str
    required: bool
    type: str  # e.g., 'text', 'boolean', 'file', 'select'
    options: list[str] | None = None


@dataclass
class ApplicationPayload:
    """Prepared application payload, holding answers and missing fields."""
    provider_name: str
    is_ready: bool
    missing_fields: list[str]
    questions: list[ApplicationQuestion]
    answers: dict[str, Any]
    candidate: CandidateProfile


@dataclass
class ApplicationResult:
    """Result of an application submission attempt."""
    success: bool
    requires_manual_action: bool
    action_url: str | None = None
    external_id: str | None = None
    error_message: str | None = None
    raw_response: dict | None = None


# ---------------------------------------------------------------------------
# ATS Provider Abstraction
# ---------------------------------------------------------------------------


class ApplicationProvider(ABC):
    
    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Name of the platform (e.g., 'Greenhouse', 'Lever', 'LinkedIn')."""
        pass

    @abstractmethod
    def can_auto_apply(self, job_url: str) -> bool:
        """Return True if this URL can be safely and automatically applied to."""
        pass

    @abstractmethod
    def extract_job_id(self, job_url: str) -> str | None:
        """Extract the ATS internal job/board ID from the URL."""
        pass

    @abstractmethod
    async def prepare_application(self, job_url: str, candidate: CandidateProfile) -> ApplicationPayload:
        """Fetch questions and prepare the application payload.
        
        This should attempt to map candidate profile fields to ATS fields.
        """
        pass

    @abstractmethod
    async def submit_application(self, job_url: str, payload: ApplicationPayload) -> ApplicationResult:
        """Attempt to submit the application."""
        pass


# ---------------------------------------------------------------------------
# Manual Provider (Fallback for LinkedIn, Indeed, etc.)
# ---------------------------------------------------------------------------


class ManualApplicationProvider(ApplicationProvider):
    
    @property
    def platform_name(self) -> str:
        return "Manual"

    def can_auto_apply(self, job_url: str) -> bool:
        return False

    def extract_job_id(self, job_url: str) -> str | None:
        return None

    async def prepare_application(self, job_url: str, candidate: CandidateProfile) -> ApplicationPayload:
        is_notion = "notion.so" in (job_url or "").lower()
        missing = ["No external job listing URL available."] if is_notion else ["Requires manual human submission"]
        return ApplicationPayload(
            provider_name=self.platform_name,
            is_ready=False,
            missing_fields=missing,
            questions=[],
            answers={},
            candidate=candidate,
        )

    async def submit_application(self, job_url: str, payload: ApplicationPayload) -> ApplicationResult:
        is_notion = "notion.so" in (job_url or "").lower()
        msg = "No external job listing URL available." if is_notion else "This platform requires manual application submission."
        return ApplicationResult(
            success=False,
            requires_manual_action=True,
            action_url=None if is_notion else job_url,
            error_message=msg,
        )


# ---------------------------------------------------------------------------
# Greenhouse ATS
# ---------------------------------------------------------------------------


class GreenhouseApplicationProvider(ApplicationProvider):
    
    @property
    def platform_name(self) -> str:
        return "Greenhouse"

    def can_auto_apply(self, job_url: str) -> bool:
        # Greenhouse Job Board API is designed for programmatic apply, assuming no CAPTCHA.
        return True

    def extract_job_id(self, job_url: str) -> str | None:
        """E.g., https://boards.greenhouse.io/companyname/jobs/1234567"""
        match = re.search(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", job_url)
        if match:
            return f"{match.group(1)}:{match.group(2)}"
        return None

    async def prepare_application(self, job_url: str, candidate: CandidateProfile) -> ApplicationPayload:
        # In a real implementation, we would GET /v1/boards/{board}/jobs/{job_id} to fetch required fields.
        # For this prototype, we simulate standard Greenhouse fields.
        
        questions = [
            ApplicationQuestion(id="first_name", label="First Name", required=True, type="text"),
            ApplicationQuestion(id="last_name", label="Last Name", required=True, type="text"),
            ApplicationQuestion(id="email", label="Email", required=True, type="text"),
            ApplicationQuestion(id="resume", label="Resume/CV", required=True, type="file"),
        ]
        
        answers = {
            "first_name": candidate.first_name,
            "last_name": candidate.last_name,
            "email": candidate.email,
        }
        
        missing = []
        if not candidate.cv_path:
            missing.append("Resume/CV path is missing from candidate profile")
            
        return ApplicationPayload(
            provider_name=self.platform_name,
            is_ready=len(missing) == 0,
            missing_fields=missing,
            questions=questions,
            answers=answers,
            candidate=candidate,
        )

    async def submit_application(self, job_url: str, payload: ApplicationPayload) -> ApplicationResult:
        if not self.can_auto_apply(job_url):
            return ApplicationResult(success=False, requires_manual_action=True, action_url=job_url)
            
        if not payload.is_ready:
            return ApplicationResult(success=False, requires_manual_action=False, error_message="Payload not ready")
            
        # Mocking the actual POST request since we don't have real credentials in tests
        # We assume standard Greenhouse successful payload response
        return ApplicationResult(
            success=True,
            requires_manual_action=False,
            external_id=f"gh_{self.extract_job_id(job_url)}",
            raw_response={"status": "success"}
        )


# ---------------------------------------------------------------------------
# Lever ATS
# ---------------------------------------------------------------------------


class LeverApplicationProvider(ApplicationProvider):
    
    @property
    def platform_name(self) -> str:
        return "Lever"

    def can_auto_apply(self, job_url: str) -> bool:
        return True

    def extract_job_id(self, job_url: str) -> str | None:
        """E.g., https://jobs.lever.co/companyname/uuid"""
        match = re.search(r"jobs\.lever\.co/([^/]+)/([a-f0-9\-]+)", job_url)
        if match:
            return f"{match.group(1)}:{match.group(2)}"
        return None

    async def prepare_application(self, job_url: str, candidate: CandidateProfile) -> ApplicationPayload:
        questions = [
            ApplicationQuestion(id="name", label="Full Name", required=True, type="text"),
            ApplicationQuestion(id="email", label="Email", required=True, type="text"),
            ApplicationQuestion(id="resume", label="Resume/CV", required=True, type="file"),
        ]
        
        answers = {
            "name": f"{candidate.first_name} {candidate.last_name}",
            "email": candidate.email,
        }
        
        missing = []
        if not candidate.cv_path:
            missing.append("Resume/CV path is missing from candidate profile")
            
        return ApplicationPayload(
            provider_name=self.platform_name,
            is_ready=len(missing) == 0,
            missing_fields=missing,
            questions=questions,
            answers=answers,
            candidate=candidate,
        )

    async def submit_application(self, job_url: str, payload: ApplicationPayload) -> ApplicationResult:
        if not self.can_auto_apply(job_url):
            return ApplicationResult(success=False, requires_manual_action=True, action_url=job_url)
            
        if not payload.is_ready:
            return ApplicationResult(success=False, requires_manual_action=False, error_message="Payload not ready")
            
        return ApplicationResult(
            success=True,
            requires_manual_action=False,
            external_id=f"lever_{self.extract_job_id(job_url)}",
            raw_response={"status": "success"}
        )


# ---------------------------------------------------------------------------
# ATS Detector
# ---------------------------------------------------------------------------

def detect_ats_provider(url: str) -> ApplicationProvider:
    """Identify ATS based on URL and return appropriate provider."""
    if not url:
        return ManualApplicationProvider()
        
    url_lower = url.lower()
    
    if "boards.greenhouse.io" in url_lower:
        return GreenhouseApplicationProvider()
    if "jobs.lever.co" in url_lower:
        return LeverApplicationProvider()
        
    # Default to manual for unknown/LinkedIn/Indeed/WttJ etc.
    return ManualApplicationProvider()
