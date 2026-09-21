from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import re

from app.schemas import OpportunityIn, SearchCriteria


class RelevanceCategory(str, Enum):
    EXPLICIT_PFE = "EXPLICIT_PFE"
    EXPLICIT_INTERNSHIP = "EXPLICIT_INTERNSHIP"
    GRADUATE = "GRADUATE"
    JUNIOR = "JUNIOR"
    FULL_TIME = "FULL_TIME"
    SENIOR = "SENIOR"


TIER_SCORE_BOUNDS = {
    RelevanceCategory.EXPLICIT_PFE: (75, 100),
    RelevanceCategory.EXPLICIT_INTERNSHIP: (75, 100),
    RelevanceCategory.GRADUATE: (55, 74),
    RelevanceCategory.JUNIOR: (40, 54),
    RelevanceCategory.FULL_TIME: (25, 39),
    RelevanceCategory.SENIOR: (0, 19),
}

TIER_RANK = {
    RelevanceCategory.EXPLICIT_PFE: 1,
    RelevanceCategory.EXPLICIT_INTERNSHIP: 1,
    RelevanceCategory.GRADUATE: 3,
    RelevanceCategory.JUNIOR: 4,
    RelevanceCategory.FULL_TIME: 5,
    RelevanceCategory.SENIOR: 6,
}


@dataclass(frozen=True)
class ScoreResult:
    score: int
    reason: str
    work_authorization_warning: str | None = None
    detected_language: str = "unknown"
    relevance_category: str = RelevanceCategory.FULL_TIME.value


# ---------------------------------------------------------------------------
# PFE / internship terms
# ---------------------------------------------------------------------------
PFE_TERMS = {
    "pfe": 25,
    "stage fin d'etudes": 24,
    "stage fin d'études": 24,
    "stage de fin d'etudes": 24,
    "stage de fin d'études": 24,
    "internship": 18,
    "intern": 14,
    "stage": 14,
    "stagiaire": 18,
    "final year": 15,
}

_DESC_PFE_WEIGHT_FRACTION = 0.25

# ---------------------------------------------------------------------------
# Category matching regex patterns
# ---------------------------------------------------------------------------
PFE_TITLE_PATTERNS = [
    re.compile(r"\bpfe\b", re.IGNORECASE),
    re.compile(r"\bstage\s+(?:de\s+)?fin\s+d['’]?études?\b", re.IGNORECASE),
    re.compile(r"\bstage\s+(?:de\s+)?fin\s+d['’]?etudes?\b", re.IGNORECASE),
    re.compile(r"\bfinal\s+year\b", re.IGNORECASE),
    re.compile(r"\bend\s+of\s+studies\b", re.IGNORECASE),
    re.compile(r"\bprojet\s+de\s+fin\s+d['’]?[ée]tudes?\b", re.IGNORECASE),
]

INTERNSHIP_TITLE_PATTERNS = [
    re.compile(r"\bstage\b", re.IGNORECASE),
    re.compile(r"\bstagiaire\b", re.IGNORECASE),
    re.compile(r"\bintern\b", re.IGNORECASE),
    re.compile(r"\binternship\b", re.IGNORECASE),
    re.compile(r"\btrainee\b", re.IGNORECASE),
    re.compile(r"\balternance\b", re.IGNORECASE),
    re.compile(r"\bapprenti(?:sage)?\b", re.IGNORECASE),
]

SENIOR_PATTERNS = [
    re.compile(r"\bsenior\b", re.IGNORECASE),
    re.compile(r"\bsr\.?\b", re.IGNORECASE),
    re.compile(r"\blead\b", re.IGNORECASE),
    re.compile(r"\bprincipal\b", re.IGNORECASE),
    re.compile(r"\bmanager\b", re.IGNORECASE),
    re.compile(r"\barchitect\b", re.IGNORECASE),
    re.compile(r"\bstaff\b", re.IGNORECASE),
    re.compile(r"\bconfirmed\b", re.IGNORECASE),
    re.compile(r"\bexpert\b", re.IGNORECASE),
    re.compile(r"\bexperienced\b", re.IGNORECASE),
    re.compile(r"\bhead\s+of\b", re.IGNORECASE),
    re.compile(r"\bdirector\b", re.IGNORECASE),
    re.compile(r"\bvp\b", re.IGNORECASE),
    re.compile(r"\bcto\b", re.IGNORECASE),
    re.compile(r"\btech\s+lead\b", re.IGNORECASE),
    re.compile(r"\bteam\s+lead\b", re.IGNORECASE),
    re.compile(r"\bsoftware\s+engineer\s+(?:iii|3|iv|4)\b", re.IGNORECASE),
]

