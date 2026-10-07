from collections import defaultdict
from sqlalchemy.orm import Session

from app.database import Settings
from app.integrations.providers import build_providers
from app.schemas import DiscoveryResult, OpportunityIn, ProviderStats, SearchCriteria
from app.scoring import score_opportunity
from app.services.opportunities import upsert_opportunity
from app.utils import canonicalize_url


async def search_and_persist(
    db: Session, criteria: SearchCriteria, settings: Settings
) -> DiscoveryResult:
    fetched: list[OpportunityIn] = []
    errors_by_provider: dict[str, str] = {}
    sources_queried: list[str] = []
    provider_results: dict[str, list[OpportunityIn]] = {}
    provider_errors: dict[str, str] = {}
    
    providers = build_providers(criteria, settings)
    for provider in providers:
        sources_queried.append(provider.name)
        try:
            results = await provider.search(criteria)
            provider_results[provider.name] = results
            fetched.extend(results)
        except Exception as e:
            provider_errors[provider.name] = str(e)
            errors_by_provider[provider.name] = str(e)

    new_opportunities = 0
    new_high_value_opportunities = 0
    duplicates_ignored = 0
    rejected_low_score = 0
    morocco_count = 0
    france_count = 0
    canada_count = 0
    belgium_count = 0
    switzerland_count = 0
    remote_count = 0
    other_countries_count = 0
    
    provider_new_counts: dict[str, int] = defaultdict(int)
    provider_dup_counts: dict[str, int] = defaultdict(int)
    provider_low_counts: dict[str, int] = defaultdict(int)

    applications = []
    new_application_ids: set[int] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    seen_urls: set[str] = set()

    for opportunity in fetched:
        provider_key = opportunity.source.split(":")[0].lower()

        # Cross-fetch deduplication by company, title, location
        key = (
            opportunity.company.strip().lower(),
            opportunity.title.strip().lower(),
            (opportunity.location or "").strip().lower(),
        )
        if key in seen_keys:
            duplicates_ignored += 1
            provider_dup_counts[provider_key] += 1
            continue

        # Cross-fetch deduplication by canonical URL
        canon_url = canonicalize_url(str(opportunity.url))
        if canon_url in seen_urls:
            duplicates_ignored += 1
            provider_dup_counts[provider_key] += 1
            continue

        seen_keys.add(key)
        seen_urls.add(canon_url)

        score = score_opportunity(opportunity, criteria)

        # Work authorization warning note
        if score.work_authorization_warning:
            warning_note = f"[Visa/Auth Warning: {score.work_authorization_warning}]"
            if opportunity.notes:
                if warning_note not in opportunity.notes:
                    opportunity.notes += f"; {warning_note}"
            else:
                opportunity.notes = warning_note

        # Geographic stats
        loc = (opportunity.location or "").lower()
        is_morocco = any(term in loc for term in ["morocco", "maroc", "casablanca", "rabat", "tanger", "tangier", "marrakech", ", ma"])
        is_france = any(term in loc for term in ["france", "paris", "lyon", "toulouse", "bordeaux", "nantes", ", fr"])
        is_canada = any(term in loc for term in ["canada", "montreal", "toronto", "vancouver", "quebec", ", ca"])
        is_belgium = any(term in loc for term in ["belgium", "belgique", "brussels", "bruxelles", ", be"])
        is_switzerland = any(term in loc for term in ["switzerland", "suisse", "schweiz", "zurich", "geneva", ", ch"])
        is_remote = any(term in loc for term in ["remote", "télétravail", "teletravail", "home based", "worldwide", "anywhere", "emea"])

        if score.score < criteria.min_score:
            rejected_low_score += 1
            provider_low_counts[provider_key] += 1
            continue

        if is_morocco:
            morocco_count += 1
        if is_france:
            france_count += 1
        if is_canada:
            canada_count += 1
        if is_belgium:
            belgium_count += 1
        if is_switzerland:
            switzerland_count += 1
        if is_remote:
            remote_count += 1
        if not (is_morocco or is_france or is_canada or is_belgium or is_switzerland or is_remote):
            other_countries_count += 1

        # Database upsert
        application, was_created = upsert_opportunity(db, opportunity, score)
        if was_created:
            new_opportunities += 1
            if score.score >= 60:
                new_high_value_opportunities += 1
            provider_new_counts[provider_key] += 1
            new_application_ids.add(application.id)
        else:
            duplicates_ignored += 1
            provider_dup_counts[provider_key] += 1
            
        applications.append(application)

    db.commit()
    for application in applications:
        db.refresh(application)

    applications.sort(key=lambda item: (item.score, item.created_at), reverse=True)

    # Build provider statistics
    provider_stats: list[ProviderStats] = []
    for provider in providers:
        prov_err = provider_errors.get(provider.name)
        prov_status = provider.status
        if prov_err:
            if "credentials" in prov_err.lower() or provider.status == "credentials_missing":
                prov_status = "credentials_missing"
            elif provider.status == "blocked/restricted":
                prov_status = "blocked/restricted"
            elif provider.status == "manual-only":
                prov_status = "manual-only"
            else:
                prov_status = "error"

        stat = ProviderStats(
            provider_name=provider.name,
            countries_covered=provider.countries_covered,
            access_method=provider.access_method,
            status=prov_status,
            jobs_fetched=len(provider_results.get(provider.name, [])),
            jobs_normalized=len(provider_results.get(provider.name, [])),
            new_jobs=provider_new_counts.get(provider.name, 0),
            duplicates=provider_dup_counts.get(provider.name, 0),
            low_score_jobs=provider_low_counts.get(provider.name, 0),
            errors_or_restrictions=prov_err or provider.restriction_reason,
        )
        provider_stats.append(stat)

    return DiscoveryResult(
        sources_queried=sources_queried,
        jobs_fetched=len(fetched),
        jobs_normalized=len(fetched),
        new_opportunities=new_opportunities,
        duplicates_ignored=duplicates_ignored,
        rejected_low_score=rejected_low_score,
        errors_by_provider=errors_by_provider,
        morocco_count=morocco_count,
        france_count=france_count,
        canada_count=canada_count,
        belgium_count=belgium_count,
        switzerland_count=switzerland_count,
        remote_count=remote_count,
        other_countries_count=other_countries_count,
        provider_stats=provider_stats,
        new_application_ids=new_application_ids,
        applications=applications,
    )
