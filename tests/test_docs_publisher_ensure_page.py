from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from publish_docs import DocsPublisher, Cfg, _label_for, PROPERTY_KEY, LEGACY_SRC_LABEL_PREFIX


class FakeConfluence:
    """A tiny stub Confluence client used for unit tests.

    It simulates only the methods used by DocsPublisher.ensure_page() and
    DocsPublisher._link_resolver().
    """

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str]] = []  # (space, parent_id, title, storage)
        self.updated: list[tuple[str, str, str, str, str]] = []  # (page_id, space, parent_id, title, storage)
        self.labeled: list[tuple[str, list[str]]] = []
        self.props: list[tuple[str, str, dict]] = []
        self.deleted_labels: list[tuple[str, str]] = []  # (page_id, label)

        self._create_fail_titles: set[str] = set()
        self._update_fail_titles: set[str] = set()
        self._find_by_title: dict[str, dict] = {}

    # --- controls ---
    def fail_create_for_title(self, title: str) -> None:
        self._create_fail_titles.add(title)

    def fail_update_for_title(self, title: str) -> None:
        self._update_fail_titles.add(title)

    def set_find_page(self, title: str, page: dict | None) -> None:
        if page is None:
            self._find_by_title.pop(title, None)
        else:
            self._find_by_title[title] = page

    # --- methods used by publisher ---
    def create_page(self, space: str, parent_id: str, title: str, storage: str):
        if title in self._create_fail_titles:
            raise RuntimeError("A page with this title already exists")
        self.created.append((space, str(parent_id), title, storage))
        return {"id": f"new:{len(self.created)}"}

    def update_page(self, page_id: str, space: str, parent_id: str, title: str, storage: str):
        if title in self._update_fail_titles:
            raise RuntimeError("A page with this title already exists")
        self.updated.append((str(page_id), space, str(parent_id), title, storage))
        return {"id": page_id}

    def add_labels(self, page_id: str, labels: list[str]) -> None:
        self.labeled.append((str(page_id), labels))

    def put_property(self, page_id: str, key: str, value: dict):
        self.props.append((str(page_id), key, value))
        return {"id": f"prop:{page_id}:{key}"}


    def delete_label(self, page_id: str, label: str) -> None:
        self.deleted_labels.append((str(page_id), str(label)))

    def find_page_by_title(self, space: str, title: str, *, expand: str = "ancestors"):
        return self._find_by_title.get(title)


def _mk_cfg(tmp_path: Path, *, base_url: str = "https://conf.company.ru/wiki") -> Cfg:
    return Cfg(
        base_url=base_url,
        space="DOC",
        docs_root_id="100",
        docs_dir=tmp_path / "docs",
        domain_title_map={},
        section_title_map={},
        options={"adopt_existing_by_title_under_root": True},
    )


def test_ensure_page_creates_with_collision_prefix(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path)
    pub = DocsPublisher(cfg, token="t")
    fake = FakeConfluence()
    pub.conf = fake  # type: ignore

    # First candidate title is taken, second (with prefix) is free.
    fake.fail_create_for_title("Hello")

    page_id = pub.ensure_page(
        key="file:docs/a.md",
        title="Hello",
        parent_id="100",
        storage="<p>x</p>",
        extra_labels=["md"],
        collision_prefix="DOCS",
    )

    assert page_id.startswith("new:")
    assert fake.created[0][2] == "DOCS · Hello"

    # labels include managed + extra (no src-hash labels anymore)
    assert all(not any(l.startswith(LEGACY_SRC_LABEL_PREFIX) for l in labels) for _pid, labels in fake.labeled)
    assert any("managed-docs" in labels for _pid, labels in fake.labeled)
    assert all("md" not in labels for _pid, labels in fake.labeled)
    # property written (hidden metadata)
    # property written (hidden metadata) and contains our meta_labels
    assert any(k == PROPERTY_KEY for _pid, k, _v in fake.props)
    v = next(v for _pid, k, v in fake.props if k == PROPERTY_KEY)
    assert "md" in (v.get("meta_labels") or [])


def test_ensure_page_uses_custom_managed_label(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path)
    cfg.options["managed_label"] = "team-a-docs"
    pub = DocsPublisher(cfg, token="t")
    fake = FakeConfluence()
    pub.conf = fake  # type: ignore

    page_id = pub.ensure_page(
        key="file:docs/a.md",
        title="Hello",
        parent_id="100",
        storage="<p>x</p>",
        extra_labels=["md"],
        collision_prefix="DOCS",
    )

    assert page_id.startswith("new:")
    assert any("team-a-docs" in labels for _pid, labels in fake.labeled)
    assert all("managed-docs" not in labels for _pid, labels in fake.labeled)


def test_ensure_page_updates_existing_by_property(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path)
    pub = DocsPublisher(cfg, token="t")
    fake = FakeConfluence()
    pub.conf = fake  # type: ignore

    key = "file:docs/a.md"
    pub.key_to_page[key] = "777"

    page_id = pub.ensure_page(
        key=key,
        title="Hello",
        parent_id="100",
        storage="<p>x</p>",
        extra_labels=["md"],
        collision_prefix="DOCS",
    )

    assert page_id == "777"
    assert fake.updated and fake.updated[0][0] == "777"
    assert fake.props and fake.props[0][0] == "777"




def test_ensure_page_updates_existing_by_legacy_label_and_migrates(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path)
    pub = DocsPublisher(cfg, token="t")
    fake = FakeConfluence()
    pub.conf = fake  # type: ignore

    key = "file:docs/a.md"
    lb = _label_for(key)
    pub.label_to_page[lb] = "777"  # legacy index

    page_id = pub.ensure_page(
        key=key,
        title="Hello",
        parent_id="100",
        storage="<p>x</p>",
        extra_labels=["md"],
        collision_prefix="DOCS",
    )

    assert page_id == "777"
    assert fake.updated and fake.updated[0][0] == "777"
    # legacy label должен быть удалён (чтобы не светился пользователям)
    assert ("777", lb) in fake.deleted_labels
    # и страница теперь доступна по property-карте
    assert pub.key_to_page[key] == "777"


def test_link_resolver_generates_relative_with_context_and_fragment(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path, base_url="https://conf.company.ru/wiki")
    pub = DocsPublisher(cfg, token="t")
    pub.path_to_page["docs/a/b.md"] = "123"

    out = pub._link_resolver("./b.md#sec", current_path="docs/a/c.md")
    assert out == "/wiki/pages/viewpage.action?pageId=123#sec"


def test_link_resolver_strips_rest_api_suffix(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path, base_url="https://conf.company.ru/wiki/rest/api")
    pub = DocsPublisher(cfg, token="t")
    pub.path_to_page["docs/a/b.md"] = "123"
    out = pub._link_resolver("b.md", current_path="docs/a/c.md")
    assert out == "/wiki/pages/viewpage.action?pageId=123"


def test_link_resolver_ignores_non_md_and_external(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path)
    pub = DocsPublisher(cfg, token="t")
    pub.path_to_page["docs/a/b.md"] = "123"

    assert pub._link_resolver("https://example.com/x", current_path="docs/a/c.md") is None
    assert pub._link_resolver("#sec", current_path="docs/a/c.md") is None
    assert pub._link_resolver("file.txt", current_path="docs/a/c.md") is None
