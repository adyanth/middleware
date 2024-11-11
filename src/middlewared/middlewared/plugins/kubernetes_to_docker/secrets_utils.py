import binascii
import contextlib
import gzip
import json
import os
from base64 import b64decode

import yaml

from .yaml import SerializedDatesFullLoader


HELM_SECRET_PREFIX = 'sh.helm.release'


def list_secrets(secrets_dir: str) -> dict[str, dict[str, dict]]:
    secrets = {
        'helm_secret': {
            'secret_name': None,
            'name': None,
        },
        'release_secrets': {},
    }
    with os.scandir(secrets_dir) as it:
        for entry in it:
            if not entry.is_file():
                continue

            if entry.name.startswith(HELM_SECRET_PREFIX):
                if secrets['helm_secret']['secret_name'] is None or entry.name > secrets['helm_secret']['secret_name']:
                    secret_contents = get_secret_contents(entry.path, True).get('release', {})
                    secrets['helm_secret'].update({
                        'secret_name': entry.name,
                        **(secret_contents if all(
                            k in secret_contents and k for k in ('appVersion', 'config', 'name')
                        ) else {}),
                    })
            else:
                secrets['release_secrets'][entry.name] = get_secret_contents(entry.path)

    return secrets


def get_secret_contents(secret_path: str, helm_secret: bool = False) -> dict:
    with open(secret_path, 'r') as f:
        secret = yaml.load(f.read(), Loader=SerializedDatesFullLoader)

    if isinstance(secret.get('data'), dict) is False:
        return {}

    contents = {}
    for k, v in secret['data'].items():
        with contextlib.suppress(binascii.Error, gzip.BadGzipFile, KeyError, UnicodeDecodeError):
            if helm_secret:
                v = json.loads(gzip.decompress(b64decode(b64decode(v))).decode())
                for pop_k in ('manifest', 'info', 'version', 'namespace'):
                    v.pop(pop_k)
                chart = v.pop('chart')['metadata']
                for add_k in ('appVersion', 'name'):
                    v[add_k] = chart[add_k]
            else:
                v = b64decode(v).decode()

            contents[k] = v

    return contents
