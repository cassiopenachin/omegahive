from __future__ import annotations

import stat

import pytest

from omegahive_mcp.config import ConfigError, load_config, normalize_origin, write_config


def test_normalize_origin_accepts_a_plain_https_origin():
    assert normalize_origin("https://beastie.tail.ts.net:8443") == (
        "https://beastie.tail.ts.net:8443"
    )


def test_normalize_origin_strips_a_trailing_slash_and_keeps_a_path_prefix():
    assert normalize_origin("https://beastie.tail.ts.net:8443/omegahive/") == (
        "https://beastie.tail.ts.net:8443/omegahive"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ftp://beastie.tail.ts.net",
        "beastie.tail.ts.net:8443",
        "https://",
        "https://beastie.tail.ts.net?x=1",
        "https://beastie.tail.ts.net#frag",
        "https://user:pass@beastie.tail.ts.net",
    ],
)
def test_normalize_origin_refuses_anything_that_is_not_a_bare_https_or_http_origin(bad):
    with pytest.raises(ConfigError):
        normalize_origin(bad)


def test_write_then_load_round_trips(tmp_path):
    path = tmp_path / "config.json"

    written = write_config("https://beastie.tail.ts.net:8443/omegahive/", path)

    assert written == path
    assert load_config(path) == "https://beastie.tail.ts.net:8443/omegahive"


def test_write_config_sets_owner_only_permissions(tmp_path):
    path = tmp_path / "nested" / "config.json"

    write_config("https://beastie.tail.ts.net:8443", path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR
    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert dir_mode == stat.S_IRWXU


def test_load_config_refuses_when_the_file_is_absent(tmp_path):
    with pytest.raises(ConfigError, match="run `omegahive-mcp setup`"):
        load_config(tmp_path / "does-not-exist.json")


def test_load_config_refuses_malformed_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")

    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(path)


def test_load_config_refuses_a_missing_origin_field(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"other": "value"}')

    with pytest.raises(ConfigError, match="missing a string 'origin'"):
        load_config(path)
