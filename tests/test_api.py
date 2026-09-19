from fastapi.testclient import TestClient

from app.main import app

def test_discover_without_adzuna_credentials_returns_error_in_stats() -> None:
    client = TestClient(app)
    response = client.post(
        "/opportunities/discover",
        json={
            "keywords": ["PFE", "backend"],
            "locations": ["Casablanca"],
            "technologies": [".NET"],
            "providers": ["adzuna"],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "adzuna" in data["errors_by_provider"]
    assert "ADZUNA_APP_ID" in data["errors_by_provider"]["adzuna"]

