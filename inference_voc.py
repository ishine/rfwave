import librosa
import warnings

import numpy as np
import soundfile
import torch
import yaml
import time
import rfwave
import reflow
import re
import kaldiio
import torchaudio
import torch.cuda.amp as amp

from pathlib import Path
from argparse import ArgumentParser
from tqdm import tqdm


torch.set_float32_matmul_precision('high')

ts_config = {
    2: [0, 0.33, 1.0],
    3: [0, 0.12, 0.65, 1.0],
    4: [0, 0.06, 0.33, 0.76, 1.0],
    5: [0, 0.04, 0.18, 0.53, 0.82, 1.0],
    6: [0, 0.03, 0.12, 0.33, 0.65, 0.86, 1.0],
    7: [0, 0.02, 0.08, 0.22, 0.48, 0.72, 0.88, 1.0],
    8: [0, 0.01, 0.06, 0.15, 0.33, 0.58, 0.76, 0.9, 1.0],
    9: [0, 0.01, 0.05, 0.12, 0.24, 0.44, 0.65, 0.8, 0.91, 1.0],
    10: [0, 0.01, 0.04, 0.09, 0.18, 0.33, 0.53, 0.7, 0.82, 0.92, 1.0]
}


def load_config(config_yaml):
    with open(config_yaml, 'r') as stream:
        config = yaml.safe_load(stream)
    return config


def create_instance(config):
    for k, v in config['init_args'].items():
        if isinstance(v, dict) and 'class_path' in v and 'init_args' in v:
            config['init_args'][k] = create_instance(v)
    return eval(config['class_path'])(**config['init_args'])


def load_model(model_dir, device, last=False):
    config_yaml = Path(model_dir) / 'config.yaml'
    if last:
        ckpt_fp = list(Path(model_dir).rglob("last.ckpt"))
        if len(ckpt_fp) == 0:
            raise ValueError(f"No checkpoint found in {model_dir}")
        elif len(ckpt_fp) > 1:
            warnings.warn(f"More than 1 checkpoints found in {model_dir}")
            ckpt_fp = sorted([fp for fp in ckpt_fp], key=lambda x: x.stat().st_ctime)[-1:]
        ckpt_fp = ckpt_fp[0]
        print(f'using last ckpt form {str(ckpt_fp)}')
    else:
        ckpt_fp = [fp for fp in list(Path(model_dir).rglob("*.ckpt")) if 'last' not in fp.stem]
        ckpt_fp = sorted(ckpt_fp, key=lambda x: int(re.search('_step=(\d+)_', x.stem).group(1)))[-1]
        print(f'using best ckpt form {str(ckpt_fp)}')

    config = load_config(config_yaml)
    exp = create_instance(config['model'])

    model_dict = torch.load(ckpt_fp, map_location='cpu')
    exp.load_state_dict(model_dict['state_dict'])
    exp.eval()
    exp.to(device)
    return exp


def copy_synthesis(exp, y, N=1000):
    features = exp.feature_extractor(y)
    if N in ts_config:
        ts = ts_config[N]
        assert N == len(ts) - 1
    else:
        ts = np.linspace(0, 1, N + 1)
    start = time.time()
    sample = exp.reflow.sample_ode(features, N=N, ts=ts)[-1]
    cost = time.time() - start
    l = min(sample.size(-1), y.size(-1))
    rvm_loss = exp.rvm(sample[..., :l], y[..., :l])
    recon = sample.detach().cpu().numpy()[0]
    return recon, cost, rvm_loss


def copy_synthesis_encodec(exp, y, N=1000):
    num_encodec_bandwidths = len(exp.feature_extractor.bandwidths)
    recons = {}
    costs = {}
    rmv_losses = {}
    for encodec_bandwidth_id in range(num_encodec_bandwidths):
        # encodec_bandwidth_id is set in feature_extractor.forward
        features = exp.feature_extractor(y, encodec_bandwidth_id=encodec_bandwidth_id)
        encodec_audio = exp.feature_extractor.encodec(y[None, :])
        encodec_audio = encodec_audio.detach().cpu().numpy()[0, 0]
        encodec_bandwidth_id = torch.tensor([encodec_bandwidth_id], dtype=torch.long, device=y.device)
        start = time.time()
        sample = exp.reflow.sample_ode(features, encodec_bandwidth_id=encodec_bandwidth_id, N=N)[-1]
        cost = time.time() - start
        l = min(sample.size(-1), y.size(-1))
        rvm_loss = exp.rvm(sample[..., :l], y[..., :l])
        recon = sample.detach().cpu().numpy()[0]
        recons[f'bw_{encodec_bandwidth_id.item()}'] = recon
        recons[f'enc_bw_{encodec_bandwidth_id.item()}'] = encodec_audio
        costs[f'bw_{encodec_bandwidth_id.item()}'] = cost
        rmv_losses[f'bw_{encodec_bandwidth_id.item()}'] = rvm_loss
    return recons, costs, rmv_losses


