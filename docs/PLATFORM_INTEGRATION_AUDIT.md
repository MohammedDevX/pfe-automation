# Platform Integration Audit (2026)

This document provides a technical audit of major job platforms and Applicant Tracking Systems (ATS) to determine the feasibility of automating job discovery, application submission, and candidate messaging.

## Platform Summary

| Platform | Discovery | Apply | Messaging | API | Candidate API | Browser automation | Human-in-loop | Recommended approach |
|----------|-----------|-------|-----------|-----|---------------|--------------------|----------------|-----------------------|
| **LinkedIn** | Public web/scraping | Partner ATS only | Partner only | Yes (Partner) | No | High Risk / Blocked | Required | Manual apply/message with tracking |
| **Indeed** | Aggregators / Scrapers | Partner ATS only | Partner only | Yes (Partner) | No | High Risk / Blocked | Required | Manual apply with tracking |
| **Welcome to the Jungle** | Unofficial APIs / Scraping | No | N/A | Employer only | No | Prohibited / Blocked | Required | Manual apply |
| **Glassdoor** | Scrapers | No | N/A | Enterprise only | No | Prohibited / Blocked | Required | Manual apply |
| **Moroccan Boards (Rekrute, etc.)** | Scraping | Internal APIs | Internal APIs | No | No | High Risk | Required | Manual apply |
| **Greenhouse (ATS)** | Public Job Board API | Public Job Board API | N/A | Yes | Yes (Public endpoint) | Feasible (if no CAPTCHA)| Optional | Automated API submission |
| **Lever (ATS)** | Public Postings API | Public Postings API | N/A | Yes | Yes (Public endpoint) | Feasible (if no CAPTCHA)| Optional | Automated API submission |

---

## Detailed Platform Analysis

### 1. LinkedIn
* **What is officially possible:** Enterprise partners can use "Apply Connect" or "Apply with LinkedIn" to sync ATS data.
* **What is not officially possible:** There is **no public API** for candidate job application submission or messaging.
* **Limitations / Risks:** Extreme anti-bot presence. Automating a personal LinkedIn account via Selenium/Playwright violates the Terms of Service and carries a high risk of permanent account bans.
* **Recommended implementation:** Treat LinkedIn as a read-only or manual-action platform. The system should generate the outreach message or application data, provide a direct URL, and require the candidate to manually paste and submit.

### 2. Indeed
* **What is officially possible:** Job sync and "Indeed Apply" are strictly restricted to official, vetted ATS partners.
* **What is not officially possible:** No public candidate API. The public Publisher API was deprecated in 2024.
* **Limitations / Risks:** Indeed aggressively blocks scrapers and bots. "Auto-apply" bots that puppet candidate accounts violate the ToS and face high ban rates.
* **Recommended implementation:** Manual human-in-the-loop application. The system can track the URL and status, but submission must be manual.

### 3. Welcome to the Jungle (WttJ)
* **What is officially possible:** Employer-side API for managing branding and recruitment data.
* **What is not officially possible:** No candidate-facing API for applications.
* **Limitations / Risks:** ToS prohibits bots and scrapers. The platform enforces anti-bot measures on its application forms.
* **Recommended implementation:** Manual application via the platform's Application Tracker.

### 4. Glassdoor
* **What is officially possible:** Enterprise-level partnerships for data access.
* **What is not officially possible:** No public API for general users or candidates.
* **Limitations / Risks:** Aggressive bot protection (CAPTCHAs, JS rendering, rate-limiting). 
* **Recommended implementation:** Manual application.

### 5. Moroccan Platforms (e.g., Rekrute, m-job)
* **What is officially possible:** Very limited official APIs.
* **Limitations / Risks:** Applying generally requires session management and reverse-engineering internal, undocumented endpoints. While anti-bot measures may be less sophisticated than LinkedIn, automation still violates ToS.
* **Recommended implementation:** Manual application.

### 6. Company Career Pages / ATS (Greenhouse & Lever)
* **What is officially possible:** 
  * **Greenhouse:** Provides a public Job Board API (`POST /v1/boards/{board_token}/jobs/{job_id}`) that accepts `multipart/form-data` for resumes and candidate details.
  * **Lever:** Provides a Postings API (`POST /v0/postings/:site/:postingId`) for custom career sites to submit applications.
* **What is not officially possible:** Bypassing CAPTCHAs if the employer has explicitly enabled them on their specific board.
* **Limitations / Risks:** Low risk. These endpoints are designed to accept applications programmatically from custom career sites.
* **Recommended implementation:** Direct API integration. We can map the candidate's profile to the ATS payload and submit it automatically.

---

## Proposed Architecture

To safely accommodate these varying levels of automation, the core application system must be decoupled from specific platforms using a Provider pattern.

### Interfaces

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ApplicationResult:
    success: bool
    requires_manual_action: bool
    action_url: str | None = None
    error_message: str | None = None

class JobSourceProvider(ABC):
    """Handles discovering jobs or extracting job details from a given URL."""
    @abstractmethod
    def extract_job_details(self, url: str) -> dict:
        pass

class ApplicationProvider(ABC):
    """Handles the application submission process."""
    @property
    @abstractmethod
    def platform_name(self) -> str:
        pass

    @abstractmethod
    def can_auto_apply(self, job_url: str) -> bool:
        """Returns True if the platform supports automated API submission without CAPTCHA/ToS violations."""
        pass

    @abstractmethod
    def apply(self, job_url: str, candidate_profile: dict) -> ApplicationResult:
        """Attempts to apply, or returns a manual action requirement."""
        pass

class MessagingProvider(ABC):
    """Handles candidate-to-recruiter messaging (already partially implemented)."""
    @abstractmethod
    def can_auto_send(self) -> bool:
        pass
        
    @abstractmethod
    def send(self, recipient: str, message_body: str) -> bool:
        pass
```

### Example Implementation Flow

*   **GreenhouseProvider:** `can_auto_apply` returns `True`. The `apply()` method constructs a multipart payload and POSTs to the Greenhouse API.
*   **LinkedInProvider:** `can_auto_apply` returns `False`. The `apply()` method returns `requires_manual_action=True` and provides the exact job URL for the candidate to click.
*   **LinkedInMessagingProvider:** `can_auto_send` returns `False`. The system generates the message and provides a deep link or copy-to-clipboard functionality for the candidate to manually send via their browser.

---

## Conclusion & Next Steps

1. **Which integration to implement FIRST:** **Greenhouse / Lever (ATS)**.
   * **Why:** These ATS platforms provide documented, public-facing endpoints designed to accept applications programmatically. They offer the highest chance of successful automation without violating ToS or risking account bans. 
2. **Estimated Implementation Complexity:**
   * **Greenhouse/Lever Apply (ATS):** Medium (Requires mapping profile fields to API requirements and handling multipart file uploads for CVs).
   * **LinkedIn/Indeed Apply:** Low (We only implement the manual fallback flow, avoiding actual browser automation).
   * **Discovery/Scraping (All platforms):** High (Maintaining scrapers against anti-bot defenses is difficult. We should rely on manual URL ingestion or third-party aggregators where possible).

**Crucial Safety Rule:** We will *not* implement Selenium/Playwright automation for authenticated candidate accounts on LinkedIn or Indeed. The system will act as an orchestrator, automating what is officially permitted and elegantly handing off manual tasks to the user for everything else.
