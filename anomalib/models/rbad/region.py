#  COSMONiO © All rights reserved.
#  This file is subject to the terms and conditions defined in file 'LICENSE.txt',
#  which is part of this source code package.

from collections import OrderedDict

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models.detection as detection
from torchvision.ops import boxes as box_ops


def extract_patches_from_frame(frame, boxes, isize, channels=3, out_type=np.float64):
    if torch.is_tensor(boxes):
        boxes = boxes.numpy()

    assert isinstance(frame, np.ndarray)
    assert frame.ndim in [2, 3]
    if frame.ndim == 3:
        assert frame.shape[2] in [1, 3]  # Order of channels is unimportant, they simply remain consistent

    assert isinstance(boxes, np.ndarray)
    assert boxes.ndim == 2
    assert boxes.shape[1] == 4

    assert 0 <= channels <= 3

    if boxes.dtype in [np.float32, np.float64]:
        boxes = np.round(boxes).astype(np.int32)

    np_type = {
        np.uint8: np.uint8,
        np.float32: np.float32,
        np.float64: np.float64,
        torch.uint8: np.uint8,
        torch.float32: np.float32,
        torch.float64: np.float64,
    }

    if channels == 0:
        patches = np.empty((boxes.shape[0], isize, isize), dtype=np_type[out_type])
    else:
        patches = np.empty((boxes.shape[0], isize, isize, channels), dtype=np_type[out_type])

    for idx, box in enumerate(boxes):
        if channels == 0:
            if frame.ndim == 3:
                if frame.shape[2] == 1:
                    patches[idx, :, :] = frame[box[1] : box[3], box[0] : box[2], 0]
                elif frame.shape[2] == 3:
                    patches[idx, :, :] = cv2.cvtColor(frame[box[1] : box[3], box[0] : box[2], :], cv2.COLOR_BGR2GRAY)
            elif frame.ndim == 2:
                patches[idx, :, :] = frame[box[1] : box[3], box[0] : box[2]]
        else:
            for c in range(channels):
                if frame.ndim == 3:
                    if frame.shape[2] == 1:
                        patches[idx, :, :, c] = frame[box[1] : box[3], box[0] : box[2], 0]
                    elif frame.shape[2] == 3:
                        if channels == 1:
                            patches[idx, :, :, c] = cv2.cvtColor(
                                frame[box[1] : box[3], box[0] : box[2], :], cv2.COLOR_BGR2GRAY
                            )
                        else:
                            patches[idx, :, :, c] = frame[box[1] : box[3], box[0] : box[2], c]
                else:
                    patches[idx, :, :, c] = frame[box[1] : box[3], box[0] : box[2]]

    if torch.is_tensor(patches):
        patches = torch.from_numpy(patches)

    return patches


def tiled_boxes(frame, isize, dtype=np.int32):
    frame_size_rc = frame.shape[:2]

    tile_count_r = np.ceil(frame_size_rc[0] / isize).astype(np.int64)
    tile_count_c = np.ceil(frame_size_rc[1] / isize).astype(np.int64)

    tile_stride_r = (frame_size_rc[0] - isize) / (tile_count_r - 1)
    tile_stride_c = (frame_size_rc[1] - isize) / (tile_count_c - 1)

    boxes = np.empty((tile_count_r * tile_count_c, 4), dtype=dtype)
    for r in range(tile_count_r):
        for c in range(tile_count_c):
            tile_row = np.floor(r * tile_stride_r).astype(np.int32)
            tile_col = np.floor(c * tile_stride_c).astype(np.int32)
            boxes[tile_count_c * r + c, :] = [tile_col, tile_row, tile_col + isize, tile_row + isize]

    return boxes


def likelihood_to_class_threshold(likelihood):
    threshold = np.cos(likelihood * np.pi / 2.0) ** 4.0
    return threshold


