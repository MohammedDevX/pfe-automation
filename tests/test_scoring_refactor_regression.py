import pytest

from app.schemas import OpportunityIn
from app.scoring import score_opportunity, _calculate_technical_relevance


def test_high_technical_internships_reach_top_score_tier():
    """HIGH technical internships and PFEs must score in the top tier (>= 75)."""
    high_tech_roles = [
        ("Datadog", "Software Engineering Intern", "Paris, France", "Python, Go, distributed systems backend engineering."),
        ("TechCasa", "Backend Developer Intern", "Casablanca, Morocco", "C# .NET Core backend development."),
        ("Acme", "Full Stack Developer Intern", "Remote", "React, Node.js, TypeScript fullstack development."),
        ("NovaTech", ".NET PFE", "Rabat, Morocco", "Stage PFE .NET Angular C# ASP.NET Core."),
        ("Atlas", "C# Backend Internship", "Casablanca", "C# backend internship with SQL Server."),
        ("JavaCorp", "Java Spring Internship", "Paris", "Java Spring Boot microservices backend internship."),
        ("CloudCo", "DevOps Intern", "Remote", "Docker, Kubernetes, CI/CD pipeline automation intern."),
        ("TestCo", "QA Automation Intern", "Casablanca", "QA test automation with Selenium, Cypress, and Python."),
    ]

    for company, title, location, desc in high_tech_roles:
        opp = OpportunityIn(
            source="test",
            company=company,
            title=title,
            location=location,
            description=desc,
            url="https://example.com/job",
        )
        res = score_opportunity(opp)
        assert res.score >= 75, f"Expected score >= 75 for '{title}', got {res.score} (Reason: {res.reason})"


def test_low_non_technical_internships_demoted():
    """LOW / Non-technical internships must score <= 40."""
    non_tech_roles = [
        ("Sony Music", "Juriste - Stage", "Paris", "Stage 6 mois juriste, propriété intellectuelle."),
        ("GovTech", "Public Affairs Intern", "Paris", "Public affairs internship, government relations."),
        ("Doctolib", "Account Management Intern", "Paris", "Stage account management commercial."),
        ("Datadog", "Product Management Intern", "Paris", "Product management internship, roadmap analytics."),
        ("Scaleway", "Event Operations Intern", "Paris", "Event operations internship, logistics."),
        ("BrandCo", "Marketing Intern", "Remote", "Marketing internship, social media."),
        ("PeopleCo", "HR Intern", "Casablanca", "HR internship, recruiting, talent acquisition."),
    ]

    for company, title, location, desc in non_tech_roles:
        opp = OpportunityIn(
            source="test",
            company=company,
            title=title,
            location=location,
            description=desc,
            url="https://example.com/job",
        )
        res = score_opportunity(opp)
        assert res.score <= 40, f"Expected score <= 40 for '{title}', got {res.score} (Reason: {res.reason})"


def test_hybrid_technical_roles_not_demoted():
    """Hybrid technical roles with code/engineering in title must NOT be demoted."""
    hybrid_roles = [
        ("TechCorp", "Technical Account Engineer", "Paris", "Technical account engineer working with APIs and python code."),
        ("ScaleCo", "Solutions Engineer", "Remote", "Solutions engineer building architecture and C# demos."),
        ("CloudCo", "Sales Engineer", "Paris", "Sales engineer with software engineering background."),
        ("DevOpsCo", "DevOps Consultant", "Casablanca", "DevOps consultant automating CI/CD pipelines."),
        ("ProductTech", "Technical Product Engineer", "Remote", "Technical product engineer coding prototypes in Python."),
    ]

    for company, title, location, desc in hybrid_roles:
        opp = OpportunityIn(
            source="test",
            company=company,
            title=title,
            location=location,
            description=desc,
            url="https://example.com/job",
        )
        res = score_opportunity(opp)
        level, _, _ = _calculate_technical_relevance(title, desc, f"{title} {desc}")
        assert level in ("HIGH", "MEDIUM"), f"Expected HIGH/MEDIUM tech level for hybrid role '{title}', got {level}"


def test_language_parity_english_intern_vs_french_pfe():
    """English 'Software Engineering Intern' and French 'Stage PFE Développeur Backend' reach equivalent high score."""
    english_opp = OpportunityIn(
        source="test",
        company="Datadog",
        title="Software Engineering Intern",
        location="Paris, France",
        description="Software engineering internship in Python and backend services.",
        url="https://example.com/en",
    )
    french_opp = OpportunityIn(
        source="test",
        company="TechCasa",
        title="Stage PFE Développeur Backend",
        location="Paris, France",
        description="Stage de fin d'études développeur backend Python et API.",
        url="https://example.com/fr",
    )

    res_en = score_opportunity(english_opp)
    res_fr = score_opportunity(french_opp)

    assert res_en.score >= 75
    assert res_fr.score >= 75
    assert abs(res_en.score - res_fr.score) <= 10


def test_boilerplate_description_does_not_elevate_non_tech_job():
    """Company description containing 'web', 'API', 'IT', 'technology' for 'Account Manager Intern' remains LOW tech."""
    opp = OpportunityIn(
        source="test",
        company="TechCorp",
        title="Account Manager Intern",
        location="Paris, France",
        description="Notre entreprise développe des API web, des solutions IT et des technologies digitales de pointe. Stage en account management commercial.",
        url="https://example.com/job",
    )

    res = score_opportunity(opp)
    level, _, _ = _calculate_technical_relevance(opp.title, opp.description, f"{opp.title} {opp.description}")

    assert level == "LOW"
    assert res.score <= 40
