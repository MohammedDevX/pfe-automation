import pytest
from app.schemas import OpportunityIn
from app.scoring import score_opportunity, RelevanceCategory


def test_explicit_pfe_dotnet_developer() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE .NET / C# Developer",
        company="TechCorp",
        url="https://example.com/job1",
        location="Casablanca, Morocco",
        description="Stage de fin d'études 6 mois C# ASP.NET Core SQL Server",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 85 <= res.score <= 100


def test_explicit_pfe_angular_spring_boot() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage de fin d'études Angular / Spring Boot",
        company="DevCo",
        url="https://example.com/job2",
        location="Rabat",
        description="Projet de fin d'études en développement web Java Spring Boot et Angular",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 85 <= res.score <= 100


def test_explicit_pfe_php_symfony() -> None:
    opp = OpportunityIn(
        source="test",
        title="PFE Internship PHP / Symfony",
        company="WebAgency",
        url="https://example.com/job3",
        location="Tangier",
        description="Développement d'une application web PHP Symfony et MySQL",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 80 <= res.score <= 100


def test_explicit_pfe_devops_docker() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE DevOps & Docker",
        company="CloudOps",
        url="https://example.com/job4",
        location="Remote",
        description="Mise en place de CI/CD avec Docker, Kubernetes et GitLab CI",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 80 <= res.score <= 100


def test_explicit_pfe_qa_test_automation() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE QA / Test Automation",
        company="QualityLab",
        url="https://example.com/job5",
        location="Casablanca",
        description="Automatisation des tests de recette avec Selenium et Python",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 80 <= res.score <= 100


def test_explicit_pfe_data_engineering() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE Data Engineering & SQL",
        company="DataInc",
        url="https://example.com/job6",
        location="Rabat",
        description="Pipeline ETL, Python, SQL Server et PostgreSQL",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 80 <= res.score <= 100


def test_explicit_pfe_ui_ux_frontend() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE UI/UX & Frontend (Angular/HTML/CSS)",
        company="DesignTech",
        url="https://example.com/job7",
        location="Casablanca",
        description="Conception UI/UX sur Figma et intégration web Angular HTML CSS",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 75 <= res.score <= 100


def test_explicit_pfe_designer_figma_only_scores_low() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE Designer UI/UX",
        company="CreativeStudio",
        url="https://example.com/job8",
        location="Casablanca",
        description="Création de maquettes, prototipage Figma, charte graphique, wireframes",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert res.score < 50


def test_stagiaire_comptable_pfe_scores_low() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stagiaire comptable PFE",
        company="AuditCo",
        url="https://example.com/job9",
        location="Casablanca",
        description="Stage PFE en comptabilité et finance. Tenue de caisse et facturation.",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert res.score < 50


def test_communication_pfe_scores_low() -> None:
    opp = OpportunityIn(
        source="test",
        title="Communication PFE",
        company="MediaAgency",
        url="https://example.com/job10",
        location="Rabat",
        description="Stage de fin d'études en communication, réseaux sociaux et événementiel",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert res.score < 50


def test_rh_pfe_scores_low() -> None:
    opp = OpportunityIn(
        source="test",
        title="RH PFE",
        company="HRConsulting",
        url="https://example.com/job11",
        location="Casablanca",
        description="Stage PFE Ressources Humaines, gestion des recrutements et paie",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert res.score < 50


def test_business_development_internship_scores_low() -> None:
    opp = OpportunityIn(
        source="test",
        title="Business Development Internship",
        company="SalesForceX",
        url="https://example.com/job12",
        location="Casablanca",
        description="Commercial internship, client prospection, sales strategy",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_INTERNSHIP.value
    assert res.score < 50


def test_stage_pfe_acheteur_commercial_scores_low() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE Acheteur / Commercial",
        company="BuyCo",
        url="https://example.com/job13",
        location="Tangier",
        description="Stage PFE en achats et négociation commerciale",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert res.score < 50


def test_stage_pfe_juriste_droit_scores_low() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE Juriste / Droit",
        company="LegalFirm",
        url="https://example.com/job14",
        location="Casablanca",
        description="Stage PFE droit des affaires, révision des contrats juridiques",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert res.score < 50


def test_stage_pfe_restauration_hotellerie_scores_low() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE Restauration / Hôtellerie",
        company="GrandHotel",
        url="https://example.com/job15",
        location="Marrakech",
        description="Stage PFE en gestion hôtelière et restauration",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert res.score < 50


def test_stage_pfe_informatique_ambiguous() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE Informatique",
        company="ITServices",
        url="https://example.com/job16",
        location="Casablanca",
        description="Participation aux projets IT et réseaux de l'entreprise",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 50 <= res.score <= 74


def test_stage_pfe_systemes_information_ambiguous() -> None:
    opp = OpportunityIn(
        source="test",
        title="Stage PFE Systèmes d'information",
        company="EnterpriseIT",
        url="https://example.com/job17",
        location="Rabat",
        description="Assistance sur le système d'information de gestion",
    )
    res = score_opportunity(opp)
    assert res.relevance_category == RelevanceCategory.EXPLICIT_PFE.value
    assert 50 <= res.score <= 74
