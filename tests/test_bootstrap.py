from evccplan.__main__ import ensure_config


def test_creates_config_from_example(tmp_path):
    target = tmp_path / "sub" / "config.yaml"
    ensure_config(str(target))
    assert target.is_file() and "dry_run: true" in target.read_text()


def test_keeps_existing_config(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("dry_run: false\n")
    ensure_config(str(target))
    assert target.read_text() == "dry_run: false\n"
