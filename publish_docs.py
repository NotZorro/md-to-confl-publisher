import argparse
import hashlib
import os
import re
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, unquote

import yaml

from confl_client import Confluence
from converter.md_to_confluence_storage import MdToConfluenceStorage, strip_front_matter

DEFAULT_MANAGED_LABEL = "managed-docs"
# Content property key used to store publisher metadata (hidden from normal users)
PROPERTY_KEY = "md2conf_source"
# Legacy visible label prefix kept only for one-time migration
LEGACY_SRC_LABEL_PREFIX = "src-"
# Previously we used visible labels for doc type (md/dir/section). Keep list for cleanup/migration.
VISIBLE_META_LABELS = ("md", "dir", "section")


_LABEL_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")


def sanitize_label(raw: str) -> str | None:
    """Sanitize a string to Confluence label rules.

    Confluence labels: lowercase a-z, 0-9, '-'.
    We also accept placeholders like "<protocol>" and strip angle brackets.
    """
    s = (raw or "").strip().lower()
    if not s:
        return None
    # common placeholders: <tag>
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    s = s.replace("_", "-").replace(" ", "-")
    s = _LABEL_SANITIZE_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or None


def extract_tag_labels(front_matter: dict | None) -> list[str]:
    """Extract 'tags' from YAML front matter and convert them into page labels."""
    if not isinstance(front_matter, dict):
        return []
    tags = front_matter.get("tags")
    out: list[str] = []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if isinstance(tags, (list, tuple)):
        for t in tags:
            s = sanitize_label(str(t))
            if s:
                out.append(s)
    # uniq preserve order
    seen: set[str] = set()
    res: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            res.append(t)
    return res


def managed_label_from_cfg(cfg: "Cfg") -> str:
    """Return the label used to mark pages as managed by this publisher.

    We keep exactly one visible label on pages (by default: "managed-docs").
    Teams may want a different label to avoid collisions when sharing a single
    Confluence space/root or to simplify cleanup.

    Config:
      options.managed_label: <string>

    The value is sanitized to Confluence label rules (lowercase a-z, 0-9, '-').
    """
    raw = (cfg.options.get("managed_label") or "").strip()
    if not raw:
        return DEFAULT_MANAGED_LABEL
    s = sanitize_label(raw)
    return s or DEFAULT_MANAGED_LABEL



def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _label_for(key: str) -> str:
    # Confluence label: lower-case, digits, hyphen
    return "src-" + _sha1(key)[:12]


def _norm_posix(p: str) -> str:
    # 1) слэши
    p = p.replace("\\", "/")
    # 2) схлопываем ./ и ../
    p = posixpath.normpath(p)
    # 3) убираем ведущие "./"
    if p.startswith("./"):
        p = p[2:]
    return p


def _humanize(seg: str) -> str:
    seg = seg.replace("_", " ").replace("-", " ").strip()
    if not seg:
        return seg
    return seg[0].upper() + seg[1:]


@dataclass
class Cfg:
    base_url: str
    space: str
    docs_root_id: str
    docs_dir: Path
    domain_title_map: dict
    section_title_map: dict
    options: dict


@dataclass
class Entry:
    path: Path
    current_path: str
    md_text: str
    title: str
    key: str
    parent_id: str
    collision_prefix: str
    # rename support (git name-status "R" lines)
    rename_from_key: str | None = None
    rename_from_path: str | None = None


@dataclass
class Change:
    op: str  # A/M/R/D
    path: str
    new_path: str | None = None


