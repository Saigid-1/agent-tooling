"""Bounded installed-metadata observations; never proof of source/build equivalence."""
import importlib.metadata
import json


def _installed_distribution(name):
    distribution = importlib.metadata.distribution(name)
    direct_url = distribution.read_text('direct_url.json')
    return {'version': distribution.version, 'direct_url': direct_url}


def installed_observations(model, provider=None):
    observations = {}
    for name, repo_key in sorted(model.get('distributions', {}).items()):
        row = {'repository_key': repo_key, 'status': 'unavailable', 'version': None,
               'source_pointer': None, 'artifact_equivalence': 'not_proven'}
        try:
            observed = (provider or _installed_distribution)(name)
            if (not isinstance(observed, dict) or not isinstance(observed.get('version'), str)
                    or not observed['version'].strip() or len(observed['version'].encode('utf-8')) > 256):
                raise ValueError('malformed distribution metadata')
            row['version'] = observed['version']
            pointer = observed.get('direct_url')
            if pointer is not None:
                if not isinstance(pointer, str) or len(pointer.encode('utf-8')) > 16384:
                    raise ValueError('malformed direct URL metadata')
                try:
                    direct = json.loads(pointer)
                except (TypeError, ValueError, RecursionError):
                    raise ValueError('malformed direct URL metadata')
                if (not isinstance(direct, dict) or not isinstance(direct.get('url'), str)
                        or not direct['url'].strip()):
                    raise ValueError('malformed direct URL metadata')
                dir_info = direct.get('dir_info', {})
                if not isinstance(dir_info, dict):
                    raise ValueError('malformed direct URL metadata')
                editable = dir_info.get('editable', False)
                if not isinstance(editable, bool):
                    raise ValueError('malformed editable flag')
                row['source_pointer'] = {'kind': 'direct_url', 'editable': editable}
                row['status'] = 'unverified'
            else:
                row['status'] = 'observed'
        except importlib.metadata.PackageNotFoundError:
            row['reason'] = 'not_installed'
        except (OSError, TypeError, ValueError, UnicodeError):
            row['status'] = 'unverified'
            row['reason'] = 'metadata_unavailable_or_malformed'
        observations[name] = row
    return observations
