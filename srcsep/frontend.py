""" Frontend functions for analysis and generation. """
import os
from pathlib import Path
from itertools import product
from time import time

import numpy as np
from numpy.random import default_rng
import scipy
import pandas as pd
import torch
import torch.nn as nn
import matplotlib.pyplot as plt

from srcsep.utils import to_numpy, df_product, df_product_channel_single, df_product_channel_double
from srcsep.data_source import ProcessDataLoader, FBmLoader, PoissonLoader, MRWLoader, SMRWLoader
from srcsep.layers.scale_indexer import ScaleIndexer
from srcsep.layers.described_tensor import Description, DescribedTensor
from srcsep.layers.layers_basics import ChunkedModule, ChunkedModuleDeglitching, NormalizationLayer
from srcsep.layers.layers_time import Wavelet
from srcsep.layers.moment_layers import Order1Moments, ScatCoefficients, Cov, CovScaleInvariant
from srcsep.layers.loss import MSELossScat, DeglitchingLoss
from srcsep.layers.solver import Solver, CheckConvCriterion, SmallEnoughException
""" Notations

Dimension sizes:
- B: batch size
- R: number of realizations (used to estimate scattering covariance)
- N: number of data channels (N>1 : multivariate process)
- T: number of data samples (time samples)
- J: number of scales (octaves)
- Q: number of wavelets per octave
- r: number of conv layers in a scattering model

Tensor shapes:
- x: input, of shape  (B, N, T)
- Rx: output (DescribedTensor),
    - y: tensor of shape (B, K, T) with K the number of coefficients in the representation
    - descri: pandas DataFrame of shape K x nb_attributes used, description of the output tensor y
"""

##################
# DATA LOADING
##################


def load_data(process_name, R, T, cache_dir=None, **data_param):
    """ Time series data loading function.

    :param process_name: fbm, poisson, mrw, smrw, hawkes, turbulence or snp
    :param R: number of realizations
    :param T: number of time samples
    :param cache_dir: the directory used to cache trajectories
    :param data_param: the model
    :return: dataloader
    """
    loader = {
        'fbm': FBmLoader,
        'poisson': PoissonLoader,
        'mrw': MRWLoader,
        'smrw': SMRWLoader,
    }

    if process_name == 'snp':
        raise ValueError("S&P data is private, please provide your own data.")
    if process_name == 'heliumjet':
        raise ValueError(
            "Helium jet data is private, please provide your own data.")
    if process_name == 'hawkes':
        raise ValueError("Hawkes data is not yet supported.")
    if process_name not in loader.keys():
        raise ValueError("Unrecognized model name.")

    if cache_dir is None:
        cache_dir = Path(__file__).parents[0] / '_cached_dir'

    dtld = loader[process_name](cache_dir)
    x = dtld.load(R=R, n_files=R, T=T, **data_param).x

    return x


def format_np(x):
    """ Unsqueeze x so its shape becomes (B, N, T). """
    if x.ndim == 1:
        return x[None, None, :]
    if x.ndim == 1:
        return x[:, None, :]
    if x.ndim == 3:
        return x
    raise ValueError("Array cannot be formatted to (B,N,T) shape.")


def format_tensor(x):
    """ From numpy array go to tensor of shape (B, N, J, T)  """
    return torch.from_numpy(format_np(x)).unsqueeze(-2).unsqueeze(-2)


##################
# ANALYSIS
##################


class Model(nn.Module):
    """ Model class for analysis and generation. """

    def __init__(self, model_type, qs, c_types, T, r, J, Q, wav_type,
                 high_freq, wav_norm, N, Ns, channel_mode, sigma2,
                 norm_on_the_fly, no_mean, estim_operator, c_types_used,
                 cov_chunk, dtype):
        super(Model, self).__init__()
        self.model_type = model_type
        self.sc_idxer = ScaleIndexer(r=r, J=J, Q=Q)
        self.r = r
        # time layers
        self.Ws = nn.ModuleList([
            Wavelet(T, J[o], Q[o], wav_type[o], wav_norm[o], high_freq[o],
                    o + 1, self.sc_idxer) for o in range(r)
        ])
        # normalization layer
        if norm_on_the_fly:
            self.norm_layer_scale = NormalizationLayer(2, None, True)
        elif model_type == 'covreduced' or sigma2 is not None:
            self.norm_layer_scale = NormalizationLayer(2, sigma2.pow(0.5),
                                                       False)
        else:
            self.norm_layer_scale = nn.Identity()

        # channel transforms
        self.N = N
        self.Ns = Ns[:-1]
        self.channel_mode = channel_mode
        self.no_mean = no_mean

        # marginal moments
        self.module_scat = ScatCoefficients(qs or [1.0, 2.0], estim_operator)
        self.module_scat_q1 = ScatCoefficients([1.0], estim_operator)
        self.module_q1 = Order1Moments(estim_operator)

        if r == 2:
            # correlation moments
            self.module_cov_w = Cov(1, 1, self.sc_idxer, 1, estim_operator)
            self.module_cov_wmw = Cov(1, 2, self.sc_idxer, 1, estim_operator)
            self.module_cov_mw = Cov(2, 2, self.sc_idxer, cov_chunk,
                                     estim_operator)
            self.df_cov = Description(
                self.build_description_correlation([1, 1],
                                                   self.sc_idxer,
                                                   channel_mode='full'))
            self.module_covinv = CovScaleInvariant(
                self.sc_idxer,
                self.df_cov) if model_type == "covreduced" else None

        self.description = self.build_description()
        self.c_types = None if "c_type" not in self.description.columns else self.description.c_type.unique(
        ).tolist()
        self.c_types_used = c_types_used or c_types

        if dtype == torch.float64:
            self.double()

    def double(self):
        """ Change model parameters and buffers to double precision (float64 and complex128). """

        def cast(t):
            #TODO: the cast function hangs for large tensors on gpu for some reason, bypassing for now.
            return t
            if t.is_floating_point():
                return t.to(torch.float64)
            if t.is_complex():
                return t.to(torch.complex128)
            return t

        return self._apply(cast)

    def build_descri_scattering_network(self, Ns):
        """ Assemble the description of output of Sx = (Wx, W|Wx|, ..., W|...|Wx||). """
        r_max = self.sc_idxer.r

        df_l = []
        for (r, N, sc_idces, sc_paths) in \
                zip(range(1, r_max + 1), Ns, self.sc_idxer.sc_idces, self.sc_idxer.sc_paths):
            # assemble description at a given scattering layer r
            ns = pd.DataFrame(np.arange(N), columns=['n'])
            scs = pd.DataFrame([sc for sc in sc_idces], columns=['sc'])
            js = pd.DataFrame(np.array([
                self.sc_idxer.idx_to_path(sc, squeeze=False)
                for sc in scs.sc.values
            ]),
                              columns=[f'j{r}' for r in range(1, r_max + 1)])
            scs_js = pd.concat([scs, js], axis=1)
            a_s = pd.DataFrame(np.arange(1), columns=['a'])
            df_l.append(df_product(ns, scs_js, a_s))
        df = pd.concat(df_l)
        df['low'] = [self.sc_idxer.is_low_pass(sc) for sc in df['sc'].values]
        df['r'] = [self.sc_idxer.order(sc) for sc in df['sc'].values]
        df = df.reindex(columns=[
            'r', 'n', 'sc', *[f'j{r}' for r in range(1, r_max + 1)], 'a', 'low'
        ])

        return df

    @staticmethod
    def make_description_compatible(df):
        """ Convert marginal description to correlation description. """
        df = df.rename(columns={
            'r': 'rl',
            'n': 'nl',
            'sc': 'scl',
            'j1': 'jl1',
            'a': 'al'
        })
        df['real'] = True
        df['nr'] = df['nl']
        df['rr'] = df['scr'] = df['ar'] = df['jr1'] = pd.NA
        df['c_type'] = ['mean' if low else 'spars' for low in df.low.values]
        df = df.reindex(columns=[
            'c_type', 'nl', 'nr', 'q', 'rl', 'rr', 'scl', 'scr', 'jl1', 'jr1',
            'j2', 'al', 'ar', 'real', 'low'
        ])

        return df

    def build_description_q1_moments(self, N):
        """ Assemble the description of averages E{Wx} and E{|Wx|}. """
        df = self.build_descri_scattering_network([N])
        df = df.query("r==1")

        # compatibility with covariance description
        df = Model.make_description_compatible(df)
        df['q'] = 1

        return df

    @staticmethod
    def build_description_correlation(Ns, sc_idxer, channel_mode):
        """ Assemble the description the phase modulus correlation E{Sx, Sx}. """
        scs_r1, scs_r2 = sc_idxer.sc_idces[:2]

        df_ww = Cov.create_scale_description(scs_r1, scs_r1, sc_idxer)
        df_wmw = Cov.create_scale_description(scs_r1, scs_r2, sc_idxer)
        df_mw = Cov.create_scale_description(scs_r2, scs_r2, sc_idxer)

        def channel_expand(df, N1, N2):
            if channel_mode == 'diag':
                return df_product_channel_single(df, N1, "same")
            elif channel_mode == 'full':
                return df_product_channel_double(df, N1, N2, False)
            elif channel_mode == 'offdiag':
                return df_product_channel_double(df, N1, N2, True)

        df_cov = pd.concat([
            channel_expand(df_ww, Ns[0], Ns[0]),
            channel_expand(df_wmw, Ns[0], Ns[1]),
            channel_expand(df_mw, Ns[1], Ns[1])
        ])

        return df_cov

    def build_description(self):
        """ Assemble the description of output of forward. """

        if self.model_type is None:

            df = self.build_descri_scattering_network(self.Ns)

        elif self.model_type == 'scat':

            df = self.build_descri_scattering_network(self.Ns)
            df['c_type'] = 'scat'
            df['real'] = True
            qs = pd.DataFrame(self.module_scat.qs.detach().cpu().numpy(),
                              columns=['q'])
            df = df_product(df, qs)

        elif self.model_type == 'cov':

            df_r1 = self.build_description_q1_moments(self.N)
            df_r2 = self.build_description_correlation(
                [self.N, self.N],
                self.sc_idxer,
                channel_mode=self.channel_mode)

            df = pd.concat([df_r1, df_r2])

        elif self.model_type == 'covreduced':

            df_r1 = self.build_description_q1_moments(self.N)
            df_r2 = self.build_description_correlation(
                [self.N, self.N],
                self.sc_idxer,
                channel_mode=self.channel_mode)

            # ps and low pass of phaseenv and envelope
            df_cov_non_invariant = df_r2[df_r2['low'] |
                                         (df_r2['c_type'] == "ps")]
            df_non_invariant = pd.concat([df_r1, df_cov_non_invariant])

            # phaseenv and envelope that are invariant
            df_inv = CovScaleInvariant.create_scale_description(self.sc_idxer)
            df_inv = df_product_channel_single(df_inv, self.N, method="same")

            # make non-invariant / invariant descriptions compatible
            df_inv['scr'] = df_inv['scl'] = df_inv['jl1'] = df_inv[
                'jr1'] = df_inv['j2'] = pd.NA
            df_non_invariant['a'] = df_non_invariant['b'] = pd.NA

            df = pd.concat([df_non_invariant, df_inv])

        elif self.model_type == 'scat+cov':

            df_exp = self.build_description_q1_moments(self.N)

            df_scat = self.build_descri_scattering_network(self.Ns)
            df_scat = df_scat[df_scat['r'] == 2]
            df_scat = self.make_description_compatible(df_scat)
            df_scat['q'] = 1
            df_scat['c_type'] = 'scat'

            df_cov = self.build_description_correlation(
                self.Ns, self.sc_idxer, channel_mode=self.channel_mode)

            df = pd.concat([df_exp, df_scat, df_cov])

        else:

            raise ValueError("Unrecognized model type.")

        return Description(df)

    def compute_scattering(self, x):
        """ Compute the Wx, W|Wx|, ..., W|...|Wx||. """
        Sx_l = []
        for order, W in enumerate(self.Ws):
            x = W(x)
            if order == 0:
                x = self.norm_layer_scale(x)
            Sx_l.append(x)
            x = torch.abs(x)
            if self.no_mean:
                x = x - x.mean(-1, keepdim=True)

        return Sx_l

    def compute_spars(self, Wx, reshape=True):
        """ Compute E{Wx} and E{|Wx|}. """
        exp = self.module_q1(Wx)
        if reshape:
            return exp.view(exp.shape[0], -1, exp.shape[-1])
        return exp

    def compute_phase_mod_correlation(self,
                                      Wx,
                                      WmWx,
                                      channel_mode,
                                      reshape=True):
        """ Compute phase-modulus correlation matrix E{rho Wx (rho Wx)^ *}. """
        cov1 = self.module_cov_w(Wx, Wx, channel_mode=channel_mode)
        cov2 = self.module_cov_wmw(Wx, WmWx, channel_mode=channel_mode)
        cov3 = self.module_cov_mw(WmWx, WmWx, channel_mode=channel_mode)

        def reshaper(y):
            if reshape:
                return y.view(y.shape[0], -1, y.shape[-1])
            return y

        return torch.cat([reshaper(cov) for cov in [cov1, cov2, cov3]], dim=-2)

    def count_coefficients(self, **kwargs) -> int:
        """ Returns the number of moments satisfying kwargs. """
        descri = self.description
        if self.c_types_used is not None:
            descri = descri.reduce(c_type=self.c_types_used)
        return descri.where(**kwargs).sum()

    def forward(self, x):

        # scattering layer
        Sx = self.compute_scattering(x)

        if self.model_type is None:

            y = torch.cat(
                [out.view(x.shape[0], -1, x.shape[-1]) for out in Sx], dim=1)

        elif self.model_type == 'scat':

            Sx = torch.cat(
                [out.view(x.shape[0], -1, x.shape[-1]) for out in Sx], dim=1)
            y = self.module_scat(Sx)
            y = y.view(y.shape[0], -1, y.shape[-1])

        elif self.model_type == 'cov':

            exp = self.compute_spars(Sx[0])
            cov = self.compute_phase_mod_correlation(
                *Sx, channel_mode=self.channel_mode)
            y = torch.cat([exp, cov], dim=1)

        elif self.model_type == 'covreduced':

            exp = self.compute_spars(Sx[0])

            noninv_mask = self.df_cov.where(c_type="ps") | self.df_cov.where(
                low=True)
            cov_full = self.compute_phase_mod_correlation(
                *Sx, channel_mode=self.channel_mode, reshape=False)
            cov_noninv = cov_full[..., noninv_mask, :]
            cov_inv = self.module_covinv(cov_full)  # invariant to scaling

            cov = torch.cat([
                c.view(c.shape[0], -1, c.shape[-1])
                for c in [cov_noninv, cov_inv]
            ],
                            dim=-2)

            y = torch.cat([exp, cov], dim=-2)

        elif self.model_type == 'scat+cov':
            Wx, WmWx = Sx[:2]

            exp1 = self.compute_spars(Wx)
            exp2 = self.module_scat_q1(WmWx)
            exp = torch.cat(
                [exp1, exp2.view(exp2.shape[0], -1, exp2.shape[-1])], dim=1)

            cov = self.compute_phase_mod_correlation(
                Wx, WmWx, channel_mode=self.channel_mode)

            y = torch.cat([exp, cov], 1)

        else:

            raise ValueError("Unrecognized model type.")

        if not y.is_complex():
            y = torch.complex(y, torch.zeros_like(y))

        Rx = DescribedTensor(x=None, y=y, descri=self.description)

        if self.c_types_used is not None:
            return Rx.reduce(c_type=self.c_types_used)

        return Rx


