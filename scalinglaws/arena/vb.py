from torch.distributions.transforms import SigmoidTransform
from rebar import arrdict, dotdict
import numpy as np
import sympy as sym
import scipy as sp
import scipy.stats
import matplotlib.pyplot as plt
import torch
from torch import nn
from torch.autograd import Function
from tqdm.auto import tqdm

μ0 = 0
σ0 = 2

μ_lims = [-5*σ0, +5*σ0]
σ2_lims = [-4, +2]

def test_d_integral():
    Σ = np.array([[.6, .5], [.5, 1]])
    μ = np.array([0, 1])

    def ϕ(d):
        return 1/(1 + np.exp(-d))

    N = sp.stats.multivariate_normal(μ, Σ)
    s = N.rvs(10000)
    actual = np.log(ϕ(s[..., 0] - s[..., 1])).mean()

    R = np.array([[+1, -1]])
    σ2d = R @ Σ @ R.T
    μd = R @ μ

    Nd = sp.stats.norm(μd, σ2d**.5)
    d = Nd.rvs(10000)
    expected = np.log(ϕ(d)).mean()

    return expected, actual

class Differ(nn.Module):

    def __init__(self, N):
        super().__init__()

        self.N = N
        j, k = torch.as_tensor(np.indices((N, N)).reshape(2, -1))
        self.j, self.k = j[j != k], k[j != k]

    def forward(self, μ, Σ):
        j, k = self.j, self.k
        μd = μ[j] - μ[k]
        σ2d = Σ[j, j] - Σ[j, k] - Σ[k, j] + Σ[k, k]
        return μd, σ2d

    def as_square(self, x, fill=0.):
        y = torch.full((self.N, self.N), fill)
        y[self.j, self.k] = x
        return y

def evaluate(interp, μd, σd):
    return torch.as_tensor(interp(μd.detach().numpy(), σd.detach().numpy(), grid=False)).float()

class GaussianExpectation(Function):

    @staticmethod
    def auxinfo(f, K=101, S=1000):
        μ = np.linspace(*μ_lims, K)
        σ2 = np.logspace(*σ2_lims, K, base=10)

        #TODO: Importance sample these zs
        zs = np.linspace(-5, +5, S)
        pdf = sp.stats.norm.pdf(zs)[None, None, :]
        ds = (μ[:, None, None] + zs[None, None, :]*σ2[None, :, None]**.5)
        fs = (f(ds)*pdf/pdf.sum()).sum(-1)
        f = sp.interpolate.RectBivariateSpline(μ, σ2, fs, kx=1, ky=1)

        dμs = (fs[2:, :] - fs[:-2, :])/(μ[2:] - μ[:-2])[:, None]
        dμ = sp.interpolate.RectBivariateSpline(μ[1:-1], σ2, dμs, kx=1, ky=1)
        dσ2s = (fs[:, 2:] - fs[:, :-2])/(σ2[2:] - σ2[:-2])[None, :]
        dσ2 = sp.interpolate.RectBivariateSpline(μ, σ2[1:-1], dσ2s, kx=1, ky=1)

        return dotdict.dotdict(
            μ=μ, σ2=σ2, 
            f=f, dμ=dμ, dσ2=dσ2, 
            fs=fs, dμs=dμs, dσs=dσ2s)

    @staticmethod
    def forward(ctx, μd, σ2d, aux):
        ctx.save_for_backward(μd, σ2d)
        ctx.aux = aux
        return evaluate(aux.f, μd, σ2d)

    @staticmethod
    def backward(ctx, dldf):
        μd, σ2d = ctx.saved_tensors
        dfdμ = evaluate(ctx.aux.dμ, μd, σ2d)
        dfdσ2d = evaluate(ctx.aux.dσ2, μd, σ2d)

        dldμ = dldf*dfdμ
        dldσ2d = dldf*dfdσ2d

        return dldμ, dldσ2d, None

    @staticmethod
    def plot(aux):
        Y, X = np.meshgrid(aux.μ, aux.σ2)
        (t, b), (l, r) = μ_lims, σ2_lims
        plt.imshow(np.exp(aux.fs), extent=(l, r, b, t), vmin=0, vmax=1, cmap='RdBu', aspect='auto')
        plt.colorbar()


