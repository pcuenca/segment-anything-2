import argparse
import os
import ast
import coremltools as ct
import torch
from coremltools.converters.mil._deployment_compatibility import AvailableTarget
from coremltools import ComputeUnit
from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor
from PIL import Image
import numpy as np
import enum

class SAM2Variant(enum.Enum):
    Tiny = "tiny"
    Small = "small"
    BasePlus = "base_plus"
    Large = "large"

def parse_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--output-dir",
        type=str,
        default=".",
        help="Provide location to save exported models.",
    )
    parser.add_argument(
        "--variant",
        type=SAM2Variant,
        choices=list(SAM2Variant),
        default=SAM2Variant.Small,
        help="SAM2 variant to export.",
    )
    parser.add_argument(
        "--points",
        type=str,
        help="List of 2D points, e.g., '[[10,20], [30,40]]'",
        required=True
    )
    parser.add_argument(
        "--labels",
        type=str,
        help="List of binary labels for each points entry, denoting foreground (1) or background (0).",
        required=True
    )
    parser.add_argument(
        "--min-deployment-target",
        type=lambda x: getattr(AvailableTarget, x),
        choices=[target.name for target in AvailableTarget],
        default=AvailableTarget.iOS17,
        help="Minimum deployment target for CoreML model.",
    )
    parser.add_argument(
        "--compute-units",
        type=lambda x: getattr(ComputeUnit, x),
        choices=[cu for cu in ComputeUnit],
        default=ComputeUnit.ALL,
        help="Which compute units to target for CoreML model.",
    )
    return parser

from coremltools.converters.mil import register_torch_op
from coremltools.converters.mil.mil import Builder as mb

@register_torch_op
def upsample_bicubic2d(context, node):
    x = context[node.inputs[0]]
    output_size = context[node.inputs[1]].val
    
    scale_factor_height = output_size[0] / x.shape[2]
    scale_factor_width = output_size[1] / x.shape[3]

    #align_corners = context[node.inputs[2]].val #false anyway
    # No upsample_bicubic2d in coremltools
    # Upsample_bilinear rounds
    x = mb.upsample_nearest_neighbor(x=x, scale_factor_height=scale_factor_height, scale_factor_width=scale_factor_width, name=node.name)
    context.add(x)

class SAM2ImageEmbedder(torch.nn.Module):
    def __init__(self, model: SAM2ImagePredictor):
        super().__init__()
        self.model = model
    
    @torch.no_grad()
    def forward(self, image):
        img_embedding = self.model.generate_image_embedding(image)
        return img_embedding

class SAM2PromptEncoder(torch.nn.Module):
    def __init__(self, model: SAM2ImagePredictor):
        super().__init__()
        self.model = model
    
    @torch.no_grad()
    def forward(self, points, labels):
        prompt_embedding = self.model.encode_points_prompt(points, labels)
        return prompt_embedding 

def export_image_embedder(image_predictor: SAM2ImagePredictor, output_dir: str, min_deployment_target: AvailableTarget, compute_units: ComputeUnit):
    # Prepare input tensors
    image = Image.open('notebooks/images/truck.jpg')
    image = np.array(image.convert("RGB"))

    prepared_images = image_predictor._transforms(image)
    prepared_images = prepared_images[None, ...].to("cpu")
    sam2 = SAM2ImageEmbedder(image_predictor)
    
    # Trace the model
    traced_model = torch.jit.trace(sam2, prepared_images)
    
    output_path = os.path.join(output_dir, f"sam2_image_embedder")
    pt_name = output_path + ".pt"
    traced_model.save(pt_name)
    
    # Convert to CoreML
    mlmodel = ct.convert(
        traced_model,
        inputs=[
            ct.TensorType(name="image", shape=(1,3,1024,1024)),
        ],
        outputs=[
            ct.TensorType(name="embedding"),
        ],
        minimum_deployment_target=min_deployment_target,
        compute_units=compute_units,
    )
    
    # Save the CoreML model
    mlmodel.save(output_path + ".mlpackage")

def export_prompt_encoder(image_predictor: SAM2ImagePredictor, points: list, labels: list, output_dir: str, min_deployment_target: AvailableTarget, compute_units: ComputeUnit):
    image_predictor.model.sam_prompt_encoder.eval()

    points = torch.tensor(points, dtype=torch.float32)
    labels = torch.tensor(labels, dtype=torch.int32)

    unnorm_coords = image_predictor._transforms.transform_coords(
        points, normalize=True, orig_hw=(1200, 1800)
    )
    if len(unnorm_coords.shape) == 2:
        unnorm_coords, labels = unnorm_coords[None, ...], labels[None, ...]

    print(unnorm_coords)

    sam2 = SAM2PromptEncoder(image_predictor)

    # Trace the model
    traced_model = torch.jit.trace(sam2, (unnorm_coords, labels))
    
    output_path = os.path.join(output_dir, f"sam2_prompt_encoder")
    pt_name = output_path + ".pt"
    traced_model.save(pt_name)

    #points_shapes = ct.EnumeratedShapes(shapes=[[1, i, 2] for i in range(1, 16)])
    #labels_shapes = ct.EnumeratedShapes(shapes=[[1, i] for i in range(1, 16)])

    points_shape = ct.Shape(shape=(1, ct.RangeDim(lower_bound=1, upper_bound=16), 2))
    labels_shape = ct.Shape(shape=(1, ct.RangeDim(lower_bound=1, upper_bound=16)))

    # Convert to CoreML
    mlmodel = ct.convert(
        traced_model,
        inputs=[
            ct.TensorType(name="points", shape=points_shape),
            ct.TensorType(name="labels", shape=labels_shape),
        ],
        outputs=[
            ct.TensorType(name="embedding"),
        ],
        minimum_deployment_target=min_deployment_target,
        compute_units=compute_units,
    )
    
    # Save the CoreML model
    mlmodel.save(output_path + ".mlpackage")

def export(output_dir: str, variant: SAM2Variant, points: list, labels: list, min_deployment_target: AvailableTarget, compute_units: ComputeUnit):
    os.makedirs(output_dir, exist_ok=True)
    device = torch.device("cpu")

    # Build SAM2 model
    sam2_checkpoint = f"/Users/fleetwood/Code/segment-anything-2/checkpoints/sam2_hiera_small.pt"
    model_cfg = "sam2_hiera_s.yaml"

    with torch.no_grad():
        model = build_sam2(model_cfg, sam2_checkpoint, device=device)
        img_predictor = SAM2ImagePredictor(model)
        img_predictor.model.eval()

    #export_image_embedder(img_predictor, output_dir, min_deployment_target, compute_units)
    export_prompt_encoder(img_predictor, points, labels, output_dir, min_deployment_target, compute_units)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SAM2 -> CoreML CLI"
    )
    parser = parse_args(parser)
    args = parser.parse_args()
    
    # Process points and labels
    try:
        points = ast.literal_eval(args.points)
        labels = ast.literal_eval(args.labels)
    except (SyntaxError, ValueError) as e:
        raise ValueError(f"Invalid format for points or labels: {e}")
    
    if not isinstance(points, list) or not all(isinstance(p, list) and len(p) == 2 for p in points):
        raise ValueError("Points must be a list of [x, y] coordinates")
    
    if not isinstance(labels, list) or not all(isinstance(l, int) and l in [0, 1] for l in labels):
        raise ValueError("Labels must denote foreground (1) or background (0)")
    
    if len(points) != len(labels):
        raise ValueError("Number of points must match the number of labels")
    
    export(args.output_dir, args.variant, points, labels, args.min_deployment_target, args.compute_units)