class ModelDeglitching(nn.Module):
    """ Should inherit from a cross-scatcov model. That computes interactions between scales of 2 signals. """

    def __init__(self, x_init, nks, cuda, **kwargs):
        super(ModelDeglitching, self).__init__()
        self.x_init = x_init  # signal to deglitch
        self.nks = nks  # noises realizations

        # init models
        self.phi = Model(**kwargs)
        self.cross_phi = Model(
            N=2,
            Ns=[2, 2],
            no_mean=True,
            channel_mode='offdiag',
            **{
                key: value
                for (key, value) in kwargs.items()
                if key not in ['N', 'Ns', 'no_mean', 'channel_mode']
            })

        if cuda:
            self.cuda()
            self.x_init = self.x_init.cuda()
            self.nks = self.nks.cuda()

        # init fixed representations
        phi_chunked = ChunkedModule(
            self.phi, nchunks=self.nks.shape[0]
        )  # chunked version only used for initialization
        self.phi_x, self.phi_nks = self.phi(self.x_init), phi_chunked(self.nks)

        # init weights used in the loss
        cross_phi_chunked = ChunkedModule(
            self.cross_phi,
            nchunks=self.nks.shape[0])  # only used for initialization
        self.std_nks, self.std_x_nks, self.std_cross = self.init_weight(
            phi_chunked, cross_phi_chunked)

        self.description = self.init_description()
        self.c_types = self.phi.c_types

    def init_weight(self, phi_chunked, cross_phi_chunked):
        """ Compute the stds along noise realizations. """
        # std(phi(nks))
        std_nks = self.phi_nks.y.std(0)[:, 0]

        # std(phi(x-nt+nk)) approximated by std(phi(x+nk))
        phi_x_nks = phi_chunked(self.x_init + self.nks)
        std_x_nks = phi_x_nks.y.std(0)[:, 0]

        # std(phi(x-nt, nk)) approximated by std(phi(x,nk))
        x_repeat = self.x_init.repeat(self.nks.shape[0], 1, 1, 1, 1)
        x_nk = torch.cat([x_repeat, self.nks], dim=1)
        phi_x_nt_nk = cross_phi_chunked(x_nk)
        c_types = ['spars', 'ps', 'phaseenv', 'envelope'
                   ]  # the coefficient type on which imposing independence
        phi_x_nt_nk_1 = phi_x_nt_nk.reduce(nl=0, nr=1, c_type=c_types)
        phi_x_nt_nk_2 = phi_x_nt_nk.reduce(nl=1, nr=0, c_type=c_types)
        std_cross1 = phi_x_nt_nk_1.y.std(0)[:, 0]
        std_cross2 = phi_x_nt_nk_2.y.std(0)[:, 0]

        return std_nks, std_x_nks, [std_cross1, std_cross2]

    def init_description(self):
        """ Pandas dataframe used to describe each coefficient. """
        # describes phi(nt)
        descri1 = self.phi.description.clone()
        descri1['deglitch_loss_term'] = 0

        # describes phi(x-nt+nks)
        descri2 = self.phi.description.clone()
        descri2['deglitch_loss_term'] = 1

        # describes phi(x-nt, nk)
        descri3 = self.cross_phi.description.clone()
        descri3['deglitch_loss_term'] = 2

        return Description.cat(descri1, descri2, descri3)

    def count_coefficients(self, **kwargs):
        """ Returns the number of moments satisfying kwargs. """
        return -1

    def forward(self, nt, bs):
        """ Compute phi(nt), phi(x-nt+nks), phi(nt,x-nks). """
        nks_b = self.nks[bs, ...]  # nks for this batch

        # statistics phi(nt)
        y_nt = self.phi(nt).y.repeat(bs.size, 1, 1)

        # statistics phi(x-nt+nk)  # different ks appears as different nl values in self.description
        y_x_nt_nk = self.phi(self.x_init - nt + nks_b).y

        # statistics phi(x-nt, nk)
        x_nt_nk = (self.x_init - nt).repeat(bs.size, 1, 1, 1, 1)
        x_nt_nk = torch.cat([x_nt_nk, nks_b], dim=1)
        y_indep = self.cross_phi(x_nt_nk).y

        return DescribedTensor(x=None,
                               y=torch.cat([y_nt, y_x_nt_nk, y_indep], dim=1),
                               descri=self.description)


