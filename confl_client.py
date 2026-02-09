import json
import requests
from typing import Iterator, Optional

class Confluence:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def create_page(self, space: str, parent_id: str, title: str, storage: str):
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space},
            "ancestors": [{"id": str(parent_id)}],
            "body": {"storage": {"value": storage, "representation": "storage"}},
        }
        r = self.s.post(f"{self.base}/rest/api/content", json=payload, timeout=60)

        if not r.ok:
            raise RuntimeError(f"Create page failed: {r.status_code}\n{r.text}")

        return r.json()

    def get_page(self, page_id: str, expand="version"):
        r = self.s.get(f"{self.base}/rest/api/content/{page_id}?expand={expand}", timeout=60)
        r.raise_for_status()
        return r.json()

    def update_page(self, page_id: str, space: str, parent_id: str, title: str, storage: str):
        cur = self.get_page(page_id, expand="version")
        ver = int(cur["version"]["number"]) + 1
        payload = {
            "id": str(page_id),
            "type": "page",
            "title": title,
            "space": {"key": space},
            "ancestors": [{"id": str(parent_id)}],
            "version": {"number": ver},
            "body": {"storage": {"value": storage, "representation": "storage"}},
        }
        r = self.s.put(f"{self.base}/rest/api/content/{page_id}", json=payload, timeout=60)
        if not r.ok:
            raise RuntimeError(
                "Update page failed\n"
                f"status={r.status_code}\n"
                f"page_id={page_id}\n"
                f"title={title}\n"
                f"parent_id={parent_id}\n"
                f"response={r.text}\n"
            )
        return r.json()

    def find_page_by_title(self, space: str, title: str, *, expand: str = "ancestors"):
        """Exact title search in a space (returns first match)."""
        r = self.s.get(
            f"{self.base}/rest/api/content",
            params={"spaceKey": space, "title": title, "type": "page", "limit": 1, "expand": expand},
            timeout=60,
        )
        r.raise_for_status()
        results = r.json().get("results", []) or []
        return results[0] if results else None

    def cql_iter(self, cql: str, *, expand: str = "metadata.labels,ancestors", limit: int = 200) -> Iterator[dict]:
        """Iterate through Confluence CQL search results."""
        start = 0
        while True:
            r = self.s.get(
                f"{self.base}/rest/api/content/search",
                params={"cql": cql, "limit": limit, "start": start, "expand": expand},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            results = data.get("results", []) or []
            for it in results:
                yield it
            if len(results) < limit:
                return
            start += limit

    def add_labels(self, page_id: str, labels: list[str]) -> None:
        """Add global labels to a page (idempotent-ish)."""
        labels = [l.strip().lower() for l in labels if l and l.strip()]
        if not labels:
            return
        payload = [{"prefix": "global", "name": l} for l in labels]
        r = self.s.post(f"{self.base}/rest/api/content/{page_id}/label", json=payload, timeout=60)
        r.raise_for_status()

    def delete_page(self, page_id: str) -> None:
        r = self.s.delete(f"{self.base}/rest/api/content/{page_id}", timeout=60)
        r.raise_for_status()

    # Content properties (ключ к upsert)
    def put_property(self, page_id: str, key: str, value: dict):
        # если свойства нет — POST, если есть — PUT (нужна version у prop)
        url = f"{self.base}/rest/api/content/{page_id}/property/{key}"
        r = self.s.get(url, timeout=60)
        if r.status_code == 404:
            r2 = self.s.post(
                f"{self.base}/rest/api/content/{page_id}/property",
                json={"key": key, "value": value},
                timeout=60,
            )
            r2.raise_for_status()
            return r2.json()
        r.raise_for_status()
        prop = r.json()
        new_version = int(prop["version"]["number"]) + 1
        payload = {"key": key, "value": value, "version": {"number": new_version}}
        r3 = self.s.put(url, json=payload, timeout=60)
        r3.raise_for_status()
        return r3.json()


    def get_property(self, page_id: str, key: str) -> Optional[dict]:
        """Get a content property by key. Returns None if missing (404)."""
        url = f"{self.base}/rest/api/content/{page_id}/property/{key}"
        r = self.s.get(url, timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def delete_label(self, page_id: str, label: str) -> None:
        """Remove a label from content (query-param form)."""
        r = self.s.delete(
            f"{self.base}/rest/api/content/{page_id}/label",
            params={"name": label},
            timeout=60,
        )
        # 404 if label missing; treat as ok
        if r.status_code in (204, 404):
            return
        r.raise_for_status()