GRADUATE_PATTERNS = [
    re.compile(r"\bgraduate\s+program\b", re.IGNORECASE),
    re.compile(r"\byoung\s+graduate\b", re.IGNORECASE),
    re.compile(r"\bjeune\s+diplômé[es]?\b", re.IGNORECASE),
    re.compile(r"\bjeune\s+diplome[es]?\b", re.IGNORECASE),
    re.compile(r"\bnew\s+grad\b", re.IGNORECASE),
    re.compile(r"\bfresh\s+graduate\b", re.IGNORECASE),
    re.compile(r"\bentry\s+level\b", re.IGNORECASE),
    re.compile(r"\bdébutant\b", re.IGNORECASE),
    re.compile(r"\bdebutant\b", re.IGNORECASE),
    re.compile(r"\b0-1\s*(?:year|years|ans?)\b", re.IGNORECASE),
    re.compile(r"\b0\s*à\s*1\s*an[s]?\b", re.IGNORECASE),
]

JUNIOR_PATTERNS = [
    re.compile(r"\bjunior\b", re.IGNORECASE),
    re.compile(r"\b1-2\s*(?:year|years|ans?)\b", re.IGNORECASE),
    re.compile(r"\b0-2\s*(?:year|years|ans?)\b", re.IGNORECASE),
    re.compile(r"\b1\s*à\s*2\s*ans?\b", re.IGNORECASE),
    re.compile(r"\b1\s*to\s*2\s*years?\b", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Non-technical domain patterns for demotion
# ---------------------------------------------------------------------------
NON_TECHNICAL_PATTERNS = [
    re.compile(r"\b(?:juriste|legal|droit)\b", re.IGNORECASE),
    re.compile(r"\b(?:affaires?\s+publiques|public\s+affairs)\b", re.IGNORECASE),
    re.compile(r"\b(?:account\s+manage(?:ment|r)|chargé[es]?\s+d['']?affaires?|grands?\s+comptes?)\b", re.IGNORECASE),
    re.compile(r"\b(?:sales|commercial[es]?|prospection|vendeur)\b", re.IGNORECASE),
    re.compile(r"\b(?:business\s+development|bdr|sdr)\b", re.IGNORECASE),
    re.compile(r"\b(?:marketing|communication|réseaux\s+sociaux|social\s+media|brand)\b", re.IGNORECASE),
    re.compile(r"\b(?:planne?ur[es]?\s+stratégique|strategic\s+plann?ing)\b", re.IGNORECASE),
    re.compile(r"\b(?:event\s+operations?|événementiel|events?)\b", re.IGNORECASE),
    re.compile(r"\b(?:hr|human\s+resources|ressources\s+humaines|recrutement|recruiter|recruiting|talent)\b", re.IGNORECASE),
    re.compile(r"\b(?:finance|accounting|comptabilité|comptable|audit)\b", re.IGNORECASE),
    re.compile(r"\b(?:achats|procurement|approvisionneur|acheteur)\b", re.IGNORECASE),
    re.compile(r"\b(?:hospitality|receptionist|hôte(?:sse)?)\b", re.IGNORECASE),
    # --- Phase 3.4.2 additions ---
    # Product management (guards: "product engineer/developer" still has tech keyword in title)
    re.compile(r"\b(?:product\s+manage(?:ment|r)|chef\s+de\s+produit)\b", re.IGNORECASE),
    # Corporate development / M&A analyst (not "software developer")
    re.compile(r"\b(?:corporate\s+development|analyst\s+(?:m&a|business))\b", re.IGNORECASE),
    # Customer/assistant satisfaction — explicit role label only
    re.compile(r"\b(?:customer\s+satisfaction|assistant\s+satisfaction|charg[eé][es]?\s+(?:de\s+la\s+)?satisfaction)\b", re.IGNORECASE),
    # Partnerships+operations or partnerships+strategy compound only.
    # "partnerships" alone is NOT matched to avoid demoting "Partnerships Developer".
    # Protection: if title also contains a HIGH_TECH keyword (engineer/developer) the
    # NON_TECH gate is skipped (is_non_tech_domain and not title_has_high_tech check).
    re.compile(r"\b(?:partenariats?\s+(?:op[eé]rations?|strat[eé]g|commerciaux?)|partnerships?\s+(?:operations?|strategy|commercial))\b", re.IGNORECASE),
    # Operations+strategy as a compound phrase.
    # NOT just "strategy" alone — preserves "Technology Strategy Engineer",
    # "Data Strategy", "Cloud Strategy", "DevOps Strategy".
    re.compile(r"\b(?:op[eé]rations?\s+(?:et\s+)?strat[eé]g|operations?\s+(?:and\s+)?strategy)\b", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Engineering / technology / location terms
# ---------------------------------------------------------------------------
ENGINEERING_TERMS = {
    "backend": 12,
    "back-end": 12,
    "software engineer": 11,
    "software developer": 10,
    "developpeur": 8,
    "développeur": 8,
    "full stack": 8,
    "api": 6,
    "web": 4,
}

TECH_TERMS = {
    ".net": 10,
    "c#": 10,
    "asp.net": 10,
    "java": 8,
    "spring": 8,
    "python": 8,
    "fastapi": 8,
    "django": 7,
    "node": 7,
    "react": 6,
    "angular": 6,
    "sql": 6,
    "postgresql": 6,
    "mysql": 5,
    "docker": 6,
    "kubernetes": 5,
    "git": 4,
    "cloud": 5,
    "azure": 5,
    "aws": 5,
}

LOCATION_TERMS = {
    "casablanca": 8,
    "rabat": 8,
    "oujda": 8,
    "tanger": 8,
    "tangier": 8,
    "marrakech": 8,
    "agadir": 8,
    "fes": 8,
    "fez": 8,
    "meknes": 8,
    "kenitra": 8,
    "mohammedia": 8,
    "nador": 8,
    "el jadida": 8,
    "settat": 8,
    "tetouan": 8,
    "safi": 8,
    "beni mellal": 8,
    "taza": 8,
    "dakhla": 8,
    "laayoune": 8,
    "nouaceur": 8,
    "morocco": 8,
    "maroc": 8,
    "paris": 7,
    "lyon": 7,
    "toulouse": 7,
    "nantes": 7,
    "bordeaux": 7,
    "france": 7,
    "remote": 6,
    "télétravail": 6,
    "teletravail": 6,
    "home based": 6,
    "worldwide": 6,
    "anywhere": 5,
    "emea": 5,
    "hybrid": 3,
    "canada": 3,
    "montreal": 3,
    "toronto": 3,
    "switzerland": 3,
    "suisse": 3,
    "geneva": 3,
    "zurich": 3,
    "belgium": 3,
    "belgique": 3,
    "brussels": 3,
    "bruxelles": 3,
    "luxembourg": 3,
    "germany": 3,
    "allemagne": 3,
    "berlin": 3,
    "munich": 3,
    "netherlands": 3,
    "pays-bas": 3,
    "amsterdam": 3,
    "united kingdom": 3,
    "uk": 3,
    "london": 3,
    "ireland": 3,
    "irlande": 3,
    "dublin": 3,
    "spain": 3,
    "espagne": 3,
    "madrid": 3,
    "barcelona": 3,
    "italy": 3,
    "italie": 3,
    "milan": 3,
    "rome": 3,
    "portugal": 3,
    "lisbon": 3,
    "lisbonne": 3,
}

VISA_WARNING_PATTERNS = [
    re.compile(r"\b(?:must\s+have|require[sd]?)\s+(?:valid\s+)?work\s+authorization\b", re.IGNORECASE),
    re.compile(r"\b(?:legally\s+authorized\s+to\s+work|authorized\s+to\s+work\s+in)\b", re.IGNORECASE),
    re.compile(r"\b(?:no|not\s+provide|without|unable\s+to|cannot)\s+(?:visa\s+)?sponsorship\b", re.IGNORECASE),
    re.compile(r"\b(?:visa\s+)?sponsorship\s+is\s+not\s+(?:provided|available)\b", re.IGNORECASE),
    re.compile(r"\b(?:u\.?s\.?\s+citizenship|security\s+clearance)\s+required\b", re.IGNORECASE),
    re.compile(r"\b(?:must\s+reside\s+in|must\s+be\s+based\s+in|only\s+open\s+to\s+residents\s+of)\b", re.IGNORECASE),
    re.compile(r"\b(?:right\s+to\s+work\s+in|valid\s+work\s+permit\s+required)\b", re.IGNORECASE),
    re.compile(r"\b(?:autorisation\s+de\s+travail\s+obligatoire|titre\s+de\s+s[eé]jour\s+valide\s+requis|pas\s+de\s+(?:sponsorisation|parrainage)\s+de\s+visa)\b", re.IGNORECASE),
]

_FR_TOKENS = frozenset({"stage", "stagiaire", "developpeur", "développeur", "etudes", "études", "pour", "avec", "dans", "nous", "vous", "recherche", "missions"})
_EN_TOKENS = frozenset({"internship", "intern", "developer", "engineer", "software", "with", "for", "looking", "team", "skills", "experience", "building"})

SENIORITY_PENALTIES = {
    "senior": -22,
    "lead": -16,
    "principal": -18,
    "manager": -12,
    "architect": -12,
    "expert": -10,
    "staff": -14,
    "confirmed": -10,
    "experienced": -8,
}

TITLE_SENIORITY_KEYWORDS = frozenset({
    "senior", "lead", "principal", "manager", "architect",
    "staff", "confirmed", "expert", "experienced",
})

_TITLE_YEARS_RE = re.compile(r"\b([3-9]|10)\+?\s*(?:years?|ans)\b", re.IGNORECASE)
DURATION_RE = re.compile(r"\b([4-6])\s*(?:mois|months?|m)\b", re.IGNORECASE)
YEARS_RE = re.compile(r"\b([3-9]|10)\+?\s*(?:years?|ans)\b", re.IGNORECASE)


CDI_FULLTIME_PATTERNS = [
    re.compile(r"\bemployment_type\s*=\s*(?:full_time|permanent|direct_hire)\b", re.IGNORECASE),
    re.compile(r"\bemploymenttype\s*[:=]\s*(?:full_time|permanent|direct_hire)\b", re.IGNORECASE),
    re.compile(r"\bcontract_type\s*[:=]\s*cdi\b", re.IGNORECASE),
    re.compile(r"\bcontrat\s*[:=]\s*cdi\b", re.IGNORECASE),
    re.compile(r"\bcontrat\s+à\s+durée\s+indéterminée\b", re.IGNORECASE),
    re.compile(r"\bcontrat\s+a\s+duree\s+indeterminee\b", re.IGNORECASE),
    re.compile(r"\bcontrat\s+cdi\b", re.IGNORECASE),
    re.compile(r"\bpermanent\s+position\b", re.IGNORECASE),
    re.compile(r"\bfull-?time\s+position\b", re.IGNORECASE),
    re.compile(r"\btemps\s+plein\b", re.IGNORECASE),
]

EXPIRED_PATTERNS = [
    re.compile(r"\bno\s+longer\s+accepting\s+applications\b", re.IGNORECASE),
    re.compile(r"\bjob\s+(?:is\s+)?closed\b", re.IGNORECASE),
    re.compile(r"\boffre\s+(?:n['’]est\s+plus\s+disponible|expirée|fermée)\b", re.IGNORECASE),
    re.compile(r"\bthis\s+position\s+has\s+been\s+filled\b", re.IGNORECASE),
    re.compile(r"\barchived\s+job\b", re.IGNORECASE),
]

TECH_PATTERNS = [
    (re.compile(r"(?:\.net\b|\bdotnet\b)", re.IGNORECASE), ".net", 10),
    (re.compile(r"(?:c#|\bc-sharp\b)", re.IGNORECASE), "c#", 10),
    (re.compile(r"\basp\.net\b", re.IGNORECASE), "asp.net", 10),
    (re.compile(r"\bjava\b", re.IGNORECASE), "java", 8),
    (re.compile(r"\bspring\s*boot\b|\bspring\b", re.IGNORECASE), "spring", 8),
    (re.compile(r"\bpython\b", re.IGNORECASE), "python", 8),
    (re.compile(r"\bfastapi\b", re.IGNORECASE), "fastapi", 8),
    (re.compile(r"\bdjango\b", re.IGNORECASE), "django", 7),
    (re.compile(r"\bnode(?:\.js)?\b", re.IGNORECASE), "node", 7),
    (re.compile(r"\breact(?:\.js)?\b", re.IGNORECASE), "react", 6),
    (re.compile(r"\bangular(?:\.js)?\b", re.IGNORECASE), "angular", 6),
    (re.compile(r"\bsql\b", re.IGNORECASE), "sql", 6),
    (re.compile(r"\bpostgresql\b|\bpostgres\b", re.IGNORECASE), "postgresql", 6),
    (re.compile(r"\bmysql\b", re.IGNORECASE), "mysql", 5),
    (re.compile(r"\bdocker\b", re.IGNORECASE), "docker", 6),
    (re.compile(r"\bkubernetes\b|\bk8s\b", re.IGNORECASE), "kubernetes", 5),
    (re.compile(r"\bsymfony\b", re.IGNORECASE), "symfony", 7),
    (re.compile(r"\bphp\b", re.IGNORECASE), "php", 6),
    (re.compile(r"\bkafka\b", re.IGNORECASE), "kafka", 6),
    (re.compile(r"\bdevops\b", re.IGNORECASE), "devops", 6),
]

# ---------------------------------------------------------------------------
# Technical relevance patterns & hierarchy
# ---------------------------------------------------------------------------

HIGH_TECH_TITLE_PATTERNS = [
    re.compile(r"(?:\bsoftware\s+(?:engineer(?:ing)?|developer|dev)\b|\bd[eé]veloppeur\b|\bdeveloper\b|\bengineer\b|\bing[eé]nieur\b|\bbackend\b|\bback-end\b|\bfrontend\b|\bfront-end\b|\bfull\s*stack\b|\bfullstack\b|\bweb\s+developer\b|\bmobile\s+developer\b|\barchitecte\s+logiciel\b|\bsdet\b)", re.IGNORECASE),
    re.compile(r"(?:\.net\b|\bdotnet\b|c#|\bc-sharp\b|\basp\.net\b|\bjava\b|\bspring\b|\bpython\b|\bphp\b|\bsymfony\b|\breact\b|\bangular\b|\bnode\b)", re.IGNORECASE),
    re.compile(r"(?:\bdevops\b|\bqa\b|\bquality\s+assurance\b|\btest\s+automation\b|\bautomatisation\s+des?\s+tests?\b|\bdata\s+engineer(?:ing)?\b|\bcloud\s+engineer(?:ing)?\b)", re.IGNORECASE),
    re.compile(r"(?:\bsolutions?\s+engineer\b|\bsales\s+engineer\b|\btechnical\s+account\b|\bdevops\s+consultant\b|\btechnical\s+product\b)", re.IGNORECASE),
]

HIGH_TECH_BODY_PATTERNS = [
    re.compile(r"(?:\.net\b|\bdotnet\b|c#|\bc-sharp\b|\basp\.net\b|\bentity\s+framework\b|\bef\s+core\b|\bblazor\b)", re.IGNORECASE),
    re.compile(r"(\bjava\b|\bspring\b|\bspring\s*boot\b|\bhibernate\b|\bj2ee\b|\bmaven\b|\bgradle\b)", re.IGNORECASE),
    re.compile(r"(\bpython\b|\bdjango\b|\bfastapi\b|\bflask\b|\bpytest\b|\bpandas\b|\bnumpy\b)", re.IGNORECASE),
    re.compile(r"(?:\bnode(?:\.js)?\b|\breact(?:\.js)?\b|\bangular(?:\.js)?\b|\bvue(?:\.js)?\b|\bjavascript\b|\btypescript\b|\bexpress(?:\.js)?\b|\bnest(?:\.js)?\b|\bnext(?:\.js)?\b)", re.IGNORECASE),
    re.compile(r"(\bphp\b|\bsymfony\b|\blaravel\b)", re.IGNORECASE),
    re.compile(r"(\bc\+\+\b|\bgolang\b|\bgo\s+language\b|\brust\b|\bruby\b|\brails\b|\bflutter\b|\breact\s+native\b|\bkotlin\b|\bswift\b)", re.IGNORECASE),
    re.compile(r"(?:\bsql\b|\bpostgresql\b|\bpostgres\b|\bmysql\b|\bmongodb\b|\boracle\b|\bsql\s+server\b|\bt-sql\b|\bpl/sql\b|\bredis\b|\belasticsearch\b)", re.IGNORECASE),
    re.compile(r"(?:\bdocker\b|\bkubernetes\b|\bk8s\b|\bdevops\b|\bci/cd\b|\bjenkins\b|\bgitlab\b|\bgithub\s+actions\b|\bterraform\b|\bansible\b|\baws\b|\bazure\b|\bgcp\b)", re.IGNORECASE),
    re.compile(r"(?:\bqa\b|\bquality\s+assurance\b|\btest\s+automation\b|\bautomatisation\s+des?\s+tests?\b|\bselenium\b|\bcypress\b|\bplaywright\b|\bpostman\b|\btesteur\b)", re.IGNORECASE),
    re.compile(r"(?:\bdata\s+engineering?\b|\bdata\s+scienc(?:e|tist)\b|\bmachine\s+learning\b|\bdeep\s+learning\b|\bai\s+engineer\b|\bintelligence\s+artificielle\b|\bbig\s+data\b|\betl\b|\bspark\b)", re.IGNORECASE),
    re.compile(r"(?:\bsoftware\s+(?:engineer(?:ing)?|developer|dev)\b|\bd[eé]veloppeur\b|\bdeveloper\b|\bengineer\b|\bing[eé]nieur\b|\bbackend\b|\bback-end\b|\bfrontend\b|\bfront-end\b|\bfull\s*stack\b|\bfullstack\b|\bweb\s+developer\b|\bmobile\s+developer\b|\barchitecte\s+logiciel\b)", re.IGNORECASE),
]

MEDIUM_TECH_PATTERNS = [
    re.compile(r"(?:\binformatique\b|\binformation\s+technology\b|\bit\b|\bsyst[eè]mes?\s+d['’]informations?\b|\bsi\b|\bt[eé]l[eé]com\b|\br[eé]seau(?:x)?\s+(?:informatique|t[eé]l[eé]com|sans\s+fil|local|entreprise|d'entreprise)\b|\bnetwork(?:s|ing)?\b|\bsupport\s+it\b)", re.IGNORECASE),
    re.compile(r"(?:\bdatabase\b|\bbase\s+de\s+donn[eé]es\b|\bapi\b|\brest\b|\bgit\b|\bagile\b|\bscrum\b)", re.IGNORECASE),
]

FRONTEND_CODE_PATTERNS = [
    re.compile(r"(?:\bhtml\b|\bcss\b|\bjavascript\b|\btypescript\b|\bjs\b|\bts\b|\bangular\b|\breact\b|\bvue\b|\bfrontend\b|\bfront-end\b|\bd[eé]veloppeur\b|\bdeveloper\b|\bint[eé]gration\b|\bint[eé]grateur\b)", re.IGNORECASE),
]

UI_UX_PATTERNS = [
    re.compile(r"(?:\bui/ux\b|\bux/ui\b|\bdesigner\b|\bfigma\b|\bcharte\s+graphique\b|\bwireframes?\b)", re.IGNORECASE),
]


def _calculate_technical_relevance(title: str, description: str, full_text: str) -> tuple[str, int, list[str]]:
    title_lower = title.lower()
    desc_lower = description.lower()
    full_lower = full_text.lower()
    notes: list[str] = []

    # 1. Non-technical domain title demotion check
    is_non_tech_domain = any(pat.search(title_lower) for pat in NON_TECHNICAL_PATTERNS)
    title_has_high_tech = any(pat.search(title_lower) for pat in HIGH_TECH_TITLE_PATTERNS)

    if is_non_tech_domain and not title_has_high_tech:
        notes.append("Low technical relevance: non-technical domain title")
        return "LOW", 0, notes

    # 2. UI/UX design check
    has_ui_ux = any(pat.search(title_lower) or pat.search(desc_lower) for pat in UI_UX_PATTERNS)
    has_frontend_code = any(pat.search(title_lower) or pat.search(desc_lower) for pat in FRONTEND_CODE_PATTERNS)
    if has_ui_ux and not has_frontend_code:
        notes.append("Pure UI/UX design without code keywords")
        return "LOW", 0, notes

    # 3. Technical score evaluation with title priority
    tech_score = 0
    high_matches = 0

    if title_has_high_tech:
        tech_score += 40
        high_matches += 1

    body_high_matches = 0
    for pat in HIGH_TECH_BODY_PATTERNS:
        if pat.search(desc_lower):
            tech_score += 15
            body_high_matches += 1

    medium_matches = 0
    for pat in MEDIUM_TECH_PATTERNS:
        if pat.search(full_lower):
            tech_score += 5
            medium_matches += 1

    # 4. Final level classification
    if title_has_high_tech or body_high_matches >= 2:
        level = "HIGH"
        notes.append(f"High technical relevance ({high_matches + body_high_matches} tech matches)")
    elif body_high_matches >= 1 or medium_matches >= 1:
        level = "MEDIUM"
        notes.append("Medium/ambiguous technical relevance (general IT/sys intent)")
    else:
        level = "LOW"
        notes.append("Low technical relevance (no software/IT evidence)")

    return level, tech_score, notes


def classify_relevance_category(
    title: str, description: str, full_text: str, notes: str | None = None
) -> RelevanceCategory:
    title_lower = title.lower()
    desc_lower = description.lower()
    full_lower = full_text.lower()
    notes_lower = (notes or "").lower()
    combined_text = f"{full_lower} {notes_lower}"

    is_senior_title = _title_is_senior(title_lower)
    has_intern_title = any(pat.search(title_lower) for pat in INTERNSHIP_TITLE_PATTERNS) or any(pat.search(title_lower) for pat in PFE_TITLE_PATTERNS)

    if is_senior_title:
        return RelevanceCategory.SENIOR

    if YEARS_RE.search(full_lower) and not has_intern_title:
        return RelevanceCategory.SENIOR

    if not has_intern_title:
        for pat in SENIOR_PATTERNS:
            if pat.search(desc_lower):
                return RelevanceCategory.SENIOR

    # Check Expired Status
    is_expired = False
    if "valid_through=" in notes_lower:
        m_vt = re.search(r'valid_through=([^;]+)', notes_lower)
        if m_vt:
            vt_val = m_vt.group(1).strip()
            try:
                from datetime import date as dt_date
                vt_dt = datetime.strptime(vt_val[:10], "%Y-%m-%d").date()
                if vt_dt < dt_date.today():
                    is_expired = True
            except Exception:
                pass

    if not is_expired:
        for pat in EXPIRED_PATTERNS:
            if pat.search(combined_text):
                is_expired = True
                break

    if is_expired:
        return RelevanceCategory.FULL_TIME

    # Structured EmploymentType & Contract Precedence Rule
    has_full_time_override = False
    if (
        "employment_type=full_time" in notes_lower
        or "employmenttype: full_time" in notes_lower
        or "employmenttype=full_time" in notes_lower
        or "employment_type=permanent" in notes_lower
        or "contract_type=cdi" in notes_lower
    ):
        has_full_time_override = True

    if not has_full_time_override:
        for pat in CDI_FULLTIME_PATTERNS:
            if pat.search(combined_text) and not DURATION_RE.search(combined_text):
                has_full_time_override = True
                break

    if has_full_time_override:
        return RelevanceCategory.FULL_TIME

    if any(pat.search(title_lower) for pat in PFE_TITLE_PATTERNS):
        return RelevanceCategory.EXPLICIT_PFE

    if has_intern_title and any(pat.search(desc_lower) for pat in PFE_TITLE_PATTERNS):
        return RelevanceCategory.EXPLICIT_PFE

    if has_intern_title or any(pat.search(desc_lower) for pat in PFE_TITLE_PATTERNS):
        return RelevanceCategory.EXPLICIT_INTERNSHIP

    if any(pat.search(title_lower) for pat in GRADUATE_PATTERNS) or any(pat.search(desc_lower) for pat in GRADUATE_PATTERNS):
        return RelevanceCategory.GRADUATE

    if any(pat.search(title_lower) for pat in JUNIOR_PATTERNS) or any(pat.search(desc_lower) for pat in JUNIOR_PATTERNS):
        return RelevanceCategory.JUNIOR

    return RelevanceCategory.FULL_TIME


def map_score_to_tier(
    raw_subscore: int,
    category: RelevanceCategory | str,
    tech_level: str = "HIGH",
) -> int:
    if isinstance(category, str):
        try:
            category = RelevanceCategory(category)
        except ValueError:
            category = RelevanceCategory.FULL_TIME

    if tech_level == "HIGH":
        min_s, max_s = TIER_SCORE_BOUNDS[category]
    elif tech_level == "MEDIUM":
        if category in (RelevanceCategory.EXPLICIT_PFE, RelevanceCategory.EXPLICIT_INTERNSHIP):
            min_s, max_s = (35, 59)
        else:
            min_s, max_s = (35, 49)
    else:  # LOW
        if category in (RelevanceCategory.EXPLICIT_PFE, RelevanceCategory.EXPLICIT_INTERNSHIP):
            min_s, max_s = (15, 39)
        elif category in (RelevanceCategory.GRADUATE, RelevanceCategory.JUNIOR):
            min_s, max_s = (15, 34)
        elif category == RelevanceCategory.FULL_TIME:
            min_s, max_s = (10, 24)
        else:
            min_s, max_s = (0, 19)

    clamped_raw = max(0, min(100, raw_subscore))
    fraction = clamped_raw / 100.0
    scaled = min_s + fraction * (max_s - min_s)
    return int(round(scaled))


def score_opportunity(
    opportunity: OpportunityIn, criteria: SearchCriteria | None = None
) -> ScoreResult:
    title = (opportunity.title or "").lower()
    company = (opportunity.company or "").lower()
    location_str = (opportunity.location or "").lower()
    description = (opportunity.description or "").lower()
    notes_str = (opportunity.notes or "").lower()

    full_text = " ".join([title, company, location_str, description])

    category = classify_relevance_category(title, description, full_text, opportunity.notes)

    raw_score = 30
    hits: list[str] = []

    title_is_senior = (category == RelevanceCategory.SENIOR)

    pfe_from_title = 0
    pfe_from_desc = 0

    for term, weight in PFE_TERMS.items():
        in_title = term in title
        in_desc = term in description and not in_title

        if in_title:
            pfe_from_title += weight
            hits.append(f"+{weight} PFE (title): {term}")
        elif in_desc:
            if title_is_senior:
                hits.append(f"+0 PFE (suppressed, senior title): {term}")
            else:
                desc_weight = max(1, round(weight * _DESC_PFE_WEIGHT_FRACTION))
                pfe_from_desc += desc_weight
                hits.append(f"+{desc_weight} PFE (desc): {term}")

    raw_score += pfe_from_title + pfe_from_desc
    raw_score += _score_terms(full_text, ENGINEERING_TERMS, hits, "engineering")

    # Token-aware Technology Matching
    for pat, tech_name, weight in TECH_PATTERNS:
        if pat.search(full_text):
            raw_score += weight
            hits.append(f"+{weight} technology: {tech_name}")

    raw_score += _score_terms(full_text, LOCATION_TERMS, hits, "location")
    raw_score += _score_terms(full_text, SENIORITY_PENALTIES, hits, "seniority")

    tech_level, tech_score, tech_notes = _calculate_technical_relevance(title, description, full_text)
    hits.extend(tech_notes)

    # Apply Non-Technical Domain Penalty if title matches non-technical patterns
    is_non_tech_domain = any(pat.search(title) for pat in NON_TECHNICAL_PATTERNS)
    title_has_high_tech = any(pat.search(title) for pat in HIGH_TECH_TITLE_PATTERNS)
    if is_non_tech_domain and not title_has_high_tech:
        raw_score -= 30
        hits.append("-30 non-technical domain penalty")

    if YEARS_RE.search(full_text):
        raw_score -= 18
        hits.append("-18 experience requirement")

    if DURATION_RE.search(full_text):
        raw_score += 8
        hits.append("+8 internship duration")

    # Expired Posting Penalization
    is_expired = False
    expired_reason = ""
    if "valid_through=" in notes_str:
        m_vt = re.search(r'valid_through=([^;]+)', notes_str)
        if m_vt:
            vt_val = m_vt.group(1).strip()
            try:
                from datetime import date as dt_date
                vt_dt = datetime.strptime(vt_val[:10], "%Y-%m-%d").date()
                if vt_dt < dt_date.today():
                    is_expired = True
                    expired_reason = f"validThrough date ({vt_dt}) is in past"
            except Exception:
                pass

    if not is_expired:
        for pat in EXPIRED_PATTERNS:
            if pat.search(full_text):
                is_expired = True
                expired_reason = "Page content indicates job is closed"
                break

    if is_expired:
        raw_score -= 50
        hits.append(f"⛔ EXPIRED ({expired_reason})")

    if criteria:
        for technology in criteria.technologies:
            if technology.lower() in full_text:
                raw_score += 4
                hits.append(f"+4 requested technology: {technology}")
        for location in criteria.locations:
            if location.lower() in full_text:
                raw_score += 4
                hits.append(f"+4 requested location: {location}")

    warning = detect_work_authorization_warning(full_text)
    if warning:
        hits.append(f"⚠️ Visa/Auth Warning: {warning}")

    lang = detect_language(full_text)

    final_score = map_score_to_tier(raw_score, category, tech_level=tech_level)
    hits.append(f"Category: {category.value} (tech: {tech_level}, score {final_score})")

    reason = ", ".join(hits) if hits else "Baseline score; no strong PFE signals found."

    return ScoreResult(
        score=final_score,
        reason=reason,
        work_authorization_warning=warning,
        detected_language=lang,
        relevance_category=category.value,
    )


def detect_work_authorization_warning(text: str) -> str | None:
    """Detect if the job posting explicitly mentions work authorization/visa restrictions."""
    for pat in VISA_WARNING_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0).strip()
    return None


def detect_language(text: str) -> str:
    """Return 'fr' for French, 'en' for English, or 'unknown'."""
    words = set(re.findall(r"\b[a-zA-ZàâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ]+\b", text.lower()))
    fr_count = len(words & _FR_TOKENS)
    en_count = len(words & _EN_TOKENS)
    if fr_count > en_count and fr_count > 0:
        return "fr"
    if en_count > 0:
        return "en"
    return "unknown"


def _title_is_senior(title_lower: str) -> bool:
    """Return True if the job title itself signals a senior / non-internship role."""
    for pat in SENIOR_PATTERNS:
        if pat.search(title_lower):
            return True
    for kw in TITLE_SENIORITY_KEYWORDS:
        if kw in title_lower:
            return True
    if _TITLE_YEARS_RE.search(title_lower):
        return True
    return False


def _score_terms(text: str, terms: dict[str, int], hits: list[str], category: str) -> int:
    score = 0
    for term, weight in terms.items():
        if term in text:
            score += weight
            sign = "+" if weight > 0 else ""
            hits.append(f"{sign}{weight} {category}: {term}")
    return score
