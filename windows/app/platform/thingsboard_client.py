from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


class DependencyUnavailable(RuntimeError):
    pass


class ThingsBoardError(RuntimeError):
    pass


def _load_httpx():
    try:
        import httpx  # type: ignore
    except ModuleNotFoundError as exc:
        raise DependencyUnavailable(
            "Missing dependency 'httpx'. Install dependencies with: pip install -r requirements.txt"
        ) from exc
    return httpx


def extract_entity_id(raw: dict[str, Any]) -> str:
    value = raw.get("id", "")
    if isinstance(value, dict):
        return str(value.get("id", ""))
    return str(value or "")


def extract_activation_token(link_or_token: str) -> str:
    text = link_or_token.strip().strip('"')
    if not text:
        raise ThingsBoardError("empty activation link returned by platform")
    parsed = urlparse(text)
    if parsed.query:
        token = parse_qs(parsed.query).get("activateToken", [""])[0]
        if token:
            return token
    if "activateToken=" in text:
        return text.split("activateToken=", 1)[1].split("&", 1)[0]
    return text


@dataclass
class PrecheckResult:
    ok: bool
    status_code: int | None = None
    error: str = ""


def precheck_base_url(base_url: str, timeout_seconds: float = 5.0) -> PrecheckResult:
    httpx = _load_httpx()
    try:
        response = httpx.get(
            base_url.rstrip("/"),
            timeout=timeout_seconds,
            follow_redirects=True,
            trust_env=False,
        )
        return PrecheckResult(ok=response.status_code < 500, status_code=response.status_code)
    except Exception as exc:  # noqa: BLE001 - user-facing precheck should preserve the root message.
        return PrecheckResult(ok=False, error=str(exc))


