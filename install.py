#!/usr/bin/env python3
"""Install or enable the host executor without touching application data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

BASE = Path('/opt/cpa-platform')
CONFIG_DIR = Path('/etc/cpa-upgrader')
CONFIG = CONFIG_DIR / 'config.json'
PROTOCOL = BASE / 'upgrader'
OVERLAY = BASE / 'runtime' / 'cpa-upgrader-compose.yml'


def run(args):
    result = subprocess.check_output(args, stderr=subprocess.PIPE)
    return result.decode('utf-8')


def inspect(name):
    return json.loads(run(['docker', 'inspect', name]))[0]


def require(value, code):
    if not value:
        raise SystemExit(code)


def service_name(container):
    labels = container['Config']['Labels'] or {}
    return labels.get('com.docker.compose.service')


def paths(container):
    return {m['Destination']: [m['Source'], bool(m.get('RW'))] for m in container['Mounts']}


def discover():
    listing = run(['docker', 'ps', '--format', '{{.Names}}']).splitlines()
    containers = [inspect(name) for name in listing]
    by_service = {}
    for container in containers:
        service = service_name(container)
        if service in ('cli-proxy-api', 'cpa-manager-plus'):
            by_service[service] = container
    require(set(by_service) == {'cli-proxy-api', 'cpa-manager-plus'}, 'CPA_SERVICES_NOT_RUNNING')
    labels = by_service['cpa-manager-plus']['Config']['Labels'] or {}
    project = labels.get('com.docker.compose.project')
    work = labels.get('com.docker.compose.project.working_dir')
    files = labels.get('com.docker.compose.project.config_files', '').split(',')
    require(project and work and len(files) >= 1 and all(Path(p).parent == Path(work) and Path(p).is_file() for p in files), 'COMPOSE_IDENTITY_UNAVAILABLE')
    require(Path(work).is_dir() and not Path(work).is_symlink(), 'COMPOSE_WORKDIR_UNSAFE')
    for component, service in (('cli', 'cli-proxy-api'), ('manager', 'cpa-manager-plus')):
        c = by_service[service]
        require(c['Config']['Labels'].get('com.docker.compose.project') == project, 'MIXED_COMPOSE_PROJECT')
        require(c['State']['Running'] and c['State'].get('Health', {}).get('Status') == 'healthy', 'CPA_SERVICES_UNHEALTHY')
    manager_paths = paths(by_service['cpa-manager-plus'])
    key = manager_paths.get('/run/secrets/cpa_admin_key', [None])[0]
    require(key and Path(key).is_file(), 'MANAGER_KEY_FILE_NOT_FOUND')
    for component, service in (('cli', 'cli-proxy-api'), ('manager', 'cpa-manager-plus')):
        require(by_service[service]['HostConfig']['PortBindings'] == {
            ('8317' if component == 'cli' else '18317') + '/tcp': [{'HostIp': '127.0.0.1', 'HostPort': ('8317' if component == 'cli' else '18317')}]
        }, 'UNEXPECTED_PORT_BINDING')
    baseline = {}
    for component, service in (('cli', 'cli-proxy-api'), ('manager', 'cpa-manager-plus')):
        c = by_service[service]
        image = json.loads(run(['docker', 'image', 'inspect', c['Image']]))[0]
        baseline[component] = {'imageId': c['Image'], 'version': (image['Config'].get('Labels') or {}).get('org.opencontainers.image.version', 'pinned-image')}
    root = str(PROTOCOL)
    runtime_dir = BASE / 'runtime'
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ('catalog', 'requests', 'status', 'journal'):
        (PROTOCOL / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    desired_manager = dict(manager_paths)
    desired_manager['/upgrades/catalog'] = [str(PROTOCOL / 'catalog'), False]
    desired_manager['/upgrades/requests'] = [str(PROTOCOL / 'requests'), True]
    desired_manager['/upgrades/status'] = [str(PROTOCOL / 'status'), False]
    overlay = {
        'services': {'cpa-manager-plus': {'environment': {'CPA_UPGRADE_DIR': '/upgrades'}, 'volumes': [
            {'type': 'bind', 'source': str(PROTOCOL / 'catalog'), 'target': '/upgrades/catalog', 'read_only': True},
            {'type': 'bind', 'source': str(PROTOCOL / 'requests'), 'target': '/upgrades/requests'},
            {'type': 'bind', 'source': str(PROTOCOL / 'status'), 'target': '/upgrades/status', 'read_only': True},
        ]}}
    }
    OVERLAY.write_text(json.dumps(overlay, indent=2) + '\n')  # JSON is a YAML subset.
    config = {
        'schemaVersion': 1, 'enabled': False, 'project': project, 'workingDir': work,
        'composeFiles': files, 'overlayFile': str(OVERLAY), 'protocolDir': root,
        'backupDir': str(BASE / 'backups' / 'upgrades'), 'pinFile': str(runtime_dir / 'image-pins.json'),
        'managerKeyFile': key, 'containers': {'cli': by_service['cli-proxy-api']['Name'].lstrip('/'), 'manager': by_service['cpa-manager-plus']['Name'].lstrip('/')},
        'ports': {'cli': {'8317/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8317'}]}, 'manager': {'18317/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18317'}]}},
        'mounts': {'cli': paths(by_service['cli-proxy-api']), 'manager': manager_paths},
        'desiredMounts': {'cli': paths(by_service['cli-proxy-api']), 'manager': desired_manager},
        'baseline': baseline,
    }
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = CONFIG.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, indent=2) + '\n')
    os.chmod(str(temporary), 0o600)
    os.replace(str(temporary), str(CONFIG))
    pin_file = runtime_dir / 'image-pins.json'
    pin_file.write_text(json.dumps({'services': {
        'cli-proxy-api': {'image': baseline['cli']['imageId'], 'pull_policy': 'never'},
        'cpa-manager-plus': {'image': baseline['manager']['imageId'], 'pull_policy': 'never'},
    }}, indent=2) + '\n')
    os.chmod(str(pin_file), 0o600)
    if not (PROTOCOL / 'catalog/releases.json').exists():
        (PROTOCOL / 'catalog/releases.json').write_text('{"schemaVersion": 1, "releases": []}\n')
    if not (PROTOCOL / 'catalog/channel.json').exists():
        (PROTOCOL / 'catalog/channel.json').write_text('{"schemaVersion": 1, "releases": []}\n')
    return config


def install_unit():
    unit = '''[Unit]\nDescription=CPA Linux automatic upgrade executor\nAfter=docker.service network-online.target\nWants=network-online.target\n[Service]\nType=simple\nExecStart=/usr/bin/python3 /opt/cpa-platform/upgrader/upgrader.py --config /etc/cpa-upgrader/config.json\nRestart=always\nRestartSec=15\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=full\nReadWritePaths=/opt/cpa-platform/upgrader /opt/cpa-platform/runtime /opt/cpa-platform/backups /etc/cpa-upgrader\n[Install]\nWantedBy=multi-user.target\n'''
    Path('/etc/systemd/system/cpa-upgrader.service').write_text(unit)
    subprocess.check_call(['systemctl', 'daemon-reload'])
    subprocess.check_call(['systemctl', 'enable', '--now', 'cpa-upgrader.service'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    config = discover() if not CONFIG.exists() else json.loads(CONFIG.read_text())
    if args.enable:
        config['enabled'] = True
        temporary = CONFIG.with_suffix('.tmp')
        temporary.write_text(json.dumps(config, indent=2) + '\n')
        os.chmod(str(temporary), 0o600)
        os.replace(str(temporary), str(CONFIG))
    install_unit()
    print(json.dumps({'installed': True, 'enabled': config.get('enabled', False), 'project': config['project'], 'workingDir': config['workingDir']}))


if __name__ == '__main__':
    main()
