from copy import deepcopy

from Pipeline.pipeline_settings import evaluate_job
from Pipeline.policy_guard import application_blockers, work_authorization_blocker
from Pipeline.tests.test_policy_guard import candidate, configured_settings, job, ready_project
from Pipeline.tests.conftest import project_root


def test_known_sponsorship_need_does_not_turn_advert_silence_into_refusal():
    user = candidate(work_authorization=[{"countries": ["ZZ"], "status": "sponsorship_required"}])
    assert work_authorization_blocker(job(sponsorship_status="unknown"), user) is None
    assert work_authorization_blocker(job(sponsorship_status="not_offered"), user)
    assert work_authorization_blocker(job(sponsorship_status="not_required"), user)
    assert work_authorization_blocker(job(sponsorship_status="offered"), user) is None


def test_unknown_candidate_authorization_still_blocks_silent_advert():
    assert work_authorization_blocker(job(country="YY"), candidate())


def test_vie_is_worldwide_and_allowance_exempt_but_belgium_is_excluded():
    settings = configured_settings(vie_enabled=True, minimum_salary={"amount": 99999, "currency": "EUR"})
    target = job(country="YY", is_vie=True, salary_amount=12000, salary_currency="USD")
    assert evaluate_job(target, settings)[0]
    assert not evaluate_job({**target, "country": "BE"}, settings)[0]
    assert not evaluate_job({**target, "is_vie": False}, settings)[0]
    assert not evaluate_job(target, {**settings, "vie_enabled": False})[0]


def test_excluded_end_employer_cannot_hide_behind_recruiter():
    settings = configured_settings(excluded_employers=["Synthetic Excluded Group"])
    assert not evaluate_job(job(company="Recruiter", end_employer="Synthetic Excluded Group"), settings)[0]
    assert evaluate_job(job(company="Recruiter", end_employer="Different Group"), settings)[0]


def test_final_readiness_rechecks_current_country_and_role_settings(project_root):
    store = ready_project(project_root)
    target = job()
    current = store.get_settings()["settings"]
    changed = deepcopy(current)
    changed["countries"][0]["enabled"] = False
    assert "country is not enabled" in application_blockers(project_root, target, changed)
    changed = deepcopy(current)
    changed["role_families"][0]["enabled"] = False
    assert "no enabled role family matched" in application_blockers(project_root, target, changed)
