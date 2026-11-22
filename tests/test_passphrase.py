import pytest

from lightferry import cli


def test_same_phrase_same_key(monkeypatch):
    monkeypatch.delenv("LIGHTFERRY_KEY", raising=False)
    monkeypatch.delenv("LIGHTFERRY_PASSPHRASE", raising=False)
    a = cli.load_key(None, None, "correct horse battery")
    b = cli.load_key(None, None, "correct horse battery")
    assert a == b and len(a) == 32


def test_different_phrase_different_key():
    assert cli.derive_psk("correct horse battery") != cli.derive_psk("correct horse batterz")


def test_short_phrase_rejected():
    with pytest.raises(ValueError, match="too short"):
        cli.derive_psk("short")


def test_env_passphrase_used_only_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("LIGHTFERRY_KEY", raising=False)
    monkeypatch.setenv("LIGHTFERRY_PASSPHRASE", "correct horse battery")
    assert cli.load_key(None, None) == cli.derive_psk("correct horse battery")
    kf = tmp_path / "k"
    kf.write_bytes(bytes(range(32)))
    assert cli.load_key(str(kf), None) == bytes(range(32))  # key file wins over the env passphrase


def test_passphrase_conflicts_with_key(tmp_path):
    kf = tmp_path / "k"
    kf.write_bytes(bytes(range(32)))
    with pytest.raises(ValueError, match="cannot be combined"):
        cli.load_key(str(kf), None, "correct horse battery")


def test_passphrase_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LIGHTFERRY_KEY", raising=False)
    f = tmp_path / "pp"
    f.write_text("correct horse battery\n")
    assert cli.load_key(None, None, "@" + str(f)) == cli.derive_psk("correct horse battery")
