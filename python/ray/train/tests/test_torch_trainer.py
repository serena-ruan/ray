import contextlib
import uuid

import pytest
import time
import torch
import os

import ray
from ray.train.examples.pytorch.torch_linear_example import (
    train_func as linear_train_func,
)
from ray.train.constants import DISABLE_LAZY_CHECKPOINTING_ENV
from ray.train.torch import TorchPredictor, TorchTrainer
from ray.train import RunConfig, ScalingConfig
from ray.train.torch import TorchConfig
from ray.train.trainer import TrainingFailedError
import ray.train as train
from unittest.mock import patch
from ray.cluster_utils import Cluster
from ray.train.tests.dummy_preprocessor import DummyPreprocessor
from ray.train.torch.torch_checkpoint import LegacyTorchCheckpoint


@pytest.fixture
def ray_start_4_cpus():
    address_info = ray.init(num_cpus=4)
    yield address_info
    # The code after the yield will run as teardown code.
    ray.shutdown()


@contextlib.contextmanager
def ray_start_2_node_cluster(num_cpus_per_node: int, num_gpus_per_node: int):
    cluster = Cluster()
    for _ in range(2):
        cluster.add_node(num_cpus=num_cpus_per_node, num_gpus=num_gpus_per_node)

    ray.init(address=cluster.address)

    yield

    ray.shutdown()
    cluster.shutdown()


@pytest.mark.parametrize("num_workers", [1, 2])
def test_torch_linear(ray_start_4_cpus, num_workers):
    def train_func(config):
        result = linear_train_func(config)
        assert len(result) == epochs
        assert result[-1]["loss"] < result[0]["loss"]

    num_workers = num_workers
    epochs = 3
    scaling_config = ScalingConfig(num_workers=num_workers)
    config = {"lr": 1e-2, "hidden_size": 1, "batch_size": 4, "epochs": epochs}
    trainer = TorchTrainer(
        train_loop_per_worker=train_func,
        train_loop_config=config,
        scaling_config=scaling_config,
    )
    trainer.fit()


@pytest.mark.parametrize("prepare_model", (True, False))
def test_torch_e2e(ray_start_4_cpus, prepare_model):
    def train_func():
        model = torch.nn.Linear(3, 1)
        if prepare_model:
            model = train.torch.prepare_model(model)
        train.report({}, checkpoint=LegacyTorchCheckpoint.from_model(model))

    scaling_config = ScalingConfig(num_workers=2)
    trainer = TorchTrainer(
        train_loop_per_worker=train_func,
        scaling_config=scaling_config,
        preprocessor=DummyPreprocessor(),
    )
    result = trainer.fit()
    assert isinstance(result.checkpoint.get_preprocessor(), DummyPreprocessor)


@pytest.mark.parametrize("prepare_model", (True, False))
def test_torch_e2e_state_dict(ray_start_4_cpus, prepare_model):
    def train_func():
        model = torch.nn.Linear(3, 1)
        if prepare_model:
            model = train.torch.prepare_model(model)
        train.report(
            {}, checkpoint=LegacyTorchCheckpoint.from_state_dict(model.state_dict())
        )

    scaling_config = ScalingConfig(num_workers=2)
    trainer = TorchTrainer(
        train_loop_per_worker=train_func,
        scaling_config=scaling_config,
        preprocessor=DummyPreprocessor(),
    )
    result = trainer.fit()
    isinstance(result.checkpoint.get_preprocessor(), DummyPreprocessor)

    # If loading from a state dict, a model definition must be passed in.
    with pytest.raises(ValueError):
        TorchPredictor.from_checkpoint(result.checkpoint)


# We can't really test for prepare_model here as we can't detect what the user
# has saved without loading (and thus triggering the exception anyway)
@pytest.mark.parametrize("lazy_checkpointing", (True, False))
def test_torch_e2e_dir(ray_start_4_cpus, tmpdir, lazy_checkpointing):
    def train_func():
        model = torch.nn.Linear(3, 1)
        torch.save(model, os.path.join(tmpdir, "model"))
        train.report({}, checkpoint=LegacyTorchCheckpoint.from_directory(tmpdir))

    scaling_config = ScalingConfig(num_workers=2)
    with patch.dict(
        os.environ, {DISABLE_LAZY_CHECKPOINTING_ENV: str(int(not lazy_checkpointing))}
    ):
        trainer = TorchTrainer(
            train_loop_per_worker=train_func,
            scaling_config=scaling_config,
            preprocessor=DummyPreprocessor(),
        )
        result = trainer.fit()
    isinstance(result.checkpoint.get_preprocessor(), DummyPreprocessor)

    # TODO(ml-team): Add a way for LegacyTorchCheckpoint to natively support
    # models from files
    class TorchScorer:
        def __init__(self):
            with result.checkpoint.as_directory() as checkpoint_path:
                model = torch.load(os.path.join(checkpoint_path, "model"))
            preprocessor = result.checkpoint.get_preprocessor()
            self.pred = TorchPredictor.from_checkpoint(
                LegacyTorchCheckpoint.from_model(model, preprocessor=preprocessor)
            )

        def __call__(self, x):
            return self.pred.predict(x, dtype=torch.float)

    predict_dataset = ray.data.range(9)
    predictions = predict_dataset.map_batches(
        TorchScorer,
        batch_size=3,
        batch_format="pandas",
        compute=ray.data.ActorPoolStrategy(),
    )
    assert predictions.count() == 3


