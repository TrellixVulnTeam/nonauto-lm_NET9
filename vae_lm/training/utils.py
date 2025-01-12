from typing import NamedTuple, Dict, Callable, Union
import os
import json
import wandb
import torch
import random
import shutil
import tarfile
import tempfile
import numpy as np
from pathlib import Path
from loguru import logger
from copy import deepcopy
from functools import wraps
import torch.distributed as dist
import vae_lm.training.ddp as ddp
from contextlib import contextmanager
from torch_nlp_utils.common import Params
from vae_lm.utils.base import run_on_rank_zero

# Modules
from vae_lm.models.base import VAELmModel


CONFIG_NAME = "config.json"
WEIGHTS_NAME = "weights.pt"
METRICS_NAME = "metrics.json"


class TorchBatchError(Exception):
    """
    This exception is raised during any batch processing.
    It has an attribute `batch` to get a tensor that raised an error.
    """

    def __init__(self, message: str, batch: torch.Tensor):
        self.message = message
        self.batch = batch

    def __str__(self):
        return repr(self.message)


class Archive(NamedTuple):
    """An archive comprises a Model and its experimental config with metrics."""

    model: VAELmModel
    config: Params
    metrics: Dict[str, float]


def archive_model(
    serialization_dir: Path,
    weights: Path,
    archive_path: Path = None,
) -> None:
    """
    Archive the model weights, its training configuration, and its vocabulary to `model.tar.gz`.

    Parameters
    ----------
    serialization_dir : `Path`, required
        The directory where the weights and vocabulary are written out.
    weights : `Path`, required
        Which weights file to include in the archive. The default is `best.th`.
    archive_path : `str`, optional, (default = `None`)
        A full path to serialize the model to. The default is "model.tar.gz" inside the
        serialization_dir. If you pass a directory here, we'll serialize the model
        to "model.tar.gz" inside the directory.
    """
    # Check weights
    weights_file = weights / "model.pt"
    if not weights_file.exists():
        logger.error(f"weights file {weights_file} does not exist, unable to archive model.")
        return
    # Check metrics
    metrics_file = weights / METRICS_NAME
    if not metrics_file.exists():
        logger.error(f"metrics file {metrics_file} does not exist, unable to archive model.")
        return
    # Check config
    config_file = serialization_dir / CONFIG_NAME
    if not config_file.exists():
        logger.error(f"config file {config_file} does not exist, unable to archive model.")
    # Check archive path
    if archive_path is not None:
        archive_file = archive_path
        if archive_file.is_dir():
            archive_file = archive_file / "model.tar.gz"
    else:
        archive_file = serialization_dir / "model.tar.gz"
    logger.info(f"Archiving data to {archive_file}.")
    with tarfile.open(archive_file, "w:gz") as archive:
        archive.add(config_file, arcname=CONFIG_NAME)
        archive.add(weights_file, arcname=WEIGHTS_NAME)
        archive.add(metrics_file, arcname=METRICS_NAME)
        archive.add(str(serialization_dir / "vocabulary"), arcname="vocabulary")


def load_archive(
    archive_file: Path,
    cuda_device: int = -1,
) -> Archive:
    """
    Instantiates an Archive from an archived `tar.gz` file.

    Parameters
    ----------
    archive_file : `Path`, required
        The archive file to load the model from.
    cuda_device : `int`, optional (default = `-1`)
        If `cuda_device` is >= 0, the model will be loaded onto the
        corresponding GPU. Otherwise it will be loaded onto the CPU.
    """
    logger.info(f"Loading archive file {archive_file}")
    tempdir = None
    try:
        if archive_file.is_dir():
            serialization_dir = archive_file
        else:
            with extracted_archive(archive_file, cleanup=False) as tempdir:
                serialization_dir = Path(tempdir)
        weights_path = serialization_dir / WEIGHTS_NAME
        # Load config
        config = Params.from_file(str(serialization_dir / CONFIG_NAME))
        # Load metrics
        with (serialization_dir / METRICS_NAME).open("r", encoding="utf-8") as file:
            metrics = json.load(file)
        # Instantiate model. Use a duplicate of the config, as it will get consumed.
        model_params = config.duplicate()
        model_params["vocabulary"] = str(serialization_dir / "vocabulary")
        model = VAELmModel.load(
            model_params,
            weights=weights_path,
            device=cuda_device,
        )
    finally:
        if tempdir is not None:
            logger.info(f"Removing temporary unarchived model dir at {tempdir}")
            shutil.rmtree(tempdir, ignore_errors=True)
    return Archive(
        model=model,
        config=config,
        metrics=metrics,
    )