def init_model(model_type, B, N, T, r, J, Q, wav_type, high_freq, wav_norm, qs,
               sigma2, norm_on_the_fly, c_types_used, estim_operator,
               channel_mode, nchunks, dtype, deglitching_params):
    """ Initialize a scattering covariance model.

    :param model_type: moments to compute on scattering
    :param B: batch size
    :param N: number of in_data channel
    :param T: number of time samples
    :param r: number of wavelet layers
    :param J: number of octaves for each waveelt layer
    :param Q: number of scales per octave for each wavelet layer
    :param wav_type: wavelet types for each wavelet layer
    :param high_freq: central frequency of mother wavelet for each waveelt layer, 0.5 gives important aliasing
    :param wav_norm: wavelet normalization for each waveelt layer e.g. l1, l2
        None: compute Sx = W|Wx|(t,j1,j2) and keep time axis t
        "scat": compute marginal moments on Sx: E{|Sx|^q} by time average
        "cov": compute covariance on Sx: Cov{Sx, Sx} as well as E{|Wx|} and E{|Wx|^2} by time average
        "covreduced": same as "cov" but compute reduced covariance: P Cov{Sx, Sx}, where P is the self-similar projection
        "scat+cov": both "scat" and "cov"
    :param qs: if model_type == 'scat' the exponents of the scattering marginal moments
    :param sigma2: a tensor of size B x N x J, wavelet power spectrum to normalize the representation with
    :param norm_on_the_fly: normalize first wavelet layer on the fly
    :param c_types_used: coefficient types used (None will use all of them)
    :param estim_operator: estimation operator to use
    :param channel_mode: wether to go full, diagonal, offdiag along in channels
    :param nchunks: the number of chunks
    :param dtype: data precision, either float32 or float64
    :param deglitching_params: dict containing signal x = n + g to deglitch and noise realizations \tilde{n}

    :return: a torch module
    """
    if deglitching_params is not None:
        batch_chunk = nchunks
        cov_chunk = 1
    elif nchunks < B:
        batch_chunk = nchunks
        cov_chunk = 1
    else:
        batch_chunk = B
        cov_chunk = nchunks // B

    # SCATTERING MODULE
    Ns = [N] * (r + 1)

    if deglitching_params is None:
        model = Model(model_type, qs, None, T, r, J, Q, wav_type, high_freq,
                      wav_norm, N, Ns, channel_mode, sigma2, norm_on_the_fly,
                      False, estim_operator, c_types_used, cov_chunk, dtype)

        model = ChunkedModule(model, batch_chunk)

    else:
        model = ModelDeglitching(deglitching_params['x_init'],
                                 deglitching_params['nks'],
                                 deglitching_params['cuda'],
                                 model_type=model_type,
                                 qs=qs,
                                 c_types=None,
                                 T=T,
                                 r=r,
                                 J=J,
                                 Q=Q,
                                 wav_type=wav_type,
                                 high_freq=high_freq,
                                 wav_norm=wav_norm,
                                 N=1,
                                 Ns=[1] * 2,
                                 channel_mode='full',
                                 sigma2=None,
                                 norm_on_the_fly=norm_on_the_fly,
                                 no_mean=False,
                                 estim_operator=estim_operator,
                                 c_types_used=c_types_used,
                                 cov_chunk=cov_chunk,
                                 dtype=dtype)
        model = ChunkedModuleDeglitching(model, batch_chunk)

    return model


def compute_sigma2(x, J, Q, wav_type, high_freq, wav_norm, nchunks, cuda):
    """ Computes power specturm sigma(j)^2 used to normalize scattering coefficients. """
    marginal_model = init_model(model_type='scat',
                                B=x.shape[0],
                                N=x.shape[1],
                                T=x.shape[-1],
                                r=1,
                                J=J,
                                Q=Q,
                                wav_type=wav_type,
                                high_freq=high_freq,
                                wav_norm=wav_norm,
                                qs=[2.0],
                                sigma2=None,
                                norm_on_the_fly=False,
                                c_types_used=None,
                                estim_operator=None,
                                channel_mode='diag',
                                nchunks=nchunks,
                                dtype=x.dtype,
                                deglitching_params=None)
    if cuda:
        x = x.cuda()
        marginal_model = marginal_model.cuda()

    sigma2 = marginal_model(x).y.reshape(x.shape[0], x.shape[1],
                                         -1)  # B x N x J

    return sigma2


def analyze(x,
            model_type='cov',
            r=2,
            J=None,
            Q=1,
            wav_type='battle_lemarie',
            wav_norm='l1',
            high_freq=0.425,
            qs=None,
            normalize=None,
            keep_ps=False,
            sigma2=None,
            channel_mode='diag',
            estim_operator=None,
            nchunks=1,
            cuda=False):
    """ Compute scattering based model.

    :param x: an array of shape (T, ) or (B, T) or (B, N, T)
    :param r: number of wavelet layers
    :param J: number of octaves for each wavelet layer
    :param Q: number of scales per octave for each wavelet layer
    :param wav_type: wavelet types for each wavelet layer
    :param wav_norm: wavelet normalization i.e. l1, l2 for each layer
    :param high_freq: central frequency of mother wavelet for each layer, 0.5 gives important aliasing
    :param model_type: moments to compute on scattering
        None: compute Sx = W|Wx|(t,j1,j2) and keep time axis t
        "scat": compute marginal moments on Sx: E{|Sx|^q} by time average
        "cov": compute covariance on Sx: Cov{Sx, Sx} as well as E{|Wx|} and E{|Wx|^2} by time average
        "covreduced": same as "cov" but compute reduced covariance: P Cov{Sx, Sx}, where P is the self-similar projection
        "scat+cov": both "scat" and "cov"
    :param normalize:
        None: no normalization,
        "each_ps": normalize Rx.y[b,:,:] by its power spectrum
        "batch_ps": normalize RX.y[b,:,:] by the average power spectrum over all trajectories b in the batch
    :param qs: exponent to use in a marginal model
    :param keep_ps: keep the power spectrum even after normalization
    :param channel_mode: wether to go full, diagonal, offdiag along in channels
    :param estim_operator: AveragingOperator by default, but can be overwritten
    :param nchunks: nb of chunks, increase it to reduce memory usage
    :param cuda: does calculation on gpu

    :return: a DescribedTensor result
    """
    if model_type not in [None, "scat", "cov", "covreduced", "scat+cov"]:
        raise ValueError("Unrecognized model type.")
    if normalize not in [None, "each_ps", "batch_ps"]:
        raise ValueError("Unrecognized normalization.")
    if model_type == "covreduced" and normalize is None:
        raise ValueError(
            "For covreduced model, user should provide a normalize argument.")
    if r > 2 and model_type not in [None, 'scat']:
        raise ValueError(
            "Moments with covariance are not implemented for more than 3 convolution layers."
        )

    if len(x.shape) == 1:  # assumes that x is of shape (T, )
        x = x[None, None, :]
    elif len(x.shape) == 2:  # assumes that x is of shape (B, T)
        x = x[:, None, :]

    B, N, T = x.shape
    x = torch.from_numpy(x)[:, :, None, None, :]

    if x.dtype not in [torch.float32, torch.float64]:
        x = x.type(torch.float32)
        print("WARNING. Casting data to float 32.")
    dtype = x.dtype

    if J is None:
        J = int(np.log2(T)) - 3
    if isinstance(J, int):
        J = [J] * r
    if isinstance(Q, int):
        Q = [Q] * r
    if isinstance(wav_type, str):
        wav_type = [wav_type] * r
    if isinstance(wav_norm, str):
        wav_norm = [wav_norm] * r
    if isinstance(high_freq, float):
        high_freq = [high_freq] * r
    if qs is None:
        qs = [1.0, 2.0]

    # covreduced needs a spectrum normalization
    if normalize is not None and sigma2 is None:
        sigma2 = compute_sigma2(x, J, Q, wav_type, high_freq, wav_norm,
                                nchunks, cuda)
        if normalize == "batch_ps":
            sigma2 = sigma2.mean(0, keepdim=True)

    # initialize model
    model = init_model(model_type=model_type,
                       B=B,
                       N=N,
                       T=T,
                       r=r,
                       J=J,
                       Q=Q,
                       wav_type=wav_type,
                       high_freq=high_freq,
                       wav_norm=wav_norm,
                       qs=qs,
                       sigma2=sigma2,
                       norm_on_the_fly=normalize == "each_ps",
                       estim_operator=estim_operator,
                       c_types_used=None,
                       channel_mode=channel_mode,
                       nchunks=nchunks,
                       dtype=dtype,
                       deglitching_params=None)

    # compute
    if cuda:
        x = x.cuda()
        model = model.cuda()

    Rx = model(x)

    if keep_ps and normalize is not None and model_type in [
            "cov", "covreduced", "scat+cov"
    ] and estim_operator is None:
        # retrieve the power spectrum that was normalized
        for n in range(N):
            mask_ps = Rx.descri.where(c_type='ps', nl=n, nr=n)
            if mask_ps.sum() != 0:
                Rx.y[:,
                     mask_ps, :] = Rx.y[:,
                                        mask_ps, :] * sigma2[:, n, :].reshape(
                                            sigma2.shape[0], -1, 1)

    return Rx.cpu()


