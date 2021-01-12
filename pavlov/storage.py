import pandas as pd
import torch
import numpy as np
from . import runs, files, tests
from io import BytesIO

LATEST = 'storage.latest.pkl'
SNAPSHOT = 'storage.snapshot.{n}.pkl'
NAMED = 'storage.named.{name}.pkl'

def collapse(state_dict, depth=np.inf):
    if depth == 0:
        return state_dict
    if not isinstance(state_dict, dict):
        return state_dict

    collapsed = {}
    for prefix, d in state_dict.items():
        for k, v in d.items():
            collapsed[f'{prefix}.{k}'] = collapse(v, depth-1)
    return collapsed

def expand(state_dict, depth=np.inf):
    if depth == 0:
        return state_dict
    if not isinstance(state_dict, dict):
        return state_dict

    d = {}
    for k, v in state_dict.items():
        parts = k.split('.')
        [head] = parts[:1]
        tail = '.'.join(parts[1:])
        d.setdefault(head, {})[tail] = expand(v, depth-1)
    return d

def state_dicts(**objs):
    dicts = {}
    for k, v in objs.items():
        if isinstance(v, dict):
            dicts[k] = state_dicts(**v)
        elif hasattr(v, 'state_dict'):
            dicts[k] = v.state_dict()
        else:
            dicts[k] = v
    return dicts

def _save_raw(path, bs):
    path.with_suffix('.tmp').write_bytes(bs)
    path.with_suffix('.tmp').rename(path)

def _save(path, objs):
    #TODO: Is there a better way to do this?
    bs = BytesIO()
    torch.save(objs, bs)
    _save_raw(path, bs.getvalue())

def _load(path, device='cpu'):
    return torch.load(path, map_location=device)

def save_latest(run, objs):
    path = files.path(run, LATEST)
    if not path.exists():
        files.new_file(run, LATEST)
    _save(path, objs)

def load_latest(run=-1, device='cpu'):
    path = files.path(run, LATEST)
    return _load(path, device)

def timestamp_latest(run=-1):
    return pd.Timestamp(files.path(run, LATEST).stat().st_mtime, unit='s')

def throttled_latest(run, objs, throttle):
    if files.path(run, LATEST).exists():
        last = pd.to_datetime(files.info(run, LATEST)['_created'])
    else:
        last = pd.Timestamp(0, unit='s', tz='UTC')

    if tests.timestamp() > last + pd.Timedelta(throttle, 's'):
        save_latest(run, objs)

def snapshot(run, objs, **kwargs):
    path = files.new_file(run, SNAPSHOT, **kwargs)
    _save(path, objs)

def snapshots(run=-1):
    return {files.idx(run, fn): {**info, 'path': files.path(run, fn)} for fn, info in files.seq(run, SNAPSHOT).items()}

def load_snapshot(run=-1, n=-1, device='cpu'):
    n = list(snapshots(run))[n]
    path = files.path(run, SNAPSHOT.format(n=n))
    return _load(path, device)

def throttled_snapshot(run, objs, throttle):
    files = snapshots(run)
    if files:
        last = pd.to_datetime(max(f['_created'] for f in files.values()))
    else:
        last = pd.Timestamp(0, unit='s', tz='UTC')

    if tests.timestamp() > last + pd.Timedelta(throttle, 's'):
        snapshot(run, objs)

def named(run, name, objs):
    name = NAMED.format(name=name)
    if not files.exists(run, name):
        files.new_file(run, name)
    _save(files.path(run, name), objs)

def raw(run, name, bs):
    name = NAMED.format(name=name)
    path = files.new_file(run, name)
    _save_raw(path, bs)

def throttled_raw(run, name, f, throttle):
    name = NAMED.format(name=name)
    path = files.path(run, name)
    if path.exists():
        last = pd.to_datetime(files.info(run, LATEST)['_created'])
    else:
        files.new_file(run, name)
        last = pd.Timestamp(0, unit='s', tz='UTC')

    if tests.timestamp() > last + pd.Timedelta(throttle, 's'):
        _save_raw(path, f())