def _parse_paths_file(paths_file: Path, docs_dir: Path) -> list[Change]:
    """Parse a file with changed paths.

    Supported formats (one per line):
      - Plain path (treated as "M"):
          docs/domain/section/file.md
      - "A <path>" / "M <path>" / "D <path>"
      - Git name-status rename ("R", "R100", ...):
          R docs/old.md docs/new.md
          R100 docs/old.md docs/new.md

    Anything outside docs_dir is ignored.
    """
    out: list[Change] = []
    for raw in paths_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()

        # 1) plain path
        if len(parts) == 1 and (parts[0].endswith(".md") or ".md" in parts[0]):
            p = _norm_posix(parts[0])
            out.append(Change("M", p))
            continue

        # 2) status-prefixed
        op = parts[0]
        if op.startswith("R") and len(parts) >= 3:
            old_p = _norm_posix(parts[1])
            new_p = _norm_posix(parts[2])
            out.append(Change("R", old_p, new_p))
            continue

        if op in ("A", "M", "D") and len(parts) >= 2:
            p = _norm_posix(parts[1])
            out.append(Change(op, p))
            continue

        # ignore unknown lines

    # Normalize to paths under docs_dir (as stored in the repo).
    docs_root = _norm_posix(str(docs_dir))
    filtered: list[Change] = []
    for ch in out:
        def is_under_docs(p: str) -> bool:
            p = _norm_posix(p)
            # Accept both "docs/..." and "<docs_dir>/..."
            if p == docs_root or p.startswith(docs_root + "/"):
                return True
            if docs_dir.name == "docs" and p.startswith("docs/"):
                return True
            return False

        if ch.op == "R":
            if ch.new_path and is_under_docs(ch.new_path) and is_under_docs(ch.path):
                filtered.append(ch)
            continue
        if is_under_docs(ch.path):
            filtered.append(ch)

    return filtered


def load_cfg(path: str) -> Cfg:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    base_url = os.getenv("CONF_BASE_URL") or raw.get("base_url") or "http://localhost:8090"
    space = os.getenv("CONF_SPACE") or raw.get("space") or "DOC"
    docs_root_id = str(os.getenv("CONF_DOCS_ROOT_ID") or raw.get("docs_root_id") or "")
    if not docs_root_id:
        raise SystemExit("publish.yml must define docs_root_id (or set CONF_DOCS_ROOT_ID)")

    docs_dir = Path(raw.get("docs_dir") or "docs")
    domain_title_map = raw.get("domain_title_map") or {
        "product offering": "Product offering",
        "shopping-cart": "Shopping cart",
        "strategy": "Strategy",
    }
    section_title_map = raw.get("section_title_map") or {
        "ADR": "ADR",
        "features": "Features",
        "playbook": "Playbook",
        "algorithm-specs": "Algorithm specs",
        "context-diagram": "Context diagram",
        "descriptionOfAPI": "API description",
    }
    options = raw.get("options") or {}

    return Cfg(
        base_url=base_url,
        space=space,
        docs_root_id=docs_root_id,
        docs_dir=docs_dir,
        domain_title_map=domain_title_map,
        section_title_map=section_title_map,
        options=options,
    )


def guess_title(md_text: str, fallback: str) -> str:
    fm, body = strip_front_matter(md_text)
    if isinstance(fm, dict) and fm.get("title"):
        return str(fm["title"]).strip()
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if m:
        return m.group(1).strip()
    return fallback


def find_index_file(dir_path: Path) -> Path | None:
    for name in ("_index.md", "README.md", "readme.md"):
        p = dir_path / name
        if p.exists() and p.is_file():
            return p
    return None


def children_macro() -> str:
    return (
        "<ac:structured-macro ac:name=\"children\">"
        "<ac:parameter ac:name=\"sort\">title</ac:parameter>"
        "</ac:structured-macro>"
    )


