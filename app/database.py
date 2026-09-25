from collections.abc import Generator
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


DEFAULT_CANDIDATE_PROJECTS = [
    {
        "name": "GearOil",
        "description": "Industrial asset monitoring and telemetry system with real-time alert processing.",
        "technologies": ["ASP.NET Core", ".NET", "C#", "SQL Server", "Kafka", "Redis", "Docker", "PostgreSQL", "FastAPI"],
    },
    {
        "name": "SmartMunicipality",
        "description": "Citizen service portal for automated request processing and administrative document workflows.",
        "technologies": ["Laravel", "PHP", "React", "TypeScript", "MySQL", "Tailwind CSS"],
    },
    {
        "name": "DataPipelineX",
        "description": "ETL ingestion and processing engine for distributed data streams.",
        "technologies": ["Python", "PyTorch", "TensorFlow", "Pandas", "Spark", "AWS", "Docker", "Machine Learning", "Data Engineering"],
    },
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
    )

    database_url: str = "sqlite:///./pfe_jobs.db"
    target_database_url: str | None = None
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    adzuna_country: str = "ma"
    hunter_api_key: str | None = None

    # Web Search Discovery settings
    web_search_engine: str = "duckduckgo"
    web_search_api_key: str | None = None
    web_search_max_queries_per_run: int = 30
    web_search_max_results_per_query: int = 10
    web_search_max_pages_per_query: int = 2
    web_search_max_candidate_pages_per_run: int = 50

    # Discovery Scheduler settings
    discovery_scheduler_enabled: bool = False
    discovery_scheduler_interval_hours: int = 24
    discovery_scheduler_max_runtime_minutes: int = 30
    discovery_run_on_startup: bool = False

    # -----------------------------------------------------------------------
    # Candidate Profile — source of truth for all submissions and messages.
    # Set these via environment variables or a .env file.
    # For production: also store CV at candidate_cv_path.
    # -----------------------------------------------------------------------
    candidate_first_name: str = "Your First Name"
    candidate_last_name: str = "Your Last Name"
    candidate_email: str = "your.email@example.com"
    candidate_phone: str = "+212 600 000 000"
    candidate_location: str | None = "Casablanca, Morocco"
    candidate_school: str | None = None           # e.g., ENSIAS, INPT
    candidate_degree: str | None = "Master"       # e.g., Bachelor, Master
    candidate_specialization: str | None = None   # e.g., Backend, Data Engineering
    candidate_skills: str | None = None           # comma-separated e.g., "Python, FastAPI, Docker"
    candidate_linkedin_url: str | None = None
    candidate_github_url: str | None = None
    candidate_portfolio_url: str | None = None
    # Absolute path to CV file on disk. Must exist before ATS submission.
    candidate_cv_path: str | None = None
    # Optional default cover letter body (plain text)
    candidate_cover_letter: str | None = None
    # Short professional summary for message generation (FR/EN and legacy fallback)
    candidate_cv_summary: str | None = None
    candidate_cv_summary_fr: str | None = None
    candidate_cv_summary_en: str | None = None
    candidate_projects: list[dict] | None = None

    # Language configuration
    message_languages: str = "fr,en"
    message_default_language: str = "fr"

    @model_validator(mode="after")
    def _sync_candidate_summaries(self) -> "Settings":
        # If legacy candidate_cv_summary is set but language-specific are not, populate them
        if self.candidate_cv_summary:
            if not self.candidate_cv_summary_fr:
                self.candidate_cv_summary_fr = self.candidate_cv_summary
            if not self.candidate_cv_summary_en:
                self.candidate_cv_summary_en = self.candidate_cv_summary
        # If legacy candidate_cv_summary is not set, default to fr (or en) for backward compatibility
        elif self.candidate_cv_summary_fr:
            self.candidate_cv_summary = self.candidate_cv_summary_fr
        elif self.candidate_cv_summary_en:
            self.candidate_cv_summary = self.candidate_cv_summary_en
        return self

    def get_cv_summary(self, language: str | None = "fr") -> str | None:
        lang = (language or "fr").lower().strip()
        if lang.startswith("en"):
            return self.candidate_cv_summary_en or self.candidate_cv_summary or self.candidate_cv_summary_fr
        return self.candidate_cv_summary_fr or self.candidate_cv_summary or self.candidate_cv_summary_en

    def get_candidate_projects(self) -> list[dict]:
        if self.candidate_projects:
            return self.candidate_projects
        return DEFAULT_CANDIDATE_PROJECTS

    # Legacy alias kept for backward compatibility with template message generation
    @property
    def candidate_name(self) -> str:  # type: ignore[override]
        return f"{self.candidate_first_name} {self.candidate_last_name}".strip()

    # Legacy alias kept for backward compatibility with template message generation
    @property
    def candidate_education(self) -> str | None:
        return self.candidate_school

    # -----------------------------------------------------------------------
    # LLM message generation
    # -----------------------------------------------------------------------
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    # -----------------------------------------------------------------------
    # SMTP email sending
    # -----------------------------------------------------------------------
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None  # defaults to smtp_user

    # -----------------------------------------------------------------------
    # IMAP response ingestion (reuses smtp_user / smtp_password by default)
    # -----------------------------------------------------------------------

    imap_host: str = "imap.gmail.com"
    imap_port: int = 993

    # -----------------------------------------------------------------------
    # Follow-up scheduling
    # -----------------------------------------------------------------------
    followup_delay_days_1: int = 3   # days after first contact before follow-up #1
    followup_delay_days_2: int = 5   # days after follow-up #1 before follow-up #2
    followup_max: int = 2            # maximum number of follow-ups per application

    # -----------------------------------------------------------------------
    # Safety flags — MUST be False in production to allow real sends
    # -----------------------------------------------------------------------
    # When True: email sending is blocked; body is logged but NOT delivered.
    dry_run_email: bool = True
    # When True: ATS submission payload is prepared and validated but NOT submitted externally.
    dry_run_ats: bool = True

    # -----------------------------------------------------------------------
    # Notion CRM integration (Phase 1: Notion → SQLite import only)
    # -----------------------------------------------------------------------
    # Both fields must be set for the integration to activate.
    # Never hardcode these values — always load from environment / .env.
    notion_api_key: str | None = None
    notion_database_id: str | None = None
    # When True: preview mode — no SQLite or Notion writes occur.
    notion_sync_dry_run: bool = True
    # Max pages fetched per Notion API call (Notion limit: 100).
    notion_sync_batch_size: int = 100
    # Source tag recorded on records imported from Notion.
    notion_import_source_tag: str = "notion_crm"



@lru_cache
def get_settings() -> Settings:
    import os
    env_file = ".env" if os.path.exists(".env") else None
    return Settings(_env_file=env_file)


def _normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def _connect_args(database_url: str) -> dict[str, object]:
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


db_url_effective = _normalize_postgres_url(get_settings().database_url)

engine = create_engine(
    db_url_effective,
    connect_args=_connect_args(db_url_effective),
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
