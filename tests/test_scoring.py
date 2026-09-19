from app.schemas import OpportunityIn, SearchCriteria
from app.scoring import score_opportunity, _title_is_senior


def test_pfe_backend_technology_location_scores_high() -> None:
    opportunity = OpportunityIn(
        source="manual",
        title="Stage PFE Backend .NET Developer",
        company="ExampleCo",
        url="https://example.com/jobs/1",
        location="Casablanca, Morocco",
        description="Stage de fin d'etudes de 6 mois avec C#, ASP.NET, API, SQL et Docker.",
    )
    criteria = SearchCriteria(
        keywords=["PFE", "backend"],
        locations=["Casablanca"],
        technologies=[".NET", "C#"],
    )

    result = score_opportunity(opportunity, criteria)

    assert result.score >= 90
    assert "PFE" in result.reason
    assert "requested technology" in result.reason
    assert "internship duration" in result.reason


def test_senior_roles_are_penalized() -> None:
    opportunity = OpportunityIn(
        source="manual",
        title="Senior Backend Engineer",
        company="ExampleCo",
        url="https://example.com/jobs/2",
        location="Rabat",
        description="Requires 5 years of experience as a lead backend engineer.",
    )

    result = score_opportunity(opportunity)

    assert result.score < 50
    assert "seniority" in result.reason
    assert "experience requirement" in result.reason


# ---------------------------------------------------------------------------
# Regression tests for false-positive PFE scoring (issue exposed by real ATS run)
# ---------------------------------------------------------------------------


def test_senior_title_with_intern_in_description_does_not_get_pfe_bonus() -> None:
    """A senior-titled role that mentions 'intern' in the description must NOT
    receive a PFE bonus — the description-level mention must be suppressed."""
    opportunity = OpportunityIn(
        source="greenhouse:canonical",
        title="Senior Software Engineer (Backend)",
        company="Canonical",
        url="https://job-boards.greenhouse.io/canonical/jobs/3142044",
        location="Home based - EMEA",
        description=(
            "Senior Backend Software Engineer. Canonical also runs internship "
            "and stage programmes for students. 5+ years required. "
            "Python, SQL, PostgreSQL, cloud. Lead architects preferred."
        ),
    )

    result = score_opportunity(opportunity)

    # Must be penalised well below the PFE threshold
    assert result.score <= 45, (
        f"Senior role scored {result.score} — PFE suppression failed.\n{result.reason}"
    )
    # Suppression note must appear in the reason
    assert "suppressed" in result.reason, (
        f"Expected suppression note in reason.\n{result.reason}"
    )
    # Seniority penalty must appear
    assert "seniority" in result.reason


def test_lead_title_with_stage_in_description_does_not_get_pfe_bonus() -> None:
    """A lead-titled role mentioning 'stage' (career stage sense) in description
    must not receive PFE bonus."""
    opportunity = OpportunityIn(
        source="lever:fintech",
        title="Lead Backend Engineer",
        company="FinTech",
        url="https://jobs.lever.co/fintech/abc",
        location="Paris, France",
        description=(
            "At this stage in your career you will mentor junior engineers. "
            "internship experience is valued. 5 years minimum experience."
        ),
    )

    result = score_opportunity(opportunity)

    assert result.score <= 45, (
        f"Lead role scored {result.score} — PFE suppression failed.\n{result.reason}"
    )
    assert "suppressed" in result.reason


def test_confirmed_backend_engineer_scores_low() -> None:
    """'Confirmed' is a seniority signal (used e.g. by BlaBlaCar).
    The role must score below the PFE threshold."""
    opportunity = OpportunityIn(
        source="lever:blablacar",
        title="Confirmed Backend Engineer - Carpool Supply",
        company="blablacar",
        url="https://jobs.lever.co/blablacar/78ea5dab",
        location="Paris, France",
        description=(
            "Backend engineer role. No internship. Lead with senior experience. "
            "Python, SQL. Manager track possible."
        ),
    )

    result = score_opportunity(opportunity)

    assert result.score <= 35, (
        f"Confirmed Backend Engineer scored {result.score} — too high.\n{result.reason}"
    )
    # 'confirmed' seniority penalty must appear
    assert "confirmed" in result.reason


