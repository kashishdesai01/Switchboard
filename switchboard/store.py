"""The scheduler uses the same versioned transactions in both deployment modes."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx

Resource = dict[str, Any]


def bundle_patient_reference(patient_id: str) -> str:
    return f"urn:uuid:{UUID(patient_id)}"


class Conflict(Exception):
    """A resource changed since it was read."""


class Unavailable(Exception):
    """A store operation failed or its outcome is unknown."""


class ResourceStore(Protocol):
    async def read(self, kind: str, resource_id: str) -> Resource | None: ...

    async def search(self, kind: str, **query: str) -> list[Resource]: ...

    async def transaction(self, *, updates: list[Resource], creates: list[Resource]) -> None: ...


class FhirStore:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise Unavailable("FHIR request outcome unavailable") from exc

    @staticmethod
    def _json(response: httpx.Response) -> Resource:
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            return data
        except ValueError as exc:
            raise Unavailable("Unexpected FHIR response") from exc

    async def read(self, kind: str, resource_id: str) -> Resource | None:
        response = await self._request("GET", f"{kind}/{resource_id}")
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise Unavailable("FHIR read failed")
        return self._json(response)

    async def search(self, kind: str, **query: str) -> list[Resource]:
        path = kind
        query.setdefault("_count", "100")
        resources = []
        # Never follow a pagination URL to a different FHIR origin.
        for _ in range(100):
            if path == "Patient":
                response = await self._request(
                    "POST",
                    "Patient/_search",
                    data=query,
                    headers={
                        "Cache-Control": "no-cache",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            else:
                response = await self._request(
                    "GET", path, params=query or None, headers={"Cache-Control": "no-cache"}
                )
            if not response.is_success:
                raise Unavailable("FHIR search failed")
            bundle = self._json(response)
            if bundle.get("type") != "searchset":
                raise Unavailable("Expected FHIR search bundle")
            resources.extend(
                entry["resource"]
                for entry in bundle.get("entry", [])
                if entry.get("resource", {}).get("resourceType") == kind
            )
            next_url = next(
                (link["url"] for link in bundle.get("link", []) if link["relation"] == "next"),
                None,
            )
            if next_url is None:
                return resources
            target = response.url.join(next_url)
            base = self.client.base_url
            if (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port):
                raise Unavailable("Invalid FHIR pagination origin")
            path, query = str(target), {}
        raise Unavailable("FHIR pagination limit exceeded")

    async def transaction(self, *, updates: list[Resource], creates: list[Resource]) -> None:
        entries = [
            {
                "resource": resource,
                "request": {
                    "method": "PUT",
                    "url": f"{resource['resourceType']}/{resource['id']}",
                    "ifMatch": f'W/"{resource["meta"]["versionId"]}"',
                },
            }
            for resource in updates
        ]
        for resource in creates:
            entry = {
                "resource": resource,
                "request": {"method": "POST", "url": resource["resourceType"]},
            }
            if resource["resourceType"] == "Patient" and resource.get("id"):
                entry["fullUrl"] = bundle_patient_reference(resource["id"])
                entry["resource"] = {key: value for key, value in resource.items() if key != "id"}
            entries.append(entry)
        response = await self._request(
            "POST", "", json={"resourceType": "Bundle", "type": "transaction", "entry": entries}
        )
        if response.status_code in {409, 412}:
            raise Conflict("FHIR version conflict")
        if not response.is_success:
            raise Unavailable("FHIR transaction outcome unavailable")
        bundle = self._json(response)
        results = bundle.get("entry", [])
        if bundle.get("type") != "transaction-response" or len(results) != len(entries):
            raise Unavailable("Unexpected FHIR transaction response")
        if any(not item.get("response", {}).get("status", "").startswith("2") for item in results):
            raise Unavailable("FHIR transaction returned a failed entry")


class DemoStore:
    """Local synthetic fixtures and FHIR-shaped resources in an atomic SQLite store."""

    def __init__(self, path: Path):
        self.path = path
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS resources (
                    kind TEXT, id TEXT, version INTEGER, body TEXT, PRIMARY KEY (kind, id)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=1)

    def seed(self, resources: list[Resource]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT OR IGNORE INTO resources VALUES (?, ?, 1, ?)",
                [(item["resourceType"], item["id"], json.dumps(item)) for item in resources],
            )

    @staticmethod
    def _decode(row: tuple[int, str]) -> Resource:
        version, body = row
        item = json.loads(body)
        item.setdefault("meta", {})["versionId"] = str(version)
        return item

    async def read(self, kind: str, resource_id: str) -> Resource | None:
        return await self._run(self._read, kind, resource_id)

    def _read(self, kind: str, resource_id: str) -> Resource | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT version, body FROM resources WHERE kind=? AND id=?", (kind, resource_id)
            ).fetchone()
            return self._decode(row) if row else None

    async def search(self, kind: str, **query: str) -> list[Resource]:
        return await self._run(self._search, kind, query)

    def _search(self, kind: str, query: dict[str, str]) -> list[Resource]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT version, body FROM resources WHERE kind=?", (kind,))
            return [item for row in rows if matches(item := self._decode(row), query)]

    async def transaction(self, *, updates: list[Resource], creates: list[Resource]) -> None:
        await self._run(self._transaction, updates, creates)

    @staticmethod
    async def _run(method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except sqlite3.Error as exc:
            raise Unavailable("Demo database unavailable") from exc

    def _transaction(self, updates: list[Resource], creates: list[Resource]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in updates:
                changed = connection.execute(
                    """UPDATE resources SET body=?, version=version+1
                       WHERE kind=? AND id=? AND version=?""",
                    (
                        json.dumps(item),
                        item["resourceType"],
                        item["id"],
                        item["meta"]["versionId"],
                    ),
                )
                if changed.rowcount != 1:
                    raise Conflict("Resource version conflict")
            for item in creates:
                resource = {**item, "id": item.get("id") or uuid4().hex}
                connection.execute(
                    "INSERT INTO resources VALUES (?, ?, 1, ?)",
                    (item["resourceType"], resource["id"], json.dumps(resource)),
                )


def matches(item: Resource, query: dict[str, str]) -> bool:
    """Evaluate the search parameters used by Switchboard's demo."""
    for key, value in query.items():
        match key:
            case "_count":
                continue
            case "start" | "date" if value.startswith("ge"):
                if not item.get("start") or datetime.fromisoformat(
                    item["start"]
                ) < datetime.fromisoformat(value[2:]):
                    return False
            case "identifier":
                if not any(
                    f"{entry.get('system', '')}|{entry['value']}" == value
                    for entry in item.get("identifier", [])
                ):
                    return False
            case "beneficiary" | "subject":
                if item.get(key, {}).get("reference") != value:
                    return False
            case "patient":
                if not any(
                    entry.get("actor", {}).get("reference") == value
                    for entry in item.get("participant", [])
                ):
                    return False
            case "family":
                if not any(
                    name.get("family", "").casefold() == value.casefold()
                    for name in item.get("name", [])
                ):
                    return False
            case "birthdate":
                if item.get("birthDate") != value:
                    return False
            case _:
                if item.get(key) != value:
                    return False
    return True
