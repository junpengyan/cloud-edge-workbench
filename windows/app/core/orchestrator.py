from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable

from app.models.config import RunConfig
from app.models.entities import (
    EdgeAssignmentRecord,
    EntityRecord,
    RelationRecord,
    RunResult,
)
from app.platform.thingsboard_client import ThingsBoardClient, extract_entity_id, precheck_base_url
from app.services.estimates import estimate_manual_savings
from app.services.mqtt_publisher import publish_telemetry
from app.services.naming import (
    make_auto_tenant_admin_email,
    make_auto_tenant_name,
    make_custom_rule_chain_name,
    make_entity_name,
    make_prefixed_device_name,
    make_prefixed_template_name,
    make_run_id,
)
from app.services.passwords import describe_password_policy, validate_password
from secure_reporting import write_reports
from app.services.relation_rules import resolve_relation_rules
from app.services.rule_chain_imports import RuleChainImport, parse_rule_chain_imports
from app.services.templates import parse_name_list, parse_template
from app.services.validation import build_run_validation


LogSink = Callable[[str], None]


class QuickDataOrchestrator:
    def __init__(
        self,
        config: RunConfig,
        logger: LogSink | None = None,
        run_id: str | None = None,
        telemetry_stop_event: Event | None = None,
    ):
        self.config = config
        self.log = logger or (lambda message: None)
        self.run_id = run_id or make_run_id()
        self.output_dir = Path(config.output_root) / self.run_id
        self.telemetry_stop_event = telemetry_stop_event or Event()

    def run(self) -> RunResult:
        started = time.monotonic()
        result = RunResult(
            run_id=self.run_id,
            status="RUNNING",
            config_snapshot=self.config.model_dump(mode="json"),
            output_dir=str(self.output_dir),
        )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._log("开始执行快速造数任务")

        try:
            self._run_steps(result)
            result.validation_checks = build_run_validation(result, self.config)
            failed_checks = [item for item in result.validation_checks if not item.success]
            if failed_checks:
                self._log(f"执行后校验发现 {len(failed_checks)} 项异常，详情请查看报告")
            else:
                self._log("执行后校验通过")
            result.status = "SUCCESS"
            self._log("任务执行成功")
        except Exception as exc:  # noqa: BLE001 - report should capture any runtime failure.
            result.status = "FAILED"
            result.error = str(exc)
            self._log(f"任务执行失败：{exc}")
        finally:
            result.finished_at = datetime.now()
            elapsed = time.monotonic() - started
            result.manual_estimate = estimate_manual_savings(
                edge_count=len(result.edges),
                device_count=len(result.devices),
                asset_count=len(result.assets),
                rule_chain_count=len(result.rule_chains),
                relation_count=len(result.relations),
                edge_assignment_count=len(result.edge_assignments),
                mqtt_publish_count=len(result.mqtt_publishes),
                automation_seconds=elapsed,
            )
            write_reports(result, self.output_dir)
            self._log(f"报告已生成：{self.output_dir}")
        return result

    def _run_steps(self, result: RunResult) -> None:
        if self.config.reuse_existing_devices and not self.config.uses_existing_tenant_admin:
            raise ValueError("只读复用必须使用已有租户账号")
        self._precheck()
        admin_client: ThingsBoardClient | None = None
        tenant_client: ThingsBoardClient | None = None
        try:
            if not self.config.uses_existing_tenant_admin:
                admin_client = ThingsBoardClient(self.config.base_url, timeout_seconds=self.config.request_timeout_seconds)
                self._log("使用超管账号登录云平台")
                admin_client.login(self.config.sysadmin_username, self.config.sysadmin_password)
                password_policy = admin_client.get_password_policy()
                password_errors = validate_password(self.config.tenant_admin_password, password_policy)
                if password_errors:
                    raise ValueError(
                        "租户管理员密码不符合云平台策略："
                        f"{'，'.join(password_errors)}。当前策略：{describe_password_policy(password_policy)}"
                    )
            tenant_client = self._prepare_tenant_context(admin_client, result)
            if self.config.reuse_existing_devices:
                self._load_existing_devices(tenant_client, result)
                self._load_existing_relations(tenant_client, result)
                return
            if self.config.enable_asset_creation:
                self._create_assets(tenant_client, result)
            else:
                self._log("已关闭资产创建，跳过资产批量创建")
            if self.config.reuse_existing_devices:
                self._log("跳过设备创建，沿用已有设备身份与凭据")
            elif self.config.enable_device_creation:
                self._create_devices(tenant_client, result)
            else:
                self._log("已关闭设备创建，跳过设备批量创建")
            if self.config.enable_relation_creation:
                if result.assets or result.devices:
                    self._create_relations_from_rules(tenant_client, result)
                else:
                    self._log("本次未创建资产或设备，跳过关系建立")
            else:
                self._log("已关闭关系建立，跳过资产与设备关联")
            if self.config.enable_rule_chain_creation:
                self._create_rule_chains(tenant_client, result)
            else:
                self._log("已关闭规则链创建，跳过规则链导入")
            if self.config.enable_edge_creation:
                self._create_edges(tenant_client, result)
            elif self.config.enable_edge_assignment:
                self._load_existing_edges(tenant_client, result)
            else:
                self._log("已关闭边缘实例创建，跳过边缘实例创建")
            if self.config.enable_edge_assignment:
                if result.edges:
                    self._assign_to_edges(tenant_client, result)
                else:
                    self._log("未创建边缘实例，跳过设备、资产和边缘规则链模板分配")
            else:
                self._log("已关闭边缘分配，跳过设备、资产和边缘规则链模板分配")
            if self.config.enable_telemetry:
                if result.devices:
                    self._publish_mqtt(result)
                else:
                    self._log("本次未创建设备，跳过 MQTT 遥测上送")
            else:
                self._log("已关闭遥测数据上送，跳过 MQTT 模拟遥测上报")
        finally:
            if admin_client:
                admin_client.close()
            if tenant_client:
                tenant_client.close()

    def _precheck(self) -> None:
        self._log("执行云平台连接预检")
        precheck = precheck_base_url(self.config.base_url, self.config.precheck_timeout_seconds)
        if not precheck.ok:
            detail = precheck.error or f"HTTP {precheck.status_code}"
            raise RuntimeError(
                "云平台连接预检失败。请检查云服务是否启动、IP 是否正确、"
                "虚拟机网络是否连通、防火墙是否放行 8080。"
                f" 原因：{detail}"
            )
        self._log(f"云平台连接预检通过：HTTP {precheck.status_code}")

    def _prepare_tenant_context(self, admin_client: ThingsBoardClient | None, result: RunResult) -> ThingsBoardClient:
        if self.config.uses_existing_tenant_admin:
            self._log("使用已有租户管理员账号登录，跳过租户创建和激活")
            tenant_client = ThingsBoardClient(
                self.config.base_url,
                timeout_seconds=self.config.request_timeout_seconds,
            )
            tenant_client.login(
                self.config.existing_tenant_admin_username,
                self.config.existing_tenant_admin_password,
            )
            result.tenant_admin = EntityRecord(
                entity_type="USER",
                name=self.config.existing_tenant_admin_username,
                type="TENANT_ADMIN",
                notes="existing tenant admin",
            )
            return tenant_client

        tenant_name = self.config.tenant_name or make_auto_tenant_name(self.run_id)
        tenant_admin_email = self.config.tenant_admin_email or make_auto_tenant_admin_email(self.run_id)

        self._log("准备租户管理员权限")
        admin_client.grant_tenant_admin_permissions()
        self._log(f"创建租户：{tenant_name}")
        tenant_raw = admin_client.create_tenant(tenant_name)
        tenant_id = extract_entity_id(tenant_raw)
        if not tenant_id:
            raise RuntimeError("创建租户成功但响应中没有租户 ID")
        result.tenant = EntityRecord(
            entity_type="TENANT",
            name=tenant_name,
            id=tenant_id,
            type="TENANT",
            raw=tenant_raw,
        )

        self._log(f"创建租户管理员：{tenant_admin_email}")
        user_raw = admin_client.create_tenant_admin(tenant_id, tenant_admin_email)
        user_id = extract_entity_id(user_raw)
        if not user_id:
            raise RuntimeError("创建租户管理员成功但响应中没有用户 ID")
        result.tenant_admin = EntityRecord(
            entity_type="USER",
            name=tenant_admin_email,
            id=user_id,
            type="TENANT_ADMIN",
            raw=user_raw,
        )

        self._log("获取并使用激活链接设置租户管理员密码")
        activation_link = admin_client.get_activation_link(user_id)
        admin_client.activate_user(activation_link, self.config.tenant_admin_password)

        self._log("使用租户管理员登录")
        tenant_client = ThingsBoardClient(
            self.config.base_url,
            timeout_seconds=self.config.request_timeout_seconds,
        )
        tenant_client.login(tenant_admin_email, self.config.tenant_admin_password)
        return tenant_client

    def _create_assets(self, client: ThingsBoardClient, result: RunResult) -> None:
        self._log("按模板批量创建资产")
        asset_name_sequences: dict[str, int] = {}
        for template_row, item in enumerate(parse_template(self.config.asset_template), start=1):
            for _ in range(1, item.count + 1):
                sequence_key = item.name.strip().lower()
                name_sequence = asset_name_sequences.get(sequence_key, 0) + 1
                asset_name_sequences[sequence_key] = name_sequence
                requested_sequence = name_sequence
                name, name_sequence = _next_available_asset_name(
                    client,
                    self.config,
                    self.run_id,
                    item.name,
                    name_sequence,
                )
                asset_name_sequences[sequence_key] = name_sequence
                if name_sequence > requested_sequence:
                    self._log(
                        f"资产名称已存在，自动跳过 {item.name} 的已占用编号，继续使用：{name}"
                    )
                raw = client.create_asset(name, item.type)
                entity_id = extract_entity_id(raw)
                if not entity_id:
                    raise RuntimeError(f"资产 {name} 创建后缺少 ID")
                result.assets.append(
                    EntityRecord(
                        entity_type="ASSET",
                        name=name,
                        id=entity_id,
                        type=item.type,
                        notes=f"template={item.name}; templateIndex={template_row}",
                        raw=raw,
                    )
                )
        self._log(f"资产创建完成：{len(result.assets)} 个")



    def _load_existing_relations(self, client: ThingsBoardClient, result: RunResult) -> None:
        names = {d.id:d.name for d in result.devices}
        seen = set()
        for device in result.devices:
            for direction in ('from', 'to'):
                for relation in client.get_device_relations(device.id, direction):
                    source, target = relation.get('from', {}), relation.get('to', {})
                    source_id, target_id = source.get('id'), target.get('id')
                    source_type, target_type = source.get('entityType'), target.get('entityType')
                    if not all((source_id,target_id,source_type,target_type)):
                        raise ValueError('平台已有关系缺少实体ID或类型')
                    key = (source_type,source_id,target_type,target_id,relation.get('type','Contains'),relation.get('typeGroup','COMMON'))
                    if key in seen:
                        continue
                    seen.add(key)
                    result.relations.append(RelationRecord(
                        from_entity_type=source_type, from_id=source_id, from_name=names.get(source_id,''),
                        to_entity_type=target_type, to_id=target_id, to_name=names.get(target_id,''),
                        relation_type=relation.get('type','Contains'),
                        additional_info=relation.get('additionalInfo') or {}))
        self._log(f"已读取 {len(result.relations)} 条已有关系，未创建或修改任何平台资源")

    def _load_existing_devices(self, client: ThingsBoardClient, result: RunResult) -> None:
        """Resolve every requested device before any asset or relation writes."""
        devices = []
        sequences = {}
        seen_ids, seen_clients = set(), set()
        for template_row, item in enumerate(parse_template(self.config.device_template), start=1):
            for _ in range(item.count):
                key = item.name.strip().lower()
                sequences[key] = sequences.get(key, 0) + 1
                name = make_prefixed_device_name(self.config.device_name_prefix, item.name, sequences[key])
                device_id = client.find_device_id_by_name(name)
                if not device_id:
                    raise ValueError(f"已有设备未找到：{name}；请核对设备前缀和数量，不会自动补建")
                credentials = client.get_device_credentials(device_id)
                if credentials.get('credentialsType') not in ('MQTT_BASIC', 'ACCESS_TOKEN'):
                    raise ValueError(f"设备 {name} 的凭据类型不支持")
                client_id, username, secret, notes = _extract_mqtt_credentials(credentials)
                if not (client_id or username) or any('***' in v for v in (client_id, username, secret)):
                    raise ValueError(f"设备 {name} 的 MQTT 凭据缺失或已脱敏")
                if device_id in seen_ids or (client_id and client_id in seen_clients):
                    raise ValueError("已有设备ID或MQTT客户端ID重复")
                seen_ids.add(device_id)
                if client_id:
                    seen_clients.add(client_id)
                devices.append(EntityRecord(
                    entity_type='DEVICE', name=name, id=device_id, type=item.type,
                    mqtt_client_id=client_id, mqtt_username=username, mqtt_password_or_token=secret,
                    notes=f"template={item.name}; templateIndex={template_row}; reused=true; {notes}",
                    raw={'id':{'id':device_id}, 'name':name, 'credentials':credentials}))
        result.devices.extend(devices)
        self._log(f"已读取并复用 {len(devices)} 台平台设备，不创建或修改设备凭据")

    def _create_devices(self, client: ThingsBoardClient, result: RunResult) -> None:
        self._log("按模板批量创建设备并获取 MQTT 凭据")
        device_name_sequences: dict[str, int] = {}
        for template_row, item in enumerate(parse_template(self.config.device_template), start=1):
            for template_index in range(1, item.count + 1):
                sequence_key = item.name.strip().lower()
                name_sequence = device_name_sequences.get(sequence_key, 0) + 1
                device_name_sequences[sequence_key] = name_sequence
                requested_sequence = name_sequence
                name, name_sequence = _next_available_device_name(
                    client,
                    self.config,
                    self.run_id,
                    item.name,
                    name_sequence,
                )
                device_name_sequences[sequence_key] = name_sequence
                if name_sequence > requested_sequence:
                    self._log(
                        f"设备名称已存在，自动跳过 {item.name} 的已占用编号，继续使用：{name}"
                    )
                while True:
                    raw = client.create_device(name, item.type)
                    device_id = extract_entity_id(raw)
                    if not device_id:
                        raise RuntimeError(f"设备 {name} 创建后缺少 ID")
                    try:
                        credentials = client.ensure_device_mqtt_credentials(
                            device_id,
                            name,
                            self.config.mqtt_credentials_mode,
                        )
                        break
                    except Exception as exc:  # noqa: BLE001 - rollback just-created partial device.
                        try:
                            client.delete_device(device_id)
                        except Exception:  # noqa: BLE001 - keep the original credentials error actionable.
                            pass
                        if not _is_credentials_assigned_to_another_device(exc):
                            raise RuntimeError(f"设备 {name} 设置 MQTT 凭据失败：{exc}") from exc
                        name_sequence += 1
                        if name_sequence - requested_sequence > 10000:
                            raise RuntimeError(
                                f"设备 {item.name} 连续尝试 10000 个编号仍无法生成唯一 MQTT 凭据。"
                                "请更换设备名称前缀，或清理平台中的历史凭据。"
                            ) from exc
                        name, name_sequence = _next_available_device_name(
                            client,
                            self.config,
                            self.run_id,
                            item.name,
                            name_sequence,
                        )
                        device_name_sequences[sequence_key] = name_sequence
                        self._log(f"MQTT 凭据已被平台占用，自动尝试下一个设备名称：{name}")
                mqtt_client_id, mqtt_username, mqtt_secret, notes = _extract_mqtt_credentials(credentials)
                result.devices.append(
                    EntityRecord(
                        entity_type="DEVICE",
                        name=name,
                        id=device_id,
                        type=item.type,
                        mqtt_client_id=mqtt_client_id,
                        mqtt_username=mqtt_username,
                        mqtt_password_or_token=mqtt_secret,
                        notes=f"template={item.name}; templateIndex={template_row}; {notes}",
                        raw={**raw, "credentials": credentials},
                    )
                )
        self._log(f"设备创建完成：{len(result.devices)} 个")

    def _create_asset_relations(self, client: ThingsBoardClient, result: RunResult) -> None:
        levels = _group_assets_by_template_order(result.assets, self.config.asset_template)
        if len(levels) < 2:
            return
        self._log("建立资产层级关系")
        for level_index in range(1, len(levels)):
            parents = levels[level_index - 1]
            children = levels[level_index]
            for child_index, child in enumerate(children):
                parent = parents[child_index % len(parents)]
                client.create_relation("ASSET", parent.id, "ASSET", child.id, self.config.relation_type)
                child.parent_name = parent.name
                result.relations.append(
                    RelationRecord(
                        from_entity_type="ASSET",
                        from_id=parent.id,
                        from_name=parent.name,
                        to_entity_type="ASSET",
                        to_id=child.id,
                        to_name=child.name,
                        relation_type=self.config.relation_type,
                    )
                )
        self._log(f"资产层级关系建立完成：{len(result.relations)} 条")

    def _create_device_asset_relations(self, client: ThingsBoardClient, result: RunResult) -> None:
        leaf_assets = _leaf_assets(result.assets, self.config.asset_template)
        if not leaf_assets or not result.devices:
            return
        self._log("将设备平均关联到最后一层资产")
        before = len(result.relations)
        for index, device in enumerate(result.devices):
            asset = leaf_assets[index % len(leaf_assets)]
            client.create_relation("ASSET", asset.id, "DEVICE", device.id, self.config.relation_type)
            device.parent_name = asset.name
            result.relations.append(
                RelationRecord(
                    from_entity_type="ASSET",
                    from_id=asset.id,
                    from_name=asset.name,
                    to_entity_type="DEVICE",
                    to_id=device.id,
                    to_name=device.name,
                    relation_type=self.config.relation_type,
                )
            )
        self._log(f"设备资产关系建立完成：{len(result.relations) - before} 条")

    def _create_relations_from_rules(self, client: ThingsBoardClient, result: RunResult) -> None:
        self._log("按页面关系规则建立资产和设备关联")
        resolution = resolve_relation_rules(
            self.config.relation_rules,
            assets=result.assets,
            devices=result.devices,
        )
        for relation in resolution.relations:
            from_entity = relation.from_entity
            to_entity = relation.to_entity
            client.create_relation(
                from_entity.entity_type,
                from_entity.id,
                to_entity.entity_type,
                to_entity.id,
                relation.relation_type,
                relation.additional_info,
            )
            to_entity.parent_name = from_entity.name
            result.relations.append(
                RelationRecord(
                    from_entity_type=from_entity.entity_type,
                    from_id=from_entity.id,
                    from_name=from_entity.name,
                    to_entity_type=to_entity.entity_type,
                    to_id=to_entity.id,
                    to_name=to_entity.name,
                    relation_type=relation.relation_type,
                    additional_info=relation.additional_info,
                )
            )
        self._log(
            f"页面关系规则处理完成：创建 {len(resolution.relations)} 条，"
            f"因本次实体数量不足跳过 {resolution.skipped_count} 条，"
            f"规则共 {resolution.rule_count} 条"
        )

    def _create_rule_chains(self, client: ThingsBoardClient, result: RunResult) -> None:
        self._log("创建规则链")
        for item in _rule_chain_imports_for(self.config, self.run_id):
            for index in range(1, item.count + 1):
                name = make_custom_rule_chain_name(item.name, index, item.count)
                if item.type == "EDGE":
                    self._log(f"创建边缘管理-规则链模板：{name}")
                else:
                    self._log(f"创建规则链库规则链：{name}")
                raw = client.create_rule_chain(name, item.type)
                entity_id = extract_entity_id(raw)
                if not entity_id:
                    raise RuntimeError(f"规则链 {name} 创建后缺少 ID")
                self._log(f"写入规则链节点：{name}")
                metadata = client.save_rule_chain_metadata(
                    entity_id,
                    _rule_chain_metadata(item.template, self.config.local_rule_chain_templates),
                )
                result.rule_chains.append(
                    EntityRecord(
                        entity_type="RULE_CHAIN",
                        name=name,
                        id=entity_id,
                        type=item.type,
                        notes=f"template={item.template}",
                        raw={**raw, "metadata": metadata},
                    )
                )
        self._log(f"规则链创建完成：{len(result.rule_chains)} 条")

    def _create_edges(self, client: ThingsBoardClient, result: RunResult) -> None:
        self._log("创建边缘实例")
        for index, name in enumerate(_edge_names_for(self.config, self.run_id), start=1):
            routing_key = f"rk-{self.run_id[-6:]}-{index}-{secrets.token_hex(4)}"
            secret = f"sec-{secrets.token_urlsafe(16)}"
            raw = client.create_edge(name, routing_key, secret)
            entity_id = extract_entity_id(raw)
            if not entity_id:
                raise RuntimeError(f"边缘实例 {name} 创建后缺少 ID")
            result.edges.append(
                EntityRecord(
                    entity_type="EDGE",
                    name=name,
                    id=entity_id,
                    type="default",
                    notes=f"routingKey={routing_key}; secret={secret}",
                    raw=raw,
                )
            )
        self._log(f"边缘实例创建完成：{len(result.edges)} 个")

    def _load_existing_edges(self, client: ThingsBoardClient, result: RunResult) -> None:
        names = parse_name_list(self.config.edge_names)
        if not names:
            raise ValueError("使用已有边缘实例分配时，请填写边缘实例名称")
        self._log("查找已有边缘实例")
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            entity_id = client.find_edge_id_by_name(name)
            if not entity_id:
                raise RuntimeError(f"未找到已有边缘实例：{name}")
            result.edges.append(
                EntityRecord(
                    entity_type="EDGE",
                    name=name,
                    id=entity_id,
                    type="default",
                    notes="existing edge instance",
                )
            )
        self._log(f"已有边缘实例确认完成：{len(result.edges)} 个")

    def _assign_to_edges(self, client: ThingsBoardClient, result: RunResult) -> None:
        if not result.edges:
            return
        self._log("将设备、资产和边缘规则链模板平均分配到边缘实例")
        assignable = _edge_assignable_entities(result)
        skipped_rule_chains = len(result.rule_chains) - sum(
            1 for entity in assignable if entity.entity_type == "RULE_CHAIN"
        )
        if skipped_rule_chains:
            self._log(f"跳过云端 CORE 规则链边缘分配：{skipped_rule_chains} 条")
        for index, entity in enumerate(assignable):
            edge = result.edges[index % len(result.edges)]
            client.assign_to_edge(edge.id, entity.entity_type, entity.id)
            entity.edge_name = edge.name
            result.edge_assignments.append(
                EdgeAssignmentRecord(
                    edge_id=edge.id,
                    edge_name=edge.name,
                    entity_type=entity.entity_type,
                    entity_id=entity.id,
                    entity_name=entity.name,
                )
            )
        self._log(f"边缘分配完成：{len(result.edge_assignments)} 条")

    def _publish_mqtt(self, result: RunResult) -> None:
        self._log("开始 MQTT 模拟遥测上报")
        result.mqtt_publishes = publish_telemetry(
            self.config,
            self.run_id,
            result.devices,
            stop_event=self.telemetry_stop_event,
        )
        ok = sum(1 for item in result.mqtt_publishes if item.success)
        failed = len(result.mqtt_publishes) - ok
        if self.telemetry_stop_event.is_set():
            self._log("已手动停止当前任务的 MQTT 遥测上送")
        self._log(f"MQTT 上报完成：成功 {ok} 条，失败 {failed} 条")

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log(f"[{timestamp}] {message}")