def test_checkpoint_freq(ray_start_4_cpus):
    # checkpoint_freq is not supported so raise an error
    trainer = TorchTrainer(
        train_loop_per_worker=lambda config: None,
        scaling_config=train.ScalingConfig(num_workers=1),
        run_config=train.RunConfig(
            checkpoint_config=train.CheckpointConfig(
                checkpoint_frequency=2,
            ),
        ),
    )
    with pytest.raises(ValueError):
        trainer.fit()


def test_torch_session_errors(ray_start_4_cpus):
    """Test fail-fast behavior when reporting dicts with Torch tensors"""

    def train_func():
        model = torch.nn.Linear(1, 1).state_dict()
        with pytest.raises(ValueError):
            train.report(model)

    scaling_config = ScalingConfig(num_workers=2)
    trainer = TorchTrainer(
        train_loop_per_worker=train_func,
        scaling_config=scaling_config,
    )
    trainer.fit()


def test_single_worker_failure(ray_start_4_cpus):
    """Tests if training fails upon any worker failure."""

    def single_worker_fail():
        if train.get_context().get_world_rank() == 0:
            raise RuntimeError
        else:
            time.sleep(1000000)

    scaling_config = ScalingConfig(num_workers=2)
    trainer = TorchTrainer(
        train_loop_per_worker=single_worker_fail,
        scaling_config=scaling_config,
    )
    with pytest.raises(TrainingFailedError) as exc_info:
        trainer.fit()
    assert isinstance(exc_info.value.__cause__, RuntimeError)


# See comment in backend.py::_warn_about_bad_checkpoint_type
# for why test_torch_bad_checkpoint_warning is commented out

# def test_torch_bad_checkpoint_warning(ray_start_4_cpus):
#     """Test that a warning is printed if bad checkpoint type is used."""

#     def train_func():
#         model = torch.nn.Linear(1, 1).state_dict()
#         train.report({}, checkpoint=LegacyTorchCheckpoint.from_dict({"model": model}))

#     scaling_config = ScalingConfig(num_workers=2)
#     trainer = TorchTrainer(
#         train_loop_per_worker=train_func,
#         scaling_config=scaling_config,
#     )
#     output = io.StringIO()
#     with redirect_stdout(output), redirect_stderr(output):
#         trainer.fit()
#     output = output.getvalue()
#     assert "You have reported a checkpoint" not in output

#     def train_func():
#         model = torch.nn.Linear(1, 1).state_dict()
#         train.report({}, checkpoint=Checkpoint.from_dict({"model": model}))

#     trainer = TorchTrainer(
#         train_loop_per_worker=train_func,
#         scaling_config=scaling_config,
#     )
#     output = io.StringIO()
#     with redirect_stdout(output), redirect_stderr(output):
#         trainer.fit()
#     output = output.getvalue()
#     assert "You have reported a checkpoint" in output