class ThingsBoardClient:
    """Thin adapter around the ThingsBoard REST API.

    The methods intentionally stay close to ThingsBoard endpoint names so any
    Sprixin-specific differences can be contained in this class later.
    """

    def __init__(self, base_url: str, token: str = "", timeout_seconds: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._login_username = ""
        self._login_password = ""
        self.timeout_seconds = timeout_seconds
        self._httpx = _load_httpx()
        self._client = self._httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ThingsBoardClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def with_token(self, token: str) -> "ThingsBoardClient":
        return ThingsBoardClient(self.base_url, token=token, timeout_seconds=self.timeout_seconds)

    def login(self, username: str, password: str) -> str:
        payload = {"username": username, "password": password}
        data = self._request("POST", "/api/auth/login", json_body=payload, auth=False)
        token = data.get("token")
        if not token:
            raise ThingsBoardError("login response did not contain a token")
        self.token = str(token)
        self._login_username = username
        self._login_password = password
        return self.token

    def grant_tenant_admin_permissions(self) -> None:
        """Reserved hook for Sprixin custom permission APIs.

        Standard ThingsBoard does not require a separate REST call here. Keeping
        this hook makes the orchestration explicit without guessing a custom URL.
        """

    def get_security_settings(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/securitySettings")

    def get_password_policy(self) -> dict[str, Any]:
        settings = self.get_security_settings()
        policy = settings.get("passwordPolicy")
        return policy if isinstance(policy, dict) else {}

    def create_tenant(self, title: str) -> dict[str, Any]:
        return self._request("POST", "/api/tenant", json_body={"title": title})

    def create_tenant_admin(self, tenant_id: str, email: str) -> dict[str, Any]:
        payload = {
            "tenantId": {"id": tenant_id, "entityType": "TENANT"},
            "email": email,
            "authority": "TENANT_ADMIN",
            "firstName": "Quick",
            "lastName": "Data",
        }
        return self._request("POST", "/api/user?sendActivationMail=false", json_body=payload)

    def get_activation_link(self, user_id: str) -> str:
        return self._request_text("GET", f"/api/user/{user_id}/activationLink")

    def activate_user(self, activation_link_or_token: str, password: str) -> dict[str, Any]:
        token = extract_activation_token(activation_link_or_token)
        payload = {"activateToken": token, "password": password}
        return self._request(
            "POST",
            "/api/noauth/activate?sendActivationMail=false",
            json_body=payload,
            auth=False,
        )

    def create_asset(self, name: str, type_name: str) -> dict[str, Any]:
        return self._request("POST", "/api/asset", json_body={"name": name, "type": type_name})

    def create_device(self, name: str, type_name: str) -> dict[str, Any]:
        return self._request("POST", "/api/device", json_body={"name": name, "type": type_name})

    def asset_exists_by_name(self, name: str) -> bool:
        return bool(self.find_asset_id_by_name(name))

    def device_exists_by_name(self, name: str) -> bool:
        return bool(self.find_device_id_by_name(name))

    def find_device_id_by_name(self, name: str) -> str:
        if not name:
            return ""
        try:
            raw = self._request("GET", f"/api/tenant/devices?deviceName={quote(name, safe='')}")
        except ThingsBoardError as exc:
            if " returned 404:" in str(exc):
                return ""
            raise
        return extract_entity_id(raw)

    def find_asset_id_by_name(self, name: str) -> str:
        return self._find_exact_entity_id(f"/api/tenant/assets?assetName={quote(name, safe='')}", name)

    def find_edge_id_by_name(self, name: str) -> str:
        return self._find_exact_entity_id(f"/api/tenant/edges?edgeName={quote(name, safe='')}", name)

    def find_rule_chain_id_by_name(self, name: str) -> str:
        return self._find_paged_entity_id("/api/ruleChains", name)

    def find_tenant_id_by_name(self, name: str) -> str:
        return self._find_paged_entity_id("/api/tenantInfos", name, name_key="title")

    def find_device_ids_by_prefix(self, prefix: str) -> dict[str, str]:
        return self._find_paged_entities_by_prefix("/api/tenant/devices", prefix)

    def find_device_ids_by_names(self, names: list[str]) -> dict[str, str]:
        return self._find_exact_ids_by_names(names, self.find_device_id_by_name)

    def find_asset_ids_by_prefix(self, prefix: str) -> dict[str, str]:
        return self._find_paged_entities_by_prefix("/api/tenant/assets", prefix)

    def find_edge_ids_by_names(self, names: list[str]) -> dict[str, str]:
        return self._find_exact_ids_by_names(names, self.find_edge_id_by_name)

    def find_rule_chain_ids_by_prefix(self, prefix: str) -> dict[str, str]:
        return self._find_paged_entities_by_prefix("/api/ruleChains", prefix)

    def _find_exact_entity_id(self, path: str, name: str) -> str:
        if not name:
            return ""
        try:
            return extract_entity_id(self._request("GET", path))
        except ThingsBoardError as exc:
            if " returned 404:" in str(exc):
                return ""
            raise

    def _find_paged_entity_id(self, path: str, name: str, *, name_key: str = "name") -> str:
        if not name:
            return ""
        query = f"{path}?pageSize=100&page=0&textSearch={quote(name, safe='')}"
        raw = self._request("GET", query)
        entities = raw.get("data", [])
        if not isinstance(entities, list):
            return ""
        for entity in entities:
            if isinstance(entity, dict) and str(entity.get(name_key) or "") == name:
                return extract_entity_id(entity)
        return ""

    def _find_paged_entities_by_prefix(self, path: str, prefix: str) -> dict[str, str]:
        if not prefix:
            return {}
        page = 0
        matches: dict[str, str] = {}
        while True:
            query = f"{path}?pageSize=100&page={page}&textSearch={quote(prefix, safe='')}"
            raw = self._request("GET", query)
            entities = raw.get("data", [])
            if not isinstance(entities, list):
                return matches
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                name = str(entity.get("name") or "")
                entity_id = extract_entity_id(entity)
                if name.startswith(prefix) and entity_id:
                    matches[entity_id] = name
            if not raw.get("hasNext") or not entities:
                return matches
            page += 1

    @staticmethod
    def _find_exact_ids_by_names(
        names: list[str],
        find_id_by_name: Any,
    ) -> dict[str, str]:
        matches: dict[str, str] = {}
        for name in names:
            entity_id = find_id_by_name(name)
            if entity_id:
                matches[entity_id] = name
        return matches

    def get_device_credentials(self, device_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/device/{device_id}/credentials")

    def set_mqtt_basic_credentials(
        self,
        device_id: str,
        client_id: str,
        username: str,
        password: str,
        existing_credentials: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            **(existing_credentials or {}),
            "deviceId": {"id": device_id, "entityType": "DEVICE"},
            "credentialsType": "MQTT_BASIC",
            "credentialsId": client_id or username,
            "credentialsValue": json.dumps(
                {"clientId": client_id, "userName": username, "password": password},
                ensure_ascii=False,
            ),
        }
        return self._request("POST", "/api/device/credentials", json_body=payload)

    def ensure_device_mqtt_credentials(
        self,
        device_id: str,
        preferred_name: str,
        credentials_mode: str = "USERNAME",
    ) -> dict[str, Any]:
        existing_credentials = self.get_device_credentials(device_id)
        if credentials_mode == "CLIENT_ID":
            client_id, username, password = preferred_name, "", ""
        elif credentials_mode == "CLIENT_ID_USERNAME":
            client_id, username, password = preferred_name, preferred_name, preferred_name
        elif credentials_mode == "USERNAME":
            client_id, username, password = "", preferred_name, preferred_name
        else:
            raise ValueError(f"Unsupported MQTT credentials mode: {credentials_mode}")
        return self.set_mqtt_basic_credentials(
            device_id,
            client_id,
            username,
            password,
            existing_credentials=existing_credentials,
        )


    def get_device_relations(self, device_id: str, direction: str = 'from') -> list[dict[str, Any]]:
        if direction not in ('from', 'to'):
            raise ValueError('Invalid relation direction')
        result = self._request('GET', f"/api/relations?{direction}Id={quote(device_id, safe='')}&{direction}Type=DEVICE&relationTypeGroup=COMMON")
        if not isinstance(result, list):
            raise ThingsBoardError('平台已有关系返回格式无效')
        return result

    def create_relation(
        self,
        from_entity_type: str,
        from_id: str,
        to_entity_type: str,
        to_id: str,
        relation_type: str,
        additional_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "from": {"id": from_id, "entityType": from_entity_type},
            "to": {"id": to_id, "entityType": to_entity_type},
            "type": relation_type,
            "typeGroup": "COMMON",
        }
        if additional_info:
            payload["additionalInfo"] = additional_info
        return self._request("POST", "/api/relation", json_body=payload)

    def create_rule_chain(self, name: str, type_name: str) -> dict[str, Any]:
        payload = {
            "name": name,
            "type": type_name,
            "root": False,
            "debugMode": False,
        }
        return self._request("POST", "/api/ruleChain", json_body=payload)

    def get_rule_chain_metadata(self, rule_chain_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/ruleChain/{rule_chain_id}/metadata")

    def save_rule_chain_metadata(self, rule_chain_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        current_metadata = self.get_rule_chain_metadata(rule_chain_id)
        payload = {
            **metadata,
            "version": current_metadata.get("version", 0),
            "ruleChainId": {"id": rule_chain_id, "entityType": "RULE_CHAIN"},
        }
        return self._request("POST", "/api/ruleChain/metadata", json_body=payload)

    def create_edge(self, name: str, routing_key: str, secret: str) -> dict[str, Any]:
        payload = {
            "name": name,
            "type": "default",
            "routingKey": routing_key,
            "secret": secret,
        }
        return self._request("POST", "/api/edge", json_body=payload)

    def assign_to_edge(self, edge_id: str, entity_type: str, entity_id: str) -> dict[str, Any]:
        entity_path = {
            "DEVICE": "device",
            "ASSET": "asset",
            "RULE_CHAIN": "ruleChain",
        }.get(entity_type)
        if not entity_path:
            raise ThingsBoardError(f"unsupported edge assignment entity type: {entity_type}")
        return self._request("POST", f"/api/edge/{edge_id}/{entity_path}/{entity_id}", json_body={})

    def delete_tenant(self, tenant_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/tenant/{tenant_id}")

    def delete_edge(self, edge_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/edge/{edge_id}")

    def delete_rule_chain(self, rule_chain_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/ruleChain/{rule_chain_id}")

    def delete_device(self, device_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/device/{device_id}")

    def delete_asset(self, asset_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/asset/{asset_id}")

    def _headers(self, auth: bool, *, accept: str = "application/json") -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": accept}
        if auth:
            if not self.token:
                raise ThingsBoardError("authenticated request attempted without token")
            headers["X-Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(
                method,
                url,
                headers=self._headers(auth),
                json=json_body,
            )
        except Exception as exc:  # noqa: BLE001
            raise ThingsBoardError(f"{method} {path} failed: {exc}") from exc

        if auth and self._is_expired_token_response(response) and self._refresh_token():
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=self._headers(auth),
                    json=json_body,
                )
            except Exception as exc:  # noqa: BLE001
                raise ThingsBoardError(f"{method} {path} failed after token refresh: {exc}") from exc

        if response.status_code >= 400:
            raise ThingsBoardError(f"{method} {path} returned {response.status_code}: {response.text[:500]}")
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {"value": response.text}
        if isinstance(data, dict):
            return data
        return {"value": data}

    def _request_text(self, method: str, path: str, *, auth: bool = True) -> str:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(
                method,
                url,
                headers=self._headers(auth, accept="text/plain"),
            )
        except Exception as exc:  # noqa: BLE001
            raise ThingsBoardError(f"{method} {path} failed: {exc}") from exc
        if auth and self._is_expired_token_response(response) and self._refresh_token():
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=self._headers(auth, accept="text/plain"),
                )
            except Exception as exc:  # noqa: BLE001
                raise ThingsBoardError(f"{method} {path} failed after token refresh: {exc}") from exc
        if response.status_code >= 400:
            raise ThingsBoardError(f"{method} {path} returned {response.status_code}: {response.text[:500]}")
        return response.text.strip()

    @staticmethod
    def _is_expired_token_response(response: Any) -> bool:
        if response.status_code != 401:
            return False
        text = response.text or ""
        return "Token has expired" in text or '"errorCode":11' in text

    def _refresh_token(self) -> bool:
        if not self._login_username or not self._login_password:
            return False
        payload = {"username": self._login_username, "password": self._login_password}
        try:
            data = self._request("POST", "/api/auth/login", json_body=payload, auth=False)
        except ThingsBoardError:
            return False
        token = data.get("token")
        if not token:
            return False
        self.token = str(token)
        return True
