import os
from shutil import move

import numpy as np
import pytest
import tensorflow as tf

from doctr import datasets
from doctr.datasets import DataLoader
from doctr.transforms import Resize


def _validate_dataset(ds, input_size, batch_size=2, relative_coords=True, class_indices=False):

    # Fetch one sample
    img, target = ds[0]
    assert isinstance(img, tf.Tensor)
    assert img.shape == (*input_size, 3)
    assert img.dtype == tf.float32
    assert isinstance(target, dict)
    assert isinstance(target['boxes'], np.ndarray)
    if relative_coords:
        assert np.all((target['boxes'][:, :4] <= 1) & (target['boxes'][:, :4] >= 0))
    if class_indices:
        assert isinstance(target['labels'], np.ndarray) and target['labels'].dtype == np.int64
    else:
        assert isinstance(target['labels'], list) and all(isinstance(s, str) for s in target['labels'])
    assert len(target['labels']) == len(target['boxes'])

    # Check batching
    loader = DataLoader(ds, batch_size=batch_size)

    images, targets = next(iter(loader))
    assert isinstance(images, tf.Tensor) and images.shape == (batch_size, *input_size, 3)
    assert isinstance(targets, list) and all(isinstance(elt, dict) for elt in targets)


def test_detection_dataset(mock_image_folder, mock_detection_label):

    input_size = (1024, 1024)

    ds = datasets.DetectionDataset(
        img_folder=mock_image_folder,
        label_path=mock_detection_label,
        img_transforms=Resize(input_size),
    )

    assert len(ds) == 5
    img, target = ds[0]
    assert isinstance(img, tf.Tensor)
    assert img.shape[:2] == input_size
    assert img.dtype == tf.float32
    # Bounding boxes
    assert isinstance(target, np.ndarray) and target.dtype == np.float32
    assert np.all(np.logical_and(target[:, :4] >= 0, target[:, :4] <= 1))
    assert target.shape[1] == 4

    loader = DataLoader(ds, batch_size=2)
    images, targets = next(iter(loader))
    assert isinstance(images, tf.Tensor) and images.shape == (2, *input_size, 3)
    assert isinstance(targets, list) and all(isinstance(elt, np.ndarray) for elt in targets)

    # Rotated DS
    rotated_ds = datasets.DetectionDataset(
        img_folder=mock_image_folder,
        label_path=mock_detection_label,
        img_transforms=Resize(input_size),
        rotated_bbox=True
    )
    _, r_target = rotated_ds[0]
    assert r_target.shape[1] == 5

    # File existence check
    img_name, _ = ds.data[0]
    move(os.path.join(ds.root, img_name), os.path.join(ds.root, "tmp_file"))
    with pytest.raises(FileNotFoundError):
        datasets.DetectionDataset(mock_image_folder, mock_detection_label)
    move(os.path.join(ds.root, "tmp_file"), os.path.join(ds.root, img_name))


def test_recognition_dataset(mock_image_folder, mock_recognition_label):
    input_size = (32, 128)
    ds = datasets.RecognitionDataset(
        img_folder=mock_image_folder,
        labels_path=mock_recognition_label,
        img_transforms=Resize(input_size, preserve_aspect_ratio=True),
    )
    assert len(ds) == 5
    image, label = ds[0]
    assert isinstance(image, tf.Tensor)
    assert image.shape[:2] == input_size
    assert image.dtype == tf.float32
    assert isinstance(label, str)

    loader = DataLoader(ds, batch_size=2)
    images, labels = next(iter(loader))
    assert isinstance(images, tf.Tensor) and images.shape == (2, *input_size, 3)
    assert isinstance(labels, list) and all(isinstance(elt, str) for elt in labels)

    # File existence check
    img_name, _ = ds.data[0]
    move(os.path.join(ds.root, img_name), os.path.join(ds.root, "tmp_file"))
    with pytest.raises(FileNotFoundError):
        datasets.RecognitionDataset(mock_image_folder, mock_recognition_label)
    move(os.path.join(ds.root, "tmp_file"), os.path.join(ds.root, img_name))