def format_to_real(Rx):
    """ Transforms a complex described tensor z into a real tensor (Re z, Im z). """
    if "real" not in Rx.descri:
        raise ValueError(
            "Described tensor should have a column indicating which coefficients are real."
        )
    Rx_real = Rx.reduce(real=True)
    Rx_complex = Rx.reduce(real=False)

    descri_complex_real = Rx_complex.descri.clone()
    descri_complex_imag = Rx_complex.descri.clone()
    descri_complex_real["real"] = True
    descri = Description(
        pd.concat([Rx_real.descri, descri_complex_real, descri_complex_imag]))

    y = torch.cat([Rx_real.y.real, Rx_complex.y.real, Rx_complex.y.imag],
                  dim=1)

    return DescribedTensor(None, y, descri)


def self_simi_obstruction_score(x,
                                J=None,
                                Q=1,
                                wav_type='battle_lemarie',
                                wav_norm='l1',
                                high_freq=0.425,
                                nchunks=1,
                                cuda=False):
    """ Quantifies obstruction to self-similarity in a certain range of scales.

    :param x: an array of shape (T, ) or (B, T) or (B, N, T)
    :param J: number of octaves for each wavelet layer
    :param Q: number of scales per octave for each wavelet layer
    :param wav_type: wavelet types for each wavelet layer
    :param wav_norm: wavelet normalization i.e. l1, l2 for each layer
    :param high_freq: central frequency of mother wavelet for each layer, 0.5 gives important aliasing
    :param nchunks: nb of chunks, increase it to reduce memory usage
    :param cuda: does calculation on gpu

    :return:
        - score on white noise reference (gives the score estimation error)
        - score on x
    """
    Rx = analyze(x,
                 model_type='cov',
                 r=2,
                 J=J,
                 Q=Q,
                 wav_type=wav_type,
                 wav_norm=wav_norm,
                 high_freq=high_freq,
                 qs=None,
                 normalize="batch_ps",
                 keep_ps=True,
                 sigma2=None,
                 channel_mode='diag',
                 estim_operator=None,
                 nchunks=nchunks,
                 cuda=cuda).mean_batch()

    # white noise reference score
    x_wn = np.random.randn(*x.shape)
    Rx_wn = analyze(x_wn,
                    model_type='cov',
                    r=2,
                    J=J,
                    Q=Q,
                    wav_type=wav_type,
                    wav_norm=wav_norm,
                    high_freq=high_freq,
                    qs=None,
                    normalize="batch_ps",
                    keep_ps=True,
                    sigma2=None,
                    channel_mode='diag',
                    estim_operator=None,
                    nchunks=nchunks,
                    cuda=cuda).mean_batch()

    def self_simi_score_spars(Rx):
        Wx1 = Rx.select(c_type='spars', low=False)[0, :, 0]
        logWxs = Wx1.pow(2.0).log2()
        dlogWxs = torch.diff(logWxs)
        return 1e1 * dlogWxs.std().numpy().item()

    def self_simi_score_ps(Rx):
        logWx2 = Rx.select(c_type='ps', low=False)[0, :, 0].log2()
        dlogWx2 = torch.diff(logWx2)
        return 2e1 * dlogWx2.std().numpy().item()

    def self_simi_score_phase_mod(Rx):
        J = Rx.descri.j.max(
        ) if 'j' in Rx.descri.columns else Rx.descri.jl1.max()

        score = 0.0
        for a in range(1, J - 1):
            phi3 = torch.stack([
                Rx.select(c_type='phaseenv', jl1=j1, jr1=j1 - a,
                          low=False)[0, 0, 0] for j1 in range(a, J)
            ])
            score += phi3.std()

        return 1e1 * score.numpy() / (J - 1)

    def self_simi_score_mod(Rx):
        J = Rx.descri.j.max(
        ) if 'j' in Rx.descri.columns else Rx.descri.jl1.max()

        ndiagonals = 0
        score = 0.0
        for (a, b) in product(range(J - 1), range(-J + 1, 0)):
            if a - b >= J - 1:
                continue
            phi4 = torch.stack([
                Rx.select(c_type='envelope',
                          jl1=j1,
                          jr1=j1 - a,
                          j2=j1 - b,
                          low=False)[0, 0, 0] for j1 in range(a, J + b)
            ])
            ndiagonals += 1
            score += phi4.std()

        return 1e2 * score.numpy() / ndiagonals

    def get_score(Rx):
        score_phi1 = self_simi_score_spars(Rx)
        score_phi2 = self_simi_score_ps(Rx)
        score_phi3 = self_simi_score_phase_mod(Rx)
        score_phi4 = self_simi_score_mod(Rx)
        return {
            'spars': score_phi1,
            'ps': score_phi2,
            'phaseenv': score_phi3,
            'envelope': score_phi4,
            'total': score_phi1 + score_phi2 + score_phi3 + score_phi4
        }

    return get_score(Rx_wn), get_score(Rx)


##################
# GENERATION
##################


class GenDataLoader(ProcessDataLoader):
    """ A data loader for generation. Caches the generated trajectories. """

    def __init__(self, *args):
        super(GenDataLoader, self).__init__(*args)
        self.default_kwargs = {}

    def dirpath(self, **kwargs):
        """ The directory path in which the generated trajectories will be stored. """
        B_target = kwargs['x'].shape[0]
        model_params = kwargs['model_params']
        N, T, r, J, Q, wav_type, model_type = \
            (model_params[key] for key in ['N', 'T', 'r', 'J', 'Q', 'wav_type', 'model_type'])
        path_str = f"{self.model_name}_{wav_type[0]}_B{B_target}_N{N}_T{T}_J{J[0]}_Q1_{Q[0]}_Q2_{Q[1]}_rmax{r}_model_{model_type}" \
                   + f"_tol{kwargs['optim_params']['tol_optim']:.2e}" \
                   + f"_it{kwargs['optim_params']['it']}"
        return self.dir_name / path_str.replace('.', '_').replace('-', '_')

    def generate_trajectory(self, seed, x, Rx, model_params, optim_params, gpu,
                            dirpath):
        """ Performs cached generation. """
        if gpu is not None:
            os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu)
        np.random.seed(seed)
        filename = dirpath / str(np.random.randint(1e7, 1e8))
        if filename.is_file():
            raise OSError("File for saving this trajectory already exists.")

        x_torch = torch.from_numpy(x.astype(
            np.float64)).unsqueeze(-2).unsqueeze(-2)
        # sigma = None
        sigma2 = compute_sigma2(x_torch, model_params['J'], model_params['Q'],
                                model_params['wav_type'],
                                model_params['high_freq'],
                                model_params['wav_norm'],
                                model_params['nchunks'], optim_params['cuda'])

        model_params['sigma2'] = sigma2.mean(
            0, keepdim=True)  # do a "batch_ps" normalization

        # initialize model
        model = init_model(B=x.shape[0], **model_params)
        if optim_params['cuda'] and gpu is not None:
            x_torch = x_torch.cuda()
            model = model.cuda()

        # prepare target representation
        if Rx is None:
            if model_params['deglitching_params'] is None:
                Rx = model(x_torch).cpu()
            else:
                Rx = None

        # prepare initial gaussian process
        if optim_params['x0'] is not None:
            x0 = optim_params['x0']
        else:
            x0_mean = x.mean(-1).mean(0)
            x0_var = np.var(x, axis=-1).mean(0)

            def gen_wn(shape, mean, var):
                wn = np.random.randn(*shape)
                if x.dtype == np.float32:
                    wn = np.float32(wn)
                wn -= wn.mean(axis=-1, keepdims=True)
                wn /= np.std(wn, axis=-1, keepdims=True)

                return wn * var[:, None] + mean[:, None]

            x0 = gen_wn(x.shape, x0_mean, x0_var**0.5)

        # init loss, solver and convergence criterium
        if model_params['deglitching_params'] is None:
            loss = MSELossScat()
            # This renders `fixed_ts` useless in this case.
            solver_fn = Solver(model=model,
                               loss=loss,
                               xf=x,
                               Rxf=Rx,
                               x0=x0,
                               fixed_ts=None,
                               cuda=optim_params['cuda'])
        else:
            loss = DeglitchingLoss(
                phi_x=model.module.phi_x,
                phi_nks=model.module.phi_nks,
                std_nks=model.module.std_nks,
                std_x_nks=model.module.std_x_nks,
                std_cross=model.module.std_cross,
                x_loss_w=model_params['deglitching_params']['x_loss_w'],
                indep_loss_w=model_params['deglitching_params']
                ['indep_loss_w'])
            solver_fn = Solver(
                model=model,
                loss=loss,
                xf=x,
                Rxf=Rx,
                x0=x0,
                fixed_ts=model_params['deglitching_params']['fixed_ts'],
                cuda=optim_params['cuda'])

        check_conv_criterion = CheckConvCriterion(
            solver=solver_fn, tol=optim_params['tol_optim'])

        print('Embedding: uses {} coefficients {}'.format(
            model.module.count_coefficients(), ' '.join([
                '{}={}'.format(c_type,
                               model.module.count_coefficients(c_type=c_type))
                for c_type in model.module.description.c_type.unique()
            ])))

        method, maxfun, jac = optim_params['method'], optim_params[
            'maxfun'], optim_params['jac']
        relative_optim, it = optim_params['relative_optim'], optim_params['it']

        tic = time()
        # Decide if the function provides gradient or not
        func = solver_fn.joint if jac else solver_fn.function
        try:
            res = scipy.optimize.minimize(func,
                                          x0.reshape(-1),
                                          method=method,
                                          jac=jac,
                                          callback=check_conv_criterion,
                                          options={
                                              'ftol': 1e-24,
                                              'gtol': 1e-24,
                                              'maxiter': it,
                                              'maxfun': maxfun
                                          })
            loss_tmp, x_opt, it, msg = res['fun'], res['x'], res['nit'], res[
                'message']
        except SmallEnoughException:  # raised by check_conv_criterion
            print('SmallEnoughException')
            x_opt = check_conv_criterion.result
            it = check_conv_criterion.counter
            msg = "SmallEnoughException"

        toc = time()

        flo, fgr = solver_fn.joint(x_opt)
        flo, fgr = flo, np.max(np.abs(fgr))
        x_synt = x_opt.reshape(x0.shape)

        if not isinstance(msg, str):
            msg = msg.decode("ASCII")

        print('Optimization Exit Message : ' + msg)
        print(
            f"found parameters in {toc - tic:0.2f}s, {it} iterations -- {it / (toc - tic):0.2f}it/s"
        )
        print(f"    abs sqrt error {flo ** 0.5:.2E}")
        print(f"    relative gradient error {fgr:.2E}")
        print(f"    loss0 {solver_fn.loss0:.2E}")

        if x.dtype == np.float32:
            x_synt = np.float32(x_synt)

        return x_synt  # S x N x T

    def worker(self, i, **kwargs):
        cuda = kwargs['optim_params']['cuda']
        gpus = kwargs['optim_params']['gpus']
        seed = kwargs['optim_params']['seed'][i]
        kwargs['seed'] = seed
        if cuda and gpus is None:
            kwargs['gpu'] = '0'
        elif cuda and gpus is not None:
            kwargs['gpu'] = str(gpus[i % len(gpus)])
        else:
            kwargs['gpu'] = None
        try:
            x = self.generate_trajectory(**kwargs)
            fname = f"{np.random.randint(1e7, 1e8)}.npy"
            np.save(str(kwargs['dirpath'] / fname), x)
            print(f"Saved: {kwargs['dirpath'].name}/{fname}")
        except ValueError as e:
            print(e)
            return


