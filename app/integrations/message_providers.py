"""LLM provider abstraction for outreach message generation.

Supported providers:
  - ``template`` — Python f-string template, no API key required (default/fallback).
  - ``openai``   — OpenAI Chat Completions API via httpx; requires OPENAI_API_KEY.

Add new providers by subclassing ``MessageProvider`` and registering them in
``build_message_provider``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from app.database import Settings


# ---------------------------------------------------------------------------
# Context object passed to every provider
# ---------------------------------------------------------------------------


@dataclass
class MessageContext:
    """All factual data available for personalising a message."""

    # Candidate
    candidate_name: str
    candidate_degree: str
    candidate_school: str | None
    candidate_specialization: str | None
    candidate_email: str | None
    candidate_cv_summary: str | None

    # Job
    job_title: str
    job_description: str | None
    job_url: str
    job_location: str | None

    # Company
    company_name: str
    company_description: str | None
    company_website: str | None

    # Contact (optional)
    recruiter_name: str | None
    recruiter_title: str | None
    recruiter_email: str | None

    # Channel
    channel: str  # "email" | "linkedin"

    # Follow-up context (optional)
    follow_up_count: int = 0          # 0 = initial outreach, 1+ = follow-up
    previous_body: str | None = None  # body of the most recent sent message

    # Language & Bilingual summaries (optional)
    language: str = "fr"              # "fr" | "en"
    candidate_cv_summary_fr: str | None = None
    candidate_cv_summary_en: str | None = None


# ---------------------------------------------------------------------------
# Generated message returned by every provider
# ---------------------------------------------------------------------------


@dataclass
class GeneratedMessage:
    subject: str | None  # None for LinkedIn (no subject line)
    body: str
    provider: str
    model: str | None = None


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class MessageProvider(ABC):
    name: str

    @abstractmethod
    async def generate(self, ctx: MessageContext) -> GeneratedMessage:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Template provider — no API key, deterministic output
# ---------------------------------------------------------------------------


_GREETING_TEMPLATES = {
    "email": "Madame, Monsieur,",
    "linkedin": "Bonjour,",
}


class TemplateMessageProvider(MessageProvider):
    """Generates a factual, concise PFE outreach message from a Python template.

    No external API is called. Safe to use in tests and as a fallback when
    no LLM key is configured.
    Supports French (default) and English message generation.
    """

    name = "template"

    async def generate(self, ctx: MessageContext) -> GeneratedMessage:
        if ctx.language == "en":
            greeting = (
                f"Dear {ctx.recruiter_name},"
                if ctx.recruiter_name
                else ("Dear Hiring Team," if ctx.channel == "email" else "Hello,")
            )
            school_line = f" at {ctx.candidate_school}" if ctx.candidate_school else ""
            spec_line = (
                f", specializing in {ctx.candidate_specialization}"
                if ctx.candidate_specialization
                else ""
            )
            location_line = (
                f" based in {ctx.job_location}" if ctx.job_location else ""
            )
            summary = ctx.candidate_cv_summary_en or ctx.candidate_cv_summary
            cv_line = f"\n\n{summary}" if summary else ""

            if ctx.follow_up_count > 0:
                body = (
                    f"{greeting}\n\n"
                    f"I am following up on my application for the "
                    f"final-year internship (PFE) as **{ctx.job_title}** at **{ctx.company_name}**. "
                    f"I remain very enthusiastic about the opportunity to discuss my profile with you.\n\n"
                    f"Best regards,\n{ctx.candidate_name}"
                )
            else:
                body = (
                    f"{greeting}\n\n"
                    f"I am writing to express my interest in a final-year internship (PFE) "
                    f"for the position of **{ctx.job_title}**{location_line} at "
                    f"**{ctx.company_name}**.\n\n"
                    f"As a student in {ctx.candidate_degree}{school_line}{spec_line}, "
                    f"I am seeking a graduation internship to apply and grow my technical "
                    f"skills in a professional environment."
                    f"{cv_line}\n\n"
                    f"Would you be available for a brief conversation to discuss this "
                    f"opportunity? I remain at your disposal to provide my resume and "
                    f"cover letter.\n\n"
                    f"Best regards,\n{ctx.candidate_name}"
                    + (f"\n{ctx.candidate_email}" if ctx.candidate_email else "")
                )

            subject: str | None = None
            if ctx.channel == "email":
                prefix = "Follow-up: " if ctx.follow_up_count > 0 else ""
                subject = (
                    f"{prefix}Internship Application – {ctx.job_title} – {ctx.candidate_name}"
                )

            return GeneratedMessage(subject=subject, body=body, provider=self.name)

        greeting = (
            f"Bonjour {ctx.recruiter_name},"
            if ctx.recruiter_name
            else _GREETING_TEMPLATES.get(ctx.channel, "Bonjour,")
        )
        school_line = f" à {ctx.candidate_school}" if ctx.candidate_school else ""
        spec_line = (
            f", spécialisation {ctx.candidate_specialization}"
            if ctx.candidate_specialization
            else ""
        )
        location_line = (
            f" basé(e) à {ctx.job_location}" if ctx.job_location else ""
        )
        summary = ctx.candidate_cv_summary_fr or ctx.candidate_cv_summary
        cv_line = (
            f"\n\n{summary}" if summary else ""
        )

        if ctx.follow_up_count > 0:
            body = (
                f"{greeting}\n\n"
                f"Je me permets de revenir vers vous concernant ma candidature pour "
                f"le stage PFE de **{ctx.job_title}** chez **{ctx.company_name}**. "
                f"Je reste très enthousiaste à l'idée d'échanger avec vous.\n\n"
                f"Cordialement,\n{ctx.candidate_name}"
            )
        else:
            body = (
                f"{greeting}\n\n"
                f"Je me permets de vous contacter au sujet d'un stage PFE (fin d'études) "
                f"pour le poste de **{ctx.job_title}**{location_line} au sein de "
                f"**{ctx.company_name}**.\n\n"
                f"Étudiant(e) en {ctx.candidate_degree}{school_line}{spec_line}, "
                f"je suis à la recherche d'un stage de fin d'études qui me permettra de mettre "
                f"en pratique mes compétences techniques dans un environnement professionnel."
                f"{cv_line}\n\n"
                f"Seriez-vous disponible pour un court échange afin de discuter de cette "
                f"opportunité ? Je reste bien entendu à votre disposition pour vous faire "
                f"parvenir mon CV et lettre de motivation.\n\n"
                f"Cordialement,\n{ctx.candidate_name}"
                + (f"\n{ctx.candidate_email}" if ctx.candidate_email else "")
            )

        subject: str | None = None
        if ctx.channel == "email":
            prefix = "Relance : " if ctx.follow_up_count > 0 else ""
            subject = (
                f"{prefix}Candidature Stage PFE – {ctx.job_title} – {ctx.candidate_name}"
            )

        return GeneratedMessage(subject=subject, body=body, provider=self.name)


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert career advisor helping a final-year engineering student write a \
personalized internship outreach message. You must only use the information provided — \
do not invent facts about the candidate, the company, the recruiter, or the role.

Rules:
- Be professional, concise, and genuine.
- Do not use clichés like "I am passionate about" or "dream job".
- Keep the email body under 200 words.
- Write in the requested language (French or English). If not specified, write in French unless the job description is clearly in English.
- For email output format:
    SUBJECT: <subject line>
    BODY:
    <email body>
- For LinkedIn output: only the message body (no subject line).
"""