def _extract_mqtt_credentials(credentials: dict[str, object]) -> tuple[str, str, str, str]:
    credentials_type = str(credentials.get("credentialsType", "") or "")
    credentials_id = str(credentials.get("credentialsId", "") or "")
    credentials_value = credentials.get("credentialsValue", "")
    notes = f"credentialsType={credentials_type or 'UNKNOWN'}"

    if credentials_type == "MQTT_BASIC":
        client_id = ""
        username = ""
        password = ""
        if isinstance(credentials_value, str) and credentials_value:
            try:
                parsed = json.loads(credentials_value)
                client_id = str(parsed.get("clientId") or "")
                username = str(parsed.get("userName") or parsed.get("username") or "")
                password = str(parsed.get("password") or "")
            except json.JSONDecodeError:
                password = credentials_value
        return client_id, username, password, notes

    token = credentials_id or str(credentials_value or "")
    return "", token, token, notes


def _group_assets_by_template_order(assets: list[EntityRecord], template: str) -> list[list[EntityRecord]]:
    levels: list[list[EntityRecord]] = []
    cursor = 0
    for item in parse_template(template):
        level = assets[cursor : cursor + item.count]
        if level:
            levels.append(level)
        cursor += item.count
    return levels


def _leaf_assets(assets: list[EntityRecord], template: str) -> list[EntityRecord]:
    levels = _group_assets_by_template_order(assets, template)
    if not levels:
        return []
    return levels[-1]


