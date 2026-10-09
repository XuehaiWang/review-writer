"""Shared image response/configuration behavior, without network or real secrets."""
import os

import pytest

from review_writer_core.providers import decode_json_object, load_dotenv


@pytest.mark.parametrize("raw,expected", [(b'{"ok":true}', {"ok": True}), (b'{}', {})])
def test_decode_json_object(raw, expected):
    assert decode_json_object(raw, "image") == expected


@pytest.mark.parametrize("raw,message", [
    (b" \n", "empty response"), (b"<html>bad gateway</html>", "non-JSON"),
    (b"\xff", "non-JSON"), (b"[]", "expected an object"), (b"null", "expected an object"),
])
def test_invalid_provider_data_keeps_an_actionable_error(raw, message):
    with pytest.raises(RuntimeError, match=message):
        decode_json_object(raw, "test-provider")


def test_local_config_never_overrides_task_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_TEST_EXISTING", "task-value")
    monkeypatch.setenv("RW_TEST_LOCAL", "")
    monkeypatch.delenv("RW_TEST_LOCAL", raising=False)
    (tmp_path / ".env").write_text(
        '# comment\nRW_TEST_EXISTING=file-value\nexport RW_TEST_LOCAL="x=y"\nignored\n=value\n',
        encoding="utf-8",
    )
    load_dotenv(tmp_path)
    assert os.environ["RW_TEST_EXISTING"] == "task-value"
    assert os.environ["RW_TEST_LOCAL"] == "x=y"


def test_missing_local_config_is_optional(tmp_path):
    load_dotenv(tmp_path)
