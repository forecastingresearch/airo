"""Copy outside the checkout. Replace every fixture value privately."""
from copy import deepcopy

admin = {'ssh_user': 'operator', 'ssh_port': 22, 'ssh_strict_host_key_checking': 'yes'}
bootstrap_path = '/usr/local/bin:/usr/bin:/bin'
repository = {
    'repository': 'https://git.example.org/example/forecasts.git',
    'github_repository': 'example/forecasts',
    'path': bootstrap_path,
}

# Use keys generated for the dashboard publisher. Only their public keys go here.
# This illustrative key text is deliberately incomplete.
public_keys = {name: 'ssh-ed25519 REPLACE_WITH_' + name.upper() + '_PUBLIC_KEY'
               for name in ('airo', 'eeai', 'rsi')}


def publisher(name):
    return ['/usr/bin/python3', '/opt/fri-ops/publish_ssh.py', 'send',
            '--ssh-config', '/var/lib/forecast/.ssh/forecast-web.conf',
            '--target', 'forecast-web-' + name, '--receiver', '/opt/fri-ops/publish_ssh.py',
            '--root', '/srv/forecast-sites/' + name, '--verify', '/opt/fri-ops/verify-' + name]


projects = {name: {**repository, 'public_publisher': publisher(name),
                   'public_url': 'https://' + name + '.example.org'}
            for name in public_keys}
for name in ('eeai', 'rsi'):
    projects[name]['panel_runtime_config'] = '/etc/fri-dashboards/airo.json'

sites = {}
for name, downloads in {
    'airo': ['redlines-data.zip', 'forecasts.csv', 'rationales.csv'],
    'eeai': ['eeai-data.zip'],
    'rsi': [],
}.items():
    sites[name] = {
        'hostname': name + '.example.org',
        'certificate': '/etc/ssl/forecast/' + name + '/fullchain.pem',
        'certificate_key': '/etc/ssl/forecast/' + name + '/key.pem',
        'publisher_key': public_keys[name],
        'access': 'public' if name == 'airo' else 'basic',
        'downloads': downloads,
    }
    if sites[name]['access'] == 'basic':
        sites[name].update(
            password_file='/etc/nginx/private/' + name + '.htpasswd',
            verifier_credentials='/var/lib/forecast-publish/' + name + '.json',
        )

backup = {
    'repository_file': '/etc/fri-secrets/restic-repository',
    'password_file': '/etc/fri-secrets/restic-password',
    'enabled': False,
}

forecast_prod = [('forecast-prod.example.org', {
    **admin, 'settings': {'role': 'forecast-prod', 'staging': True,
                          'projects': projects, 'backup': deepcopy(backup)},
})]
# To install a pinned Node distribution, supply its verified private host path.
# forecast_prod[0][1]['settings']['node'] = {
#     'archive': '/var/cache/fri/node-v22.21.1-linux-x64.tar.xz',
#     'sha256': '<trusted SHA-256 for this archive>',
#     'version': '22.21.1',
# }
forecast_web = [('forecast-web.example.org', {
    **admin, 'settings': {'role': 'forecast-web', 'staging': True,
                          'sites': sites, 'backup': deepcopy(backup)},
})]