def test_ocrdataset(mock_ocrdataset):

    input_size = (512, 512)

    ds = datasets.OCRDataset(
        *mock_ocrdataset,
        img_transforms=Resize(input_size),
    )
    assert len(ds) == 3
    img, target = ds[0]
    assert isinstance(img, tf.Tensor)
    assert img.dtype == tf.float32
    assert img.shape[:2] == input_size
    # Bounding boxes
    assert isinstance(target['boxes'], np.ndarray) and target['boxes'].dtype == np.float32
    assert np.all(np.logical_and(target['boxes'][:, :4] >= 0, target['boxes'][:, :4] <= 1))
    assert target['boxes'].shape[1] == 5
    # Flags
    assert isinstance(target['labels'], list) and all(isinstance(s, str) for s in target['labels'])
    # Cardinality consistency
    assert target['boxes'].shape[0] == len(target['labels'])

    loader = DataLoader(ds, batch_size=2)
    images, targets = next(iter(loader))
    assert isinstance(images, tf.Tensor) and images.shape == (2, *input_size, 3)
    assert isinstance(targets, list) and all(isinstance(elt, dict) for elt in targets)

    # File existence check
    img_name, _ = ds.data[0]
    move(os.path.join(ds.root, img_name), os.path.join(ds.root, "tmp_file"))
    with pytest.raises(FileNotFoundError):
        datasets.OCRDataset(*mock_ocrdataset)
    move(os.path.join(ds.root, "tmp_file"), os.path.join(ds.root, img_name))


def test_charactergenerator():

    input_size = (32, 32)
    vocab = 'abcdef'

    ds = datasets.CharacterGenerator(
        vocab=vocab,
        num_samples=10,
        cache_samples=True,
        img_transforms=Resize(input_size),
    )

    assert len(ds) == 10
    image, label = ds[0]
    assert isinstance(image, tf.Tensor)
    assert image.shape[:2] == input_size
    assert image.dtype == tf.float32
    assert isinstance(label, int) and label < len(vocab)

    loader = DataLoader(ds, batch_size=2, collate_fn=ds.collate_fn)
    images, targets = next(iter(loader))
    assert isinstance(images, tf.Tensor) and images.shape == (2, *input_size, 3)
    assert isinstance(targets, tf.Tensor) and targets.shape == (2,)
    assert targets.dtype == tf.int32