class RegionExtractor(nn.Module):
    def __init__(self, stage="rcnn", patch_mode=False, patch_size=32, use_original=False, **kwargs):
        # kwargs gives the configurable parameters
        # kwargs.keys() == {'max_overlap', 'min_size', 'likelihood'}

        assert stage in ["rpn", "rcnn"]

        super(RegionExtractor, self).__init__()

        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.pseudo_scores = torch.arange(1000, 0, -1, dtype=torch.float32, device=device)

        # Affects global behaviour of the extractor
        self.use_original = use_original
        self.stage = stage
        self.patch_mode = patch_mode

        self.min_size = 25 if "min_size" not in kwargs else kwargs["min_size"]
        self.stage_nms_thresh = 0.3 if "max_overlap" not in kwargs else kwargs["max_overlap"]
        self.box_confidence = 0.2 if "likelihood" not in kwargs else likelihood_to_class_threshold(kwargs["likelihood"])

        # Affects operation only when stage='rcnn'
        self.rcnn_score_thresh = self.box_confidence
        self.rcnn_detections_per_img = 100

        # Affects operation only when patch_mode == True
        self.patch_side_length = patch_size
        self.patch_nms_thresh = 0.3

        # Model and model components
        self.base_model = detection.fasterrcnn_resnet50_fpn(
            pretrained=True, rpn_pre_nms_top_n_test=1000, rpn_nms_thresh=0.7, rpn_post_nms_top_n_test=1000
        )

        self.transform = self.base_model.transform
        self.backbone = self.base_model.backbone
        self.rpn = self.base_model.rpn

        self.box_roi_pool = self.base_model.roi_heads.box_roi_pool
        self.box_head = self.base_model.roi_heads.box_head
        self.box_predictor = self.base_model.roi_heads.box_predictor

        self.box_coder = self.base_model.roi_heads.box_coder

    @torch.no_grad()
    def forward(self, cv2_images):
        if self.training:
            raise ValueError("Should not be in training mode")

        assert isinstance(cv2_images, list)
        assert isinstance(cv2_images[0], np.ndarray)
        assert cv2_images[0].ndim == 3
        assert cv2_images[0].shape[2] == 3

        input_list = []
        for image in cv2_images:
            image = image.astype(np.float32)
            image /= 255.0
            image = torch.from_numpy(image).contiguous()
            image = image.permute(2, 0, 1)
            input_list.append(image)

        for p in self.parameters():
            device = p.device
            input_list = [i.to(device) for i in input_list]
            break

        if self.use_original:
            out = self.base_model(input_list)
            out = [x["boxes"].cpu().numpy() for x in out]
            return out
        else:
            original_image_sizes = [img.shape[-2:] for img in input_list]
            images, targets = self.transform(input_list)
            new_image_sizes = images.image_sizes

            features = self.backbone(images.tensors)
            if isinstance(features, torch.Tensor):
                features = OrderedDict([(0, features)])

            proposals, proposal_losses = self.rpn(images, features, targets)

            if self.stage == "rpn":
                output_boxes = []
                output_scores = []
                for boxes, original_image_size, new_image_size in zip(proposals, original_image_sizes, new_image_sizes):
                    boxes = box_ops.clip_boxes_to_image(boxes, new_image_size)

                    keep = box_ops.remove_small_boxes(boxes, min_size=self.min_size)
                    boxes = boxes[keep]

                    keep = box_ops.nms(boxes, self.pseudo_scores[: boxes.shape[0]], self.stage_nms_thresh)
                    boxes = boxes[keep]

                    boxes = update_box_sizes_following_image_resize(boxes, new_image_size, original_image_size)

                    if self.patch_mode:
                        boxes = convert_to_patch_boxes(boxes, original_image_size, self.patch_side_length)

                        keep = box_ops.nms(boxes, self.pseudo_scores[: boxes.shape[0]], self.patch_nms_thresh)
                        boxes = boxes[keep]

                    output_boxes.append(boxes)
            else:
                box_features = self.box_roi_pool(features, proposals, new_image_sizes)
                box_features = self.box_head(box_features)
                class_logits, box_regression = self.box_predictor(box_features)
                boxes_list, scores_list, _ = self.postprocess_detections(
                    class_logits, box_regression, proposals, new_image_sizes
                )

                output_boxes = []
                output_scores = []
                for boxes, scores, original_image_size, new_image_size in zip(
                    boxes_list, scores_list, original_image_sizes, new_image_sizes
                ):
                    boxes = update_box_sizes_following_image_resize(boxes, new_image_size, original_image_size)

                    if self.patch_mode:
                        boxes = convert_to_patch_boxes(boxes, original_image_size, self.patch_side_length)

                        keep = box_ops.nms(boxes, self.pseudo_scores[: boxes.shape[0]], self.patch_nms_thresh)
                        boxes = boxes[keep]
                        scores = scores[keep]

                    output_boxes.append(boxes)
                    output_scores.append(scores)

            output_boxes = [b.cpu().detach().numpy() for b in output_boxes]
            output_scores = [s.view((-1, 1)).cpu().detach().numpy() for s in output_scores]

            return output_boxes[0]

    def postprocess_detections(self, class_logits, box_regression, proposals, image_shapes):
        device = class_logits.device
        num_classes = class_logits.shape[-1]

        boxes_per_image = [len(boxes_in_image) for boxes_in_image in proposals]
        pred_boxes = self.box_coder.decode(box_regression, proposals)

        pred_scores = F.softmax(class_logits, -1)

        # split boxes and scores per image
        pred_boxes = pred_boxes.split(boxes_per_image, 0)
        pred_scores = pred_scores.split(boxes_per_image, 0)

        all_boxes = []
        all_scores = []
        all_labels = []

        for boxes, scores, image_shape in zip(pred_boxes, pred_scores, image_shapes):
            boxes = box_ops.clip_boxes_to_image(boxes, image_shape)

            # create labels for each prediction
            labels = torch.arange(num_classes, device=device)
            labels = labels.view(1, -1).expand_as(scores)

            # remove predictions with the background label
            boxes = boxes[:, 1:]
            scores = scores[:, 1:]
            labels = labels[:, 1:]

            # batch everything, by making every class prediction be a separate instance
            boxes = boxes.reshape(-1, 4)
            scores = scores.flatten()
            labels = labels.flatten()

            # remove low scoring boxes
            inds = torch.nonzero(scores > self.rcnn_score_thresh).squeeze(1)
            boxes, scores, labels = boxes[inds], scores[inds], labels[inds]

            # remove small boxes
            keep = box_ops.remove_small_boxes(boxes, min_size=self.min_size)
            boxes, scores, labels = boxes[keep], scores[keep], labels[keep]

            # non-maximum suppression, all boxes together
            keep = box_ops.nms(boxes, scores, self.stage_nms_thresh)

            # keep only topk scoring predictions
            keep = keep[: self.rcnn_detections_per_img]
            boxes, scores, labels = boxes[keep], scores[keep], labels[keep]

            all_boxes.append(boxes)
            all_scores.append(scores)
            all_labels.append(labels)

        return all_boxes, all_scores, all_labels


