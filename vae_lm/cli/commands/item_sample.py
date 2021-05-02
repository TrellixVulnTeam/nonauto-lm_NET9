from typing import List
import json
from pathlib import Path
from loguru import logger
from torch_nlp_utils.data import DatasetReader, CollateBatch, Batch
from vae_lm.training.utils import load_archive, log_metrics
from cleo import option, argument, Command


class ItemSampleCommand(Command):
    name = "item-sample"
    description = "Sample texts from VAE Language Model based on input sample."
    arguments = [argument("archive", description="Path to archive with trained model.")]
    options = [
        option(
            "items",
            None,
            description="Items to use for sampling",
            flag=False,
            value_required=False,
        ),
        option(
            "num-samples",
            "s",
            description="Num latent samples to generate.",
            flag=False,
            value_required=False,
        ),
        option(
            "log-model-info",
            None,
            description=(
                "Whether to log model information like its config and metrics or not."
            ),
            flag=True,
            value_required=False,
        ),
        option(
            "random",
            None,
            description="Wether to make random sampling in posterior or just use mean.",
            flag=True,
            value_required=False,
        ),
    ]

    def handle(self) -> None:
        # Load archive
        archive = load_archive(Path(self.argument("archive")))
        vocab = archive.model.vocab
        if self.option("log-model-info"):
            # Log model config
            logger.info(
                "Config: {}".format(json.dumps(archive.config.as_flat_dict(), indent=2, ensure_ascii=False))
            )
            # Log model metrics
            log_metrics("Trained model", archive.metrics)
        # Parse options
        num_samples = int(self.option("num-samples"))
        items = self.parse_items()
        # Prepare data for the model
        dataset_reader_params = archive.config.get("dataset_reader")
        dataset_reader_params["sample_masking"] = False
        dataset_reader = DatasetReader.from_params(**dataset_reader_params)
        collate_batch = CollateBatch.by_name(dataset_reader_params.get("type"))
        input_dict = collate_batch(
            Batch([vocab.encode(dataset_reader.item_to_instance(item)) for item in items])
        ).as_dict()
        # Set posterior samples
        archive.model.set_samples(num_samples)
        # Run it
        output_dict = archive.model(**input_dict, random=self.option("random"))
        # Make it readable
        samples = archive.model.make_output_human_readable(output_dict)
        print(samples)

    def parse_items(self) -> List[str]:
        items = self.option("items")
        return items.split(",")