def generate(x,
             Rx=None,
             S=1,
             model_type='cov',
             r=2,
             J=None,
             Q=1,
             wav_type='battle_lemarie',
             wav_norm='l1',
             high_freq=0.425,
             channel_mode='diag',
             qs=None,
             c_types_used=None,
             nchunks=1,
             it=10000,
             tol_optim=5e-4,
             seed=None,
             x0=None,
             generated_dir=None,
             exp_name=None,
             cuda=False,
             gpus=None,
             num_workers=1,
             deglitching_params=None):
    """ Generate new realizations of x from a scattering covariance model.
    We first compute the scattering covariance representation of x and then sample it using gradient descent.

    :param x: an array of shape (T, ) or (B, T) or (B, N, T)
    :param Rx: instead of x, the representation to generate from
    :param S: number of syntheses
    :param r: number of wavelet layers
    :param J: number of octaves for each wavelet layer
    :param Q: number of scales per octave for each wavelet layer
    :param wav_type: wavelet type
    :param wav_norm: wavelet normalization i.e. l1, l2
    :param high_freq: central frequency of mother wavelet, 0.5 gives important aliasing
    :param channel_mode: diagonal model along input channels
    :param model_type: moments to compute on scattering, ex: None, 'scat', 'cov', 'covreduced'
    :param qs: if model_type == 'marginal' the exponents of the scattering marginal moments
    :param c_types_used: coefficient types used (None will use all of them)
    :param nchunks: nb of chunks, increase it to reduce memory usage
    :param it: maximum number of gradient descent iteration
    :param tol_optim: error below which gradient descent stops
    :param seed: None, int or list, random seed of original white noise in gradient descent for each synthesis
    :param x0: an array of shape (B, N, T) for the initial signal in the gradient descent
    :param generated_dir: the directory in which the generated dir will be located
    :param exp_name: experience name
    :param cuda: does calculation on gpu
    :param gpus: a list of gpus to use
    :param num_workers: number of generation workers
    :param deglitching_params: dict containing signal x = n + g to deglitch and noise realizations \tilde{n}

    :return: a DescribedTensor result
    """
    if len(x.shape) == 1:  # assumes that x is of shape (T, )
        x = x[None, None, :]
    elif len(x.shape) == 2:  # assumes that x is of shape (B, T)
        x = x[:, None, :]

    B, N, T = x.shape

    if J is None:
        J = int(np.log2(T)) - 5
    if isinstance(J, int):
        J = [J] * r
    if isinstance(Q, int):
        Q = [Q] * r
    if isinstance(wav_type, str):
        wav_type = [wav_type] * r
    if isinstance(wav_norm, str):
        wav_norm = [wav_norm] * r
    if isinstance(high_freq, float):
        high_freq = [high_freq] * r
    if seed is None or isinstance(seed, int):
        seed = [seed] * S
    if qs is None:
        qs = [1.0, 2.0]
    if generated_dir is None:
        generated_dir = Path(__file__).parents[0] / '_cached_dir'
    if x0 is not None and x0.shape != x.shape:
        raise ValueError(f"If specified, x0 should be of shape {x.shape}")

    # use a GenDataLoader to cache trajectories
    dtld = GenDataLoader(exp_name or 'gen_scat_cov', generated_dir,
                         num_workers)

    # MODEL params
    model_params = {
        'N': N,
        'T': T,
        'r': r,
        'J': J,
        'Q': Q,
        'wav_type': wav_type,  # 'battle_lemarie' 'morlet' 'shannon'
        'high_freq': high_freq,  # 0.323645 or 0.425
        'wav_norm': wav_norm,
        'model_type': model_type,
        'qs': qs,
        'sigma2': None,
        'norm_on_the_fly': False,
        'c_types_used': c_types_used,
        'nchunks': nchunks,
        'estim_operator': None,
        'channel_mode': channel_mode,
        'dtype': torch.float64 if x.dtype == np.float64 else torch.float32,
        'deglitching_params': deglitching_params
    }

    # OPTIM params
    optim_params = {
        'it': it,
        'cuda': cuda,
        'gpus': gpus,
        'relative_optim': False,
        'maxfun': 2e6,
        'method': 'L-BFGS-B',
        'jac':
        True,  # origin of gradient, True: provided by solver, else estimated
        'tol_optim': tol_optim,
        'seed': seed,
        'x0': x0
    }

    # multi-processed generation
    x_gen = dtld.load(R=S,
                      n_files=int(np.ceil(S / B)),
                      x=x,
                      Rx=Rx,
                      model_params=model_params,
                      optim_params=optim_params).x

    return x_gen


##################
# VIZUALIZE
##################

COLORS = [
    'skyblue', 'coral', 'lightgreen', 'darkgoldenrod', 'mediumpurple', 'red',
    'purple', 'black', 'paleturquoise'
] + ['orchid'] * 20


def bootstrap_variance_complex(x, n_points, n_samples):
    """ Estimate variance of tensor x along last axis using bootstrap method. """
    # sample data uniformly
    sampling_idx = np.random.randint(low=0,
                                     high=x.shape[-1],
                                     size=(n_samples, n_points))
    sampled_data = x[..., sampling_idx]

    # computes mean
    mean = sampled_data.mean(-1).mean(-1)

    # computes bootstrap variance
    var = (torch.abs(sampled_data.mean(-1) -
                     mean[..., None]).pow(2.0)).sum(-1) / (n_samples - 1)

    return mean, var


def error_arg(z_mod, z_err, eps=1e-12):
    """ Transform an error on |z| into an error on Arg(z). """
    z_mod = np.maximum(z_mod, eps)
    return np.arctan(z_err / z_mod /
                     np.sqrt(np.clip(1 - z_err**2 / z_mod**2, 1e-6, 1)))


def get_variance(z):
    """ Compute complex variance of a sequence of complex numbers z1, z2, ... """
    B = z.shape[0]
    return torch.abs(z - z.mean(0, keepdim=True)).pow(2.0).sum(0).div(B -
                                                                      1).div(B)


def plot_raw(Rx, ax, legend=False):
    """ Raw plot of the coefficients contained in Rx. """
    if 'j' in Rx.descri.columns or 'j2' not in Rx.descri.columns:
        raise ValueError(
            "Raw plot of DescribedTensor only implemented for ouput of cov model."
        )
    descri = Description(
        Rx.descri.reindex(columns=[
            'c_type', 'real', 'low', 'nl', 'nr', 'q', 'rl', 'rr', 'scl', 'scr',
            'jl1', 'jr1', 'j2', 'al', 'ar'
        ]))
    Rx = DescribedTensor(x=None, descri=descri, y=Rx.y).mean_batch()
    if Rx.y.is_complex():
        Rx = format_to_real(Rx)
    Rx = Rx.sort()
    ctypes = Rx.descri['c_type'].unique()

    cycle = plt.rcParams['axes.prop_cycle'].by_key()['color']
    for i, ctype in enumerate(ctypes):
        mask_real = np.where(Rx.descri.where(c_type=ctype, real=True))[0]
        mask_imag = np.where(Rx.descri.where(c_type=ctype, real=False))[0]
        ax.axvspan(mask_real.min(),
                   mask_real.max(),
                   color=cycle[i],
                   label=ctype if legend else None,
                   alpha=0.7)
        if mask_imag.size > 0:
            ax.axvspan(mask_imag.min(),
                       mask_imag.max(),
                       color=cycle[i],
                       alpha=0.4)
        ax.axhline(0.0, color='black', linewidth=0.02)
    ax.plot(Rx.y[0, :, 0], linewidth=0.7)
    if legend:
        ax.legend()
    return Rx.descri


