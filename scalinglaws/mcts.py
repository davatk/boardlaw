import numpy as np
import torch
from rebar import arrdict

def search(f, xl, xr, tol=1e-3):
    # We expect f(xl) >= 0, f(xr) <= 0, but sometimes - thanks to numerical issues
    # that turn up when you sum a bunch of reciprocals on float32 - that doesn't hold!
    # So we just call those cases 'bad' and ignore them for the duration of the search.
    # At the end, we'll fill in with whichever of the left/right is better. 
    bad = (f(xl) < -tol) | (f(xr) > +tol)
    xl, xr = xl.clone(), xr.clone()
    while True: 
        # Ugh, underflows
        xm = xl + (xr - xl)/2
        yl, ym, yr = f(xl), f(xm), f(xr)

        converged = (ym.abs() < tol)
        underflow = (xm == xl) | (xm == xr)
        if (converged | underflow | bad).all():
            fallback_l = bad & (f(xl).abs() <= f(xr).abs())
            xm[fallback_l] = xl[fallback_l]
            fallback_r = bad & (f(xl).abs() > f(xr).abs())
            xm[fallback_r] = xl[fallback_r]
            return xm
        if torch.isnan(yl + ym + yr).any():
            raise ValueError('Hit a nan')
        if (yl < -tol).any():
            raise ValueError('Left boundary has passed the root')
        if (yr > tol).any():
            raise ValueError('Right boundary has passed the root')

        in_left = (torch.sign(ym) == -1) & ~bad
        xr[in_left] = xm[in_left]

        in_right = (torch.sign(ym) == +1) & ~bad
        xl[in_right] = xm[in_right]

def regularized_policy(pi, q, lambda_n):
    alpha_min = (q + lambda_n[:, None]*pi).max(-1).values
    alpha_max = q.max(-1).values + lambda_n

    def policy(alpha):
        p = lambda_n[:, None]*pi/(alpha[:, None] - q)
        # alpha_min guarantees us we're on the right-hand side of 
        # the singularity, so let's complete it with the positive limit.
        # Practically, this makes the search a damn sight simpler.
        p[alpha[:, None] == q] = np.inf
        return p
    error = lambda alpha: policy(alpha).sum(-1) - 1

    alpha_star = search(error, alpha_min, alpha_max)

    return policy(alpha_star)

