"""Seed an isolated synthetic HAPI instance."""

import argparse
import asyncio
import os

import httpx

from switchboard.config import configured_clinic
from switchboard.fixtures import synthetic_resources
from switchboard.scheduling import utc_now


async def fhir_metadata(client: httpx.AsyncClient, wait_seconds: int) -> httpx.Response:
    if not wait_seconds:
        return await client.get("metadata")
    try:
        async with asyncio.timeout(wait_seconds):
            while True:
                try:
                    response = await client.get("metadata", timeout=5)
                    if response.status_code < 500:
                        return response
                except httpx.RequestError:
                    pass
                await asyncio.sleep(2)
    except TimeoutError as exc:
        raise RuntimeError("FHIR server did not become ready before the seed deadline") from exc


async def main(wait_seconds: int = 0) -> None:
    async with httpx.AsyncClient(
        base_url=os.environ["SWITCHBOARD_FHIR_URL"],
        headers={"Accept": "application/fhir+json", "Content-Type": "application/fhir+json"},
        timeout=30,
    ) as client:
        metadata = await fhir_metadata(client, wait_seconds)
        metadata.raise_for_status()
        if metadata.json().get("fhirVersion") != "4.0.1":
            raise RuntimeError("Expected a FHIR R4 server")
        resources = synthetic_resources(configured_clinic(), utc_now())
        missing = []
        for resource in resources:
            path = f"{resource['resourceType']}/{resource['id']}"
            existing = await client.get(path)
            if existing.status_code == 404:
                missing.append(
                    {
                        "resource": resource,
                        "request": {"method": "PUT", "url": path, "ifNoneMatch": "*"},
                    }
                )
            else:
                existing.raise_for_status()
        for offset in range(0, len(missing), 30):
            response = await client.post(
                "",
                json={
                    "resourceType": "Bundle",
                    "type": "transaction",
                    "entry": missing[offset : offset + 30],
                },
            )
            response.raise_for_status()
        print(f"Created {len(missing)} synthetic resources.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS")
    args = parser.parse_args()
    if args.wait < 0:
        parser.error("--wait must be nonnegative")
    asyncio.run(main(args.wait))
