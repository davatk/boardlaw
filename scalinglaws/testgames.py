"""
What to test?
    * Does it all run? Only need one action, one player, one timestep for this. 
    * Does it correctly pick the winning move? One player, two actions, instant returns. 
    * Does it correctly pick the winning move deep in the tree? One player, two actions, a bunch of ignored actions,
      and then a payoff depending on those first two
    * Does it correctly estimate state values for different players? Need two players with different returns.
    * 
"""
import torch
from rebar import arrdict
from . import heads
from collections import namedtuple

class ProxyAgent:

    def __init__(self, env):
        self.n_seats = env.n_seats 

    def __call__(self, inputs, value=False):
        decisions = arrdict.arrdict(
            logits=inputs.logits)
        if value:
            decisions['v'] = inputs.v
        return decisions

class RandomAgent:

    def __init__(self, env):
        self.n_seats = env.n_seats

    def __call__(self, inputs):
        B, _ = inputs.valid.shape
        return arrdict.arrdict(
            logits=torch.log(inputs.valid.float()/inputs.valid.sum(-1, keepdims=True)),
            actions=torch.distributions.Categorical(probs=inputs.valid.float()).sample(),
            v=torch.zeros((B, self.n_seats), device=inputs.valid.device))

class RandomRolloutAgent:

    def __init__(self, env, n_rollouts, temperature=1):
        self.env = env
        self.n_rollouts = n_rollouts
        self.temperature = temperature
        self.envs = torch.arange(env.n_envs, device=env.device)

    def rollout(self, inputs):
        B, _ = inputs.valid.shape
        env = self.env
        original = env.state_dict()

        live = torch.ones((B,), dtype=torch.bool, device=env.device)
        reward = torch.zeros((B, env.n_seats), dtype=torch.float, device=env.device)
        while True:
            if not live.any():
                break

            actions = torch.distributions.Categorical(probs=inputs.valid.float()).sample()

            responses, inputs = env.step(actions)

            reward += responses.rewards * live.unsqueeze(-1).float()
            live = live & ~responses.terminal

        env.load_state_dict(original)

        return reward

    def __call__(self, inputs, value=True):
        v = torch.stack([self.rollout(inputs) for _ in range(self.n_rollouts)]).mean(0)

        v_seat = v[self.envs.long(), :, inputs.seats.long()]
        logits = self.temperature*v_seat*inputs.valid.float()
        logits = logits - torch.logsumexp(logits, -1)
        return arrdict.arrdict(
            logits=logits,
            actions=torch.distributions.Categorical(logits=logits).sample(),
            v=v)

def combine(xs, seats):
    envs = torch.arange(seats.size(0), device=seats.device)
    return arrdict.stack(xs)[seats.long(), envs.long()]

def rollout(env, agents, n_steps):
    inputs = env.reset()
    trace = []
    for _ in range(n_steps):
        decisions = combine([agent(inputs) for agent in agents], inputs.seats)
        responses, new_inputs = env.step(decisions.actions)
        trace.append(arrdict.arrdict(
            inputs=inputs,
            decisions=decisions,
            responses=responses))
        inputs = new_inputs
    return arrdict.stack(trace)


def uniform_logits(valid):
    return torch.log(valid.float()/valid.sum(-1, keepdims=True))
    
class InstantWin:

    def __init__(self, n_envs=1, device='cuda'):
        self.device = torch.device(device)
        self.n_envs = n_envs
        self.n_seats = 1

        self.obs_space = (0,)
        self.action_space = (1,)

    def _observe(self):
        valid = torch.ones((self.n_envs, 1), dtype=torch.bool, device=self.device),
        return arrdict.arrdict(
            valid=valid,
            seats=torch.zeros((self.n_envs,), dtype=torch.long, device=self.device),
            logits=uniform_logits(valid),
            v=torch.ones((self.n_envs, self.n_seats), dtype=torch.float, device=self.device))

    def reset(self):
        return self._observe()

    def step(self, actions):
        responses = arrdict.arrdict(
            terminal=torch.ones((self.n_envs,), dtype=torch.bool, device=self.device),
            rewards=torch.ones((self.n_envs, self.n_seats), dtype=torch.float, device=self.device))
        
        return responses, self._observe()

    def state_dict(self):
        return arrdict.arrdict()

    def load_state_dict(self, sd):
        pass

class FirstWinsSecondLoses:

    def __init__(self, n_envs=1, device='cuda'):
        self.device = torch.device(device)
        self.n_envs = n_envs
        self.n_seats = 2

        self.obs_space = (0,)
        self.action_space = (1,)

        self._seats = torch.zeros((self.n_envs,), dtype=torch.long, device=self.device)

    def _observe(self):
        valid = torch.ones((self.n_envs, 1), dtype=torch.bool, device=self.device)
        return arrdict.arrdict(
            valid=valid,
            seats=self._seats,
            logits=uniform_logits(valid),
            v=torch.tensor([[+1., -1.]], device=self.device).expand(self.n_envs, 2)).clone()

    def reset(self):
        return self._observe()

    def step(self, actions):
        terminal = (self._seats == 1)
        responses = arrdict.arrdict(
            terminal=terminal,
            rewards=torch.stack([terminal.float(), -terminal.float()], -1))

        self._seats += 1
        self._seats[responses.terminal] = 0

        inputs = self._observe()
        
        return responses, inputs

    def state_dict(self):
        return arrdict.arrdict(
            seat=self._seats).clone()

    def load_state_dict(self, sd):
        self._seats[:] = sd.seat

class AllOnes:

    def __init__(self, n_envs=1, length=4, device='cpu'):
        self.device = device 
        self.n_envs = n_envs
        self.n_seats = 1
        self.length = length

        self.obs_space = (0,)
        self.action_space = (2,)
        self.history = torch.full((n_envs, length), -1, dtype=torch.long, device=self.device)
        self.idx = torch.full((n_envs,), 0, dtype=torch.long, device=self.device)

    def _observe(self):
        correct_so_far = (self.history == 1).sum(-1) == self.idx+1
        correct_to_go = 2**((self.history == 1).sum(-1) - self.length).float()
        v = correct_so_far.float()*correct_to_go

        valid = torch.ones((self.n_envs, 2), dtype=torch.bool, device=self.device)
        return arrdict.arrdict(
            valid=valid,
            seats=torch.zeros((self.n_envs,), dtype=torch.int, device=self.device),
            logits=uniform_logits(valid),
            v=v[..., None]).clone()

    def reset(self):
        return self._observe()

    def step(self, actions):
        self.history[:, self.idx] = actions

        inputs = self._observe()

        self.idx += 1 

        response = arrdict.arrdict(
            terminal=(self.idx == self.length),
            rewards=((self.idx == self.length) & (self.history == 1).all(-1))[:, None].float())

        self.idx[response.terminal] = 0
        self.history[response.terminal] = -1
        
        return response, inputs

    def state_dict(self):
        return arrdict.arrdict(
            history=self.history,
            idx=self.idx).clone()

    def load_state_dict(self, d):
        self.history = d.history
        self.idx = d.idx