def plot_marginal_moments(Rxs,
                          estim_bar=False,
                          axes=None,
                          labels=None,
                          linewidth=3.0,
                          fontsize=30):
    """ Plot the marginal moments
        - (wavelet power spectrum) sigma^2(j)
        - (sparsity factors) s^2(j)

    :param Rxs: DescribedTensor or list of DescribedTensor
    :param estim_bar: display estimation error due to estimation on several realizations
    :param axes: custom axes: array of size 2
    :param labels: list of labels for each model output
    :param linewidth: curve linewidth
    :param fontsize: labels fontsize
    :return:
    """
    if isinstance(Rxs, DescribedTensor):
        Rxs = [Rxs]
    if labels is not None and len(Rxs) != len(labels):
        raise ValueError("Invalid number of labels")
    if axes is not None and axes.size != 2:
        raise ValueError(
            "The axes provided to plot_marginal_moments should be an array of size 2."
        )

    labels = labels or [''] * len(Rxs)
    axes = None if axes is None else axes.ravel()

    def plot_exponent(js, ax, label, color, y, y_err):
        plt.sca(ax)
        plt.plot(-js, y, label=label, linewidth=linewidth, color=color)
        if not estim_bar:
            plt.scatter(-js,
                        y,
                        marker='+',
                        s=200,
                        linewidth=linewidth,
                        color=color)
        else:
            eb = plt.errorbar(-js,
                              y,
                              yerr=y_err,
                              capsize=4,
                              color=color,
                              fmt=' ')
            eb[-1][0].set_linestyle('--')
        plt.yscale('log', base=2)
        plt.xlabel(r'$-j$', fontsize=fontsize)
        plt.yticks(fontsize=fontsize)
        plt.xticks(-js, [fr'$-{j + 1}$' for j in js], fontsize=fontsize)

    if axes is None:
        ax1 = plt.subplot2grid((1, 2), (0, 0))
        ax2 = plt.subplot2grid((1, 2), (0, 1))
        axes = [ax1, ax2]

    for i_lb, (lb, Rx) in enumerate(zip(labels, Rxs)):
        if 'c_type' not in Rx.descri.columns:
            raise ValueError("The model output does not have the moments.")
        js = np.unique(Rx.descri.reduce(low=False).jl1.dropna())

        has_power_spectrum = 2.0 in Rx.descri.q.values
        has_sparsity = 1.0 in Rx.descri.q.values

        # averaging on the logs may have strange behaviors because of the strict convexity of the log
        if has_power_spectrum:
            Wx2_nj = Rx.select(rl=1,
                               c_type=['ps', 'scat', 'spars', 'marginal'],
                               q=2.0,
                               low=False)[:, :, 0]
            if Wx2_nj.is_complex():
                Wx2_nj = Wx2_nj.real
            logWx2_n = torch.log2(Wx2_nj)

            # little subtlety here, we plot the log on the mean but the variance is the variance on the log
            logWx2_err = get_variance(logWx2_n)**0.5
            logWx2 = torch.log2(Wx2_nj.mean(0))
            # logWx2 -= logWx2[0].item()
            ps_norm_rectifier = 2 * 0.5 * np.arange(logWx2.shape[-1])
            plot_exponent(js, axes[0], lb, COLORS[i_lb],
                          2.0**(logWx2 + ps_norm_rectifier),
                          np.log(2) * logWx2_err * 2.0**logWx2)
            a, b = axes[0].get_ylim()
            if i_lb == len(labels):
                axes[0].set_ylim(min(a, 2**(-2)), max(b, 2**2))
            if i_lb == len(labels) - 1 and any([lb != '' for lb in labels]):
                plt.legend(prop={'size': 15})
            plt.title(r'Wavelet Spectrum $\Phi_2$', fontsize=fontsize)

            if has_sparsity:
                Wx1_nj = Rx.select(rl=1,
                                   c_type=['ps', 'scat', 'spars', 'marginal'],
                                   q=1.0,
                                   low=False)[:, :, 0]
                if Wx1_nj.is_complex():
                    Wx1_nj = Wx1_nj.real
                logWx1_nj = torch.log2(Wx1_nj)

                logWxs_n = 2 * logWx1_nj - logWx2_n.mean(0, keepdims=True)
                logWxs_err = get_variance(logWxs_n)**0.5
                logWxs = torch.log2(Wx1_nj.mean(0).pow(2.0) / Wx2_nj.mean(0))
                plot_exponent(js, axes[1], lb, COLORS[i_lb], 2.0**logWxs,
                              np.log(2) * logWxs_err * 2.0**logWxs)
                a, b = axes[1].get_ylim()
                if i_lb == len(labels) - 1:
                    axes[1].set_ylim(min(2**(-2), a), 1.0)
                if i_lb == len(labels) - 1 and any([lb != ''
                                                    for lb in labels]):
                    plt.legend(prop={'size': 15})
                plt.title(r'Sparsity factors $\Phi_1$', fontsize=fontsize)


def plot_phase_envelope_spectrum(Rxs,
                                 estim_bar=False,
                                 self_simi_bar=False,
                                 theta_threshold=0.005,
                                 sigma2=None,
                                 axes=None,
                                 labels=None,
                                 fontsize=30,
                                 single_plot=False,
                                 ylim=0.1):
    """ Plot the phase-envelope cross-spectrum C_{W|W|}(a) as two graphs : |C_{W|W|}| and Arg(C_{W|W|}).

    :param Rxs: DescribedTensor or list of DescribedTensor
    :param estim_bar: display estimation error due to estimation on several realizations
    :param self_simi_bar: display self-similarity error, it is a measure of scale regularity
    :param theta_threshold: rules phase instability
    :param sigma2: override normalization factors
    :param axes: custom axes: array of size 2
    :param labels: list of labels for each model output
    :param fontsize: labels fontsize
    :param single_plot: output all DescribedTensor on a single plot
    :param ylim: above y limit of modulus graph
    :return:
    """
    if isinstance(Rxs, DescribedTensor):
        Rxs = [Rxs]
    if labels is not None and len(Rxs) != len(labels):
        raise ValueError("Invalid number of labels")

    labels = labels or [''] * len(Rxs)
    columns = Rxs[0].descri.columns
    J = Rxs[0].descri.j.max() if 'j' in columns else Rxs[0].descri.jl1.max()

    c_wmw = torch.zeros(len(labels), J - 1, dtype=Rxs[0].y.dtype)
    err_estim = torch.zeros(len(labels), J - 1)
    err_self_simi = torch.zeros(len(labels), J - 1)

    for i_lb, Rx in enumerate(Rxs):

        if 'phaseenv' not in Rx.descri.c_type.values:
            continue

        model_type = 'general'
        if 'a' in Rx.descri.columns:
            model_type = 'covreduced'

        B = Rx.y.shape[0]

        norm2 = sigma2
        if sigma2 is None:
            norm2 = Rx.select(
                rl=1, rr=1, q=2,
                low=False).real.mean(0)[None, :,
                                        0]  # power spectrum averaged on batch
            if Rx.descri['nl'].iloc[0] != Rx.descri['nr'].iloc[0]:
                print(
                    "WARNING. Carefull, sigma2 should be given for left and right independently."
                )

        for a in range(1, J):
            if model_type == 'covreduced':
                c_mwm_n = Rx.select(c_type='phaseenv', a=a, low=False)
                c_mwm_n = c_mwm_n[:, 0, 0]

                c_wmw[i_lb, a - 1] = c_mwm_n.mean(0)
                err_estim[i_lb, a - 1] = get_variance(c_mwm_n).pow(0.5)
            else:
                c_mwm_nj = torch.zeros(B, J - a, dtype=Rx.y.dtype)
                for j1 in range(a, J):
                    coeff = Rx.select(c_type='phaseenv',
                                      jl1=j1,
                                      jr1=j1 - a,
                                      low=False)
                    coeff = coeff[:, 0, 0]
                    coeff /= norm2[:, j1, ...].pow(0.5) * norm2[:, j1 - a,
                                                                ...].pow(0.5)
                    c_mwm_nj[:, j1 - a] = coeff

                # the mean in j of the variance of time estimators
                c_wmw[i_lb, a - 1] = c_mwm_nj.mean(0).mean(0)
                err_self_simi_n = (
                    torch.abs(c_mwm_nj).pow(2.0).mean(1) -
                    torch.abs(c_mwm_nj.mean(1)).pow(2.0)) / c_mwm_nj.shape[1]
                err_self_simi[i_lb, a - 1] = err_self_simi_n.mean(0).pow(0.5)
                err_estim[i_lb,
                          a - 1] = get_variance(c_mwm_nj.mean(1)).pow(0.5)

    c_wmw_mod, cwmw_arg = np.abs(c_wmw.numpy()), np.angle(c_wmw.numpy())
    err_self_simi, err_estim = to_numpy(err_self_simi), to_numpy(err_estim)
    err_self_simi_arg, err_estim_arg = error_arg(c_wmw_mod,
                                                 err_self_simi), error_arg(
                                                     c_wmw_mod, err_estim)

    # phase instability at z=0
    for z_arg in [cwmw_arg, err_self_simi_arg, err_estim_arg]:
        z_arg[c_wmw_mod < theta_threshold] = 0.0

    def plot_modulus(i_lb, label, color, y, y_err_estim, y_err_self_simi):
        a_s = np.arange(1, J)
        plt.plot(a_s, y, color=color or 'green', label=label)
        if not estim_bar and not self_simi_bar:
            plt.scatter(a_s, y, color=color or 'green', marker='+')
        if self_simi_bar:
            plot_x_offset = -0.07 if estim_bar else 0.0
            plt.errorbar(a_s + plot_x_offset,
                         y,
                         yerr=y_err_self_simi,
                         capsize=4,
                         color=color,
                         fmt=' ')
        if estim_bar:
            plot_x_offset = 0.07 if self_simi_bar else 0.0
            eb = plt.errorbar(a_s + plot_x_offset,
                              y,
                              yerr=y_err_estim,
                              capsize=4,
                              color=color,
                              fmt=' ')
            eb[-1][0].set_linestyle('--')
        plt.title('Cross-spectrum' + '\n' + r'$|\Phi_3|$', fontsize=fontsize)
        plt.axhline(0.0, linewidth=0.7, color='black')
        plt.xticks(np.arange(1, J), [(rf'${j}$' if j % 2 == 1 else '')
                                     for j in np.arange(1, J)],
                   fontsize=fontsize)
        plt.xlabel(r'$a$', fontsize=fontsize)
        plt.ylim(-0.02, ylim)
        if i_lb == 0:
            plt.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))
            plt.yticks(fontsize=fontsize)
        plt.locator_params(axis='y', nbins=5)

    def plot_phase(label, color, y, y_err_estim, y_err_self_simi):
        a_s = np.arange(1, J)
        plt.plot(a_s, y, color=color, label=label)
        if not estim_bar and not self_simi_bar:
            plt.scatter(a_s, y, color=color, marker='+')
        if self_simi_bar:
            plot_x_offset = -0.07 if estim_bar else 0.0
            plt.errorbar(a_s + plot_x_offset,
                         y,
                         yerr=y_err_self_simi,
                         capsize=4,
                         color=color,
                         fmt=' ')
        if estim_bar:
            plot_x_offset = 0.07 if self_simi_bar else 0.0
            eb = plt.errorbar(a_s + plot_x_offset,
                              y,
                              yerr=y_err_estim,
                              capsize=4,
                              color=color,
                              fmt=' ')
            eb[-1][0].set_linestyle('--')
        plt.xticks(np.arange(1, J), [(rf'${j}$' if j % 2 == 1 else '')
                                     for j in np.arange(1, J)],
                   fontsize=fontsize)
        plt.yticks([-np.pi, -0.5 * np.pi, 0.0, 0.5 * np.pi, np.pi], [
            r'$-\pi$', r'$-\frac{\pi}{2}$', r'$0$', r'$\frac{\pi}{2}$',
            r'$\pi$'
        ],
                   fontsize=fontsize)
        plt.axhline(0.0, linewidth=0.7, color='black')
        plt.xlabel(r'$a$', fontsize=fontsize)
        plt.title('Cross-spectrum' + '\n' + r'Arg$\Phi_3$', fontsize=fontsize)

    if axes is None:
        plt.figure(figsize=(5, 10) if single_plot else (len(labels) * 5, 10))
        ax_mod = plt.subplot2grid((2, 1), (0, 0))
        ax_mod.yaxis.set_tick_params(which='major',
                                     direction='in',
                                     width=1.5,
                                     length=7)
    else:
        plt.sca(axes[0])
    for i_lb, lb in enumerate(labels):
        plot_modulus(i_lb, lb, COLORS[i_lb], c_wmw_mod[i_lb], err_estim[i_lb],
                     err_self_simi[i_lb])
        if i_lb == len(labels) - 1 and any([lb != '' for lb in labels]):
            plt.legend(prop={'size': 15})

    if axes is None:
        plt.subplot2grid((2, 1), (1, 0))
    else:
        plt.sca(axes[1])
    for i_lb, lb in enumerate(labels):
        plot_phase(lb, COLORS[i_lb], cwmw_arg[i_lb], err_estim_arg[i_lb],
                   err_self_simi_arg[i_lb])

    if axes is None:
        plt.tight_layout()


