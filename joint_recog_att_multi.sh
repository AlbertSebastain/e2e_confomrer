#!/bin/bash

# Copyright 2017 Johns Hopkins University (Shinji Watanabe)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

##########
# the file is for e2e attention model  and enhance model for 3 data
# clean       match           unmatch
# the data names 
# test        mix_test_match  mix_test_unmatch
# note the feats.scp in mix_test folder is the mix feats but delete the flag 'mix'
# there are two types of models
# 1 attention model tranined by clean data
# 2 attention model is trained by mct
# there is one enhance model
##########
# general configuration
stage=5        # start from 0 if you need to start from data preparation
#gpu=            # will be deprecated, please use ngpu
ngpu=0          # number of gpus ("0" uses cpu, otherwise use gpu)
debugmode=1
dumpdir=dump   # directory to dump full features
N=0            # number of minibatches to be used (mainly for debugging). "0" uses all minibatches.
verbose=1      # verbose option
resume=${resume:=none}    # Resume the training from snapshot

# feature configuration
do_delta=false # true when using CNN



# rnnlm related
model_unit=char
lm_weight=0.2
fusion=${fusion:=none}

# decoding parameter
lmtype=rnnlm
beam_size=12
nbest=12
penalty=0.0
maxlenratio=0.0
minlenratio=0.0
ctc_weight=0.3

. utils/parse_options.sh || exit 1;
. ./cmd.sh
. ./path.sh
# check gpu option usage
if [ ! -z $gpu ]; then
    echo "WARNING: --gpu option will be deprecated."
    echo "WARNING: please use --ngpu option."
    if [ $gpu -eq -1 ]; then
        ngpu=0
    else
        ngpu=1
    fi
fi

# Set bash to 'debug' mode, it will exit on :
# -e 'error', -u 'undefined variable', -o ... 'error in pipeline', -x 'print commands',     --resume $resume \
set -e
set -u
set -o pipefail

dataroot="/usr/home/shi/projects/data_aishell/data"
dictroot="/usr/home/shi/projects/data_aishell/data/lang/phones"
train_set=train
train_dev=dev
recog_set=("test" "mix_test_match" "mix_test_unmatch")

