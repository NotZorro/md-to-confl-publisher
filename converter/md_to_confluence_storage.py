from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Any
import re
import os
import hashlib

import yaml
from markdown_it import MarkdownIt

# Optional markdown-it-py plugins. The publisher must not crash if they're
# missing in the environment where unit tests run.
try:
    from mdit_py_plugins.tasklists import tasklists_plugin  # type: ignore
except Exception:  # pragma: no cover
    tasklists_plugin = None  # type: ignore

try:
    from mdit_py_plugins.footnote import footnote_plugin  # type: ignore
except Exception:  # pragma: no cover
    footnote_plugin = None  # type: ignore

from bs4 import BeautifulSoup
from bs4.element import CData, NavigableString, Tag


# ----------------------------
# Models
# ----------------------------

@dataclass
class ConversionResult:
    storage: str
    front_matter: dict[str, Any] = field(default_factory=dict)
    attachments: set[str] = field(default_factory=set)  # filenames only
    sha256: str = ""  # hash of resulting storage for idempotency


LinkResolver = Callable[[str, Optional[str]], Optional[str]]
ImageResolver = Callable[[str, Optional[str]], tuple[str, str]]
# ImageResolver returns (kind, value) where kind in {"attachment", "url"}
# value is filename for attachment or absolute URL for url


# ----------------------------
# Front-matter
# ----------------------------

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

def strip_front_matter(md: str) -> tuple[dict[str, Any], str]:
    """
    Extract YAML front matter if present.
    """
    m = _FRONT_MATTER_RE.match(md)
    if not m:
        return {}, md
    raw = m.group(1)
    data = yaml.safe_load(raw) or {}
    body = md[m.end():]
    return data, body


# ----------------------------
# Converter
# ----------------------------

