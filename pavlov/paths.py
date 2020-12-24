import re
import pandas as pd
from contextlib import contextmanager
from pathlib import Path
import json
from portalocker import RLock, AlreadyLocked
import shutil
import pytest
from aljpy import humanhash
import uuid

ROOT = 'output/pavlov'

### Basic file stuff

def root():
    root = Path(ROOT)
    if not root.exists():
        root.mkdir(exist_ok=True, parents=True)
    return root

def mode(prefix, x):
    if isinstance(x, str):
        return prefix + 't'
    if isinstance(x, bytes):
        return prefix + 'b'
    raise ValueError()

def assert_file(path, default):
    try:
        path.parent.mkdir(exist_ok=True, parents=True)
        with RLock(path, mode('x+', default), fail_when_locked=True) as f:
            f.write(default)
    except (FileExistsError, AlreadyLocked):
        pass

def read(path, mode):
    with RLock(path, mode) as f:
        return f.read()

def read_default(path, default):
    assert_file(path, default)
    return read(path, mode('r', default))

def write(path, contents):
    with RLock(path, mode('w', contents)) as f:
        f.write(contents)

def dir(run):
    return root() / run

def delete(run):
    assert run != ''
    shutil.rmtree(dir(run))

### Info file stuff

def infopath(run):
    return dir(run) / '_info.json'

def info(run, val=None, create=False):
    path = infopath(run)
    if val is None and create:
        return json.loads(read_default(path, r'{}'))
    elif val is None:
        return json.loads(read(path, 'rt'))
    elif create:
        assert isinstance(val, dict)
        assert_file(path, r'{}')
        write(path, json.dumps(val))
        return path
    else:
        assert isinstance(val, dict)
        write(path, json.dumps(val))
        return path

@contextmanager
def infoupdate(run, create=False):
    # Make sure it's created
    info(run, create=create)
    # Now grab the lock and do whatever
    with RLock(infopath(run), 'r+t') as f:
        i = json.loads(f.read())
        yield i
        f.truncate(0)
        f.seek(0)
        f.write(json.dumps(i))

def infos():
    return {dir: info(dir.name) for dir in root().iterdir()}

### Run creation stuff

def run_name(now=None):
    now = (now or pd.Timestamp.now('UTC')).strftime('%Y-%m-%d %H-%M-%S')
    suffix = humanhash(str(uuid.uuid4()), n=2)
    return f'{now} {suffix}'

def new_run(**kwargs):
    now = pd.Timestamp.now('UTC')
    run = run_name(now)
    kwargs = {**kwargs, '_created': str(now), '_files': {}}
    info(run, kwargs, create=True)
    return run

### File stuff

def new_file(run, pattern, info={}):
    match = re.fullmatch(r'(?P<name>.*)\.(?P<suffix>.*)', pattern)
    salt = humanhash(str(uuid.uuid4()), n=1)
    name = f'{match.group("name")}-{salt}.{match.group("suffix")}'

    with infoupdate(run) as i:
        assert name not in i['_files']
        i['_files'][name] = {'_created': str(pd.Timestamp.now('UTC')), **info}
    return dir(run) / name

def fileinfos(run):
    return info(run)['_files']

def filepath(run, name):
    return dir(run) / name

### Tests

def in_test_dir(f):

    def wrapped(*args, **kwargs):
        global ROOT
        old_ROOT = ROOT
        ROOT = 'output/pavlov-test'
        if Path(ROOT).exists():
            shutil.rmtree(ROOT)

        try:
            result = f(*args, **kwargs)
        finally:
            ROOT = old_ROOT
        return result
    
    return wrapped

@in_test_dir
def test_info():

    # Check reading from a nonexistant file errors
    with pytest.raises(FileNotFoundError):
        info('test')

    # Check trying to write to a nonexistant file errors
    with pytest.raises(FileNotFoundError):
        with infoupdate('test') as (i, writer):
            pass

    # Check we can create a file
    i = info('test', create=True)
    assert i == {}
    # and read from it
    i = info('test')
    assert i == {}

    # Check we can write to an already-created file
    with infoupdate('test') as (i, writer):
        assert i == {}
        writer({'a': 1})
    # and read it back
    i = info('test')
    assert i == {'a': 1}

    # Check we can write to a not-yet created file
    delete('test')
    with infoupdate('test', create=True) as (i, writer):
        assert i == {}
        writer({'a': 1})
    # and read it back
    i = info('test')
    assert i == {'a': 1}

@in_test_dir
def test_infos():
    info('test-1', {'idx': 1}, create=True)
    info('test-2', {'idx': 2}, create=True)

    i = infos()
    assert len(i) == 2
    assert i['test-1'] == {'idx': 1}
    assert i['test-2'] == {'idx': 2}

@in_test_dir
def test_new_run():
    run = new_run(desc='test')

    i = info(run)
    assert i['desc'] == 'test'
    assert i['_created']
    assert i['_files'] == {}


@in_test_dir
def test_new_file():
    run = new_run()
    path = new_file(run, 'test.txt', {'hello': 'one'})
    name = path.name

    path.write_text('contents')

    i = fileinfos(run)[name]
    assert i['hello'] == 'one'
    assert filepath(run, name).read_text()  == 'contents'