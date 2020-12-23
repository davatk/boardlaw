import numpy as np
import torch
from . import heads
from torch import nn
from rebar import recurrence, arrdict
from torch.nn import functional as F

class Residual(nn.Linear):

    def __init__(self, width):
        super().__init__(width, width)

    def forward(self, x, *args, **kwargs):
        return x + F.relu(super().forward(x))

class Network(nn.Module):

    def __init__(self, obs_space, action_space, width=256):
        super().__init__()
        self.policy = heads.output(action_space, width)
        self.sampler = self.policy.sample
        self.body = recurrence.Sequential(
            heads.intake(obs_space, width),
            Residual(width),
            Residual(width),
            Residual(width),
            Residual(width))

        self.value = heads.ValueOutput(width)

    # def trace(self, world):
    #     self.policy = torch.jit.trace_module(self.policy, {'forward': (world.obs, world.valid)})
    #     self.vaue = torch.jit.trace_module(self.value, {'forward': (world.obs, world.valid, world.seats)})

    def forward(self, world, value=False):
        neck = self.body(world.obs)
        outputs = arrdict.arrdict(
            logits=self.policy(neck, valid=world.valid))

        if value:
            #TODO: Maybe the env should handle this? 
            # Or there should be an output space for values? 
            outputs['v'] = self.value(neck, valid=world.valid, seats=world.seats)
        return outputs