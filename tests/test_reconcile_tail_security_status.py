from __future__ import annotations

import copy

import pytest

from scripts.reconcile_tail_security_status import apply_review, audit_namechange_rows


def test_review_excludes_proposal_and_duplicate_but_keeps_source_immutable() -> None:
    payload = {
        "namechange": {
            "rows": [
                {"ts_code": "300132.SZ", "name": "旧名", "start_date": "20100101",
                 "end_date": "20260810", "ann_date": "20100101"},
                {"ts_code": "300132.SZ", "name": "新名", "start_date": "20260718",
                 "end_date": float("nan"), "ann_date": "20260718"},
                {"ts_code": "300132.SZ", "name": "新名", "start_date": "20260811",
                 "end_date": float("nan"), "ann_date": "20260718"},
            ]
        }
    }
    before = copy.deepcopy(payload)
    actions = [{
        "action": "exclude_from_active_intervals",
        "selector": {"ts_code": "300132.SZ", "name": "新名", "start_date": "20260718",
                     "end_date": None, "ann_date": "20260718"},
        "reason_code": "announced_proposal_not_effective_short_name",
        "reason": "officially effective later",
        "evidence": ["notice.pdf"],
    }]
    canonical, decisions = apply_review(payload, actions, {"notice.pdf": {"sha256": "abc"}})

    assert payload == before
    assert len(canonical["namechange"]["rows"]) == 2
    assert canonical["namechange"]["rows"][1]["start_date"] == "20260811"
    assert decisions[0]["source_row_sha256"]
    assert audit_namechange_rows(canonical["namechange"]["rows"])["overlap_pairs"] == 0


def test_review_fills_only_a_missing_announcement_date() -> None:
    payload = {"namechange": {"rows": [
        {"ts_code": "689009.SH", "name": "九号公司-UWD", "start_date": "20201029",
         "end_date": "20210418", "ann_date": None}
    ]}}
    actions = [{
        "action": "set_announcement_date",
        "selector": {"ts_code": "689009.SH", "name": "九号公司-UWD", "start_date": "20201029",
                     "end_date": "20210418", "ann_date": None},
        "announcement_date": "20201028",
        "reason_code": "official_listing_notice_publication_date",
        "reason": "official SSE notice",
        "evidence": ["listing.html"],
    }]

    canonical, decisions = apply_review(payload, actions, {"listing.html": {"sha256": "def"}})

    assert payload["namechange"]["rows"][0]["ann_date"] is None
    assert canonical["namechange"]["rows"][0]["ann_date"] == "20201028"
    assert decisions[0]["canonical_announcement_date"] == "20201028"
    assert audit_namechange_rows(canonical["namechange"]["rows"])["missing_announcement_date"] == 0


def test_review_refuses_ambiguous_or_unreviewed_source_rows() -> None:
    payload = {"namechange": {"rows": [
        {"ts_code": "000001.SZ", "name": "平安银行", "start_date": "19910403",
         "end_date": None, "ann_date": "19910403"},
        {"ts_code": "000001.SZ", "name": "平安银行", "start_date": "19910403",
         "end_date": None, "ann_date": "19910403"},
    ]}}
    action = {
        "action": "exclude_from_active_intervals",
        "selector": {"ts_code": "000001.SZ", "name": "平安银行", "start_date": "19910403",
                     "end_date": None, "ann_date": "19910403"},
        "reason_code": "duplicate",
        "reason": "reviewed",
        "evidence": ["notice.pdf"],
    }

    with pytest.raises(ValueError, match="exactly one source row"):
        apply_review(payload, [action], {"notice.pdf": {"sha256": "abc"}})


def test_namechange_audit_counts_overlap_and_pit_gaps() -> None:
    rows = [
        {"ts_code": "000001.SZ", "start_date": "20200101", "end_date": None,
         "ann_date": None},
        {"ts_code": "000001.SZ", "start_date": "20210101", "end_date": None,
         "ann_date": "20220101"},
    ]

    assert audit_namechange_rows(rows) == {
        "rows": 2,
        "missing_announcement_date": 1,
        "announcement_after_effective_start": 1,
        "overlap_pairs": 1,
    }
