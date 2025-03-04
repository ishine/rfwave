import os
import torch

from pytorch_lightning.cli import LightningCLI

torch.set_float32_matmul_precision('high')


class CustomCLI(LightningCLI):
    def add_arguments_to_parser(self, parser):
        super().add_arguments_to_parser(parser)
        # Add a custom argument for the checkpoint path
        parser.add_argument('--ckpt_path', type=str, default=None, help='Path to the checkpoint file.')


if __name__ == "__main__":
    # Initialize your custom CLI
    cli = CustomCLI(run=False, save_config_kwargs={"overwrite": True})
    
    # Create the logging directory
    os.makedirs(cli.trainer.logger.save_dir, exist_ok=True)
    
    # Access the checkpoint path from the parsed arguments
    ckpt_path = cli.config['ckpt_path'] if 'ckpt_path' in cli.config else None
    
    if ckpt_path:
        cli.trainer.fit(model=cli.model, datamodule=cli.datamodule, ckpt_path=ckpt_path)
    else:
        cli.trainer.fit(model=cli.model, datamodule=cli.datamodule)