@contextmanager
def extracted_archive(resolved_archive_file, cleanup=True):
    tempdir = None
    try:
        tempdir = tempfile.mkdtemp()
        logger.info(f"Extracting archive file {resolved_archive_file} to temp dir {tempdir}")
        with tarfile.open(resolved_archive_file, "r:gz") as archive:
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(archive, tempdir)
        yield tempdir
    finally:
        if tempdir is not None and cleanup:
            logger.info(f"Removing temporary unarchived model dir at {tempdir}")
            shutil.rmtree(tempdir, ignore_errors=True)


def seed_everything(seed: int) -> None:
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True


def configure_world(func: Callable) -> Callable:
    """Decorator to configure Distributed Training world and wandb if needed for function."""

    @wraps(func)
    def wrapper(process_rank: int, config: Params, world_size: int = 1, **kwargs) -> None:
        # Set info related to process rank
        is_master = process_rank == 0
        os.environ["LOCAL_RANK"] = str(process_rank)
        use_wandb = config.pop("use_wandb", False)
        serialization_dir = Path(config["serialization_dir"])
        # Setup world for Distributed Training
        if world_size > 1:
            ddp.setup_world(process_rank, world_size, backend=dist.Backend.NCCL)
        # Run wandb in master process
        # TODO: Think about config unflat for wandb sweep to work for hyperparameters optimization.
        if is_master and use_wandb:
            logger.use_wandb = True
            wandb.init(
                project=os.getenv("WANDB_PROJECT_NAME"),
                config=config.as_flat_dict(),
                reinit=True,
                tags=config.pop("tags"),
            )
        # Run function
        try:
            result = func(process_rank=process_rank, config=config, world_size=world_size, **kwargs)
        except Exception as error:
            # If it is a TorchBatchError then save it for convenience
            if isinstance(error, TorchBatchError):
                logger.bind(
                    batch=error.batch, serialization_dir=serialization_dir.stem, message=str(error)
                ).debug("Saving batch that caused an error")
            logger.error(error)
            result = {}
        finally:
            if is_master:
                # Construct archive in distributed training there
                # because wandb hangs in distributed training mode
                # and we also need to finish it manually.
                best_model = serialization_dir / "best-model"
                if best_model.exists():
                    archive_model(
                        serialization_dir=serialization_dir,
                        weights=best_model,
                    )
                if use_wandb:
                    # Save archived model to wandb if exists
                    if best_model.exists():
                        wandb.save(str(serialization_dir / "model.tar.gz"))
                    wandb.finish()
        return result

    wrapper.original = func
    return wrapper


def description_from_metrics(metrics: Dict[str, float]) -> str:
    # Copy dict for safety
    metrics = deepcopy(metrics)
    # Configure loss first
    loss = f"loss: {metrics.pop('loss'):.4f}, "
    return loss + ", ".join([f"{name}: {value:.4f}" for name, value in metrics.items()]) + " ||"


@run_on_rank_zero
def log_metrics(
    mode_str: str,
    metrics: Dict[str, float],
    info: Dict[str, Union[float, int, str]] = None,
) -> None:
    """
    Pretty log metrics and sort them by length and alphabetic order.

    Parameters
    ----------
    mode_str : `str`, required
        Mode string. Usually train or validation.
    metrics : `Dict[str, float]`, required
        Dictionary of metrics.
    info : `Dict[str, Union[float, int, str]]`, optional (default = `None`)
        Info to additionally log after and epoch.
    """
    logger.info(
        f"{mode_str}: info -- {', '.join([f'{k}: {v}'.lower() for k, v in info.items()])}"
        if info is not None
        else f"{mode_str}"
    )
    max_length = max(len(x) for x in metrics)
    # Sort by length to make it prettier
    for metric in sorted(metrics, key=lambda x: (len(x), x)):
        metric_value = metrics.get(metric)
        # Log only numbers to stdout as with additional loggers
        # we might want to log DataFrames, distributions and etc.
        if isinstance(metric_value, (float, int)):
            logger.info(f"{metric.ljust(max_length)} | {metric_value:.4f}")
    logger.bind(metrics={f"{mode_str.lower()}/{k}": v for k, v in metrics.items()}).debug(
        "Logging metrics to additional sources."
    )