def test_stage_developpeur_scores_strongly() -> None:
    """A French 'stage développeur' title must score strongly as a valid PFE."""
    opportunity = OpportunityIn(
        source="manual",
        title="Stage developpeur Backend - PFE",
        company="TechMaroc",
        url="https://example.com/jobs/3",
        location="Rabat",
        description=(
            "Stage de fin etudes 5 mois. Vous travaillerez sur une API REST Python/Django."
        ),
    )

    result = score_opportunity(opportunity)

    assert result.score >= 60, (
        f"Stage developpeur PFE scored {result.score} — too low.\n{result.reason}"
    )
    assert "PFE (title)" in result.reason


def test_backend_pfe_internship_in_title_scores_strongly() -> None:
    """A title containing 'PFE internship' must score strongly."""
    opportunity = OpportunityIn(
        source="manual",
        title="Backend PFE internship - Python",
        company="StartupFR",
        url="https://example.com/jobs/4",
        location="Paris",
        description=(
            "Internship for final year students. 5 months duration. "
            "Python, FastAPI, PostgreSQL."
        ),
    )

    result = score_opportunity(opportunity)

    assert result.score >= 60, (
        f"Backend PFE internship scored {result.score} — too low.\n{result.reason}"
    )
    assert "PFE (title)" in result.reason


def test_full_time_junior_without_pfe_signal_does_not_classify_as_pfe() -> None:
    """A 'Junior Software Engineer' with no explicit PFE/internship term in the
    title should score in a medium range but NOT receive a title-level PFE bonus."""
    opportunity = OpportunityIn(
        source="manual",
        title="Junior Software Engineer",
        company="StartupX",
        url="https://example.com/jobs/5",
        location="Casablanca",
        description="Entry level position. 0-2 years experience. Backend Python developer.",
    )

    result = score_opportunity(opportunity)

    # Must NOT have PFE (title) signal
    assert "PFE (title)" not in result.reason, (
        f"Junior SW Eng should not receive title-level PFE bonus.\n{result.reason}"
    )


def test_title_seniority_detection_positive_cases() -> None:
    """_title_is_senior correctly identifies senior titles."""
    senior_titles = [
        "Senior Software Engineer",
        "Lead Backend Engineer",
        "Confirmed Backend Engineer",
        "Staff Engineer",
        "Principal Architect",
        "Engineering Manager",
        "Expert DevOps Engineer",
        "Experienced Backend Developer",
    ]
    for title in senior_titles:
        assert _title_is_senior(title.lower()), (
            f"Expected _title_is_senior to return True for: {title!r}"
        )


def test_title_seniority_detection_negative_cases() -> None:
    """_title_is_senior does NOT flag real PFE / junior / ambiguous titles."""
    non_senior_titles = [
        "Stage PFE Backend",
        "Intern Backend Developer",
        "Junior Software Engineer",
        "Backend Developer",
        "Software Engineer",
        "Stagiaire Développeur",
    ]
    for title in non_senior_titles:
        assert not _title_is_senior(title.lower()), (
            f"Expected _title_is_senior to return False for: {title!r}"
        )


