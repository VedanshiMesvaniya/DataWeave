"""Regression tests for path-traversal defenses.

Covers the two vulnerabilities these helpers close:
  * upload filenames escaping the uploads dir (arbitrary file write)
  * the SPA catch-all serving files outside the web root (arbitrary file read,
    e.g. leaking .env)
"""

from pathlib import Path

import pytest

from src.core.config import Settings
from src.core.paths import contained_path, safe_basename, unique_upload_dest


# ---------------------------------------------------------------------------
# safe_basename — used by the upload endpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("report.pdf", "report.pdf"),
        ("my report.pdf", "my report.pdf"),
        ("a/b/c.txt", "c.txt"),            # nested path flattened
        ("../../etc/passwd", "passwd"),    # dot-dot escape flattened
        ("/etc/passwd", "passwd"),         # absolute path flattened
        ("./legit.csv", "legit.csv"),
    ],
)
def test_safe_basename_flattens_to_basename(raw, expected):
    assert safe_basename(raw) == expected


@pytest.mark.parametrize("raw", ["", ".", "..", "/", "foo\x00.pdf", "\x00"])
def test_safe_basename_rejects_unsafe(raw):
    assert safe_basename(raw) is None


def test_safe_basename_result_never_escapes_upload_dir(tmp_path):
    """Whatever comes out must resolve to a direct child of the upload dir."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    for attack in ["../../etc/passwd", "/etc/shadow", "a/../../b/evil.py"]:
        name = safe_basename(attack)
        assert name is not None
        dest = (upload_dir / name).resolve()
        assert dest.parent == upload_dir.resolve()


# ---------------------------------------------------------------------------
# contained_path — used by the SPA catch-all
# ---------------------------------------------------------------------------

def test_contained_path_allows_files_inside_root(tmp_path):
    root = tmp_path / "frontend"
    (root / "assets").mkdir(parents=True)
    asset = root / "assets" / "app.js"
    asset.write_text("//")

    resolved = contained_path(root, "assets/app.js")
    assert resolved == asset.resolve()


@pytest.mark.parametrize(
    "attack",
    [
        "../.env",              # sibling secret
        "../../etc/passwd",     # deep escape
        "/etc/passwd",          # absolute reset
        "assets/../../secret",  # escape after legit prefix
        "",                     # empty
        "x\x00.js",             # NUL byte
    ],
)
def test_contained_path_blocks_escapes(tmp_path, attack):
    root = tmp_path / "frontend"
    root.mkdir()
    # Plant a secret one level up to prove it can't be reached.
    (tmp_path / ".env").write_text("SECRET=1")
    assert contained_path(root, attack) is None


def test_contained_path_root_itself_is_allowed(tmp_path):
    root = tmp_path / "frontend"
    root.mkdir()
    assert contained_path(root, ".") == root.resolve()


# ---------------------------------------------------------------------------
# unique_upload_dest — collision-proof per-upload storage
# ---------------------------------------------------------------------------

def test_unique_upload_dest_places_name_in_child_dir(tmp_path):
    dest = unique_upload_dest(tmp_path, "resume.pdf")
    assert dest.name == "resume.pdf"
    assert dest.parent.parent == tmp_path  # base/<token>/resume.pdf
    assert dest.parent.is_dir()            # subdir created eagerly


def test_unique_upload_dest_never_collides_for_same_name(tmp_path):
    """Two uploads sharing a filename must resolve to distinct paths.

    Regression: /upload/batch previously wrote every file to
    ``upload_dir/<name>`` directly, so two same-named files (or a batch file
    colliding with an existing upload) clobbered each other on disk.
    """
    dest_a = unique_upload_dest(tmp_path, "dup.txt")
    dest_b = unique_upload_dest(tmp_path, "dup.txt")

    assert dest_a != dest_b
    assert dest_a.parent != dest_b.parent

    # Writing different content to each must not overwrite the other.
    dest_a.write_text("A")
    dest_b.write_text("B")
    assert dest_a.read_text() == "A"
    assert dest_b.read_text() == "B"


# ---------------------------------------------------------------------------
# CORS origin parsing
# ---------------------------------------------------------------------------

def test_cors_default_is_not_wildcard():
    origins = Settings().cors_allow_origins_list
    assert "*" not in origins
    assert all(o.startswith("http") for o in origins)


def test_cors_origins_parsed_from_comma_string():
    s = Settings(cors_allow_origins="https://a.com, https://b.com ,, https://c.com")
    assert s.cors_allow_origins_list == ["https://a.com", "https://b.com", "https://c.com"]
