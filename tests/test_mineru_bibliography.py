from __future__ import annotations

from review_writer_core.bibliography_audit import audit_bibliography
from review_writer_core.mineru_bibliography import (
    as_document_audit_extraction,
    extract_mineru_bibliography,
)
from review_writer_core.publication_metadata import PdfFirstPageText


def _extract(pdf_text: str) -> dict:
    result = extract_mineru_bibliography(
        [],
        "# A sufficiently descriptive article title\n",
        filename="article.pdf",
        pdf_first_page_text=pdf_text,
    )
    return result["fields"]


def test_recovers_sciencedirect_journal_header_from_pdf_page() -> None:
    fields = _extract(
        """Chinese Chemical Letters 35 (2024) 109617
        Contents lists available at ScienceDirect
        Chinese Chemical Letters
        journal homepage: www.elsevier.com/locate/cclet
        Article title"""
    )

    assert fields["journal"]["value"] == "Chinese Chemical Letters"
    assert fields["journal"]["source"] == "pdf_first_page_citation_header"
    assert fields["year"]["value"] == 2024
    assert fields["volume"]["value"] == "35"
    assert fields["article_number"]["value"] == "109617"


def test_recovers_acs_footer_and_removes_publisher_prefix() -> None:
    fields = _extract(
        """Article title
        Published on Web 05/07/2009
        10.1021/ja901528b CCC: $40.75 © 2009 American Chemical Society7212 9 J. AM. CHEM. SOC. 2009, 131, 7212–7213"""
    )

    assert fields["journal"]["value"] == "J. AM. CHEM. SOC"
    assert fields["year"]["value"] == 2009
    assert fields["pages"]["value"] == "7212-7213"


def test_recovers_rsc_cite_line_without_volume() -> None:
    fields = _extract(
        """Communication
        DOI: 10.1039/b008818h Chem. Commun., 2001, 441–442"""
    )

    assert fields["journal"]["value"] == "Chem. Commun"
    assert fields["year"]["value"] == 2001
    assert fields["pages"]["value"] == "441-442"
    assert "volume" not in fields


def test_explicit_journal_homepage_recovers_name_when_header_has_no_numbers() -> None:
    fields = _extract(
        """Contents lists available at ScienceDirect
        Tetrahedron
        journal homepage: www.elsevier.com/locate/tet
        A sufficiently descriptive article title"""
    )

    assert fields["journal"]["value"] == "Tetrahedron"
    assert fields["journal"]["source"] == "pdf_first_page_journal_homepage"


def test_does_not_treat_middle_page_reference_as_current_journal() -> None:
    middle = "\n".join(f"body line {index}" for index in range(30))
    fields = _extract(
        f"""A sufficiently descriptive article title
        First Author and Second Author
        {middle}
        Smith et al. Nature Chemistry 2020, 12, 100–110
        concluding body line
        another concluding body line"""
    )

    assert "journal" not in fields


def test_skips_reference_lines_and_uses_the_publisher_footer() -> None:
    fields = _extract(
        """Article title
        body
        Author, A.; Author, B. Angew. Chem. 1997, 36, 1750-1753.
        10262 J. Am. Chem. Soc. 1998, 120, 10262-10263
        S0002-7863(98)01299-2 CCC: $15.00 © 1998 American Chemical Society"""
    )

    assert fields["journal"]["value"] == "J. Am. Chem. Soc"
    assert fields["year"]["value"] == 1998


def test_recovers_split_acs_footer() -> None:
    fields = _extract(
        """Article title
        ORGANIC
        LETTERS
        2010
        Vol. 12, No. 18
        4050-4053
        10.1021/ol101544c © 2010 American Chemical Society"""
    )

    assert fields["journal"]["value"] == "ORGANIC LETTERS"
    assert fields["volume"]["value"] == "12"
    assert fields["issue"]["value"] == "18"
    assert fields["pages"]["value"] == "4050-4053"


def test_coordinate_regions_prioritize_split_header_over_footer_references() -> None:
    page = PdfFirstPageText(
        """ORGANIC
        LETTERS
        Article title 2010
        Vol. 12, No. 18
        4050-4053
        body
        cited therein. Rao, Y. S. Chem. Rev. 1976, 76, 625.""",
        header_text="""ORGANIC
        LETTERS
        Article title 2010
        Vol. 12, No. 18
        4050-4053""",
        body_text="body",
        footer_text="cited therein. Rao, Y. S. Chem. Rev. 1976, 76, 625.",
        extraction_mode="coordinate_layout",
    )

    fields = _extract(page)

    assert fields["journal"]["value"] == "ORGANIC LETTERS"
    assert fields["journal"]["evidence"]["source_location"] == "pdf_page_1_header"
    assert fields["year"]["value"] == 2010