def test_relevance_category_tier_bounds_and_ranking() -> None:
    """Test all 20 required false-positive and ranking scenarios."""
    scenarios = [
        # Explicit PFE (75..100)
        ("Stage PFE Backend Developer", "Casablanca", "Stage fin d'etudes 6 mois", "EXPLICIT_PFE", 75, 100),
        ("Stage de fin d'études - Développeur Full Stack", "Paris", "Stage 6 mois Python", "EXPLICIT_PFE", 75, 100),
        ("Internship - Software Engineer (PFE)", "Rabat", "Final year project", "EXPLICIT_PFE", 75, 100),
        
        # Explicit Internship (60..74)
        ("Stage Développeur Java/Spring", "Casablanca", "Stage de 5 mois", "EXPLICIT_INTERNSHIP", 60, 74),
        ("Software Engineering Intern", "Remote", "Internship role for summer/fall", "EXPLICIT_INTERNSHIP", 60, 74),

        # Graduate (45..59)
        ("Graduate Software Engineer", "Paris", "New grad program, 0-1 year exp", "GRADUATE", 45, 59),
        ("Développeur Jeune Diplômé", "Lyon", "Poste pour débutant M2", "GRADUATE", 45, 59),

        # Junior (35..44)
        ("Junior Backend Developer", "Casablanca", "1-2 years experience required", "JUNIOR", 35, 44),

        # Full Time (20..34)
        ("Software Engineer II - Java/Python", "Remote", "Canonical runs internship and stage programs", "FULL_TIME", 20, 34),
        ("Full-time Backend Developer (CDI)", "Paris", "CDI position for software dev", "FULL_TIME", 20, 34),

        # Senior (0..19)
        ("Senior Software Engineer (Backend)", "Paris", "Canonical runs internship and stage programs", "SENIOR", 0, 19),
        ("Lead Backend Engineer", "Paris", "At this stage in your career you mentor interns. 5+ years exp", "SENIOR", 0, 19),
        ("Principal Architect", "Remote", "10+ years experience required", "SENIOR", 0, 19),
        ("Staff Engineer", "Paris", "Staff software engineer role", "SENIOR", 0, 19),
        ("Engineering Manager", "Casablanca", "Manager of engineering teams", "SENIOR", 0, 19),
        ("Confirmed Backend Engineer", "Paris", "BlaBlaCar confirmed developer", "SENIOR", 0, 19),
        ("Experienced Software Developer", "Rabat", "Experienced dev role", "SENIOR", 0, 19),
        ("Senior Java Developer", "Casablanca", "Company runs intern program. 5 years exp", "SENIOR", 0, 19),
        ("Software Engineer", "Paris", "Requires 5+ years experience in Python", "SENIOR", 0, 19),
        ("Backend Engineer", "Remote", "3+ years experience required", "SENIOR", 0, 19),
    ]

    for title, loc, desc, expected_cat, min_score, max_score in scenarios:
        opp = OpportunityIn(
            source="test",
            title=title,
            company="TestCo",
            url="https://example.com/test",
            location=loc,
            description=desc,
        )
        res = score_opportunity(opp)
        assert res.relevance_category == expected_cat, (
            f"Title: {title!r} expected category {expected_cat}, got {res.relevance_category}"
        )
        assert min_score <= res.score <= max_score, (
            f"Title: {title!r} expected score between {min_score} and {max_score}, got {res.score}"
        )


def test_quality_precedence_and_expired_rules():
    # 1. Structured employmentType = FULL_TIME overrides title PFE
    opp_fulltime_struct = OpportunityIn(
        source="test",
        title="Stage PFE en Developpement .NET",
        company="FullTimeCorp",
        url="https://example.com/job1",
        location="Casablanca",
        description="Stage PFE backend",
        notes="employment_type=FULL_TIME",
    )
    res_struct = score_opportunity(opp_fulltime_struct)
    assert res_struct.relevance_category == "FULL_TIME"

    # 2. CDI / Contrat à durée indéterminée overrides title PFE
    opp_cdi = OpportunityIn(
        source="test",
        title="Stage PFE Développeur Java",
        company="CDICorp",
        url="https://example.com/job2",
        location="Rabat",
        description="Poste en Contrat à durée indéterminée (CDI) à temps plein",
    )
    res_cdi = score_opportunity(opp_cdi)
    assert res_cdi.relevance_category == "FULL_TIME"

    # 3. Token-aware tech matching: "React" should not match "Reaction"
    opp_react = OpportunityIn(
        source="test",
        title="Stage PFE Front-end",
        company="Tech",
        url="https://example.com/job3",
        location="Casablanca",
        description="Ce projet demande une bonne réaction de l'équipe",
    )
    res_react = score_opportunity(opp_react)
    assert "+6 technology: react" not in res_react.reason

    # 4. Token-aware tech matching: "Java" should not match "JavaScript"
    opp_js = OpportunityIn(
        source="test",
        title="Stage PFE JavaScript",
        company="Tech",
        url="https://example.com/job4",
        location="Casablanca",
        description="Développement JavaScript uniquement",
    )
    res_js = score_opportunity(opp_js)
    assert "+8 technology: java" not in res_js.reason

    # 5. Expired opportunity handling via valid_through date in past
    opp_expired = OpportunityIn(
        source="test",
        title="Stage PFE Backend Python",
        company="ExpiredCorp",
        url="https://example.com/job5",
        location="Casablanca",
        description="Stage PFE Python",
        notes="valid_through=2020-01-01T00:00:00",
    )
    res_expired = score_opportunity(opp_expired)
    assert "⛔ EXPIRED" in res_expired.reason
    assert res_expired.score < 50



