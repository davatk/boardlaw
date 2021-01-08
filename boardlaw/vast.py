from subprocess import Popen, check_output, PIPE, STDOUT
import time
import pandas as pd
import json
from pathlib import Path
import aljpy
from fabric import Connection
from patchwork.transfers import rsync
from logging import getLogger

log = getLogger(__name__)

DISK = 10
MAX_DPH = .5
MAX_INSTANCES = 3

def set_key():
    target = Path('~/.vast_api_key').expanduser()
    if not target.exists():
        key = json.loads(Path('credentials.json').read_text())['vast']
        target.write_text(key)

def invoke(command):
    set_key()
    while True:
        s = check_output(f'vast {command}', shell=True).decode()
        if s.startswith('failed with error 502'):
            log.info('Hit 502 error, trying again')
        else:
            return s

def offers():
    js = json.loads(invoke(f'search offers --raw --storage {DISK}'))
    return pd.DataFrame.from_dict(js)

def suggest():
    o = offers()
    viable = o.query('gpu_name == "RTX 2080 Ti" & num_gpus == 1 & cuda_max_good >= 11.1')
    return viable.sort_values('dph_total').iloc[0]

def launch():
    s = suggest()
    assert s.dph_total < MAX_DPH
    assert status() is None or len(status()) < MAX_INSTANCES
    label = aljpy.humanhash(n=2)
    resp = invoke(f'create instance {s.id}'
        ' --image andyljones/boardlaw'
        ' --onstart-cmd "tini -- dev.sh"'
        f' --disk {DISK}'
        f' --label {label}'
        ' --raw') 
    # Need to slice off the first chars of 'Started.', which are for some reason
    # printed along with the json. 
    # resp = resp[8:]
    resp = json.loads(resp)
    assert resp['success']
    return label

def destroy(label):
    id = status(label).id
    resp = invoke(f'destroy instance {id} --raw')
    assert resp.startswith('destroying instance')

def status(label=None):
    if label:
        s = status()
        if s is None: 
            raise ValueError('No instances')
        elif isinstance(label, int):
            return s.iloc[label]
        else:
            return s.loc[label]
    js = json.loads(invoke('show instances --raw'))
    if js:
        return pd.DataFrame.from_dict(js).set_index('label')

def wait(label):
    from IPython import display
    while True:
        s = status(label)
        display.clear_output(wait=True)
        if s['actual_status'] is None:
            print('Waiting on first status message')
        if s['actual_status'] == 'running':
            print('Ready')
            break
        else:
            print(f'({s["actual_status"]}) {s["status_msg"]}')


_cache = {}
def connection(label):
    # Get the vast key into place: `docker cp ~/.ssh/boardlaw_rsa boardlaw:/root/.ssh/`
    # Would be better to use SSH agent forwarding, if vscode's worked reliably :(
    if label not in _cache:
        s = status(label)
        _cache[label] = Connection(
            host=s.ssh_host, 
            user='root', 
            port=int(s.ssh_port), 
            connect_kwargs={'key_filename': ['/root/.ssh/vast_rsa']})
    return _cache[label]
    
def ssh_command(label):
    s = status(label)
    print(f'SSH_AUTH_SOCK="" ssh root@{s.ssh_host} -p {s.ssh_port} -o StrictHostKeyChecking=no -i /root/.ssh/vast_rsa')

def setup(label):
    conn = connection(label)
    conn.run('touch /root/.no_auto_tmux')
    conn.run('rm /etc/banner')
    
def deploy(label):
    conn = connection(label)
    rsync(conn, 
        source='.',
        target='/code',
        exclude=('.git',),
        rsync_opts='--filter=":- .gitignore"',
        strict_host_keys=False)

def run(label):
    conn = connection(label)
    conn.run('cd /code && python -c "from boardlaw.main import *; run()"', pty=False, disown=True)

def fetch(label):
    # rsync -r root@ssh4.vast.ai:/code/output/pavlov output/  -e "ssh -o StrictHostKeyChecking=no -i /root/.ssh/vast_rsa -p 37481"
    conn = connection(label)
    [keyfile] = conn.connect_kwargs['key_filename']
    command = f"""rsync -r -e "ssh -o StrictHostKeyChecking=no -i {keyfile} -p {conn.port}" {conn.user}@{conn.host}:/code/output/pavlov output/"""
    return Popen(command, shell=True, stdout=PIPE, stderr=PIPE)

def watch():
    ps = {}
    while True:
        for label in set(status().index) - set(ps):
            log.debug(f'Fetching "{label}"')
            ps[label] = fetch(label)
        
        for label in list(ps):
            r = ps[label].poll()
            if r is None:
                pass
            elif r == 0:
                log.debug(f'Fetched "{label}"')
                del ps[label]
            else:
                log.warn(f'Fetching "{label}" failed with retcode {r}. Stdout: "{r.stderr.read()}"')
        
        time.sleep(1)
            
def demo():
    label = launch()
    wait(label)

    setup(label)
    deploy(label)
    run(label)

if __name__ == '__main__':
    fns = [watch]
    from argparse import ArgumentParser
    parser = ArgumentParser()
    parser.add_argument('action', type=str, choices=[f.__name__ for f in fns])
    args = parser.parse_args()
    globals()[args.action]()