import os
import datetime

import torch
import torch.nn.functional as F
from tqdm.auto import tqdm

import wandb
from logger.logger import NeptuneLogger, WandbLogger
from trainers.tensorboard_pytorch import TensorboardPyTorch


# def get_laser(model, x_true):
#     pass

class EarlyExitTrainer(object):
    def __init__(self, model, ee_method, loaders, criterion, optim, accelerator=None, lr_scheduler=None, device='cpu'):
        self.model = model
        self.ee_method = ee_method
        self.criterion = criterion
        self.optim = optim
        self.accelerator = accelerator
        self.lr_scheduler = lr_scheduler
        self.loaders = loaders
        self.t_logger = None  # tensorflow logger
        self.device = device

    def run_exp(self, epoch_start, epoch_end, exp_name, config_run_epoch, temp=1.0, random_seed=42):
        """
        Main method of trainer.
        Init df -> [Init Run -> [Run Epoch]_{IL} -> Update df]_{IL}]
        {IL - In Loop}
        Args:
            epoch_start (int): A number representing the beginning of run
            epoch_end (int): A number representing the end of run
            exp_name (str): Base name of experiment
            config_run_epoch (): ##
            temp (float): CrossEntropy Temperature
            random_seed (int): Seed generator
        """
        save_path = self.at_exp_start(exp_name, random_seed)
        for epoch in tqdm(range(epoch_start, epoch_end), desc='run_exp'):
            self.model.train()
            self.ee_method.train()
            self.run_epoch(epoch, save_path, config_run_epoch, phase='train')
            self.model.eval()
            self.ee_method.eval()
            with torch.no_grad():
                self.run_epoch(epoch, save_path, config_run_epoch, phase='test')
        wandb.finish()

    def at_exp_start(self, exp_name, random_seed):
        """
        Initialization of experiment.
        Creates fullname, dirs and loggers.
        Args:
            exp_name (str): Base name of experiment
            random_seed (int): seed generator
        Returns:
            save_path (str): Path to save the model
        """
        self.manual_seed(random_seed)
        print('is fp16?', self.accelerator.use_fp16)
        date = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_path = os.path.join(os.getcwd(), f'exps/{exp_name}/{date}')
        save_path = f'{base_path}/checkpoints'
        if not os.path.exists(save_path):
            os.makedirs(save_path)
        WandbLogger(wandb, exp_name, 0)
        # wandb.tensorboard.patch(root_logdir=f'{base_path}/tensorboard', pytorch=True, save=True)
        wandb.watch(self.student, log_freq=1000, idx=0, log_graph=True, log='all', criterion=self.criterion1)
        self.t_logger = TensorboardPyTorch(f'{base_path}/tensorboard', self.device)
        return save_path

    def run_epoch(self, epoch, save_path, config_run_epoch, phase):
        """
        Init df -> [Init Run -> [Run Epoch]_{IL} -> Update df]_{IL}]
        {IL - In Loop}
        Args:
            epoch (int): Current epoch
            save_path (str): Path to save the model
            config_run_epoch (): ##
            phase (train|test): Phase of training
        """
        global_loss = 0.0
        global_denom = 0.0
        running_loss = 0.0
        running_denom = 0.0
        loader_size = len(self.loaders[phase])
        wandb.log({'epoch': epoch})
        progress_bar = tqdm(self.loaders[phase], desc=f'run_epoch: {phase}',
                            mininterval=30, leave=False, total=loader_size)
        for i, data in enumerate(progress_bar):
            wandb.log({'step': i}, step=i+1)
            x_true, y_true = data['x_true'], data['y_true']

            # with torch.autocast(device_type=self.device, dtype=torch.float16):

            if 'train' in phase:
                laser = get_laser(self.student, x_true)
                loss = self.ee_method.run_train(laser, y_true)
                loss /= config_run_epoch.grad_accum_steps
                self.accelerator.backward(loss) # jedyne użycie acceleratora w trainerze, wraz z clip_grad_norm
                if (i + 1) % config_run_epoch.grad_accum_steps == 0 or (i + 1) == loader_size:
                    self.accelerator.clip_grad_norm_(filter(lambda p: p.requires_grad, self.ee_method.parameters()), 3.0)
                    self.optim.step()
                    if self.lr_scheduler is not None:
                        self.lr_scheduler.step()
                    self.optim.zero_grad()
                loss *= config_run_epoch.grad_accum_steps
            else:
                loss = self.ee_method.run_val(laser, y_true)

            wandb.log({f'every_step/ee_loss/{phase}': loss.item()}, step=i+1)

            denom = y_true.size(0)
            running_loss += loss.item() * denom
            running_denom += denom
            # loggers
            if (i + 1) % config_run_epoch.grad_accum_steps == 0 or (i + 1) == loader_size:
                tmp_loss = running_loss / running_denom
                losses = {f'running/ee_loss/{phase}': round(tmp_loss, 4)}
                progress_bar.set_postfix(losses)
                wandb.log(losses, step=i+1)

                if self.lr_scheduler is not None:
                    wandb.log({'lr_scheduler': self.lr_scheduler.get_last_lr()[0]}, step=i+1)

                global_loss += running_loss
                global_denom += global_denom
                running_loss = 0.0
                running_denom = 0.0

                if (i + 1) % config_run_epoch.save_interval == 0 or (i + 1) == loader_size:
                    self.save_student(save_path, i)

                if (i + 1) == loader_size:
                    tmp_loss = running_loss / running_denom
                    global_losses = {f'ee_loss/{phase}': round(tmp_loss, 4)}
                    wandb.log(global_losses, step=epoch)


    def save_student(self, path, step):
        """
        Save the model.
        Args:
            save_path (str): Path to save the model
            step (int): Step of the run
        """
        self.student.save_pretrained(f"{path}/model_{datetime.datetime.utcnow()}_step_{step}.pth")

    def manual_seed(self, random_seed):
        """
        Set the environment for reproducibility purposes.
        Args:
            random_seed (int): seed generator
        """
        if 'cuda' in self.device.type:
            torch.cuda.empty_cache()
            torch.backends.cudnn.deterministic = True
            torch.cuda.manual_seed_all(random_seed)
            # torch.backends.cudnn.benchmark = False
        import numpy as np
        np.random.seed(random_seed)
        torch.manual_seed(random_seed)
