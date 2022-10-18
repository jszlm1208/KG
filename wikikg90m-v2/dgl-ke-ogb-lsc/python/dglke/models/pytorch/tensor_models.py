# -*- coding: utf-8 -*-
#
# tensor_models.py
#
# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
KG Sparse embedding
"""
import math
import os
import numpy as np

import torch
import torch as th
import torch.nn as nn
import torch.nn.functional as functional
import torch.nn.init as INIT

import torch.multiprocessing as mp
from torch.multiprocessing import Queue
from _thread import start_new_thread
import traceback
from functools import wraps

from .. import *

logsigmoid = functional.logsigmoid


def abs(val):
    return th.abs(val)


def masked_select(input, mask):
    return th.masked_select(input, mask)


def get_dev(gpu):
    return th.device('cpu') if gpu < 0 else th.device('cuda:' + str(gpu))


def get_device(args):
    return th.device('cpu') if args.gpu[0] < 0 else th.device('cuda:' + str(args.gpu[0]))


def none(x): return x
def norm(x, p): return x.norm(p=p)**p
def get_scalar(x): return x.detach().item()
def reshape(arr, x, y): return arr.view(x, y)
def cuda(arr, gpu): return arr.cuda(gpu)


def l2_dist(x, y, pw=False):
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return -th.norm(x-y, p=2, dim=-1)


def l1_dist(x, y, pw=False):
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return -th.norm(x-y, p=1, dim=-1)


def dot_dist(x, y, pw=False):
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return th.sum(x * y, dim=-1)


def cosine_dist(x, y, pw=False):
    score = dot_dist(x, y, pw)

    x = x.norm(p=2, dim=-1)
    y = y.norm(p=2, dim=-1)
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return score / (x * y)


def extended_jaccard_dist(x, y, pw=False):
    score = dot_dist(x, y, pw)

    x = x.norm(p=2, dim=-1)**2
    y = y.norm(p=2, dim=-1)**2
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return score / (x + y - score)


def floor_divide(input, other):
    return th.floor_divide(input, other)


def thread_wrapped_func(func):
    """Wrapped func for torch.multiprocessing.Process.

    With this wrapper we can use OMP threads in subprocesses
    otherwise, OMP_NUM_THREADS=1 is mandatory.

    How to use:
    @thread_wrapped_func
    def func_to_wrap(args ...):
    """
    @wraps(func)
    def decorated_function(*args, **kwargs):
        queue = Queue()
        def _queue_result():
            exception, trace, res = None, None, None
            try:
                res = func(*args, **kwargs)
            except Exception as e:
                exception = e
                trace = traceback.format_exc()
            queue.put((res, exception, trace))

        start_new_thread(_queue_result, ())
        result, exception, trace = queue.get()
        if exception is None:
            return result
        else:
            assert isinstance(exception, Exception)
            raise exception.__class__(trace)
    return decorated_function


@thread_wrapped_func
def async_update(args, emb, queue):
    """Asynchronous embedding update for entity embeddings.
    How it works:
        1. trainer process push entity embedding update requests into the queue.
        2. async_update process pull requests from the queue, calculate
           the gradient state and gradient and write it into entity embeddings.

    Parameters
    ----------
    args :
        Global confis.
    emb : ExternalEmbedding
        The entity embeddings.
    queue:
        The request queue.
    """
    th.set_num_threads(args.num_thread)
    while True:
        (grad_indices, grad_values, gpu_id) = queue.get()
        clr = emb.args.lr
        if grad_indices is None:
            return
        with th.no_grad():
            grad_sum = (grad_values * grad_values).mean(1)
            device = emb.state_sum.device
            if device != grad_indices.device:
                grad_indices = grad_indices.to(device)
            if device != grad_sum.device:
                grad_sum = grad_sum.to(device)

            emb.state_sum.index_add_(0, grad_indices, grad_sum)
            std = emb.state_sum[grad_indices]  # _sparse_mask
            if gpu_id >= 0:
                std = std.cuda(gpu_id)
            std_values = std.sqrt_().add_(1e-10).unsqueeze(1)
            tmp = (-clr * grad_values / std_values)
            if tmp.device != device:
                tmp = tmp.to(device)
            emb.emb.index_add_(0, grad_indices, tmp)


class InferEmbedding:
    def __init__(self, device):
        self.device = device

    def load(self, path, name):
        """Load embeddings.

        Parameters
        ----------
        path : str
            Directory to load the embedding.
        name : str
            Embedding name.
        """
        file_name = os.path.join(path, name+'.npy')
        self.emb = th.Tensor(np.load(file_name))

    def load_emb(self, emb_array):
        """Load embeddings from numpy array.

        Parameters
        ----------
        emb_array : numpy.array  or torch.tensor
            Embedding array in numpy array or torch.tensor
        """
        if isinstance(emb_array, np.ndarray):
            self.emb = th.Tensor(emb_array)
        else:
            self.emb = emb_array

    def __call__(self, idx):
        return self.emb[idx].to(self.device)


class ExternalEmbedding:
    """Sparse Embedding for Knowledge Graph
    It is used to store both entity embeddings and relation embeddings.

    Parameters
    ----------
    args :
        Global configs.
    num : int
        Number of embeddings.
    dim : int
        Embedding dimention size.
    device : th.device
        Device to store the embedding.
    """

    def __init__(self, args, num, dim, device, is_feat=False):
        self.gpu = args.gpu
        self.args = args
        self.num = num
        self.trace = []
        self.is_feat = is_feat
        if not is_feat:
            self.emb = th.empty(num, dim, dtype=th.float32, device=device)
            self.state_sum = self.emb.new().resize_(self.emb.size(0)).zero_()
        else:
            self.emb = None
            self.state_sum = None
        self.state_step = 0
        self.has_cross_rel = False
        # queue used by asynchronous update
        self.async_q = None
        # asynchronous update process
        self.async_p = None
        self.idx_all = th.arange(num, device=device)

    def init(self, emb_init):
        """Initializing the embeddings.

        Parameters
        ----------
        emb_init : float
            The intial embedding range should be [-emb_init, emb_init].
        """
        INIT.uniform_(self.emb, -emb_init, emb_init)
        INIT.zeros_(self.state_sum)

    def setup_cross_rels(self, cross_rels, global_emb):
        cpu_bitmap = th.zeros((self.num,), dtype=th.bool)
        for i, rel in enumerate(cross_rels):
            cpu_bitmap[rel] = 1
        self.cpu_bitmap = cpu_bitmap
        self.has_cross_rel = True
        self.global_emb = global_emb

    def get_noncross_idx(self, idx):
        cpu_mask = self.cpu_bitmap[idx]
        gpu_mask = ~cpu_mask
        return idx[gpu_mask]

    def share_memory(self):
        """Use torch.tensor.share_memory_() to allow cross process tensor access
        """
        if not self.is_feat:
            self.emb.share_memory_()
            self.state_sum.share_memory_()

    def __call__(self, idx, gpu_id=-1, trace=True):
        """ Return sliced tensor.

        Parameters
        ----------
        idx : th.tensor
            Slicing index
        gpu_id : int
            Which gpu to put sliced data in.
        trace : bool
            If True, trace the computation. This is required in training.
            If False, do not trace the computation.
            Default: True
        """
        if idx is None:
            idx = self.idx_all
        if self.is_feat:
            assert not trace
        if self.has_cross_rel:
            cpu_idx = idx.cpu()
            cpu_mask = self.cpu_bitmap[cpu_idx]
            cpu_idx = cpu_idx[cpu_mask]
            cpu_idx = th.unique(cpu_idx)
            if cpu_idx.shape[0] != 0:
                cpu_emb = self.global_emb.emb[cpu_idx]
                self.emb[cpu_idx] = cpu_emb.cuda(gpu_id)
        if self.is_feat:
            assert not trace
            s = th.from_numpy(self.emb[idx.numpy()]).to(th.float)
        else:
            s = self.emb[idx]

        if gpu_id >= 0:
            s = s.cuda(gpu_id)
        # During the training, we need to trace the computation.
        # In this case, we need to record the computation path and compute the gradients.
        if trace:
            data = s.clone().detach().requires_grad_(True)
            self.trace.append((idx, data))
        else:
            data = s
        return data

    def update(self, gpu_id=-1):
        """ Update embeddings in a sparse manner
        Sparse embeddings are updated in mini batches. we maintains gradient states for
        each embedding so they can be updated separately.

        Parameters
        ----------
        gpu_id : int
            Which gpu to accelerate the calculation. if -1 is provided, cpu is used.
        """
        self.state_step += 1
        with th.no_grad():
            for idx, data in self.trace:
                grad = data.grad.data

                clr = self.args.lr
                #clr = self.args.lr / (1 + (self.state_step - 1) * group['lr_decay'])

                # the update is non-linear so indices must be unique
                grad_indices = idx
                grad_values = grad
                if self.async_q is not None:
                    grad_indices.share_memory_()
                    grad_values.share_memory_()
                    self.async_q.put((grad_indices, grad_values, gpu_id))
                else:
                    grad_sum = (grad_values * grad_values).mean(1)
                    device = self.state_sum.device
                    if device != grad_indices.device:
                        grad_indices = grad_indices.to(device)
                    if device != grad_sum.device:
                        grad_sum = grad_sum.to(device)

                    if self.has_cross_rel:
                        cpu_mask = self.cpu_bitmap[grad_indices]
                        cpu_idx = grad_indices[cpu_mask]
                        if cpu_idx.shape[0] > 0:
                            cpu_grad = grad_values[cpu_mask]
                            cpu_sum = grad_sum[cpu_mask].cpu()
                            cpu_idx = cpu_idx.cpu()
                            self.global_emb.state_sum.index_add_(0, cpu_idx, cpu_sum)
                            std = self.global_emb.state_sum[cpu_idx]
                            if gpu_id >= 0:
                                std = std.cuda(gpu_id)
                            std_values = std.sqrt_().add_(1e-10).unsqueeze(1)
                            tmp = (-clr * cpu_grad / std_values)
                            tmp = tmp.cpu()
                            self.global_emb.emb.index_add_(0, cpu_idx, tmp)
                    self.state_sum.index_add_(0, grad_indices, grad_sum)
                    std = self.state_sum[grad_indices]  # _sparse_mask
                    if gpu_id >= 0:
                        std = std.cuda(gpu_id)
                    std_values = std.sqrt_().add_(1e-10).unsqueeze(1)
                    tmp = (-clr * grad_values / std_values)
                    if tmp.device != device:
                        tmp = tmp.to(device)
                    # TODO(zhengda) the overhead is here.
                    self.emb.index_add_(0, grad_indices, tmp)
        self.trace = []

    def create_async_update(self):
        """Set up the async update subprocess.
        """
        self.async_q = Queue(1)
        self.async_p = mp.Process(target=async_update, args=(self.args, self, self.async_q))
        self.async_p.start()

    def finish_async_update(self):
        """Notify the async update subprocess to quit.
        """
        self.async_q.put((None, None, None))
        self.async_p.join()

    def curr_emb(self):
        """Return embeddings in trace.
        """
        data = [data for _, data in self.trace]
        return th.cat(data, 0)

    def save(self, path, name):
        """Save embeddings.

        Parameters
        ----------
        path : str
            Directory to save the embedding.
        name : str
            Embedding name.
        """
        file_name = os.path.join(path, name+'.npy')
        np.save(file_name, self.emb.cpu().detach().numpy())

    def load(self, path, name):
        """Load embeddings.

        Parameters
        ----------
        path : str
            Directory to load the embedding.
        name : str
            Embedding name.
        """
        file_name = os.path.join(path, name+'.npy')
        self.emb = th.Tensor(np.load(file_name))

class RelationExternalEmbedding(ExternalEmbedding):
    """Sparse Embedding for Knowledge Graph
    It is used to store both entity embeddings and relation embeddings.

    Parameters
    ----------
    args :
        Global configs.
    num : int
        Number of embeddings.
    dim : int
        Embedding dimention size.
    device : th.device
        Device to store the embedding.
    """

    def __init__(self, args, num, dim, device, is_feat=False):
        super(RelationExternalEmbedding, self).__init__(args, num, dim, device,
                                                        is_feat)
        self.ote_size = args.ote_size
        self.scale_type = args.scale_type
        self.use_scale = True if args.scale_type > 0 else False
        self.dim = dim
        self.final_dim = self.dim * (int(self.use_scale) + args.ote_size)
        self.num = num
        self.emb = th.empty(
            num, self.final_dim, dtype=th.float32, device=device)

    def scale_init(self):
        if self.scale_type == 1:
            return 1.0
        if self.scale_type == 2:
            return 0.0
        raise ValueError("Scale Type %d is not supported!" % self.scale_type)

    def orth_embedding(self, embeddings, eps=1e-18, do_test=True):
        #orthogonormalizing embeddings
        #embeddings: num_emb X ote_size X (num_elem + (1 or 0))
        num_emb = embeddings.size(0)
        assert embeddings.size(1) == self.ote_size
        assert embeddings.size(2) == (self.ote_size +
                                      (1 if self.use_scale else 0))
        if self.use_scale:
            emb_scale = embeddings[:, :, -1]
            embeddings = embeddings[:, :, :self.ote_size]

        u = [embeddings[:, 0]]
        uu = [0] * self.ote_size
        uu[0] = (u[0] * u[0]).sum(dim=-1)
        if do_test and (uu[0] < eps).sum() > 1:
            return None
        u_d = embeddings[:, 1:]
        for i in range(1, self.ote_size):
            u_d = u_d - u[-1].unsqueeze(dim=1) * (
                (embeddings[:, i:] * u[i - 1].unsqueeze(dim=1)).sum(
                    dim=-1) / uu[i - 1].unsqueeze(dim=1)).unsqueeze(-1)
            u_i = u_d[:, 0]
            u_d = u_d[:, 1:]
            uu[i] = (u_i * u_i).sum(dim=-1)
            if do_test and (uu[i] < eps).sum() > 1:
                return None
            u.append(u_i)

        u = torch.stack(u, dim=1)  #num_emb X ote_size X num_elem
        u_norm = u.norm(dim=-1, keepdim=True)
        u = u / u_norm
        if self.use_scale:
            u = torch.cat((u, emb_scale.unsqueeze(-1)), dim=-1)
        return u

    def orth_rel_embedding(self):
        rel_emb_size = self.emb.size()
        ote_size = self.ote_size
        scale_dim = 1 if self.use_scale else 0
        rel_embedding = self.emb.view(-1, ote_size, ote_size + scale_dim)
        rel_embedding = self.orth_embedding(rel_embedding).view(rel_emb_size)
        return rel_embedding

    def init(self, emb_init):
        """Initializing the embeddings.

        Parameters
        ----------
        emb_init : float
            The intial embedding range should be [-emb_init, emb_init].
        """
        init_range = 1. / math.sqrt(self.final_dim)
        INIT.uniform_(self.emb, a=-init_range, b=init_range)
        if self.use_scale:
            self.emb.data.view(
                -1, self.ote_size +
                1)[:, -1] = self.scale_init()  #start with no scale
        rel_emb_data = self.orth_rel_embedding()
        self.emb.data.copy_(
            rel_emb_data.view(-1, self.dim * (int(self.use_scale) +
                                              self.ote_size)))

