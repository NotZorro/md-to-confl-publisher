from __future__ import annotations

import re

from converter.md_to_confluence_storage import MdToConfluenceStorage, strip_front_matter


def test_strip_front_matter_extracts_yaml_and_body() -> None:
    md = (
        "---\n"
        "title: Hello\n"
        "tags: [a, b]\n"
        "---\n"
        "# Body\n"
        "Text\n"
    )
    fm, body = strip_front_matter(md)
    assert fm["title"] == "Hello"
    assert fm["tags"] == ["a", "b"]
    assert body.startswith("# Body")


def test_converter_codeblock_becomes_confluence_macro() -> None:
    conv = MdToConfluenceStorage()
    md = """\
```python
print('hi')
```
"""
    res = conv.convert(md)
    assert "ac:structured-macro" in res.storage
    assert "ac:name=\"code\"" in res.storage
    assert "ac:name=\"language\"" in res.storage
    assert ">python<" in res.storage
    assert "print('hi')" in res.storage


def test_converter_images_attachment_and_url() -> None:
    conv = MdToConfluenceStorage()
    md = """\
![rel](images/pic.png)

![abs](https://example.com/a.png)
"""
    res = conv.convert(md, current_path="docs/domain/section/file.md")
    # relative => attachment
    assert "<ri:attachment" in res.storage
    assert "ri:filename=\"pic.png\"" in res.storage
    assert "pic.png" in res.attachments
    # absolute => url
    assert "<ri:url" in res.storage
    assert "ri:value=\"https://example.com/a.png\"" in res.storage


def test_converter_rewrites_links_and_marks_unresolved_md_links() -> None:
    def resolver(href: str, current_path: str | None) -> str | None:
        if href == "other.md":
            return "/pages/viewpage.action?pageId=123"
        return None

    conv = MdToConfluenceStorage(link_resolver=resolver)
    md = """\
[ok](other.md)
[later](unresolved.md#sec)
[ext](https://example.com)
"""
    res = conv.convert(md, current_path="docs/a/b.md")
    assert "href=\"/pages/viewpage.action?pageId=123\"" in res.storage
    # unresolved internal md link should be marked
    assert "data-source-href=\"unresolved.md#sec\"" in res.storage
    # external should remain intact
    assert "href=\"https://example.com\"" in res.storage


def test_converter_tasklists_become_unicode_not_inputs() -> None:
    conv = MdToConfluenceStorage()
    md = """\
- [x] done
- [ ] todo
"""
    res = conv.convert(md)
    assert "<input" not in res.storage
    assert "☑" in res.storage
    assert "☐" in res.storage


def test_converter_injects_toc_first() -> None:
    conv = MdToConfluenceStorage(inject_toc=True, toc_min_level=1, toc_max_level=3)
    md = """\
## Section
Text
"""
    res = conv.convert(md)
    # TOC macro should be at the very beginning
    assert res.storage.startswith("<ac:structured-macro ac:name=\"expand\"")
    assert 'ac:name="toc"' in res.storage


def test_converter_injects_page_properties_before_toc() -> None:
    conv = MdToConfluenceStorage(inject_toc=True)
    md = (
        "---\n"
        "doc_type: service_spec\n"
        "owner: pagrigorev@mts.ru\n"
        "creation_date: 2026-02-12\n"
        "task: https://jira.mts.ru/browse/EOFFR-6694\n"
        "tags: [service, api, <protocol>]\n"
        "---\n"
        "## Section\n"
        "Text\n"
    )
    res = conv.convert(md)
    # Page properties should come first, TOC second
    assert res.storage.startswith("<ac:structured-macro ac:name=\"details\"")
    assert res.storage.find('ac:name="details"') < res.storage.find('ac:name="toc"')
    # owner mention
    assert 'ri:user ri:username="pagrigorev@mts.ru"' in res.storage
    # date as <time>
    assert '<time datetime="2026-02-12">2026-02-12</time>' in res.storage
    # jira macro with extracted key
    assert '<ac:structured-macro ac:name="jira">' in res.storage
    assert '<ac:parameter ac:name="key">EOFFR-6694</ac:parameter>' in res.storage


def test_heading_normalization_strip_promote_and_numbering() -> None:
    conv = MdToConfluenceStorage(
        strip_title_h1=True,
        promote_headings=True,
        strip_heading_numbers=True,
        heading_numbering_in_text=True,
        inject_toc=False,
    )

    md = """\
# Page Title

## 1. First

### 1.1 Second

Text
"""

    res = conv.convert(md)

    # First H1 stripped from body, so it must not appear
    assert "Page Title" not in res.storage

    # H2 promoted to H1 and numbered
    assert re.search(r"<h1>1\. First</h1>", res.storage)

    # H3 promoted to H2 and numbered (1.1.)
    assert re.search(r"<h2>1\.1\. Second</h2>", res.storage)
