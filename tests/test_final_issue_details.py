from review_writer_core.final_issue_details import final_issue_details, without_permission_checks


def test_legacy_rights_only_report_keeps_reference_problems():
    report = {
        "valid": True, "release_ready": False,
        "figure_argument_findings": [{"figure_id": "F1", "issues": ["source_reuse_permission_unverified"]}],
        "warning_issues": ["figure_argument_closure_incomplete"],
        "release_integrity_issues": ["figure_rights_unresolved", "bibliography_identity_unresolved"],
        "bibliography_identity": {"papers": [{"paper_id": "P1", "verified": False, "missing_fields": ["journal"]}]},
    }
    cleaned = without_permission_checks(report)
    assert cleaned["release_ready"] is False
    assert final_issue_details(report) == [{"target_type": "reference", "target_id": "P1", "issues": ["journal"]}]
    assert report["figure_argument_findings"][0]["issues"] == ["source_reuse_permission_unverified"]


def test_permission_removal_preserves_real_figure_failures():
    report = {
        "figure_argument_findings": [{"figure_id": "F1", "issues": ["source_reuse_permission_unverified", "image_missing"]}],
        "warning_issues": ["figure_argument_closure_incomplete"],
        "release_integrity_issues": ["figure_rights_unresolved", "figure_evidence_binding_incomplete"],
    }
    assert final_issue_details(report) == [{"target_type": "figure", "target_id": "F1", "issues": ["image_missing"], "title": "F1"}]
    assert without_permission_checks(report)["warning_issues"] == ["figure_argument_closure_incomplete"]


def test_legacy_release_only_permission_gate_is_retired():
    findings = [{"figure_id": "F1", "issues": ["source_reuse_permission_unverified"]}]
    report = {"status": "released", "release_ready": False, "publication_status": "review_only",
              "release_integrity_issues": ["figure_rights_unresolved"],
              "validation_warning_issues": ["figure_argument_closure_incomplete"],
              "pending_issue_details": [{"target_type": "figure", "target_id": "F1", "issues": ["source_reuse_permission_unverified"]},
                                        {"target_type": "manuscript", "target_id": "current_final", "issues": ["figure_argument_closure_incomplete"]}]}
    cleaned = without_permission_checks(report, findings)
    assert cleaned["release_ready"] is True
    assert cleaned["publication_status"] == "release_ready"
    assert cleaned["pending_issue_details"] == []
    assert cleaned["validation_warning_issues"] == []
    report["validation_blocking_issues"] = ["image_missing"]
    assert without_permission_checks(report, findings)["release_ready"] is False


def test_final_issue_details_retains_concrete_targets() -> None:
    rows = final_issue_details(
        {
            "figure_argument_findings": [
                {"figure_id": "P001-F01", "issues": ["caption_missing"]}
            ],
            "claim_citation_mapping": {
                "issues": [
                    {"claim_id": "S02-p1-C01", "issues": ["claim_has_no_evidence_identity"]}
                ]
            },
            "bibliography_identity": {
                "papers": [
                    {"paper_id": "P002", "verified": False, "missing_fields": ["doi"]}
                ]
            },
        }
    )
    assert {(row["target_type"], row["target_id"]) for row in rows} == {
        ("figure", "P001-F01"),
        ("claim", "S02-p1-C01"),
        ("reference", "P002"),
    }
