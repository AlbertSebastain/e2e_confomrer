# Copyright 2019 Shigeki Karita
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Transformer speech recognition model (pytorch)."""

from argparse import Namespace
from itertools import groupby
import logging
import math

import numpy
import torch

# from transformer.asr_interface import ASRInterface
from transformer.ctc_prefix_score import CTCPrefixScore
from model.e2e_common import end_detect
from model.e2e_common import ErrorCalculator
from transformer.ctc import CTC

# from e2e_asr import CTC_LOSS_THRESHOLD
# from e2e_asr import Reporter
from transformer.nets_utils import get_subsample
from transformer.nets_utils import make_non_pad_mask
from transformer.nets_utils import th_accuracy

# from rnn.decoders import CTC_SCORING_RATIO
from transformer.add_sos_eos import add_sos_eos
from transformer.add_sos_eos import mask_uniform
from transformer.argument import add_arguments_transformer_common
from transformer.attention import MultiHeadedAttention , RelPositionMultiHeadedAttention
from transformer.dynamic_conv import DynamicConvolution
from transformer.dynamic_conv2d import DynamicConvolution2D
from transformer.decoder import Decoder

from transformer.encoder import Encoder
from transformer.initializer import initialize
from transformer.label_smoothing_loss import LabelSmoothingLoss
from transformer.mask import subsequent_mask
from transformer.mask import target_mask
from transformer.nets_utils import to_device, pad_list

# from transformer.plot import PlotAttentionReport
# from espnet.utils.fill_missing_args import fill_missing_args

CTC_LOSS_THRESHOLD = 10000
CTC_SCORING_RATIO = 1.5


class E2E(torch.nn.Module):
    """E2E module.
    :param int idim: dimension of inputs
    :param int odim: dimension of outputs
    :param Namespace args: argument Namespace containing options
    """

    @staticmethod
    def add_arguments(parser):
        """Add arguments."""
        group = parser.add_argument_group("transformer model setting")
        group = add_arguments_transformer_common(group)
        # Non-autoregressive training
        group.add_argument(
            "--decoder-mode",
            default="AR",
            type=str,
            choices=["ar", "maskctc"],
            help="AR: standard autoregressive training, " "maskctc: non-autoregressive training based on Mask CTC",
        )
        return parser

    def __init__(self, idim, odim, args, ignore_id=-1):
        """Construct an E2E object.
        :param int idim: dimension of inputs
        :param int odim: dimension of outputs
        :param Namespace args: argument Namespace containing options
        """
        torch.nn.Module.__init__(self)

        # fill missing arguments for compatibility
        # args = fill_missing_args(args, self.add_arguments)

        self.idim = idim

        if args.transformer_attn_dropout_rate is None:
            args.transformer_attn_dropout_rate = args.dropout_rate
        self.encoder = Encoder(
            idim=idim,
            selfattention_layer_type=args.transformer_encoder_selfattn_layer_type,
            attention_dim=args.adim,
            attention_heads=args.aheads,
            conv_wshare=args.wshare,
            conv_kernel_length=args.ldconv_encoder_kernel_length,
            conv_usebias=args.ldconv_usebias,
            linear_units=args.eunits,
            num_blocks=args.elayers,
            input_layer=args.transformer_input_layer,
            dropout_rate=args.dropout_rate,
            positional_dropout_rate=args.dropout_rate,
            attention_dropout_rate=args.transformer_attn_dropout_rate,
        )
        if args.mtlalpha < 1:
            self.decoder = Decoder(
                odim=odim,
                selfattention_layer_type=args.transformer_decoder_selfattn_layer_type,
                attention_dim=args.adim,
                attention_heads=args.aheads,
                conv_wshare=args.wshare,
                conv_kernel_length=args.ldconv_decoder_kernel_length,
                conv_usebias=args.ldconv_usebias,
                linear_units=args.dunits,
                num_blocks=args.dlayers,
                dropout_rate=args.dropout_rate,
                positional_dropout_rate=args.dropout_rate,
                self_attention_dropout_rate=args.transformer_attn_dropout_rate,
                src_attention_dropout_rate=args.transformer_attn_dropout_rate,
            )
            self.criterion = LabelSmoothingLoss(
                odim,
                ignore_id,
                args.lsm_weight,
                args.transformer_length_normalized_loss,
            )
        else:
            self.decoder = None
            self.criterion = None

        self.blank = 0
        self.decoder_mode = args.decoder_mode

        if self.decoder_mode == "maskctc":
            self.mask_token = odim - 1
            self.sos = odim - 2
            self.eos = odim - 2
        else:
            self.sos = odim - 1
            self.eos = odim - 1
        self.odim = odim
        self.ignore_id = ignore_id
        self.subsample = get_subsample(args, mode="asr", arch="transformer")
        # self.reporter = Reporter()

        self.reset_parameters(args)
        self.adim = args.adim  # used for CTC (equal to d_model)
        self.mtlalpha = args.mtlalpha
        if args.mtlalpha > 0.0:
            self.ctc = CTC(odim, args.adim, args.dropout_rate, ctc_type=args.ctc_type, reduce=True)
        else:
            self.ctc = None
        args.sym_blank = '<blank>'
        args.sym_space = '<space>'
        if args.report_cer or args.report_wer:
            self.error_calculator = ErrorCalculator(
                args.char_list,
                args.sym_space,
                args.sym_blank,
                args.report_cer,
                args.report_wer,
            )
        else:
            self.error_calculator = None
        self.rnnlm = None

    def reset_parameters(self, args):
        """Initialize parameters."""
        # initialize parameters
        initialize(self, args.transformer_init)

    def forward(self, xs_pad, ilens, targets,target_sizes):
        """E2E forward.
        :param torch.Tensor xs_pad: batch of padded source sequences (B, Tmax, idim)
        :param torch.Tensor ilens: batch of lengths of source sequences (B)
        :param torch.Tensor ys_pad: batch of padded target sequences (B, Lmax)
        :return: ctc loss value
        :rtype: torch.Tensor
        :return: attention loss value
        :rtype: torch.Tensor
        :return: accuracy in attention decoder
        :rtype: float
        """
        # 1. forward encoder
        # 准备ys_pad
        ys = []
        offset = 0
        for size in target_sizes:
            ys.append(targets[offset : offset + size])
            offset += size
        ys_pad = pad_list(ys, self.ignore_id)
        ys_pad = to_device(self, ys_pad)
        # 准备xs_pad
        xs_pad = to_device(self, xs_pad)  # 输入
        ilens = to_device(self, ilens)  # 输入的尺寸

        xs_pad = xs_pad[:, : max(ilens)]  # for data parallel
        src_mask = make_non_pad_mask(ilens.tolist()).to(xs_pad.device).unsqueeze(-2)
        hs_pad, hs_mask = self.encoder(xs_pad, src_mask)
        self.hs_pad = hs_pad

        # 2. forward decoder
        if self.decoder is not None:
            if self.decoder_mode == "maskctc":
                ys_in_pad, ys_out_pad = mask_uniform(ys_pad, self.mask_token, self.eos, self.ignore_id)
                ys_mask = (ys_in_pad != self.ignore_id).unsqueeze(-2)
            else:
                ys_in_pad, ys_out_pad = add_sos_eos(ys_pad, self.sos, self.eos, self.ignore_id)
                ys_mask = target_mask(ys_in_pad, self.ignore_id)
            pred_pad, pred_mask = self.decoder(ys_in_pad, ys_mask, hs_pad, hs_mask)
            self.pred_pad = pred_pad

            # 3. compute attention loss
            loss_att = self.criterion(pred_pad, ys_out_pad)
            self.acc = th_accuracy(pred_pad.view(-1, self.odim), ys_out_pad, ignore_label=self.ignore_id)
        else:
            loss_att = None
            self.acc = None

        cer_ctc = None
        if self.mtlalpha == 0.0:
            loss_ctc = None
        else:
            batch_size = xs_pad.size(0)
            hs_len = hs_mask.view(batch_size, -1).sum(1)
            loss_ctc = self.ctc(hs_pad.view(batch_size, -1, self.adim), hs_len, ys_pad)
            if not self.training and self.error_calculator is not None:
                ys_hat = self.ctc.argmax(hs_pad.view(batch_size, -1, self.adim)).data
                cer_ctc = self.error_calculator(ys_hat.cpu(), ys_pad.cpu(), is_ctc=True)
            # for visualization
            if not self.training:
                self.ctc.softmax(hs_pad)

        # 5. compute cer/wer
        if self.training or self.error_calculator is None or self.decoder is None:
            cer, wer = None, None
        else:
            ys_hat = pred_pad.argmax(dim=-1)
            cer, wer = self.error_calculator(ys_hat.cpu(), ys_pad.cpu())

        # copied from e2e_asr
        alpha = self.mtlalpha
        if alpha == 0:
            self.loss = loss_att
            loss_att_data = float(loss_att)
            loss_ctc_data = None
        elif alpha == 1:
            self.loss = loss_ctc
            loss_att_data = None
            loss_ctc_data = float(loss_ctc)
        else:
            self.loss = alpha * loss_ctc + (1 - alpha) * loss_att
            loss_att_data = float(loss_att)
            loss_ctc_data = float(loss_ctc)

        loss_data = float(self.loss)
        if loss_data < CTC_LOSS_THRESHOLD and not math.isnan(loss_data):
            pass
            # self.reporter.report(loss_ctc_data, loss_att_data, self.acc, cer_ctc, cer, wer, loss_data)
        else:
            logging.warning("loss (=%f) is not correct", loss_data)
        return self.loss, self.acc

    def encode(self, x):
        """Encode acoustic features.
        :param ndarray x: source acoustic feature (T, D)
        :return: encoder outputs
        :rtype: torch.Tensor
        """
        self.eval()
        x = torch.as_tensor(x)
        if len(x.shape) == 2:
            x = x.unsqueeze(1)
        enc_output, _ = self.encoder(x, None)
        return enc_output.squeeze(0)

    def recognize(self, x, recog_args, char_list=None, rnnlm=None, use_jit=False):
        """Recognize input speech.
        :param ndnarray x: input acoustic feature (B, T, D) or (T, D)
        :param Namespace recog_args: argment Namespace contraining options
        :param list char_list: list of characters
        :param torch.nn.Module rnnlm: language model module
        :return: N-best decoding results
        :rtype: list
        """
        enc_output = self.encode(x).unsqueeze(0)
        if self.mtlalpha == 1.0:
            recog_args.ctc_weight = 1.0
            logging.info("Set to pure CTC decoding mode.")

        if self.mtlalpha > 0 and recog_args.ctc_weight == 1.0:
            from itertools import groupby

            lpz = self.ctc.argmax(enc_output)
            collapsed_indices = [x[0] for x in groupby(lpz[0])]
            hyp = [x for x in filter(lambda x: x != self.blank, collapsed_indices)]
            nbest_hyps = [{"score": 0.0, "yseq": [self.sos] + hyp}]
            if recog_args.beam_size > 1:
                raise NotImplementedError("Pure CTC beam search is not implemented.")
            # TODO(hirofumi0810): Implement beam search
            return nbest_hyps
        elif self.mtlalpha > 0 and recog_args.ctc_weight > 0.0:
            lpz = self.ctc.log_softmax(enc_output)
            lpz = lpz.squeeze(0)
        else:
            lpz = None

        h = enc_output.squeeze(0)

        logging.info("input lengths: " + str(h.size(0)))
        # search parms
        beam = recog_args.beam_size
        penalty = recog_args.penalty
        ctc_weight = recog_args.ctc_weight

        # preprare sos
        y = self.sos
        vy = h.new_zeros(1).long()
        vy = to_device(self,vy)

        if recog_args.maxlenratio == 0:
            maxlen = h.shape[0]
        else:
            # maxlen >= 1
            maxlen = max(1, int(recog_args.maxlenratio * h.size(0)))
        minlen = int(recog_args.minlenratio * h.size(0))
        logging.info("max output length: " + str(maxlen))
        logging.info("min output length: " + str(minlen))

        # initialize hypothesis
        if rnnlm:
            hyp = {"score": 0.0, "yseq": [y], "rnnlm_prev": None}
        else:
            hyp = {"score": 0.0, "yseq": [y]}
        if lpz is not None:
            ctc_prefix_score = CTCPrefixScore(lpz.detach().cpu().numpy(), 0, self.eos, numpy)
            hyp["ctc_state_prev"] = ctc_prefix_score.initial_state()
            hyp["ctc_score_prev"] = 0.0
            if ctc_weight != 1.0:
                # pre-pruning based on attention scores
                ctc_beam = min(lpz.shape[-1], int(beam * CTC_SCORING_RATIO))
            else:
                ctc_beam = lpz.shape[-1]
        hyps = [hyp]
        ended_hyps = []

        import six

        traced_decoder = None
        for i in six.moves.range(maxlen):
            logging.debug("position " + str(i))

            hyps_best_kept = []
            for hyp in hyps:
                vy[0] = hyp["yseq"][i]

                # get nbest local scores and their ids
                ys_mask = subsequent_mask(i + 1).unsqueeze(0)
                ys = torch.tensor(hyp["yseq"]).unsqueeze(0)
                ys = to_device(self,ys)
                ys_mask = to_device(self,ys_mask)
                # FIXME: jit does not match non-jit result
                if use_jit:
                    if traced_decoder is None:
                        traced_decoder = torch.jit.trace(self.decoder.forward_one_step, (ys, ys_mask, enc_output))
                    local_att_scores = traced_decoder(ys, ys_mask, enc_output)[0]
                else:
                    local_att_scores = self.decoder.forward_one_step(ys, ys_mask, enc_output)[0]

                if rnnlm:
                    rnnlm_state, local_lm_scores = rnnlm.predict(hyp["rnnlm_prev"], vy)
                    local_scores = local_att_scores + recog_args.lm_weight * local_lm_scores
                else:
                    local_scores = local_att_scores

                if lpz is not None:
                    local_best_scores, local_best_ids = torch.topk(local_att_scores, ctc_beam, dim=1)
                    ctc_scores, ctc_states = ctc_prefix_score(hyp["yseq"], local_best_ids[0], hyp["ctc_state_prev"])
                    local_scores = (1.0 - ctc_weight) * local_att_scores[:, local_best_ids[0]].cpu() + ctc_weight * torch.from_numpy(ctc_scores - hyp["ctc_score_prev"])
                    if rnnlm:
                        local_scores += recog_args.lm_weight * local_lm_scores[:, local_best_ids[0]].cpu()
                    local_best_scores, joint_best_ids = torch.topk(local_scores, beam, dim=1)
                    local_best_ids = local_best_ids[:, joint_best_ids[0]]
                else:
                    local_best_scores, local_best_ids = torch.topk(local_scores, beam, dim=1)

                for j in six.moves.range(beam):
                    new_hyp = {}
                    new_hyp["score"] = hyp["score"] + float(local_best_scores[0, j])
                    new_hyp["yseq"] = [0] * (1 + len(hyp["yseq"]))
                    new_hyp["yseq"][: len(hyp["yseq"])] = hyp["yseq"]
                    new_hyp["yseq"][len(hyp["yseq"])] = int(local_best_ids[0, j])
                    if rnnlm:
                        new_hyp["rnnlm_prev"] = rnnlm_state
                    if lpz is not None:
                        new_hyp["ctc_state_prev"] = ctc_states[joint_best_ids[0, j]]
                        new_hyp["ctc_score_prev"] = ctc_scores[joint_best_ids[0, j]]
                    # will be (2 x beam) hyps at most
                    hyps_best_kept.append(new_hyp)

                hyps_best_kept = sorted(hyps_best_kept, key=lambda x: x["score"], reverse=True)[:beam]

            # sort and get nbest
            hyps = hyps_best_kept
            logging.debug("number of pruned hypothes: " + str(len(hyps)))
            if char_list is not None:
                logging.debug("best hypo: " + "".join([char_list[int(x)] for x in hyps[0]["yseq"][1:]]))

            # add eos in the final loop to avoid that there are no ended hyps
            if i == maxlen - 1:
                logging.info("adding <eos> in the last postion in the loop")
                for hyp in hyps:
                    hyp["yseq"].append(self.eos)

            # add ended hypothes to a final list, and removed them from current hypothes
            # (this will be a probmlem, number of hyps < beam)
            remained_hyps = []
            for hyp in hyps:
                if hyp["yseq"][-1] == self.eos:
                    # only store the sequence that has more than minlen outputs
                    # also add penalty
                    if len(hyp["yseq"]) > minlen:
                        hyp["score"] += (i + 1) * penalty
                        #if rnnlm:  # Word LM needs to add final <eos> score
                            #hyp["score"] += recog_args.lm_weight * rnnlm.final(hyp["rnnlm_prev"])
                        ended_hyps.append(hyp)
                else:
                    remained_hyps.append(hyp)

            # end detection
            if end_detect(ended_hyps, i) and recog_args.maxlenratio == 0.0:
                logging.info("end detected at %d", i)
                break

            hyps = remained_hyps
            if len(hyps) > 0:
                logging.debug("remeined hypothes: " + str(len(hyps)))
            else:
                logging.info("no hypothesis. Finish decoding.")
                break

            if char_list is not None:
                for hyp in hyps:
                    logging.debug("hypo: " + "".join([char_list[int(x)] for x in hyp["yseq"][1:]]))

            logging.debug("number of ended hypothes: " + str(len(ended_hyps)))

        nbest_hyps = sorted(ended_hyps, key=lambda x: x["score"], reverse=True)[: min(len(ended_hyps), recog_args.nbest)]

        # check number of hypotheis
        if len(nbest_hyps) == 0:
            logging.warning("there is no N-best results, perform recognition " "again with smaller minlenratio.")
            # should copy becasuse Namespace will be overwritten globally
            recog_args = Namespace(**vars(recog_args))
            recog_args.minlenratio = max(0.0, recog_args.minlenratio - 0.1)
            return self.recognize(x, recog_args, char_list, rnnlm)

        logging.info("total log probability: " + str(nbest_hyps[0]["score"]))
        logging.info("normalized log probability: " + str(nbest_hyps[0]["score"] / len(nbest_hyps[0]["yseq"])))
        return nbest_hyps

    def recognize_maskctc(self, x, recog_args, char_list=None):
        """Non-autoregressive decoding using Mask CTC.
        :param ndnarray x: input acoustic feature (B, T, D) or (T, D)
        :param Namespace recog_args: argment Namespace contraining options
        :param list char_list: list of characters
        :return: decoding result
        :rtype: list
        """
        self.eval()
        h = self.encode(x).unsqueeze(0)

        ctc_probs, ctc_ids = torch.exp(self.ctc.log_softmax(h)).max(dim=-1)
        y_hat = torch.stack([x[0] for x in groupby(ctc_ids[0])])
        y_idx = torch.nonzero(y_hat != 0).squeeze(-1)

        probs_hat = []
        cnt = 0
        for i, y in enumerate(y_hat.tolist()):
            probs_hat.append(-1)
            while cnt < ctc_ids.shape[1] and y == ctc_ids[0][cnt]:
                if probs_hat[i] < ctc_probs[0][cnt]:
                    probs_hat[i] = ctc_probs[0][cnt].item()
                cnt += 1
        probs_hat = torch.from_numpy(numpy.array(probs_hat))

        char_mask = "_"
        p_thres = recog_args.maskctc_probability_threshold
        mask_idx = torch.nonzero(probs_hat[y_idx] < p_thres).squeeze(-1)
        confident_idx = torch.nonzero(probs_hat[y_idx] >= p_thres).squeeze(-1)
        mask_num = len(mask_idx)

        y_in = torch.zeros(1, len(y_idx) + 1, dtype=torch.long) + self.mask_token
        y_in[0][confident_idx] = y_hat[y_idx][confident_idx]
        y_in[0][-1] = self.eos

        logging.info("ctc:{}".format("".join([char_list[y] if y != self.mask_token else char_mask for y in y_in[0].tolist()]).replace("<space>", " ")))

        if not mask_num == 0:
            K = recog_args.maskctc_n_iterations
            num_iter = K if mask_num >= K and K > 0 else mask_num

            for t in range(1, num_iter):
                pred, _ = self.decoder(y_in, (y_in != self.ignore_id).unsqueeze(-2), h, None)
                pred_sc, pred_id = pred[0][mask_idx].max(dim=-1)
                cand = torch.topk(pred_sc, mask_num // num_iter, -1)[1]
                y_in[0][mask_idx[cand]] = pred_id[cand]
                mask_idx = torch.nonzero(y_in[0] == self.mask_token).squeeze(-1)

                logging.info("msk:{}".format("".join([char_list[y] if y != self.mask_token else char_mask for y in y_in[0].tolist()]).replace("<space>", " ")))

            pred, pred_mask = self.decoder(y_in, (y_in != self.ignore_id).unsqueeze(-2), h, None)
            y_in[0][mask_idx] = pred[0][mask_idx].argmax(dim=-1)
            logging.info("msk:{}".format("".join([char_list[y] if y != self.mask_token else char_mask for y in y_in[0].tolist()]).replace("<space>", " ")))

        ret = y_in.tolist()[0][:-1]
        hyp = {"score": 0.0, "yseq": [self.sos] + ret + [self.eos]}

        return [hyp]

    def calculate_all_attentions(self, xs_pad, ilens, ys_pad):
        """E2E attention calculation.
        :param torch.Tensor xs_pad: batch of padded input sequences (B, Tmax, idim)
        :param torch.Tensor ilens: batch of lengths of input sequences (B)
        :param torch.Tensor ys_pad: batch of padded token id sequence tensor (B, Lmax)
        :return: attention weights (B, H, Lmax, Tmax)
        :rtype: float ndarray
        """
        self.eval()
        with torch.no_grad():
            self.forward(xs_pad, ilens, ys_pad)
        ret = dict()
        for name, m in self.named_modules():
            if isinstance(m, MultiHeadedAttention) or isinstance(m, DynamicConvolution) or isinstance(m, RelPositionMultiHeadedAttention):
                ret[name] = m.attn.cpu().numpy()
            if isinstance(m, DynamicConvolution2D):
                ret[name + "_time"] = m.attn_t.cpu().numpy()
                ret[name + "_freq"] = m.attn_f.cpu().numpy()
        self.train()
        return ret

    def calculate_all_ctc_probs(self, xs_pad, ilens, ys_pad):
        """E2E CTC probability calculation.
        :param torch.Tensor xs_pad: batch of padded input sequences (B, Tmax)
        :param torch.Tensor ilens: batch of lengths of input sequences (B)
        :param torch.Tensor ys_pad: batch of padded token id sequence tensor (B, Lmax)
        :return: CTC probability (B, Tmax, vocab)
        :rtype: float ndarray
        """
        ret = None
        if self.mtlalpha == 0:
            return ret

        self.eval()
        with torch.no_grad():
            self.forward(xs_pad, ilens, ys_pad)
        for name, m in self.named_modules():
            if isinstance(m, CTC) and m.probs is not None:
                ret = m.probs.cpu().numpy()
        self.train()
        return ret
    @classmethod
    def load_model(cls, path, state_dict, opt=None):
        """classmethode类型，代表可以不需要实例化，直接使用该函数，并使用cls使用该类的属性等内容。
        这个函数可以读取模型。

        Args:
            path (str): 模型的地址
            state_dict (dict): 模型的参数

        Returns:
            model类: 模型
        """
        if path is not None:
            # # Load all tensors onto GPU 1
            package = torch.load(path, map_location=lambda storage, loc: storage)
            # 加载opt
            model = cls(args=package["opt"])
            print("model.state_dict() is", model.state_dict().keys())
            if state_dict in package and package[state_dict] is not None:
                model.load_state_dict(package[state_dict])
                print("package.state_dict() is", package[state_dict].keys())
                print("checkpoint found at {} {}".format(path, state_dict))
        else:
            model = cls(opt)
            print("no checkpoint found, so init model")
        if opt is not None and len(opt.gpu_ids) > 0:
            model = model.cuda()
        print(model)
        return model