from pathlib import Path

from bigworld.coordinator.deploy import patch_lss_generation


def test_patch_lss_only_touches_generation_block(tmp_path: Path):
    p = tmp_path / "lss-server-config.yaml"
    p.write_text("enabled: true\ngeneration:\n  # Kommentar\n  enabled: true\n  rate: 80\nother:\n  enabled: true\n")
    assert patch_lss_generation(p)
    assert p.read_text() == "enabled: true\ngeneration:\n  # Kommentar\n  enabled: false\n  rate: 80\nother:\n  enabled: true\n"


def test_patch_lss_without_block(tmp_path: Path):
    p = tmp_path / "lss.yaml"
    p.write_text("enabled: true\n")
    assert not patch_lss_generation(p)
    assert p.read_text() == "enabled: true\n"
