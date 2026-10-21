"""config.py startup validation: search CSV, environment, writable paths.

Every failure is a ConfigError with an actionable message that never echoes
the Discord token. Fixture CSVs live under tests/fixtures/; tiny malformed
variants are written into tmp_path because the message text is the point.
"""

import os
from pathlib import Path

import pytest

import config

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
SEARCH_CSV = str(FIX / "smoke.csv")


def test_load_search_rows_accepts_template_shape() -> None:
    rows = config.load_search_rows(SEARCH_CSV)
    assert rows == [
        {
            "friendly_name": "Example search",
            "url": "https://sfbay.craigslist.org/search/sss?query=example",
        },
        {
            "friendly_name": "Second search",
            "url": "https://sfbay.craigslist.org/search/sss?query=second",
        },
    ]


def test_load_search_rows_handles_bom_and_extra_columns(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    _ = path.write_text(
        "\ufefffriendly_name,url,notes\n"
        "Example search,https://sfbay.craigslist.org/search/sss?query=bom,ignored\n",
        encoding="utf-8",
    )
    rows = config.load_search_rows(str(path))
    assert rows == [
        {
            "friendly_name": "Example search",
            "url": "https://sfbay.craigslist.org/search/sss?query=bom",
        }
    ]


def test_load_search_rows_rejects_missing_headers() -> None:
    with pytest.raises(config.ConfigError) as raised:
        config.load_search_rows(str(FIX / "missing-headers.csv"))
    assert "missing required headers: friendly_name, url" in str(raised.value)


def test_load_search_rows_rejects_blank_fields_with_line_number(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError) as raised:
        config.load_search_rows(str(FIX / "bad.csv"))
    assert "line 2: blank friendly_name" in str(raised.value)
    path = tmp_path / "blank-url.csv"
    _ = path.write_text("friendly_name,url\nName,\n", encoding="utf-8")
    with pytest.raises(config.ConfigError) as raised:
        config.load_search_rows(str(path))
    assert "line 2: blank url" in str(raised.value)


def test_load_search_rows_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError) as raised:
        config.load_search_rows(str(tmp_path / "absent.csv"))
    assert "search CSV not found" in str(raised.value)


def test_search_url_policy(tmp_path: Path) -> None:
    accepted = (
        "https://sfbay.craigslist.org/search/sss?query=x",
        "https://craigslist.org/search/sss",
        "https://newhaven.craigslist.org/d/spaces+lake/search/spaces+lake",
        "https://SFBAY.Craigslist.ORG/search/sss",
    )
    refused = {
        "http://sfbay.craigslist.org/search/sss": "must be https",
        "https://craigslist.org.evil/search/sss": "host must be craigslist.org",
        "https://evil.example.com/search/sss": "host must be craigslist.org",
        "https://sfbay.craigslist.org/dss/sss": "path must be a Craigslist search path",
        "https://sfbay.craigslist.org/sss": "path must be a Craigslist search path",
    }
    for index, url in enumerate(accepted):
        path = tmp_path / f"ok-{index}.csv"
        _ = path.write_text(f"friendly_name,url\nRow {index},{url}\n", encoding="utf-8")
        rows = config.load_search_rows(str(path))
        assert rows == [{"friendly_name": f"Row {index}", "url": url}]
    for index, (url, needle) in enumerate(refused.items()):
        path = tmp_path / f"bad-{index}.csv"
        _ = path.write_text(f"friendly_name,url\nRow {index},{url}\n", encoding="utf-8")
        with pytest.raises(config.ConfigError) as raised:
            config.load_search_rows(str(path))
        assert needle in str(raised.value)


def test_search_csv_fixtures_use_the_same_policy() -> None:
    for name, needle in (
        ("bad-host.csv", "host must be craigslist.org"),
        ("bad-path.csv", "path must be a Craigslist search path"),
    ):
        with pytest.raises(config.ConfigError) as raised:
            config.load_search_rows(str(FIX / name))
        assert needle in str(raised.value)


def test_load_search_rows_rejects_non_utf8_bytes(tmp_path: Path) -> None:
    path = tmp_path / "binary.csv"
    _ = path.write_bytes(b"friendly_name,url\nbad,\xff\xfe\n")
    with pytest.raises(config.ConfigError) as raised:
        config.load_search_rows(str(path))
    assert "not valid UTF-8 text" in str(raised.value)


def test_header_only_csv_is_an_empty_search_set(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    _ = path.write_text("friendly_name,url\n", encoding="utf-8")
    assert config.load_search_rows(str(path)) == []


def test_load_env_prefers_process_environment(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "DISCORD_TOKEN=file-token\nCHANNELID=111\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("CHANNELID", raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "process-token")
    values = config.load_env(str(env_file))
    assert values["discord_token"] == "process-token"
    assert values["channel_id"] == 111


def test_load_env_falls_back_to_env_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("CHANNELID", raising=False)
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "DISCORD_TOKEN=file-token\nCHANNELID=222\n",
        encoding="utf-8",
    )
    values = config.load_env(str(env_file))
    assert values["discord_token"] == "file-token"
    assert values["channel_id"] == 222
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("CHANNELID", raising=False)
    with pytest.raises(config.ConfigError) as raised:
        config.load_env(str(tmp_path / "absent.env"))
    assert "DISCORD_TOKEN is missing" in str(raised.value)
    assert "never printed" in str(raised.value)


def test_load_env_never_echoes_the_token(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "s3cr3t-token")
    monkeypatch.delenv("CHANNELID", raising=False)
    with pytest.raises(config.ConfigError) as raised:
        config.load_env(None)
    assert "s3cr3t-token" not in str(raised.value)
    assert "CHANNELID is missing" in str(raised.value)


def test_load_env_channel_id_must_be_digits(monkeypatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("CHANNELID", raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    for bad in ("12x", "channel-name", "1" * 26):
        monkeypatch.setenv("CHANNELID", bad)
        with pytest.raises(config.ConfigError) as raised:
            config.load_env(None)
        assert "must be a numeric Discord channel ID" in str(raised.value)
    monkeypatch.setenv("CHANNELID", "1" * 25)
    assert config.load_env(None)["channel_id"] == int("1" * 25)


def test_ensure_writable_dir_creates_and_reports(tmp_path: Path) -> None:
    target = str(tmp_path / "state" / "nested")
    assert config.ensure_writable_dir(target) == target
    assert os.path.isdir(target)


def test_ensure_writable_dir_refuses_read_only_dir(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("root can write to a read-only directory")
    target = tmp_path / "locked"
    os.makedirs(target)
    os.chmod(target, 0o500)
    with pytest.raises(config.ConfigError) as raised:
        config.ensure_writable_dir(str(target))
    assert "not writable" in str(raised.value)


def test_ensure_writable_dir_refuses_uncreatable_path(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("root can create paths under a read-only parent")
    parent = tmp_path / "parent"
    os.makedirs(parent)
    os.chmod(parent, 0o500)
    with pytest.raises(config.ConfigError) as raised:
        config.ensure_writable_dir(os.path.join(parent, "child"))
    assert "cannot create data directory" in str(raised.value)