@pytest.mark.parametrize("num_gpus_per_worker", [0.5, 1, 2])
def test_tune_torch_get_device_gpu(num_gpus_per_worker):
    """Tests if GPU ids are set correctly when running train concurrently in nested actors
    (for example when used with Tune).
    """
    from ray.train import ScalingConfig

    num_samples = 2
    num_workers = 2

    # We should have exactly enough resources in the cluster to run both samples
    # concurrently.
    total_gpus_required = num_workers * num_gpus_per_worker * num_samples
    # Divide by two because of a 2 node cluster.
    gpus_per_node = total_gpus_required // 2

    exception = None
    # Use the same number of cpus per node as gpus per node.
    with ray_start_2_node_cluster(
        num_cpus_per_node=gpus_per_node, num_gpus_per_node=gpus_per_node
    ):

        @patch("torch.cuda.is_available", lambda: True)
        def train_fn():
            # We use STRICT_SPREAD strategy to force multiple samples on the same node.
            # For single or fractional GPU case, each worker has only 1 visible device (
            # the other is taken by the other sample) so device index should be 0.
            # For the multiple GPU case, each worker has 2 visible devices so device
            # index should be either 0 or 1. It doesn't matter which.
            devices = train.torch.get_device()
            if isinstance(devices, list):
                assert sorted([device.index for device in devices]) == [0, 1]
            else:
                assert train.torch.get_device().index == 0

        @ray.remote(num_cpus=0)
        class TrialActor:
            def __init__(self, warmup_steps):
                self.trainer = TorchTrainer(
                    train_fn,
                    torch_config=TorchConfig(backend="gloo"),
                    run_config=RunConfig(
                        # Use a unique name to avoid using the same
                        # experiment directory
                        name=f"test_tune_torch_get_device_gpu_{uuid.uuid4()}"
                    ),
                    scaling_config=ScalingConfig(
                        num_workers=num_workers,
                        use_gpu=True,
                        resources_per_worker={"CPU": 1, "GPU": num_gpus_per_worker},
                        # Need to specify 0 trainer resources so STRICT_SPREAD
                        # will work.
                        trainer_resources={"CPU": 0},
                        placement_strategy="STRICT_SPREAD",
                        # Each gpu worker will be spread onto separate nodes. This
                        # forces different samples to run concurrently on the same
                        # node.
                    ),
                )

            def run(self):
                return self.trainer.fit()

        try:
            actors = [TrialActor.remote(1) for _ in range(num_samples)]
            ray.get([actor.run.remote() for actor in actors])
        except Exception as exc:
            exception = exc

    # Raise exception after Ray cluster has been shutdown to avoid corrupted state
    if exception:
        raise exception


def test_torch_auto_unwrap(ray_start_4_cpus):
    """Tests if underlying model from DDP is extracted when saving ckpt."""

    def train_fn():
        model = torch.nn.Linear(1, 1)

        # Wrap in DDP.
        model = train.torch.prepare_model(model)

        # Save DDP wrapped model.
        train.report({}, checkpoint=LegacyTorchCheckpoint.from_model(model))

    trainer = TorchTrainer(
        train_loop_per_worker=train_fn,
        scaling_config=ScalingConfig(num_workers=2),
    )
    results = trainer.fit()

    last_checkpoint = results.checkpoint
    model = last_checkpoint.get_model()
    assert isinstance(model, torch.nn.Module) and not isinstance(
        model, torch.nn.parallel.DistributedDataParallel
    )


def test_torch_amp(ray_start_4_cpus):
    def train_fn():
        train.torch.accelerate(amp=True)
        model = torch.nn.Linear(1, 1)
        model = train.torch.prepare_model(model)

        train.report({}, checkpoint=LegacyTorchCheckpoint.from_model(model))

    trainer = TorchTrainer(
        train_fn,
        scaling_config=ScalingConfig(num_workers=2),
    )
    results = trainer.fit()
    assert results.checkpoint


def test_torch_amp_with_custom_get_state(ray_start_4_cpus):
    """Tests amp with a model that has a custom __getstate__ method defined.

    See https://discuss.ray.io/t/ray-train-hangs-for-long-time/6333/7
    """

    def train_fn():
        train.torch.accelerate(amp=True)

        class CustomLinear(torch.nn.Linear):
            def __getstate__(self):
                return self.__dict__.copy()

        model = CustomLinear(1, 1)
        model = train.torch.prepare_model(model)

        # Make sure model is serializable even with amp enabled.
        train.report({}, checkpoint=LegacyTorchCheckpoint.from_model(model))

    trainer = TorchTrainer(
        train_fn,
        scaling_config=ScalingConfig(num_workers=2),
    )
    results = trainer.fit()
    assert results.checkpoint


def test_torch_env_vars(ray_start_4_cpus):
    """Check that env vars are set as expected."""

    def train_func(config):
        context = train.get_context()
        assert os.environ["LOCAL_RANK"] == str(context.get_local_rank())
        assert os.environ["RANK"] == str(context.get_world_rank())
        assert os.environ["LOCAL_WORLD_SIZE"] == str(context.get_local_world_size())
        assert os.environ["WORLD_SIZE"] == str(context.get_world_size())
        assert os.environ["NODE_RANK"] == str(context.get_node_rank())

        assert os.environ["ACCELERATE_TORCH_DEVICE"] == str(train.torch.get_device())

    num_workers = 1
    scaling_config = ScalingConfig(num_workers=num_workers)
    trainer = TorchTrainer(
        train_loop_per_worker=train_func,
        scaling_config=scaling_config,
    )
    trainer.fit()


def test_nonserializable_train_function(ray_start_4_cpus):
    import threading

    lock = threading.Lock()

    def train_func():
        print(lock)

    trainer = TorchTrainer(train_func)
    # Check that the `inspect_serializability` trace was printed
    with pytest.raises(TypeError, match=r".*was found to be non-serializable.*"):
        trainer.fit()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-x", __file__]))