# you can skip this and remove --rnnlm option in the recognition (stage 5)
dict=${dictroot}/${train_set}_units.txt
embed_init_file=${dictroot}/sgns
echo "dictionary: ${dict}"
nlsyms=${dictroot}/non_lang_syms.txt
#lmexpdir=checkpoints/train_${lmtype}_2layer_${input_unit_lm}_${hidden_unit_lm}_drop${dropout_lm}_bs${batchsize_lm}
lmexpdir=checkpoints_new/train_rnnlm
###########input argument
save_type=$1
if [ $# -eq 5 ]; then
    models[0]=$2
    mcts[0]=$3
    models[1]=$4
    mcts[1]=$5
    max=1
else
    models[0]=$2
    mcts[0]=$3
    max=0
fi
#########################
exproot="/usr/home/shi/projects/e2e_speech_project/data_model"
save_dir="/usr/home/shi/projects/e2e_speech_project/checkpoints_new/recog_multitype/${save_type}/"
# task related
#models=("asr_conformer_retrain" "asr_conformer_mct_retrain")
#mcts=("False" "True")
    nj=8
if [[ ${save_type} != *"joint_train"* ]]; then
    echo "joint"
    for((index=0;index<=max;index++)); do 
        #model=${models[${index}]}
        model=${models[${index}]}
        mct=${mcts[${index}]}
        #mct=${mcts[${index}]}
        for index_r in 0 1 2; do
    ##(
            rtask=${recog_set[${index_r}]}
            decode_dir="recog_${save_type}_${model}_${rtask}"
            name=${save_dir}/${decode_dir}
            echo "the recog result is saved in"
            echo ${name}  
            feat_recog_dir=${dataroot}/${rtask}
            expdir=${exproot}/${model}
            feat_recog_dir=${dataroot}/${rtask}
            #./utils/fix_data_dir.sh ${feat_recog_dir}
            # split data
            #splitjson.py --parts ${nj} ${feat_recog_dir}/data.json  --kenlm ${dictroot}/text.arpa \
            sdata=${feat_recog_dir}/split$nj
       

            #[[ -d $sdata && ${feat_recog_dir}/feats.scp -ot $sdata ]] || 
            utils/split_data.sh ${feat_recog_dir} $nj || exit 1;
            echo $nj > ${expdir}/num_jobs

            #### use CPU for decoding  ##& ##${decode_cmd} JOB=1 ${expdir}/${decode_dir}/log/decode.JOB.log \
        ${decode_cmd} JOB=1:${nj} ${save_dir}/${decode_dir}/log/decode.JOB.log \
                python3 joint_recog.py \
                --dataroot ${dataroot} \
                --dict_dir ${dictroot} \
                --name ${name} \
                --model-unit $model_unit \
                --nj $nj \
                --gpu_ids 0 \
                --fbank_dim 80 \
                --nbest $nbest \
                --enhance_layers 3 \
                --enhance_resume ${exproot}/base_fbank_train_match_noise/model.loss.best \
                --e2e_resume ${expdir}/model.acc.best\
                --recog-dir ${sdata}/JOB  \
                --test_folder ${rtask} \
                --result-label ${save_dir}/${decode_dir}/data.JOB.json \
                --beam-size ${beam_size} \
                --penalty ${penalty} \
                --maxlenratio ${maxlenratio} \
                --minlenratio ${minlenratio} \
                --ctc-weight ${ctc_weight} \
                --lmtype ${lmtype} \
                --verbose ${verbose} \
                --exp_path ${expdir} \
                --normalize_type 1 \
                --rnnlm ${lmexpdir}/rnnlm.model.best \
                --lm-weight ${lm_weight} \
                --embed-init-file ${embed_init_file} \
                --MCT ${mct}
            score_sclite.sh --nlsyms ${nlsyms} ${save_dir}/${decode_dir} ${dict}
            #--joint_resume ${expdir}/model.acc.best \

            ##kenlm_path="/home/bliu/mywork/workspace/e2e/src/kenlm/build/text_character.arpa"
            ##rescore_sclite.sh --nlsyms ${nlsyms} ${expdir}/${decode_dir} ${expdir}/${decode_dir}_rescore ${dict} ${kenlm_path}
    ##) &
        done
    done
else
    for((index=0;index<=max;index++)); do 
        #model=${models[${index}]}
        model=${models[${index}]}
        mct=${mcts[${index}]}
        #mct=${mcts[${index}]}
        for index_r in 0 1 2; do
    ##(
            rtask=${recog_set[${index_r}]}
            decode_dir="recog_${save_type}_${model}_${rtask}"
            name=${save_dir}/${decode_dir}
            echo "the recog result is saved in"
            echo ${name}  
            feat_recog_dir=${dataroot}/${rtask}
            expdir=${exproot}/${model}
            feat_recog_dir=${dataroot}/${rtask}
            #./utils/fix_data_dir.sh ${feat_recog_dir}
            # split data
            #splitjson.py --parts ${nj} ${feat_recog_dir}/data.json  --kenlm ${dictroot}/text.arpa \
            sdata=${feat_recog_dir}/split$nj
    

            #[[ -d $sdata && ${feat_recog_dir}/feats.scp -ot $sdata ]] || 
            utils/split_data.sh ${feat_recog_dir} $nj || exit 1;
            echo $nj > ${expdir}/num_jobs

            #### use CPU for decoding  ##& ##${decode_cmd} JOB=1 ${expdir}/${decode_dir}/log/decode.JOB.log \
        ${decode_cmd} JOB=1:${nj} ${save_dir}/${decode_dir}/log/decode.JOB.log \
                python3 joint_recog.py \
                --dataroot ${dataroot} \
                --dict_dir ${dictroot} \
                --name ${name} \
                --model-unit $model_unit \
                --nj $nj \
                --gpu_ids 0 \
                --fbank_dim 80 \
                --nbest $nbest \
                --enhance_layers 3 \
                --joint_resume ${expdir}/model.acc.best \
                --recog-dir ${sdata}/JOB  \
                --test_folder ${rtask} \
                --result-label ${save_dir}/${decode_dir}/data.JOB.json \
                --beam-size ${beam_size} \
                --penalty ${penalty} \
                --maxlenratio ${maxlenratio} \
                --minlenratio ${minlenratio} \
                --ctc-weight ${ctc_weight} \
                --lmtype ${lmtype} \
                --verbose ${verbose} \
                --exp_path ${expdir} \
                --normalize_type 1 \
                --rnnlm ${lmexpdir}/rnnlm.model.best \
                --lm-weight ${lm_weight} \
                --embed-init-file ${embed_init_file} \
                --MCT ${mct}
            score_sclite.sh --nlsyms ${nlsyms} ${save_dir}/${decode_dir} ${dict}
        done
    done
fi
echo "Finished"
