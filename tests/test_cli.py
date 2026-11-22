"""CLI guards that need no hardware."""
from lightferry import cli


def _key(tmp_path):
    k = tmp_path / "k.key"
    assert cli.main(["keygen", "--out", str(k)]) == 0
    return str(k)


def test_yes_rejected_with_webcam(tmp_path, capsys):
    rc = cli.main(["send", "--path", str(tmp_path), "--key-file", _key(tmp_path), "--yes"])
    assert rc == 2 and "--yes is a test hook" in capsys.readouterr().err


def test_dir_channel_with_audio_backchannel_rejected(tmp_path, capsys):
    (tmp_path / "f.txt").write_text("x")
    key, ch = _key(tmp_path), f"dir:{tmp_path}/ch"
    for argv in (["send", "--path", str(tmp_path / "f.txt"), "--backchannel", "audio"],
                 ["send", "--path", str(tmp_path / "f.txt"), "--no-camera"],
                 ["receive", "--path", str(tmp_path / "dst"), "--backchannel", "audio"]):
        assert cli.main([*argv, "--key-file", key, "--camera", ch]) == 2, argv
        assert "camera back-channel" in capsys.readouterr().err


def test_auto_confirm_only_on_test_channels():
    ap = cli.build_parser()
    assert not cli._auto(ap.parse_args(["--send", "--camera", "webcam"]))
    assert cli._auto(ap.parse_args(["--send", "--camera", "dir:/x"]))
    assert cli._auto(ap.parse_args(["--send", "--camera", "dir:/x", "--yes"]))
