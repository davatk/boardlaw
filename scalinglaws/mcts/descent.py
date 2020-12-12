import torch
import torch.distributions
from . import search, cuda
from rebar import arrdict
import aljpy

def assert_shape(x, s):
    assert x.shape == s, f'Expected {s}, got {x.shape}'
    assert x.device.type == 'cuda', f'Expected CUDA tensor, got {x.device.type}'

def descend(logits, w, n, c_puct, seats, terminal, children):
    B, T, A = logits.shape
    S = w.shape[-1]
    assert_shape(w, (B, T, S))
    assert_shape(n, (B, T))
    assert_shape(c_puct, (B,))
    assert_shape(seats, (B, T))
    assert_shape(terminal, (B, T))
    assert_shape(children, (B, T, A))
    assert (c_puct > 0.).all(), 'Zero c_puct not supported; will lead to an infinite loop in the kernel'

    with torch.cuda.device(logits.device):
        result = cuda.descend(logits, w, n.int(), c_puct, seats.int(), terminal, children.int())
    return arrdict.arrdict(
        parents=result.parents, 
        actions=result.actions)

def benchmark():
    import pickle
    with open('output/descent/hex.pkl', 'rb') as f:
        data = pickle.load(f)
        data['c_puct'] = torch.repeat_interleave(data.c_puct[:, None], data.logits.shape[1], 1)
        data = data.cuda()

    results = []
    with aljpy.timer() as timer:
        torch.cuda.synchronize()
        for t in range(data.logits.shape[0]):
            results.append(descend(**data[t]))
        torch.cuda.synchronize()
    results = arrdict.stack(results)
    time = timer.time()
    samples = results.parents.nelement()
    print(f'{1000*time:.0f}ms total, {1e9*time/samples:.0f}ns/descent')

    return results

def test():
    import pickle
    with open('output/descent/hex.pkl', 'rb') as f:
        data = pickle.load(f)
        data['c_puct'] = torch.repeat_interleave(data.c_puct[:, None], data.logits.shape[1], 1)
        data = data.cuda()

    torch.manual_seed(2)
    result = descend(**data[-1,:3])
    print(result.parents, result.actions)

def assert_distribution(xs, freqs):
    for i, freq in enumerate(freqs):
        actual = (xs == i).float().mean()
        ci = 3*(freq*(1-freq)/len(xs))**.5
        assert abs(actual - freq) <= ci, f'Expected {freq:.2f}±{ci:.2f} to be {i}, got {actual:.2f}'

def test_one_node():
    data = arrdict.arrdict(
        logits=torch.tensor([[1/3, 2/3]]).log(),
        w=torch.tensor([[0.]]),
        n=torch.tensor([0]),
        c_puct=torch.tensor(.0),
        seats=torch.tensor([0]),
        terminal=torch.tensor([False]),
        children=torch.tensor([[-1, -1]]))
    
    result = descend(**data.cuda()[None].repeat_interleave(1024, 0))
    assert_distribution(result.parents, [1])
    assert_distribution(result.actions, [1/3, 2/3])

def test_three_node():
    data = arrdict.arrdict(
        logits=torch.tensor([
            [1/3, 2/3],
            [1/4, 3/4],
            [1/5, 4/5]]).log(),
        w=torch.tensor([[0.], [0.], [0.,]]),
        n=torch.tensor([2, 1, 1]),
        c_puct=torch.tensor(1.),
        seats=torch.tensor([0, 0, 0]),
        terminal=torch.tensor([False, False, False]),
        children=torch.tensor([
            [1, 2], 
            [-1, -1], 
            [-1, -1]]))

    result = descend(**data.cuda()[None].repeat_interleave(1024, 0))

    assert_distribution(result.parents, [0, 1/3, 2/3])
    assert_distribution(result.actions, [1/3*1/4 + 2/3*1/5, 1/3*3/4 + 2/3*4/5])