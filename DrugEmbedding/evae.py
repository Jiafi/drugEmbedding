import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.utils.rnn as rnn_utils
from torch.distributions import MultivariateNormal
import numpy as np
from DrugEmbedding.utils import to_cuda_var, pairwise_dist

# reproducibility
#torch.manual_seed(216)
#np.random.seed(216)

class EVAE(nn.Module):

    def __init__(self,
                 hidden_size, latent_size,
                 bidirectional, num_layers,
                 word_dropout_rate,
                 vocab_size, max_sequence_length,
                 sos_idx, eos_idx, pad_idx, unk_idx,
                 prior, alpha, beta, gamma):
        super().__init__()

        self.hidden_size = hidden_size
        self.latent_size = latent_size

        self.bidirectional = bidirectional
        self.num_layers = num_layers
        self.hidden_factor = (2 if bidirectional else 1) * num_layers
        self.word_dropout_rate = word_dropout_rate

        self.vocab_size = vocab_size
        self.max_sequence_length = max_sequence_length
        self.sos_idx = sos_idx
        self.eos_idx = eos_idx
        self.pad_idx = pad_idx
        self.unk_idx = unk_idx

        # define layers
        self.encoder_rnn = nn.GRU(input_size=self.vocab_size, hidden_size=self.hidden_size,
                                  num_layers=self.num_layers, bidirectional=self.bidirectional, batch_first=True)
        self.decoder_rnn = nn.GRU(input_size=self.vocab_size, hidden_size=self.hidden_size,
                                  num_layers=self.num_layers, bidirectional=False, batch_first=True)

        self.hidden2mean = nn.Linear(self.hidden_size*self.hidden_factor, self.latent_size)
        self.hidden2logv = nn.Linear(self.hidden_size*self.hidden_factor, self.latent_size)
        self.latent2hidden = nn.Linear(self.latent_size, self.hidden_size*self.num_layers)
        self.outputs2vocab = nn.Linear(self.hidden_size, self.vocab_size)

        # define prior type
        self.prior = prior

        # marginal KL coefficient alpha * KL(q(z)||p(z))
        self.alpha = alpha
        # conditional KL coefficient beta * KL(q(z|x)||p(z))
        self.beta = beta
        # MMD coefficent gamma * MMD
        self.gamma = gamma

        # define loss function
        self.RECON = torch.nn.NLLLoss(ignore_index=self.pad_idx, reduction='sum')

    def one_hot_embedding(self, input_sequence):
        embeddings = np.zeros((input_sequence.shape[0], input_sequence.shape[1], self.vocab_size), dtype=np.float32)
        for b, batch in enumerate(input_sequence):
            for t, char in enumerate(batch):
                if char.item() != 0:
                    embeddings[b, t, char.item()] = 1
        return to_cuda_var(torch.from_numpy(embeddings))

    def encoder(self, input_sequence, sorted_lengths):
        batch_size = input_sequence.shape[0]
        # create one-hot embeddings
        one_hot_rep = self.one_hot_embedding(input_sequence)
        # packed input
        packed_input = rnn_utils.pack_padded_sequence(one_hot_rep, sorted_lengths.data.tolist(), batch_first=True)
        # pass forward encoder GRU
        _, hidden = self.encoder_rnn(packed_input) # hidden_factor, batch_size, hidden_size

        if self.hidden_factor > 1:
            hidden = hidden.permute(1, 0, 2).reshape(batch_size, self.hidden_size * self.hidden_factor).contiguous() # batch_size, hidden_size * hidden_factor
        else:
            hidden = hidden.squeeze(0)
        return hidden

    def reparameterize(self, hidden):
        # mean vector
        mean_z = self.hidden2mean(hidden)
        # logvar vector
        logv = self.hidden2logv(hidden)
        std = torch.exp(0.5 * logv)
        eps = to_cuda_var(torch.randn([mean_z.shape[0], self.latent_size]))
        z = mean_z + eps * std
        return mean_z, logv, z

    def decoder(self, input_sequence, sorted_lengths, sorted_idx, z):
        hidden = self.latent2hidden(z) # batch_size, hidden_size * hidden_factor
        hidden = hidden.reshape(-1, self.num_layers, self.hidden_size).permute(1, 0, 2).contiguous() # hidden_factor, batch_size, hidden_size

        # teacher forcing dropout
        if self.word_dropout_rate > 0:
            # randomly replace decoder input with <unk>
            prob = torch.rand(input_sequence.size())
            if torch.cuda.is_available():
                prob=prob.cuda()
            prob[(input_sequence.data - self.sos_idx) * (input_sequence.data - self.pad_idx) == 0] = 1
            decoder_input_sequence = input_sequence.clone()
            decoder_input_sequence[prob < self.word_dropout_rate] = self.unk_idx
            decoder_one_hot_rep = self.one_hot_embedding(decoder_input_sequence)
        packed_input = rnn_utils.pack_padded_sequence(decoder_one_hot_rep, sorted_lengths.data.tolist(), batch_first=True)

        # decoder forward pass
        outputs, _ = self.decoder_rnn(packed_input, hidden)

        # process outputs
        padded_outputs = rnn_utils.pad_packed_sequence(outputs, batch_first=True)[0]
        padded_outputs = padded_outputs.contiguous()
        _, reversed_idx = torch.sort(sorted_idx) # restore back to input order
        padded_outputs = padded_outputs[reversed_idx]
        b, s, _ = padded_outputs.size()

        # project outputs to vocab
        logp = F.log_softmax(self.outputs2vocab(padded_outputs.view(-1, padded_outputs.size(2))), dim=-1)
        logp = logp.view(b, s, self.vocab_size)
        return logp

    def forward(self, task, batch, num_samples):

        if task == 'vae':
            recon_loss, kl_loss, mkl_loss, mmd_loss = self.vae_loss(batch, num_samples) # SMILES recon. loss
            return recon_loss, kl_loss, mkl_loss, mmd_loss, to_cuda_var(torch.tensor(0.0))

        elif task == 'atc':
            local_ranking_loss = self.ranking_loss(batch)  # ATC local ranking loss
            return  to_cuda_var(torch.tensor(0.0)),  to_cuda_var(torch.tensor(0.0)),  to_cuda_var(torch.tensor(0.0)), to_cuda_var(torch.tensor(0.0)), local_ranking_loss

        elif task == 'vae + atc':
            recon_loss, kl_loss, mkl_loss, mmd_loss = self.vae_loss(batch, num_samples) # SMILES recon. loss
            local_ranking_loss = self.ranking_loss(batch) # ATC local ranking loss
            return recon_loss, kl_loss, mkl_loss, mmd_loss, local_ranking_loss

    def get_intermediates(self, batch):
        """
        get intermediates in right order
        :param batch:
        :return:
        """
        input_sequence = batch['drug_inputs']
        input_sequence_length = batch['drug_len']

        sorted_lengths, sorted_idx = torch.sort(input_sequence_length, descending=True)  # change input order
        input_sequence = input_sequence[sorted_idx]

        _, reversed_idx = torch.sort(sorted_idx)  # restore back to input order

        hidden = self.encoder(input_sequence, sorted_lengths) # hidden_factor, batch_size, hidden_size
        mean, logv, z = self.reparameterize(hidden)

        return mean[reversed_idx], logv[reversed_idx], z[reversed_idx]


    def vae_loss(self, batch, num_samples):
        batch_size = len(batch['drug_name'])
        input_sequence = batch['drug_inputs']
        target_sequence = batch['drug_targets']
        input_sequence_length = batch['drug_len']

        # compute reconstruction loss
        sorted_lengths, sorted_idx = torch.sort(input_sequence_length, descending=True) # change input order
        input_sequence = input_sequence[sorted_idx]

        hidden = self.encoder(input_sequence, sorted_lengths) # hidden_factor, batch_size, hidden_size
        mean, logv, z = self.reparameterize(hidden)
        logp_drug = self.decoder(input_sequence, sorted_lengths, sorted_idx, z)

        target = target_sequence[:, :torch.max(input_sequence_length).item()].contiguous().view(-1)
        logp = logp_drug.view(-1, logp_drug.size(2))

        # reconstruction loss
        recon_loss = self.RECON(logp, target)/batch_size
        # kl loss
        if self.beta > 0.0:
            kl_loss = self.kl_loss(mean, logv, z)/batch_size
        else:
            kl_loss = to_cuda_var(torch.tensor(0.0))
        # marginal kl loss
        if self.alpha > 0.0:
            mkl_loss = self.marginal_posterior_divergence(z, mean, logv, num_samples)/batch_size
        else:
            mkl_loss = to_cuda_var(torch.tensor(0.0))
        # MMD loss, p(z) ~ standard normal distribution
        if self.gamma > 0.0:
            mmd_loss = self.mmd_loss(z)
        else:
            mmd_loss = to_cuda_var(torch.tensor(0.0))
        return recon_loss, kl_loss, mkl_loss, mmd_loss

    def kl_loss(self, mean, logv, z):
        batch_size, n = mean.shape
        diag = to_cuda_var(torch.eye(n).repeat(batch_size, 1, 1))
        cov = torch.exp(logv).unsqueeze(dim=-1) * diag

        # compute log probabilities of posterior
        z_posterior_pdf = MultivariateNormal(mean, cov)
        logp_posterior_z = z_posterior_pdf.log_prob(z)

        if self.prior == 'Standard':
            z_prior_pdf = MultivariateNormal(to_cuda_var(torch.zeros(n)), diag)
            logp_prior_z = z_prior_pdf.log_prob(z)
            kl_loss = torch.sum(logp_posterior_z.squeeze() - logp_prior_z.squeeze())
        return kl_loss

    # estimate the KL divergence between marginal posterior q(z) to prior p(z) in a batch
    def marginal_posterior_divergence(self, z, mean, logv, num_samples):
        batch_size, n = mean.shape
        diag = to_cuda_var(torch.eye(n).repeat(1, 1, 1))

        logq_zb_lst = []
        logp_zb_lst = []
        for b in range(batch_size):
            zb = z[b, :].unsqueeze(0)
            mu_b = mean[b, :].unsqueeze(0)
            logv_b = logv[b, :].unsqueeze(0)
            diag_b = to_cuda_var(torch.eye(n).repeat(1, 1, 1))
            cov_b = torch.exp(logv_b).unsqueeze(dim=2) * diag_b

            # removing b-th mean and logv
            zr = zb.repeat(batch_size - 1, 1)
            mu_r = torch.cat((mean[:b, :], mean[b + 1:, :]))
            logv_r = torch.cat((logv[:b, :], logv[b + 1:, :]))
            diag_r = to_cuda_var(torch.eye(n).repeat(batch_size - 1, 1, 1))
            cov_r = torch.exp(logv_r).unsqueeze(dim=2) * diag_r

            # E[log q(zb)] = - H(q(z))
            zb_xb_posterior_pdf = MultivariateNormal(mu_b, cov_b)
            logq_zb_xb = zb_xb_posterior_pdf.log_prob(zb)

            zb_xr_posterior_pdf = MultivariateNormal(mu_r, cov_r)
            logq_zb_xr = zb_xr_posterior_pdf.log_prob(zr)

            yb1 = logq_zb_xb - torch.log(to_cuda_var(torch.tensor(num_samples).float()))
            yb2 = logq_zb_xr + torch.log(to_cuda_var(torch.tensor((num_samples - 1) / ((batch_size - 1) * num_samples)).float()))
            yb = torch.cat([yb1, yb2], dim=0)
            logq_zb = torch.logsumexp(yb, dim=0)

            # E[log p(zb)]
            zb_prior_pdf = MultivariateNormal(to_cuda_var(torch.zeros(n)), diag)
            logp_zb = zb_prior_pdf.log_prob(zb)

            logq_zb_lst.append(logq_zb)
            logp_zb_lst.append(logp_zb)

        logq_zb = torch.stack(logq_zb_lst, dim=0)
        logp_zb = torch.stack(logp_zb_lst, dim=0).squeeze(-1)

        return (logq_zb - logp_zb).sum()

    def ranking_loss(self, batch):

        batch_size = len(batch['drug_name'])
        instance_idx = batch['loc_ranking_indicator']
        select_idx = []
        for i in range(batch_size):
            if instance_idx[i] == 1:
                select_idx.append(i)

        # if no instance in the batch has ATC information
        if len(select_idx) == 0:
            return torch.tensor(0.0)
        else:
            input_sequence = batch['drug_inputs'][select_idx]
            input_sequence_length = batch['drug_len'][select_idx]

            # compute reconstruction loss
            sorted_lengths, sorted_idx = torch.sort(input_sequence_length, descending=True)  # change input order
            input_sequence = input_sequence[sorted_idx]

            hidden_drug = self.encoder(input_sequence, sorted_lengths)  # hidden_factor, batch_size, hidden_size
            mean_drug, _, z_drug = self.reparameterize(hidden_drug)

            local_ranking_sequence_length = batch['loc_ranking_len'][select_idx]
            local_ranking_inputs = batch['loc_ranking_inputs'][select_idx]
            nneg = local_ranking_sequence_length.shape[1] - 1

            # compute local ranking loss
            # step 1, flatten local drugs (as if change bache_size -> batch_size * (1+nneg))
            local_ranking_sequence_length_flatten = local_ranking_sequence_length.view(-1, 1)
            local_ranking_inputs_flatten = local_ranking_inputs.view(-1, self.max_sequence_length-1)  # batch, 1+nneg, seq -> batch * (1+nneg), seq

            # step 2, sort local_ranking_inputs_flatten
            local_sorted_lengths, local_sorted_idx = torch.sort(local_ranking_sequence_length_flatten.squeeze(), descending=True)
            local_ranking_inputs_flatten_sorted = local_ranking_inputs_flatten[local_sorted_idx]
            hidden_local_ranking_drug = self.encoder(local_ranking_inputs_flatten_sorted, local_sorted_lengths)
            mean_local_ranking, _,  _ = self.reparameterize(hidden_local_ranking_drug)

            # step 3, restore order of local ranking inputs
            _, local_reversed_idx = torch.sort(local_sorted_idx)  # restore back to input order
            mean_local_ranking = mean_local_ranking[local_reversed_idx]

            # step 4, restore order of drug inputs
            _, reversed_idx = torch.sort(sorted_idx)  # restore back to input order
            mean_drug = mean_drug[reversed_idx]
            mean_drug_exp = mean_drug.unsqueeze(0).repeat(1, 1, nneg + 1).view(-1, self.latent_size)

            # step 5, Lorentz distance between z_drug and z_local_ranking
            #euclidean_dist = torch.sum((mean_local_ranking - mean_drug_exp)**2, dim=1)
            #euclidean_dist = euclidean_dist.view(len(select_idx), 1+nneg)
            #euclidean_dist = torch.clamp(euclidean_dist, min=0.0)
            #euclidean_dist = torch.pow(euclidean_dist, 0.5)

            euclidean_dist = F.pairwise_distance(mean_local_ranking, mean_drug_exp)
            euclidean_dist = euclidean_dist.view(len(select_idx), 1 + nneg)

            # step 6, compute local ranking loss (as a classification task)
            #ranking_loss = - torch.log((torch.exp(-euclidean_dist[:, 0])/torch.exp(-euclidean_dist).sum(dim=1))).sum()
            ranking_loss = (euclidean_dist[:, 0] + torch.logsumexp(-euclidean_dist, dim=1)).sum()
        return ranking_loss/len(select_idx)

    def mmd_loss(self, zq):
        # true standard normal distribution samples
        true_samples = to_cuda_var(torch.randn([zq.shape[0], self.latent_size]))
        # compute mmd
        mmd = self.compute_mmd(true_samples, zq)
        return mmd

    def compute_kernel(self, x, y):
        x_size = x.size(0)
        y_size = y.size(0)
        dim = x.size(1)
        x = x.unsqueeze(1)  # (x_size, 1, dim)
        y = y.unsqueeze(0)  # (1, y_size, dim)
        tiled_x = x.expand(x_size, y_size, dim)
        tiled_y = y.expand(x_size, y_size, dim)
        kernel_input = (tiled_x - tiled_y).pow(2).mean(2) / float(dim) # 2 * sigma2 = dim^2
        return torch.exp(-kernel_input)  # (x_size, y_size)

    def compute_mmd(self, x, y):
        x_kernel = self.compute_kernel(x, x)
        y_kernel = self.compute_kernel(y, y)
        xy_kernel = self.compute_kernel(x, y)
        mmd = x_kernel.mean() + y_kernel.mean() - 2 * xy_kernel.mean()
        return mmd

