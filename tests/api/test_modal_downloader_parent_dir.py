import os

from src.modal_apps.modal_downloader import ensure_parent_dir


def test_bare_filename_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ensure_parent_dir("model.safetensors")


def test_nested_path_creates_parent(tmp_path):
    target = tmp_path / "a" / "b" / "model.safetensors"
    ensure_parent_dir(str(target))
    assert os.path.isdir(target.parent)
