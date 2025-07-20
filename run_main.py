import argparse
import torch
from accelerate import Accelerator, DeepSpeedPlugin
from accelerate import DistributedDataParallelKwargs
from torch import nn, optim
from torch.optim import lr_scheduler
from tqdm import tqdm
import time
import random
import numpy as np
import os
import json

# Add MLflow for experiment tracking
import mlflow
import mlflow.pytorch
import socket
from contextlib import nullcontext

from utils.metrics import get_loss_function, get_metric_function
from models import Autoformer, DLinear, TimeLLM
from data_provider.data_factory import data_provider
from utils.tools import del_files, EarlyStopping, adjust_learning_rate, vali, load_content

# Environment setup
os.environ['CURL_CA_BUNDLE'] = ''
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:64"
os.environ["TOKENIZERS_PARALLELISM"] = "false" # mute the warning

def main():
    parser = argparse.ArgumentParser(description='Time-LLM')

    # Basic config
    parser.add_argument('--task_name', type=str, required=True, default='long_term_forecast',
                        help='task name, options:[long_term_forecast, short_term_forecast, imputation, classification, anomaly_detection]')
    parser.add_argument('--is_training', type=int, required=True, default=1, help='status')
    parser.add_argument('--model_id', type=str, required=True, default='test', help='model id')
    parser.add_argument('--model_comment', type=str, required=True, default='none', help='prefix when saving test results')
    parser.add_argument('--model', type=str, required=True, default='Autoformer',
                        help='model name, options: [Autoformer, DLinear, TimeLLM]')
    parser.add_argument('--seed', type=int, default=2021, help='random seed')

    # Data loader
    parser.add_argument('--data', type=str, required=True, default='ETTm1', help='dataset type')
    parser.add_argument('--root_path', type=str, default='./dataset', help='root path of the data file')
    parser.add_argument('--data_path', type=str, default='ETTh1.csv', help='data file')
    parser.add_argument('--features', type=str, default='M',
                        help='forecasting task, options:[M, S, MS]; '
                         'M:multivariate predict multivariate, S: univariate predict univariate, '
                         'MS:multivariate predict univariate')
    parser.add_argument('--target', type=str, default='OT', help='target feature in S or MS task')
    parser.add_argument('--loader', type=str, default='modal', help='dataset type')
    parser.add_argument('--freq', type=str, default='d',
                        help='freq for time features encoding, '
                         'options:[s:secondly, t:minutely, h:hourly, d:daily, b:business days, w:weekly, m:monthly], '
                         'you can also use more detailed freq like 15min or 3h')
    parser.add_argument('--checkpoints', type=str, default='./checkpoints/', help='location of model checkpoints')

    # Forecasting task
    parser.add_argument('--seq_len', type=int, default=96, help='input sequence length')
    parser.add_argument('--label_len', type=int, default=48, help='start token length')
    parser.add_argument('--pred_len', type=int, default=96, help='prediction sequence length')
    parser.add_argument('--seasonal_patterns', type=str, default='Monthly', help='subset for M4')

    # Model define
    parser.add_argument('--enc_in', type=int, default=7, help='encoder input size')
    parser.add_argument('--dec_in', type=int, default=7, help='decoder input size')
    parser.add_argument('--c_out', type=int, default=7, help='output size')
    parser.add_argument('--d_model', type=int, default=16, help='dimension of model')
    parser.add_argument('--n_heads', type=int, default=8, help='num of heads')
    parser.add_argument('--e_layers', type=int, default=2, help='num of encoder layers')
    parser.add_argument('--d_layers', type=int, default=1, help='num of decoder layers')
    parser.add_argument('--d_ff', type=int, default=32, help='dimension of fcn')
    parser.add_argument('--moving_avg', type=int, default=25, help='window size of moving average')
    parser.add_argument('--factor', type=int, default=1, help='attn factor')
    parser.add_argument('--dropout', type=float, default=0.1, help='dropout')
    parser.add_argument('--embed', type=str, default='timeF', help='time features encoding, options:[timeF, fixed, learned]')
    parser.add_argument('--activation', type=str, default='gelu', help='activation')
    parser.add_argument('--output_attention', action='store_true', help='whether to output attention in encoder')
    parser.add_argument('--patch_len', type=int, default=16, help='patch length')
    parser.add_argument('--stride', type=int, default=8, help='stride')
    parser.add_argument('--prompt_domain', type=int, default=0, help='')
    parser.add_argument('--llm_model', type=str, default='LLAMA', help='LLM model') # LLAMA, GPT2, BERT
    parser.add_argument('--llm_dim', type=int, default='4096', help='LLM model dimension') # LLama7b:4096; GPT2-small:768; BERT-base:768

    # Optimization
    parser.add_argument('--num_workers', type=int, default=10, help='data loader num workers')
    parser.add_argument('--itr', type=int, default=1, help='experiments times')
    parser.add_argument('--train_epochs', type=int, default=10, help='train epochs')
    parser.add_argument('--align_epochs', type=int, default=10, help='alignment epochs')
    parser.add_argument('--batch_size', type=int, default=32, help='batch size of train input data')
    parser.add_argument('--eval_batch_size', type=int, default=8, help='batch size of model evaluation')
    parser.add_argument('--patience', type=int, default=10, help='early stopping patience')
    parser.add_argument('--learning_rate', type=float, default=0.0001, help='optimizer learning rate')
    parser.add_argument('--des', type=str, default='test', help='exp description')
    parser.add_argument('--loss', type=str, default='MSE', help='loss function for training')
    parser.add_argument('--metric', type=str, default='MAE', help='metric for evaluation')
    parser.add_argument('--lradj', type=str, default='type1', help='adjust learning rate')
    parser.add_argument('--pct_start', type=float, default=0.2, help='pct_start')
    parser.add_argument('--use_amp', action='store_true', help='use automatic mixed precision training', default=False)
    parser.add_argument('--llm_layers', type=int, default=6)
    parser.add_argument('--percent', type=int, default=100)
    parser.add_argument('--num_tokens', type=int, default=1000, help='number of tokens for mapping layer')
    
    args = parser.parse_args()

    # Set random seeds for reproducibility
    fix_seed = args.seed
    random.seed(fix_seed)
    torch.manual_seed(fix_seed)
    np.random.seed(fix_seed)

    # Initialize Accelerator
    ddp_kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    deepspeed_plugin = DeepSpeedPlugin(hf_ds_config='./config/ds_config_zero2.json')
    accelerator = Accelerator(kwargs_handlers=[ddp_kwargs], deepspeed_plugin=deepspeed_plugin)

    # --- MLflow Integration ---    
    # Setting the Context Manager to avoid creating duplicate MLflow runs
    if accelerator.is_main_process:
        mlflow.set_experiment(args.llm_model)
        run_context = mlflow.start_run(run_name=args.model_id)
        # --- Log Hostname as a Tag ---
        hostname = socket.gethostname()
        mlflow.set_tag("hostname", hostname)
    else:
        run_context = nullcontext()

    with run_context:
        if accelerator.is_local_main_process:
            mlflow.log_params(vars(args))

        for ii in range(args.itr):
            # No need for the 'setting' string anymore, as MLflow handles run naming and parameter logging.
            
            train_data, train_loader = data_provider(args, 'train')
            vali_data, vali_loader = data_provider(args, 'val')
            test_data, test_loader = data_provider(args, 'test')

            if args.model == 'Autoformer':
                model = Autoformer.Model(args).float()
            elif args.model == 'DLinear':
                model = DLinear.Model(args).float()
            elif args.model == 'TimeLLM':
                model = TimeLLM.Model(args).float()
            else:
                raise ValueError(f"Model {args.model} not recognized.")
            
            # Early stopping needs a checkpoint path, which MLflow can provide, or we save temporarily.
            # For simplicity, we'll create a temporary path for early stopping checkpoints.
            temp_checkpoint_path = os.path.join(args.checkpoints, args.model_id)
            if accelerator.is_local_main_process:
                os.makedirs(temp_checkpoint_path, exist_ok=True)
        
            time_now = time.time()
            train_steps = len(train_loader)
            early_stopping = EarlyStopping(accelerator=accelerator, patience=args.patience)

            trained_parameters = [p for p in model.parameters() if p.requires_grad]
            model_optim = optim.Adam(trained_parameters, lr=args.learning_rate)

            if args.lradj == 'COS':
                scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(model_optim, T_max=20, eta_min=1e-8)
            else:
                scheduler = lr_scheduler.OneCycleLR(optimizer=model_optim,
                                                    steps_per_epoch=train_steps,
                                                    pct_start=args.pct_start,
                                                    epochs=args.train_epochs,
                                                    max_lr=args.learning_rate)

            criterion = get_loss_function(args.loss)
            metric_func = get_metric_function(args.metric)

            train_loader, vali_loader, test_loader, model, model_optim, scheduler = accelerator.prepare(
                train_loader, vali_loader, test_loader, model, model_optim, scheduler)

            if args.use_amp:
                scaler = torch.cuda.amp.GradScaler()

            for epoch in range(args.train_epochs):
                iter_count = 0
                train_loss = []

                model.train()
                epoch_time = time.time()
                for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in tqdm(enumerate(train_loader), disable=not accelerator.is_local_main_process):
                    iter_count += 1
                    model_optim.zero_grad()

                    batch_x = batch_x.float().to(accelerator.device)
                    batch_y = batch_y.float().to(accelerator.device)
                    batch_x_mark = batch_x_mark.float().to(accelerator.device)
                    batch_y_mark = batch_y_mark.float().to(accelerator.device)

                    # Decoder input
                    dec_inp = torch.zeros_like(batch_y[:, -args.pred_len:, :]).float()
                    dec_inp = torch.cat([batch_y[:, :args.label_len, :], dec_inp], dim=1).float().to(accelerator.device)

                    # Training logic
                    if args.use_amp:
                        with torch.cuda.amp.autocast():
                            outputs = model(batch_x, batch_x_mark, dec_inp, batch_y_mark)
                            if args.output_attention:
                                outputs = outputs[0]
                            
                            f_dim = -1 if args.features == 'MS' else 0
                            loss = criterion(outputs[:, -args.pred_len:, f_dim:], batch_y[:, -args.pred_len:, f_dim:])
                    else:
                        outputs = model(batch_x, batch_x_mark, dec_inp, batch_y_mark)
                        if args.output_attention:
                            outputs = outputs[0]
                        
                        f_dim = -1 if args.features == 'MS' else 0
                        loss = criterion(outputs[:, -args.pred_len:, f_dim:], batch_y[:, -args.pred_len:, f_dim:])
                    
                    train_loss.append(loss.item())

                    if args.use_amp:
                        scaler.scale(loss).backward()
                        scaler.step(model_optim)
                        scaler.update()
                    else:
                        accelerator.backward(loss)
                        model_optim.step()
                    
                    # 'TST' Scheduler should be stepped after each batch.
                    if args.lradj == 'TST':
                        adjust_learning_rate(accelerator, model_optim, scheduler, epoch + 1, args, printout=False)
                        scheduler.step()

                    if (i + 1) % 100 == 0:
                        accelerator.print(f"\titers: {i + 1}, epoch: {epoch + 1} | loss: {loss.item():.7f}")
                        speed = (time.time() - time_now) / iter_count
                        left_time = speed * ((args.train_epochs - epoch) * train_steps - i)
                        accelerator.print(f'\tspeed: {speed:.4f}s/iter; left time: {left_time:.4f}s')
                        iter_count = 0
                        time_now = time.time()
                
                accelerator.print(f"Epoch: {epoch + 1} cost time: {time.time() - epoch_time:.4f}s")
                train_loss = np.average(train_loss)
                vali_loss, vali_metric = vali(args, accelerator, model, vali_data, vali_loader, criterion, metric_func)
                test_loss, test_metric = vali(args, accelerator, model, test_data, test_loader, criterion, metric_func)
                
                accelerator.print(f"Epoch: {epoch + 1} | Train Loss: {train_loss:.7f} Vali Loss: {vali_loss:.7f} Test Loss: {test_loss:.7f}")
                accelerator.print(f"{args.metric} Metric: {test_metric:.7f}")

                # --- MLflow Metric Logging ---
                if accelerator.is_local_main_process:
                    metrics_to_log = {
                        f"train_{args.loss.lower()}_loss": train_loss,
                        f"vali_{args.loss.lower()}_loss": vali_loss,
                        f"vali_{args.metric.lower()}_metric": vali_metric,
                        f"test_{args.loss.lower()}_loss": test_loss,
                        f"test_{args.metric.lower()}_metric": test_metric
                    }
                    mlflow.log_metrics(metrics_to_log, step=epoch)

                
                early_stopping(vali_loss, model, temp_checkpoint_path)
                if early_stopping.early_stop:
                    accelerator.print("Early stopping")
                    break

                # Learning rate adjustment
                if args.lradj != 'TST':
                    if args.lradj == 'COS':
                        scheduler.step()
                        accelerator.print(f"lr = {model_optim.param_groups[0]['lr']:.10f}")
                    else:
                        if epoch == 0:
                            args.learning_rate = model_optim.param_groups[0]['lr']
                            accelerator.print(f"lr = {model_optim.param_groups[0]['lr']:.10f}")
                        adjust_learning_rate(accelerator, model_optim, scheduler, epoch + 1, args, printout=True)
                else:
                    accelerator.print(f'Updating learning rate to {scheduler.get_last_lr()[0]}')

        
            # --- MLflow Model Artifact Logging ---
            accelerator.wait_for_everyone()
            if accelerator.is_local_main_process:
                # Unwrap the model to save the raw state_dict
                unwrapped_model = accelerator.unwrap_model(model)

                # Filter out frozen parameters (I hope this works correctly)
                state_dict = {
                    k: v for k, v in unwrapped_model.state_dict().items()
                    if unwrapped_model.get_parameter(k).requires_grad
                }
                
                mlflow.pytorch.log_state_dict(state_dict, artifact_path="model_state_dict")
                accelerator.print(f"Model '{args.model_id}' has been logged to MLflow.")
                
                # Save scaler for inference
                import pickle
                import tempfile
                scaler = train_data.scaler if hasattr(train_data, 'scaler') else None
                if scaler is not None:
                    with tempfile.NamedTemporaryFile(mode='wb', suffix=".pkl", delete=False) as tmp:
                        pickle.dump(scaler, tmp)
                        mlflow.log_artifact(tmp.name, "scaler.pkl")
                    os.remove(tmp.name)
                    accelerator.print(f"Scaler has been logged to MLflow.")
                
                # Clean up temporary early stopping checkpoints
                if os.path.exists(temp_checkpoint_path):
                    del_files(temp_checkpoint_path)
                


if __name__ == "__main__":
    main()