def plot_scattering_spectrum(Rxs,
                             estim_bar=False,
                             self_simi_bar=False,
                             bootstrap=True,
                             theta_threshold=0.01,
                             sigma2=None,
                             axes=None,
                             labels=None,
                             fontsize=40,
                             ylim=2.0,
                             d=1):
    """ Plot the scattering cross-spectrum C_S(a,b) as two graphs : |C_S| and Arg(C_S).

    :param Rxs: DescribedTensor or list of DescribedTensor
    :param estim_bar: display estimation error due to estimation on several realizations
    :param self_simi_bar: display self-similarity error, it is a measure of scale regularity
    :param theta_threshold: rules phase instability
    :param sigma2: override normalization factors
    :param axes: custom axes: array of size 2 x labels
    :param labels: list of labels for each model output
    :param fontsize: labels fontsize
    :param ylim: above y limit of modulus graph
    :return:
    """
    if isinstance(Rxs, DescribedTensor):
        Rxs = [Rxs]
    if labels is not None and len(Rxs) != len(labels):
        raise ValueError("Invalid number of labels.")
    if axes is not None and axes.size != 2 * len(Rxs):
        raise ValueError(
            f"Existing axes must be provided as an array of size {2 * len(Rxs)}"
        )

    axes = None if axes is None else axes.reshape(2, len(Rxs))

    labels = labels or [''] * len(Rxs)
    i_graphs = np.arange(len(labels))

    columns = Rxs[0].descri.columns
    J = Rxs[0].descri.j.max() if 'j' in columns else Rxs[0].descri.jl1.max()

    cs = torch.zeros(len(labels), J - 1, J - 1, dtype=Rxs[0].y.dtype)
    err_estim = torch.zeros(len(labels), J - 1, J - 1)
    err_self_simi = torch.zeros(len(labels), J - 1, J - 1)

    for i_lb, (Rx, lb, color) in enumerate(zip(Rxs, labels, COLORS)):

        if 'envelope' not in Rx.descri.c_type.values:
            continue

        model_type = 'general'
        if 'b' in Rx.descri.columns:
            model_type = 'covreduced'

        if self_simi_bar and model_type == 'covreduced':
            raise ValueError(
                "Impossible to output self-similarity error on covreduced model. Use a cov model instead."
            )

        B = Rx.y.shape[0]

        norm2 = sigma2
        if sigma2 is None:
            norm2 = Rx.select(
                rl=1, rr=1, q=2,
                low=False).real.mean(0)[None, :,
                                        0]  # power spectrum averaged on batch
            if Rx.descri['nl'].iloc[0] != Rx.descri['nr'].iloc[0]:
                print(
                    "WARNING. Carefull, sigma2 should be given for left and right independently."
                )

        for (a, b) in product(range(J - 1), range(-J + 1, 0)):
            if a - b >= J:
                continue

            # prepare covariances
            if model_type == "covreduced":
                coeff_ab = Rx.select(c_type='envelope', a=a, b=b, low=False)
                coeff_ab = coeff_ab[:, 0, 0]
                cs[i_lb, a, J - 1 + b] = coeff_ab.mean(0)
            else:
                cs_nj = torch.zeros(B, J + b - a, dtype=Rx.y.dtype)
                for j1 in range(a, J + b):
                    coeff = Rx.select(c_type='envelope',
                                      jl1=j1,
                                      jr1=j1 - a,
                                      j2=j1 - b,
                                      low=False)
                    coeff = coeff[:, 0, 0]
                    coeff /= norm2[:, j1, ...].pow(0.5) * norm2[:, j1 - a,
                                                                ...].pow(0.5)
                    cs_nj[:, j1 - a] = coeff

                cs_j = cs_nj.mean(0)
                cs[i_lb, a, J - 1 + b] = cs_j.mean(0)
                if b == -J + a + 1:
                    err_self_simi[i_lb, a, J - 1 + b] = 0.0
                else:
                    err_self_simi[i_lb, a, J-1+b] = torch.abs(cs_j - cs_j.mean(0, keepdim=True)) \
                        .pow(2.0).sum(0).div(J+b-a-1).pow(0.5)
                # compute estimation error
                if bootstrap:
                    # mean, var = bootstrap_variance_complex(cs_nj.transpose(0, 1), cs_nj.shape[0], 20000)
                    mean, var = bootstrap_variance_complex(
                        cs_nj.mean(1), cs_nj.shape[0], 20000)
                    err_estim[i_lb, a, J - 1 + b] = var.pow(0.5)
                else:
                    err_estim[i_lb, a, J - 1 +
                              b] = (torch.abs(cs_nj).pow(2.0).mean(0) -
                                    torch.abs(cs_nj.mean(0)).pow(2.0)) / (B -
                                                                          1)

    cs, cs_mod, cs_arg = cs.numpy(), np.abs(cs.numpy()), np.angle(cs.numpy())
    err_self_simi, err_estim = to_numpy(err_self_simi), to_numpy(err_estim)
    err_self_simi_arg, err_estim_arg = error_arg(cs_mod,
                                                 err_self_simi), error_arg(
                                                     cs_mod, err_estim)

    # power spectrum normalization
    bs = np.arange(-J + 1, 0)[None, :] * d
    cs_mod /= (2.0**bs)
    err_self_simi /= (2.0**bs)
    err_estim /= (2.0**bs)

    # phase instability at z=0
    for z_arg in [cs_arg, err_self_simi_arg, err_estim_arg]:
        z_arg[cs_mod < theta_threshold] = 0.0

    def plot_modulus(label, y, y_err_estim, y_err_self_simi, title):
        for a in range(J - 1):
            bs = np.arange(-J + 1 + a, 0)
            line = plt.plot(bs, y[a, a:], label=label if a == 0 else '')
            color = line[-1].get_color()
            if not estim_bar and not self_simi_bar:
                plt.scatter(bs, y[a, a:], marker='+')
            if self_simi_bar:
                plot_x_offset = -0.07 if self_simi_bar else 0.0
                plt.errorbar(bs + plot_x_offset,
                             y[a, a:],
                             yerr=y_err_self_simi[a, a:],
                             capsize=4,
                             color=color,
                             fmt=' ')
            if estim_bar:
                plot_x_offset = 0.07 if self_simi_bar else 0.0
                eb = plt.errorbar(bs + plot_x_offset,
                                  y[a, a:],
                                  yerr=y_err_estim[a, a:],
                                  capsize=4,
                                  color=color,
                                  fmt=' ')
                eb[-1][0].set_linestyle('--')
        plt.axhline(0.0, linewidth=0.7, color='black')
        plt.xticks(np.arange(-J + 1, 0), [(rf'${b}$' if b % 2 == 1 else '')
                                          for b in np.arange(-J + 1, 0)],
                   fontsize=fontsize)
        plt.xlabel(r'$b$', fontsize=fontsize)
        plt.ylim(-0.02, ylim)
        plt.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))
        plt.yticks(fontsize=fontsize)
        plt.locator_params(axis='x', nbins=J - 1)
        plt.locator_params(axis='y', nbins=5)
        if title:
            plt.title('Cross-spectrum' + '\n' + r'$|\Phi_4|$',
                      fontsize=fontsize)
        if label != '':
            plt.legend(prop={'size': 15})

    def plot_phase(y, y_err_estim, y_err_self_simi, title):
        for a in range(J - 1):
            bs = np.arange(-J + 1 + a, 0)
            line = plt.plot(bs, y[a, a:], label=fr'$a={a}$')
            color = line[-1].get_color()
            if not estim_bar and not self_simi_bar:
                plt.scatter(bs, y[a, a:], marker='+')
            if self_simi_bar:
                plot_x_offset = -0.07 if estim_bar else 0.0
                plt.errorbar(bs + plot_x_offset,
                             y[a, a:],
                             yerr=y_err_self_simi[a, a:],
                             capsize=4,
                             color=color,
                             fmt=' ')
            if estim_bar:
                plot_x_offset = 0.07 if self_simi_bar else 0.0
                eb = plt.errorbar(bs + plot_x_offset,
                                  y[a, a:],
                                  yerr=y_err_estim[a, a:],
                                  capsize=4,
                                  color=color,
                                  fmt=' ')
                eb[-1][0].set_linestyle('--')
        plt.xticks(np.arange(-J + 1, 0), [(rf'${b}$' if b % 2 == 1 else '')
                                          for b in np.arange(-J + 1, 0)],
                   fontsize=fontsize)
        plt.yticks(np.arange(-2, 3) * np.pi / 8, [
            r'$-\frac{\pi}{4}$', r'$-\frac{\pi}{8}$', r'$0$',
            r'$\frac{\pi}{8}$', r'$\frac{\pi}{4}$'
        ],
                   fontsize=fontsize)
        plt.axhline(0.0, linewidth=0.7, color='black')
        plt.xlabel(r'$b$', fontsize=fontsize)
        if title:
            plt.title('Cross-spectrum' + '\n' + r'Arg$\Phi_4$',
                      fontsize=fontsize)

    if axes is None:
        plt.figure(figsize=(max(len(labels), 5) * 3, 10))
    for i_lb, lb in enumerate(labels):
        if axes is not None:
            plt.sca(axes[0, i_lb])
            ax_mod = axes[0, i_lb]
        else:
            ax_mod = plt.subplot2grid((2, np.unique(i_graphs).size),
                                      (0, i_graphs[i_lb]))
        ax_mod.yaxis.set_tick_params(which='major',
                                     direction='in',
                                     width=1.5,
                                     length=7)
        ax_mod.yaxis.set_label_coords(-0.18, 0.5)
        plot_modulus(lb, cs_mod[i_lb], err_estim[i_lb], err_self_simi[i_lb],
                     i_lb == 0)

    for i_lb, lb in enumerate(labels):
        if axes is not None:
            plt.sca(axes[1, i_lb])
            ax_ph = axes[1, i_lb]
        else:
            ax_ph = plt.subplot2grid((2, np.unique(i_graphs).size),
                                     (1, i_graphs[i_lb]))
        plot_phase(cs_arg[i_lb], err_estim_arg[i_lb], err_self_simi_arg[i_lb],
                   i_lb == 0)
        if i_lb == 0:
            ax_ph.yaxis.set_tick_params(which='major',
                                        direction='in',
                                        width=1.5,
                                        length=7)

    if axes is None:
        plt.tight_layout()
        leg = plt.legend(loc='upper center',
                         ncol=1,
                         fontsize=35,
                         handlelength=1.0,
                         labelspacing=1.0,
                         bbox_to_anchor=(1.3, 2.25, 0, 0))
        for legobj in leg.legendHandles:
            legobj.set_linewidth(5.0)


