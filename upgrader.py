#!/usr/bin/env python3
"""CPA Linux executor. Python 3.6+, standard library only; no GitHub credentials.

Public protocol: catalog / requests / status. Recovery journal is host-only.
Every destructive transition is journaled BEFORE Docker; recovery reconciles
actual identity and never repeats an uncertain switch or restores a database.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.request
import uuid

IMAGE = re.compile(r'^sha256:[a-f0-9]{64}$')
VERSION = re.compile(r'^v([0-9]+)\.([0-9]+)\.([0-9]+)(?:-custom\.([0-9]+))?$')
ID = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
RELEASE_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{0,79}$')
TERMINAL = ('succeeded', 'failed', 'rolled_back')
SERVICES = {'cli': 'cli-proxy-api', 'manager': 'cpa-manager-plus'}
PROTOCOL_FIELDS = ('releaseId component version imageTag imageSource imageDigest imageId sourceCommit '
                   'allowedFromImageIds rollbackDataCompatible migrationRequired migrationMode '
                   'evidence evidenceSha256 validatedAt').split()
CHANNEL = 'https://raw.githubusercontent.com/qazwsxedc-coder/CPA-Platform-Deploy/channel/manifest.json'


class Fault(Exception):
    pass


def require(ok, code):
    if not ok:
        raise Fault(code)


def utc():
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'


def plain(path):
    path = Path(path).absolute()
    for item in [path] + list(path.parents):
        require(not item.is_symlink(), 'symlink_rejected')
    return path


def read_json(path, default=None):
    path = plain(path)
    if not path.exists():
        return default
    require(path.is_file() and path.stat().st_size <= 8 * 1024 ** 2, 'invalid_protocol_file')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'duplicate_json_key')
            value[key] = item
        return value
    with path.open(encoding='utf-8') as stream:
        return json.load(stream, object_pairs_hook=unique)


def sync_dir(path):
    if os.name == 'posix':
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, data):
    path = plain(path)
    temporary = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    sync_dir(path.parent)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def command(args, timeout=30):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Fault('docker_state_uncertain' if args[0] == 'docker' else 'command_timeout')
    require(p.returncode == 0, 'docker_command_failed' if args[0] == 'docker' else 'command_failed')
    return p.stdout.decode('utf-8')


def fetch_json(url):
    require(url == CHANNEL, 'untrusted_channel')
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'CPA-Upgrader/1'}), timeout=30) as response:
        data = response.read(8 * 1024 ** 2 + 1)
    require(len(data) <= 8 * 1024 ** 2, 'channel_too_large')
    return json.loads(data.decode('utf-8'))


def validate_release(r):
    component = r.get('component')
    require(component in SERVICES and RELEASE_ID.match(r.get('releaseId', '')), 'invalid_release')
    require(IMAGE.match(r.get('imageId', '')) and VERSION.match(r.get('version', '')), 'invalid_image_identity')
    require(re.match(r'^[a-f0-9]{40}$', r.get('sourceCommit', '')), 'invalid_source_commit')
    repository = 'ghcr.io/qazwsxedc-coder/' + ('cpa-cli' if component == 'cli' else 'cpa-manager')
    require(r.get('imageTag') == repository + ':' + r['version'], 'untrusted_image')
    require(r.get('imageDigest', '').startswith(repository + '@sha256:') and
            IMAGE.match(r['imageDigest'].split('@')[-1]), 'unpinned_image')
    require(r.get('imageSource') == ('official' if component == 'cli' else 'custom'), 'invalid_image_source')
    sources = r.get('allowedFromImageIds', [])
    require(len(sources) == 1 and IMAGE.match(sources[0]) and sources[0] != r['imageId'], 'invalid_compatibility_path')
    mode = r.get('migrationMode')
    require((mode == 'none' and r.get('migrationRequired') is False and r.get('rollbackDataCompatible') is True) or
            (component == 'manager' and mode == 'automatic-additive' and r.get('migrationRequired') is True and
             r.get('rollbackDataCompatible') is False), 'unsafe_migration')
    evidence = r.get('evidence', {})
    require(hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest() ==
            r.get('evidenceSha256'), 'invalid_evidence')
    require(evidence.get('startupPassed') is True and evidence.get('apiSmokePassed') is True and
            evidence.get('imageId') == r['imageId'] and evidence.get('allowedFromImageIds') == sources,
            'unverified_candidate')
    require(mode != 'automatic-additive' or (evidence.get('migrationRehearsalPassed') is True and
            evidence.get('officialCompatible') is True and evidence.get('recognizedAdditive') is True), 'migration_unverified')


class Executor:
    def __init__(self, root, runtime):
        self.root, self.runtime = Path(root), runtime

    def initialize(self):
        for name in ('catalog', 'requests', 'status', 'journal'):
            plain(self.root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (self.root / 'catalog/releases.json').exists():
            atomic_json(self.root / 'catalog/releases.json', {'schemaVersion': 1, 'releases': []})
        atomic_json(self.root / 'catalog/capabilities.json', {'schemaVersion': 1, 'prepareOfficialCLI': False, 'prepareCustomManager': False})

    def last_result(self):
        return read_json(self.root / 'status/last-result.json')

    def failed_releases(self):
        return read_json(self.root / 'journal/failed.json', [])

    def enqueue(self, release, automatic=False):
        request = {'schemaVersion': 1, 'id': str(uuid.uuid4()), 'component': release['component'],
                   'releaseId': release['releaseId'], 'createdAt': utc()}
        if (self.root / 'requests/check.json').exists():
            return None
        try:
            fd = os.open(str(plain(self.root / 'requests/active.json')), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return None
        with os.fdopen(fd, 'w') as stream:
            json.dump(request, stream)
            stream.flush()
            os.fsync(stream.fileno())
        sync_dir(self.root / 'requests')
        if automatic:
            atomic_json(self.root / ('journal/auto-' + request['id'] + '.json'), {'day': shanghai().date().isoformat()})
        return request

    def save(self, job, state, code=''):
        job.update(state=state, step=state, updatedAt=utc(), message={
            'preflight': '正在核对镜像和兼容来源', 'backup': '正在制作一致备份',
            'installing': '正在切换已验证镜像', 'checking': '正在核对实际版本和服务状态',
            'succeeded': '升级完成，实际镜像和服务检查通过', 'failed': '安装未完成，原版本已保留',
            'rolled_back': '已切回旧镜像，现有数据保留', 'manual_recovery': '已暂停后续升级，请核对保留的现场'
        }.get(state, state))
        if code:
            job['errorCode'] = code
        atomic_json(self.root / ('journal/' + job['id'] + '.json'), job)
        fields = ('schemaVersion id component releaseId state step message createdAt updatedAt '
                  'fromVersion toVersion backupPath errorCode').split()
        public = {key: job[key] for key in fields if key in job}
        atomic_json(self.root / ('status/' + job['id'] + '.json'), public)
        if state in TERMINAL or state == 'manual_recovery':
            atomic_json(self.root / 'status/last-result.json', public)
            if state != 'succeeded':
                failed = self.failed_releases()
                if job['releaseId'] not in failed:
                    atomic_json(self.root / 'journal/failed.json', failed + [job['releaseId']])
            if state == 'manual_recovery':
                atomic_json(self.root / 'journal/pause.json', {'reason': code or 'manual_recovery', 'jobId': job['id']})

    def finish(self, job):
        if job['state'] in TERMINAL:
            request = read_json(self.root / 'requests/active.json')
            if request and request.get('id') == job['id']:
                (self.root / 'requests/active.json').unlink()
                sync_dir(self.root / 'requests')

    def recover(self, job, code):
        try:
            current = self.runtime.current(job['component'])
            release = job.get('release', {})
            # A command timeout can leave Docker working in the background.
            require(code != 'docker_state_uncertain', 'docker_state_uncertain')
            if current['imageId'] == job.get('fromImageId'):
                require(job.get('mutation') != 'switch_pending', 'switch_not_confirmed')
                if not current['running']:
                    self.runtime.start(job['component'])
                self.runtime.check(job['component'], job['fromImageId'], None, job)
                self.save(job, 'failed', code)
            elif (current['imageId'] == release.get('imageId') and job.get('backupComplete') and
                  release.get('migrationMode') == 'none' and release.get('rollbackDataCompatible') is True):
                job['mutation'] = 'rollback_pending'
                self.save(job, 'installing', code)
                self.runtime.install(job['component'], job['fromImageId'])
                job['mutation'] = ''
                self.runtime.check(job['component'], job['fromImageId'], None, job)
                self.save(job, 'rolled_back', code)
            else:
                self.save(job, 'manual_recovery', code)
        except Exception as error:
            self.save(job, 'manual_recovery', safe_code(error))
        self.finish(job)

    def process(self):
        request = read_json(self.root / 'requests/active.json')
        if not request:
            return
        require(set(request) == set(('schemaVersion', 'id', 'component', 'releaseId', 'createdAt')) and
                request.get('schemaVersion') == 1 and ID.match(request.get('id', '')) and
                request.get('component') in SERVICES and RELEASE_ID.match(request.get('releaseId', '')),
                'invalid_request')
        job = read_json(self.root / ('journal/' + request['id'] + '.json'), dict(request))
        require(all(job.get(k) == request[k] for k in request), 'job_identity_conflict')
        if job.get('state') in TERMINAL or job.get('state') == 'manual_recovery':
            self.finish(job)
            return
        if job.get('state') in ('backup', 'installing', 'checking'):
            try:
                current = self.runtime.current(job['component'])
                if job.get('mutation') == 'rollback_pending':
                    require(current['imageId'] == job['fromImageId'], 'rollback_not_confirmed')
                    self.runtime.check(job['component'], job['fromImageId'], None, job)
                    self.save(job, 'rolled_back', 'reconciled_after_restart')
                elif job.get('backupComplete') and current['imageId'] == job['release']['imageId']:
                    job['mutation'] = ''
                    self.save(job, 'checking')
                    self.runtime.check(job['component'], job['release']['imageId'], job['release'], job)
                    self.save(job, 'succeeded')
                else:
                    raise Fault('interrupted_transition')
                self.finish(job)
            except Exception as error:
                self.recover(job, safe_code(error))
            return
        try:
            self.save(job, 'preflight')
            require(not read_json(self.root / 'journal/pause.json'), 'upgrades_paused')
            require(request['releaseId'] not in self.failed_releases(), 'candidate_already_failed')
            catalog = read_json(self.root / 'catalog/releases.json')
            matches = [r for r in catalog['releases'] if r['releaseId'] == request['releaseId'] and r['component'] == request['component']]
            require(len(matches) == 1, 'candidate_not_prepared')
            release = matches[0]
            current = self.runtime.current(job['component'])
            require(current['imageId'] in release['allowedFromImageIds'] and current['imageId'] != release['imageId'], 'source_not_compatible')
            job.update(release=release, fromImageId=current['imageId'], fromVersion=current['version'],
                       toVersion=release['version'], oldContainerId=current['containerId'], backupComplete=False)
            self.runtime.preflight(job['component'], release)
            job['mutation'] = 'stop_pending'
            self.save(job, 'backup')
            self.runtime.stop(job['component'])
            job['mutation'] = ''
            self.save(job, 'backup')
            job['backupPath'] = self.runtime.backup(job)
            job['backupComplete'], job['mutation'] = True, 'switch_pending'
            self.save(job, 'installing')
            self.runtime.install(job['component'], release['imageId'])
            job['mutation'] = ''
            self.save(job, 'checking')
            self.runtime.check(job['component'], release['imageId'], release, job)
            self.save(job, 'succeeded')
            self.finish(job)
        except Exception as error:
            if job.get('state') == 'preflight':
                self.save(job, 'failed', safe_code(error))
                self.finish(job)
            else:
                self.recover(job, safe_code(error))


def safe_code(error):
    return str(error) if isinstance(error, Fault) and re.match(r'^[a-z_]{1,100}$', str(error)) else 'operation_failed'


class DockerRuntime:
    def __init__(self, config, protocol, config_file=None):
        self.config, self.protocol = config, Path(protocol)
        self.configFile = Path(config_file) if config_file else None
        self.work = plain(config['workingDir'])
        self.compose = ['docker', 'compose', '--project-name', config['project'], '--project-directory', str(self.work)]
        for path in config['composeFiles']:
            require(plain(path).is_file() and Path(path).parent == self.work, 'compose_path_changed')
            self.compose += ['-f', path]
        self.overlay = plain(config['overlayFile'])
        require(self.overlay.is_file(), 'compose_overlay_missing')

    def compose_command(self):
        return self.compose + ['-f', str(self.overlay), '-f', str(self.config['pinFile'])]

    def inspect(self, component):
        c = json.loads(command(['docker', 'inspect', self.config['containers'][component]]))[0]
        labels = c['Config']['Labels']
        require(labels.get('com.docker.compose.project') == self.config['project'] and
                labels.get('com.docker.compose.service') == SERVICES[component] and
                labels.get('com.docker.compose.project.working_dir') == str(self.work), 'container_identity_changed')
        require(c['HostConfig']['PortBindings'] == self.config['ports'][component], 'ports_changed')
        mounts = {m['Destination']: [m['Source'], m['RW']] for m in c['Mounts']}
        allowed_mounts = [self.config['mounts'][component]]
        if self.config.get('desiredMounts', {}).get(component):
            allowed_mounts.append(self.config['desiredMounts'][component])
        require(mounts in allowed_mounts, 'mounts_changed')
        require(not c['HostConfig'].get('Privileged') and not any(m['Destination'] == '/var/run/docker.sock' for m in c['Mounts']), 'unsafe_container')
        return c

    def current(self, component):
        c = self.inspect(component)
        image = json.loads(command(['docker', 'image', 'inspect', c['Image']]))[0]
        labels = image['Config'].get('Labels') or {}
        version = labels.get('org.opencontainers.image.version') or self.config['baseline'][component]['version']
        channel = read_json(self.protocol / 'catalog/channel.json', {'releases': []})
        for release in channel['releases']:
            if release['imageId'] == c['Image']:
                version = release['version']
        return {'imageId': c['Image'], 'version': version, 'running': c['State']['Running'],
                'healthy': c['State'].get('Health', {}).get('Status') == 'healthy', 'containerId': c['Id']}

    def preflight(self, component, release):
        current = self.current(component)
        require(current['running'] and current['healthy'], 'source_not_healthy')
        image = json.loads(command(['docker', 'image', 'inspect', release['imageId']]))[0]
        require(image['Id'] == release['imageId'] and image['Os'] == 'linux' and image['Architecture'] == 'amd64', 'image_not_prepared')
        require(release['imageDigest'] in image.get('RepoDigests', []), 'digest_mismatch')
        command(self.compose_command() + ['config', '--quiet'])
        require(shutil.disk_usage(str(self.work)).free > self.backup_size(component) * 2 + 1024 ** 3, 'insufficient_backup_space')

    def backup_sources(self, component):
        sources = {Path(value[0]) for dest, value in self.config['mounts'][component].items() if not dest.startswith('/upgrades/')}
        sources.update(Path(path) for path in self.config['composeFiles'])
        for path in (self.work / '.env',):
            if path.exists():
                sources.add(path)
        return sorted(sources)

    def backup_size(self, component):
        total = 0
        for source in self.backup_sources(component):
            for path in ([source] if source.is_file() else source.rglob('*')):
                plain(path)
                if path.is_file():
                    require(stat.S_ISREG(path.stat().st_mode), 'non_regular_data')
                    total += path.stat().st_size
        return total

    def stop(self, component):
        self.inspect(component)
        command(['docker', 'stop', '--time', '60', self.config['containers'][component]], 85)
        require(not self.inspect(component)['State']['Running'], 'stop_not_confirmed')

    def start(self, component):
        self.inspect(component)
        command(['docker', 'start', self.config['containers'][component]], 45)

    def backup(self, job):
        component = job['component']
        require(not self.current(component)['running'], 'cold_backup_required')
        target = plain(self.config['backupDir']) / job['id']
        target.mkdir(mode=0o700, parents=True)
        def hashes(source):
            return {str(p.relative_to(source)): sha(plain(p)) for p in source.rglob('*') if p.is_file()}
        for index, source in enumerate(self.backup_sources(component)):
            plain(source)
            dest = target / ('{:02d}-'.format(index) + source.name)
            if source.is_dir():
                before = hashes(source)
                shutil.copytree(str(source), str(dest))
                require(before == hashes(source) == hashes(dest), 'backup_verification_failed')
            else:
                before = sha(source)
                shutil.copy2(str(source), str(dest))
                require(before == sha(source) == sha(dest), 'backup_verification_failed')
        require(not self.current(component)['running'], 'source_restarted_during_backup')
        atomic_json(target / 'source-references.json', read_json(self.protocol / 'catalog/channel.json', {}))
        atomic_json(target / 'manifest.json', {'schemaVersion': 1, 'jobId': job['id'], 'coldBackup': True,
                    'oldImageId': job['fromImageId'], 'targetImageId': job['release']['imageId'], 'createdAt': utc(),
                    'sources': [str(p) for p in self.backup_sources(component)]})
        # Flush cold copies before changing the fixed image file.
        for path in target.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        sync_dir(target)
        return str(target)

    def install(self, component, image):
        require(IMAGE.match(image), 'invalid_image_identity')
        command(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
        pins = {'services': {SERVICES[c]: {'image': image if c == component else self.current(c)['imageId'],
                                          'pull_policy': 'never'} for c in SERVICES}}
        atomic_json(Path(self.config['pinFile']), pins)  # JSON is a YAML subset.
        command(self.compose_command() + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', SERVICES[component]], 150)
        if component == 'manager' and self.configFile:
            self.config['mounts'][component] = self.config['desiredMounts'][component]
            atomic_json(self.configFile, self.config)

    def http(self, component, path, authenticated=False):
        port = 8317 if component == 'cli' else 18317
        headers = {}
        if authenticated:
            # Credential goes only into a loopback HTTP header, never argv/logs.
            headers['Authorization'] = 'Bearer ' + Path(self.config['managerKeyFile']).read_text().strip()
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:' + str(port) + path, headers=headers), timeout=10) as r:
            return json.loads(r.read(4 * 1024 ** 2).decode())

    def check(self, component, image, release, job):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = self.current(component)
            require(current['imageId'] == image, 'unexpected_runtime_image')
            if current['running'] and current['healthy']:
                try:
                    self.http(component, '/healthz' if component == 'cli' else '/health')
                    if component == 'manager':
                        info = self.http('manager', '/usage-service/info')
                        actual = info.get('managerVersion') or info.get('version')
                        expected = release['version'] if release else job['fromVersion']
                        require(actual == expected, 'version_mismatch')
                        self.http('manager', '/status', True)
                    else:
                        text = command(['docker', 'exec', self.config['containers']['cli'], '/CLIProxyAPI/CLIProxyAPI', '-h'])
                        expected = (release['version'] if release else job['fromVersion']).lstrip('v')
                        require(('CLIProxyAPI Version: ' + expected + ',') in text or
                                ('Version: ' + expected + ',') in text, 'version_mismatch')
                    break
                except Fault:
                    raise
                except Exception:
                    pass
            time.sleep(2)
        else:
            raise Fault('startup_failed')
        if component == 'manager':
            # Persist the wall-clock deadline so restarts cannot extend migration forever.
            journal = self.protocol / ('journal/' + job['id'] + '.json')
            if not job.get('migrationDeadline'):
                job['migrationDeadline'] = time.time() + 1800
                atomic_json(journal, job)
            while time.time() < job['migrationDeadline']:
                data = self.http('manager', '/status', True)
                migration = data.get('dataMigration', {}).get('status')
                require(migration != 'failed', 'migration_failed')
                require(not data.get('databaseMaintenance', {}).get('required'), 'database_maintenance_required')
                if migration == 'completed':
                    return
                time.sleep(5)
            raise Fault('migration_timeout')


def shanghai():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


@contextlib.contextmanager
def maintenance_lock():
    import fcntl
    files = []
    try:
        for name in ('/run/cpa-maintenance.lock', '/run/cpa-security-backup.lock'):
            stream = open(name, 'a')
            files.append(stream)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for unit in ('cpa-security-backup.service', 'dnf-automatic-install.service'):
            value = command(['systemctl', 'show', unit, '-p', 'ActiveState', '--value']).strip()
            require(value in ('inactive', 'failed'), 'maintenance_busy')
        yield
    finally:
        for stream in reversed(files):
            stream.close()


class Daemon:
    def __init__(self, config_file):
        self.config_file = Path(config_file)
        self.config = read_json(self.config_file)
        self.root = Path(self.config['protocolDir'])
        self.runtime = DockerRuntime(self.config, self.root, self.config_file)
        self.executor = Executor(self.root, self.runtime)
        self.executor.initialize()
        self.stop = threading.Event()
        self.prepare_error = ''
        self.last_prepare = 0

    def heartbeat(self):
        while not self.stop.is_set():
            try:
                self.publish_status()
            except Exception:
                pass  # Stale heartbeat is visible; never fabricate healthy status.
            self.stop.wait(5)

    def publish_status(self):
        current = {c: self.runtime.current(c) for c in SERVICES}
        channel = read_json(self.root / 'catalog/channel.json', {'releases': [], 'preparation': {}})
        latest = dict((c, current[c]['version']) for c in SERVICES)
        latest.update(channel.get('latest', {}))
        atomic_json(self.root / 'status/host.json', {'schemaVersion': 1, 'executorVersion': '1', 'updatedAt': utc(),
                    'current': {c: {k: v for k, v in current[c].items() if k in ('version', 'imageId')} for c in SERVICES}, 'latest': latest})
        config = read_json(self.config_file)
        now = shanghai()
        next_time = now.replace(hour=5, minute=0, second=0, microsecond=0)
        attempts = read_json(self.root / 'journal/daily.json', {})
        if now.hour >= 6 or (now.hour >= 5 and all(attempts.get(c) == now.date().isoformat() for c in SERVICES)):
            next_time += dt.timedelta(days=1)
        elif now.hour >= 5:
            next_time = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        pause = read_json(self.root / 'journal/pause.json', {})
        pending_failure = next((r['releaseId'] for r in channel['releases'] if r['releaseId'] in self.executor.failed_releases() and
                                current[r['component']]['imageId'] in r['allowedFromImageIds']), '')
        preparation = channel.get('preparation', {})
        reason = pause.get('reason') or self.prepare_error or ('candidate_failed:' + pending_failure if pending_failure else '')
        if not reason and preparation.get('state') == 'failed':
            reason = preparation.get('reason', 'preparation_failed')
        atomic_json(self.root / 'status/automation.json', {'schemaVersion': 1, 'enabled': config.get('enabled', False),
                    'updatedAt': utc(), 'timezone': 'Asia/Shanghai', 'nextRunAt': next_time.isoformat() if config.get('enabled') else '',
                    'pauseReason': reason, 'lastResult': self.executor.last_result(), 'preparation': preparation,
                    'current': {c: {k: v for k, v in current[c].items() if k in ('version', 'imageId')} for c in SERVICES}})

    def prepare(self):
        channel = fetch_json(CHANNEL)
        require(channel.get('schemaVersion') == 1 and isinstance(channel.get('releases'), list), 'invalid_channel')
        seen = set()
        edges = set()
        for release in channel['releases']:
            validate_release(release)
            require(release['releaseId'] not in seen, 'duplicate_release')
            seen.add(release['releaseId'])
            edge = (release['component'], release['allowedFromImageIds'][0])
            require(edge not in edges, 'ambiguous_upgrade_path')
            edges.add(edge)
        previous = read_json(self.root / 'catalog/channel.json', {'releases': []})
        by_id = {r['releaseId']: r for r in channel['releases']}
        for old in previous['releases']:
            require(by_id.get(old['releaseId']) == old, 'published_candidate_changed')
        atomic_json(self.root / 'catalog/channel.json', channel)
        prepared = []
        for component in ('cli', 'manager'):
            current = self.runtime.current(component)
            candidates = [r for r in channel['releases'] if r['component'] == component and current['imageId'] in r['allowedFromImageIds']]
            if not candidates:
                continue
            release = candidates[0]
            if release['releaseId'] in self.executor.failed_releases():
                continue
            command(['docker', 'pull', '--platform', 'linux/amd64', release['imageDigest']], 600)
            actual = json.loads(command(['docker', 'image', 'inspect', release['imageDigest']]))[0]
            require(actual['Id'] == release['imageId'] and actual['Architecture'] == 'amd64' and actual['Os'] == 'linux', 'download_identity_mismatch')
            prepared.append({k: release[k] for k in PROTOCOL_FIELDS})
        # Only publish a complete ready catalog after downloads succeed.
        atomic_json(self.root / 'catalog/releases.json', {'schemaVersion': 1, 'releases': prepared})
        self.prepare_error = ''

    def daily(self):
        now = shanghai()
        config = read_json(self.config_file)
        if not config.get('enabled') or not 5 <= now.hour < 6 or read_json(self.root / 'journal/pause.json'):
            return
        if (self.root / 'requests/active.json').exists() or (self.root / 'requests/check.json').exists():
            return
        attempts = read_json(self.root / 'journal/daily.json', {})
        day = now.date().isoformat()
        releases = read_json(self.root / 'catalog/releases.json')['releases']
        for component in ('cli', 'manager'):
            if attempts.get(component) == day:
                continue
            current = self.runtime.current(component)
            candidate = next((r for r in releases if r['component'] == component and current['imageId'] in r['allowedFromImageIds'] and
                              r['releaseId'] not in self.executor.failed_releases()), None)
            if candidate:
                # Daily gate and the queue creation occur under the host lock.
                request = self.executor.enqueue(candidate, automatic=True)
                if request:
                    attempts[component] = day
                    atomic_json(self.root / 'journal/daily.json', attempts)
                return
            attempts[component] = day
            atomic_json(self.root / 'journal/daily.json', attempts)

    def run(self):
        import fcntl
        lock = open(str(self.root / 'worker.lock'), 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        thread = threading.Thread(target=self.heartbeat)
        thread.daemon = True
        thread.start()
        while True:
            try:
                # Resume journals before any new network preparation.
                if (self.root / 'requests/active.json').exists():
                    with maintenance_lock():
                        self.executor.process()
                check = read_json(self.root / 'requests/check.json')
                if check:
                    require(ID.match(check.get('id', '')) and check.get('schemaVersion') == 1, 'invalid_check_request')
                if check or time.monotonic() - self.last_prepare >= 3600:
                    self.last_prepare = time.monotonic()
                    try:
                        self.prepare()
                    except Exception as error:
                        self.prepare_error = safe_code(error)
                    if check:
                        atomic_json(self.root / 'status/check.json', dict(check, state='failed' if self.prepare_error else 'succeeded',
                                    updatedAt=utc(), message=self.prepare_error or '已读取已发布版本并准备镜像'))
                        (self.root / 'requests/check.json').unlink()
                with maintenance_lock():
                    self.daily()
            except (BlockingIOError, Fault) as error:
                self.prepare_error = safe_code(error)
            except Exception:
                self.prepare_error = 'executor_requires_review'
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/cpa-upgrader/config.json')
    parser.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    daemon = Daemon(args.config)
    if args.prepare:
        daemon.prepare()
        daemon.publish_status()
    else:
        daemon.run()


if __name__ == '__main__':
    main()
