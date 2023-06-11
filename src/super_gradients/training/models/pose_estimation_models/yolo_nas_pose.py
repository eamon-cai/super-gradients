import collections
import copy
from functools import lru_cache
from typing import Union, Tuple, Optional, List

import numpy as np
import torch.nn.functional
from omegaconf import DictConfig
from torch import nn, Tensor

from super_gradients.common.decorators.factory_decorator import resolve_param
from super_gradients.common.factories.activations_type_factory import ActivationsTypeFactory
from super_gradients.common.factories.processing_factory import ProcessingFactory
from super_gradients.common.object_names import Models
from super_gradients.common.registry import register_model, register_detection_module
from super_gradients.modules import BaseDetectionModule, ConvBNAct
from super_gradients.training.models.arch_params_factory import get_arch_params
from super_gradients.training.models.detection_models.customizable_detector import CustomizableDetector
from super_gradients.training.pipelines.pipelines import PoseEstimationPipeline
from super_gradients.training.processing.processing import Processing
from super_gradients.training.utils import HpmStruct, get_param, DEKRPoseEstimationDecodeCallback
from super_gradients.training.utils.media.image import ImageSource
from super_gradients.training.utils.predict import ImagesPoseEstimationPrediction


@register_detection_module()
class YoloNASHead(BaseDetectionModule):
    @resolve_param("activation_type", ActivationsTypeFactory())
    def __init__(
        self,
        num_classes: int,
        in_channels: Tuple[int, int, int],
        inter_channels: int,
        channels_per_kpt: int,
        num_blocks: int,
        activation_type,
        upsample_factor: int,
    ):
        super().__init__(in_channels)
        self.num_classes = num_classes
        self.num_classes_with_center = num_classes + 1
        self.channels_per_kpt = channels_per_kpt
        self.stem = ConvBNAct(sum(in_channels), inter_channels, kernel_size=1, stride=1, padding=0, bias=False, activation_type=activation_type)
        self.heatmap = self.build_heatmap_path(inter_channels, activation_type, num_blocks, self.num_classes_with_center, upsample_factor)

        self.offset_transition = ConvBNAct(
            inter_channels, channels_per_kpt * num_classes, kernel_size=3, stride=1, padding=1, bias=False, activation_type=activation_type
        )

        offset_modules = []
        for _ in range(num_classes):
            offset_modules.append(self.build_offset_path(inter_channels, activation_type, num_blocks, channels_per_kpt, upsample_factor))
        self.offset_modules = nn.ModuleList(offset_modules)

    def forward(self, feats: Tuple[Tensor]):
        biggest_size = feats[0].size()[2:]
        feats = [feats[0]] + [torch.nn.functional.interpolate(x, size=biggest_size, mode="bilinear", align_corners=False) for x in feats[1:]]
        feats = torch.cat(feats, dim=1)
        feats = self.stem(feats)

        heatmap = self.heatmap(feats)
        offset_features = self.offset_transition(feats)
        final_offset = []

        for j in range(self.num_classes):
            offset_specific_features = offset_features[:, j * self.channels_per_kpt : (j + 1) * self.channels_per_kpt]
            offset_predictions = self.offset_modules[j](offset_specific_features)
            final_offset.append(offset_predictions)

        offsets = torch.cat(final_offset, dim=1)
        return heatmap, offsets

    def build_heatmap_path(self, inter_channels, activation_type, num_blocks, num_joints, upsample_factor):
        blocks = [
            (
                f"conv_bn_act_{block_index:02d}",
                ConvBNAct(inter_channels, inter_channels, kernel_size=3, stride=1, padding=1, bias=False, activation_type=activation_type),
            )
            for block_index in range(num_blocks)
        ]
        blocks += [("final", nn.Conv2d(inter_channels, num_joints * (2**upsample_factor), kernel_size=1)), ("shuffle", nn.PixelShuffle(upsample_factor))]
        return nn.Sequential(collections.OrderedDict(blocks))

    def build_offset_path(self, inter_channels, activation_type, num_blocks, channels_per_kpt, upsample_factor):
        blocks = []
        blocks.extend(
            [
                (
                    f"conv_bn_act_{block_index:02d}",
                    ConvBNAct(channels_per_kpt, channels_per_kpt, kernel_size=3, stride=1, padding=1, bias=False, activation_type=activation_type),
                )
                for block_index in range(num_blocks)
            ]
        )

        blocks += [("final", nn.Conv2d(channels_per_kpt, 2 * (2**upsample_factor), kernel_size=1)), ("shuffle", nn.PixelShuffle(upsample_factor))]
        return nn.Sequential(collections.OrderedDict(blocks))

    @property
    def out_channels(self):
        return (self.num_classes_with_center), (2 * self.num_classes)


