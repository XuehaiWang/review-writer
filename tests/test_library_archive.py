import io
from pathlib import Path
import stat
import zipfile

import pytest

from review_writer_api.domain_services.library_archive import inspect_archive, open_archive
from review_writer_api.errors import WorkflowValidationError


def zip_bytes(entries):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return content.getvalue()


def inspect(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return inspect_archive(archive, 80 * 1024 * 1024)


def test_nested_folders_keep_same_named_pdfs_and_ignore_non_pdfs():
    pdfs, ignored = inspect(zip_bytes([
        ("one/paper.pdf", b"%PDF-1.7"), ("two/paper.PDF", b"%PDF-1.7"),
        ("metadata.json", b"{}"), ("nested.zip", b"zip"), ("__MACOSX/._paper.pdf", b"x"),
    ]))
    assert [entry.filename for entry in pdfs] == ["one/paper.pdf", "two/paper.PDF"]
    assert ignored == 3


@pytest.mark.parametrize("path", ["../paper.pdf", "/paper.pdf", "C:/paper.pdf", "a/../../paper.pdf", "a\\..\\paper.pdf"])
def test_archive_paths_never_escape(path):
    with pytest.raises(WorkflowValidationError, match="unsafe"):
        inspect(zip_bytes([(path, b"%PDF-")]))


def test_symlink_rejected_even_when_not_pdf():
    link = zipfile.ZipInfo("alias")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(WorkflowValidationError, match="linked"):
        inspect(zip_bytes([("paper.pdf", b"%PDF-"), (link, b"/outside")]))


@pytest.mark.parametrize("kind", ["encrypted", "ratio", "total", "pdf_size", "count"])
def test_archive_limits(kind):
    with zipfile.ZipFile(io.BytesIO(zip_bytes([("paper.pdf", b"%PDF-")]))) as archive:
        entry = archive.infolist()[0]
        if kind == "encrypted": entry.flag_bits |= 1
        if kind == "ratio": entry.file_size = 2 * 1024 * 1024
        if kind == "total": entry.file_size = 2 * 1024 * 1024 * 1024
        if kind == "pdf_size": entry.file_size = entry.compress_size = 81 * 1024 * 1024
        if kind == "count": archive.filelist *= 301
        with pytest.raises(WorkflowValidationError):
            inspect_archive(archive, 80 * 1024 * 1024)


def test_empty_and_malformed_zip(tmp_path):
    with pytest.raises(WorkflowValidationError, match="No PDF"):
        inspect(zip_bytes([("file.txt", b"x")]))
    path = tmp_path / "bad.zip"
    path.write_bytes(b"not zip")
    with pytest.raises(WorkflowValidationError, match="damaged"):
        open_archive(path)


def test_directory_entry_count_checked_before_allocating_zip_objects(tmp_path):
    path = tmp_path / "too-many.zip"
    path.write_bytes(zip_bytes([(f"{i}.txt", b"") for i in range(1001)]))
    with pytest.raises(WorkflowValidationError, match="safety"):
        open_archive(path)


def test_valid_archive_opens_under_bounds(tmp_path):
    path = tmp_path / "papers.zip"
    path.write_bytes(zip_bytes([("paper.pdf", b"%PDF-1.7")]))
    with open_archive(path) as archive:
        assert len(inspect_archive(archive, 80 * 1024 * 1024)[0]) == 1
