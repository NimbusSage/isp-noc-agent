#!/usr/bin/env python3
"""Build a restorable Wave ucfg tar.

Merges the Oxidized backup's values into the DEVICE's own config.json structure
(the device generated it, so its schema/version matches what ucfg expects),
keeps the device's real board.json, and reuses the device's tar member metadata.
"""
import json, sys, tarfile, io, os

dev_ucfg = sys.argv[1]   # device's own .ucfg (tar.gz) pulled off the radio
ox_json  = sys.argv[2]   # Oxidized config for this radio (banner + JSON)
out_tar  = sys.argv[3]

# ---- read device backup members + their tar metadata -------------------
with tarfile.open(dev_ucfg, 'r:gz') as t:
    members = {m.name: m for m in t.getmembers()}
    dev_cfg = json.load(t.extractfile('config.json'))
    dev_board = t.extractfile('board.json').read()

# ---- read the Oxidized backup (strip any Ubiquiti banner) -------------
raw = open(ox_json).read()
ox_cfg = json.loads(raw[raw.index('{'):])

# ---- deep merge: backup values win, device structural keys retained -----
def merge(dev, backup):
    if isinstance(dev, dict) and isinstance(backup, dict):
        out = dict(dev)
        for k, v in backup.items():
            out[k] = merge(dev.get(k), v) if k in dev else v
        return out
    if isinstance(dev, list) and isinstance(backup, list) and len(dev) == len(backup):
        return [merge(d, b) for d, b in zip(dev, backup)]
    return backup

merged = merge(dev_cfg, ox_cfg)

# ---- IMPORTANT: keep the DEVICE's version so ucfg accepts it ----------
merged['version'] = dev_cfg.get('version')

cfg_bytes = json.dumps(merged, indent=2).encode()

with tarfile.open(out_tar, 'w:gz') as out:
    for name, data in (('config.json', cfg_bytes), ('board.json', dev_board)):
        src = members.get(name)
        ti = tarfile.TarInfo(name)
        if src is not None:
            # reuse the device's own tar metadata (mode/uid/gid/uname/gname/mtime)
            ti.mode, ti.uid, ti.gid = src.mode, src.uid, src.gid
            ti.uname, ti.gname = src.uname, src.gname
            ti.mtime = src.mtime
        else:
            ti.mode, ti.mtime = 0o644, 0
        ti.size = len(data)
        out.addfile(ti, io.BytesIO(data))

print(f'built {out_tar}')
print(f'  device version kept : {dev_cfg.get("version")}')
print(f'  backup version was  : {ox_cfg.get("version")}')
print(f'  hostname            : {merged.get("system",{}).get("hostname")}')
print(f'  mgmt cidr           : {json.dumps(merged.get("network",{}).get("interfaces",{}))[:120]}')
print(f'  config.json bytes   : {len(cfg_bytes)}')