class MCTS:

    def __init__(self, world, n_nodes, c_puct=2.5): 
        self.device = world.device
        self.n_envs = world.n_envs
        self.n_nodes = n_nodes
        self.n_seats = world.n_seats
        assert n_nodes > 1, 'MCTS requires at least two nodes'

        self.envs = torch.arange(world.n_envs, device=self.device)

        self.n_actions = np.prod(world.action_space)
        self.tree = arrdict.arrdict(
            children=self.envs.new_full((world.n_envs, self.n_nodes, self.n_actions), -1),
            parents=self.envs.new_full((world.n_envs, self.n_nodes), -1),
            relation=self.envs.new_full((world.n_envs, self.n_nodes), -1))

        self.worlds = arrdict.stack([world for _ in range(self.n_nodes)], 1)
        
        self.transitions = arrdict.arrdict(
            rewards=torch.full((world.n_envs, self.n_nodes, self.n_seats), 0., device=self.device, dtype=torch.float),
            terminal=torch.full((world.n_envs, self.n_nodes), False, device=self.device, dtype=torch.bool))

        self.log_pi = torch.full((world.n_envs, self.n_nodes, self.n_actions), np.nan, device=self.device)

        self.stats = arrdict.arrdict(
            n=torch.full((world.n_envs, self.n_nodes), 0, device=self.device, dtype=torch.int),
            w=torch.full((world.n_envs, self.n_nodes, self.n_seats), 0., device=self.device))

        self.sim = 0
        self.worlds[:, 0] = world

        # https://github.com/LeelaChessZero/lc0/issues/694
        self.c_puct = c_puct

    def action_stats(self, envs, nodes):
        children = self.tree.children[envs, nodes]
        mask = (children != -1)
        stats = self.stats[envs[:, None].expand_as(children), children]
        n = stats.n.where(mask, torch.zeros_like(stats.n))
        w = stats.w.where(mask[..., None], torch.zeros_like(stats.w))

        q = w/n[..., None]

        # Q scaling + pessimistic initialization
        q[n == 0] = 0 
        q = (q - q.min())/(q.max() - q.min() + 1e-6)
        q[n == 0] = 0 

        return q, n

    def policy(self, envs, nodes):
        pi = self.log_pi[envs, nodes].exp()

        # https://arxiv.org/pdf/2007.12509.pdf
        worlds = self.worlds[envs, nodes]
        q, n = self.action_stats(envs, nodes)

        seats = worlds.seats[:, None, None].expand(-1, q.size(1), -1)
        q = q.gather(2, seats.long()).squeeze(-1)

        # N == 0 leads to nans, so let's clamp it at 1
        N = n.sum(-1).clamp(1, None)
        lambda_n = self.c_puct*N/(self.n_actions + N)

        policy = regularized_policy(pi, q, lambda_n)

        return policy

    def sample(self, env, nodes):
        probs = self.policy(env, nodes)
        return torch.distributions.Categorical(probs=probs).sample()

    def initialize(self, agent):
        decisions = agent(self.worlds[:, 0], value=True)
        self.log_pi[:, self.sim] = decisions.logits

        self.sim += 1

    def descend(self):
        current = torch.full_like(self.envs, 0)
        actions = torch.full_like(self.envs, -1)
        parents = torch.full_like(self.envs, 0)

        while True:
            interior = ~torch.isnan(self.log_pi[self.envs, current]).any(-1)
            terminal = self.transitions.terminal[self.envs, current]
            active = interior & ~terminal
            if not active.any():
                break

            actions[active] = self.sample(self.envs[active], current[active])
            parents[active] = current[active]
            current[active] = self.tree.children[self.envs[active], current[active], actions[active]]

        return parents, actions

    def backup(self, leaves, v):
        current, v = leaves.clone(), v.clone()
        while True:
            active = (current != -1)
            if not active.any():
                break

            t = self.transitions.terminal[self.envs[active], current[active]]
            v[self.envs[active][t]] = 0. 
            v[active] += self.transitions.rewards[self.envs[active], current[active]]

            self.stats.n[self.envs[active], current[active]] += 1
            self.stats.w[self.envs[active], current[active]] += v[active]
        
            current[active] = self.tree.parents[self.envs[active], current[active]]

    def simulate(self, evaluator):
        if self.sim >= self.n_nodes:
            raise ValueError('Called simulate more times than were declared in the constructor')

        parents, actions = self.descend()

        # If the transition is terminal - and so we stopped our descent early
        # we don't want to end up creating a new node. 
        leaves = self.tree.children[self.envs, parents, actions]
        leaves[leaves == -1] = self.sim

        self.tree.children[self.envs, parents, actions] = leaves
        self.tree.parents[self.envs, leaves] = parents
        self.tree.relation[self.envs, leaves] = actions

        old_world = self.worlds[self.envs, parents]
        world, transition = old_world.step(actions)

        self.worlds[self.envs, leaves] = world
        self.transitions[self.envs, leaves] = transition

        with torch.no_grad():
            decisions = evaluator(world, value=True)
        self.log_pi[self.envs, leaves] = decisions.logits

        self.backup(leaves, decisions.v)

        self.sim += 1

    def root(self):
        root = torch.zeros_like(self.envs)
        q, n = self.action_stats(self.envs, root)
        p = self.policy(self.envs, root)

        #TODO: Is this how I should be evaluating root value?
        # Not actually used in AlphaZero at all, but it's nice to have around for validation
        v = (q*p[..., None]).sum(-2)

        return arrdict.arrdict(
            logits=torch.log(p),
            v=v)

    def display(self, e=0):
        import networkx as nx
        import matplotlib.pyplot as plt

        root_seat = self.worlds[:, 0].seats[e]

        ws = self.stats.w[e, ..., root_seat]
        ns = self.stats.n[e]
        qs = (ws/ns).where(ns > 0, torch.zeros_like(ws)).cpu()
        q_min, q_max = np.nanmin(qs), np.nanmax(qs)

        nodes, edges = {}, {}
        for i in range(self.sim):
            p = int(self.tree.parents[e, i].cpu())
            if (i == 0) or (p >= 0):
                t = self.transitions.terminal[e, i].cpu().numpy()
                if i == 0:
                    color = 'C0'
                elif t:
                    color = 'C3'
                else:
                    color = 'C2'
                nodes[i] = {
                    'label': f'{i}', 
                    'color': color}
            
            if p >= 0:
                r = int(self.tree.relation[e, i].cpu())
                q, n = float(qs[i]), int(ns[i])
                edges[(p, i)] = {
                    'label': f'{r}\n{q:.2f}, {n}',
                    'color':  (q - q_min)/(q_max - q_min + 1e-6)}

        G = nx.from_edgelist(edges)

        pos = nx.kamada_kawai_layout(G)
        nx.draw(G, pos, 
            node_color=[nodes[i]['color'] for i in G.nodes()],
            edge_color=[edges[e]['color'] for e in G.edges()], width=5)
        nx.draw_networkx_edge_labels(G, pos, font_size='x-small',
            edge_labels={e: d['label'] for e, d in edges.items()})
        nx.draw_networkx_labels(G, pos, 
            labels={i: d['label'] for i, d in nodes.items()})

        return plt.gca()