class MdToConfluenceStorage:
    """
    Convert Markdown to Confluence Storage format (XHTML + Confluence macros).

    Extension points:
      - link_resolver: rewrite links (e.g. *.md -> confluence page URL)
      - image_resolver: decide whether image becomes attachment or external url
    """

    def __init__(
        self,
        *,
        link_resolver: Optional[LinkResolver] = None,
        image_resolver: Optional[ImageResolver] = None,
        # TOC macro
        inject_toc: bool = False,
        toc_min_level: int = 1,
        toc_max_level: int = 3,
        toc_type: str = "list",
        toc_style: str = "none",   # list style; "none" => no bullets/numbering
        toc_outline: bool = False, # true => show section numbers in TOC
        # Markdown structure normalization
        strip_title_h1: bool = True,        # use first H1 as page title, don't duplicate in body
        promote_headings: bool = True,      # shift headings up: H2->H1, H3->H2, ...
        strip_heading_numbers: bool = True, # remove "1.", "1.1", "1)" from heading text
        # Heading numbering
        # - in_text: prefixes headings with "1.", "1.1.", "1.1.1." (recommended if you don't rely on Confluence auto-numbering)
        # - css: visual numbering via HTML macro + CSS (requires HTML macro enabled in Confluence)
        heading_numbering_in_text: bool = False,
        heading_numbering_css: bool = False,
        heading_numbering_max_level: int = 3,
        # Code blocks
        code_theme: str = "Default",
        code_linenumbers: bool = True,
    ):
        self.link_resolver = link_resolver
        self.image_resolver = image_resolver
        # TOC
        self.inject_toc = inject_toc
        self.toc_min_level = toc_min_level
        self.toc_max_level = toc_max_level
        self.toc_type = toc_type
        self.toc_style = toc_style
        self.toc_outline = toc_outline

        # Markdown structure normalization
        self.strip_title_h1 = strip_title_h1
        self.promote_headings = promote_headings
        self.strip_heading_numbers = strip_heading_numbers

        self.heading_numbering_in_text = heading_numbering_in_text
        self.heading_numbering_css = heading_numbering_css
        self.heading_numbering_max_level = heading_numbering_max_level

        # Code blocks
        self.code_theme = code_theme
        self.code_linenumbers = code_linenumbers

        self._has_tasklist_plugin = tasklists_plugin is not None
        md = (
            MarkdownIt("commonmark", {"html": False, "linkify": True})
            .enable("table")
            .enable("strikethrough")
        )
        if tasklists_plugin is not None:
            md = md.use(tasklists_plugin, enabled=True)
        if footnote_plugin is not None:
            md = md.use(footnote_plugin)
        self.md = md

    def convert(self, md_text: str, *, current_path: Optional[str] = None) -> ConversionResult:
        fm, body = strip_front_matter(md_text)

        body = self._normalize_markdown(body)

        # If tasklist plugin is unavailable, do a minimal tasklist replacement
        # so that "- [x]" becomes "- ☑" and "- [ ]" becomes "- ☐".
        if not self._has_tasklist_plugin:
            body = self._tasklists_md_to_unicode(body)

        html = self.md.render(body)
        soup = BeautifulSoup(html, "html.parser")

        attachments: set[str] = set()

        if self.heading_numbering_in_text:
            self._apply_heading_numbering_in_text(soup)

        if self.heading_numbering_css:
            self._inject_heading_numbering_css(soup)

        self._convert_code_blocks(soup)
        self._convert_images(soup, attachments, current_path=current_path)
        self._rewrite_links(soup, current_path=current_path)
        self._tasklist_inputs_to_unicode(soup)

        # Front-matter driven metadata block (Page Properties) must appear
        # at the very top of the page, before the TOC macro.
        self._inject_page_properties(soup, fm)

        if self.inject_toc:
            self._inject_toc(soup)

        # storage is a fragment; Confluence accepts it in body.storage.value
        storage = soup.decode(formatter="minimal").strip()

        sha256 = hashlib.sha256(storage.encode("utf-8")).hexdigest()
        return ConversionResult(storage=storage, front_matter=fm, attachments=attachments, sha256=sha256)


    def _apply_heading_numbering_in_text(self, soup: BeautifulSoup) -> None:
        """Compatibility hook.

        Headings are already numbered in _normalize_markdown() when
        heading_numbering_in_text=True.

        This method is intentionally a no-op: it prevents crashes if older
        code paths still call it, and avoids double-numbering.
        """
        return


    # ----------------------------
    # Page metadata (Page Properties (Details) macro)
    # ----------------------------

    _JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

    def _inject_page_properties(self, soup: BeautifulSoup, front_matter: dict[str, Any]) -> None:
        """Inject Page Properties (Details) macro based on YAML front matter.

        Expected keys in front matter:
          - owner: string (username/email) -> will be rendered as a user mention link
          - creation_date: YYYY-MM-DD -> rendered as a <time> tag
          - task: Jira issue key or URL -> rendered via jira macro

        The macro is inserted at the top of the page. The TOC macro (if enabled)
        must appear *after* this block.
        """
        if not isinstance(front_matter, dict) or not front_matter:
            return

        owner = (str(front_matter.get("owner") or "").strip() or None)
        creation_date = (str(front_matter.get("creation_date") or "").strip() or None)
        task = (str(front_matter.get("task") or "").strip() or None)

        if not any([owner, creation_date, task]):
            return

        macro = soup.new_tag("ac:structured-macro", attrs={"ac:name": "details"})
        rtb = soup.new_tag("ac:rich-text-body")
        macro.append(rtb)

        table = soup.new_tag("table")
        tbody = soup.new_tag("tbody")
        table.append(tbody)
        rtb.append(table)

        def add_row(label: str, value_node: Tag | NavigableString | str) -> None:
            tr = soup.new_tag("tr")
            th = soup.new_tag("th")
            th.string = label
            td = soup.new_tag("td")

            if isinstance(value_node, str):
                td.string = value_node
            else:
                td.append(value_node)

            tr.append(th)
            tr.append(td)
            tbody.append(tr)

        if owner:
            add_row("Owner", self._user_mention(soup, owner))

        if creation_date:
            # Confluence understands <time datetime="YYYY-MM-DD">...</time>
            t = soup.new_tag("time", attrs={"datetime": creation_date})
            t.string = creation_date
            add_row("Creation date", t)

        if task:
            add_row("Task", self._jira_macro(soup, task))

        # Insert at the top (before everything, including TOC)
        soup.insert(0, macro)


    def _user_mention(self, soup: BeautifulSoup, owner: str) -> Tag:
        """Return a Confluence user mention link.

        Confluence DC/Server commonly supports ri:username. In Cloud the storage
        format prefers ri:account-id, but many SSO setups on DC also use email as
        the username, so this is the best we can do without an extra API lookup.
        """
        link = soup.new_tag("ac:link")
        user = soup.new_tag("ri:user", attrs={"ri:username": owner})
        link.append(user)
        return link


    def _jira_macro(self, soup: BeautifulSoup, task: str) -> Tag:
        """Return a Jira macro for a task value (key or URL)."""
        macro = soup.new_tag("ac:structured-macro", attrs={"ac:name": "jira"})

        m = self._JIRA_KEY_RE.search(task)
        if m:
            p = soup.new_tag("ac:parameter", attrs={"ac:name": "key"})
            p.string = m.group(1)
            macro.append(p)
        else:
            # Fallback: keep the URL (some Confluence setups accept url parameter)
            p = soup.new_tag("ac:parameter", attrs={"ac:name": "url"})
            p.string = task
            macro.append(p)
        return macro


    # ----------------------------
    # Markdown structure normalization
    # ----------------------------

    _FENCE_RE = re.compile(r"^\s*(```|~~~)")
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    # Matches: "1.", "1.1", "1.1.", "1)", "1.2.3)" etc (followed by space)
    _HEADING_NUM_PREFIX_RE = re.compile(
        r"^\s*(?:\d+\)|\d+\.(?:\d+(?:\.\d+)*)?\.?|\d+(?:\.\d+)+)\s+(.*)$"
    )

    def _strip_heading_number_prefix(self, text: str) -> str:
        if not self.strip_heading_numbers:
            return text
        m = self._HEADING_NUM_PREFIX_RE.match(text)
        return (m.group(1) if m else text).strip()

    def _normalize_markdown(self, body: str) -> str:
        """Normalize Markdown heading structure for Confluence.

        - First H1 becomes the page title (handled upstream), so remove it from body.
        - Promote headings by one: H2→H1, H3→H2, ...
        - Strip manual numbering from headings: 1., 1.1, 1)
        - (Optional) Auto-number headings (up to level 3 by default): 1., 1.1., 1.1.1.
        """
        if not (
            self.strip_title_h1
            or self.promote_headings
            or self.strip_heading_numbers
            or self.heading_numbering_in_text
        ):
            return body

        out: list[str] = []
        in_fence = False
        removed_first_h1 = False

        # Counters for auto-numbering (post-promotion levels)
        max_lvl = int(self.heading_numbering_max_level or 3)
        max_lvl = max(1, min(max_lvl, 6))
        n1 = n2 = n3 = 0

        for line in body.splitlines(True):
            # keep original line endings
            if line.endswith("\r\n"):
                core, nl = line[:-2], "\r\n"
            elif line.endswith("\n"):
                core, nl = line[:-1], "\n"
            else:
                core, nl = line, ""

            if self._FENCE_RE.match(core):
                in_fence = not in_fence
                out.append(core + nl)
                continue

            if not in_fence:
                m = self._HEADING_RE.match(core)
                if m:
                    hashes, text = m.group(1), m.group(2)
                    level = len(hashes)

                    if level == 1 and self.strip_title_h1 and not removed_first_h1:
                        removed_first_h1 = True
                        continue

                    if self.promote_headings and level > 1:
                        level -= 1

                    text = self._strip_heading_number_prefix(text)

                    # Auto-number headings in text (H1..H3 by default)
                    if self.heading_numbering_in_text and level <= 3 and level <= max_lvl:
                        if level == 1:
                            n1 += 1
                            n2 = 0
                            n3 = 0
                            prefix = f"{n1}. "
                        elif level == 2:
                            if n1 == 0:
                                n1 = 1
                            n2 += 1
                            n3 = 0
                            prefix = f"{n1}.{n2}. "
                        else:  # level == 3
                            if n1 == 0:
                                n1 = 1
                            if n2 == 0:
                                n2 = 1
                            n3 += 1
                            prefix = f"{n1}.{n2}.{n3}. "

                        # Avoid double-prefix if someone already wrote the same (rare, but still)
                        if not text.startswith(prefix):
                            text = prefix + text

                    core = ("#" * level) + " " + text

            out.append(core + nl)

        return "".join(out)


    def _inject_heading_numbering_css(self, soup: BeautifulSoup) -> None:
        """Add CSS-based auto-numbering for headings (H1-H3) without polluting heading text.

        Uses Confluence HTML macro (must be enabled). Scoped to .md-content so it won't affect other content/macros.
        """
        max_lvl = int(self.heading_numbering_max_level or 3)
        max_lvl = max(1, min(max_lvl, 6))

        # Wrap existing content (so selectors are scoped)
        wrapper = soup.new_tag("div", attrs={"class": "md-content"})
        for node in list(soup.contents):
            wrapper.append(node.extract())
        soup.append(wrapper)

        # Build CSS for up to 3 levels
        css_lines = [
            ".md-content { counter-reset: h1; }",
            ".md-content h1 { counter-reset: h2; counter-increment: h1; }",
            ".md-content h1::before { content: counter(h1) \". \"; }",
        ]

        if max_lvl >= 2:
            css_lines += [
                ".md-content h2 { counter-reset: h3; counter-increment: h2; }",
                ".md-content h2::before { content: counter(h1) \".\" counter(h2) \" \"; }",
            ]
        if max_lvl >= 3:
            css_lines += [
                ".md-content h3 { counter-increment: h3; }",
                ".md-content h3::before { content: counter(h1) \".\" counter(h2) \".\" counter(h3) \" \"; }",
            ]

        css = "<style>\n" + "\n".join(css_lines) + "\n</style>"

        html_macro = soup.new_tag("ac:structured-macro", attrs={"ac:name": "html"})
        body = soup.new_tag("ac:plain-text-body")
        body.append(CData(css))
        html_macro.append(body)

        # Place at the very top; TOC (if enabled) will be inserted above it later
        soup.insert(0, html_macro)

    def _inject_toc(self, soup: BeautifulSoup) -> None:
        """Insert Confluence TOC macro near the top of the page.

        We wrap the TOC into an Expand macro so it's collapsible ("hideable")
        for readers.
        """
        toc = soup.new_tag("ac:structured-macro", attrs={"ac:name": "toc"})

        def param(name: str, value: str) -> None:
            p = soup.new_tag("ac:parameter", attrs={"ac:name": name})
            p.string = value
            toc.append(p)

        param("minLevel", str(self.toc_min_level))
        param("maxLevel", str(self.toc_max_level))

        # Keep TOC clean by default: no bullets and no numbering.
        param("outline", "true" if self.toc_outline else "false")
        param("type", str(self.toc_type))
        param("style", str(self.toc_style))

        # Make it collapsible via Expand macro.
        expand = soup.new_tag("ac:structured-macro", attrs={"ac:name": "expand"})
        p_title = soup.new_tag("ac:parameter", attrs={"ac:name": "title"})
        p_title.string = "Оглавление"
        expand.append(p_title)
        p_expanded = soup.new_tag("ac:parameter", attrs={"ac:name": "expanded"})
        p_expanded.string = "false"
        expand.append(p_expanded)

        rtb = soup.new_tag("ac:rich-text-body")
        rtb.append(toc)
        expand.append(rtb)

        # Place near the top. If Page Properties (Details) is present,
        # TOC must go after it, separated by an empty line.
        insert_at = 0
        details_found = False
        for i, node in enumerate(list(soup.contents)):
            if isinstance(node, NavigableString) and not str(node).strip():
                continue
            if (
                isinstance(node, Tag)
                and node.name == "ac:structured-macro"
                and node.get("ac:name") == "details"
            ):
                insert_at = i + 1
                details_found = True
            else:
                insert_at = i
            break

        if details_found:
            spacer = soup.new_tag("p")
            spacer.append(soup.new_tag("br"))
            soup.insert(insert_at, spacer)
            insert_at += 1

        soup.insert(insert_at, expand)
    # ----------------------------
    # Post-process steps
    # ----------------------------

    def _convert_code_blocks(self, soup: BeautifulSoup) -> None:
        """
        <pre><code class="language-python">...</code></pre>
          =>
        <ac:structured-macro ac:name="code">...</ac:structured-macro>
        """
        for pre in list(soup.find_all("pre")):
            code = pre.find("code")
            if not code:
                continue

            lang = self._extract_language(code)
            code_text = code.get_text()

            macro = soup.new_tag("ac:structured-macro", attrs={"ac:name": "code"})

            if lang:
                p_lang = soup.new_tag("ac:parameter", attrs={"ac:name": "language"})
                p_lang.string = lang
                macro.append(p_lang)

            p_theme = soup.new_tag("ac:parameter", attrs={"ac:name": "theme"})
            p_theme.string = self.code_theme
            macro.append(p_theme)

            p_ln = soup.new_tag("ac:parameter", attrs={"ac:name": "linenumbers"})
            p_ln.string = "true" if self.code_linenumbers else "false"
            macro.append(p_ln)

            body = soup.new_tag("ac:plain-text-body")
            body.append(CData(code_text))
            macro.append(body)

            pre.replace_with(macro)

    def _extract_language(self, code_tag: Tag) -> str:
        cls = code_tag.get("class") or []
        for c in cls:
            if c.startswith("language-"):
                return c.replace("language-", "", 1).strip()
            if c.startswith("lang-"):
                return c.replace("lang-", "", 1).strip()
        return ""

    def _convert_images(
        self,
        soup: BeautifulSoup,
        attachments: set[str],
        *,
        current_path: Optional[str],
    ) -> None:
        """
        <img src="relative.png" alt="a"> =>
          <ac:image ac:alt="a"><ri:attachment ri:filename="relative.png"/></ac:image>

        <img src="https://..."> =>
          <ac:image ac:alt="a"><ri:url ri:value="https://..."/></ac:image>
        """
        for img in list(soup.find_all("img")):
            src = (img.get("src") or "").strip()
            alt = (img.get("alt") or "").strip()

            if not src:
                img.decompose()
                continue

            kind, value = self._resolve_image(src, current_path=current_path)

            ac_image = soup.new_tag("ac:image")
            if alt:
                ac_image.attrs["ac:alt"] = alt

            if kind == "attachment":
                filename = os.path.basename(value)
                attachments.add(filename)

                ri_att = soup.new_tag("ri:attachment", attrs={"ri:filename": filename})
                ac_image.append(ri_att)
            else:
                ri_url = soup.new_tag("ri:url", attrs={"ri:value": value})
                ac_image.append(ri_url)

            img.replace_with(ac_image)

    def _resolve_image(self, src: str, *, current_path: Optional[str]) -> tuple[str, str]:
        if self.image_resolver:
            return self.image_resolver(src, current_path)

        # default strategy:
        # - absolute http(s) => url
        # - else => attachment (filename only)
        if src.startswith("http://") or src.startswith("https://"):
            return ("url", src)

        return ("attachment", src)

    def _rewrite_links(self, soup: BeautifulSoup, *, current_path: Optional[str]) -> None:
        """
        Rewrite <a href="..."> if link_resolver provided.
        Also marks unresolved internal md links with data-source-href for later pass.
        """
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if not href:
                continue

            if self.link_resolver:
                new_href = self.link_resolver(href, current_path)
                if new_href:
                    a["href"] = new_href
                    continue

            # If looks like internal md link, keep original in data attr for later pass
            if href.lower().endswith(".md") or ".md#" in href.lower():
                a["data-source-href"] = href


    _TASKLIST_MD_RE = re.compile(r"^(\s*[-*+]\s*)\[\s*([xX ])\s*\]\s+", re.MULTILINE)

    def _tasklists_md_to_unicode(self, body: str) -> str:
        """Fallback tasklist support when markdown-it plugins are unavailable.

        Converts markdown list items:
          - [x] done
          - [ ] todo
        into:
          - ☑ done
          - ☐ todo
        """
        def repl(m: re.Match[str]) -> str:
            prefix = m.group(1)
            flag = m.group(2)
            sym = "☑" if flag.lower() == "x" else "☐"
            return f"{prefix}{sym} "

        return self._TASKLIST_MD_RE.sub(repl, body)

    def _tasklist_inputs_to_unicode(self, soup: BeautifulSoup) -> None:
        """
        markdown-it tasklists plugin emits <input type="checkbox" ...>.
        Confluence storage doesn't like raw inputs; replace with unicode.
        """
        for inp in list(soup.find_all("input")):
            if (inp.get("type") or "").lower() != "checkbox":
                continue
            checked = inp.has_attr("checked")
            mark = "☑ " if checked else "☐ "
            inp.replace_with(NavigableString(mark))
