import pytest

from app.schemas import OpportunityIn
from app.scoring import score_opportunity, _calculate_technical_relevance


def test_high_technical_internships_reach_top_score_tier():
    """HIGH technical internships and PFEs must score in the top tier (>= 75)."""
    high_tech_roles = [
        ("Datadog", "Software Engineering Intern", "Paris, France", "Python, Go, distributed systems backend engineering."),
        ("Trusteq", "AI Developer Intern", "Berlin, Germany", "AI developer internship. Python, machine learning, deep learning."),
        ("TechCasa", "Backend Developer Intern", "Casablanca, Morocco", "C# .NET Core backend development."),
        ("DevCorp", ".NET Developer Intern", "Rabat, Morocco", "Internship in C# .NET Core and ASP.NET."),
        ("NovaTech", ".NET PFE", "Rabat, Morocco", "Stage PFE .NET Angular C# ASP.NET Core."),
        ("DataCo", "Data Engineering Intern", "Paris", "Data engineering internship with Python, Spark, ETL pipelines."),
        ("Acme", "Full Stack Developer Intern", "Remote", "React, Node.js, TypeScript fullstack development."),
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
        ("Doctolib", "Stage - Corporate Development Analyst", "Paris", "Corporate development analyst internship."),
        ("Doctolib", "Stage - Assistant Satisfaction", "Paris", "Internship in customer satisfaction and operations."),
        ("Doctolib", "Stage - Partenariats Opérations et Stratégie", "Paris", "Internship in partnerships operations and strategy."),
        ("Scaleway", "Event Operations Intern", "Paris", "Event operations internship, logistics."),
        ("BrandCo", "Marketing Intern", "Remote", "Marketing internship, social media."),
        ("PeopleCo", "HR Intern", "Casablanca", "HR internship, recruiting, talent acquisition."),
        ("MediaCo", "Communication Intern", "Paris", "Communication internship, social media, press relations."),
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
    """Company description containing 'web', 'API', 'IT', 'technology', 'cloud', 'AWS', 'data analytics' for non-tech roles remains demoted."""
    boilerplate_cases = [
        ("Account Manager Intern", "Notre entreprise développe des API web, des solutions IT et des technologies digitales de pointe. Cloud AWS data analytics. Stage en account management commercial."),
        ("Product Management Intern", "Join our team building cloud digital platforms, REST APIs, and microservices architecture. Internship focused on product roadmap analytics and feature planning."),
        ("Stage - Corporate Development Analyst", "Working at a leading cloud tech company utilizing AWS, Python, and data platform tech. Corporate development analyst internship, M&A and strategy."),
        ("Stage - Assistant Satisfaction", "Plateforme SaaS, cloud, digital, API. Stage assistant satisfaction client et opérations."),
        ("Stage - Partenariats Opérations et Stratégie", "Leading tech platform with cloud infrastructure and data analytics. Stage en partenariats opérations et stratégie."),
    ]

    for title, desc in boilerplate_cases:
        opp = OpportunityIn(
            source="test",
            company="TechCorp",
            title=title,
            location="Paris, France",
            description=desc,
            url="https://example.com/job",
        )
        res = score_opportunity(opp)
        level, _, _ = _calculate_technical_relevance(opp.title, opp.description, f"{opp.title} {opp.description}")
        assert level in ("LOW", "MEDIUM"), f"Expected LOW/MEDIUM tech level for '{title}', got {level}"
        assert res.score <= 40, f"Expected score <= 40 for '{title}', got {res.score} (Reason: {res.reason})"


def test_strategy_titles_not_incorrectly_demoted():
    """Titles containing 'strategy' combined with technical domains (Technology Strategy, Data Strategy, Cloud Strategy) must not be demoted as non-technical."""
    tech_strategy_roles = [
        ("Technology Strategy Engineer", "Paris", "Work on tech strategy, cloud architecture and software design."),
        ("Data Strategy Specialist", "Remote", "Python, data pipelines, SQL, and data strategy."),
        ("Cloud Strategy Architect", "Paris", "AWS, Azure cloud strategy and platform engineering."),
        ("DevOps Strategy Consultant", "Casablanca", "CI/CD automation, Docker, and DevOps strategy."),
    ]

    for title, location, desc in tech_strategy_roles:
        opp = OpportunityIn(
            source="test",
            company="ScaleTech",
            title=title,
            location=location,
            description=desc,
            url="https://example.com/job",
        )
        res = score_opportunity(opp)
        level, _, _ = _calculate_technical_relevance(title, desc, f"{title} {desc}")
        assert level in ("HIGH", "MEDIUM"), f"Expected HIGH or MEDIUM tech level for strategy tech role '{title}', got {level}"
        assert res.score >= 25, f"Expected score >= 25 for strategy tech role '{title}', got {res.score}"


def test_phase343_data_analyst_vs_data_engineering():
    """Phase 3.4.3: Non-engineering data analyst roles are demoted to LOW, while engineering data/ML roles remain HIGH."""
    # Non-engineering analyst roles -> LOW (score <= 40)
    non_tech_analysts = [
        ("Stage - Business Data Analyst", "Paris", "Python, SQL, Tableau dashboards, AWS cloud data analytics for business reporting."),
        ("Data Analyst Intern", "Paris", "Reporting, BI, business metrics reporting, SQL data extraction, Python data cleaning."),
    ]
    for title, loc, desc in non_tech_analysts:
        opp = OpportunityIn(source="test", company="TechCorp", title=title, location=loc, description=desc, url="https://example.com/job")
        res = score_opportunity(opp)
        level, _, _ = _calculate_technical_relevance(title, desc, f"{title} {desc}")
        assert level == "LOW", f"Expected LOW tech level for non-engineering analyst '{title}', got {level}"
        assert res.score <= 40, f"Expected score <= 40 for non-engineering analyst '{title}', got {res.score}"

    # Engineering data / ML roles -> HIGH (score >= 75 for internships, level == HIGH)
    high_tech_data_roles = [
        ("Data Engineering Intern", "Paris", "Stage de fin d'etudes Python, SQL, Spark, ETL pipelines data engineering."),
        ("Data Engineer Intern", "Paris", "Data engineer internship building data infrastructure, SQL, Python."),
        ("Analytics Engineer", "Remote", "dbt, SQL, Python, Snowflake analytics engineering."),
        ("ML Engineer", "Paris", "PyTorch, Python, ML model development and deployment."),
        ("Data/ML Engineer", "Remote", "Data and machine learning engineering, Python, Docker."),
        ("Software/Data Engineer", "Paris", "Software engineering and data pipelines, Python, Go."),
    ]
    for title, loc, desc in high_tech_data_roles:
        opp = OpportunityIn(source="test", company="DataCorp", title=title, location=loc, description=desc, url="https://example.com/job")
        res = score_opportunity(opp)
        level, _, _ = _calculate_technical_relevance(title, desc, f"{title} {desc}")
        assert level == "HIGH", f"Expected HIGH tech level for data engineering role '{title}', got {level}"
        assert res.score >= 25, f"Expected score >= 25 for data engineering role '{title}', got {res.score}"