def mcts(world, agent, **kwargs):
    mcts = MCTS(world, **kwargs)

    mcts.initialize(agent)
    for _ in range(mcts.n_nodes-1):
        mcts.simulate(agent)

    return mcts

class MCTSAgent:

    def __init__(self, evaluator, **kwargs):
        self.evaluator = evaluator
        self.kwargs = kwargs

    def __call__(self, world, value=True):
        m = mcts(world, self.evaluator, **self.kwargs)
        r = m.root()
        return arrdict.arrdict(
            logits=r.logits,
            v=r.v,
            actions=torch.distributions.Categorical(logits=r.logits).sample())

from . import validation, analysis

def test_trivial():
    world = validation.InstantWin.initial(device='cpu')
    agent = validation.ProxyAgent()

    m = mcts(world, agent, n_nodes=3)

    expected = torch.tensor([[+1.]], device=world.device)
    torch.testing.assert_allclose(m.root().v, expected)

def test_two_player():
    world = validation.FirstWinsSecondLoses.initial(device='cpu')
    agent = validation.ProxyAgent()

    m = mcts(world, agent, n_nodes=3)

    expected = torch.tensor([[+1., -1.]], device=world.device)
    torch.testing.assert_allclose(m.root().v, expected)

def test_depth():
    world = validation.AllOnes.initial(length=3, device='cpu')
    agent = validation.ProxyAgent()

    m = mcts(world, agent, n_nodes=15)

    expected = torch.tensor([[1/8.]], device=world.device)
    torch.testing.assert_allclose(m.root().v, expected)

def test_multienv():
    # Need to use a fairly complex env here to make sure we've not got 
    # any singleton dims hanging around internally. They can really ruin
    # a tester's day. 
    world = validation.AllOnes.initial(n_envs=2, length=3)
    agent = validation.ProxyAgent()

    m = mcts(world, agent, n_nodes=15)

    expected = torch.tensor([[1/8.], [1/8.]], device=world.device)
    torch.testing.assert_allclose(m.root().v, expected)

def full_game_mcts(s, n_nodes, n_rollouts, **kwargs):
    from . import hex
    world = hex.from_string(s, device='cpu')
    agent = validation.RandomRolloutAgent(n_rollouts)
    return mcts(world, agent, n_nodes=n_nodes, **kwargs)

def test_planted_game():
    black_wins = """
    bwb
    wbw
    ...
    """
    m = full_game_mcts(black_wins, 17, 1)
    expected = torch.tensor([[+1., -1.]], device=m.device)
    torch.testing.assert_allclose(m.root().v, expected)

    white_wins = """
    wb.
    bw.
    wbb
    """
    m = full_game_mcts(white_wins, 4, 1)
    expected = torch.tensor([[-1., +1.]], device=m.device)
    torch.testing.assert_allclose(m.root().v, expected)

    # Hard to validate the logits
    competitive = """
    wb.
    bw.
    wb.
    """
    m = full_game_mcts(competitive, 31, 4, c_puct=100.)
    expected = torch.tensor([[-1/3., +1/3.]], device=m.device)
    assert ((m.root().v - expected).abs() < 1/3).all()

def test_full_game():
    from . import hex
    world = hex.Hex.initial(1, boardsize=3, device='cpu')
    black = MCTSAgent(validation.RandomRolloutAgent(4), n_nodes=16, c_puct=.5)
    white = validation.RandomAgent()
    trace = analysis.rollout(world, [black, white], 128)
    winrates = trace.trans.rewards.sum(0).sum(0)/trace.trans.terminal.sum(0).sum(0)

def benchmark(T=16):
    import pandas as pd
    import aljpy
    import matplotlib.pyplot as plt
    from . import hex

    results = []
    for n in np.logspace(0, 14, 15, base=2, dtype=int):
        env = hex.Hex.initial(n_envs=n, boardsize=3, device='cuda')
        black = MCTSAgent(validation.RandomAgent(), n_nodes=16)
        white = validation.RandomAgent()

        torch.cuda.synchronize()
        with aljpy.timer() as timer:
            trace = analysis.rollout(env, [black, white], 16)
            torch.cuda.synchronize()
        results.append({'n_envs': n, 'runtime': timer.time(), 'samples': T*n})
        print(results[-1])
    df = pd.DataFrame(results)
        
    with plt.style.context('seaborn-poster'):
        ax = df.plot.scatter('n_envs', 'runtime', zorder=2)
        ax.set_xscale('log', base=2)
        ax.set_xlim(1, 2**14)
        ax.set_title('scaling of runtime w/ env count')
        ax.grid(True, zorder=1, alpha=.25)