# Efficient-Split-Learning

### based on [PFSL](https://paperswithcode.com/paper/pfsl-personalized-fair-split-learning-with)

```bibtex
@software{Manas_Wadhwa_and_Gagan_Gupta_and_Ashutosh_Sahu_and_Rahul_Saini_and_Vidhi_Mittal_PFSL_2023,
author = {Manas Wadhwa and Gagan Gupta and Ashutosh Sahu and Rahul Saini and Vidhi Mittal},
month = {2},
title = {{PFSL}},
url = {https://github.com/mnswdhw/PFSL},
version = {1.0.0},
year = {2023} 
}
```

### Usage:

```bash
git clone https://github.com/Manisha-IITBH/Efficient-Split-Learning.git --recursive
```

- update `WANDB_KEY` in config.py

### RUN:

- activate venv: `source ./.venv/bin/activate`

KiTS-19 EXAMPLE:

```
python trainer.py -c 6 -bs 4 -tbs 2 -n 30 --client_lr 6e-4 --server_lr 6e-4 --dataset kits19 --seed 42 --model nnunet --split 3 -kv --kv_refresh_rate 5 --kv_factor 2 --wandb
```

CIFAR10 EXAMPLE:

```
python trainer.py -c 10 -bs 64 -tbs 256 -n 80 --client_lr 1e-3 --server_lr 1e-3 --dataset CIFAR10 --seed 42 --model resnet18 --split 1 -kv --kv_refresh_rate 0 --kv_factor 1 --wandb

python trainer.py -c 10 -bs 64 -tbs 256 -n 80 --client_lr 1e-3 --server_lr 1e-3 --dataset CIFAR10 --seed 42 --model resnet18 --split 1 -kv --kv_refresh_rate 0 --kv_factor 1 --wandb > text.txt
```

```
options:
  -h, --help            show this help message and exit
  -c C, --number_of_clients C
                        Number of Clients (default: 6)
  -bs B, --batch_size B
                        Batch size (default: 2)
  -tbs TB, --test_batch_size TB
                        Input batch size for testing (default: 2)
  -n N, --epochs N      Total number of epochs to train (default: 10)
  --client_lr LR        Client-side learning rate (default: 0.001)
  --server_lr serverLR  Server-side learning rate (default: 0.001)
  --dataset DATASET     States dataset to be used (default: kits19)
  --seed SEED           Random seed (default: 42)
  --model MODEL         Model you would like to train (default: nnunet)
  --split SPLIT         The model split version to use (default: 1)
  -kv, --use_key_value_store
                        use key value store for faster training (default: False)
  --kv_factor KV_FACTOR
                        populate key value store kv_factor times (default: 1)
  --kv_refresh_rate KV_REFRESH_RATE
                        refresh key-value store every kv_refresh_rate epochs, 0 =
                        disable refresing (default: 5)
  --wandb               Enable wandb logging (default: False)
  --pretrained          Model is pretrained/not, DEFAULT True, No change required
                        (default: True)
  --personalize         Enable client personalization (default: False)
  --pool                create a single client with all the data, trained in split
                        learning mode, overrides number_of_clients (default: False)
  --dynamic             Use dynamic transforms, transforms will be applied to the
                        server-side kv-store every epoch (default: False)
  --p_epoch P_EPOCH     Epoch at which personalisation phase will start (default: 50)
  --offload_only        USE SERVER ONLY FOR OFFLOADING, CURRENTLY ONLY IMPLEMENTED FOR
                        IXI-TINY (default: False)
```

---

## Datasets

- KiTS19
- IXI Tiny
- ISIC2019
- CIAFR10
