import numpy as np
import pandas as pd
import sqlalchemy
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, Float, String, ForeignKey, create_engine
from pavlov import runs, storage
import ast
from tqdm.auto import tqdm
from pathlib import Path
from contextlib import contextmanager

# First modern run
FIRST_RUN = pd.Timestamp('2021-02-03 12:47:26.557749+00:00')

DATABASE = Path('output/experiments/eval/database.sql')

Base = declarative_base()
class Run(Base):
    __tablename__ = 'runs'

    run = Column(String, primary_key=True)
    description = Column(String)
    boardsize = Column(Integer)
    width = Column(Integer)
    depth = Column(Integer)
    nodes = Column(Integer)

class Snap(Base):
    __tablename__ = 'snaps'

    id = Column(Integer, primary_key=True)
    run = Column(String, ForeignKey('runs.run'))
    idx = Column(Integer)
    samples = Column(Float)
    flops = Column(Float)

class Agent(Base):
    __tablename__ = 'agents'

    id = Column(Integer, primary_key=True)
    snap = Column(Integer, ForeignKey('snaps.id'))
    nodes = Column(Integer)
    c = Column(Float)

class Trial(Base):
    __tablename__ = 'trials'

    id = Column(Integer, primary_key=True)
    black_agent = Column(Integer, ForeignKey('agents.id'))
    white_agent = Column(Integer, ForeignKey('agents.id'))
    black_wins = Column(Integer)
    white_wins = Column(Integer)
    moves = Column(Integer)
    times = Column(Integer)

class MohexTrial(Base):
    __tablename__ = 'mohex_trials'

    id = Column(Integer, primary_key=True)
    black_agent = Column(Integer, ForeignKey('agents.id'), nullable=True)
    white_agent = Column(Integer, ForeignKey('agents.id'), nullable=True)
    black_wins = Column(Integer)
    white_wins = Column(Integer)
    moves = Column(Integer)
    times = Column(Integer)

_engine = None
@contextmanager
def connection():
    if not DATABASE.parent.exists():
        DATABASE.parent.mkdir(exist_ok=True, parents=True)

    global _engine
    _engine = create_engine('sqlite:///' + str(DATABASE)) if _engine is None else _engine
    with _engine.connect() as conn:
        yield conn

def create():
    with connection() as conn:
        Base.metadata.create_all(conn.engine)

        conn.execute('''
            create view agents_details as
            select 
                agents.id, agents.nodes as test_nodes, 
                snaps.id as snap_id, snaps.samples, snaps.flops as train_flops, snaps.idx, 
                runs.run, runs.description, runs.boardsize, runs.width, runs.depth, runs.nodes as train_nodes
            from agents
                inner join snaps on (agents.snap == snaps.id)
                inner join runs on (snaps.run == runs.run)''')

def run_data():
    r = runs.pandas().loc[lambda df: df._created >= FIRST_RUN]
    params = r.params.dropna().apply(pd.Series).reindex(r.index)
    insert = pd.concat([r.index.to_series().to_frame('run'), r[['description']], params[['boardsize', 'width', 'depth', 'nodes']]], 1)
    insert['nodes'] = insert.nodes.fillna(64)
    return insert.reset_index(drop=True)

def snapshot_data(new_runs):
    snapshots = {}
    for _, r in tqdm(list(new_runs.iterrows()), desc='snapshots'):
        for i, s in storage.snapshots(r.run).items():
            stored = storage.load_snapshot(r.run, i)
            if 'n_samples' in stored:
                snapshots[r.run, i] = {
                    'samples': stored['n_samples'], 
                    'flops': stored['n_flops']}
    snapshots = (pd.DataFrame.from_dict(snapshots, orient='index')
                    .rename_axis(index=('run', 'idx'))
                    .reset_index())
    # snapshots['id'] = snapshots.index.to_series()
    return snapshots

def refresh_runs():
    current = run_data()
    with connection() as conn:
        old = pd.read_sql_query('select * from runs', conn)
        new = current[~current.run.isin(old.run)]
        new.to_sql('runs', conn, index=False, if_exists='append')

        snaps = snapshot_data(new)
        snaps.to_sql('snaps', conn, index=False, if_exists='append')

    return new

def create_agents(rs, test_nodes=64, c=1/16, dry_run=False):
    with connection() as conn:
        snaps = pd.read_sql_query('select * from snaps', conn, index_col='id')

        agents = (snaps.index
            [snaps.run.isin(rs.run)]
            .to_frame(name='snap')
            .reset_index(drop=True))
        agents['nodes'] = test_nodes
        agents['c'] = c

        old_agents = pd.read_sql_query('select * from agents', conn)
        old_agents = pd.merge(
            agents.rename_axis(index='idx').reset_index(), 
            old_agents, 
            on=['snap', 'nodes', 'c'], how='inner')
        new_agents = agents[~agents.index.isin(old_agents.idx)]

        if dry_run:
            print(new_agents)
        else:
            new_agents.to_sql('agents', conn, index=False, if_exists='append')

def execute(sql):
    with connection() as conn:
        return conn.execute(sql)

def query(sql, **kwargs):
    with connection() as conn:
        return pd.read_sql_query(sql, conn, **kwargs)

def agent_query():
    return query('''select * from agents_details''', index_col='id')

def trial_query(boardsize=None, desc='%', test_nodes=None):
    q = '''
        select trials.* 
        from trials 
            inner join agents_details as black
                on (trials.black_agent == black.id)
            inner join agents_details as white
                on (trials.white_agent == white.id)
        where 
            (black.description like ?) and (white.description like ?)
            '''
    params = (desc, desc)
    
    if test_nodes is not None:
        q += '\nand (black.test_nodes == ?) and (white.test_nodes == ?)'
        params += (test_nodes, test_nodes)
    if boardsize is not None:
        q += '\nand (black.boardsize == ?) and (white.boardsize == ?)'
        params += (int(boardsize), int(boardsize)) 
    
    return query(q, index_col='id', params=params)

def save_trials(results):
    rows = []
    for r in results:
        rows.append({
            'black_agent': r.names[0],
            'white_agent': r.names[1],
            'black_wins': r.wins[0],
            'white_wins': r.wins[1],
            'moves': r.moves,
            'times': r.times})
    rows = pd.DataFrame(rows)
    with connection() as conn:
        rows.to_sql('trials', conn, index=False, if_exists='append')

def save_mohex_trials(results):
    rows = []
    for r in results:
        assert sum(n is None for n in r.names) == 1, 'One agent should be MoHex'
        rows.append({
            'black_agent': r.names[0],
            'white_agent': r.names[1],
            'black_wins': r.wins[0],
            'white_wins': r.wins[1],
            'moves': r.moves,
            'times': r.times})
    rows = pd.DataFrame(rows)
    with connection() as conn:
        rows.to_sql('mohex_trials', conn, index=False, if_exists='append')

def mohex_trial_query(boardsize, desc='%'):
    return query('''
        select mohex_trials.* 
        from mohex_trials 
            left join agents_details as black
                on (mohex_trials.black_agent == black.id)
            left join agents_details as white
                on (mohex_trials.white_agent == white.id)
        where 
            ((black.boardsize == ?) or (white.boardsize == ?)) and 
            ((black.description like ?) or (white.description like ?))''', index_col='id', params=(int(boardsize), int(boardsize), desc, desc))

def file_change_counter():
    # https://www.sqlite.org/fileformat.html
    counter = DATABASE.open('rb').read(30)[24:28]
    dt = np.dtype('int32').newbyteorder('>')
    return int(np.frombuffer(counter, dtype=dt))