class YoloNASPose(CustomizableDetector):
    def __init__(self, arch_params: Union[HpmStruct, DictConfig]):
        super().__init__(
            backbone=arch_params.backbone,
            neck=arch_params.neck,
            heads=arch_params.heads,
            num_classes=get_param(arch_params, "num_classes", None),
            in_channels=get_param(arch_params, "in_channels", 3),
            bn_momentum=get_param(arch_params, "bn_momentum", None),
            bn_eps=get_param(arch_params, "bn_eps", None),
            inplace_act=get_param(arch_params, "inplace_act", None),
        )

    @staticmethod
    def get_post_prediction_callback(conf: float = 0.05):
        return DEKRPoseEstimationDecodeCallback(
            min_confidence=conf,
            keypoint_threshold=0.05,
            nms_threshold=0.05,
            apply_sigmoid=True,
            max_num_people=30,
            nms_num_threshold=8,
            output_stride=4,
        )

    @resolve_param("image_processor", ProcessingFactory())
    def set_dataset_processing_params(
        self,
        edge_links: Union[np.ndarray, List[Tuple[int, int]]],
        edge_colors: Union[np.ndarray, List[Tuple[int, int, int]]],
        keypoint_colors: Union[np.ndarray, List[Tuple[int, int, int]]],
        image_processor: Optional[Processing] = None,
        conf: Optional[float] = None,
    ) -> None:
        """Set the processing parameters for the dataset.

        :param image_processor: (Optional) Image processing objects to reproduce the dataset preprocessing used for training.
        :param conf:            (Optional) Below the confidence threshold, prediction are discarded
        """
        self._edge_links = edge_links or self._edge_links
        self._edge_colors = edge_colors or self._edge_colors
        self._keypoint_colors = keypoint_colors or self._keypoint_colors
        self._image_processor = image_processor or self._image_processor
        self._default_nms_conf = conf or self._default_nms_conf

    @lru_cache(maxsize=1)
    def _get_pipeline(self, conf: Optional[float] = None, fuse_model: bool = True) -> PoseEstimationPipeline:
        """Instantiate the prediction pipeline of this model.

        :param conf:    (Optional) Below the confidence threshold, prediction are discarded.
                        If None, the default value associated to the training is used.
        :param fuse_model: If True, create a copy of the model, and fuse some of its layers to increase performance. This increases memory usage.
        """
        if None in (self._edge_links, self._image_processor, self._default_nms_conf):
            raise RuntimeError(
                "You must set the dataset processing parameters before calling predict.\n" "Please call `model.set_dataset_processing_params(...)` first."
            )

        conf = conf or self._default_nms_conf

        if len(self._keypoint_colors) != self.num_classes:
            raise RuntimeError(
                "The number of colors for the keypoints ({}) does not match the number of joints ({})".format(len(self._keypoint_colors), self.num_classes)
            )
        if len(self._edge_colors) != len(self._edge_links):
            raise RuntimeError(
                "The number of colors for the joints ({}) does not match the number of joint links ({})".format(len(self._edge_colors), len(self._edge_links))
            )

        pipeline = PoseEstimationPipeline(
            model=self,
            image_processor=self._image_processor,
            edge_links=self._edge_links,
            edge_colors=self._edge_colors,
            keypoint_colors=self._keypoint_colors,
            post_prediction_callback=self.get_post_prediction_callback(conf=conf),
            fuse_model=fuse_model,
        )
        return pipeline

    def predict(self, images: ImageSource, conf: Optional[float] = None, fuse_model: bool = True) -> ImagesPoseEstimationPrediction:
        """Predict an image or a list of images.

        :param images:  Images to predict.
        :param conf:    (Optional) Below the confidence threshold, prediction are discarded.
                        If None, the default value associated to the training is used.
        :param fuse_model: If True, create a copy of the model, and fuse some of its layers to increase performance. This increases memory usage.
        """
        pipeline = self._get_pipeline(conf=conf, fuse_model=fuse_model)
        return pipeline(images)  # type: ignore

    def predict_webcam(self, conf: Optional[float] = None, fuse_model: bool = True):
        """Predict using webcam.

        :param conf:    (Optional) Below the confidence threshold, prediction are discarded.
                        If None, the default value associated to the training is used.
        :param fuse_model: If True, create a copy of the model, and fuse some of its layers to increase performance. This increases memory usage.
        """
        pipeline = self._get_pipeline(conf=conf, fuse_model=fuse_model)
        pipeline.predict_webcam()

    def train(self, mode: bool = True):
        self._get_pipeline.cache_clear()
        torch.cuda.empty_cache()
        return super().train(mode)

    @property
    def num_classes(self):
        return self.heads.num_classes


@register_model(Models.YOLO_NAS_POSE_S)
class YoloNASPose_S(YoloNASPose):
    def __init__(self, arch_params: Union[HpmStruct, DictConfig]):
        default_arch_params = get_arch_params("yolo_nas_pose_s_arch_params")
        merged_arch_params = HpmStruct(**copy.deepcopy(default_arch_params))
        merged_arch_params.override(**arch_params.to_dict())
        super().__init__(merged_arch_params)


@register_model(Models.YOLO_NAS_POSE_M)
class YoloNASPose_M(YoloNASPose):
    def __init__(self, arch_params: Union[HpmStruct, DictConfig]):
        default_arch_params = get_arch_params("yolo_nas_pose_m_arch_params")
        merged_arch_params = HpmStruct(**copy.deepcopy(default_arch_params))
        merged_arch_params.override(**arch_params.to_dict())
        super().__init__(merged_arch_params)


@register_model(Models.YOLO_NAS_POSE_L)
class YoloNASPose_L(YoloNASPose):
    def __init__(self, arch_params: Union[HpmStruct, DictConfig]):
        default_arch_params = get_arch_params("yolo_nas_pose_l_arch_params")
        merged_arch_params = HpmStruct(**copy.deepcopy(default_arch_params))
        merged_arch_params.override(**arch_params.to_dict())
        super().__init__(merged_arch_params)