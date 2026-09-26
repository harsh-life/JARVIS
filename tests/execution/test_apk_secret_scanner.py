"""The ANDC-T11 APK scanner itself (tests/tools/check_apk_secrets.py) —
proof that it fires on what it exists to catch, so a green CI step means
something. Key-shaped values are assembled at runtime so none is committed."""

from __future__ import annotations

import zipfile

from tests.tools.check_apk_secrets import scan


def _apk(tmp_path, entries: dict[str, bytes]):
    path = tmp_path / "app.apk"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def test_a_clean_apk_passes(tmp_path):
    apk = _apk(tmp_path, {"classes.dex": b"dex\n035\x00 hello", "assets/device_mapping.json": b"{}"})
    assert scan(apk) == []


def test_a_groq_key_compiled_into_dex_is_found(tmp_path):
    """The donor's actual REPO-T2 failure: a Groq key in BuildConfig."""

    key = "gsk" + "_" + "Abc123" * 5
    apk = _apk(tmp_path, {"classes.dex": b"dex\x00" + key.encode() + b"\x00"})
    assert "classes.dex: groq_api_key" in scan(apk)


def test_a_stripe_style_key_compiled_into_dex_is_found(tmp_path):
    key = "sk" + "_live_" + "Abc123" * 4
    apk = _apk(tmp_path, {"classes.dex": b"dex\x00" + key.encode() + b"\x00"})
    assert "classes.dex: stripe_key" in scan(apk)


def test_a_cloud_key_in_resources_is_found(tmp_path):
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    apk = _apk(tmp_path, {"classes.dex": b"dex", "resources.arsc": key.encode()})
    assert any("resources.arsc: aws_access_key_id" in f for f in scan(apk))


def test_a_private_key_block_in_assets_is_found(tmp_path):
    block = "-----BEGIN " + "PRIVATE KEY-----\nMII...\n"
    apk = _apk(tmp_path, {"classes.dex": b"dex", "assets/k.pem": block.encode()})
    assert any("private_key_block" in f for f in scan(apk))


def test_server_only_configuration_names_are_found(tmp_path):
    apk = _apk(tmp_path, {"classes.dex": b"HYPERMIND_" + b"KEK"})
    assert any("server-only" in f for f in scan(apk))


def test_an_archive_without_dex_is_not_a_pass(tmp_path):
    apk = _apk(tmp_path, {"assets/x": b"nothing"})
    assert scan(apk)