def expected_log_likelihood(n, w, μ, Σ):
    self = expected_log_likelihood
    N = n.shape[0]
    if not hasattr(self, '_differ') or self._differ.N != N:
        self._differ = Differ(n.shape[0])
        self._aux = GaussianExpectation.auxinfo(lambda d: -np.log(1 + np.exp(-d)))
    differ, aux = self._differ, self._aux

    μd, σ2d = differ(μ, Σ)
    wins = w*differ.as_square(GaussianExpectation.apply(μd, σ2d, aux), .5)
    losses = (n - w)*differ.as_square(GaussianExpectation.apply(-μd, σ2d, aux), .5)
    return wins + losses

def cross_entropy(n, w, μ, Σ):

    # Proof:
    # from sympy.stats import E, Normal
    # s, μ, μ0, σ, σ0 = symbols('s μ μ_0 σ σ_0')
    # s = Normal('s', μ, σ)
    # 1/(2*σ0)*E(-(s - μ0)**2)
    expected_prior = -1/(2*σ0)*((μ - μ0)**2 + Σ**2)

    return -expected_log_likelihood(n, w, μ, Σ).sum() - expected_prior.sum()

def entropy(Σ):
    return 1/2*torch.logdet(2*np.pi*np.e*Σ)

def elbo(n, w, μ, Σ):
    return -cross_entropy(n, w, μ, Σ) + entropy(Σ)

@torch.no_grad()
def project(Σ):
    symmetric = (Σ + Σ.T)/2
    λ, v = torch.symeig(symmetric, True)
    return v @ torch.diag(λ.clamp(1e-6, None)) @ v.T

def solve(n, w, tol=1e-5, T=5000):
    N = n.shape[0]

    μ = torch.nn.Parameter(torch.zeros((N,)))
    Σ = torch.nn.Parameter(torch.eye(N))

    optim = torch.optim.Adam([μ, Σ], 1e-3)

    ls, norms = [], []
    with tqdm(total=T) as pbar:
        for i in range(T):
            Σ.data = project(Σ)
            μ.data[0] = 0
            l = -elbo(n, w, μ, Σ)
            optim.zero_grad()
            l.backward()
            optim.step()

            norm = torch.cat([μ.grad.flatten(), Σ.grad.flatten()]).abs().max()
            ls.append(l.detach())
            norms.append(norm.detach())
            if len(ls) > 10 and max(ls[-10:]) - min(ls[-10:]) < tol*ls[-10]:
                break

            pbar.update(1)
            pbar.set_description(f'{l:5G}, {norm:5G}')
        else:
            print('Didn\'t converge')

    differ = Differ(N)
    μd, σ2d = map(differ.as_square, differ(μ, Σ))
    
    return arrdict.arrdict(
        μ=μ, 
        Σ=Σ, 
        μd=μd,
        σ2d=σ2d,
        l=torch.as_tensor(ls),
        norms=torch.as_tensor(norms)).detach().numpy()

def plot(soln):
    fig, axes = plt.subplots(1, 3)
    fig.set_size_inches(15, 5)

    ax = axes[0]
    ax.plot(soln.l)
    ax.set_xlim(0, len(soln.l))
    ax.set_title('loss')

    ax = axes[1]
    ax.plot(soln.μ)
    ax.set_xlim(0, len(soln.μ))
    ax.set_title('μ')

    ax = axes[2]
    ax.imshow(soln.σ2d**.5)
    ax.set_title('σd')

def test():
    N = 5

    s = np.random.randn(N)

    n = np.random.randint(1, 10, (N, N))

    d = s[:, None] - s[None, :]
    w = sp.stats.binom(n, 1/(1 + np.exp(-d))).rvs()

    soln = solve(n, w)


    plt.plot(soln.μ)
    plt.imshow(soln.σd)

    from scalinglaws.arena import database

    run_name = '2020-11-27 21-32-59 az-test'
    winrate = database.symmetric_winrate(run_name).fillna(0).values
    n = database.symmetric_games(run_name).values
    w = (winrate*n).astype(int)

    n, w = map(torch.as_tensor, (n, w))

    soln = solve(n, w)

    plot(soln)
