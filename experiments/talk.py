import aljpy
import hashlib
import json
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import aljpy.plot
from pavlov import runs
from boardlaw import analysis, arena
from grid import sql, plot, elos
from tqdm.auto import tqdm

def record_games():
    rs = runs.pandas().dropna()
    run = rs[rs.params.apply(lambda r: r['boardsize'] == 9)].index[-1]

    world = arena.common.worlds(run, 49)
    agent = arena.common.agent(run)
    analysis.record(world, [agent, agent], n_trajs=1).notebook()

def plot_elo_winrates():
    diffs = np.linspace(-1000, +1000)
    rates = 1/(1 + 10**(-(diffs/400)))
    with plt.style.context('seaborn-poster'):
        fig, ax = plt.subplots()
        ax.plot(diffs, rates)
        ax.set_ylim(0, 1)
        ax.set_xlim(-1000, +1000)
        ax.grid(True)
        ax.axhline(0.5, color='k', alpha=.5)
        ax.axvline(0, color='k', alpha=.5)
        ax.set_ylabel('win rate')
        ax.set_xlabel('difference in Elos')
        ax.set_yticks([0, .25, .5, .75, 1.])
        aljpy.plot.percent_axis(ax, axis='y')
        ax.set_title('win rate is a sigmoid in rating difference')

@aljpy.autocache()
def _trial_elos(boardsize, counter):
    trials = sql.trial_query(boardsize, 'bee/%')
    ws, gs = elos.symmetrize(trials)
    return elos.solve(ws, gs)

def trial_elos(boardsize):
    counter = sql.file_change_counter()
    return _trial_elos(boardsize, counter)

def load():
    ags = sql.agent_query()

    es = []
    for b in tqdm(ags.boardsize.unique()):
        es.append(trial_elos(b))
    es = pd.concat(es)

    ags['elo'] = es

    return ags