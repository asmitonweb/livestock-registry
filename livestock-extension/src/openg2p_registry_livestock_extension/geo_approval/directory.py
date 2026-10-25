"""Read-only: each level's approver role holders and the places their data policies allow.

Same Keycloak reads as the Farmer reference directory (role users, then each
user's composite client roles); no identity writes.
"""
from __future__ import annotations

from urllib.parse import quote

from openg2p_registry_core.errors import G2PRegistryException
from openg2p_registry_core.helpers.data_policy_keycloak_helper import (
    DataPolicyKeycloakHelper,
)
from openg2p_registry_core.models import G2PRegistryDataPolicy
from sqlalchemy import select

from .scopes import ROLES, Candidate, GeoApprovalError, policy_scopes


class GeoPolicyDirectory:
    async def candidates(self, session, register_id, levels) -> tuple[Candidate, ...]:
        """Holders of each level's role, scoped to that level by their data policies."""
        helper = DataPolicyKeycloakHelper()
        cfg = helper._config
        if not all((cfg.keycloak_admin_url, cfg.keycloak_admin_client_id,
                    cfg.keycloak_admin_client_secret, cfg.keycloak_client_id, cfg.keycloak_realm)):
            raise GeoApprovalError(
                "Approvers cannot be looked up: the registry's Keycloak admin client is not configured. "
                "Ask the administrator to set the data policy Keycloak settings")
        base = f"/realms/{quote(cfg.keycloak_realm, safe='')}"
        client_id = quote(await helper._resolve_client_uuid(helper._registry_client_id()), safe="")
        users = {}
        for level in levels:
            role = ROLES[level]
            for start in range(0, 10001, 100):
                try:
                    page = await helper._admin_request(
                        "GET", f"{base}/clients/{client_id}/roles/{quote(role, safe='')}/users",
                        params={"first": start, "max": 100})
                except G2PRegistryException:
                    page = None
                if not isinstance(page, list):
                    raise GeoApprovalError(
                        f"Approvers cannot be looked up: Keycloak has no client role \"{role}\" on the "
                        "registry's client. Ask the administrator to create it")
                for user in page:
                    if user.get("enabled", False) and user.get("id"):
                        users.setdefault(user["id"], user)
                if len(page) < 100:
                    break
        policies = (await session.execute(select(G2PRegistryDataPolicy).where(
            G2PRegistryDataPolicy.register_id == register_id,
            G2PRegistryDataPolicy.policy_target == "REGISTER_RECORD"))).scalars().all()
        result = []
        for user in users.values():
            roles = await helper._admin_request(
                "GET", f"{base}/users/{quote(user['id'], safe='')}/role-mappings/clients/{client_id}/composite")
            names = {item.get("name") for item in roles if isinstance(item, dict)} if isinstance(roles, list) else set()
            scopes = policy_scopes([policy for policy in policies if "DP_" + policy.policy_mnemonic in names])
            # A level's scope counts only with that level's role.
            scopes = {level: ids for level, ids in scopes.items() if level in levels and ROLES[level] in names}
            if scopes:
                # AWE's Keycloak assignee ordering.
                result.append(Candidate(user.get("preferred_username") or user.get("username") or user["id"], scopes))
        return tuple(result)
