"""Human-readable, credential-free summaries for gateway task archives."""

from datetime import datetime, timezone


def generated_archive_summary(project_name, projects, job, platform_url, template_mode=False):
    """One report for the whole ZIP, outside every gateway project directory."""
    devices = sum(expected['devices'] for _, expected, _ in projects)
    points = sum(expected['points'] for _, expected, _ in projects)
    collect_channels = sum(expected['collectChannels'] for _, expected, _ in projects)
    forward_channels = sum(expected['forwardChannels'] for _, expected, _ in projects)
    elapsed = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(job['created'])).total_seconds())
    lines = [
        f'# 物联网测试工程自动化平台 · {project_name}', '',
        '## 生成概况', '',
        '- 执行状态：工程文件已生成并校验',
        f"- 生成任务 ID：{job['id']}",
        f"- 开始时间：{job['created']}",
        f'- 本次网关工程生成耗时：{round(elapsed / 60, 2)} 分钟',
        f'- 平台地址：{platform_url}',
        '- 平台上报：未在线验证',
        f"- 设备凭据：{'模板凭据，需导入平台并核对' if template_mode else '已绑定凭据，未在线验证'}",
        f'- 网关工程：{len(projects)} 个',
        f'- 逻辑设备：{devices}',
        f'- 业务测点：{points}',
        f'- 采集通道：{collect_channels}',
        f'- 转发通道：{forward_channels}', '',
        '## 工程清单', '',
    ]
    kinds = (['PCS', 'ESS', 'INV', 'BOX'] if any('ESS' in expected.get('counts', {}) for _, expected, _ in projects)
             else ['PCS', 'BMS', 'GROUPBMS', 'CELL', 'CL', 'BOX'])
    headers = ['工程', *kinds, '设备总数', '采集通道', '转发通道', '业务点数']
    lines.extend(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |'])
    for name, expected, _ in projects:
        values = [name, *(f"{expected.get('counts', {}).get(kind, 0):,}" for kind in kinds),
                  f"{expected['devices']:,}", f"{expected['collectChannels']:,}",
                  f"{expected['forwardChannels']:,}", f"{expected['points']:,}"]
        lines.append('| ' + ' | '.join(values) + ' |')
    lines.append('')
    for name, _, project in projects:
        lines.append(f"- {name} 文件校验：{'通过' if project['project_name'] == name else '未通过'}")
    lines.append('')
    lines.extend(['## 在线验证', '', '- 尚未执行平台遥测校验；本文件不代表设备已上报或压力测试已通过。'])
    return '\n'.join(lines) + '\n'


def deployment_archive_summary(project_name, projects, record):
    lines = [f'# 网关部署任务总结 - {project_name}', '',
             f"- 执行状态：{record.get('state', 'prepared')}",
             f"- 部署任务 ID：{record['id']}",
             f"- 目标网关：{record['options']['host']}",
             '- 平台上报：请以部署验收记录为准', '', '## 工程清单', '']
    for name, config in projects.items():
        channels = config.get('corridors_zf', [])
        members = [device for channel in channels for device in channel.get('server', {}).get('devices', [])]
        lines.extend([f'### {name}', '', f'- 本工程转发设备：{len(members)}',
                      f"- 本工程转发测点：{sum(len(device.get('point_table', [])) for device in members)}",
                      f'- 本工程转发通道：{len(channels)}', ''])
    lines.extend(['## 在线验证', '', '- ZIP 是任务配置快照；运行结果和遥测验收请查看平台中的部署记录。'])
    return '\n'.join(lines) + '\n'


def generated_summary(name, expected, project, job, platform_url, template_mode=False):
    counts = expected['counts']
    lines = [
        f'# 网关工程任务总结 - {name}', '',
        '- 执行状态：工程文件已生成并校验',
        f"- 生成任务 ID：{job['id']}",
        f"- 开始时间：{job['created']}",
        f"- 平台地址：{platform_url}",
        '- 平台上报：未在线验证',
        f"- 设备凭据：{'模板凭据，需导入平台并核对' if template_mode else '已绑定凭据，未在线验证'}",
        '', '## 工程统计', '',
        f"- 逻辑设备：{expected['devices']}",
        f"- 业务测点：{expected['points']}",
        f"- 采集通道：{expected['collectChannels']}",
        f"- 转发通道：{expected['forwardChannels']}",
    ]
    lines.extend(f'- {kind}：{count} 台' for kind, count in counts.items() if count)
    lines.extend([
        '', '## 文件校验', '',
        f"- [通过] 工程名称：预期 {name}，实际 {project['project_name']}",
        f"- [通过] 采集通道：预期 {expected['collectChannels']}，实际 {len(project['corridors_eq'])}",
        f"- [通过] 转发通道：预期 {expected['forwardChannels']}，实际 {len(project['corridors_zf'])}",
        '- [通过] 设备身份与转发凭据匹配本次工程配置',
        '', '## 在线验证', '',
        '- 尚未执行平台遥测校验；本文件不代表设备已上报或压力测试已通过。',
    ])
    return '\n'.join(lines) + '\n'


def deployment_summary(name, config, record):
    channels = config.get('corridors_zf', [])
    members = [device for channel in channels for device in channel.get('server', {}).get('devices', [])]
    lines = [
        f'# 网关部署任务总结 - {name}', '',
        f"- 执行状态：{record.get('state', 'prepared')}",
        f"- 部署任务 ID：{record['id']}",
        f"- 准备时间：{record['created']}",
        f"- 平台地址：{record.get('platformUrl', '')}",
        f"- 目标网关：{record['options']['host']}",
        '- 平台上报：请以部署验收记录为准',
        '', '## 任务统计', '',
        f'- 本工程转发设备：{len(members)}',
        f"- 本工程转发测点：{sum(len(device.get('point_table', [])) for device in members)}",
        f'- 本工程转发通道：{len(channels)}',
    ]
    lines.extend(['', '## 文件校验', '',
                  f"- [通过] 工程名称：预期 {name}，实际 {config['project_name']}",
                  '- [通过] 配置来自已校验的任务快照',
                  '', '## 在线验证', '',
                  '- ZIP 是任务配置快照；运行结果和遥测验收请查看平台中的部署记录。'])
    return '\n'.join(lines) + '\n'