@pytest.mark.parametrize(
    "num_samples, rotate",
    [
        [5, True],  # Actual set has 229 train and 233 test samples
        [5, False]

    ],
)
def test_ic13_dataset(mock_ic13, num_samples, rotate):
    input_size = (512, 512)
    ds = datasets.IC13(
        *mock_ic13,
        img_transforms=Resize(input_size),
        rotated_bbox=rotate,
    )

    assert len(ds) == num_samples
    img, target = ds[0]
    assert isinstance(img, tf.Tensor)
    assert img.shape[:2] == input_size
    assert img.dtype == tf.float32
    assert isinstance(target, dict)
    assert isinstance(target['boxes'], np.ndarray) and np.all((target['boxes'] <= 1) & (target['boxes'] >= 0))
    assert isinstance(target['labels'], list) and all(isinstance(s, str) for s in target['labels'])
    assert len(target['labels']) == len(target['boxes'])

    loader = DataLoader(ds, batch_size=2)
    images, targets = next(iter(loader))
    assert isinstance(images, tf.Tensor) and images.shape == (2, *input_size, 3)
    assert isinstance(targets, list) and all(isinstance(elt, dict) for elt in targets)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[32, 128], 3, True],  # Actual set has 33402 training samples and 13068 test samples
        [[32, 128], 3, False],
    ],
)
def test_svhn(input_size, num_samples, rotate, mock_svhn_dataset):
    # monkeypatch the path to temporary dataset
    datasets.SVHN.TRAIN = (mock_svhn_dataset, None, "svhn_train.tar")

    ds = datasets.SVHN(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_svhn_dataset.split("/")[:-2]), cache_subdir=mock_svhn_dataset.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"SVHN(train={True})"
    _validate_dataset(ds, input_size, relative_coords=False)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[512, 512], 3, True],  # Actual set has 626 training samples and 360 test samples
        [[512, 512], 3, False],
    ],
)
def test_sroie(input_size, num_samples, rotate, mock_sroie_dataset):
    # monkeypatch the path to temporary dataset
    datasets.SROIE.TRAIN = (mock_sroie_dataset, None)

    ds = datasets.SROIE(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_sroie_dataset.split("/")[:-2]), cache_subdir=mock_sroie_dataset.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"SROIE(train={True})"
    _validate_dataset(ds, input_size, relative_coords=False)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[512, 512], 3, True],  # Actual set has 149 training samples and 50 test samples
        [[512, 512], 3, False],
    ],
)
def test_funsd(input_size, num_samples, rotate, mock_funsd_dataset):
    # monkeypatch the path to temporary dataset
    datasets.FUNSD.URL = mock_funsd_dataset
    datasets.FUNSD.SHA256 = None
    datasets.FUNSD.FILE_NAME = "funsd.zip"

    ds = datasets.FUNSD(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_funsd_dataset.split("/")[:-2]), cache_subdir=mock_funsd_dataset.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"FUNSD(train={True})"
    _validate_dataset(ds, input_size, relative_coords=False)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[512, 512], 3, True],  # Actual set has 800 training samples and 100 test samples
        [[512, 512], 3, False],
    ],
)
def test_cord(input_size, num_samples, rotate, mock_cord_dataset):
    # monkeypatch the path to temporary dataset
    datasets.CORD.TRAIN = (mock_cord_dataset, None)

    ds = datasets.CORD(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_cord_dataset.split("/")[:-2]), cache_subdir=mock_cord_dataset.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"CORD(train={True})"
    _validate_dataset(ds, input_size, relative_coords=False)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[512, 512], 2, True],  # Actual set has 772875 training samples and 85875 test samples
        [[512, 512], 2, False],
    ],
)
def test_synthtext(input_size, num_samples, rotate, mock_synthtext_dataset):
    # monkeypatch the path to temporary dataset
    datasets.SynthText.URL = mock_synthtext_dataset
    datasets.SynthText.SHA256 = None

    ds = datasets.SynthText(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_synthtext_dataset.split("/")[:-2]), cache_subdir=mock_synthtext_dataset.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"SynthText(train={True})"
    _validate_dataset(ds, input_size, relative_coords=False)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[512, 512], 3, True],  # Actual set has 2700 training samples and 300 test samples
        [[512, 512], 3, False],
    ],
)
def test_artefact_detection(input_size, num_samples, rotate, mock_doc_artefacts):
    # monkeypatch the path to temporary dataset
    datasets.DocArtefacts.URL = mock_doc_artefacts
    datasets.DocArtefacts.SHA256 = None

    ds = datasets.DocArtefacts(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_doc_artefacts.split("/")[:-2]), cache_subdir=mock_doc_artefacts.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"DocArtefacts(train={True})"
    _validate_dataset(ds, input_size, class_indices=True)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[32, 128], 1, True],  # Actual set has 2000 training samples and 3000 test samples
        [[32, 128], 1, False],
    ],
)
def test_iiit5k(input_size, num_samples, rotate, mock_iiit5k_dataset):
    # monkeypatch the path to temporary dataset
    datasets.IIIT5K.URL = mock_iiit5k_dataset
    datasets.IIIT5K.SHA256 = None

    ds = datasets.IIIT5K(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_iiit5k_dataset.split("/")[:-2]), cache_subdir=mock_iiit5k_dataset.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"IIIT5K(train={True})"
    img, target = ds[0]
    _validate_dataset(ds, input_size, batch_size=1, relative_coords=False)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[512, 512], 3, True],  # Actual set has 100 training samples and 249 test samples
        [[512, 512], 3, False],
    ],
)
def test_svt(input_size, num_samples, rotate, mock_svt_dataset):
    # monkeypatch the path to temporary dataset
    datasets.SVT.URL = mock_svt_dataset
    datasets.SVT.SHA256 = None

    ds = datasets.SVT(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_svt_dataset.split("/")[:-2]), cache_subdir=mock_svt_dataset.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"SVT(train={True})"
    _validate_dataset(ds, input_size)


@pytest.mark.parametrize(
    "input_size, num_samples, rotate",
    [
        [[512, 512], 3, True],  # Actual set has 246 training samples and 249 test samples
        [[512, 512], 3, False],
    ],
)
def test_ic03(input_size, num_samples, rotate, mock_ic03_dataset):
    # monkeypatch the path to temporary dataset
    datasets.IC03.TRAIN = (mock_ic03_dataset, None, "ic03_train.zip")

    ds = datasets.IC03(
        train=True, download=True, img_transforms=Resize(input_size), rotated_bbox=rotate,
        cache_dir="/".join(mock_ic03_dataset.split("/")[:-2]), cache_subdir=mock_ic03_dataset.split("/")[-2],
    )

    assert len(ds) == num_samples
    assert repr(ds) == f"IC03(train={True})"
    _validate_dataset(ds, input_size)