def _make_device_name(
    config: RunConfig,
    run_id: str,
    template_name: str,
    template_index: int,
) -> str:
    if config.device_name_prefix:
        return make_prefixed_device_name(config.device_name_prefix, template_name, template_index)
    return make_entity_name(run_id, template_name, template_index)


def _next_available_device_name(
    client: ThingsBoardClient,
    config: RunConfig,
    run_id: str,
    template_name: str,
    start_index: int,
) -> tuple[str, int]:
    index = start_index
    while True:
        name = _make_device_name(config, run_id, template_name, index)
        if not config.device_name_prefix or not client.device_exists_by_name(name):
            return name, index
        index += 1


def _next_available_asset_name(
    client: ThingsBoardClient,
    config: RunConfig,
    run_id: str,
    template_name: str,
    start_index: int,
) -> tuple[str, int]:
    index = start_index
    while True:
        name = _make_asset_name(config, run_id, template_name, index)
        if not config.asset_name_prefix or not client.asset_exists_by_name(name):
            return name, index
        index += 1


def _is_credentials_assigned_to_another_device(exc: Exception) -> bool:
    return "Device credentials are already assigned to another device!" in str(exc)


def _make_asset_name(config: RunConfig, run_id: str, template_name: str, template_index: int) -> str:
    if config.asset_name_prefix:
        return make_prefixed_template_name(config.asset_name_prefix, template_name, template_index)
    return make_entity_name(run_id, template_name, template_index)


