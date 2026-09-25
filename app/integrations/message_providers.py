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
    candidate_skills: str | None = None
    candidate_github_url: str | None = None
    candidate_linkedin_url: str | None = None
    candidate_portfolio_url: str | None = None

    # Job
    job_title: str = ""
    job_description: str | None = None
    job_url: str = ""
    job_location: str | None = None
    job_tech_signals: list[str] = field(default_factory=list)

    # Company
    company_name: str = ""
    company_description: str | None = None
    company_website: str | None = None
    company_industry: str | None = None
    company_tech_signals: list[str] = field(default_factory=list)

    # Contact (optional)
    recruiter_name: str | None = None
    recruiter_title: str | None = None
    recruiter_email: str | None = None
    contact_relevance: str = "LOW"  # "HIGH" | "MEDIUM" | "LOW"
    contact_source: str | None = None

    # Selected candidate project & tech evidence
    selected_project_name: str | None = None
    selected_project_description: str | None = None
    personalization_signals: list[str] = field(default_factory=list)

    # Channel
    channel: str = "email"  # "email" | "linkedin"

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


class TemplateMessageProvider(MessageProvider):
    """Generates a factual, highly personalized PFE outreach message from Python templates.

    No external API is called. Safe to use in tests and as a fallback when
    no LLM key is configured.
    Supports French (default) and English message generation with role-aware and
    project-evidence personalization.
    """

    name = "template"

    async def generate(self, ctx: MessageContext) -> GeneratedMessage:
        school_str = f" à {ctx.candidate_school}" if (ctx.candidate_school and ctx.language == "fr") else (f" at {ctx.candidate_school}" if ctx.candidate_school else "")
        spec_str = f" ({ctx.candidate_specialization})" if ctx.candidate_specialization else ""
        location_str = f" ({ctx.job_location})" if ctx.job_location else ""

        tech_signal_str = ", ".join(ctx.personalization_signals) if ctx.personalization_signals else ""

        # -------------------------------------------------------------------
        # LINKEDIN MESSAGES (~50-100 words)
        # -------------------------------------------------------------------
        if ctx.channel == "linkedin":
            if ctx.language == "en":
                greeting = f"Hello {ctx.recruiter_name}," if ctx.recruiter_name else "Hello,"
                project_clause = (
                    f" Having worked on **{ctx.selected_project_name}** using **{tech_signal_str}**,"
                    if ctx.selected_project_name and tech_signal_str
                    else ""
                )
                body = (
                    f"{greeting}\n\n"
                    f"I am a {ctx.candidate_degree} student{school_str}{spec_str} seeking a 6-month final-year internship (PFE) "
                    f"for the **{ctx.job_title}** position at **{ctx.company_name}**.{project_clause} "
                    f"I would welcome the opportunity to connect and briefly discuss how my background aligns with your team's goals.\n\n"
                    f"Best regards,\n{ctx.candidate_name}"
                )
            else:
                greeting = f"Bonjour {ctx.recruiter_name}," if ctx.recruiter_name else "Bonjour,"
                project_clause = (
                    f" Ayant développé **{ctx.selected_project_name}** avec **{tech_signal_str}**,"
                    if ctx.selected_project_name and tech_signal_str
                    else ""
                )
                body = (
                    f"{greeting}\n\n"
                    f"Étudiant en {ctx.candidate_degree}{school_str}{spec_str}, je suis à la recherche d'un stage PFE de 6 mois "
                    f"pour le poste de **{ctx.job_title}** chez **{ctx.company_name}**.{project_clause} "
                    f"Seriez-vous ouvert à un court échange pour discuter de mon profil et des opportunités au sein de votre équipe ?\n\n"
                    f"Cordialement,\n{ctx.candidate_name}"
                )
            return GeneratedMessage(subject=None, body=body, provider=self.name)

        # -------------------------------------------------------------------
        # EMAIL MESSAGES (~120-220 words)
        # -------------------------------------------------------------------
        summary_en = ctx.candidate_cv_summary_en or ctx.candidate_cv_summary
        summary_fr = ctx.candidate_cv_summary_fr or ctx.candidate_cv_summary

        if ctx.language == "en":
            greeting = (
                f"Dear {ctx.recruiter_name},"
                if ctx.recruiter_name
                else ("Dear Hiring Team," if ctx.contact_relevance != "HIGH" else "Dear Recruiter,")
            )
            prefix = "Follow-up: " if ctx.follow_up_count > 0 else ""
            subject = f"{prefix}Internship Application – {ctx.job_title} – {ctx.candidate_name}"


            if ctx.follow_up_count > 0:
                body = (
                    f"{greeting}\n\n"
                    f"I am following up on my application for the final-year internship (PFE) as **{ctx.job_title}** at **{ctx.company_name}**.\n\n"
                    f"I remain very enthusiastic about contributing to your engineering initiatives and would welcome any updates regarding my application.\n\n"
                    f"Best regards,\n{ctx.candidate_name}\n{ctx.candidate_email or ''}"
                )
            else:
                project_section = ""
                if ctx.selected_project_name and tech_signal_str:
                    project_section = (
                        f"In my project **{ctx.selected_project_name}** ({ctx.selected_project_description or 'technical project'}), "
                        f"I implemented architectures using **{tech_signal_str}**, which directly matches the technical environment of this role.\n\n"
                    )

                role_callout = (
                    "I would be glad to discuss technical challenges and present my engineering work in detail."
                    if ctx.contact_relevance == "MEDIUM"
                    else "I would welcome the opportunity for a brief call to discuss my application and availability."
                )

                summary_line = f"\n\n{summary_en}" if summary_en else ""

                links = []
                if ctx.candidate_github_url:
                    links.append(f"GitHub: {ctx.candidate_github_url}")
                if ctx.candidate_linkedin_url:
                    links.append(f"LinkedIn: {ctx.candidate_linkedin_url}")
                links_str = ("\n" + " | ".join(links)) if links else ""

                body = (
                    f"{greeting}\n\n"
                    f"I am writing to express my strong interest in a final-year internship (PFE) for the **{ctx.job_title}**{location_str} position at **{ctx.company_name}**.\n\n"
                    f"As a final-year student pursuing a {ctx.candidate_degree}{school_str}{spec_str}, I am looking for a 6-month graduation internship starting in early 2026."
                    f"{summary_line}\n\n"
                    f"{project_section}"
                    f"{role_callout}\n\n"
                    f"Thank you for your time and consideration.\n\n"
                    f"Best regards,\n{ctx.candidate_name}\n{ctx.candidate_email or ''}{links_str}"
                )
            return GeneratedMessage(subject=subject, body=body, provider=self.name)

        # French Email (Default)
        greeting = (
            f"Bonjour {ctx.recruiter_name},"
            if ctx.recruiter_name
            else "Madame, Monsieur,"
        )
        prefix = "Relance : " if ctx.follow_up_count > 0 else ""
        subject = f"{prefix}Candidature Stage PFE – {ctx.job_title} – {ctx.candidate_name}"

        if ctx.follow_up_count > 0:
            body = (
                f"{greeting}\n\n"
                f"Je me permets de revenir vers vous concernant ma candidature pour le stage PFE de **{ctx.job_title}** chez **{ctx.company_name}**.\n\n"
                f"Je reste particulièrement motivé par les projets de votre équipe et serais ravi d'avoir votre retour.\n\n"
                f"Cordialement,\n{ctx.candidate_name}\n{ctx.candidate_email or ''}"
            )
        else:
            project_section = ""
            if ctx.selected_project_name and tech_signal_str:
                project_section = (
                    f"À titre d'exemple, lors de mon projet **{ctx.selected_project_name}** ({ctx.selected_project_description or 'projet technique'}), "
                    f"j'ai mis en œuvre des solutions basées sur **{tech_signal_str}**, des technologies au cœur des exigences de ce poste.\n\n"
                )

            role_callout = (
                "Je serais ravi d'échanger directement avec vous sur vos enjeux d'ingénierie et de vous présenter mes réalisations."
                if ctx.contact_relevance == "MEDIUM"
                else "Seriez-vous disponible pour un court échange afin de discuter de cette opportunité et de mon profil ?"
            )

            summary_line = f"\n\n{summary_fr}" if summary_fr else ""

            links = []
            if ctx.candidate_github_url:
                links.append(f"GitHub: {ctx.candidate_github_url}")
            if ctx.candidate_linkedin_url:
                links.append(f"LinkedIn: {ctx.candidate_linkedin_url}")
            links_str = ("\n" + " | ".join(links)) if links else ""

            body = (
                f"{greeting}\n\n"
                f"Je me permets de vous contacter au sujet d'un stage PFE (fin d'études) pour le poste de **{ctx.job_title}**{location_str} au sein de **{ctx.company_name}**.\n\n"
                f"Étudiant en {ctx.candidate_degree}{school_str}{spec_str}, je suis à la recherche d'un stage de fin d'études de 6 mois."
                f"{summary_line}\n\n"
                f"{project_section}"
                f"{role_callout}\n\n"
                f"Je reste à votre entière disposition pour vous transmettre mon CV et échanger lors d'un entretien.\n\n"
                f"Cordialement,\n{ctx.candidate_name}\n{ctx.candidate_email or ''}{links_str}"
            )

        return GeneratedMessage(subject=subject, body=body, provider=self.name)



# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert career advisor helping a final-year engineering student write a \
personalized internship outreach message for a PFE (Projet de Fin d'Études) opportunity.

STRICT RULES:
- Use ONLY the facts supplied in the user prompt. NEVER invent candidate experience, company facts, achievements, or unprovided technologies.
- Do NOT use exaggerated claims or generic AI clichés (e.g. "I am passionate about", "dream job", "unmatched enthusiasm").
- Adapt wording based on the contact role (Recruiter vs Engineering Manager vs Generic HR).
- For EMAIL channel: Target 120-220 words. Include a concise Subject line. Format as:
    SUBJECT: <subject>
    BODY:
    <body>
- For LINKEDIN channel: Target 50-100 words. Do NOT include a Subject line. Return BODY only.
- Write in the requested language (French or English).
"""


def _build_openai_user_prompt(ctx: MessageContext) -> str:
    lang_label = "English" if ctx.language == "en" else "French"
    parts = [
        f"Target Language: {lang_label}",
        f"Channel: {ctx.channel}",
        f"Candidate Name: {ctx.candidate_name}",
        f"Candidate Degree & School: {ctx.candidate_degree}" + (f" at {ctx.candidate_school}" if ctx.candidate_school else "") + (f" ({ctx.candidate_specialization})" if ctx.candidate_specialization else ""),
        f"Target Company: {ctx.company_name}" + (f" (Industry: {ctx.company_industry})" if ctx.company_industry else ""),
        f"Position: {ctx.job_title}" + (f" in {ctx.job_location}" if ctx.job_location else ""),
    ]

    if ctx.recruiter_name:
        parts.append(
            f"Recipient: {ctx.recruiter_name}"
            + (f" ({ctx.recruiter_title})" if ctx.recruiter_title else "")
            + f" [Role Relevance: {ctx.contact_relevance}]"
        )
    else:
        parts.append(f"Recipient: Hiring / HR Team [Role Relevance: {ctx.contact_relevance}]")

    if ctx.selected_project_name and ctx.personalization_signals:
        parts.append(
            f"Selected Candidate Project: {ctx.selected_project_name} "
            f"({ctx.selected_project_description or ''}) "
            f"with matching technology signals: {', '.join(ctx.personalization_signals)}"
        )

    if ctx.job_description:
        parts.append(f"Job Description Excerpt:\n{ctx.job_description[:400]}")

    cv_summary = (
        ctx.candidate_cv_summary_en if ctx.language == "en" else ctx.candidate_cv_summary_fr
    ) or ctx.candidate_cv_summary
    if cv_summary:
        parts.append(f"Candidate CV Summary: {cv_summary[:300]}")

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