def plot_dashboard(Rxs,
                   estim_bar=False,
                   self_simi_bar=False,
                   bootstrap=True,
                   theta_threshold=None,
                   sigma2=None,
                   labels=None,
                   linewidth=3.0,
                   fontsize=20,
                   ylim_phase=0.1,
                   ylim_modulus=3.0,
                   figsize=None,
                   axes=None):
    """ Plot the scattering covariance dashboard for multi-scale processes composed of:
        - (wavelet power spectrum) sigma^2(j)
        - (sparsity factors) s^2(j)
        - (phase-envelope cross-spectrum) C_{W|W|}(a) as two graphs : |C_{W|W|}| and Arg(C_{W|W|})
        - (scattering cross-spectrum) C_S(a,b) as two graphs : |C_S| and Arg(C_S)

    :param Rxs: DescribedTensor or list of DescribedTensor
    :param estim_bar: display estimation error due to estimation on several realizations
    :param self_simi_bar: display self-similarity error, it is a measure of scale regularity
    :param bootstrap: time variance computation method
    :param theta_threshold: rules phase instability
    :param sigma2: override normalization factors
    :param labels: list of labels for each model output
    :param linewidth: lines linewidth
    :param fontsize: labels fontsize
    :param ylim_phase: graph ylim for the phase
    :param ylim_modulus: graph ylim for the modulus
    :param figsize: figure size
    :param axes: custom array of axes, should be of shape (2, 2 + nb of representation to plot)
    :return:
    """
    if theta_threshold is None:
        theta_threshold = [0.005, 0.1]
    if isinstance(Rxs, DescribedTensor):
        Rxs = [Rxs]
    for Rx in Rxs:
        if 'nl' not in Rx.descri.columns:
            Rx.descri = Description(
                Model.make_description_compatible(Rx.descri))
        ns_unique = Rx.descri[['nl', 'nr']].dropna().drop_duplicates()
        if ns_unique.shape[0] > 1:
            raise ValueError(
                "Plotting functions do not support multi-variate representation other than "
                "univariate or single pair.")

    if axes is None:
        _, axes = plt.subplots(2,
                               2 + len(Rxs),
                               figsize=figsize or (12 + 2 * (len(Rxs) - 1), 8))

    # marginal moments sigma^2 and s^2
    plot_marginal_moments(Rxs, estim_bar, axes[:, 0], labels, linewidth,
                          fontsize)

    # phase-envelope cross-spectrum
    plot_phase_envelope_spectrum(Rxs, estim_bar, self_simi_bar,
                                 theta_threshold[0], sigma2, axes[:, 1],
                                 labels, fontsize, False, ylim_phase)

    # scattering cross spectrum
    plot_scattering_spectrum(Rxs, estim_bar, self_simi_bar, bootstrap,
                             theta_threshold[1], sigma2, axes[:, 2:], labels,
                             fontsize, ylim_modulus)

    plt.tight_layout()


if __name__ == "__main__":
    # more realistic synthetic data
    T = 2**11
    N_per_window = 3
    new_background_noise = False
    random_exponent = True

    dtld = MRWLoader()

    if new_background_noise:
        dtld.erase(R=1, n_files=1, T=T + 1, H=0.5, lam=0.25)
    n_all = dtld.load(R=1 + 100, n_files=1, T=T + 1, H=0.5, lam=0.25).x
    n_all = format_np(np.diff(n_all))

    # background noise
    n = n_all[:1, :, :]

    # realizations of background noise
    nks = n_all[1:, :, :]

    if random_exponent:

        def aux(x):
            return 1 - 4 * x * (1 - x)

        a_left, a_right = 0.1 * aux(np.random.random(2)) + 0.01
    else:
        a_left, a_right = 0.1, 0.1

    def get_glitch(nsize, rd_mid=False):
        glitch_left = a_left * np.exp(-a_left * np.arange(T // 2))
        glitch_right = a_right * np.exp(-a_right * np.arange(T // 2))
        glitch = np.concatenate([glitch_left[::-1], glitch_right])
        shift = np.random.randint(nsize)
        return np.roll(glitch, shift)

    def get_glitch_signal(nsize):
        glitch = np.zeros(nsize)
        for n in range(N_per_window):
            glitch += get_glitch(nsize, True)
        return 2 * glitch

    g = get_glitch_signal(T)[None, None, :]
    x_init = n + g

    # now adds an independency condition
    J = 8

    deglitching_params = {
        'nks': format_tensor(nks[:20, :, :]),
        'x_init': format_tensor(x_init),
        'indep_loss_w': False,
        'x_loss_w': False,
        'fixed_ts': None,
        'cuda': True
    }

    nt = generate(x_init,
                  x0=x_init,
                  J=J,
                  it=100,
                  tol_optim=0.1,
                  deglitching_params=deglitching_params,
                  exp_name="deglitch_test7",
                  nchunks=3,
                  cuda=True)

    print()
