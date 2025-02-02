#    Copyright 2021, 2022, 2023 SECTRA AB
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import logging
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum, IntEnum, auto
from functools import cached_property
from typing import Any, List, Optional, Sequence, Tuple, Union, cast

from pydicom.dataset import Dataset
from pydicom.pixel_data_handlers import pillow_handler
from pydicom.sequence import Sequence as DicomSequence
from pydicom.uid import JPEG2000, UID, JPEG2000Lossless, JPEGBaseline8Bit, generate_uid
from pydicom.valuerep import DSfloat

from wsidicom.config import settings
from wsidicom.errors import (
    WsiDicomError,
    WsiDicomFileError,
    WsiDicomRequirementError,
    WsiDicomStrictRequirementError,
    WsiDicomUidDuplicateError,
)
from wsidicom.geometry import Size, SizeMm
from wsidicom.instance.image_data import ImageData
from wsidicom.uid import WSI_SOP_CLASS_UID, FileUids, SlideUids


class ImageType(Enum):
    VOLUME = "VOLUME"
    LABEL = "LABEL"
    OVERVIEW = "OVERVIEW"


class Requirement(IntEnum):
    ALWAYS = auto()  # Always required, even if not in strict mode
    STRICT = auto()  # Required if in strict mode
    STANDARD = auto()  # Required or optional in standard, not (yet) needed


@dataclass
class WsiAttributeRequirement:
    """Class for defining requirement and optionally default value for DICOM
    WSI attribute. If image types is given, requirement is only checked for
    images of matching image type.
    """

    requirement: Requirement
    image_types: Tuple[ImageType, ...]
    default: Any = None

    def __init__(
        self,
        requirement: Requirement,
        image_type: Optional[Union[ImageType, Sequence[ImageType]]] = None,
        default: Any = None,
    ) -> None:
        self.requirement = requirement
        if image_type is not None:
            if not isinstance(image_type, Sequence):
                self.image_types = (image_type,)
            else:
                self.image_types = tuple(image_type)
        else:
            self.image_types = ()
        self.default = default

    def get_default(self, image_type: ImageType) -> Any:
        """Get default value for attribute. Raises WsiDicomRequirementError if
        attribute is set as always required for image type. Raises
        WsiDicomStrictRequirementError if attribute is set as always required
        in strict mode."""
        if self.requirement == Requirement.ALWAYS and image_type in self.image_types:
            raise WsiDicomRequirementError(
                f"Attribute is set as always required for {image_type}"
            )
        elif (
            settings.strict_attribute_check
            and self.requirement == Requirement.STRICT
            and image_type in self.image_types
        ):
            raise WsiDicomStrictRequirementError(
                f"Attribute is set as required for {image_type} " "and mode is strict"
            )
        return self.default

    def evaluate(self, image_type: ImageType) -> bool:
        """Evaluate if attribute is required for image type."""
        if self.requirement == Requirement.ALWAYS or (
            self.requirement == Requirement.STRICT and settings.strict_attribute_check
        ):
            if image_type in self.image_types:
                return True
        return False


