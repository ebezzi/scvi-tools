from typing import Union, Tuple

import torch
from torch.distributions import (
    constraints,
    Distribution,
    Gamma,
    Poisson,
)
from torch.distributions.utils import (
    broadcast_all,
    probs_to_logits,
    lazy_property,
    logits_to_probs,
)

from scvi.models.log_likelihood import log_nb_positive, log_zinb_positive


def from_nb_params2_to_1(mu, theta, eps=1e-6):
    r"""
        :param mu: mean of the NB distribution.
        :param theta: overdispersion
        :param eps: constant used for numerical log stability
        Returns the number of failures until the experiment is stopped
            and the success probability.
    """
    assert (mu is None) == (
        theta is None
    ), "If using the mu/theta NB parameterization, both parameters must be specified"
    logits = (mu + eps).log() - (theta + eps).log()
    total_count = theta
    return total_count, logits


def from_nb_params1_to_2(total_count, logits):
    """
        :param total_count: Number of failures until the experiment is stopped.
        :param logits: success logits
        Returns the mean and overdispersion of the NB distribution.
    """
    theta = total_count
    mu = logits.exp() * theta
    return mu, theta


class NB(Distribution):
    r"""
        Negative Binomial distribution using two parameterizations:
        - The first one corresponds to the parameterization NB(`total_count`, `probs`)
            where `total_count` is the number of failures until the experiment is stopped
            and `probs` the success probability.
        - The (`mu`, `theta`) parameterization is the one used by scVI. These parameters respectively
        control the mean and overdispersion of the distribution.

        `from_nb_params2_to_1` and `from_nb_params1_to_2` provide ways to convert one parameterization to another.

    """
    arg_constraints = {
        "mu": constraints.greater_than_eq(0),
        "theta": constraints.greater_than_eq(0),
    }
    support = constraints.nonnegative_integer

    def __init__(
        self,
        total_count: torch.Tensor = None,
        probs: torch.Tensor = None,
        logits: torch.Tensor = None,
        mu: torch.Tensor = None,
        theta: torch.Tensor = None,
        validate_args=True,
    ):
        self._eps = 1e-8
        if (mu is None) == (total_count is None):
            raise ValueError(
                "Please use one of the two possible parameterizations. Refer to the documentation for more information."
            )

        using_param_1 = total_count is not None and (
            logits is not None or probs is not None
        )
        if using_param_1:
            logits = logits if logits is not None else probs_to_logits(probs)
            total_count = total_count.type_as(logits)
            total_count, logits = broadcast_all(total_count, logits)
            mu, theta = from_nb_params1_to_2(total_count, logits)
        else:
            mu, theta = broadcast_all(mu, theta)
        self.mu = mu
        self.theta = theta
        super().__init__(validate_args=validate_args)

    def sample(self, sample_shape=torch.Size()):
        gamma_d = self._gamma()
        p_means = gamma_d.sample(sample_shape)

        # Clamping as distributions objects can have buggy behaviors when
        # their parameters are too high
        l_train = torch.clamp(p_means, max=1e8)
        counts = Poisson(
            l_train
        ).sample()  # Shape : (n_samples, n_cells_batch, n_genes)
        return counts

    def log_prob(self, value):
        if self._validate_args:
            self._validate_sample(value)
        return log_nb_positive(value, mu=self.mu, theta=self.theta, eps=self._eps)

    def _gamma(self):
        concentration = self.theta
        rate = self.theta / self.mu
        # Important remark: Gamma is parametrized by the rate = 1/scale!
        gamma_d = Gamma(concentration=concentration, rate=rate)
        return gamma_d


class ZINB(NB):
    r"""
        Zero Inflated Negative Binomial distribution.
        zi_logits correspond to the zero-inflation logits
mu + mu ** 2 / theta
        The negative binomial component parameters can follow two two parameterizations:
        - The first one corresponds to the parameterization NB(`total_count`, `probs`)
            where `total_count` is the number of failures until the experiment is stopped
            and `probs` the success probability.
        - The (`mu`, `theta`) parameterization is the one used by scVI. These parameters respectively
        control the mean and overdispersion of the distribution.

        `from_nb_params2_to_1` and `from_nb_params1_to_2` provide ways to convert one parameterization to another.
    """
    arg_constraints = {
        "mu": constraints.greater_than_eq(0),
        "theta": constraints.greater_than_eq(0),
        "zi_probs": constraints.half_open_interval(0.0, 1.0),
        "zi_logits": constraints.real,
    }
    support = constraints.nonnegative_integer

    def __init__(
        self,
        total_count: torch.Tensor = None,
        probs: torch.Tensor = None,
        logits: torch.Tensor = None,
        mu: torch.Tensor = None,
        theta: torch.Tensor = None,
        zi_logits: torch.Tensor = None,
        validate_args=True,
    ):

        super().__init__(
            total_count=total_count,
            probs=probs,
            logits=logits,
            mu=mu,
            theta=theta,
            validate_args=validate_args,
        )
        self.zi_logits, self.mu, self.theta = broadcast_all(
            zi_logits, self.mu, self.theta
        )

    @lazy_property
    def zi_logits(self) -> torch.Tensor:
        return probs_to_logits(self.zi_probs, is_binary=True)

    @lazy_property
    def zi_probs(self) -> torch.Tensor:
        return logits_to_probs(self.zi_logits, is_binary=True)

    def sample(
        self, sample_shape: Union[torch.Size, Tuple] = torch.Size()
    ) -> torch.Tensor:
        with torch.no_grad():
            samp = super().sample(sample_shape=sample_shape)
            is_zero = torch.rand_like(samp) <= self.zi_probs
            samp[is_zero] = 0.0
            return samp

    def log_prob(self, value: torch.Tensor) -> torch.Tensor:
        if self._validate_args:
            self._validate_sample(value)
        return log_zinb_positive(value, self.mu, self.theta, self.zi_logits, eps=1e-08)
