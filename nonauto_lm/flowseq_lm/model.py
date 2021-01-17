from typing import List, Tuple, Dict, Type, T
import torch
from overrides import overrides
import nonauto_lm.nn.utils as util
from torch_nlp_utils.data import Vocabulary
from nonauto_lm.base import NonAutoLmModel, PriorSample, PosteriorSample, Embedder, LatentSample
# Modules
from .priors import Prior
from .posteriors import Posterior
from .encoders import Encoder
from .decoders import Decoder


@NonAutoLmModel.register("flow")
class FlowModel(NonAutoLmModel):
    def __init__(
        self,
        vocab: Vocabulary,
        embedder: Embedder,
        encoder: Encoder,
        decoder: Decoder,
        posterior: Posterior,
        prior: Prior,
        num_samples_from_posterior: int = 1,
        no_kl_steps: int = 2000,
        kl_annealing_steps: int = 10000,
        label_smoothing: float = 0.0,
    ) -> None:
        super().__init__(
            vocab=vocab,
            num_samples_from_posterior=num_samples_from_posterior,
            no_kl_steps=no_kl_steps,
            kl_annealing_steps=kl_annealing_steps,
            label_smoothing=label_smoothing,
        )
        self._embedder = embedder
        self._encoder = encoder
        self._decoder = decoder
        self._posterior = posterior
        self._prior = prior
        # Vocab projection
        self._vocab_projection = torch.nn.Linear(
            self._decoder.get_output_size(),
            vocab.get_vocab_size(namespace="target"),
        )

    @overrides
    def encode(self, tokens: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Encode `tokens`.

        Returns
        -------
        `Tuple[torch.Tensor, torch.Tensor]`
            Encoded tokens and mask for them.
        """
        embedded_tokens = self._embedder(tokens)
        mask = util.get_tokens_mask(tokens)
        return self._encoder(embedded_tokens, mask)

    @overrides
    def decode(
        self, z: torch.Tensor, mask: torch.Tensor, target: torch.Tensor = None
    ) -> Dict[str, torch.Tensor]:
        """
        Decode sequence from z and mask.

        Parameters
        ----------
        z : `torch.Tensor`, required
            Latent codes.
        mask : `torch.Tensor`, required
            Mask for latent codes
        target : `torch.Tensor`, optional (default = `None`)
            Target sequence if passed in function computes loss.

        Returns
        -------
        `Dict[str, torch.Tensor]`
            logits : `torch.Tensor`
                Logits after decoding.
            probs : `torch.Tensor`
                Softmaxed logits.
            preds : `torch.Tensor`
                Predicted tokens.
            loss : `torch.Tensor`, optional
                Reconstruction error if target is passed.
        """
        # output_dict ~ logits, probs, preds
        logits = self._vocab_projection(self._decoder(z, mask))
        output_dict = {"logits": logits, "probs": torch.softmax(logits, dim=-1)}
        output_dict["preds"] = torch.argmax(output_dict["probs"], dim=-1)
        # Get padding mask
        weights = util.get_tokens_mask(target).float()
        loss = self._loss(output_dict["logits"], target, weights=weights)
        output_dict["loss"] = loss
        return output_dict

    @overrides
    def sample_from_prior(
        self, samples: int, lengths: List[int]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Sample latent codes from prior distirbution.

        Parameters
        ----------
        samples : `int`, required
            Number of samples to gather.
        lengths : `List[int]`, required
            Lengths of each sample.

        Returns
        -------
        `PriorSample`
            z : `torch.Tensor`
                Sampled latent codes.
            log_prob : `torch.Tensor`
                Log probability for sample.
            mask : `torch.Tensor`
                Mask for sampled tensor.
        """
        z, mask = self._prior.sample(samples, lengths)
        z, log_prob = self._posterior.backward(z, mask=mask)
        return PriorSample(z, log_prob, mask)

    @overrides
    def _sample_from_posterior(
        self,
        encoded: torch.Tensor,
        mask: torch.Tensor,
        random: bool = True
    ) -> PosteriorSample:
        """
        Sample latent codes from posterior distribution.

        Parameters
        ----------
        encoded : `torch.Tensor`, required
            Encoded source sequence.
        mask : `torch.Tensor`, required
            Mask for encoded source sequence.
        random : `bool`, optional (default = `True`)
            Whether to add randomness or not.

        Returns
        -------
        `PosteriorSample`
            latent : `torch.Tensor`
                Sampled latent codes.
            log_prob : `torch.Tensor`
                Log probability for sample.
        """
        posterior_sample = self._posterior(encoded, mask, self._nsamples_posterior, random=random)
        return PosteriorSample(*posterior_sample)

    @overrides
    def _get_prior_log_prob(self, z: LatentSample, mask: torch.Tensor) -> torch.Tensor:
        """Get Log Probability of Prior Distribution based on `z` and its `mask`."""
        return self._prior.log_probability(z, mask)

    @classmethod
    def from_params(cls: Type[T], vocab: Vocabulary, **params) -> T:
        embedder = Embedder.from_params(vocab=vocab, **params.pop("embedder"))
        encoder = Encoder.from_params(**params.pop("encoder"))
        decoder = Decoder.from_params(**params.pop("decoder"))
        posterior = Posterior.from_params(**params.pop("posterior"))
        prior = Prior.from_params(**params.pop("prior"))
        return cls(
            vocab=vocab,
            embedder=embedder,
            encoder=encoder,
            decoder=decoder,
            posterior=posterior,
            prior=prior,
            **params
        )
