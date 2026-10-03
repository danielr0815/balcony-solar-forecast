"""Semantic site diff; shares the learner's exact rounded fingerprint."""
from __future__ import annotations

from .config_fingerprint import site_fingerprint
from .types import SiteConfig


def site_changes(before: SiteConfig, after: SiteConfig) -> dict:
    old = {plane.name: plane.to_dict() for plane in before.planes}
    new = {plane.name: plane.to_dict() for plane in after.planes}
    modules = {}
    for name in sorted(old.keys() | new.keys()):
        if name not in old:
            modules[name] = ['added']
        elif name not in new:
            modules[name] = ['removed']
        elif old[name] != new[name]:
            modules[name] = sorted(key for key in old[name].keys() | new[name].keys()
                                   if old[name].get(key) != new[name].get(key))
    a, b = before.to_dict(), after.to_dict()
    fields = sorted(key for key in a.keys() | b.keys()
                    if key != 'planes' and a.get(key) != b.get(key))
    return {'model_changed': site_fingerprint(before) != site_fingerprint(after),
            'modules': modules, 'site_fields': fields}
