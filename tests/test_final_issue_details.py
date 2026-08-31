from review_writer_core.final_issue_details import final_issue_details


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