WSI_ATTRIBUTES = {
    "SOPClassUID": WsiAttributeRequirement(Requirement.ALWAYS),
    "SOPInstanceUID": WsiAttributeRequirement(Requirement.ALWAYS),
    "StudyInstanceUID": WsiAttributeRequirement(Requirement.ALWAYS),
    "SeriesInstanceUID": WsiAttributeRequirement(Requirement.ALWAYS),
    "Rows": WsiAttributeRequirement(Requirement.ALWAYS),
    "Columns": WsiAttributeRequirement(Requirement.ALWAYS),
    "SamplesPerPixel": WsiAttributeRequirement(Requirement.ALWAYS),
    "PhotometricInterpretation": WsiAttributeRequirement(Requirement.ALWAYS),
    "TotalPixelMatrixColumns": WsiAttributeRequirement(Requirement.ALWAYS),
    "TotalPixelMatrixRows": WsiAttributeRequirement(Requirement.ALWAYS),
    "ImageType": WsiAttributeRequirement(Requirement.ALWAYS),
    "SharedFunctionalGroupsSequence": WsiAttributeRequirement(
        Requirement.STRICT, ImageType.VOLUME
    ),
    "FrameOfReferenceUID": WsiAttributeRequirement(Requirement.STRICT),
    "FocusMethod": WsiAttributeRequirement(
        Requirement.STRICT, ImageType.VOLUME, "AUTO"
    ),
    "ExtendedDepthOfField": WsiAttributeRequirement(
        Requirement.STRICT, ImageType.VOLUME, "NO"
    ),
    "OpticalPathSequence": WsiAttributeRequirement(Requirement.STRICT),
    "ImagedVolumeWidth": WsiAttributeRequirement(Requirement.STRICT, ImageType.VOLUME),
    "ImagedVolumeHeight": WsiAttributeRequirement(Requirement.STRICT, ImageType.VOLUME),
    "ImagedVolumeDepth": WsiAttributeRequirement(Requirement.STRICT, ImageType.VOLUME),
    "TotalPixelMatrixFocalPlanes": WsiAttributeRequirement(
        Requirement.STANDARD, default=1
    ),
    "NumberOfOpticalPaths": WsiAttributeRequirement(Requirement.STANDARD, default=1),
    "NumberOfFrames": WsiAttributeRequirement(Requirement.STANDARD, default=1),
    "Modality": WsiAttributeRequirement(Requirement.STANDARD, default="SM"),
    "Manufacturer": WsiAttributeRequirement(Requirement.STANDARD),
    "ManufacturerModelName": WsiAttributeRequirement(Requirement.STANDARD),
    "DeviceSerialNumber": WsiAttributeRequirement(Requirement.STANDARD),
    "SoftwareVersions": WsiAttributeRequirement(Requirement.STANDARD),
    "BitsAllocated": WsiAttributeRequirement(Requirement.STANDARD),
    "BitsStored": WsiAttributeRequirement(Requirement.STANDARD),
    "HighBit": WsiAttributeRequirement(Requirement.STANDARD),
    "PixelRepresentation": WsiAttributeRequirement(Requirement.STANDARD),
    "TotalPixelMatrixOriginSequence": WsiAttributeRequirement(Requirement.STANDARD),
    "ImageOrientationSlide": WsiAttributeRequirement(
        Requirement.STANDARD, default=[0, 1, 0, 1, 0, 0]
    ),
    "AcquisitionDateTime": WsiAttributeRequirement(Requirement.STANDARD),
    "LossyImageCompression": WsiAttributeRequirement(Requirement.STANDARD),
    "VolumetricProperties": WsiAttributeRequirement(
        Requirement.STANDARD, default="VOLUME"
    ),
    "SpecimenLabelInImage": WsiAttributeRequirement(Requirement.STANDARD, default="NO"),
    "BurnedInAnnotation": WsiAttributeRequirement(Requirement.STANDARD, default="NO"),
    "ContentDate": WsiAttributeRequirement(Requirement.STANDARD),
    "ContentTime": WsiAttributeRequirement(Requirement.STANDARD),
    "InstanceNumber": WsiAttributeRequirement(Requirement.STANDARD),
    "DimensionOrganizationSequence": WsiAttributeRequirement(Requirement.STANDARD),
    "ContainerIdentifier": WsiAttributeRequirement(Requirement.STANDARD),
    "SpecimenDescriptionSequence": WsiAttributeRequirement(Requirement.STANDARD),
    "SpacingBetweenSlices": WsiAttributeRequirement(Requirement.STANDARD, default=0.0),
    "SOPInstanceUIDOfConcatenationSource": WsiAttributeRequirement(
        Requirement.STANDARD
    ),
    "DimensionOrganizationType": WsiAttributeRequirement(
        Requirement.STANDARD, default="TILED_SPARSE"
    ),
    "NumberOfFocalPlanes": WsiAttributeRequirement(Requirement.STANDARD, default=1),
    "DistanceBetweenFocalPlanes": WsiAttributeRequirement(
        Requirement.STANDARD, default=0.0
    ),
    "SliceThickness": WsiAttributeRequirement(Requirement.STANDARD),
}


