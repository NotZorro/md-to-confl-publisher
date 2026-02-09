from __future__ import annotations

import os
from pathlib import Path

import pytest

from publish_docs import _norm_posix, guess_title, load_cfg, sanitize_label, extract_tag_labels, _parse_paths_file


def test_norm_posix_handles_windows_paths_and_dotdot() -> None:
    assert _norm_posix(r"docs\\a\\b\\file.md") == "docs/a/b/file.md"
    assert _norm_posix("./docs/a/../b/file.md") == "docs/b/file.md"


def test_guess_title_prefers_front_matter_then_h1_then_fallback() -> None:
    md_fm = "---\ntitle: From FM\n---\n# From H1\n"
    assert guess_title(md_fm, fallback="X") == "From FM"

    md_h1 = "# From H1\ntext\n"
    assert guess_title(md_h1, fallback="X") == "From H1"

    md_none = "no headings\n"
    assert guess_title(md_none, fallback="Fallback") == "Fallback"


def test_load_cfg_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yml = tmp_path / "publish.yml"
    yml.write_text(
        """
base_url: https://example.invalid
space: DOC
docs_root_id: 1
docs_dir: docs
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("CONF_BASE_URL", "https://conf.real/wiki")
    monkeypatch.setenv("CONF_SPACE", "REAL")
    monkeypatch.setenv("CONF_DOCS_ROOT_ID", "999")

    cfg = load_cfg(str(yml))
    assert cfg.base_url == "https://conf.real/wiki"
    assert cfg.space == "REAL"
    assert cfg.docs_root_id == "999"


def test_load_cfg_requires_docs_root_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yml = tmp_path / "publish.yml"
    yml.write_text("base_url: http://x\nspace: DOC\n", encoding="utf-8")
    monkeypatch.delenv("CONF_DOCS_ROOT_ID", raising=False)
    with pytest.raises(SystemExit):
        load_cfg(str(yml))


def test_sanitize_label_and_extract_tag_labels() -> None:
    assert sanitize_label("Service") == "service"
    assert sanitize_label("<domain_tag>") == "domain-tag"
    assert sanitize_label("a_b c") == "a-b-c"

    fm = {"tags": ["service", "api", "<protocol>", "<domain_tag>"]}
    assert extract_tag_labels(fm) == ["service", "api", "protocol", "domain-tag"]


def test_parse_paths_file_supports_plain_and_status_lines(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    f = tmp_path / "changed.txt"
    f.write_text(
        """
# comment
docs/a/b.md
M docs/c/d.md
A docs/e/f.md
D docs/g/h.md
R docs/old.md docs/new.md
R100 docs/old2.md docs/new2.md
""".strip(),
        encoding="utf-8",
    )

    changes = _parse_paths_file(f, docs_dir)
    # D is kept as a change record (publisher ignores it later)
    assert [c.op for c in changes] == ["M", "M", "A", "D", "R", "R"]
    assert changes[0].path == "docs/a/b.md"
    assert changes[4].path == "docs/old.md" and changes[4].new_path == "docs/new.md"
