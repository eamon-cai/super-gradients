import os
import unittest
import tempfile
from pathlib import Path

from super_gradients.common.object_names import Models
from super_gradients.training import models
from super_gradients.training.datasets import COCODetectionDataset


class TestModelPredict(unittest.TestCase):
    def setUp(self) -> None:
        rootdir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self.images = [
            os.path.join(rootdir, "documentation", "source", "images", "examples", "countryside.jpg"),
            os.path.join(rootdir, "documentation", "source", "images", "examples", "street_busy.jpg"),
            "https://deci-datasets-research.s3.amazonaws.com/image_samples/beatles-abbeyroad.jpg",
        ]
        self._set_images_with_targets()

    def _set_images_with_targets(self):
        mini_coco_data_dir = str(Path(__file__).parent.parent / "data" / "tinycoco")
        dataset = COCODetectionDataset(
            data_dir=mini_coco_data_dir, subdir="images/val2017", json_file="instances_val2017.json", input_dim=None, transforms=[], cache_annotations=False
        )
        # x's are np.ndarrays images of shape (H,W,3)
        # y's are np.ndarrays of shape (num_boxes,x1,y1,x2,y2,class_id)
        x1, y1, _ = dataset[0]
        x2, y2, _ = dataset[1]
        # images from COCODetectionDataset are RGB and images as np.ndarrays are expected to be BGR
        x2 = x2[:, :, ::-1]
        x1 = x1[:, :, ::-1]
        self.np_array_images = [x1, x2]
        self.np_array_target_bboxes = [y1[:, :4], y2[:, :4]]
        self.np_array_target_class_ids = [y1[:, 4], y2[:, 4]]

    def test_classification_models(self):
        with tempfile.TemporaryDirectory() as tmp_dirname:
            for model_name in {Models.RESNET18, Models.EFFICIENTNET_B0, Models.MOBILENET_V2, Models.REGNETY200}:
                model = models.get(model_name, pretrained_weights="imagenet")

                predictions = model.predict(self.images)
                predictions.show()
                predictions.save(output_folder=tmp_dirname)

    def test_pose_estimation_models(self):
        model = models.get(Models.DEKR_W32_NO_DC, pretrained_weights="coco_pose")

        with tempfile.TemporaryDirectory() as tmp_dirname:
            predictions = model.predict(self.images)
            predictions.show()
            predictions.save(output_folder=tmp_dirname)

    def test_detection_models(self):
        for model_name in [Models.YOLO_NAS_S, Models.YOLOX_S, Models.PP_YOLOE_S]:
            model = models.get(model_name, pretrained_weights="coco")

            with tempfile.TemporaryDirectory() as tmp_dirname:
                predictions = model.predict(self.images)
                predictions.show()
                predictions.save(output_folder=tmp_dirname)

    def test_detection_models_with_targets(self):
        for model_name in [Models.YOLO_NAS_S, Models.YOLOX_S, Models.PP_YOLOE_S]:
            model = models.get(model_name, pretrained_weights="coco")

            with tempfile.TemporaryDirectory() as tmp_dirname:
                predictions = model.predict(self.np_array_images)
                predictions.show(target_bboxes=self.np_array_target_bboxes, target_class_ids=self.np_array_target_class_ids, target_bboxes_format="xyxy")
                predictions.save(
                    output_folder=tmp_dirname,
                    target_bboxes=self.np_array_target_bboxes,
                    target_class_ids=self.np_array_target_class_ids,
                    target_bboxes_format="xyxy",
                )

    def test_predict_class_names(self):
        for model_name in [Models.YOLO_NAS_S, Models.YOLOX_S, Models.PP_YOLOE_S]:
            model = models.get(model_name, pretrained_weights="coco")

            predictions = model.predict(self.np_array_images)
            _ = predictions.show(class_names=["person", "bicycle", "car", "motorcycle", "airplane", "bus"])

            with self.assertRaises(ValueError):
                _ = predictions.show(class_names=["human"])


if __name__ == "__main__":
    unittest.main()