class TileType(Enum):
    FULL = "TILED_FULL"
    SPARSE = "TILED_SPARSE"


class WsiDataset(Dataset):
    """Extend pydicom.dataset.Dataset (containing WSI metadata) with simple
    parsers for attributes specific for WSI. Use snake case to avoid name
    collision with dicom fields (that are handled by pydicom.dataset.Dataset).
    """

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self})"

    def __str__(self) -> str:
        return f"{type(self).__name__} of dataset {self.uids.instance}"

    @cached_property
    def uids(self) -> FileUids:
        """Return UIDs from dataset.

        Returns
        ----------
        FileUids
            Found UIDs from dataset.
        """
        instance_uid = UID(self.SOPInstanceUID)
        concatenation_uid = self._get_dicom_attribute(
            "SOPInstanceUIDOfConcatenationSource"
        )
        frame_of_reference_uid = self._get_dicom_attribute("FrameOfReferenceUID")

        slide_uids = SlideUids(
            self.StudyInstanceUID,
            self.SeriesInstanceUID,
            frame_of_reference_uid,
        )
        file_uids = FileUids(instance_uid, concatenation_uid, slide_uids)
        return file_uids

    @cached_property
    def frame_offset(self) -> int:
        """Return frame offset (offset to first frame in instance if
        concatenated). Is zero if non-concatenated instance or first instance
        in concatenated instance.

        Returns
        ----------
        int
            Concatenation offset in number of frames.
        """
        if self.uids.concatenation is None:
            return 0
        try:
            return int(self.ConcatenationFrameOffsetNumber)
        except AttributeError:
            raise WsiDicomError(
                "Concatenated file missing concatenation frame offset" "number"
            )

    @cached_property
    def frame_count(self) -> int:
        """Return number of frames in instance."""
        frame_count = self._get_dicom_attribute("NumberOfFrames")
        return cast(int, frame_count)

    @cached_property
    def tile_type(self) -> TileType:
        """Return tiling type of dataset. Raises WsiDicomError if type
        is undetermined.

        Returns
        ----------
        TileType
            Tiling type
        """
        tile_type = self._get_dicom_attribute("DimensionOrganizationType")
        if tile_type == "TILED_FULL":
            return TileType.FULL
        elif "PerFrameFunctionalGroupsSequence" in self:
            return TileType.SPARSE
        elif self.frame_count == 1:
            return TileType.FULL
        raise WsiDicomError("Undetermined tile type.")

    @cached_property
    def pixel_measure(self) -> Optional[Dataset]:
        """Return Pixel measure dataset from dataset if found.

        Returns
        ----------
        Optional[Dataset]
            Found Pixel measure dataset.
        """
        shared_functional_group = self._get_dicom_attribute(
            "SharedFunctionalGroupsSequence"
        )
        if shared_functional_group is None:
            return None
        pixel_measure_sequence = self._get_dicom_attribute(
            "PixelMeasuresSequence", shared_functional_group[0]
        )
        if pixel_measure_sequence is None:
            return None
        return pixel_measure_sequence[0]

    @cached_property
    def pixel_spacing(self) -> Optional[SizeMm]:
        """Read pixel spacing from dicom dataset.

        Parameters
        ----------
        ds: Dataset
            Pydicom dataset

        Returns
        ----------
        SizeMm
            The pixel spacing in mm/pixel.
        """
        if self.pixel_measure is None:
            return None
        pixel_spacing_values = getattr(self.pixel_measure, "PixelSpacing", None)
        if pixel_spacing_values is not None:
            if any([spacing == 0 for spacing in pixel_spacing_values]):
                logging.warn(f"Pixel spacing is zero, {pixel_spacing_values}")
                return None
            return SizeMm.from_tuple(pixel_spacing_values)
        return None

    @cached_property
    def spacing_between_slices(self) -> Optional[float]:
        """Return spacing between slices."""
        if self.pixel_measure is None:
            return None
        return getattr(self.pixel_measure, "SpacingBetweenSlices", None)

    @cached_property
    def number_of_focal_planes(self) -> int:
        """Return number of focal planes."""
        number_of_focal_planes = self._get_dicom_attribute(
            "TotalPixelMatrixFocalPlanes"
        )
        return cast(int, number_of_focal_planes)

    @cached_property
    def frame_sequence(self) -> DicomSequence:
        """Return per frame functional group sequene if present, otherwise
        shared functional group sequence.

        Returns
        ----------
        DicomSequence
            Per frame or shared functional group sequence.
        """
        if "PerFrameFunctionalGroupsSequence" in self and (
            "PlanePositionSlideSequence" in self.PerFrameFunctionalGroupsSequence[0]
        ):
            return self.PerFrameFunctionalGroupsSequence
        elif "SharedFunctionalGroupsSequence" in self:
            return self.SharedFunctionalGroupsSequence
        return DicomSequence([])

    @property
    def ext_depth_of_field(self) -> bool:
        """Return true if instance has extended depth of field
        (several focal planes are combined to one plane)."""
        return self._ext_depth_of_field[0]

    @property
    def ext_depth_of_field_planes(self) -> Optional[int]:
        """Return number of focal planes used for extended depth of
        field."""
        return self._ext_depth_of_field[1]

    @property
    def ext_depth_of_field_plane_distance(self) -> Optional[float]:
        """Return total focal depth used for extended depth of field."""
        return self._ext_depth_of_field[0]

    @cached_property
    def focus_method(self) -> str:
        """Return focus method."""
        focus_method = self._get_dicom_attribute("FocusMethod")
        return str(focus_method)

    @cached_property
    def image_size(self) -> Size:
        """Read total pixel size from dataset.

        Returns
        ----------
        Size
            The image size
        """
        image_size = Size(self.TotalPixelMatrixColumns, self.TotalPixelMatrixRows)
        if image_size.width <= 0 or image_size.height <= 0:
            raise WsiDicomError("Image size is zero")
        if self.tile_type == TileType.FULL:
            expected_tiled_size = image_size.ceil_div(self.tile_size)
            if expected_tiled_size.area != self.frame_count:
                error = (
                    f"Image size {image_size} does not match tile size "
                    f"{self.tile_size} and number of frames {self.frame_count} "
                    f"for tile type {TileType.FULL}."
                )
                if self.image_type == ImageType.VOLUME or self.frame_count != 1:
                    # Be strict on volume images.
                    raise WsiDicomError(error)
                # Labels and overviews are likely to have only one tile.
                error += " Overriding image size to tile size."
                logging.warn(error)
                image_size = self.tile_size
        return image_size

    @cached_property
    def mm_size(self) -> Optional[SizeMm]:
        """Read mm size from dataset.

        Returns
        ----------
        SizeMm
            The size of the image in mm
        """
        mm_width = self._get_dicom_attribute("ImagedVolumeWidth")
        mm_height = self._get_dicom_attribute("ImagedVolumeHeight")
        if mm_width is None or mm_height is None:
            mm_size = None
        else:
            mm_size = SizeMm(mm_width, mm_height)
        return mm_size

    @cached_property
    def mm_depth(self) -> Optional[float]:
        """Return depth of image in mm."""
        return self._get_dicom_attribute("ImagedVolumeDepth")

    @cached_property
    def tile_size(self) -> Size:
        """Read tile size from from dataset.

        Returns
        ----------
        Size
            The tile size
        """
        return Size(self.Columns, self.Rows)

    @property
    def samples_per_pixel(self) -> int:
        """Return samples per pixel (3 for RGB)."""
        return self.SamplesPerPixel

    @property
    def photometric_interpretation(self) -> str:
        """Return photometric interpretation."""
        return self.PhotometricInterpretation

    @cached_property
    def optical_path_sequence(self) -> Optional[DicomSequence]:
        """Return optical path sequence from dataset."""
        return self._get_dicom_attribute("OpticalPathSequence")

    @property
    def slice_thickness(self) -> Optional[float]:
        """Return slice thickness spacing from pixel measure dataset.

        Returns
        ----------
        Optional[float]
            Slice thickess or None if unknown.
        """
        try:
            return self._get_dicom_attribute("SliceThickness", self.pixel_measure)
        except AttributeError:
            if self.mm_depth is not None:
                return self.mm_depth / self.number_of_focal_planes
        return None

    @cached_property
    def image_type(self) -> ImageType:
        """Return wsi flavour from wsi type tuple.

        Returns
        ----------
        ImageType
            Wsi flavour.
        """
        return self._get_image_type(self.ImageType)

    @classmethod
    def is_supported_wsi_dicom(
        cls, dataset: Dataset, transfer_syntax: UID
    ) -> Optional[ImageType]:
        """Check if dataset is dicom wsi type and that required attributes
        (for the function of the library) is available.
        Warn if attribute listed as required in the library or required in the
        standard is missing.

        Parameters
        ----------
        dataset: Dataset
            Pydicom dataset to check if is a WSI dataset.
        transfer_syntax: UID
            Transfer syntax of dataset.

        Returns
        ----------
        Optional[ImageType]
            WSI image flavor
        """

        sop_class_uid: UID = getattr(dataset, "SOPClassUID")
        if sop_class_uid != WSI_SOP_CLASS_UID:
            logging.debug(f"Non-wsi image, SOP class {sop_class_uid.name}")
            return None

        try:
            image_type = cls._get_image_type(dataset.ImageType)
        except ValueError:
            logging.debug(f"Non-supported image type {dataset.ImageType}")
            return None

        for name, attribute in WSI_ATTRIBUTES.items():
            if name not in dataset and attribute.evaluate(image_type):
                logging.debug(f"Missing required attribute {name}")
                return None

        syntax_supported = pillow_handler.supports_transfer_syntax(transfer_syntax)
        if not syntax_supported:
            logging.debug(f"Non-supported transfer syntax {transfer_syntax}")
            return None

        return image_type

    @staticmethod
    def check_duplicate_dataset(
        datasets: Sequence["WsiDataset"], caller: object
    ) -> None:
        """Check for duplicates in a list of datasets. Datasets are duplicate
        if instance uids match. Stops at first found duplicate and raises
        WsiDicomUidDuplicateError.

        Parameters
        ----------
        datasets: Sequence[Dataset]
            List of datasets to check.
        caller: Object
            Object that the files belongs to.
        """
        instance_uids: List[UID] = []

        for dataset in datasets:
            instance_uid = UID(dataset.SOPInstanceUID)
            if instance_uid not in instance_uids:
                instance_uids.append(instance_uid)
            else:
                raise WsiDicomUidDuplicateError(str(dataset), str(caller))

    def matches_instance(self, other_dataset: "WsiDataset") -> bool:
        """Return true if other file is of the same instance as self.

        Parameters
        ----------
        other_dataset: 'WsiDataset'
            Dataset to check.

        Returns
        ----------
        bool
            True if same instance.
        """
        return (
            self.uids == other_dataset.uids
            and self.image_size == other_dataset.image_size
            and self.tile_size == other_dataset.tile_size
            and self.tile_type == other_dataset.tile_type
        )

    def matches_series(self, uids: SlideUids, tile_size: Optional[Size] = None) -> bool:
        """Check if instance is valid (Uids and tile size match).
        Base uids should match for instances in all types of series,
        tile size should only match for level series.
        """
        if tile_size is not None and tile_size != self.tile_size:
            return False

        return self.uids.slide.matches(uids)

    def read_optical_path_identifier(self, frame: Dataset) -> str:
        """Return optical path identifier from frame, or from self if not
        found."""
        optical_sequence = getattr(
            frame, "OpticalPathIdentificationSequence", self.optical_path_sequence
        )
        if optical_sequence is None:
            return "0"
        return getattr(optical_sequence[0], "OpticalPathIdentifier", "0")

    def as_tiled_full(
        self,
        focal_planes: Sequence[float],
        optical_paths: Sequence[str],
        tiled_size: Size,
        scale: int = 1,
    ) -> "WsiDataset":
        """Return copy of dataset with properties set to reflect a tiled full
        arrangement of the listed image data. Optionally set properties to
        reflect scaled data.

        Parameters
        ----------
        focal_planes: Sequence[float]
            Focal planes that should be encoded into dataset.
        optical_paths: Sequence[str]
            Optical paths that should be encoded into dataset.
        tiled_size: Size
            Tiled size of image.
        scale: int = 1
            Optionally scale data.

        Returns
        ----------
        WsiDataset
            Copy of dataset set as tiled full.

        """

        dataset = deepcopy(self)
        dataset.DimensionOrganizationType = "TILED_FULL"
        # Make a new Shared functional group sequence and Pixel measure
        # sequence if not in dataset, otherwise update the Pixel measure
        # sequence
        shared_functional_group = getattr(
            dataset, "SharedFunctionalGroupsSequence", DicomSequence([Dataset()])
        )
        plane_position_slide = Dataset()
        plane_position_slide.ZOffsetInSlideCoordinateSystem = DSfloat(
            focal_planes[0], True
        )
        shared_functional_group[0].PlanePositionSlideSequence = DicomSequence(
            [plane_position_slide]
        )

        pixel_measure = getattr(
            shared_functional_group[0],
            "PixelMeasuresSequence",
            DicomSequence([Dataset()]),
        )
        if dataset.pixel_spacing is not None:
            pixel_measure[0].PixelSpacing = [
                DSfloat(dataset.pixel_spacing.width * scale, True),
                DSfloat(dataset.pixel_spacing.height * scale, True),
            ]
        pixel_measure[0].SpacingBetweenSlices = DSfloat(
            self._get_spacing_between_slices_for_focal_planes(focal_planes), True
        )

        if dataset.slice_thickness is not None:
            pixel_measure[0].SliceThickness = DSfloat(dataset.slice_thickness, True)

        shared_functional_group[0].PixelMeasuresSequence = pixel_measure
        dataset.SharedFunctionalGroupsSequence = shared_functional_group

        # Remove Per Frame functional groups sequence
        if "PerFrameFunctionalGroupsSequence" in dataset:
            del dataset["PerFrameFunctionalGroupsSequence"]

        dataset.TotalPixelMatrixFocalPlanes = len(focal_planes)
        dataset.NumberOfOpticalPaths = len(optical_paths)
        dataset.NumberOfFrames = (
            max(tiled_size.ceil_div(scale).area, 1)
            * len(focal_planes)
            * len(optical_paths)
        )
        scaled_size = dataset.image_size.ceil_div(scale)
        dataset.TotalPixelMatrixColumns = max(scaled_size.width, 1)
        dataset.TotalPixelMatrixRows = max(scaled_size.height, 1)
        return dataset

    @classmethod
    def create_instance_dataset(
        cls,
        base_dataset: Dataset,
        image_type: ImageType,
        image_data: ImageData,
        pyramid_index: Optional[int] = None,
    ) -> "WsiDataset":
        """Return instance dataset for image_data based on base dataset.

        Parameters
        ----------
        base_dataset: Dataset
            Dataset common for all instances.
        image_type:
            Type of instance ('VOLUME', 'LABEL', 'OVERVIEW)
        image_data:
            Image data to create dataset for.

        Returns
        ----------
        WsiDataset
            Dataset for instance.
        """
        dataset = deepcopy(base_dataset)
        if image_type == ImageType.VOLUME and pyramid_index == 0:
            resampled = "NONE"
        else:
            resampled = "RESAMPLED"
        dataset.ImageType = ["ORIGINAL", "PRIMARY", image_type.value, resampled]
        dataset.SOPInstanceUID = generate_uid(prefix=None)
        shared_functional_group_sequence = Dataset()
        if image_data.pixel_spacing is None:
            if image_type == ImageType.VOLUME:
                raise ValueError(
                    "Image flavor 'VOLUME' requires pixel spacing to be set"
                )
        else:
            pixel_measure_sequence = Dataset()
            pixel_measure_sequence.PixelSpacing = [
                DSfloat(image_data.pixel_spacing.width, True),
                DSfloat(image_data.pixel_spacing.height, True),
            ]
            pixel_measure_sequence.SpacingBetweenSlices = DSfloat(0.0, True)
            # DICOM 2022a part 3 IODs - C.8.12.4.1.2 Imaged Volume Width,
            # Height, Depth. Depth must not be 0. Default to 0.5 microns
            pixel_measure_sequence.SliceThickness = DSfloat(0.0005, True)
            shared_functional_group_sequence.PixelMeasuresSequence = DicomSequence(
                [pixel_measure_sequence]
            )
            dataset.SharedFunctionalGroupsSequence = DicomSequence(
                [shared_functional_group_sequence]
            )
            dataset.ImagedVolumeWidth = (
                image_data.image_size.width * image_data.pixel_spacing.width
            )
            dataset.ImagedVolumeHeight = (
                image_data.image_size.height * image_data.pixel_spacing.height
            )
            dataset.ImagedVolumeDepth = pixel_measure_sequence.SliceThickness
            # DICOM 2022a part 3 IODs - C.8.12.9 Whole Slide Microscopy Image
            # Frame Type Macro. Analogous to ImageType and shared by all
            # frames so clone
            wsi_frame_type_item = Dataset()
            wsi_frame_type_item.FrameType = dataset.ImageType
            (
                shared_functional_group_sequence.WholeSlideMicroscopyImageFrameTypeSequence
            ) = DicomSequence([wsi_frame_type_item])

        if image_data.image_coordinate_system is not None:
            dataset.ImageOrientationSlide = list(
                image_data.image_coordinate_system.image_orientation_slide
            )
            dataset.TotalPixelMatrixOriginSequence = (
                image_data.image_coordinate_system.total_pixel_matrix_origin_sequence
            )

        dataset.DimensionOrganizationType = "TILED_FULL"
        dataset.TotalPixelMatrixColumns = image_data.image_size.width
        dataset.TotalPixelMatrixRows = image_data.image_size.height
        dataset.Columns = image_data.tile_size.width
        dataset.Rows = image_data.tile_size.height
        dataset.NumberOfFrames = (
            image_data.tiled_size.width * image_data.tiled_size.height
        )

        if image_data.transfer_syntax == JPEGBaseline8Bit:
            dataset.BitsAllocated = 8
            dataset.BitsStored = 8
            dataset.HighBit = 7
            dataset.PixelRepresentation = 0
            dataset.LossyImageCompression = "01"
            dataset.LossyImageCompressionRatio = 1
            dataset.LossyImageCompressionMethod = "ISO_10918_1"
            dataset.LossyImageCompression = "01"
        elif image_data.transfer_syntax == JPEG2000:
            # TODO JPEG2000 can have higher bitcount
            dataset.BitsAllocated = 8
            dataset.BitsStored = 8
            dataset.HighBit = 7
            dataset.PixelRepresentation = 0
            dataset.LossyImageCompressionRatio = 1
            dataset.LossyImageCompressionMethod = "ISO_15444_1"
            dataset.LossyImageCompression = "01"
        elif image_data.transfer_syntax == JPEG2000Lossless:
            # TODO JPEG2000 can have higher bitcount
            dataset.BitsAllocated = 8
            dataset.BitsStored = 8
            dataset.HighBit = 7
            dataset.PixelRepresentation = 0
            dataset.LossyImageCompression = "00"
        else:
            raise ValueError("Non-supported transfer syntax.")

        dataset.PhotometricInterpretation = image_data.photometric_interpretation
        dataset.SamplesPerPixel = image_data.samples_per_pixel

        dataset.PlanarConfiguration = 0

        dataset.FocusMethod = "AUTO"
        dataset.ExtendedDepthOfField = "NO"
        return WsiDataset(dataset)

    def _get_dicom_attribute(
        self, name: str, dataset: Optional[Dataset] = None
    ) -> Optional[Any]:
        """Return value for DICOM attribute by name. Optionally get it from
        another dataset. Returns default value if possible when attribute
        is missing.

        Parameters
        ----------
        name: str
            Name of attribute to get.
        dataset: Optional[Dataset] = None
            Optional other dataset to get attribute from.

        Returns
        ----------
        Optional[Any]
            Value of attribute, or None.
        """
        if dataset is None:
            dataset = self
        value = getattr(dataset, name, None)
        if value is None:
            return WSI_ATTRIBUTES[name].get_default(self.image_type)
        return value

    @cached_property
    def _ext_depth_of_field(self) -> Tuple[bool, Optional[int], Optional[float]]:
        """Return extended depth of field (enabled, number of focal planes,
        distance between focal planes) from dataset.

        Returns
        ----------
        Tuple[bool, Optional[int], Optional[float]]
            If extended depth of field is used, and if used number of focal
            planes and distance between focal planes.
        """
        if self._get_dicom_attribute("ExtendedDepthOfField") != "YES":
            return False, None, None

        planes = self._get_dicom_attribute("NumberOfFocalPlanes")
        distance = self._get_dicom_attribute("DistanceBetweenFocalPlanes")
        if planes is None or distance is None:
            raise WsiDicomFileError(
                self.filepath,
                "Missing NumberOfFocalPlanes or DistanceBetweenFocalPlanes",
            )
        return True, planes, distance

    @staticmethod
    def _get_spacing_between_slices_for_focal_planes(
        focal_planes: Sequence[float],
    ) -> float:
        """Return spacing between slices in mm for focal planes (defined in
        um). Spacing must be the same between all focal planes for TILED_FULL
        arrangement.

        Parameters
        ----------
        focal_planes: Sequence[float]
            Focal planes to calculate spacing for.

        Returns
        ----------
        float
            Spacing between focal planes.

        """
        if len(focal_planes) == 1:
            return 0.0
        spacing: Optional[float] = None
        sorted_focal_planes = sorted(focal_planes)
        for index in range(len(sorted_focal_planes) - 1):
            this_spacing = sorted_focal_planes[index + 1] - sorted_focal_planes[index]
            if spacing is None:
                spacing = this_spacing
            elif abs(spacing - this_spacing) > settings.focal_plane_distance_threshold:
                raise NotImplementedError(
                    "Image data has non-equal spacing between slices: "
                    f"{spacing, this_spacing}, difference threshold: "
                    f"{settings.focal_plane_distance_threshold}, "
                    "not possible to encode as TILED_FULL"
                )
        if spacing is None:
            raise ValueError("Could not calculate spacings.")
        return spacing / 1000.0

    @staticmethod
    def _get_image_type(wsi_type: Tuple[str, str, str, str]) -> ImageType:
        """Return wsi flavour from wsi type tuple.

        Returns
        ----------
        str
            Wsi flavour.
        """
        IMAGE_TYPE_INDEX_IN_WSI_TYPE = 2
        return ImageType(wsi_type[IMAGE_TYPE_INDEX_IN_WSI_TYPE])