def _build_openai_user_prompt(ctx: MessageContext) -> str:
    lang_label = "English" if ctx.language == "en" else "French"
    parts = [
        f"Language: {lang_label}",
        f"Channel: {ctx.channel}",
        f"Candidate: {ctx.candidate_name}, {ctx.candidate_degree}"
        + (f" at {ctx.candidate_school}" if ctx.candidate_school else "")
        + (f", specialization: {ctx.candidate_specialization}" if ctx.candidate_specialization else ""),
        f"Target company: {ctx.company_name}"
        + (f" ({ctx.company_website})" if ctx.company_website else ""),
        f"Position: {ctx.job_title}"
        + (f" – {ctx.job_location}" if ctx.job_location else ""),
    ]
    if ctx.company_description:
        parts.append(f"Company description: {ctx.company_description[:300]}")
    if ctx.job_description:
        parts.append(f"Job description excerpt:\n{ctx.job_description[:500]}")
    if ctx.recruiter_name:
        parts.append(
            f"Recruiter: {ctx.recruiter_name}"
            + (f" ({ctx.recruiter_title})" if ctx.recruiter_title else "")
        )
    cv_summary = (
        ctx.candidate_cv_summary_en if ctx.language == "en" else ctx.candidate_cv_summary_fr
    ) or ctx.candidate_cv_summary
    if cv_summary:
        parts.append(f"Candidate summary: {cv_summary[:400]}")
    return "\n".join(parts)


def _parse_openai_email_response(text: str) -> tuple[str | None, str]:
    """Extract SUBJECT / BODY from the LLM output for email channel."""
    subject: str | None = None
    body_lines: list[str] = []
    in_body = False
    for line in text.splitlines():
        if line.upper().startswith("SUBJECT:") and not in_body:
            subject = line.split(":", 1)[1].strip()
        elif line.upper().strip() == "BODY:":
            in_body = True
        elif in_body:
            body_lines.append(line)
    body = "\n".join(body_lines).strip() if body_lines else text.strip()
    return subject, body


class OpenAIMessageProvider(MessageProvider):
    """Calls OpenAI Chat Completions via httpx. Requires OPENAI_API_KEY in settings."""

    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def generate(self, ctx: MessageContext) -> GeneratedMessage:
        if not self.settings.openai_api_key:
            raise RuntimeError(
                "OpenAI message generation requires OPENAI_API_KEY. "
                "Set the environment variable or use provider='template'."
            )
        user_prompt = _build_openai_user_prompt(ctx)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.openai_model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.65,
                    "max_tokens": 700,
                },
            )
            response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"].strip()
        model_used = response.json().get("model", self.settings.openai_model)

        if ctx.channel == "email":
            subject, body = _parse_openai_email_response(content)
        else:
            subject, body = None, content

        return GeneratedMessage(
            subject=subject,
            body=body,
            provider=self.name,
            model=model_used,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_message_provider(provider_name: str, settings: Settings) -> MessageProvider:
    """Return the appropriate provider by name."""
    if provider_name == "openai":
        return OpenAIMessageProvider(settings)
    if provider_name == "template":
        return TemplateMessageProvider()
    raise ValueError(
        f"Unknown message provider '{provider_name}'. Choose 'openai' or 'template'."
    )
