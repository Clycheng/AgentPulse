"""Profile manifests for the desktop-owned Hermes runtime.

The API remains the source of company facts, but it must never copy a model
credential into a Hermes profile.  This module produces a deterministic,
non-secret manifest which the Local Worker materializes under its own
``HERMES_HOME``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.core.database import Database
from app.orchestration.capability_catalog import resolve_bundle
from app.runtime.employee_soul import build_employee_soul


def local_profile_name(agent_id: str, configured_name: str | None = None) -> str:
    """Return a stable, Hermes-safe name without sharing a host profile."""
    if configured_name and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", configured_name):
        return configured_name
    compact = re.sub(r"[^a-z0-9]", "", agent_id.lower())[-20:]
    return f"aplocal{compact}" or "aplocalworker"


def build_profile_manifests(conn: Database, workspace_id: str) -> list[dict[str, Any]]:
    """Return deterministic, secret-free local profile manifests."""
    rows = conn.execute(
        """SELECT a.id, a.name, a.role, a.description, a.prompt, a.skills_json,
                  s.hermes_profile, s.responsibilities_json
           FROM agents a
           LEFT JOIN agent_specs s ON s.agent_id = a.id
           WHERE a.workspace_id = ?
           ORDER BY a.created_at, a.id""",
        (workspace_id,),
    ).fetchall()
    manifests: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        capability_rows = conn.execute(
            """SELECT capability_key FROM agent_capabilities
               WHERE agent_id = ? AND status = 'enabled' ORDER BY created_at""",
            (row["id"],),
        ).fetchall()
        enabled_capabilities = [item["capability_key"] for item in capability_rows]
        bundle = resolve_bundle(enabled_capabilities)
        try:
            responsibilities = json.loads(row.get("responsibilities_json") or "[]")
        except json.JSONDecodeError:
            responsibilities = []
        if not isinstance(responsibilities, list):
            responsibilities = []
        try:
            skills = json.loads(row.get("skills_json") or "[]")
        except json.JSONDecodeError:
            skills = []
        if not isinstance(skills, list):
            skills = []
        manifest = {
            "agent_id": row["id"],
            "profile_name": local_profile_name(row["id"], row.get("hermes_profile")),
            "name": row["name"],
            "role": row["role"],
            "description": row["description"],
            "soul": build_employee_soul(
                name=row["name"],
                role=row["role"],
                prompt=row.get("prompt"),
                responsibilities=responsibilities,
            ),
            "skills": [str(item) for item in skills if isinstance(item, str)],
            "capability_keys": enabled_capabilities,
            "toolsets": sorted(set(bundle.get("toolsets", []))),
        }
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest["manifest_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        manifests.append(manifest)
    return manifests


def profile_manifest_for_agent(
    conn: Database, workspace_id: str, agent_id: str
) -> dict[str, Any] | None:
    return next(
        (
            manifest
            for manifest in build_profile_manifests(conn, workspace_id)
            if manifest["agent_id"] == agent_id
        ),
        None,
    )