class DocsPublisher:
    def __init__(self, cfg: Cfg, token: str):
        self.cfg = cfg
        self.conf = Confluence(cfg.base_url, token)
        self.managed_label = managed_label_from_cfg(cfg)

        toc = bool(cfg.options.get("toc", True))
        toc_min = int(cfg.options.get("toc_min", 1))
        toc_max = int(cfg.options.get("toc_max", 3))
        toc_style = str(cfg.options.get("toc_style", "none"))
        toc_outline = bool(cfg.options.get("toc_outline", False))
        heading_numbering = bool(cfg.options.get("heading_numbering", True))
        heading_numbering_max_level = int(cfg.options.get("heading_numbering_max_level", 3))

        self.conv_plain = MdToConfluenceStorage(
            inject_toc=toc,
            toc_min_level=toc_min,
            toc_max_level=toc_max,
            toc_style=toc_style,
            toc_outline=toc_outline,
            # H1 -> page title; shift headings up; remove manual numbering
            strip_title_h1=True,
            promote_headings=True,
            strip_heading_numbers=True,
            heading_numbering_in_text=heading_numbering,
            heading_numbering_css=False,
            heading_numbering_max_level=heading_numbering_max_level,
        )

        # заполняется после pass1
        self.path_to_page: dict[str, str] = {}

        # кэш существующих страниц под root
        self.key_to_page: dict[str, str] = {}
        # legacy: src-hash labels -> pageId (will be removed during migration)
        self.label_to_page: dict[str, str] = {}

    # -------- discovery --------
    def bootstrap_existing(self) -> None:
        """Собираем все страницы под docs_root.

        Раньше мы индексировали страницы по label вида src-<sha1...>, но это видно людям.
        Теперь primary-ключ хранится в content property (PROPERTY_KEY) и не светится в UI.
        При этом поддерживаем миграцию со старого режима: находим legacy src-* labels,
        строим мапу на текущий прогон и (если есть property с ключом) удаляем эти labels.
        """
        # Фильтруем сразу по managed label, иначе будем сканировать вообще всё в дереве
        cql = f'ancestor={self.cfg.docs_root_id} and type=page and label="{self.managed_label}"'

        migrate_legacy = bool(self.cfg.options.get("migrate_legacy_src_labels", True))

        for page in self.conf.cql_iter(cql, expand="metadata.labels,ancestors"):
            page_id = str(page["id"])
            labels = [l["name"] for l in (page.get("metadata", {}).get("labels", {}).get("results", []) or [])]

            # 1) primary index: content property
            src_key: str | None = None
            try:
                prop = self.conf.get_property(page_id, PROPERTY_KEY)
                if prop and isinstance(prop.get("value"), dict):
                    src_key = prop["value"].get("key")
            except Exception:
                prop = None

            # fallback: старое/общее имя "source" (если вдруг кто-то уже так публиковал)
            if not src_key:
                try:
                    prop2 = self.conf.get_property(page_id, "source")
                    if prop2 and isinstance(prop2.get("value"), dict):
                        src_key = prop2["value"].get("key")
                except Exception:
                    pass

            if src_key:
                self.key_to_page[str(src_key)] = page_id
                # IMPORTANT: in "publish only changed" mode we still need to resolve
                # links to *already existing* pages. For file:* keys we can reconstruct
                # the md path and fill path_to_page upfront.
                if str(src_key).startswith("file:"):
                    try:
                        p = _norm_posix(str(src_key)[len("file:"):])
                        if p:
                            self.path_to_page[p] = page_id
                    except Exception:
                        pass

            # 2) legacy index: src-* labels (нужен, чтобы найти страницу до миграции)
            legacy_src_labels = [lb for lb in labels if lb.startswith(LEGACY_SRC_LABEL_PREFIX)]
            for lb in legacy_src_labels:
                self.label_to_page[lb] = page_id

            # 3) миграция: если у страницы уже есть property с key, то src-* label больше не нужен
            if migrate_legacy and src_key and legacy_src_labels:
                for lb in legacy_src_labels:
                    try:
                        self.conf.delete_label(page_id, lb)
                    except Exception:
                        # не хотим ронять публикацию из-за прав/глюков удаления labels
                        pass


            # Также убираем видимые технические метки md/dir/section (метадата теперь в property)
            if bool(self.cfg.options.get("migrate_legacy_doc_labels", True)):
                for lb2 in VISIBLE_META_LABELS:
                    if lb2 in labels:
                        try:
                            self.conf.delete_label(page_id, lb2)
                        except Exception:
                            pass

    # -------- low-level upsert --------
    def _ensure_labels(self, page_id: str, labels: list[str]) -> None:
        self.conf.add_labels(page_id, labels)

    def _adopt_by_title_under_root(self, title: str) -> str | None:
        if not self.cfg.options.get("adopt_existing_by_title_under_root", True):
            return None
        found = self.conf.find_page_by_title(self.cfg.space, title, expand="ancestors,metadata.labels")
        if not found:
            return None
        # только если страница под нашим root
        ancestors = found.get("ancestors") or []
        if not any(str(a.get("id")) == str(self.cfg.docs_root_id) for a in ancestors):
            return None
        return found["id"]

    
    def ensure_page(
        self,
        *,
        key: str,
        title: str,
        parent_id: str,
        storage: str,
        extra_labels: list[str],
        page_labels: list[str] | None = None,
        collision_prefix: str | None = None,
    ) -> str:
        """Upsert по computed key (никто ничего руками не пишет).

        Confluence DC требует уникальные title в рамках space.
        Поэтому:
          - если title свободен — используем как есть (человеческий)
          - если коллизия — пробуем более “человеческий” вариант с префиксом
          - если и это занято — добавляем короткий хэш
        """

        page_labels = list(page_labels or [])

        def is_title_exists(err: str) -> bool:
            return "A page with this title already exists" in err or "already exists" in err

        def title_candidates(base: str) -> list[str]:
            # порядок важен
            out: list[str] = []
            base = (base or "").strip()
            if base:
                out.append(base)

            if collision_prefix:
                pref = str(collision_prefix).strip()
                if pref:
                    # не дублируем префикс, если он уже в начале
                    if not base.startswith(pref):
                        out.append(f"{pref} · {base}")

            short = _sha1(key)[:6]
            out.append(f"{base} [{short}]")

            # uniq preserve order
            seen = set()
            res = []
            for t in out:
                if t not in seen:
                    seen.add(t)
                    res.append(t)
            return res

        def try_update(page_id: str) -> str:
            last_err: RuntimeError | None = None
            for t in title_candidates(title):
                try:
                    self.conf.update_page(page_id, self.cfg.space, parent_id, t, storage)
                    return t
                except RuntimeError as e:
                    last_err = e
                    if is_title_exists(str(e)):
                        continue
                    raise
            # если все варианты заблокированы коллизией
            raise last_err or RuntimeError("Update failed (unknown)")

        def try_create() -> tuple[str, str]:
            last_err: RuntimeError | None = None
            for t in title_candidates(title):
                try:
                    created = self.conf.create_page(self.cfg.space, parent_id, t, storage)
                    return created["id"], t
                except RuntimeError as e:
                    last_err = e
                    if is_title_exists(str(e)):
                        # шанс усыновить (только если под root)
                        adopted_id = self._adopt_by_title_under_root(t)
                        if adopted_id:
                            used = try_update(adopted_id)
                            return adopted_id, used
                        continue
                    raise
            raise last_err or RuntimeError("Create failed (unknown)")

        lb = _label_for(key)  # legacy deterministic label (DO NOT publish)

        def write_source_meta(pid: str, used_title: str, meta_labels: list[str]) -> None:
            # Кладём source key + hash в content property (в UI обычно не видно).
            # И туда же прячем любые технические классификаторы (md/dir/section), чтобы люди их не видели.
            # Оставляем старое имя "source" для обратной совместимости/дебага.
            payload = {
                "key": key,
                "title": used_title,
                "src_hash": _sha1(key)[:12],
                "meta_labels": [str(x).strip().lower() for x in (meta_labels or []) if str(x).strip()],
            }
            self.conf.put_property(pid, PROPERTY_KEY, payload)
            self.conf.put_property(pid, "source", payload)
            if bool(self.cfg.options.get("migrate_legacy_doc_labels", True)):
                for lb2 in VISIBLE_META_LABELS:
                    try:
                        self.conf.delete_label(pid, lb2)
                    except Exception:
                        pass

        # 1) основной путь: нашли по content property (невидимо пользователю)
        page_id = self.key_to_page.get(key)
        if page_id:
            used_title = try_update(page_id)
            self._ensure_labels(page_id, [self.managed_label] + page_labels)
            write_source_meta(page_id, used_title, extra_labels)
            return page_id

        # 1b) legacy путь: нашли по src-* label (и мигрируем на property)
        legacy_id = self.label_to_page.get(lb)
        if legacy_id:
            used_title = try_update(legacy_id)
            self._ensure_labels(legacy_id, [self.managed_label] + page_labels)
            write_source_meta(legacy_id, used_title, extra_labels)
            try:
                self.conf.delete_label(legacy_id, lb)
            except Exception:
                pass
            self.key_to_page[key] = legacy_id
            return legacy_id

        # 2) попытка “усыновить” существующую страницу под нашим root (прошлые ручные/кривые прогоны)
        adopted_id = self._adopt_by_title_under_root(title)
        if adopted_id:
            used_title = try_update(adopted_id)
            self._ensure_labels(adopted_id, [self.managed_label] + page_labels)
            write_source_meta(adopted_id, used_title, extra_labels)
            # на всякий: если страница уже была в legacy-режиме
            try:
                self.conf.delete_label(adopted_id, lb)
            except Exception:
                pass
            self.key_to_page[key] = adopted_id
            return adopted_id

        # 3) create (с обработкой коллизий)
        page_id, used_title = try_create()
        self._ensure_labels(page_id, [self.managed_label] + page_labels)
        write_source_meta(page_id, used_title, extra_labels)
        self.key_to_page[key] = page_id
        return page_id