def test_coordinate_footer_recovers_pipe_delimited_journal_and_adjacent_year() -> None:
    footer = """NATURE COMMUNICATIONS | 4:2450 | DOI: 10.1038/ncomms3450 | www.nature.com/naturecommunications 1
    & 2013 Macmillan Publishers Limited. All rights reserved."""
    page = PdfFirstPageText(
        f"Cadmium iodide-mediated allenylation of terminal alkynes with ketones\n{footer}",
        header_text="Cadmium iodide-mediated allenylation of terminal alkynes with ketones",
        body_text="Article body",
        footer_text=footer,
        extraction_mode="coordinate_layout",
    )

    fields = _extract(page)

    assert fields["journal"]["value"] == "NATURE COMMUNICATIONS"
    assert fields["journal"]["source"] == "pdf_first_page_pipe_footer"
    assert fields["journal"]["evidence"]["source_location"] == "pdf_page_1_footer"
    assert fields["year"]["value"] == 2013
    assert fields["volume"]["value"] == "4"
    assert fields["article_number"]["value"] == "2450"
    assert fields["doi"]["value"] == "10.1038/ncomms3450"


def test_pipe_delimited_body_text_is_not_treated_as_publication_footer() -> None:
    body = "NATURE COMMUNICATIONS | 4:2450 | DOI: 10.1038/ncomms3450"
    page = PdfFirstPageText(
        body,
        header_text="A sufficiently descriptive article title",
        body_text=body,
        footer_text="Page 1",
        extraction_mode="coordinate_layout",
    )

    fields = _extract(page)

    assert "journal" not in fields


def test_recovers_clean_citation_fields_and_byline() -> None:
    markdown = """pubs.acs.org/joc

# Synthesis of Multisubstituted Allenamides

Zhi Zhang, Miao-Miao Ji, Xiao-Feng Wu, and Jin-Bao Peng\\*<sup>1</sup>

Cite This: J. Org. Chem. 2024, 89, 9001−9010

ABSTRACT: A paper abstract.
"""
    blocks = [
        {
            "type": "text",
            "text": "Synthesis of Multisubstituted Allenamides",
            "page_idx": 0,
            "bbox": [10, 20, 500, 60],
        },
        {
            "type": "text",
            "text": "Zhi Zhang, Miao-Miao Ji, Xiao-Feng Wu, and Jin-Bao Peng*",
            "page_idx": 0,
            "bbox": [10, 70, 500, 90],
        },
    ]

    result = extract_mineru_bibliography(blocks, markdown, filename="paper.pdf")
    fields = result["fields"]

    assert fields["authors"]["value"] == [
        "Zhi Zhang",
        "Miao-Miao Ji",
        "Xiao-Feng Wu",
        "Jin-Bao Peng",
    ]
    assert fields["journal"]["value"] == "J. Org. Chem."
    assert fields["year"]["value"] == 2024
    assert fields["volume"]["value"] == "89"
    assert fields["pages"]["value"] == "9001-9010"
    assert fields["authors"]["verification_status"] == "confirmed"
    assert fields["authors"]["evidence"]["block_id"] == "mineru-block-1"
    audit_extraction = as_document_audit_extraction(result)
    assert audit_extraction["fields"]["authors"]["verification_status"] == "verified"
    assert audit_extraction["fields"]["authors"]["block_id"] == "mineru-block-1"

    metadata = {
        key: value
        for key, value in fields.items()
        if key
        in {
            "title",
            "authors",
            "journal",
            "year",
            "bibliographic_year",
            "volume",
            "pages",
            "doi",
        }
    }
    audit = audit_bibliography(
        metadata,
        connectors=[],
        network_mode="disabled",
        local_extraction={"status": "insufficient"},
    )
    assert audit["status"] == "verified"
    assert audit["verification_method"] == "mineru_local_document"
    assert not audit["network_lookup"]["used"]


def test_rejects_publisher_residue_as_authors() -> None:
    markdown = """# A Reliable Article Title

Vol., No. –

Received 1 May 2024; Accepted 2 June 2024

## Abstract
"""

    result = extract_mineru_bibliography([], markdown, filename="paper.pdf")

    assert result["fields"]["authors"]["value"] is None
    assert result["fields"]["authors"]["verification_status"] == "missing"
    assert "authors" in result["needs_agent_fields"]


def test_unlabelled_journal_mention_does_not_become_canonical_journal() -> None:
    markdown = """# A Reliable Article Title

Alice Author and Bob Chemist

This paper compares work previously published in Green Chemistry 2017.
"""

    result = extract_mineru_bibliography([], markdown, filename="paper.pdf")

    assert "journal" not in result["fields"]
    assert "journal" in result["needs_agent_fields"]


def test_pdf_first_page_recovers_byline_missing_from_mineru_markdown() -> None:
    title = (
        "Dimethylprolinol Versus Diphenylprolinol in CuBr2-Catalyzed "
        "Enantioselective Allenylation of Terminal Alkynols"
    )
    result = extract_mineru_bibliography(
        [],
        f"# {title}\n\nLaboratory of Molecular Recognition and Synthesis\n",
        filename="paper.pdf",
        pdf_first_page_text=(
            f"{title}\nDengke Ma\nXinyu Duan\nChunling Fu\nXin Huang*\n"
            "Shengming Ma* 0000-0002-2866-2431\n"
            "Abstract The reaction gives useful products.\n"
        ),
    )
    assert result["fields"]["authors"]["value"] == [
        "Dengke Ma",
        "Xinyu Duan",
        "Chunling Fu",
        "Xin Huang",
        "Shengming Ma",
    ]
    assert result["fields"]["authors"]["source"] == "pdf_first_page_byline"
