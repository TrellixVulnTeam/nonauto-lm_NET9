from typing import Union
import torch
import wandb
import logging
from pathlib import Path
from loguru import logger
from .filters import Filter
from datetime import datetime
from rich.console import Console
from .base import run_on_rank_zero


class RichExceptionHandler(logging.Handler):
    """Much better Rich handler which works with Loguru."""

    def __init__(self, level: Union[int, str] = logging.NOTSET) -> None:
        super().__init__(level=level)
        self._console = Console()

    @run_on_rank_zero
    def emit(self, record: logging.LogRecord) -> None:
        self._console.print_exception(show_locals=True)


class SaveBatchHandler(logging.Handler):
    """Handler to save batches with error during training."""

    def __init__(self, level: Union[int, str] = logging.NOTSET) -> None:
        super().__init__(level=level)
        self._directory = Path.cwd() / "error-batches"
        if not self._directory.exists():
            self._directory.mkdir(exist_ok=False)

    @run_on_rank_zero
    def emit(self, record: logging.LogRecord) -> None:
        # Construct directory to save batch with error
        # We need it for better hierarchy
        save_directory = self._directory / record.extra.get("serialization_dir")
        if not save_directory.exists():
            save_directory.mkdir(exist_ok=False)
        # Use current time as an identifier of an error batch
        time = datetime.now()
        file_suffix = time.strftime("%d-%m-%Y_%H-%M")
        torch.save(
            {
                "message": record.extra.get("message", ""),
                "batch": record.extra.get("batch", torch.Tensor()),
            },
            save_directory / f"batch_{file_suffix}.pt",
        )


class WandBLoggingHandler(logging.Handler):
    """Log metrics to Weights & Biasses if needed."""

    @run_on_rank_zero
    def emit(self, record: logging.LogRecord) -> None:
        if getattr(logger, "use_wandb", False):
            wandb.log(record.extra.get("metrics"))


def setup_logging() -> None:
    logger.add(RichExceptionHandler(), level=logging.ERROR, format="{message}")
    logger.add(
        SaveBatchHandler(),
        filter=Filter(type="has_attr", condition="batch"),
        format="{message}",
    )
    logger.add(
        WandBLoggingHandler(),
        filter=Filter(type="has_attr", condition="metrics"),
        format="{message}",
    )