# -------- directory pages --------
    def ensure_domain_and_sections(self) -> dict[tuple[str, str], str]:
        """Создаёт доменные страницы и страницы разделов. Возвращает map (domain, section)->pageId"""
        docs_dir = self.cfg.docs_dir
        if not docs_dir.exists():
            raise SystemExit(f"Docs dir not found: {docs_dir}")

        section_pages: dict[tuple[str, str], str] = {}

        for domain_dir in sorted([p for p in docs_dir.iterdir() if p.is_dir()]):
            domain = domain_dir.name
            # игнорируем мусор типа docs/adr
            if domain not in self.cfg.domain_title_map:
                continue

            domain_title = self.cfg.domain_title_map.get(domain) or _humanize(domain)

            domain_key = f"dir:{_norm_posix(f'docs/{domain}') }"

            dom_index = find_index_file(domain_dir)
            if dom_index:
                dom_text = dom_index.read_text(encoding="utf-8")
                dom_storage = self.conv_plain.convert(dom_text, current_path=_norm_posix(str(dom_index))).storage
            else:
                dom_storage = f"<p>{domain_title}</p>"

            dom_storage = dom_storage + children_macro()

            domain_page_id = self.ensure_page(
                key=domain_key,
                title=domain_title,
                parent_id=self.cfg.docs_root_id,
                storage=dom_storage,
                extra_labels=["dir"],
                collision_prefix="DOCS",
            )

            for sec_dir in sorted([p for p in domain_dir.iterdir() if p.is_dir()]):
                section = sec_dir.name
                section_title_raw = self.cfg.section_title_map.get(section) or _humanize(section)
                section_title = f"{domain_title} · {section_title_raw}"

                sec_key = f"dir:{_norm_posix(f'docs/{domain}/{section}') }"

                sec_index = find_index_file(sec_dir)
                if sec_index:
                    sec_text = sec_index.read_text(encoding="utf-8")
                    sec_storage = self.conv_plain.convert(sec_text, current_path=_norm_posix(str(sec_index))).storage
                else:
                    sec_storage = f"<p>{section_title_raw}</p>"

                sec_storage = sec_storage + children_macro()

                sec_page_id = self.ensure_page(
                    key=sec_key,
                    title=section_title,
                    parent_id=domain_page_id,
                    storage=sec_storage,
                    extra_labels=["dir", "section"],
                    collision_prefix="DOCS",
                )

                section_pages[(domain, section)] = sec_page_id

                # Для резолва ссылок на _index.md
                if sec_index:
                    self.path_to_page[_norm_posix(str(sec_index))] = sec_page_id

            if dom_index:
                self.path_to_page[_norm_posix(str(dom_index))] = domain_page_id

        return section_pages

    # -------- link resolver --------
    def _link_resolver(self, href: str, current_path: str | None) -> str | None:
        if not current_path:
            return None

        u = urlparse(href)
        if u.scheme or href.startswith("#"):
            return None

        path = unquote(u.path)
        if path.startswith("/"):
            # repo-root relative style
            path = path.lstrip("/")
        if not path.lower().endswith(".md"):
            return None

        cur = PurePosixPath(_norm_posix(current_path))
        target = (cur.parent / path).as_posix()
        target = _norm_posix(target)

        page_id = self.path_to_page.get(target)
        if not page_id:
            return None

        # IMPORTANT:
        # cfg.base_url is used for API calls. In Docker it is often set to something
        # like host.docker.internal so the container can reach the host.
        # We MUST NOT bake that hostname into page content.
        #
        # So we generate a *relative* link with the correct Confluence context path.
        # Example:
        #   base_url = http://host.docker.internal:8090        -> /pages/viewpage.action?... 
        #   base_url = https://conf.company.ru/wiki           -> /wiki/pages/viewpage.action?...
        parsed = urlparse(self.cfg.base_url)
        base_path = (parsed.path or "").rstrip("/")
        if base_path.endswith("/rest/api"):
            base_path = base_path[: -len("/rest/api")]
        if base_path and not base_path.startswith("/"):
            base_path = "/" + base_path

        url = f"{base_path}/pages/viewpage.action?pageId={page_id}"
        if u.fragment:
            url += f"#{u.fragment}"
        return url

    # -------- publish --------
    def publish_all(self, pass_no: int, paths_file: Path | None = None) -> None:
        self.bootstrap_existing()
        section_pages = self.ensure_domain_and_sections()

        docs_dir = self.cfg.docs_dir

        def is_regular_md_file(p: Path) -> bool:
            name = p.name.lower()
            return p.is_file() and (name not in ("_index.md", "readme.md")) and name.endswith(".md")

        # Собираем md файлы:
        #  - по умолчанию: вся репа (кроме _index/README)
        #  - в MR/changed-only режиме: только перечисленные в paths_file
        md_files: list[Path] = []
        changes_by_new_path: dict[str, Change] = {}

        if paths_file:
            changes = _parse_paths_file(Path(paths_file), docs_dir)
            for ch in changes:
                if ch.op == "D":
                    continue  # пока не удаляем страницы
                if ch.op == "R" and ch.new_path:
                    changes_by_new_path[_norm_posix(ch.new_path)] = ch
                    p = Path(_norm_posix(ch.new_path))
                else:
                    changes_by_new_path[_norm_posix(ch.path)] = ch
                    p = Path(_norm_posix(ch.path))

                if is_regular_md_file(p):
                    md_files.append(p)
                else:
                    # файл может не существовать (например, переименовали + удалили в одном MR)
                    if p.suffix.lower() == ".md":
                        print(f"skip (missing or index): {p}")
        else:
            for p in docs_dir.rglob("*.md"):
                if is_regular_md_file(p):
                    md_files.append(p)

        # Собираем “единицы публикации” (для двухфазного pass2)
        entries: list[Entry] = []
        for p in sorted(md_files):
            try:
                under_docs = p.relative_to(docs_dir)
            except Exception:
                continue

            uparts = under_docs.parts
            if len(uparts) < 3:
                continue

            domain = uparts[0]
            section = uparts[1]
            if (domain, section) not in section_pages:
                continue

            parent_id = str(section_pages[(domain, section)])
            current_path = _norm_posix(str(p))

            md_text = p.read_text(encoding="utf-8")
            title = guess_title(md_text, fallback=p.stem)
            if title.lower() in ("readme", "index"):
                title = p.stem

            key = f"file:{current_path}"
            collision_prefix = (self.cfg.domain_title_map.get(domain) or _humanize(domain))

            # rename handling (if paths_file provides R old new)
            rename_from_key: str | None = None
            rename_from_path: str | None = None
            ch = changes_by_new_path.get(current_path)
            if ch and ch.op == "R" and ch.new_path:
                old_path = _norm_posix(ch.path)
                rename_from_path = old_path
                rename_from_key = f"file:{old_path}"

            entries.append(
                Entry(
                    path=p,
                    current_path=current_path,
                    md_text=md_text,
                    title=title,
                    key=key,
                    parent_id=parent_id,
                    collision_prefix=collision_prefix,
                    rename_from_key=rename_from_key,
                    rename_from_path=rename_from_path,
                )
            )

        def publish_entry(e: Entry, conv: MdToConfluenceStorage) -> str:
            # If this is a rename, "pre-adopt" the old page id under the new key,
            # so ensure_page(...) will UPDATE the same page and rewrite PROPERTY_KEY.
            if e.rename_from_key and e.rename_from_key != e.key:
                old_pid = self.key_to_page.get(e.rename_from_key)
                if old_pid and e.key not in self.key_to_page:
                    self.key_to_page[e.key] = old_pid

            res = conv.convert(e.md_text, current_path=e.current_path)
            page_id = self.ensure_page(
                key=e.key,
                title=e.title,
                parent_id=e.parent_id,
                storage=res.storage,
                extra_labels=["md"],
                page_labels=extract_tag_labels(res.front_matter),
                collision_prefix=e.collision_prefix,
            )
            # Ключ именно такой, как формирует _link_resolver (normpath+unquote)
            self.path_to_page[e.current_path] = page_id

            # Clean up rename artifacts in in-memory indexes
            if e.rename_from_path:
                self.path_to_page.pop(e.rename_from_path, None)
            if e.rename_from_key and e.rename_from_key != e.key:
                if self.key_to_page.get(e.rename_from_key) == page_id:
                    self.key_to_page.pop(e.rename_from_key, None)
            return page_id

        if pass_no == 1:
            for e in entries:
                publish_entry(e, self.conv_plain)
            mode = "changed" if paths_file else "all"
            print(f"pass {pass_no} ({mode}): published {len(entries)} pages")
            return

        # PASS2 делаем детерминированно за 1 запуск:
        #  A) ensure всех страниц + заполнение map path->pageId
        #  B) второй проход: переписывание md-ссылок и update body

        # Phase A
        for e in entries:
            publish_entry(e, self.conv_plain)

        # Phase B
        conv_links = MdToConfluenceStorage(
            link_resolver=self._link_resolver,
            inject_toc=bool(self.cfg.options.get("toc", True)),
            toc_min_level=int(self.cfg.options.get("toc_min", 1)),
            toc_max_level=int(self.cfg.options.get("toc_max", 3)),
            toc_style=str(self.cfg.options.get("toc_style", "none")),
            toc_outline=bool(self.cfg.options.get("toc_outline", False)),
            strip_title_h1=True,
            promote_headings=True,
            strip_heading_numbers=True,
            heading_numbering_in_text=bool(self.cfg.options.get("heading_numbering", True)),
            heading_numbering_css=False,
            heading_numbering_max_level=int(self.cfg.options.get("heading_numbering_max_level", 3)),
        )

        for e in entries:
            publish_entry(e, conv_links)

        mode = "changed" if paths_file else "all"
        print(f"pass {pass_no} ({mode}): published {len(entries)} pages")

    def publish_file(self, md_path: Path, pass_no: int) -> None:
        """Публикует один файл. Для корректного переписывания ссылок (pass2) лучше гонять publish_docs.py."""
        self.bootstrap_existing()
        section_pages = self.ensure_domain_and_sections()

        docs_dir = self.cfg.docs_dir
        try:
            under_docs = md_path.relative_to(docs_dir)
        except Exception:
            raise SystemExit(f"File must be under {docs_dir}: {md_path}")

        uparts = under_docs.parts
        if len(uparts) < 3:
            raise SystemExit(f"Expected docs/<domain>/<section>/...: {md_path}")
        domain, section = uparts[0], uparts[1]
        if (domain, section) not in section_pages:
            raise SystemExit(f"Unknown domain/section: {domain}/{section}")

        if pass_no == 1:
            conv = self.conv_plain
        else:
            conv = MdToConfluenceStorage(
                link_resolver=self._link_resolver,
                inject_toc=bool(self.cfg.options.get("toc", True)),
                toc_min_level=int(self.cfg.options.get("toc_min", 1)),
                toc_max_level=int(self.cfg.options.get("toc_max", 3)),
                toc_style=str(self.cfg.options.get("toc_style", "none")),
                toc_outline=bool(self.cfg.options.get("toc_outline", False)),
                strip_title_h1=True,
                promote_headings=True,
                strip_heading_numbers=True,
                heading_numbering_in_text=bool(self.cfg.options.get("heading_numbering", True)),
                heading_numbering_css=False,
                heading_numbering_max_level=int(self.cfg.options.get("heading_numbering_max_level", 3)),
            )

        parent_id = section_pages[(domain, section)]
        md_text = md_path.read_text(encoding="utf-8")
        title = guess_title(md_text, fallback=md_path.stem)
        key = f"file:{_norm_posix(str(md_path))}"
        res = conv.convert(md_text, current_path=_norm_posix(str(md_path)))

        page_id = self.ensure_page(
            key=key,
            title=title,
            parent_id=str(parent_id),
            storage=res.storage,
            extra_labels=["md"],
            page_labels=extract_tag_labels(res.front_matter),
            collision_prefix=(self.cfg.domain_title_map.get(domain) or _humanize(domain)),
        )
        self.path_to_page[_norm_posix(str(md_path))] = page_id
        print(f"pass {pass_no}: published 1 page")


def publish_all(pass_no: int, cfg_path: str = "publish.yml", paths_file: str | None = None) -> None:
    token = os.getenv("CONF_TOKEN")
    if not token:
        raise SystemExit("Set CONF_TOKEN env var")

    cfg = load_cfg(cfg_path)
    pub = DocsPublisher(cfg, token)
    pub.publish_all(pass_no, paths_file=Path(paths_file) if paths_file else None)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="pass_no", type=int, choices=[1, 2], default=1)
    ap.add_argument("--cfg", default="publish.yml")
    ap.add_argument(
        "--paths-file",
        default=None,
        help="Optional file with changed paths (MR mode). Supports lines: '<path>', 'A <path>', 'M <path>', 'D <path>', 'R <old> <new>', 'R100 <old> <new>'.",
    )
    args = ap.parse_args()

    publish_all(args.pass_no, cfg_path=args.cfg, paths_file=args.paths_file)