def _make_rule_chain_name(config: RunConfig, run_id: str, index: int) -> str:
    if config.rule_chain_name:
        return make_custom_rule_chain_name(config.rule_chain_name, index, config.rule_chain_count)
    return make_entity_name(run_id, "RuleChain", index)


def _rule_chain_imports_for(config: RunConfig, run_id: str) -> list[RuleChainImport]:
    configured_imports = parse_rule_chain_imports(config.rule_chain_imports)
    if configured_imports:
        return configured_imports
    return [
        RuleChainImport(
            name=_make_rule_chain_name(config, run_id, index),
            type=config.rule_chain_type,
            template=config.rule_chain_template,
            count=1,
        )
        for index in range(1, config.rule_chain_count + 1)
    ]


def _edge_names_for(config: RunConfig, run_id: str) -> list[str]:
    configured_names = parse_name_list(config.edge_names)
    if configured_names:
        return configured_names
    return [make_entity_name(run_id, "Edge", index) for index in range(1, config.edge_count + 1)]


def _edge_assignable_entities(result: RunResult) -> list[EntityRecord]:
    return [
        *result.devices,
        *result.assets,
        *(rule_chain for rule_chain in result.rule_chains if rule_chain.type == "EDGE"),
    ]


def _rule_chain_metadata(
    template_name: str,
    local_templates: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    template_files = {
        "rpc_test": "rpc_test_rule_chain.json",
        "common": "common_rule_chain.json",
    }
    filename = template_files.get(template_name)
    if filename:
        template_path = Path(__file__).resolve().parents[1] / "templates" / filename
        with template_path.open("r", encoding="utf-8") as handle:
            template = json.load(handle)
    else:
        template = (local_templates or {}).get(template_name)
        if not template:
            raise RuntimeError(f"不支持的规则链模板：{template_name}")
    metadata = template.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError(f"规则链模板 {template_name} 缺少 metadata")
    return metadata