def update_box_sizes_following_image_resize(boxes, original_size, new_size):
    ratios = tuple(float(s) / float(s_orig) for s, s_orig in zip(new_size, original_size))
    ratio_height, ratio_width = ratios
    xmin, ymin, xmax, ymax = boxes.unbind(1)
    xmin = xmin * ratio_width
    xmax = xmax * ratio_width
    ymin = ymin * ratio_height
    ymax = ymax * ratio_height
    return torch.stack((xmin, ymin, xmax, ymax), dim=1)


def convert_to_patch_boxes(boxes, frame_size, target_side_length):
    # boxes:      x1, y1, x2, y2
    # frame_size: (height, width)

    boxes = boxes.round_().to(torch.int32)
    assert target_side_length % 2 == 0
    assert isinstance(frame_size[0], int)
    assert isinstance(frame_size[1], int)
    assert isinstance(target_side_length, int)
    assert isinstance(boxes, torch.Tensor)
    assert boxes.dtype == torch.int32

    widths = boxes[:, 2] - boxes[:, 0]
    heights = boxes[:, 3] - boxes[:, 1]

    x2 = boxes[:, 2]
    y2 = boxes[:, 3]

    x2[(widths % 2) == 1] += 1
    y2[(heights % 2) == 1] += 1

    widths = boxes[:, 2] - boxes[:, 0]
    heights = boxes[:, 3] - boxes[:, 1]
    assert (widths % 2 == 0).all()
    assert (heights % 2 == 0).all()

    deltas = torch.zeros_like(boxes)
    deltas[:, 0] = (widths - target_side_length) / 2
    deltas[:, 1] = (heights - target_side_length) / 2
    deltas[:, 2] = (target_side_length - widths) / 2
    deltas[:, 3] = (target_side_length - heights) / 2

    boxes = boxes + deltas

    overhang = boxes[:, 0] * -1
    overhang_mask = overhang < 0
    overhang[overhang_mask] = 0
    boxes[:, 0] += overhang
    boxes[:, 2] += overhang

    overhang = boxes[:, 1] * -1
    overhang_mask = overhang < 0
    overhang[overhang_mask] = 0
    boxes[:, 1] += overhang
    boxes[:, 3] += overhang

    overhang = boxes[:, 2] - frame_size[1] + 1
    overhang_mask = overhang < 0
    overhang[overhang_mask] = 0
    boxes[:, 0] -= overhang
    boxes[:, 2] -= overhang

    overhang = boxes[:, 3] - frame_size[0] + 1
    overhang_mask = overhang < 0
    overhang[overhang_mask] = 0
    boxes[:, 1] -= overhang
    boxes[:, 3] -= overhang

    boxes = boxes.to(torch.float32)
    return boxes
