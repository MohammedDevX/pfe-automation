from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.schemas import OpportunityIn
from app.services.opportunities import upsert_opportunity


def test_upsert_deduplicates_across_provider_by_company_title_location() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    first = OpportunityIn(
        source="adzuna",
        external_id="a1",
        title="PFE Backend Developer",
        company="ExampleCo",
        url="https://adzuna.example/jobs/a1",
        location="Casablanca",
        description="PFE backend internship",
    )
    second = OpportunityIn(
        source="greenhouse:example",
        external_id="g1",
        title="PFE Backend Developer",
        company="ExampleCo",
        url="https://boards.example/jobs/g1",
        location="Casablanca",
        description="PFE backend internship",
    )

    app1, created1 = upsert_opportunity(session, first)
    session.commit()
    app2, created2 = upsert_opportunity(session, second)
    session.commit()

    assert created1 is True
    assert created2 is False
    assert app1.id == app2.id