def voc(model_dir, wav_dir, save_dir, guidance_scale, N=10):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    exp = load_model(model_dir, device=device, last=True)
    if exp.reflow.guidance_scale == 1. and guidance_scale is not None and guidance_scale > 1.:
        warnings.warn("The original does not use classifier-free guidance. cfg argument is omitted")
    if guidance_scale is not None:
        print(f"Original guidance_scale {exp.reflow.guidance_scale:.2f}, using guidance_scale {guidance_scale:.2f}")
        exp.reflow.guidance_scale = guidance_scale
    is_encodec = isinstance(exp.feature_extractor, rfwave.feature_extractors.EncodecFeatures)

    N = 1 if getattr(exp, 'one_step', False) else N

    tot_cost = 0.
    tot_dur = 0.
    if Path(wav_dir).is_dir():
        wav_fps = Path(wav_dir).rglob("*.wav")
    elif Path(wav_dir).is_file() and Path(wav_dir).suffix == '.scp':
        arc_dict = kaldiio.load_scp(wav_dir, max_cache_fd=32)
        wav_fps = arc_dict.items()
    else:
        raise ValueError(f"wav_dir should be a dir or a scp file, got {wav_dir}")

    wav_fps = list(wav_fps)
    # compile model first.
    for wav_fp in wav_fps[:5]:
        if isinstance(wav_fp, Path):
            y, fs = torchaudio.load(str(wav_fp))
            fn = wav_fp.name
        elif isinstance(wav_fp, tuple):
            fn = wav_fp[0].replace('/', '_') + '.wav'
            fs, y = wav_fp[1]
            y = torch.from_numpy(y.T.astype('float32'))
        else:
            raise ValueError(f"wav_fp should be a Path or a tuple, got {wav_fp}")
        y, _ = torchaudio.sox_effects.apply_effects_tensor(y, fs, [["norm", "-3.0"]])
        y = y.to(exp.device)
        if is_encodec:
            copy_synthesis_encodec(exp, y, N=N)
        else:
            copy_synthesis(exp, y, N=N)

    print("start synthesizing")
    for wav_fp in tqdm(wav_fps):
        if isinstance(wav_fp, Path):
            y, fs = torchaudio.load(str(wav_fp))
            fn = wav_fp.name
        elif isinstance(wav_fp, tuple):
            fn = wav_fp[0].replace('/', '_') + '.wav'
            fs, y = wav_fp[1]
            y = torch.from_numpy(y.T.astype('float32'))
        else:
            raise ValueError(f"wav_fp should be a Path or a tuple, got {wav_fp}")
        y, _ = torchaudio.sox_effects.apply_effects_tensor(y, fs, [["norm", "-3.0"]])
        # y, _ = torchaudio.sox_effects.apply_effects_tensor(y, fs, [["norm", "-1.5"]])

        if y.size(0) > 1:
            y = y[:1]

        rel_dir = wav_fp.relative_to(wav_dir).parent
        save_dir_ = Path(save_dir) / rel_dir
        save_dir_.mkdir(exist_ok=True, parents=True)

        y = y.to(exp.device)
        if is_encodec:
            fn = fn.rstrip('.wav')
            recon, cost, rvm_loss = copy_synthesis_encodec(exp, y, N=N)
            for k, v in recon.items():
                fn_ = f'{fn}-{k}.wav'
                save_fp = Path(save_dir_) / fn_
                soundfile.write(save_fp, v.astype(float), fs, 'PCM_16')
            for k in cost.keys():
                dur = len(recon[k]) / fs
                tot_dur += dur
                tot_cost += cost[k]
        else:
            save_fp = Path(save_dir_) / fn
            recon, cost, rvm_loss = copy_synthesis(exp, y, N=N)
            soundfile.write(save_fp, recon.astype(float), fs, 'PCM_16')
            dur = len(recon) / fs
            tot_cost += cost
            tot_dur += dur

    return tot_cost, tot_dur


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--model_dir', type=str, default=None)
    parser.add_argument('--wav_dir', type=str, required=True)
    parser.add_argument('--save_dir', type=str, required=True)
    parser.add_argument('--guidance_scale', type=float, default=None)
    parser.add_argument('--num_steps', type=int, default=10)

    args = parser.parse_args()
    assert not (args.model_dir is None and args.pretrained is None)
    Path(args.save_dir).mkdir(exist_ok=True)
    cost, dur = voc(args.model_dir, args.wav_dir, args.save_dir, args.guidance_scale, args.num_steps)
    print(f"Total cost: {cost:.2f}s, Total duration: {dur:.2f}s, ratio: {dur / cost:.2f}")
