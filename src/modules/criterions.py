import torch
import torch.nn as nn
from torch.autograd import Variable
from src.utils import Vocab

# Loss compute
def filter_shard_state(state):
    for k, v in state.items():
        if v is not None:
            if isinstance(v, Variable) and v.requires_grad:
                v = Variable(v.data, requires_grad=True, volatile=False)
        yield k, v

def shards(state, shard_size, eval=False, batch_dim=0):
    """
    Args:
        state: A dictionary which corresponds to the output of
               *LossCompute._make_shard_state(). The values for
               those keys are Tensor-like or None.
        shard_size: The maximum size of the shards yielded by the model.
        eval: If True, only yield the state, nothing else.
              Otherwise, yield shards.

    Yields:
        Each yielded shard is a dict.

    Side effect:
        After the last shard, this function does back-propagation.
    """
    if eval:
        yield state
    else:
        # non_none: the subdict of the state dictionary where the values
        # are not None.
        non_none = dict(filter_shard_state(state))

        # Now, the iteration:
        # state is a dictionary of sequences of tensor-like but we
        # want a sequence of dictionaries of tensors.
        # First, unzip the dictionary into a sequence of keys and a
        # sequence of tensor-like sequences.
        keys, values = zip(*((k, map(lambda t: t.contiguous(), torch.split(v, split_size=shard_size, dim=batch_dim)))
                             for k, v in non_none.items()))

        # Now, yield a dictionary for each shard. The keys are always
        # the same. values is a sequence of length #keys where each
        # element is a sequence of length #shards. We want to iterate
        # over the shards, not over the keys: therefore, the values need
        # to be re-zipped by shard and then each shard can be paired
        # with the keys.
        for shard_tensors in zip(*values):
            yield dict(zip(keys, shard_tensors))

        # Assumed backprop'd
        variables = ((state[k], v.grad.data) for k, v in non_none.items()
                     if isinstance(v, Variable) and v.grad is not None)

        inputs, grads = zip(*variables)
        torch.autograd.backward(inputs, grads)


class Critierion(nn.Module):
    """
    Class for managing efficient loss computation. Handles
    sharding next step predictions and accumulating mutiple
    loss computations


    Users can implement their own loss computation strategy by making
    subclass of this one.  Users need to implement the _compute_loss()
    and make_shard_state() methods.

    Args:
        generator (:obj:`nn.Module`) :
             module that maps the output of the decoder to a
             distribution over the target vocabulary.
        tgt_vocab (:obj:`Vocab`) :
             torchtext vocab object representing the target output
        normalzation (str): normalize by "sents" or "tokens"
    """

    def __init__(self):
        super(Critierion, self).__init__()

    def _compute_loss(self, generator, *args, **kwargs):
        """
        Compute the loss. Subclass must define this method.

        Args:
            output: the predict output from the model.
            target: the validate target to compare output with.
            **kwargs(optional): additional info for computing loss.
        """
        raise NotImplementedError

    def shared_compute_loss(self,
                            generator,
                            shard_size,
                            normalization=1.0,
                            eval=False,
                            batch_dim=0, **kwargs):

        # shard_state = self._make_shard_state(**kwargs)
        loss_data = 0.0

        for shard in shards(state=kwargs, shard_size=shard_size, eval=eval, batch_dim=batch_dim):

            loss = self._compute_loss(generator=generator, **shard) # type: Variable
            loss.div(normalization).backward()
            loss_data += loss.data.clone()

        return loss_data / normalization

    def forward(self, generator, shard_size, normalization=1.0, eval=False, batch_dim=0, **kwargs):
        if eval is True or shard_size < 0:
            loss = self._compute_loss(generator, **kwargs).div(normalization)

            if eval is False:
                loss.backward()
                return loss.data.clone()
            else:
                return loss.clone()

        else:
            return self.shared_compute_loss(generator=generator,
                                            shard_size=shard_size,
                                            normalization=normalization,
                                            eval=eval,
                                            batch_dim=batch_dim,
                                            **kwargs)

class NMTCritierion(Critierion):
    """
    TODO:
    1. Add label smoothing
    """
    def __init__(self, num_tokens, padding_idx=Vocab.PAD, label_smoothing=0.0):

        super().__init__()
        self.num_tokens = num_tokens
        self.padding_idx = padding_idx

        if label_smoothing > 0:
            # When label smoothing is turned on,
            # KL-divergence between q_{smoothed ground truth prob.}(w)
            # and p_{prob. computed by model}(w) is minimized.
            # If label smoothing value is set to zero, the loss
            # is equivalent to NLLLoss or CrossEntropyLoss.
            # All non-true labels are uniformly set to low-confidence.
            self.criterion = nn.KLDivLoss(size_average=False)
            one_hot = torch.randn(1, num_tokens)
            one_hot.fill_(label_smoothing / (num_tokens - 2))
            one_hot[0][padding_idx] = 0
            self.register_buffer('one_hot', one_hot)
        else:
            weight = torch.ones(self.num_tokens)
            weight[padding_idx] = 0

            self.criterion = nn.NLLLoss(weight=weight, size_average=False)

        self.confidence = 1.0 - label_smoothing

    def _bottle(self, v):
        return v.view(-1, v.size(2))

    def _compute_loss(self, generator, dec_outs, labels):

        scores = generator(self._bottle(dec_outs)) # [batch_size * seq_len, d_words]

        gtruth = labels.view(-1)

        if self.confidence < 1:
            tdata = gtruth.data
            mask = torch.nonzero(tdata.eq(self.padding_idx)).squeeze()
            log_likelihood = torch.gather(scores.data, 1, tdata.unsqueeze(1))
            tmp_ = self.one_hot.repeat(gtruth.size(0), 1)
            tmp_.scatter_(1, tdata.unsqueeze(1), self.confidence)
            if mask.dim() > 0:
                log_likelihood.index_fill_(0, mask, 0)
                tmp_.index_fill_(0, mask, 0)
            gtruth = Variable(tmp_, requires_grad=False)

        loss = self.criterion(scores, gtruth)

        return loss
