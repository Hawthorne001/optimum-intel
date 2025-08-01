#  Copyright 2021 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import copy
import gc
import os
import platform
import tempfile
import time
import unittest
from pathlib import Path
from typing import Dict, Generator

import numpy as np
import open_clip
import openvino as ov
import pytest
import requests
import timm
import torch
from datasets import load_dataset
from evaluate import evaluator
from huggingface_hub import hf_hub_download, snapshot_download
from parameterized import parameterized
from PIL import Image
from sentence_transformers import SentenceTransformer
from transformers import (
    AutoConfig,
    AutoFeatureExtractor,
    AutoImageProcessor,
    AutoModel,
    AutoModelForAudioClassification,
    AutoModelForAudioFrameClassification,
    AutoModelForAudioXVector,
    AutoModelForCausalLM,
    AutoModelForCTC,
    AutoModelForImageClassification,
    AutoModelForMaskedLM,
    AutoModelForQuestionAnswering,
    AutoModelForSeq2SeqLM,
    AutoModelForSequenceClassification,
    AutoModelForSpeechSeq2Seq,
    AutoModelForTextToSpectrogram,
    AutoModelForTokenClassification,
    AutoModelForVision2Seq,
    AutoModelForZeroShotImageClassification,
    AutoProcessor,
    AutoTokenizer,
    GenerationConfig,
    Pix2StructForConditionalGeneration,
    PretrainedConfig,
    pipeline,
    set_seed,
)
from transformers.onnx.utils import get_preprocessor
from transformers.testing_utils import slow
from transformers.utils import http_user_agent
from utils_tests import (
    MODEL_NAMES,
    TEST_IMAGE_URL,
    get_num_sdpa,
    mock_torch_cuda_is_available,
    patch_awq_for_inference,
)

from optimum.exporters.openvino.model_patcher import patch_update_causal_mask
from optimum.exporters.openvino.stateful import model_has_state
from optimum.intel import (
    OVDiffusionPipeline,
    OVFluxPipeline,
    OVModelForAudioClassification,
    OVModelForAudioFrameClassification,
    OVModelForAudioXVector,
    OVModelForCausalLM,
    OVModelForCTC,
    OVModelForCustomTasks,
    OVModelForFeatureExtraction,
    OVModelForImageClassification,
    OVModelForMaskedLM,
    OVModelForPix2Struct,
    OVModelForQuestionAnswering,
    OVModelForSeq2SeqLM,
    OVModelForSequenceClassification,
    OVModelForSpeechSeq2Seq,
    OVModelForTextToSpeechSeq2Seq,
    OVModelForTokenClassification,
    OVModelForVision2Seq,
    OVModelForVisualCausalLM,
    OVModelForZeroShotImageClassification,
    OVModelOpenCLIPForZeroShotImageClassification,
    OVSamModel,
    OVSentenceTransformer,
    OVStableDiffusionPipeline,
)
from optimum.intel.openvino import OV_DECODER_NAME, OV_DECODER_WITH_PAST_NAME, OV_ENCODER_NAME, OV_XML_FILE_NAME
from optimum.intel.openvino.modeling_base import OVBaseModel
from optimum.intel.openvino.modeling_seq2seq import OVDecoder, OVEncoder
from optimum.intel.openvino.modeling_timm import TimmImageProcessor
from optimum.intel.openvino.modeling_visual_language import (
    MODEL_PARTS_CLS_MAPPING,
    MODEL_TYPE_TO_CLS_MAPPING,
)
from optimum.intel.openvino.utils import (
    OV_LANGUAGE_MODEL_NAME,
    OV_PROMPT_ENCODER_MASK_DECODER_MODEL_NAME,
    OV_TEXT_EMBEDDINGS_MODEL_NAME,
    OV_VISION_EMBEDDINGS_MODEL_NAME,
    OV_VISION_ENCODER_MODEL_NAME,
    TemporaryDirectory,
    _print_compiled_model_properties,
)
from optimum.intel.pipelines import pipeline as optimum_pipeline
from optimum.intel.utils.import_utils import (
    _langchain_hf_available,
    is_openvino_version,
    is_transformers_version,
)
from optimum.intel.utils.modeling_utils import _find_files_matching_pattern
from optimum.utils import (
    DIFFUSION_MODEL_TEXT_ENCODER_2_SUBFOLDER,
    DIFFUSION_MODEL_TEXT_ENCODER_SUBFOLDER,
    DIFFUSION_MODEL_TRANSFORMER_SUBFOLDER,
    DIFFUSION_MODEL_UNET_SUBFOLDER,
    DIFFUSION_MODEL_VAE_DECODER_SUBFOLDER,
    DIFFUSION_MODEL_VAE_ENCODER_SUBFOLDER,
)
from optimum.utils.testing_utils import require_diffusers


TENSOR_ALIAS_TO_TYPE = {
    "pt": torch.Tensor,
    "np": np.ndarray,
}

SEED = 42

F32_CONFIG = {"INFERENCE_PRECISION_HINT": "f32"}


class Timer(object):
    def __enter__(self):
        self.elapsed = time.perf_counter()
        return self

    def __exit__(self, type, value, traceback):
        self.elapsed = (time.perf_counter() - self.elapsed) * 1e3


class OVModelIntegrationTest(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.OV_MODEL_ID = "echarlaix/distilbert-base-uncased-finetuned-sst-2-english-openvino"
        self.OV_DECODER_MODEL_ID = "helenai/gpt2-ov"
        self.OV_SEQ2SEQ_MODEL_ID = "echarlaix/t5-small-openvino"
        self.OV_SD_DIFFUSION_MODEL_ID = "katuni4ka/tiny-stable-diffusion-openvino"
        self.OV_FLUX_DIFFUSION_MODEL_ID = "katuni4ka/tiny-random-flux-ov"
        self.OV_VLM_MODEL_ID = "katuni4ka/tiny-random-llava-ov"
        self.OV_SAM_MODEL_ID = "katuni4ka/sam-vit-tiny-random-ov"
        self.OV_TEXTSPEECH_MODEL_ID = "optimum-internal-testing/tiny-random-SpeechT5ForTextToSpeech-openvino"

    def test_load_from_hub_and_save_model(self):
        tokenizer = AutoTokenizer.from_pretrained(self.OV_MODEL_ID)
        tokens = tokenizer("This is a sample input", return_tensors="pt")
        loaded_model = OVModelForSequenceClassification.from_pretrained(self.OV_MODEL_ID)
        self.assertIsInstance(loaded_model.config, PretrainedConfig)
        # Test that PERFORMANCE_HINT is set to LATENCY by default
        self.assertEqual(loaded_model.ov_config.get("PERFORMANCE_HINT"), "LATENCY")
        self.assertEqual(loaded_model.request.get_property("PERFORMANCE_HINT"), "LATENCY")
        loaded_model_outputs = loaded_model(**tokens)

        # Test specifying ov_config with throughput hint and manual cache dir
        manual_openvino_cache_dir = loaded_model.model_save_dir / "manual_model_cache"
        ov_config = {"CACHE_DIR": str(manual_openvino_cache_dir), "PERFORMANCE_HINT": "THROUGHPUT"}
        loaded_model = OVModelForSequenceClassification.from_pretrained(self.OV_MODEL_ID, ov_config=ov_config)
        self.assertTrue(manual_openvino_cache_dir.is_dir())
        num_blobs = len(list(manual_openvino_cache_dir.glob("*.blob")))
        self.assertGreaterEqual(num_blobs, 1)
        if is_openvino_version("<", "2023.3"):
            self.assertEqual(loaded_model.request.get_property("PERFORMANCE_HINT").name, "THROUGHPUT")
        else:
            self.assertEqual(loaded_model.request.get_property("PERFORMANCE_HINT"), "THROUGHPUT")

        # Test compile only

        compile_only_model = OVModelForSequenceClassification.from_pretrained(
            self.OV_MODEL_ID, ov_config=ov_config, compile_only=True
        )
        self.assertTrue(manual_openvino_cache_dir.is_dir())
        current_num_blobs = len(list(manual_openvino_cache_dir.glob("*.blob")))
        # compile_only get model from cache
        self.assertGreaterEqual(current_num_blobs, num_blobs)
        self.assertIsInstance(compile_only_model.model, ov.CompiledModel)
        self.assertIsInstance(compile_only_model.request, ov.CompiledModel)
        outputs = compile_only_model(**tokens)
        self.assertTrue(torch.equal(loaded_model_outputs.logits, outputs.logits))
        del compile_only_model

        with TemporaryDirectory() as tmpdirname:
            loaded_model.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            self.assertTrue(OV_XML_FILE_NAME in folder_contents)
            self.assertTrue(OV_XML_FILE_NAME.replace(".xml", ".bin") in folder_contents)
            model = OVModelForSequenceClassification.from_pretrained(tmpdirname, ov_config={"NUM_STREAMS": 2})
            # Test that PERFORMANCE_HINT is set to LATENCY by default even with ov_config provided
            self.assertEqual(model.ov_config.get("PERFORMANCE_HINT"), "LATENCY")
            self.assertEqual(model.request.get_property("PERFORMANCE_HINT"), "LATENCY")

        outputs = model(**tokens)
        self.assertTrue(torch.equal(loaded_model_outputs.logits, outputs.logits))

        del loaded_model
        del model
        gc.collect()

    @parameterized.expand((True, False))
    def test_load_from_hub_and_save_decoder_model(self, use_cache):
        model_id = "vuiseng9/ov-gpt2-fp32-kv-cache" if use_cache else "vuiseng9/ov-gpt2-fp32-no-cache"
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokens = tokenizer("This is a sample input", return_tensors="pt")
        loaded_model = OVModelForCausalLM.from_pretrained(model_id, use_cache=use_cache)
        self.assertIsInstance(loaded_model.config, PretrainedConfig)
        # Test that PERFORMANCE_HINT is set to LATENCY by default
        self.assertEqual(loaded_model.ov_config.get("PERFORMANCE_HINT"), "LATENCY")
        self.assertEqual(loaded_model.request.get_compiled_model().get_property("PERFORMANCE_HINT"), "LATENCY")
        loaded_model_outputs = loaded_model(**tokens)

        with TemporaryDirectory() as tmpdirname:
            loaded_model.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            self.assertTrue(OV_XML_FILE_NAME in folder_contents)
            self.assertTrue(OV_XML_FILE_NAME.replace(".xml", ".bin") in folder_contents)
            model = OVModelForCausalLM.from_pretrained(tmpdirname, use_cache=use_cache)
            self.assertEqual(model.use_cache, use_cache)

            compile_only_model = OVModelForCausalLM.from_pretrained(tmpdirname, compile_only=True, use_cache=use_cache)
            self.assertIsInstance(compile_only_model.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_model.request, ov.InferRequest)
            outputs = compile_only_model(**tokens)
            self.assertTrue(torch.equal(loaded_model_outputs.logits, outputs.logits))
            del compile_only_model

        outputs = model(**tokens)
        self.assertTrue(torch.equal(loaded_model_outputs.logits, outputs.logits))
        del loaded_model
        del model
        gc.collect()

    @unittest.skipIf(
        is_transformers_version("<", "4.45"),
        "model tokenizer exported with tokenizers 0.20 is not compatible with old transformers",
    )
    def test_load_from_hub_and_save_visual_language_model(self):
        model_ids = [self.OV_VLM_MODEL_ID]
        if is_transformers_version(">=", "4.51"):
            model_ids.append("katuni4ka/phi-4-multimodal-ov")
        for model_id in model_ids:
            processor = get_preprocessor(model_id)
            prompt = "What is shown in this image?"
            image = Image.open(
                requests.get(
                    TEST_IMAGE_URL,
                    stream=True,
                ).raw
            )
            loaded_model = OVModelForVisualCausalLM.from_pretrained(model_id)
            self.assertIsInstance(loaded_model, MODEL_TYPE_TO_CLS_MAPPING[loaded_model.config.model_type])
            for component_name, component in loaded_model.components.items():
                self.assertIsInstance(component, MODEL_PARTS_CLS_MAPPING[component_name])
            self.assertIsInstance(loaded_model.config, PretrainedConfig)
            # Test that PERFORMANCE_HINT is set to LATENCY by default
            self.assertEqual(loaded_model.ov_config.get("PERFORMANCE_HINT"), "LATENCY")

            for component_name, component in loaded_model.components.items():
                self.assertIsInstance(component.model, ov.Model)
                if component_name == "language_model":
                    self.assertEqual(
                        component.request.get_compiled_model().get_property("PERFORMANCE_HINT"), "LATENCY"
                    )
                    self.assertIsInstance(component.text_emb_model, ov.Model)
                    self.assertEqual(component.text_emb_request.get_property("PERFORMANCE_HINT"), "LATENCY")
                else:
                    self.assertEqual(component.request.get_property("PERFORMANCE_HINT"), "LATENCY")
            if "llava" in model_id:
                processor.patch_size = loaded_model.config.vision_config.patch_size
            inputs = loaded_model.preprocess_inputs(text=prompt, image=image, processor=processor)
            set_seed(SEED)
            loaded_model_outputs = loaded_model(**inputs)

            with TemporaryDirectory() as tmpdirname:
                loaded_model.save_pretrained(tmpdirname)
                folder_contents = os.listdir(tmpdirname)
                model_files = [
                    OV_LANGUAGE_MODEL_NAME,
                    OV_TEXT_EMBEDDINGS_MODEL_NAME,
                    OV_VISION_EMBEDDINGS_MODEL_NAME,
                ]
                model_files += [f"openvino_{part}_model.xml" for part in loaded_model.additional_parts]
                for xml_file_name in model_files:
                    self.assertTrue(xml_file_name in folder_contents)
                    self.assertTrue(xml_file_name.replace(".xml", ".bin") in folder_contents)
                model = OVModelForVisualCausalLM.from_pretrained(tmpdirname)
                compile_only_model = OVModelForVisualCausalLM.from_pretrained(tmpdirname, compile_only=True)
                for _, submodel in compile_only_model.ov_submodels.items():
                    self.assertIsInstance(submodel, ov.runtime.CompiledModel)
                for component_name, component in compile_only_model.components.items():
                    self.assertIsInstance(component.model, ov.runtime.CompiledModel)
                    if component_name == "language_model":
                        self.assertIsInstance(component.request, ov.runtime.InferRequest)
                        self.assertIsInstance(component.text_emb_model, ov.runtime.CompiledModel)
                        self.assertIsInstance(component.text_emb_request, ov.runtime.CompiledModel)
                    else:
                        self.assertIsInstance(component.request, ov.runtime.CompiledModel)

                outputs = compile_only_model(**inputs)
                self.assertTrue(torch.equal(loaded_model_outputs.logits, outputs.logits))
                del compile_only_model

            outputs = model(**inputs)
            self.assertTrue(torch.equal(loaded_model_outputs.logits, outputs.logits))
            del loaded_model
            del model
            gc.collect()

    def test_load_from_hub_and_save_seq2seq_model(self):
        tokenizer = AutoTokenizer.from_pretrained(self.OV_SEQ2SEQ_MODEL_ID)
        tokens = tokenizer("This is a sample input", return_tensors="pt")
        loaded_model = OVModelForSeq2SeqLM.from_pretrained(self.OV_SEQ2SEQ_MODEL_ID, compile=False)
        self.assertIsInstance(loaded_model.config, PretrainedConfig)
        loaded_model.to("cpu")
        loaded_model.compile()
        # Test that PERFORMANCE_HINT is set to LATENCY by default
        self.assertEqual(loaded_model.ov_config.get("PERFORMANCE_HINT"), "LATENCY")
        self.assertEqual(loaded_model.decoder.request.get_compiled_model().get_property("PERFORMANCE_HINT"), "LATENCY")

        loaded_model_outputs = loaded_model.generate(**tokens)

        with TemporaryDirectory() as tmpdirname:
            loaded_model.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            self.assertTrue(OV_ENCODER_NAME in folder_contents)
            self.assertTrue(OV_DECODER_NAME in folder_contents)
            self.assertTrue(OV_DECODER_WITH_PAST_NAME in folder_contents)
            model = OVModelForSeq2SeqLM.from_pretrained(tmpdirname, device="cpu")
            # compile only
            compile_only_model = OVModelForSeq2SeqLM.from_pretrained(tmpdirname, compile_only=True)
            self.assertIsInstance(compile_only_model.encoder.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_model.decoder.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_model.decoder_with_past.model, ov.CompiledModel)
            outputs = compile_only_model.generate(**tokens)
            self.assertTrue(torch.equal(loaded_model_outputs, outputs))
            del compile_only_model

        outputs = model.generate(**tokens)
        self.assertTrue(torch.equal(loaded_model_outputs, outputs))
        del loaded_model
        del model
        gc.collect()

    @require_diffusers
    def test_load_from_hub_and_save_stable_diffusion_model(self):
        loaded_pipeline = OVStableDiffusionPipeline.from_pretrained(self.OV_SD_DIFFUSION_MODEL_ID, compile=False)
        self.assertIsInstance(loaded_pipeline.config, Dict)
        # Test that PERFORMANCE_HINT is set to LATENCY by default
        self.assertEqual(loaded_pipeline.ov_config.get("PERFORMANCE_HINT"), "LATENCY")
        loaded_pipeline.compile()
        self.assertEqual(loaded_pipeline.unet.request.get_property("PERFORMANCE_HINT"), "LATENCY")
        batch_size, height, width = 2, 16, 16
        inputs = {
            "prompt": ["sailing ship in storm by Leonardo da Vinci"] * batch_size,
            "height": height,
            "width": width,
            "num_inference_steps": 2,
            "output_type": "np",
        }

        np.random.seed(0)
        torch.manual_seed(0)
        pipeline_outputs = loaded_pipeline(**inputs).images
        self.assertEqual(pipeline_outputs.shape, (batch_size, height, width, 3))

        with TemporaryDirectory() as tmpdirname:
            loaded_pipeline.save_pretrained(tmpdirname)
            pipeline = OVStableDiffusionPipeline.from_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            self.assertIn(loaded_pipeline.config_name, folder_contents)
            for subfoler in {
                DIFFUSION_MODEL_UNET_SUBFOLDER,
                DIFFUSION_MODEL_TEXT_ENCODER_SUBFOLDER,
                DIFFUSION_MODEL_VAE_ENCODER_SUBFOLDER,
                DIFFUSION_MODEL_VAE_DECODER_SUBFOLDER,
            }:
                folder_contents = os.listdir(os.path.join(tmpdirname, subfoler))
                self.assertIn(OV_XML_FILE_NAME, folder_contents)
                self.assertIn(OV_XML_FILE_NAME.replace(".xml", ".bin"), folder_contents)

            compile_only_pipeline = OVStableDiffusionPipeline.from_pretrained(tmpdirname, compile_only=True)
            self.assertIsInstance(compile_only_pipeline.unet.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_pipeline.text_encoder.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_pipeline.vae_encoder.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_pipeline.vae_decoder.model, ov.CompiledModel)

            np.random.seed(0)
            torch.manual_seed(0)
            outputs = compile_only_pipeline(**inputs).images
            np.testing.assert_allclose(pipeline_outputs, outputs, atol=1e-4, rtol=1e-4)
            del compile_only_pipeline

        np.random.seed(0)
        torch.manual_seed(0)
        outputs = pipeline(**inputs).images
        np.testing.assert_allclose(pipeline_outputs, outputs, atol=1e-4, rtol=1e-4)
        del pipeline
        gc.collect()

    @require_diffusers
    @unittest.skipIf(
        is_transformers_version("<", "4.45"),
        "model tokenizer exported with tokenizers 0.20 is not compatible with old transformers",
    )
    def test_load_from_hub_and_save_flux_model(self):
        loaded_pipeline = OVDiffusionPipeline.from_pretrained(self.OV_FLUX_DIFFUSION_MODEL_ID, compile=False)
        self.assertIsInstance(loaded_pipeline, OVFluxPipeline)
        self.assertIsInstance(loaded_pipeline.config, Dict)
        # Test that PERFORMANCE_HINT is set to LATENCY by default
        self.assertEqual(loaded_pipeline.ov_config.get("PERFORMANCE_HINT"), "LATENCY")
        loaded_pipeline.compile()
        self.assertIsNone(loaded_pipeline.unet)
        self.assertEqual(loaded_pipeline.transformer.request.get_property("PERFORMANCE_HINT"), "LATENCY")
        batch_size, height, width = 2, 16, 16
        inputs = {
            "prompt": ["sailing ship in storm by Leonardo da Vinci"] * batch_size,
            "height": height,
            "width": width,
            "num_inference_steps": 2,
            "output_type": "np",
        }

        np.random.seed(0)
        torch.manual_seed(0)
        pipeline_outputs = loaded_pipeline(**inputs).images
        self.assertEqual(pipeline_outputs.shape, (batch_size, height, width, 3))

        with TemporaryDirectory() as tmpdirname:
            loaded_pipeline.save_pretrained(tmpdirname)
            pipeline = OVDiffusionPipeline.from_pretrained(tmpdirname)
            self.assertIsInstance(loaded_pipeline, OVFluxPipeline)
            folder_contents = os.listdir(tmpdirname)
            self.assertIn(loaded_pipeline.config_name, folder_contents)
            for subfoler in {
                DIFFUSION_MODEL_TRANSFORMER_SUBFOLDER,
                DIFFUSION_MODEL_TEXT_ENCODER_SUBFOLDER,
                DIFFUSION_MODEL_TEXT_ENCODER_2_SUBFOLDER,
                DIFFUSION_MODEL_VAE_ENCODER_SUBFOLDER,
                DIFFUSION_MODEL_VAE_DECODER_SUBFOLDER,
            }:
                folder_contents = os.listdir(os.path.join(tmpdirname, subfoler))
                self.assertIn(OV_XML_FILE_NAME, folder_contents)
                self.assertIn(OV_XML_FILE_NAME.replace(".xml", ".bin"), folder_contents)

            compile_only_pipeline = OVDiffusionPipeline.from_pretrained(tmpdirname, compile_only=True)
            self.assertIsInstance(compile_only_pipeline, OVFluxPipeline)
            self.assertIsInstance(compile_only_pipeline.transformer.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_pipeline.text_encoder.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_pipeline.text_encoder_2.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_pipeline.vae_encoder.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_pipeline.vae_decoder.model, ov.CompiledModel)

            np.random.seed(0)
            torch.manual_seed(0)
            outputs = compile_only_pipeline(**inputs).images
            np.testing.assert_allclose(pipeline_outputs, outputs, atol=1e-4, rtol=1e-4)
            del compile_only_pipeline

        np.random.seed(0)
        torch.manual_seed(0)
        outputs = pipeline(**inputs).images
        np.testing.assert_allclose(pipeline_outputs, outputs, atol=1e-4, rtol=1e-4)
        del pipeline
        gc.collect()

    def test_load_from_hub_and_save_sam_model(self):
        loaded_model = OVModelForFeatureExtraction.from_pretrained(self.OV_SAM_MODEL_ID)
        self.assertIsInstance(loaded_model, OVSamModel)
        self.assertIsInstance(loaded_model.config, PretrainedConfig)
        # Test that PERFORMANCE_HINT is not set by default
        self.assertIsNone(loaded_model.ov_config.get("PERFORMANCE_HINT"))

        # Test specifying ov_config with throughput hint and manual cache dir
        manual_openvino_cache_dir = loaded_model._model_save_dir / "manual_model_cache"
        ov_config = {"CACHE_DIR": str(manual_openvino_cache_dir), "PERFORMANCE_HINT": "THROUGHPUT"}
        loaded_model = OVModelForFeatureExtraction.from_pretrained(self.OV_SAM_MODEL_ID, ov_config=ov_config)

        self.assertTrue(manual_openvino_cache_dir.is_dir())
        num_blobs = len(list(manual_openvino_cache_dir.glob("*.blob")))
        self.assertGreaterEqual(num_blobs, 2)
        self.assertEqual(loaded_model.vision_encoder.request.get_property("PERFORMANCE_HINT"), "THROUGHPUT")
        self.assertEqual(
            loaded_model.prompt_encoder_mask_decoder.request.get_property("PERFORMANCE_HINT"), "THROUGHPUT"
        )
        processor = get_preprocessor(self.OV_SAM_MODEL_ID)
        img_url = "https://huggingface.co/ybelkada/segment-anything/resolve/main/assets/car.png"
        input_points = [[[450, 600]]]
        raw_image = Image.open(requests.get(img_url, stream=True).raw).convert("RGB")
        inputs = processor(raw_image, input_points=input_points, return_tensors="pt")

        loaded_model_outputs = loaded_model(**inputs)

        # Test compile only

        compile_only_model = OVModelForFeatureExtraction.from_pretrained(
            self.OV_SAM_MODEL_ID, ov_config=ov_config, compile_only=True
        )
        self.assertTrue(manual_openvino_cache_dir.is_dir())
        current_num_blobs = len(list(manual_openvino_cache_dir.glob("*.blob")))
        # compile_only get model from cache
        self.assertGreaterEqual(current_num_blobs, num_blobs)
        self.assertIsInstance(compile_only_model.vision_encoder_model, ov.CompiledModel)
        self.assertIsInstance(compile_only_model.vision_encoder.request, ov.CompiledModel)
        self.assertIsInstance(compile_only_model.prompt_encoder_mask_decoder_model, ov.CompiledModel)
        self.assertIsInstance(compile_only_model.prompt_encoder_mask_decoder.request, ov.CompiledModel)
        outputs = compile_only_model(**inputs)
        self.assertTrue(torch.equal(loaded_model_outputs.iou_scores, outputs.iou_scores))
        self.assertTrue(torch.equal(loaded_model_outputs.pred_masks, outputs.pred_masks))
        del compile_only_model

        with TemporaryDirectory() as tmpdirname:
            loaded_model.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            for ir_file in [OV_VISION_ENCODER_MODEL_NAME, OV_PROMPT_ENCODER_MASK_DECODER_MODEL_NAME]:
                self.assertTrue(ir_file in folder_contents)
                self.assertTrue(ir_file.replace(".xml", ".bin") in folder_contents)
            model = OVModelForFeatureExtraction.from_pretrained(tmpdirname, ov_config={"NUM_STREAMS": 2})
            self.assertEqual(loaded_model.vision_encoder.request.get_property("PERFORMANCE_HINT"), "THROUGHPUT")
            self.assertEqual(
                loaded_model.prompt_encoder_mask_decoder.request.get_property("PERFORMANCE_HINT"), "THROUGHPUT"
            )

        outputs = model(**inputs)
        self.assertTrue(torch.equal(loaded_model_outputs.iou_scores, outputs.iou_scores))
        self.assertTrue(torch.equal(loaded_model_outputs.pred_masks, outputs.pred_masks))

        del loaded_model
        del model
        gc.collect()

    def test_load_from_hub_and_save_text_speech_model(self):
        loaded_model = OVModelForTextToSpeechSeq2Seq.from_pretrained(self.OV_TEXTSPEECH_MODEL_ID)
        self.assertIsInstance(loaded_model.config, PretrainedConfig)
        # Test that PERFORMANCE_HINT is set to LATENCY by default
        self.assertEqual(loaded_model.ov_config.get("PERFORMANCE_HINT"), "LATENCY")

        processor = AutoProcessor.from_pretrained(self.OV_TEXTSPEECH_MODEL_ID)
        text_data = "This text is converted to speech using OpenVINO backend"
        inputs = processor(text=text_data, return_tensors="pt")
        speaker_embeddings = np.random.randn(1, 512).astype(np.float32)
        loaded_model_outputs = loaded_model.generate(
            input_ids=inputs["input_ids"], speaker_embeddings=speaker_embeddings
        )

        with TemporaryDirectory() as tmpdirname:
            loaded_model.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            self.assertTrue(loaded_model.OV_ENCODER_MODEL_NAME in folder_contents)
            self.assertTrue(loaded_model.OV_DECODER_MODEL_NAME in folder_contents)
            self.assertTrue(loaded_model.OV_POSTNET_MODEL_NAME in folder_contents)
            self.assertTrue(loaded_model.OV_VOCODER_MODEL_NAME in folder_contents)
            model = OVModelForTextToSpeechSeq2Seq.from_pretrained(tmpdirname, device="cpu")
            # compile only
            compile_only_model = OVModelForTextToSpeechSeq2Seq.from_pretrained(tmpdirname, compile_only=True)
            self.assertIsInstance(compile_only_model.encoder.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_model.decoder.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_model.postnet.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_model.vocoder.model, ov.CompiledModel)

            outputs = compile_only_model.generate(input_ids=inputs["input_ids"], speaker_embeddings=speaker_embeddings)
            self.assertTrue(torch.equal(loaded_model_outputs, outputs))
            del compile_only_model

        outputs = model.generate(input_ids=inputs["input_ids"], speaker_embeddings=speaker_embeddings)
        self.assertTrue(torch.equal(loaded_model_outputs, outputs))
        del loaded_model
        del model
        gc.collect()

    @pytest.mark.run_slow
    @slow
    def test_load_model_from_hub_private_with_token(self):
        model_id = "optimum-internal-testing/tiny-random-phi-private"
        token = os.environ.get("HF_HUB_READ_TOKEN", None)
        if not token:
            self.skipTest("Test requires a token `HF_HUB_READ_TOKEN` in the environment variable")

        model = OVModelForCausalLM.from_pretrained(model_id, token=token, revision="openvino")
        self.assertIsInstance(model.config, PretrainedConfig)
        self.assertTrue(model.stateful)

    @parameterized.expand(("", "openvino"))
    def test_loading_with_config_in_root(self, subfolder):
        # config.json file in the root directory and not in the subfolder
        model_id = "sentence-transformers-testing/stsb-bert-tiny-openvino"
        export = subfolder == ""
        # hub model
        OVModelForFeatureExtraction.from_pretrained(model_id, subfolder=subfolder, export=export)
        with TemporaryDirectory() as tmpdirname:
            local_dir = Path(tmpdirname) / "model"
            snapshot_download(repo_id=model_id, local_dir=local_dir, user_agent=http_user_agent())
            OVModelForFeatureExtraction.from_pretrained(local_dir, subfolder=subfolder, export=export)

    def test_infer_export_when_loading(self):
        model_id = MODEL_NAMES["phi"]
        model = AutoModelForCausalLM.from_pretrained(model_id)
        with TemporaryDirectory() as tmpdirname:
            model.save_pretrained(Path(tmpdirname) / "original")
            # Load original model and convert
            model = OVModelForCausalLM.from_pretrained(Path(tmpdirname) / "original")
            model.save_pretrained(Path(tmpdirname) / "openvino")
            # Load openvino model
            model = OVModelForCausalLM.from_pretrained(Path(tmpdirname) / "openvino")
        del model
        gc.collect()

    def test_find_files_matching_pattern(self):
        model_id = "echarlaix/tiny-random-PhiForCausalLM"
        pattern = r"(.*)?openvino(.*)?\_model(.*)?.xml$"
        # hub model
        for revision in ("main", "ov", "itrex"):
            ov_files = _find_files_matching_pattern(
                model_id, pattern=pattern, revision=revision, subfolder="openvino" if revision == "itrex" else ""
            )
            self.assertTrue(len(ov_files) == 0 if revision == "main" else len(ov_files) > 0)

        with TemporaryDirectory() as tmpdirname:
            for revision in ("main", "ov", "itrex"):
                local_dir = Path(tmpdirname) / revision
                snapshot_download(
                    repo_id=model_id, local_dir=local_dir, revision=revision, user_agent=http_user_agent()
                )
                ov_files = _find_files_matching_pattern(
                    local_dir, pattern=pattern, revision=revision, subfolder="openvino" if revision == "itrex" else ""
                )
                self.assertTrue(len(ov_files) == 0 if revision == "main" else len(ov_files) > 0)

    @parameterized.expand(("stable-diffusion", "stable-diffusion-openvino"))
    def test_find_files_matching_pattern_sd(self, model_arch):
        pattern = r"(.*)?openvino(.*)?\_model(.*)?.xml$"
        model_id = MODEL_NAMES[model_arch]
        # hub model
        ov_files = _find_files_matching_pattern(model_id, pattern=pattern)
        self.assertTrue(len(ov_files) > 0 if "openvino" in model_id else len(ov_files) == 0)

        with TemporaryDirectory() as tmpdirname:
            local_dir = Path(tmpdirname) / "model"
            snapshot_download(repo_id=model_id, local_dir=local_dir, user_agent=http_user_agent())
            ov_files = _find_files_matching_pattern(local_dir, pattern=pattern)
            self.assertTrue(len(ov_files) > 0 if "openvino" in model_id else len(ov_files) == 0)

    @parameterized.expand(("", "openvino"))
    def test_find_files_matching_pattern_with_config_in_root(self, subfolder):
        # Notably, the model has a config.json file in the root directory and not in the subfolder
        model_id = "sentence-transformers-testing/stsb-bert-tiny-openvino"
        pattern = r"(.*)?openvino(.*)?\_model(.*)?.xml$"
        # hub model
        ov_files = _find_files_matching_pattern(model_id, pattern=pattern, subfolder=subfolder)
        self.assertTrue(len(ov_files) == 1 if subfolder == "openvino" else len(ov_files) == 0)

        with tempfile.TemporaryDirectory() as tmpdirname:
            local_dir = Path(tmpdirname) / "model"
            snapshot_download(repo_id=model_id, local_dir=local_dir, user_agent=http_user_agent())
            ov_files = _find_files_matching_pattern(local_dir, pattern=pattern, subfolder=subfolder)
            self.assertTrue(len(ov_files) == 1 if subfolder == "openvino" else len(ov_files) == 0)

    def test_find_files_matching_pattern_with_quantized_ov_model(self):
        # This model only has "openvino/openvino_model_qint8_quantized.xml" and "openvino/openvino_model_qint8_quantized.bin"
        # We want to ensure that this model is found, so the `export` isn't forced to True
        model_id = "sentence-transformers-testing/stsb-bert-tiny-openvino-quantized-only"
        subfolder = "openvino"
        pattern = r"(.*)?openvino(.*)?\_model(.*)?.xml$"
        # hub model
        ov_files = _find_files_matching_pattern(model_id, pattern=pattern, subfolder=subfolder)
        self.assertTrue(len(ov_files) == 1)

        with tempfile.TemporaryDirectory() as tmpdirname:
            local_dir = Path(tmpdirname) / "model"
            snapshot_download(repo_id=model_id, local_dir=local_dir, user_agent=http_user_agent())
            ov_files = _find_files_matching_pattern(local_dir, pattern=pattern, subfolder=subfolder)
            self.assertTrue(len(ov_files) == 1)

    def test_load_from_hub_onnx_model_and_save(self):
        model_id = "katuni4ka/tiny-random-LlamaForCausalLM-onnx"
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokens = tokenizer("This is a sample input", return_tensors="pt")
        loaded_model = OVModelForCausalLM.from_pretrained(model_id, from_onnx=True)
        self.assertIsInstance(loaded_model.config, PretrainedConfig)
        # Test that PERFORMANCE_HINT is set to LATENCY by default
        self.assertEqual(loaded_model.ov_config.get("PERFORMANCE_HINT"), "LATENCY")
        self.assertEqual(loaded_model.request.get_compiled_model().get_property("PERFORMANCE_HINT"), "LATENCY")
        loaded_model_outputs = loaded_model(**tokens)

        with TemporaryDirectory() as tmpdirname:
            loaded_model.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            self.assertTrue(OV_XML_FILE_NAME in folder_contents)
            self.assertTrue(OV_XML_FILE_NAME.replace(".xml", ".bin") in folder_contents)
            model = OVModelForCausalLM.from_pretrained(tmpdirname)
            self.assertEqual(model.use_cache, loaded_model.use_cache)

            compile_only_model = OVModelForCausalLM.from_pretrained(tmpdirname, compile_only=True)
            self.assertIsInstance(compile_only_model.model, ov.CompiledModel)
            self.assertIsInstance(compile_only_model.request, ov.InferRequest)
            outputs = compile_only_model(**tokens)
            self.assertTrue(torch.equal(loaded_model_outputs.logits, outputs.logits))
            del compile_only_model

        outputs = model(**tokens)
        self.assertTrue(torch.equal(loaded_model_outputs.logits, outputs.logits))
        del loaded_model
        del model
        gc.collect()


class PipelineTest(unittest.TestCase):
    def test_load_model_from_hub(self):
        model_id = "echarlaix/tiny-random-PhiForCausalLM"

        # verify could load both pytorch and openvino model (export argument should automatically infered)
        ov_exported_pipe = optimum_pipeline("text-generation", model_id, revision="pt", accelerator="openvino")
        ov_pipe = optimum_pipeline("text-generation", model_id, revision="ov", accelerator="openvino")
        self.assertIsInstance(ov_exported_pipe.model, OVBaseModel)
        self.assertIsInstance(ov_pipe.model, OVBaseModel)

        with TemporaryDirectory() as tmpdirname:
            ov_exported_pipe.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            self.assertTrue(OV_XML_FILE_NAME in folder_contents)
            self.assertTrue(OV_XML_FILE_NAME.replace(".xml", ".bin") in folder_contents)
            ov_exported_pipe = optimum_pipeline("text-generation", tmpdirname, accelerator="openvino")
            self.assertIsInstance(ov_exported_pipe.model, OVBaseModel)

        del ov_exported_pipe
        del ov_pipe
        gc.collect()

    def test_seq2seq_load_from_hub(self):
        model_id = "echarlaix/tiny-random-t5"
        # verify could load both pytorch and openvino model (export argument should automatically infered)
        ov_exported_pipe = optimum_pipeline("text2text-generation", model_id, accelerator="openvino")
        ov_pipe = optimum_pipeline("text2text-generation", model_id, revision="ov", accelerator="openvino")
        self.assertIsInstance(ov_exported_pipe.model, OVBaseModel)
        self.assertIsInstance(ov_pipe.model, OVBaseModel)

        with TemporaryDirectory() as tmpdirname:
            ov_exported_pipe.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            if not ov_exported_pipe.model.decoder.stateful:
                self.assertTrue(OV_DECODER_WITH_PAST_NAME in folder_contents)
                self.assertTrue(OV_DECODER_WITH_PAST_NAME.replace(".xml", ".bin") in folder_contents)
            ov_exported_pipe = optimum_pipeline("text2text-generation", tmpdirname, accelerator="openvino")
            self.assertIsInstance(ov_exported_pipe.model, OVBaseModel)

        del ov_exported_pipe
        del ov_pipe
        gc.collect()


class OVModelForSequenceClassificationIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "albert",
        "bert",
        "convbert",
        "distilbert",
        "electra",
        "flaubert",
        "ibert",
        "roberta",
        "roformer",
        "squeezebert",
        "xlm",
    )

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForSequenceClassification.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        transformers_model = AutoModelForSequenceClassification.from_pretrained(model_id)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        inputs = "This is a sample input"
        tokens = tokenizer(inputs, return_tensors="pt")
        with torch.no_grad():
            transformers_outputs = transformers_model(**tokens)
        for input_type in ["pt", "np"]:
            tokens = tokenizer(inputs, return_tensors=input_type)
            ov_outputs = ov_model(**tokens)
            self.assertIn("logits", ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])
            # Compare tensor outputs
            self.assertTrue(
                torch.allclose(
                    torch.Tensor(ov_outputs.logits),
                    transformers_outputs.logits,
                    atol=1e-4 if model_arch not in ["flaubert", "squeezebert"] else 0.08,
                )
            )
        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForSequenceClassification.from_pretrained(model_id, compile=False)
        model.eval()
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        pipe = pipeline("text-classification", model=model, tokenizer=tokenizer)
        inputs = "This restaurant is awesome"
        outputs = pipe(inputs)
        self.assertTrue(model.is_dynamic)
        self.assertEqual(pipe.device, model.device)
        self.assertGreaterEqual(outputs[0]["score"], 0.0)
        self.assertIsInstance(outputs[0]["label"], str)

        ov_pipe = optimum_pipeline("text-classification", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs)
        atol = 1e-4 if model_arch not in ["flaubert", "squeezebert"] else 0.08
        self.assertTrue(abs(ov_outputs[-1]["score"] - outputs[-1]["score"]) < atol)
        del ov_pipe

        if model_arch == "bert":
            # Test FP16 conversion
            model.half()
            model.to("cpu")
            model.compile()
            outputs = pipe(inputs)
            self.assertGreaterEqual(outputs[0]["score"], 0.0)
            self.assertIsInstance(outputs[0]["label"], str)
            # Test static shapes
            model.reshape(1, 25)
            model.compile()
            outputs = pipe(inputs)
            self.assertTrue(not model.is_dynamic)
            self.assertGreaterEqual(outputs[0]["score"], 0.0)
            self.assertIsInstance(outputs[0]["label"], str)
            # Test that model caching was not automatically enabled for exported model
            openvino_cache_dir = model.model_save_dir / "model_cache"
            self.assertFalse(openvino_cache_dir.is_dir())

        del model
        del pipe
        gc.collect()


class OVModelForQuestionAnsweringIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "bert",
        "distilbert",
        "roberta",
    )

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForQuestionAnswering.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        transformers_model = AutoModelForQuestionAnswering.from_pretrained(model_id)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        inputs = "This is a sample input"
        tokens = tokenizer(inputs, return_tensors="pt")
        with torch.no_grad():
            transformers_outputs = transformers_model(**tokens)
        for input_type in ["pt", "np"]:
            tokens = tokenizer(inputs, return_tensors=input_type)
            ov_outputs = ov_model(**tokens)
            self.assertIn("start_logits", ov_outputs)
            self.assertIn("end_logits", ov_outputs)
            self.assertIsInstance(ov_outputs.start_logits, TENSOR_ALIAS_TO_TYPE[input_type])
            self.assertIsInstance(ov_outputs.end_logits, TENSOR_ALIAS_TO_TYPE[input_type])
            # Compare tensor outputs
            self.assertTrue(
                torch.allclose(torch.Tensor(ov_outputs.start_logits), transformers_outputs.start_logits, atol=1e-4)
            )
            self.assertTrue(
                torch.allclose(torch.Tensor(ov_outputs.end_logits), transformers_outputs.end_logits, atol=1e-4)
            )
        del ov_model
        del transformers_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForQuestionAnswering.from_pretrained(model_id)
        model.eval()
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        pipe = pipeline("question-answering", model=model, tokenizer=tokenizer)
        question = "What's my name?"
        context = "My Name is Arthur and I live in Lyon."
        outputs = pipe(question, context)
        self.assertEqual(pipe.device, model.device)
        self.assertGreaterEqual(outputs["score"], 0.0)
        self.assertIsInstance(outputs["answer"], str)
        ov_pipe = optimum_pipeline("question-answering", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(question, context)
        self.assertEqual(outputs["score"], ov_outputs["score"])
        del model
        del ov_pipe
        gc.collect()

    @pytest.mark.run_slow
    @slow
    def test_metric(self):
        model_id = "distilbert-base-cased-distilled-squad"
        set_seed(SEED)
        ov_model = OVModelForQuestionAnswering.from_pretrained(model_id, export=True)
        transformers_model = AutoModelForQuestionAnswering.from_pretrained(model_id)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        data = load_dataset("squad", split="validation").select(range(50))
        task_evaluator = evaluator("question-answering")
        transformers_pipe = pipeline("question-answering", model=transformers_model, tokenizer=tokenizer)
        ov_pipe = pipeline("question-answering", model=ov_model, tokenizer=tokenizer)
        transformers_metric = task_evaluator.compute(model_or_pipeline=transformers_pipe, data=data, metric="squad")
        ov_metric = task_evaluator.compute(model_or_pipeline=ov_pipe, data=data, metric="squad")
        self.assertEqual(ov_metric["exact_match"], transformers_metric["exact_match"])
        self.assertEqual(ov_metric["f1"], transformers_metric["f1"])
        del transformers_pipe
        del transformers_model
        del ov_pipe
        del ov_model
        gc.collect()


class OVModelForTokenClassificationIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "bert",
        "distilbert",
        "roberta",
    )

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForTokenClassification.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        transformers_model = AutoModelForTokenClassification.from_pretrained(model_id)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        inputs = "This is a sample input"
        tokens = tokenizer(inputs, return_tensors="pt")
        with torch.no_grad():
            transformers_outputs = transformers_model(**tokens)
        for input_type in ["pt", "np"]:
            tokens = tokenizer(inputs, return_tensors=input_type)
            ov_outputs = ov_model(**tokens)
            self.assertIn("logits", ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])
            # Compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-4))
        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForTokenClassification.from_pretrained(model_id)
        model.eval()
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        pipe = pipeline("token-classification", model=model, tokenizer=tokenizer)
        inputs = "My Name is Arthur and I live in"
        outputs = pipe(inputs)
        self.assertEqual(pipe.device, model.device)
        self.assertTrue(all(item["score"] > 0.0 for item in outputs))
        ov_pipe = optimum_pipeline("token-classification", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs)
        self.assertEqual(outputs[-1]["score"], ov_outputs[-1]["score"])
        del ov_pipe
        del model
        del pipe
        gc.collect()

    def test_default_token_type_ids(self):
        model_id = MODEL_NAMES["bert"]
        model = OVModelForTokenClassification.from_pretrained(model_id, export=True)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokens = tokenizer("this is a simple input", return_tensors="np")
        self.assertTrue("token_type_ids" in model.input_names)
        token_type_ids = tokens.pop("token_type_ids")
        outs = model(token_type_ids=token_type_ids, **tokens)
        outs_without_token_type_ids = model(**tokens)
        self.assertTrue(np.allclose(outs.logits, outs_without_token_type_ids.logits))

        tokens["attention_mask"] = None
        with self.assertRaises(Exception) as context:
            _ = model(**tokens)

        self.assertIn("Got unexpected inputs: ", str(context.exception))
        del model
        gc.collect()


class OVModelForFeatureExtractionIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "bert",
        "distilbert",
        "roberta",
        "sentence-transformers-bert",
    )

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForFeatureExtraction.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        transformers_model = AutoModel.from_pretrained(model_id)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        inputs = "This is a sample input"
        tokens = tokenizer(inputs, return_tensors="pt")
        with torch.no_grad():
            transformers_outputs = transformers_model(**tokens)
        for input_type in ["pt", "np"]:
            tokens = tokenizer(inputs, return_tensors=input_type)
            ov_outputs = ov_model(**tokens)
            self.assertIn("last_hidden_state", ov_outputs)
            self.assertIsInstance(ov_outputs.last_hidden_state, TENSOR_ALIAS_TO_TYPE[input_type])
            # Compare tensor outputs
            self.assertTrue(
                torch.allclose(
                    torch.Tensor(ov_outputs.last_hidden_state), transformers_outputs.last_hidden_state, atol=1e-4
                )
            )
        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForFeatureExtraction.from_pretrained(model_id)
        model.eval()
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        pipe = pipeline("feature-extraction", model=model, tokenizer=tokenizer)
        inputs = "My Name is Arthur and I live in"
        outputs = pipe(inputs)
        self.assertEqual(pipe.device, model.device)
        self.assertTrue(all(all(isinstance(item, float) for item in row) for row in outputs[0]))
        ov_pipe = optimum_pipeline("feature-extraction", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs)
        self.assertEqual(outputs[-1][-1][-1], ov_outputs[-1][-1][-1])
        del ov_pipe
        del pipe
        del model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_sentence_transformers_pipeline(self, model_arch):
        """
        Check if we call OVModelForFeatureExtraction passing saved ir-model with outputs
        from Sentence Transformers then an appropriate exception raises.
        """
        model_id = MODEL_NAMES[model_arch]
        with TemporaryDirectory() as tmp_dir:
            save_dir = str(tmp_dir)
            OVSentenceTransformer.from_pretrained(model_id, export=True).save_pretrained(save_dir)
            with self.assertRaises(Exception) as context:
                OVModelForFeatureExtraction.from_pretrained(save_dir)
            self.assertIn("Please use `OVSentenceTransformer`", str(context.exception))


class OVModelForCausalLMIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "bart",
        "baichuan2",
        "baichuan2-13b",
        "gpt_bigcode",
        "blenderbot",
        "blenderbot-small",
        "bloom",
        "chatglm",
        "codegen",
        "codegen2",
        "gpt2",
        "gptj",
        "gpt_neo",
        "gpt_neox",
        "llama",
        "marian",
        "minicpm",
        "mistral",
        "mixtral",
        "mpt",
        "opt",
        "pegasus",
        "qwen",
        "phi",
        "internlm2",
        "orion",
        "falcon",
        "falcon-40b",
        "persimmon",
        "biogpt",
        "gpt_neox_japanese",
        "xglm",
        "aquila",
        "aquila2",
        "xverse",
        "internlm",
        "jais",
        "chatglm4",
        "decilm",
    )

    SUPPORTED_SSM_ARCHITECTURES = ("mamba", "falcon-mamba")
    if is_transformers_version(">=", "4.43"):
        SUPPORTED_ARCHITECTURES += SUPPORTED_SSM_ARCHITECTURES

    if is_transformers_version(">=", "4.40.0"):
        SUPPORTED_ARCHITECTURES += (
            "gemma",
            "olmo",
            "stablelm",
            "starcoder2",
            "dbrx",
            "cohere",
            "qwen2",
            "qwen2_moe",
            "arctic",
        )

    if is_transformers_version(">=", "4.41.0"):
        SUPPORTED_ARCHITECTURES += ("phi3",)

    if is_transformers_version(">=", "4.43.0"):
        SUPPORTED_ARCHITECTURES += ("gemma2", "exaone")

    if is_transformers_version(">=", "4.44.0"):
        SUPPORTED_ARCHITECTURES += ("granite", "granite-moe")

    if is_transformers_version(">=", "4.46.0"):
        SUPPORTED_ARCHITECTURES += ("glm", "mistral-nemo", "minicpm3", "phi3-moe")
        # openvino 2025.0 required for disabling check_trace
        if is_openvino_version(">=", "2025.0"):
            SUPPORTED_ARCHITECTURES += ("deepseek",)

        # gptq and awq install disabled for windows test environment
        if platform.system() != "Windows":
            SUPPORTED_ARCHITECTURES += ("opt_gptq",)

        # autoawq install disabled for windows test environment
        if is_openvino_version(">=", "2024.6.0") and platform.system() != "Windows":
            SUPPORTED_ARCHITECTURES += ("mixtral_awq",)

    if is_transformers_version(">", "4.49"):
        SUPPORTED_ARCHITECTURES += ("gemma3_text",)

    if is_transformers_version(">=", "4.51.0"):
        SUPPORTED_ARCHITECTURES += ("qwen3", "qwen3_moe")

    if is_transformers_version(">=", "4.51.3"):
        SUPPORTED_ARCHITECTURES += ("glm4",)

    GENERATION_LENGTH = 100
    REMOTE_CODE_MODELS = (
        "chatglm",
        "minicpm",
        "baichuan2",
        "baichuan2-13b",
        "jais",
        "qwen",
        "internlm2",
        "orion",
        "aquila",
        "aquila2",
        "xverse",
        "internlm",
        "codegen2",
        "arctic",
        "chatglm4",
        "exaone",
        "decilm",
        "minicpm3",
        "deepseek",
    )

    EXPECTED_NUM_SDPA = {
        "bart": 2,
        "baichuan2": 2,
        "baichuan2-13b": 2,
        "gpt_bigcode": 5,
        "blenderbot": 2,
        "blenderbot-small": 2,
        "bloom": 5,
        "chatglm": 2,
        "codegen": 5,
        "codegen2": 2,
        "gpt2": 5,
        "gptj": 5,
        "gpt_neo": 4,
        "gpt_neox": 5,
        "llama": 2,
        "marian": 2,
        "minicpm": 4,
        "mistral": 2 if is_transformers_version(">=", "4.40.0") else 0,
        "mixtral": 2 if is_transformers_version(">=", "4.40.0") else 0,
        "mpt": 5,
        "opt": 5,
        "pegasus": 2,
        "qwen": 2,
        "phi": 2 if is_transformers_version(">=", "4.40.0") else 0,
        "internlm2": 4,
        "falcon": 2,
        "falcon-40b": 2,
        "persimmon": 2,
        "biogpt": 5,
        "aquila": 2,
        "aquila2": 2,
        "xverse": 2,
        "internlm": 2,
        "jais": 2,
        "chatglm4": 6,
        "decilm": 4,
        "gemma": 1,
        "olmo": 2,
        "stablelm": 2,
        "starcoder2": 2,
        "dbrx": 2,
        "cohere": 2,
        "qwen2": 2,
        "qwen2_moe": 4,
        "arctic": 4,
        "phi3": 2,
        "gemma2": 4,
        "exaone": 8,
        "granite": 6,
        "granite-moe": 6,
        "glm": 28,
        "mistral-nemo": 8,
        "minicpm3": 6,
        "phi3-moe": 2,
        "deepseek": 2,
        "opt_gptq": 12,
        "mixtral_awq": 2,
        "gemma3_text": 2,
        "glm4": 2,
        "qwen3": 2,
        "qwen3_moe": 2,
        "mamba": 0,
        "falcon-mamba": 0,
    }

    # TODO: remove gptq/awq from here
    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]

        not_stateful = []
        if is_openvino_version("<", "2024.0"):
            not_stateful.append("mixtral")

        if is_openvino_version("<", "2024.1"):
            not_stateful.extend(["llama", "gemma", "gpt_bigcode"])

        set_seed(SEED)

        model_kwargs = {}
        if model_arch in self.REMOTE_CODE_MODELS:
            model_kwargs = {"trust_remote_code": True}

        # starting from transformers 4.45.0 gemma2 uses eager attention by default, while ov - sdpa
        if model_arch == "gemma2" and is_transformers_version(">=", "4.45.0"):
            model_kwargs["attn_implementation"] = "sdpa"

        ov_model = OVModelForCausalLM.from_pretrained(model_id, export=True, ov_config=F32_CONFIG, **model_kwargs)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        self.assertTrue(ov_model.use_cache)
        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS)
        tokens = tokenizer("This is a sample output", return_tensors="pt")

        ov_outputs = ov_model(**tokens)
        self.assertTrue("logits" in ov_outputs)
        self.assertIsInstance(ov_outputs.logits, torch.Tensor)
        if model_arch in self.SUPPORTED_SSM_ARCHITECTURES:
            from optimum.intel.openvino.modeling_decoder import OVMambaCache

            self.assertTrue("cache_params" in ov_outputs)
            self.assertIsInstance(ov_outputs.cache_params, OVMambaCache)
            is_stateful = ov_model.config.model_type not in not_stateful
            self.assertEqual(ov_model.stateful, is_stateful)
            if is_stateful:
                self.assertIsInstance(ov_outputs.cache_params.conv_states, list)
                self.assertIsInstance(ov_outputs.cache_params.ssm_states, list)
                self.assertTrue(
                    len(ov_outputs.cache_params.conv_states) > 0 and len(ov_outputs.cache_params.ssm_states) > 0
                )
        else:
            self.assertTrue("past_key_values" in ov_outputs)
            self.assertIsInstance(ov_outputs.past_key_values, tuple)
            is_stateful = ov_model.config.model_type not in not_stateful
            self.assertEqual(ov_model.stateful, is_stateful)
            if is_stateful:
                self.assertTrue(len(ov_outputs.past_key_values) == 1 and len(ov_outputs.past_key_values[0]) == 0)

        expected_num_sdpa = self.EXPECTED_NUM_SDPA.get(model_arch, 0)
        num_sdpa = get_num_sdpa(ov_model.model)
        self.assertEqual(
            expected_num_sdpa,
            num_sdpa,
            f"Expected number of SDPA {expected_num_sdpa}, while model contains {num_sdpa}",
        )

        if "awq" in model_arch or "gptq" in model_arch:
            model_kwargs["torch_dtype"] = torch.float32

        set_seed(SEED)
        with mock_torch_cuda_is_available("awq" in model_arch or "gptq" in model_arch):
            transformers_model = AutoModelForCausalLM.from_pretrained(model_id, **model_kwargs)
        if model_arch in ["qwen", "arctic", "chatglm4"]:
            transformers_model.to(torch.float32)

        with torch.no_grad():
            with patch_awq_for_inference("awq" in model_arch):
                transformers_outputs = transformers_model(**tokens)

        # Compare tensor outputs
        atol = 3e-3 if model_arch in ["minicpm", "qwen2-moe"] else 1e-4
        # quantized models have different logits value range
        if "awq" not in model_arch and "gptq" not in model_arch:
            self.assertTrue(torch.allclose(ov_outputs.logits, transformers_outputs.logits, equal_nan=True, atol=atol))

        # Qwen tokenizer does not support padding
        if model_arch in ["qwen"]:
            return

        if model_arch not in ["chatglm", "chatglm4", "persimmon"]:
            tokenizer.pad_token_id = tokenizer.eos_token_id

        if model_arch == "persimmon":
            tokenizer.pad_token_id = tokenizer.bos_token_id
        # Compare batched generation
        tokenizer.padding_side = "left"
        tokens = tokenizer(["Today is a nice day and I am longer", "This is me"], return_tensors="pt", padding=True)
        ov_model.generation_config.eos_token_id = None
        transformers_model.generation_config.eos_token_id = None
        ov_model.config.eos_token_id = None
        transformers_model.config.eos_token_id = None
        gen_config = GenerationConfig(
            max_new_tokens=30,
            min_new_tokens=30,
            num_beams=1 if model_arch == "chatglm4" else 2,
            do_sample=False,
        )

        ov_outputs = ov_model.generate(**tokens, generation_config=gen_config)

        # TODO: add back once https://huggingface.co/katuni4ka/tiny-random-minicpm3/discussions/1 merged (for all models) as current mdoeling incompatible with transformers >= v4.49
        if model_arch in {"deepseek"} and is_transformers_version(">=", "4.49"):
            self.skipTest("Incompatible modeling code")

        additional_inputs = {}
        # gemma2 does not support dynamic cache, it is unfair to compare dynamic cache result vs hybrid cache,
        # align cache representation in torch model
        if model_arch in ["gemma2", "gemma3_text"]:
            patch_update_causal_mask(transformers_model, "4.43.0")
            transformers_model._supports_cache_class = True
            transformers_model.generation_config.cache_implementation = None
            from transformers.cache_utils import DynamicCache

            additional_inputs = {"past_key_values": DynamicCache()}
        with patch_awq_for_inference("awq" in model_arch):
            transformers_outputs = transformers_model.generate(
                **tokens, generation_config=gen_config, **additional_inputs
            )

        self.assertTrue(
            torch.allclose(ov_outputs, transformers_outputs),
            f"OV output {ov_outputs}\nTransformers output  {transformers_outputs}",
        )

        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_kwargs = {}
        model_id = MODEL_NAMES[model_arch]
        if model_arch in self.REMOTE_CODE_MODELS:
            model_kwargs = {
                "config": AutoConfig.from_pretrained(model_id, trust_remote_code=True),
                "trust_remote_code": True,
            }
        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS)

        if model_arch == "qwen":
            tokenizer._convert_tokens_to_ids = lambda x: 0

        additional_args = {}
        if is_transformers_version(">=", "4.51"):
            additional_args["use_model_defaults"] = False

        set_seed(SEED)
        model = OVModelForCausalLM.from_pretrained(model_id, use_cache=True, compile=False, **model_kwargs)
        model.eval()
        model.config.encoder_no_repeat_ngram_size = 0
        model.to("cpu")
        model.half()
        model.compile()
        pipe = pipeline("text-generation", model=model, tokenizer=tokenizer)
        inputs = "My name is Arthur and I live in"
        set_seed(SEED)
        outputs = pipe(inputs, min_new_tokens=5, max_new_tokens=5, **additional_args, do_sample=False)
        self.assertEqual(pipe.device, model.device)
        self.assertTrue(all(inputs in item["generated_text"] for item in outputs))
        ov_pipe = optimum_pipeline(
            "text-generation",
            model_id,
            accelerator="openvino",
            trust_remote_code=model_arch in self.REMOTE_CODE_MODELS,
            tokenizer=tokenizer if model_arch == "qwen" else None,
        )
        set_seed(SEED)
        ov_outputs = ov_pipe(inputs, min_new_tokens=5, max_new_tokens=5, **additional_args, do_sample=False)
        self.assertEqual(outputs[-1]["generated_text"], ov_outputs[-1]["generated_text"])
        del ov_pipe
        del pipe
        del model
        gc.collect()

    def test_model_and_decoder_same_device(self):
        model_id = MODEL_NAMES["gpt2"]
        model = OVModelForCausalLM.from_pretrained(model_id, export=True)
        model.to("TEST")
        self.assertEqual(model._device, "TEST")
        # Verify that request is being reset
        self.assertEqual(model.request, None)
        del model
        gc.collect()

    def test_compare_with_and_without_past_key_values(self):
        model_id = MODEL_NAMES["gpt2"]
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokens = tokenizer("This is a sample input", return_tensors="pt")

        model_with_pkv = OVModelForCausalLM.from_pretrained(model_id, export=True, use_cache=True, stateful=False)
        outputs_model_with_pkv = model_with_pkv.generate(
            **tokens, min_length=self.GENERATION_LENGTH, max_length=self.GENERATION_LENGTH, num_beams=1
        )
        del model_with_pkv

        model_without_pkv = OVModelForCausalLM.from_pretrained(model_id, export=True, use_cache=False)
        outputs_model_without_pkv = model_without_pkv.generate(
            **tokens, min_length=self.GENERATION_LENGTH, max_length=self.GENERATION_LENGTH, num_beams=1
        )
        del model_without_pkv

        self.assertTrue(torch.equal(outputs_model_with_pkv, outputs_model_without_pkv))
        self.assertEqual(outputs_model_with_pkv.shape[1], self.GENERATION_LENGTH)
        self.assertEqual(outputs_model_without_pkv.shape[1], self.GENERATION_LENGTH)

        model_stateful = OVModelForCausalLM.from_pretrained(model_id, export=True, use_cache=True, stateful=True)
        outputs_model_stateful = model_stateful.generate(
            **tokens, min_length=self.GENERATION_LENGTH, max_length=self.GENERATION_LENGTH, num_beams=1
        )
        self.assertTrue(torch.equal(outputs_model_without_pkv, outputs_model_stateful))

        logits = model_stateful(**tokens).logits
        copy_logits = copy.deepcopy(logits)
        tokens = tokenizer("Input sample", return_tensors="pt")
        model_stateful(**tokens).logits
        self.assertTrue(torch.equal(copy_logits, logits))
        del model_stateful
        gc.collect()

    def test_print_model_properties(self):
        # test setting OPENVINO_LOG_LEVEL to 3, which calls _print_compiled_model_properties
        openvino_log_level = os.environ.get("OPENVINO_LOG_LEVEL", None)
        os.environ["OPENVINO_LOG_LEVEL"] = "3"
        model = OVModelForSequenceClassification.from_pretrained(MODEL_NAMES["bert"], export=True)
        if openvino_log_level is not None:
            os.environ["OPENVINO_LOG_LEVEL"] = openvino_log_level
        # test calling function directly
        _print_compiled_model_properties(model.request)

    def test_auto_device_loading(self):
        OV_MODEL_ID = "echarlaix/distilbert-base-uncased-finetuned-sst-2-english-openvino"
        for device in ("AUTO", "AUTO:CPU"):
            model = OVModelForSequenceClassification.from_pretrained(OV_MODEL_ID, device=device)
            model.half()
            self.assertEqual(model._device, device)
            if device == "AUTO:CPU":
                model = OVModelForSequenceClassification.from_pretrained(OV_MODEL_ID, device=device)
                message = "Model should not be loaded from cache without explicitly setting CACHE_DIR"
                self.assertFalse(model.request.get_property("LOADED_FROM_CACHE"), message)
            del model
            gc.collect()

    def test_default_filling_attention_mask(self):
        model_id = MODEL_NAMES["gpt2"]
        model_with_cache = OVModelForCausalLM.from_pretrained(model_id, stateful=False, use_cache=True)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokenizer.pad_token = tokenizer.eos_token
        texts = ["this is a simple input"]
        tokens = tokenizer(texts, return_tensors="pt")
        self.assertTrue("attention_mask" in model_with_cache.input_names)
        outs = model_with_cache(**tokens)
        attention_mask = tokens.pop("attention_mask")
        outs_without_attn_mask = model_with_cache(**tokens)
        self.assertTrue(torch.allclose(outs.logits, outs_without_attn_mask.logits))
        input_ids = torch.argmax(outs.logits[:, -1:, :], dim=2)
        past_key_values = outs.past_key_values
        attention_mask = torch.ones((input_ids.shape[0], tokens.input_ids.shape[1] + 1), dtype=torch.long)
        outs_step2 = model_with_cache(
            input_ids=input_ids, attention_mask=attention_mask, past_key_values=past_key_values
        )
        outs_without_attn_mask_step2 = model_with_cache(input_ids=input_ids, past_key_values=past_key_values)
        self.assertTrue(torch.allclose(outs_step2.logits, outs_without_attn_mask_step2.logits))
        del model_with_cache
        gc.collect()

    def test_default_filling_attention_mask_and_position_ids(self):
        model_id = MODEL_NAMES["llama"]
        model_with_cache = OVModelForCausalLM.from_pretrained(model_id, stateful=False, use_cache=True)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokenizer.pad_token = tokenizer.eos_token
        texts = ["this is a simple input"]
        tokens = tokenizer(texts, return_tensors="pt")
        self.assertTrue("position_ids" in model_with_cache.input_names)
        outs = model_with_cache(**tokens)
        attention_mask = tokens.pop("attention_mask")
        outs_without_attn_mask = model_with_cache(**tokens)
        self.assertTrue(torch.allclose(outs.logits, outs_without_attn_mask.logits))
        input_ids = torch.argmax(outs.logits[:, -1:, :], dim=2)
        past_key_values = outs.past_key_values
        attention_mask = torch.ones((input_ids.shape[0], tokens.input_ids.shape[1] + 1), dtype=torch.long)
        outs_step2 = model_with_cache(
            input_ids=input_ids, attention_mask=attention_mask, past_key_values=past_key_values
        )
        outs_without_attn_mask_step2 = model_with_cache(input_ids=input_ids, past_key_values=past_key_values)
        self.assertTrue(torch.allclose(outs_step2.logits, outs_without_attn_mask_step2.logits))
        del model_with_cache
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_beam_search(self, model_arch):
        model_kwargs = {}
        model_id = MODEL_NAMES[model_arch]
        if model_arch in self.REMOTE_CODE_MODELS:
            model_kwargs = {
                "config": AutoConfig.from_pretrained(model_id, trust_remote_code=True),
                "trust_remote_code": True,
            }

        # starting from transformers 4.45.0 gemma2 uses eager attention by default, while ov - sdpa
        if model_arch == "gemma2" and is_transformers_version(">=", "4.45.0"):
            model_kwargs["attn_implementation"] = "sdpa"

        # Qwen tokenizer does not support padding, chatglm, glm4 testing models produce nan that incompatible with beam search
        if model_arch in ["qwen", "chatglm", "chatglm4"]:
            return

        # TODO: add back once https://huggingface.co/katuni4ka/tiny-random-minicpm3/discussions/1 merged (for all models) as current mdoeling incompatible with transformers >= v4.49
        if model_arch in {"deepseek"} and is_transformers_version(">=", "4.49"):
            self.skipTest("Incompatible modeling code")

        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS)
        if model_arch == "persimmon":
            tokenizer.pad_token_id = tokenizer.bos_token_id
            tokenizer.eos_token_id = tokenizer.bos_token_id

        beam_search_gen_config = GenerationConfig(
            max_new_tokens=10,
            min_new_tokens=10,
            num_beams=4,
            do_sample=False,
            eos_token_id=None,
        )

        beam_sample_gen_config = GenerationConfig(
            max_new_tokens=10,
            min_new_tokens=10,
            num_beams=4,
            do_sample=True,
            eos_token_id=None,
        )

        if model_arch in ["minicpm", "internlm2"]:
            beam_sample_gen_config.top_k = 1

        group_beam_search_gen_config = GenerationConfig(
            max_new_tokens=10,
            min_new_tokens=10,
            num_beams=4,
            do_sample=False,
            eos_token_id=None,
            num_beam_groups=2,
            diversity_penalty=0.0000001,
        )
        force_word = "cat"
        force_words_ids = [tokenizer([force_word], add_special_tokens=False).input_ids]
        constrained_beam_search_gen_config = GenerationConfig(
            max_new_tokens=10,
            min_new_tokens=10,
            num_beams=4,
            do_sample=False,
            eos_token_id=None,
            force_words_ids=force_words_ids,
        )

        gen_configs = [
            beam_search_gen_config,
            beam_sample_gen_config,
            group_beam_search_gen_config,
            constrained_beam_search_gen_config,
        ]
        set_seed(SEED)
        ov_model_stateful = OVModelForCausalLM.from_pretrained(
            model_id, export=True, use_cache=True, stateful=True, **model_kwargs
        )
        set_seed(SEED)
        ov_model_stateless = OVModelForCausalLM.from_pretrained(
            model_id, export=True, use_cache=True, stateful=False, **model_kwargs
        )
        if "awq" in model_arch or "gptq" in model_arch:
            # infer in FP32
            model_kwargs["torch_dtype"] = torch.float32

        set_seed(SEED)
        with mock_torch_cuda_is_available("awq" in model_arch or "gptq" in model_arch):
            transformers_model = AutoModelForCausalLM.from_pretrained(model_id, **model_kwargs)
        if model_arch == "arctic":
            transformers_model.to(torch.float32)
        additional_inputs = {}
        # gemma2 does not support dynamic cache, it is unfair to compare dynamic cache result vs hybrid cache, align cache representation in torch model
        if model_arch in ["gemma2", "gemma3_text"]:
            patch_update_causal_mask(transformers_model, "4.43.0")
            transformers_model._supports_cache_class = True
            transformers_model.generation_config.cache_implementation = None
        tokenizer.pad_token_id = tokenizer.eos_token_id
        tokenization_args = {}
        if is_transformers_version(">=", "4.45") and model_arch == "gpt_neo":
            tokenization_args["padding_side"] = "left"
        tokens = tokenizer(
            ["Today is a nice day and I am longer", "This is me"],
            return_tensors="pt",
            padding=True,
            **tokenization_args,
        )
        ov_model_stateful.generation_config.eos_token_id = None
        ov_model_stateful.generation_config.forced_eos_token_id = None
        ov_model_stateful.generation_config.encoder_no_repeat_ngram_size = None
        ov_model_stateful.generation_config.do_sample = False
        ov_model_stateless.generation_config.eos_token_id = None
        ov_model_stateless.generation_config.forced_eos_token_id = None
        ov_model_stateless.generation_config.encoder_no_repeat_ngram_size = None
        ov_model_stateless.generation_config.do_sample = False
        transformers_model.generation_config.eos_token_id = None
        transformers_model.generation_config.forced_eos_token_id = None
        transformers_model.generation_config.encoder_no_repeat_ngram_size = None
        transformers_model.generation_config.do_sample = False
        ov_model_stateful.config.eos_token_id = None
        ov_model_stateless.config.eos_token_id = None
        transformers_model.config.eos_token_id = None

        if is_transformers_version(">=", "4.51"):
            additional_inputs["use_model_defaults"] = False

        for gen_config in gen_configs:
            if gen_config.do_sample and model_arch in ["baichuan2-13b", "olmo"]:
                continue
            if gen_config.num_beams > 1 and is_transformers_version(">=", "4.51.0") and model_arch in ["mixtral_awq"]:
                continue
            set_seed(SEED)

            if model_arch in ["gemma2", "gemma3_text"]:
                from transformers.cache_utils import DynamicCache

                additional_inputs["past_key_values"] = DynamicCache()
            with patch_awq_for_inference("awq" in model_arch):
                transformers_outputs = transformers_model.generate(
                    **tokens, generation_config=gen_config, **additional_inputs
                )
            set_seed(SEED)
            ov_stateful_outputs = ov_model_stateful.generate(**tokens, generation_config=gen_config)
            self.assertTrue(
                torch.equal(ov_stateful_outputs, transformers_outputs),
                f"generation config : {gen_config}, transformers output {transformers_outputs}, ov_model_stateful output {ov_stateful_outputs}",
            )

            set_seed(SEED)
            ov_stateless_outputs = ov_model_stateless.generate(**tokens, generation_config=gen_config)
            self.assertTrue(
                torch.equal(ov_stateless_outputs, transformers_outputs),
                f"generation config : {gen_config}, transformers output {transformers_outputs}, ov_model_stateless output {ov_stateless_outputs}",
            )

    def test_load_with_different_dtype(self):
        set_seed(SEED)
        model_id = MODEL_NAMES["llama"]
        pt_model = AutoModelForCausalLM.from_pretrained(
            model_id,
        )
        tokenizer = AutoTokenizer.from_pretrained(model_id)

        texts = ["this is a simple input"]
        test_input = tokenizer(texts, return_tensors="pt")

        ref_logits = pt_model(**test_input).logits
        torch_dtypes = [None, "auto", "float32", torch.float16]
        if is_openvino_version(">", "2024.2.0"):
            torch_dtypes.append("bfloat16")

        for dtype in torch_dtypes:
            ov_model = OVModelForCausalLM.from_pretrained(model_id=model_id, export=True, torch_dtype=dtype)
            ov_logits = ov_model(**test_input).logits
            self.assertTrue(
                torch.allclose(torch.Tensor(ov_logits), ref_logits, atol=5e-3),
                f"values are not close for {dtype if dtype is not None else 'None'}, max diff = {torch.abs(ov_logits - ref_logits).max()}",
            )


class OVModelForMaskedLMIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "albert",
        "bert",
        "camembert",
        "convbert",
        "data2vec-text",
        "deberta",
        "deberta-v2",
        "distilbert",
        "electra",
        "esm",
        "flaubert",
        "ibert",
        "mobilebert",
        "mpnet",
        "perceiver_text",
        "roberta",
        "roformer",
        "squeezebert",
        "xlm",
        "xlm-roberta",
    )

    # accuracy issue, need additional investigation
    if is_transformers_version("<", "4.51.0"):
        SUPPORTED_ARCHITECTURES += ("nystromformer",)

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForMaskedLM.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        set_seed(SEED)
        transformers_model = AutoModelForMaskedLM.from_pretrained(model_id)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        inputs = f"This is a sample {tokenizer.mask_token}"
        tokens = tokenizer(inputs, return_tensors="pt")
        with torch.no_grad():
            transformers_outputs = transformers_model(**tokens)
        for input_type in ["pt", "np"]:
            tokens = tokenizer(inputs, return_tensors=input_type)
            ov_outputs = ov_model(**tokens)
            self.assertIn("logits", ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])
            # Compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-4))
        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_pipeline(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        model = OVModelForMaskedLM.from_pretrained(model_id)
        model.eval()
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        pipe = pipeline("fill-mask", model=model, tokenizer=tokenizer)
        inputs = f"This is a {tokenizer.mask_token}."
        outputs = pipe(inputs)
        self.assertEqual(pipe.device, model.device)
        self.assertTrue(all(item["score"] > 0.0 for item in outputs))
        set_seed(SEED)
        ov_pipe = optimum_pipeline("fill-mask", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs)
        self.assertEqual(outputs[-1]["score"], ov_outputs[-1]["score"])
        del ov_pipe
        del pipe
        del model
        gc.collect()


class OVModelForImageClassificationIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "beit",
        "convnext",
        # "convnextv2",
        "data2vec-vision",
        "deit",
        "levit",
        "mobilenet_v1",
        "mobilenet_v2",
        "mobilevit",
        "poolformer",
        "perceiver_vision",
        "resnet",
        "segformer",
        "swin",
        "vit",
    )
    TIMM_MODELS = ("timm/pit_s_distilled_224.in1k", "timm/vit_tiny_patch16_224.augreg_in21k")

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForImageClassification.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        set_seed(SEED)
        transformers_model = AutoModelForImageClassification.from_pretrained(model_id)
        preprocessor = AutoFeatureExtractor.from_pretrained(model_id)
        url = TEST_IMAGE_URL
        image = Image.open(requests.get(url, stream=True).raw)
        inputs = preprocessor(images=image, return_tensors="pt")
        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs)
        for input_type in ["pt", "np"]:
            inputs = preprocessor(images=image, return_tensors=input_type)
            ov_outputs = ov_model(**inputs)
            self.assertIn("logits", ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])
            # Compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-4))
        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForImageClassification.from_pretrained(model_id)
        model.eval()
        preprocessor = AutoFeatureExtractor.from_pretrained(model_id)
        pipe = pipeline("image-classification", model=model, feature_extractor=preprocessor)
        inputs = TEST_IMAGE_URL
        outputs = pipe(inputs)
        self.assertEqual(pipe.device, model.device)
        self.assertGreaterEqual(outputs[0]["score"], 0.0)
        self.assertTrue(isinstance(outputs[0]["label"], str))
        set_seed(SEED)
        ov_pipe = optimum_pipeline("image-classification", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs)
        self.assertEqual(outputs[-1]["score"], ov_outputs[-1]["score"])
        del ov_pipe
        del model
        del pipe
        gc.collect()

    @parameterized.expand(TIMM_MODELS)
    def test_compare_to_timm(self, model_id):
        ov_model = OVModelForImageClassification.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertEqual(ov_model.request.get_property("INFERENCE_PRECISION_HINT").to_string(), "f32")
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        timm_model = timm.create_model(model_id, pretrained=True)
        preprocessor = TimmImageProcessor.from_pretrained(model_id)
        url = TEST_IMAGE_URL
        image = Image.open(requests.get(url, stream=True).raw)
        inputs = preprocessor(images=image, return_tensors="pt")
        with torch.no_grad():
            timm_model.eval()
            timm_outputs = timm_model(inputs["pixel_values"].float())
        for input_type in ["pt", "np"]:
            inputs = preprocessor(images=image, return_tensors=input_type)
            ov_outputs = ov_model(**inputs)
            self.assertIn("logits", ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])
            # Compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), timm_outputs, atol=1e-3))
        gc.collect()

    @parameterized.expand(TIMM_MODELS)
    def test_timm_save_and_infer(self, model_id):
        ov_model = OVModelForImageClassification.from_pretrained(model_id, export=True)
        with TemporaryDirectory() as tmpdirname:
            model_save_path = os.path.join(tmpdirname, "timm_ov_model")
            ov_model.save_pretrained(model_save_path)
            model = OVModelForImageClassification.from_pretrained(model_save_path)
            model(pixel_values=torch.zeros((5, 3, model.config.image_size, model.config.image_size)))
        gc.collect()


class OVModelForSeq2SeqLMIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "bart",
        # "bigbird_pegasus",
        "blenderbot",
        "blenderbot-small",
        # "longt5",
        "m2m_100",
        "marian",
        "mbart",
        "mt5",
        "pegasus",
        "t5",
    )

    GENERATION_LENGTH = 100
    SPEEDUP_CACHE = 1.1

    SUPPORT_STATEFUL = ("t5", "mt5")
    if is_transformers_version(">=", "4.52.0"):
        SUPPORT_STATEFUL += ("bart", "blenderbot", "blenderbot-small", "m2m_100", "marian", "mbart")
    if is_transformers_version(">=", "4.53.0"):
        SUPPORT_STATEFUL += ("pegasus",)

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForSeq2SeqLM.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        expected_stateful = is_transformers_version(">", "4.43") and model_arch in self.SUPPORT_STATEFUL
        self.assertEqual(ov_model.decoder.stateful, expected_stateful)
        self.assertEqual(model_has_state(ov_model.decoder.model), expected_stateful)
        check_with_past_available = self.assertIsNone if expected_stateful else self.assertIsNotNone
        check_with_past_available(ov_model.decoder_with_past)
        self.assertIsInstance(ov_model.encoder, OVEncoder)
        self.assertIsInstance(ov_model.decoder, OVDecoder)
        if not ov_model.decoder.stateful:
            self.assertIsInstance(ov_model.decoder_with_past, OVDecoder)
            self.assertIsInstance(ov_model.config, PretrainedConfig)

        transformers_model = AutoModelForSeq2SeqLM.from_pretrained(model_id)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokens = tokenizer("This is a sample input", return_tensors="pt")
        decoder_start_token_id = transformers_model.config.decoder_start_token_id if model_arch != "mbart" else 2
        decoder_inputs = {"decoder_input_ids": torch.ones((1, 1), dtype=torch.long) * decoder_start_token_id}
        ov_outputs = ov_model(**tokens, **decoder_inputs)

        self.assertTrue("logits" in ov_outputs)
        self.assertIsInstance(ov_outputs.logits, torch.Tensor)

        with torch.no_grad():
            transformers_outputs = transformers_model(**tokens, **decoder_inputs)
        # Compare tensor outputs
        self.assertTrue(torch.allclose(ov_outputs.logits, transformers_outputs.logits, atol=5e-3))
        gen_config = GenerationConfig(
            max_new_tokens=10,
            min_new_tokens=10,
            num_beams=2,
            do_sample=False,
            eos_token_id=None,
        )

        set_seed(SEED)
        generated_tokens = transformers_model.generate(**tokens, generation_config=gen_config)
        set_seed(SEED)
        ov_generated_tokens = ov_model.generate(**tokens, generation_config=gen_config)

        self.assertTrue(torch.equal(generated_tokens, ov_generated_tokens))

        del transformers_model
        del ov_model

        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        inputs = "This is a test"
        model = OVModelForSeq2SeqLM.from_pretrained(model_id, compile=False)
        model.eval()
        model.half()
        model.to("cpu")
        model.compile()

        # Summarization
        pipe = pipeline("summarization", model=model, tokenizer=tokenizer)
        outputs = pipe(inputs)
        self.assertEqual(pipe.device, model.device)
        self.assertIsInstance(outputs[0]["summary_text"], str)

        # Translation
        pipe = pipeline("translation_en_to_fr", model=model, tokenizer=tokenizer)
        outputs = pipe(inputs)
        self.assertEqual(pipe.device, model.device)
        self.assertIsInstance(outputs[0]["translation_text"], str)

        # Text2Text generation
        pipe = pipeline("text2text-generation", model=model, tokenizer=tokenizer)
        outputs = pipe(inputs, max_new_tokens=20)
        self.assertEqual(pipe.device, model.device)
        self.assertIsInstance(outputs[0]["generated_text"], str)

        ov_pipe = optimum_pipeline("text2text-generation", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs, max_new_tokens=20)
        self.assertEqual(outputs[-1]["generated_text"], ov_outputs[-1]["generated_text"])
        del ov_pipe
        del pipe
        del model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_generate_utils(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForSeq2SeqLM.from_pretrained(model_id, export=True)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        text = "This is a sample input"
        tokens = tokenizer(text, return_tensors="pt")

        # General case
        outputs = model.generate(**tokens)
        outputs = tokenizer.batch_decode(outputs, skip_special_tokens=True)
        self.assertIsInstance(outputs[0], str)

        # With input ids
        outputs = model.generate(input_ids=tokens["input_ids"])
        outputs = tokenizer.batch_decode(outputs, skip_special_tokens=True)
        self.assertIsInstance(outputs[0], str)
        del model

        gc.collect()

    def test_compare_with_and_without_past_key_values(self):
        model_id = MODEL_NAMES["bart"]
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        text = "This is a sample input"
        tokens = tokenizer(text, return_tensors="pt")

        model_with_pkv = OVModelForSeq2SeqLM.from_pretrained(model_id, export=True, use_cache=True)
        _ = model_with_pkv.generate(**tokens)  # warmup
        with Timer() as with_pkv_timer:
            outputs_model_with_pkv = model_with_pkv.generate(
                **tokens, min_length=self.GENERATION_LENGTH, max_length=self.GENERATION_LENGTH, num_beams=1
            )

        model_without_pkv = OVModelForSeq2SeqLM.from_pretrained(model_id, export=True, use_cache=False)
        _ = model_without_pkv.generate(**tokens)  # warmup
        with Timer() as without_pkv_timer:
            outputs_model_without_pkv = model_without_pkv.generate(
                **tokens, min_length=self.GENERATION_LENGTH, max_length=self.GENERATION_LENGTH, num_beams=1
            )

        self.assertTrue(torch.equal(outputs_model_with_pkv, outputs_model_without_pkv))
        self.assertEqual(outputs_model_with_pkv.shape[1], self.GENERATION_LENGTH)
        self.assertEqual(outputs_model_without_pkv.shape[1], self.GENERATION_LENGTH)
        self.assertTrue(
            without_pkv_timer.elapsed / with_pkv_timer.elapsed > self.SPEEDUP_CACHE,
            f"With pkv latency: {with_pkv_timer.elapsed:.3f} ms, without pkv latency: {without_pkv_timer.elapsed:.3f} ms,"
            f" speedup: {without_pkv_timer.elapsed / with_pkv_timer.elapsed:.3f}",
        )
        del model_with_pkv
        del model_without_pkv
        gc.collect()


class OVModelForAudioClassificationIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = (
        "audio-spectrogram-transformer",
        "data2vec-audio",
        "hubert",
        "sew",
        "sew_d",
        "unispeech",
        "unispeech-sat",
        "wavlm",
        "wav2vec2",
        "wav2vec2-conformer",
    )

    def _generate_random_audio_data(self):
        np.random.seed(10)
        t = np.linspace(0, 5.0, int(5.0 * 22050), endpoint=False)
        # generate pure sine wave at 220 Hz
        audio_data = 0.5 * np.sin(2 * np.pi * 220 * t)
        return audio_data

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForAudioClassification.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        set_seed(SEED)
        transformers_model = AutoModelForAudioClassification.from_pretrained(model_id)
        preprocessor = AutoFeatureExtractor.from_pretrained(model_id)
        inputs = preprocessor(self._generate_random_audio_data(), return_tensors="pt")

        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs)

        for input_type in ["pt", "np"]:
            inputs = preprocessor(self._generate_random_audio_data(), return_tensors=input_type)
            ov_outputs = ov_model(**inputs)
            self.assertIn("logits", ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])
            # Compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-3))

        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForAudioClassification.from_pretrained(model_id)
        model.eval()
        preprocessor = AutoFeatureExtractor.from_pretrained(model_id)
        pipe = pipeline("audio-classification", model=model, feature_extractor=preprocessor)
        inputs = [np.random.random(16000)]
        outputs = pipe(inputs)
        self.assertEqual(pipe.device, model.device)
        self.assertTrue(all(item["score"] > 0.0 for item in outputs[0]))
        set_seed(SEED)
        ov_pipe = optimum_pipeline("audio-classification", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs)
        self.assertEqual(outputs[-1][-1]["score"], ov_outputs[-1][-1]["score"])
        del ov_pipe
        del pipe
        del model
        gc.collect()


class OVModelForCTCIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = [
        "data2vec-audio",
        "hubert",
        "sew",
        "sew_d",
        "unispeech",
        "unispeech-sat",
        "wavlm",
        "wav2vec2-hf",
        "wav2vec2-conformer",
    ]

    def _generate_random_audio_data(self):
        np.random.seed(10)
        t = np.linspace(0, 5.0, int(5.0 * 22050), endpoint=False)
        # generate pure sine wave at 220 Hz
        audio_data = 0.5 * np.sin(2 * np.pi * 220 * t)
        return audio_data

    def test_load_vanilla_transformers_which_is_not_supported(self):
        with self.assertRaises(Exception) as context:
            _ = OVModelForCTC.from_pretrained(MODEL_NAMES["t5"], export=True)

        self.assertIn("only supports the tasks", str(context.exception))

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForCTC.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)

        set_seed(SEED)
        transformers_model = AutoModelForCTC.from_pretrained(model_id)
        processor = AutoFeatureExtractor.from_pretrained(model_id)
        input_values = processor(self._generate_random_audio_data(), return_tensors="pt")

        with torch.no_grad():
            transformers_outputs = transformers_model(**input_values)

        for input_type in ["pt", "np"]:
            input_values = processor(self._generate_random_audio_data(), return_tensors=input_type)
            ov_outputs = ov_model(**input_values)

            self.assertTrue("logits" in ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])

            # compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-4))

        del transformers_model
        del ov_model
        gc.collect()


class OVModelForAudioXVectorIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = [
        "data2vec-audio",
        "unispeech-sat",
        "wavlm",
        "wav2vec2-hf",
        "wav2vec2-conformer",
    ]

    def _generate_random_audio_data(self):
        np.random.seed(10)
        t = np.linspace(0, 5.0, int(5.0 * 22050), endpoint=False)
        # generate pure sine wave at 220 Hz
        audio_data = 0.5 * np.sin(2 * np.pi * 220 * t)
        return audio_data

    def test_load_vanilla_transformers_which_is_not_supported(self):
        with self.assertRaises(Exception) as context:
            _ = OVModelForAudioXVector.from_pretrained(MODEL_NAMES["t5"], export=True)

        self.assertIn("only supports the tasks", str(context.exception))

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForAudioXVector.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)

        set_seed(SEED)
        transformers_model = AutoModelForAudioXVector.from_pretrained(model_id)
        processor = AutoFeatureExtractor.from_pretrained(model_id)
        input_values = processor(self._generate_random_audio_data(), return_tensors="pt")

        with torch.no_grad():
            transformers_outputs = transformers_model(**input_values)
        for input_type in ["pt", "np"]:
            input_values = processor(self._generate_random_audio_data(), return_tensors=input_type)
            ov_outputs = ov_model(**input_values)

            self.assertTrue("logits" in ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])

            # compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-4))
            self.assertTrue(
                torch.allclose(torch.Tensor(ov_outputs.embeddings), transformers_outputs.embeddings, atol=1e-4)
            )

        del transformers_model
        del ov_model
        gc.collect()


class OVModelForAudioFrameClassificationIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = [
        "data2vec-audio",
        "unispeech-sat",
        "wavlm",
        "wav2vec2-hf",
        "wav2vec2-conformer",
    ]

    def _generate_random_audio_data(self):
        np.random.seed(10)
        t = np.linspace(0, 5.0, int(5.0 * 22050), endpoint=False)
        # generate pure sine wave at 220 Hz
        audio_data = 0.5 * np.sin(2 * np.pi * 220 * t)
        return audio_data

    def test_load_vanilla_transformers_which_is_not_supported(self):
        with self.assertRaises(Exception) as context:
            _ = OVModelForAudioFrameClassification.from_pretrained(MODEL_NAMES["t5"], export=True)

        self.assertIn("only supports the tasks", str(context.exception))

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForAudioFrameClassification.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)

        set_seed(SEED)
        transformers_model = AutoModelForAudioFrameClassification.from_pretrained(model_id)
        processor = AutoFeatureExtractor.from_pretrained(model_id)
        input_values = processor(self._generate_random_audio_data(), return_tensors="pt")

        with torch.no_grad():
            transformers_outputs = transformers_model(**input_values)
        for input_type in ["pt", "np"]:
            input_values = processor(self._generate_random_audio_data(), return_tensors=input_type)
            ov_outputs = ov_model(**input_values)

            self.assertTrue("logits" in ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])

            # compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-4))

        del transformers_model
        del ov_model
        gc.collect()


class OVModelForPix2StructIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ["pix2struct"]
    TASK = "image-to-text"  # is it fine as well with visual-question-answering?

    GENERATION_LENGTH = 100
    SPEEDUP_CACHE = 1.1

    IMAGE = Image.open(
        requests.get(
            "https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/transformers/tasks/ai2d-demo.jpg",
            stream=True,
        ).raw
    )

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForPix2Struct.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)

        self.assertIsInstance(ov_model.encoder, OVEncoder)
        self.assertIsInstance(ov_model.decoder, OVDecoder)
        self.assertIsInstance(ov_model.decoder_with_past, OVDecoder)
        self.assertIsInstance(ov_model.config, PretrainedConfig)

        question = "Who am I?"
        transformers_model = Pix2StructForConditionalGeneration.from_pretrained(model_id)
        preprocessor = get_preprocessor(model_id)

        inputs = preprocessor(images=self.IMAGE, text=question, padding=True, return_tensors="pt")
        ov_outputs = ov_model(**inputs)

        self.assertTrue("logits" in ov_outputs)
        self.assertIsInstance(ov_outputs.logits, torch.Tensor)

        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs)
        # Compare tensor outputs
        self.assertTrue(torch.allclose(ov_outputs.logits, transformers_outputs.logits, atol=1e-4))
        del transformers_model
        del ov_model

        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_generate_utils(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForPix2Struct.from_pretrained(model_id, export=True)
        preprocessor = get_preprocessor(model_id)
        question = "Who am I?"
        inputs = preprocessor(images=self.IMAGE, text=question, return_tensors="pt")

        # General case
        outputs = model.generate(**inputs)
        outputs = preprocessor.batch_decode(outputs, skip_special_tokens=True)
        self.assertIsInstance(outputs[0], str)
        del model

        gc.collect()

    def test_compare_with_and_without_past_key_values(self):
        model_id = MODEL_NAMES["pix2struct"]
        preprocessor = get_preprocessor(model_id)
        question = "Who am I?"
        inputs = preprocessor(images=self.IMAGE, text=question, return_tensors="pt")

        model_with_pkv = OVModelForPix2Struct.from_pretrained(model_id, export=True, use_cache=True)
        _ = model_with_pkv.generate(**inputs)  # warmup
        with Timer() as with_pkv_timer:
            outputs_model_with_pkv = model_with_pkv.generate(
                **inputs, min_length=self.GENERATION_LENGTH, max_length=self.GENERATION_LENGTH, num_beams=1
            )

        model_without_pkv = OVModelForPix2Struct.from_pretrained(model_id, export=True, use_cache=False)
        _ = model_without_pkv.generate(**inputs)  # warmup
        with Timer() as without_pkv_timer:
            outputs_model_without_pkv = model_without_pkv.generate(
                **inputs, min_length=self.GENERATION_LENGTH, max_length=self.GENERATION_LENGTH, num_beams=1
            )

        self.assertTrue(torch.equal(outputs_model_with_pkv, outputs_model_without_pkv))
        self.assertEqual(outputs_model_with_pkv.shape[1], self.GENERATION_LENGTH)
        self.assertEqual(outputs_model_without_pkv.shape[1], self.GENERATION_LENGTH)
        self.assertTrue(
            without_pkv_timer.elapsed / with_pkv_timer.elapsed > self.SPEEDUP_CACHE,
            f"With pkv latency: {with_pkv_timer.elapsed:.3f} ms, without pkv latency: {without_pkv_timer.elapsed:.3f} ms,"
            f" speedup: {without_pkv_timer.elapsed / with_pkv_timer.elapsed:.3f}",
        )
        del model_with_pkv
        del model_without_pkv
        gc.collect()


class OVModelForVisualCausalLMIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ["llava"]
    SUPPORT_VIDEO = []
    SUPPORT_AUDIO = []

    if is_transformers_version(">=", "4.40.0"):
        SUPPORTED_ARCHITECTURES += ["llava_next", "nanollava"]

    if is_transformers_version(">=", "4.42.0"):
        SUPPORTED_ARCHITECTURES += ["llava_next_video"]
        SUPPORT_VIDEO.append("llava_next_video")

    if is_transformers_version(">=", "4.45.0"):
        SUPPORTED_ARCHITECTURES += ["minicpmv", "internvl2", "phi3_v", "qwen2_vl"]
        SUPPORT_VIDEO.append("qwen2_vl")

    if is_transformers_version(">=", "4.46.0"):
        SUPPORTED_ARCHITECTURES += ["maira2", "idefics3"]

    if is_transformers_version(">=", "4.49.0"):
        SUPPORTED_ARCHITECTURES += ["qwen2_5_vl", "got_ocr2", "phi4mm"]
        SUPPORT_VIDEO.append("qwen2_5_vl")
        SUPPORT_AUDIO.append("phi4mm")
    if is_transformers_version(">", "4.49"):
        SUPPORTED_ARCHITECTURES += ["gemma3", "smolvlm"]
    if is_transformers_version(">=", "4.51"):
        SUPPORTED_ARCHITECTURES += ["llama4"]
    TASK = "image-text-to-text"
    REMOTE_CODE_MODELS = ["internvl2", "minicpmv", "nanollava", "phi3_v", "maira2", "phi4mm"]

    IMAGE = Image.open(
        requests.get(
            TEST_IMAGE_URL,
            stream=True,
        ).raw
    )

    def get_transformer_model_class(self, model_arch):
        if is_transformers_version(">=", "4.46") and model_arch in [
            "llava",
            "llava_next",
            "qwen2_vl",
            "qwen2_5_vl",
            "got_ocr2",
            "gemma3",
            "idefics3",
            "smolvlm",
            "llama4",
        ]:
            from transformers import AutoModelForImageTextToText

            return AutoModelForImageTextToText
        if model_arch == "llava_next_video":
            from transformers import AutoModelForVision2Seq

            return AutoModelForVision2Seq
        if model_arch == "llava":
            from transformers import LlavaForConditionalGeneration

            return LlavaForConditionalGeneration
        if model_arch == "llava_next":
            from transformers import LlavaNextForConditionalGeneration

            return LlavaNextForConditionalGeneration
        if model_arch == "qwen2_vl":
            from transformers import Qwen2VLForConditionalGeneration

            return Qwen2VLForConditionalGeneration
        return AutoModelForCausalLM

    def _check_device_and_request(self, ov_model, expected_device, has_request):
        request_check_fn = self.assertFalse if has_request else self.assertTrue
        self.assertEqual(ov_model._device, expected_device)
        for component_name, component in ov_model.components.items():
            if component_name == "language_model":
                request_check_fn(component.text_emb_request is None)
            self.assertEqual(component._device, expected_device)
            request_check_fn(component.request is None)

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        prompt = "What is shown in this image?"
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        loading_kwargs = {}

        if "llama4" in model_arch:
            loading_kwargs = {"_attn_implementation": "sdpa"}
        transformers_model = self.get_transformer_model_class(model_arch).from_pretrained(
            model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS, **loading_kwargs
        )
        transformers_model.eval()
        if "internvl2" in model_arch:
            tokenizer = AutoTokenizer.from_pretrained(
                model_id, trast_remote_code=model_arch in self.REMOTE_CODE_MODELS
            )
            img_context_token_id = tokenizer.convert_tokens_to_ids("<IMG_CONTEXT>")
            transformers_model.img_context_token_id = img_context_token_id
        if "nanollava" in model_arch:
            transformers_model.get_vision_tower().load_model()
        preprocessors = self.get_preprocessors(model_arch)
        set_seed(SEED)
        ov_model = OVModelForVisualCausalLM.from_pretrained(
            model_id, export=True, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS, compile=False
        )
        self.assertIsInstance(ov_model, MODEL_TYPE_TO_CLS_MAPPING[ov_model.config.model_type])
        for component_name, component in ov_model.components.items():
            self.assertIsInstance(component, MODEL_PARTS_CLS_MAPPING[component_name])
        self.assertIsInstance(ov_model.config, PretrainedConfig)

        inputs = ov_model.preprocess_inputs(**preprocessors, text=prompt, image=self.IMAGE.resize((600, 600)))
        transformers_inputs = copy.deepcopy(inputs)
        # llama4 preprocessing force bf16 dtype for pixel_values, that does not work on CPU with fp32 model
        # if past key values are not initialized, llama4 creates HybridCache with bf16 precision
        if model_arch == "llama4":
            transformers_inputs["pixel_values"] = transformers_inputs["pixel_values"].to(torch.float32)
            transformers_model.generation_config.cache_implementation = None
            from transformers.cache_utils import DynamicCache

            transformers_inputs["past_key_values"] = DynamicCache()

        test_device = "AUTO"
        ov_model.to(test_device)
        self._check_device_and_request(ov_model, test_device, False)
        test_device = "CPU"
        ov_model.to(test_device)
        ov_model.compile()
        self._check_device_and_request(ov_model, test_device, True)
        ov_model.clear_requests()
        self._check_device_and_request(ov_model, test_device, False)

        # pytorch minicpmv and internvl2 are not designed to be used via forward
        if model_arch not in ["minicpmv", "internvl2"]:
            set_seed(SEED)
            ov_outputs = ov_model(**inputs)
            set_seed(SEED)
            with torch.no_grad():
                transformers_outputs = transformers_model(**transformers_inputs)
            self.assertTrue(
                torch.allclose(ov_outputs.logits, transformers_outputs.logits, atol=4e-3),
                f"Max abs diff {(torch.abs(ov_outputs.logits - transformers_outputs.logits).max())}",
            )

        ov_model.generation_config.eos_token_id = None
        transformers_model.generation_config.eos_token_id = None
        transformers_model.generation_config.do_sample = False
        ov_model.config.eos_token_id = None
        transformers_model.config.eos_token_id = None
        ov_model.generation_config.do_sample = False
        gen_config = GenerationConfig(
            max_new_tokens=30,
            min_new_tokens=30,
            do_sample=False,
            eos_token_id=None,
        )
        set_seed(SEED)
        ov_outputs = ov_model.generate(**inputs, generation_config=gen_config)
        set_seed(SEED)

        additional_inputs = {}
        # gemma3 does not support dynamic cache, it is unfair to compare dynamic cache result vs hybrid cache,
        # align cache representation in torch model
        if model_arch == "gemma3":
            patch_update_causal_mask(
                transformers_model if is_transformers_version("<", "4.52.0") else transformers_model.language_model,
                "4.43.0",
            )
            transformers_model._supports_cache_class = True
            transformers_model.generation_config.cache_implementation = None
            from transformers.cache_utils import DynamicCache

            additional_inputs = {"past_key_values": DynamicCache()}

        if model_arch == "llama4":
            transformers_inputs["past_key_values"] = DynamicCache()

        with torch.no_grad():
            transformers_outputs = transformers_model.generate(
                **transformers_inputs, generation_config=gen_config, **additional_inputs
            )

        # original minicpmv, internvl always skip input tokens in generation results, while transformers based approach provide them
        if model_arch in ["minicpmv", "internvl2"]:
            ov_outputs = ov_outputs[:, inputs["input_ids"].shape[1] :]
        self.assertTrue(
            torch.equal(ov_outputs, transformers_outputs),
            f"generation config : {gen_config}, transformers output {transformers_outputs}, ov_model output {ov_outputs}",
        )

        # video loader helper only available for transformers >= 4.49
        if model_arch in self.SUPPORT_VIDEO and is_transformers_version(">=", "4.49"):
            if is_transformers_version("<=", "4.52"):
                from transformers.image_utils import load_video
            else:
                from transformers.video_utils import load_video

            video_path = hf_hub_download(
                repo_id="raushan-testing-hf/videos-test",
                filename="sample_demo_1.mp4",
                repo_type="dataset",
                user_agent=http_user_agent(),
            )
            input_video, _ = load_video(video_path, num_frames=2, backend="opencv")
            question = "Why is this video funny?"
            inputs = ov_model.preprocess_inputs(**preprocessors, text=question, video=input_video)
            transformers_inputs = copy.deepcopy(inputs)
            ov_outputs = ov_model.generate(**inputs, generation_config=gen_config)
            # original minicpmv, internvl always skip input tokens in generation results, while transformers based approach provide them
            if model_arch in ["minicpmv", "internvl2"]:
                ov_outputs = ov_outputs[:, inputs["input_ids"].shape[1] :]
            with torch.no_grad():
                transformers_outputs = transformers_model.generate(
                    **transformers_inputs, generation_config=gen_config, **additional_inputs
                )
            self.assertTrue(
                torch.equal(ov_outputs, transformers_outputs),
                f"generation config : {gen_config}, transformers output {transformers_outputs}, ov_model output {ov_outputs}",
            )

        if model_arch in self.SUPPORT_AUDIO:
            input_audio = self._generate_random_audio_data()
            question = "Translate this audio to French"
            inputs = ov_model.preprocess_inputs(**preprocessors, text=question, audio=[input_audio])
            transformers_inputs = copy.deepcopy(inputs)
            ov_outputs = ov_model.generate(**inputs, generation_config=gen_config)
            # original minicpmv, internvl always skip input tokens in generation results, while transformers based approach provide them
            if model_arch in ["minicpmv", "internvl2"]:
                ov_outputs = ov_outputs[:, inputs["input_ids"].shape[1] :]
            with torch.no_grad():
                transformers_outputs = transformers_model.generate(
                    **transformers_inputs, generation_config=gen_config, **additional_inputs
                )
            self.assertTrue(
                torch.equal(ov_outputs, transformers_outputs),
                f"generation config : {gen_config}, transformers output {transformers_outputs}, ov_model output {ov_outputs}",
            )
        del transformers_model
        del ov_model

        gc.collect()

    @parameterized.expand(["llava", "llava_next", "llava_next_video"])
    @unittest.skipIf(
        is_transformers_version("<", "4.45.0"), reason="New preprocessing available only in transformers >= 4.45"
    )
    def test_llava_with_new_preprocessing(self, model_arch):
        prompt = "<image>\n What is shown in this image?"
        model_id = MODEL_NAMES[model_arch]
        config = AutoConfig.from_pretrained(model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS)
        processor = AutoProcessor.from_pretrained(
            model_id,
            patch_size=config.vision_config.patch_size,
            vision_feature_select_strategy=config.vision_feature_select_strategy,
            trust_remote_code=model_arch in self.REMOTE_CODE_MODELS,
            num_additional_image_tokens=1,
        )
        transformers_model = self.get_transformer_model_class(model_arch).from_pretrained(model_id)
        ov_model = OVModelForVisualCausalLM.from_pretrained(
            model_id, export=True, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS
        )
        self.assertTrue(ov_model._support_new_processing)
        self.assertTrue(processor.patch_size is not None)
        self.assertTrue(processor.vision_feature_select_strategy is not None)
        inputs = processor(images=self.IMAGE, text=prompt, return_tensors="pt")
        self.assertGreaterEqual(
            (inputs.input_ids == ov_model.config.image_token_index).sum().max().item(),
            ov_model.config.image_seq_length,
        )
        set_seed(SEED)
        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs)
        set_seed(SEED)
        ov_outputs = ov_model(**inputs)
        self.assertTrue(torch.allclose(ov_outputs.logits, transformers_outputs.logits, atol=1e-4))
        ov_model.generation_config.eos_token_id = None
        transformers_model.generation_config.eos_token_id = None
        ov_model.config.eos_token_id = None
        transformers_model.config.eos_token_id = None
        gen_config = GenerationConfig(
            max_new_tokens=30,
            min_new_tokens=30,
            num_beams=3,
            do_sample=False,
            eos_token_id=None,
        )
        set_seed(SEED)
        ov_outputs = ov_model.generate(**inputs, generation_config=gen_config)
        set_seed(SEED)
        with torch.no_grad():
            transformers_outputs = transformers_model.generate(**inputs, generation_config=gen_config)
        self.assertTrue(
            torch.equal(ov_outputs, transformers_outputs),
            f"generation config : {gen_config}, transformers output {transformers_outputs}, ov_model output {ov_outputs}",
        )

        del ov_model
        del transformers_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_generate_utils(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForVisualCausalLM.from_pretrained(
            model_id, export=True, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS
        )

        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS)
        question = "Describe image"
        preprocessors = self.get_preprocessors(model_arch)
        inputs = model.preprocess_inputs(**preprocessors, text=question, image=self.IMAGE.resize((600, 600)))
        # General case
        outputs = model.generate(**inputs, max_new_tokens=10)
        outputs = tokenizer.batch_decode(outputs[:, inputs["input_ids"].shape[1] :], skip_special_tokens=True)
        self.assertIsInstance(outputs[0], str)

        # GOT-OCR2 does not support text-only input
        if model_arch != "got_ocr2":
            # No input image case
            question = "Hi, how are you?"
            inputs = model.preprocess_inputs(**preprocessors, text=question, image=None)
            outputs = model.generate(**inputs, max_new_tokens=10)
            # filter out original prompt becuase it may contains out of tokenizer tokens e.g. in nanollva text separator = -200
            outputs = outputs[:, inputs["input_ids"].shape[1] :]
            outputs = tokenizer.batch_decode(outputs, skip_special_tokens=True)
            self.assertIsInstance(outputs[0], str)

            if model_arch in self.SUPPORT_VIDEO and is_transformers_version(">=", "4.49"):
                # video loader helper only available for transformers >= 4.49
                if is_transformers_version("<=", "4.52"):
                    from transformers.image_utils import load_video
                else:
                    from transformers.video_utils import load_video

                video_path = hf_hub_download(
                    repo_id="raushan-testing-hf/videos-test",
                    filename="sample_demo_1.mp4",
                    repo_type="dataset",
                    user_agent=http_user_agent(),
                )
                input_video, _ = load_video(video_path, num_frames=2, backend="opencv")
                question = "Why is this video funny?"
                inputs = model.preprocess_inputs(**preprocessors, text=question, video=input_video)
                outputs = model.generate(**inputs, max_new_tokens=10)
                # filter out original prompt becuase it may contains out of tokenizer tokens e.g. in nanollva text separator = -200
                outputs = outputs[:, inputs["input_ids"].shape[1] :]
                outputs = tokenizer.batch_decode(outputs, skip_special_tokens=True)
                self.assertIsInstance(outputs[0], str)

        if model_arch in self.SUPPORT_AUDIO:
            input_audio = self._generate_random_audio_data()
            question = "Translate this audio to French"
            inputs = model.preprocess_inputs(**preprocessors, text=question, audio=[input_audio])
            outputs = model.generate(**inputs, max_new_tokens=10)
            # filter out original prompt becuase it may contains out of tokenizer tokens e.g. in nanollva text separator = -200
            outputs = outputs[:, inputs["input_ids"].shape[1] :]
            outputs = tokenizer.batch_decode(outputs, skip_special_tokens=True)
            self.assertIsInstance(outputs[0], str)
        del model

        gc.collect()

    def _generate_random_audio_data(self):
        np.random.seed(10)
        t = np.linspace(0, 5.0, int(5.0 * 22050), endpoint=False)
        # generate pure sine wave at 220 Hz
        audio_data = 0.5 * np.sin(2 * np.pi * 220 * t)
        return (audio_data, 16000)

    def get_preprocessors(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        config = AutoConfig.from_pretrained(model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS)

        if model_arch == "nanollava":
            processor = AutoProcessor.from_pretrained(
                config.mm_vision_tower, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS
            )
            tokenizer = AutoTokenizer.from_pretrained(
                model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS
            )
            preprocessors = {"processor": processor, "tokenizer": tokenizer, "config": config}
        elif model_arch == "internvl2":
            tokenizer = AutoTokenizer.from_pretrained(
                model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS
            )
            preprocessors = {"processor": None, "tokenizer": tokenizer, "config": config}
        else:
            processor = AutoProcessor.from_pretrained(
                model_id, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS
            )
            preprocessors = {"processor": processor, "tokenizer": None, "config": config}

        return preprocessors

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_model_can_be_loaded_after_saving(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        with TemporaryDirectory() as save_dir:
            ov_model = OVModelForVisualCausalLM.from_pretrained(
                model_id, compile=False, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS
            )
            ov_model.save_pretrained(save_dir)
            ov_restored_model = OVModelForVisualCausalLM.from_pretrained(
                save_dir, compile=False, trust_remote_code=model_arch in self.REMOTE_CODE_MODELS
            )
            self.assertIsInstance(ov_restored_model, type(ov_model))


class OVModelForSpeechSeq2SeqIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ("whisper",)

    def _generate_random_audio_data(self):
        np.random.seed(10)
        t = np.linspace(0, 5.0, int(5.0 * 22050), endpoint=False)
        # generate pure sine wave at 220 Hz
        audio_data = 0.5 * np.sin(2 * np.pi * 220 * t)
        return audio_data

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        transformers_model = AutoModelForSpeechSeq2Seq.from_pretrained(model_id)
        ov_model = OVModelForSpeechSeq2Seq.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        # whisper cache class support implemented in 4.43
        expected_stateful = is_transformers_version(">", "4.43")
        self.assertEqual(ov_model.decoder.stateful, expected_stateful)
        self.assertEqual(model_has_state(ov_model.decoder.model), expected_stateful)
        check_with_past_available = self.assertIsNone if expected_stateful else self.assertIsNotNone
        check_with_past_available(ov_model.decoder_with_past)

        processor = get_preprocessor(model_id)
        data = self._generate_random_audio_data()
        pt_features = processor.feature_extractor(data, return_tensors="pt")
        decoder_start_token_id = transformers_model.config.decoder_start_token_id
        decoder_inputs = {"decoder_input_ids": torch.ones((1, 1), dtype=torch.long) * decoder_start_token_id}

        with torch.no_grad():
            transformers_outputs = transformers_model(**pt_features, **decoder_inputs)

        for input_type in ["pt", "np"]:
            features = processor.feature_extractor(data, return_tensors=input_type)

            if input_type == "np":
                decoder_inputs = {"decoder_input_ids": np.ones((1, 1), dtype=np.int64) * decoder_start_token_id}

            ov_outputs = ov_model(**features, **decoder_inputs)
            self.assertIn("logits", ov_outputs)
            # Compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-3))

        generate_kwrgs = {}
        if is_transformers_version(">=", "4.50"):
            generate_kwrgs = {"use_model_defaults": False}

        gen_config = GenerationConfig(
            max_new_tokens=10,
            min_new_tokens=10,
            num_beams=2,
            do_sample=False,
            eos_token_id=None,
        )

        set_seed(SEED)
        generated_tokens = transformers_model.generate(**pt_features, generation_config=gen_config, **generate_kwrgs)
        set_seed(SEED)
        ov_generated_tokens = ov_model.generate(**pt_features, generation_config=gen_config, **generate_kwrgs)

        self.assertTrue(torch.equal(generated_tokens, ov_generated_tokens))

        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForSpeechSeq2Seq.from_pretrained(model_id)
        processor = get_preprocessor(model_id)
        pipe = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
        )
        inputs = self._generate_random_audio_data()
        outputs = pipe(inputs)
        self.assertIsInstance(outputs["text"], str)

        ov_pipe = optimum_pipeline("automatic-speech-recognition", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs)
        self.assertEqual(outputs["text"], ov_outputs["text"])
        del ov_pipe
        del pipe
        del model
        gc.collect()


class OVModelForVision2SeqIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ["vision-encoder-decoder", "trocr", "donut"]

    GENERATION_LENGTH = 100
    SPEEDUP_CACHE = 1.1

    def _get_sample_image(self):
        url = TEST_IMAGE_URL
        image = Image.open(requests.get(url, stream=True).raw)
        return image

    def _get_preprocessors(self, model_id):
        image_processor = AutoImageProcessor.from_pretrained(model_id)
        tokenizer = AutoTokenizer.from_pretrained(model_id)

        return image_processor, tokenizer

    def test_load_vanilla_transformers_which_is_not_supported(self):
        with self.assertRaises(Exception) as context:
            _ = OVModelForVision2Seq.from_pretrained(MODEL_NAMES["bert"], export=True)

        self.assertIn("only supports the tasks", str(context.exception))

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_generate_utils(self, model_arch: str):
        model_id = MODEL_NAMES[model_arch]
        model = OVModelForVision2Seq.from_pretrained(model_id, export=True)
        feature_extractor, tokenizer = self._get_preprocessors(model_id)

        data = self._get_sample_image()
        features = feature_extractor(data, return_tensors="pt")

        outputs = model.generate(inputs=features["pixel_values"])
        res = tokenizer.batch_decode(outputs, skip_special_tokens=True)
        self.assertIsInstance(res[0], str)

        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch: str):
        model_id = MODEL_NAMES[model_arch]
        ov_model = OVModelForVision2Seq.from_pretrained(model_id, export=True)

        self.assertIsInstance(ov_model.encoder, OVEncoder)

        self.assertIsInstance(ov_model.decoder, OVDecoder)
        self.assertIsInstance(ov_model.decoder_with_past, OVDecoder)

        self.assertIsInstance(ov_model.config, PretrainedConfig)

        set_seed(SEED)
        transformers_model = AutoModelForVision2Seq.from_pretrained(model_id)
        feature_extractor, tokenizer = self._get_preprocessors(model_id)

        data = self._get_sample_image()

        start_token = "<s>"
        decoder_start_token_id = tokenizer.encode(start_token)[0]

        extra_inputs = [{}, {}]

        for extra_inps in extra_inputs:
            features = feature_extractor(data, return_tensors="pt")
            decoder_inputs = {"decoder_input_ids": torch.ones((1, 1), dtype=torch.long) * decoder_start_token_id}

            with torch.no_grad():
                transformers_outputs = transformers_model(**features, **decoder_inputs, **extra_inps, use_cache=True)
            input_type = "pt"
            features = feature_extractor(data, return_tensors=input_type)
            ov_outputs = ov_model(**features, **decoder_inputs, **extra_inps)

            self.assertTrue("logits" in ov_outputs)

            # Compare tensor outputs
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-3))

        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @pytest.mark.run_slow
    @slow
    def test_pipeline(self, model_arch: str):
        set_seed(SEED)
        model_id = MODEL_NAMES[model_arch]
        ov_model = OVModelForVision2Seq.from_pretrained(model_id, compile=False)
        feature_extractor, tokenizer = self._get_preprocessors(model_id)
        ov_model.reshape(1, -1)
        ov_model.compile()

        # Image caption generation
        pipe = pipeline(
            "image-to-text",
            model=ov_model,
            tokenizer=tokenizer,
            feature_extractor=feature_extractor,
        )
        inputs = self._get_sample_image()
        outputs = pipe(inputs, max_new_tokens=3)
        self.assertEqual(pipe.device, ov_model.device)
        self.assertIsInstance(outputs[0]["generated_text"], str)
        ov_pipe = optimum_pipeline("image-to-text", model_id, accelerator="openvino")
        ov_outputs = ov_pipe(inputs, max_new_tokens=3)
        self.assertEqual(outputs[-1]["generated_text"], ov_outputs[-1]["generated_text"])

        gc.collect()


class OVModelForCustomTasksIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES_WITH_ATTENTION = ["vit-with-attentions"]
    SUPPORTED_ARCHITECTURES_WITH_HIDDEN_STATES = ["vit-with-hidden-states"]

    def _get_sample_image(self):
        url = TEST_IMAGE_URL
        image = Image.open(requests.get(url, stream=True).raw)
        return image

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_ATTENTION)
    def test_compare_output_attentions(self, model_arch):
        model_id = MODEL_NAMES[model_arch]

        image = self._get_sample_image()
        preprocessor = AutoFeatureExtractor.from_pretrained(model_id)
        inputs = preprocessor(images=image, return_tensors="pt")

        transformers_model = AutoModelForImageClassification.from_pretrained(model_id, attn_implementation="eager")
        transformers_model.eval()
        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs, output_attentions=True)

        ov_model = OVModelForCustomTasks.from_pretrained(model_id, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)

        for input_type in ["pt", "np"]:
            inputs = preprocessor(images=image, return_tensors=input_type)
            ov_outputs = ov_model(**inputs)
            self.assertIn("logits", ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-4))
            self.assertTrue(len(ov_outputs.attentions) == len(transformers_outputs.attentions))
            for i in range(len(ov_outputs.attentions)):
                self.assertTrue(
                    torch.allclose(
                        torch.Tensor(ov_outputs.attentions[i]),
                        transformers_outputs.attentions[i],
                        atol=1e-4,  # attentions are accurate
                        rtol=1e-4,  # attentions are accurate
                    ),
                    f"Attention mismatch at layer {i}",
                )

        del transformers_model
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES_WITH_HIDDEN_STATES)
    def test_compare_output_hidden_states(self, model_arch):
        model_id = MODEL_NAMES[model_arch]

        image = self._get_sample_image()
        preprocessor = AutoFeatureExtractor.from_pretrained(model_id)
        inputs = preprocessor(images=image, return_tensors="pt")

        transformers_model = AutoModelForImageClassification.from_pretrained(model_id)
        transformers_model.eval()
        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs, output_hidden_states=True)

        ov_model = OVModelForCustomTasks.from_pretrained(model_id, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        for input_type in ["pt", "np"]:
            inputs = preprocessor(images=image, return_tensors=input_type)
            ov_outputs = ov_model(**inputs)
            self.assertIn("logits", ov_outputs)
            self.assertIsInstance(ov_outputs.logits, TENSOR_ALIAS_TO_TYPE[input_type])
            self.assertTrue(torch.allclose(torch.Tensor(ov_outputs.logits), transformers_outputs.logits, atol=1e-4))
            self.assertTrue(len(ov_outputs.hidden_states) == len(transformers_outputs.hidden_states))
            for i in range(len(ov_outputs.hidden_states)):
                self.assertTrue(
                    torch.allclose(
                        torch.Tensor(ov_outputs.hidden_states[i]),
                        transformers_outputs.hidden_states[i],
                        atol=1e-3,  # hidden states are less accurate
                        rtol=1e-2,  # hidden states are less accurate
                    ),
                    f"Hidden states mismatch at layer {i}",
                )
        del transformers_model
        del ov_model
        gc.collect()


class OVModelForOpenCLIPZeroShortImageClassificationTest(unittest.TestCase):
    OV_MODEL_ID = MODEL_NAMES["open-clip"]
    OV_MODEL_ID_IR = MODEL_NAMES["open-clip-ov"]

    def _get_sample_image(self):
        url = TEST_IMAGE_URL
        image = Image.open(requests.get(url, stream=True).raw)
        return image

    def test_load_from_hub_and_save_model(self):
        loaded_model = OVModelOpenCLIPForZeroShotImageClassification.from_pretrained(self.OV_MODEL_ID_IR)

        tokenizer = AutoTokenizer.from_pretrained(self.OV_MODEL_ID_IR)
        all_text = ["a dog", "a cat", "a frog"]
        tokens = tokenizer.batch_encode_plus(
            all_text,
            return_tensors="pt",
            max_length=loaded_model.config.text_config.context_length,
            padding="max_length",
            truncation=True,
        ).input_ids

        processor_inputs = {
            "is_train": False,
            "image_size": (loaded_model.config.vision_config.image_size, loaded_model.config.vision_config.image_size),
        }

        processor = open_clip.image_transform(**processor_inputs)
        processed_image = processor(self._get_sample_image()).unsqueeze(0)

        self.assertIsInstance(loaded_model.config, PretrainedConfig)

        loaded_model_outputs = loaded_model(tokens, processed_image)

        with TemporaryDirectory() as tmpdirname:
            loaded_model.save_pretrained(tmpdirname)
            folder_contents = os.listdir(tmpdirname)
            self.assertTrue(loaded_model.text_model._xml_model_name in folder_contents)
            self.assertTrue(loaded_model.text_model._xml_model_name.replace(".xml", ".bin") in folder_contents)
            self.assertTrue(loaded_model.visual_model._xml_model_name in folder_contents)
            self.assertTrue(loaded_model.visual_model._xml_model_name.replace(".xml", ".bin") in folder_contents)
            model = OVModelOpenCLIPForZeroShotImageClassification.from_pretrained(tmpdirname)

        outputs = model(tokens, processed_image)
        self.assertTrue(torch.equal(loaded_model_outputs.logits_per_image, outputs.logits_per_image))
        self.assertTrue(torch.equal(loaded_model_outputs.logits_per_text, outputs.logits_per_text))

        del loaded_model
        del model
        gc.collect()

    def test_compare_output_open_clip(self):
        clip_model, clip_preprocessor = open_clip.create_model_from_pretrained(f"hf-hub:{self.OV_MODEL_ID}")
        clip_tokenizer = open_clip.get_tokenizer(f"hf-hub:{self.OV_MODEL_ID}")

        image = clip_preprocessor(self._get_sample_image()).unsqueeze(0)
        text = clip_tokenizer(["a dog", "a cat", "a frog"])

        with torch.no_grad():
            clip_image_features = clip_model.encode_image(image)
            clip_text_features = clip_model.encode_text(text)

        ov_model = OVModelOpenCLIPForZeroShotImageClassification.from_pretrained(self.OV_MODEL_ID, export=True)
        ov_outputs = ov_model(text, image)

        self.assertTrue(
            torch.allclose(
                clip_image_features, torch.from_numpy(ov_outputs.vision_model_output["image_features"]), atol=1e-4
            )
        )
        self.assertTrue(
            torch.allclose(
                clip_text_features, torch.from_numpy(ov_outputs.text_model_output["text_features"]), atol=1e-4
            )
        )

        del ov_model
        gc.collect()

    def test_functions(self):
        model = OVModelOpenCLIPForZeroShotImageClassification.from_pretrained(self.OV_MODEL_ID, export=True)

        tokenizer = AutoTokenizer.from_pretrained(self.OV_MODEL_ID_IR)
        all_text = ["a dog", "a cat", "a frog"]
        tokens = tokenizer.batch_encode_plus(
            all_text,
            return_tensors="pt",
            max_length=model.config.text_config.context_length,
            padding="max_length",
            truncation=True,
        ).input_ids

        processor_inputs = {
            "is_train": False,
            "image_size": (model.config.vision_config.image_size, model.config.vision_config.image_size),
        }

        processor = open_clip.image_transform(**processor_inputs)
        processed_image = processor(self._get_sample_image()).unsqueeze(0)

        model_outputs = model(tokens, processed_image)

        model.to("AUTO")
        self.assertTrue(model.visual_model._device == "AUTO")
        self.assertTrue(model.text_model._device == "AUTO")
        self.assertTrue(model.visual_model.request is None)
        self.assertTrue(model.text_model.request is None)
        res = model(tokens, processed_image)
        self.assertTrue(torch.equal(model_outputs.logits_per_image, res.logits_per_image))

        model.compile()
        self.assertTrue(model.visual_model.request is not None)
        self.assertTrue(model.text_model.request is not None)
        res = model(tokens, processed_image)
        print(model_outputs.logits_per_image, res.logits_per_image)
        self.assertTrue(torch.equal(model_outputs.logits_per_image, res.logits_per_image))

        model.half()
        model.compile()
        res = model(tokens, processed_image)
        print(model_outputs.logits_per_image, res.logits_per_image)
        self.assertTrue(torch.allclose(model_outputs.logits_per_image, res.logits_per_image, atol=1e-2))

        model.reshape(1, -1)
        reshaped_tokens = tokenizer.batch_encode_plus(
            ["a dog"],
            return_tensors="pt",
            max_length=model.config.text_config.context_length,
            padding="max_length",
            truncation=True,
        ).input_ids
        model.compile()
        res = model(reshaped_tokens, processed_image)

        del model
        gc.collect()


class OVModelForSTFeatureExtractionIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ("st-bert", "st-mpnet")

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVSentenceTransformer.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        self.assertIsInstance(ov_model.config, PretrainedConfig)
        self.assertTrue(hasattr(ov_model, "encode"))
        st_model = SentenceTransformer(model_id)
        sentences = ["This is an example sentence", "Each sentence is converted"]
        st_embeddings = st_model.encode(sentences)
        ov_embeddings = ov_model.encode(sentences)
        # Compare tensor outputs
        self.assertTrue(np.allclose(ov_embeddings, st_embeddings, atol=1e-4))
        del st_embeddings
        del ov_model
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_sentence_transformers_save_and_infer(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        ov_model = OVSentenceTransformer.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        with TemporaryDirectory() as tmpdirname:
            model_save_path = os.path.join(tmpdirname, "sentence_transformers_ov_model")
            ov_model.save_pretrained(model_save_path)
            model = OVSentenceTransformer.from_pretrained(model_save_path)
            sentences = ["This is an example sentence", "Each sentence is converted"]
            model.encode(sentences)
        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @unittest.skipIf(not _langchain_hf_available, reason="langchain not installed")
    def test_langchain(self, model_arch):
        from langchain_huggingface import HuggingFaceEmbeddings

        model_id = MODEL_NAMES[model_arch]
        model_kwargs = {"device": "cpu", "backend": "openvino"}

        embedding = HuggingFaceEmbeddings(
            model_name=model_id,
            model_kwargs=model_kwargs,
        )
        output = embedding.embed_query("foo bar")
        self.assertTrue(len(output) > 0)


class OVLangchainTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ("gpt2",)

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    @unittest.skipIf(not _langchain_hf_available, reason="langchain not installed")
    def test_huggingface_pipeline_streaming(self, model_arch):
        from langchain_huggingface import HuggingFacePipeline

        model_id = MODEL_NAMES[model_arch]

        hf_pipe = HuggingFacePipeline.from_model_id(
            model_id=model_id,
            task="text-generation",
            pipeline_kwargs={"max_new_tokens": 10},
            backend="openvino",
        )
        self.assertIsInstance(hf_pipe.pipeline.model, OVBaseModel)

        generator = hf_pipe.stream("Q: How do you say 'hello' in German? A:'", stop=["."])

        self.assertIsInstance(generator, Generator)

        stream_results_string = ""
        for chunk in generator:
            self.assertIsInstance(chunk, str)
            stream_results_string = chunk

        self.assertTrue(len(stream_results_string.strip()) > 1)

        del hf_pipe
        gc.collect()


class OVSamIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ["sam"]
    TASK = "feature-extraction"
    IMAGE_URL = "https://huggingface.co/ybelkada/segment-anything/resolve/main/assets/car.png"

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        from optimum.intel.openvino.modeling_sam import OVSamPromptEncoder, OVSamVisionEncoder

        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVSamModel.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        processor = get_preprocessor(model_id)

        self.assertIsInstance(ov_model.vision_encoder, OVSamVisionEncoder)
        self.assertIsInstance(ov_model.prompt_encoder_mask_decoder, OVSamPromptEncoder)
        self.assertIsInstance(ov_model.config, PretrainedConfig)

        input_points = [[[450, 600]]]
        IMAGE = Image.open(
            requests.get(
                self.IMAGE_URL,
                stream=True,
            ).raw
        ).convert("RGB")
        inputs = processor(IMAGE, input_points=input_points, return_tensors="pt")

        transformers_model = OVSamModel.from_pretrained(model_id)

        # test end-to-end inference
        ov_outputs = ov_model(**inputs)

        self.assertTrue("pred_masks" in ov_outputs)
        self.assertIsInstance(ov_outputs.pred_masks, torch.Tensor)
        self.assertTrue("iou_scores" in ov_outputs)
        self.assertIsInstance(ov_outputs.iou_scores, torch.Tensor)

        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs)
        # Compare tensor outputs
        self.assertTrue(torch.allclose(ov_outputs.pred_masks, transformers_outputs.pred_masks, atol=1e-4))
        self.assertTrue(torch.allclose(ov_outputs.iou_scores, transformers_outputs.iou_scores, atol=1e-4))

        # test separated image features extraction
        pixel_values = inputs.pop("pixel_values")
        features = transformers_model.get_image_features(pixel_values)
        ov_features = ov_model.get_image_features(pixel_values)
        self.assertTrue(torch.allclose(ov_features, features, atol=1e-4))
        ov_outputs = ov_model(**inputs, image_embeddings=ov_features)
        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs, image_embeddings=features)
        # Compare tensor outputs
        self.assertTrue(torch.allclose(ov_outputs.pred_masks, transformers_outputs.pred_masks, atol=1e-4))
        self.assertTrue(torch.allclose(ov_outputs.iou_scores, transformers_outputs.iou_scores, atol=1e-4))

        del transformers_model
        del ov_model

        gc.collect()

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_reshape(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVSamModel.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        processor = get_preprocessor(model_id)
        self.assertTrue(ov_model.is_dynamic)
        input_points = [[[450, 600]]]
        IMAGE = Image.open(
            requests.get(
                self.IMAGE_URL,
                stream=True,
            ).raw
        ).convert("RGB")
        inputs = processor(IMAGE, input_points=input_points, return_tensors="pt")
        ov_dyn_outputs = ov_model(**inputs)
        ov_model.reshape(*inputs["input_points"].shape[:-1])
        self.assertFalse(ov_model.is_dynamic)
        self.assertIsNone(ov_model.vision_encoder.request)
        self.assertIsNone(ov_model.prompt_encoder_mask_decoder.request)
        ov_stat_outputs = ov_model(**inputs)
        # Compare tensor outputs
        self.assertTrue(torch.allclose(ov_dyn_outputs.pred_masks, ov_stat_outputs.pred_masks, atol=1e-4))
        self.assertTrue(torch.allclose(ov_dyn_outputs.iou_scores, ov_stat_outputs.iou_scores, atol=1e-4))

        del ov_model
        gc.collect()


class OVModelForTextToSpeechSeq2SeqIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ("speecht5",)

    def _generate_text(self):
        return "This text is converted to speech using OpenVINO backend"

    def _generate_speaker_embedding(self):
        np.random.seed(42)
        speaker_embedding = np.random.randn(1, 512).astype(np.float32)
        return torch.tensor(speaker_embedding)

    def _get_processor(self, model_id, model_arch):
        if model_arch == "speecht5":
            return AutoProcessor.from_pretrained(model_id)
        else:
            raise Exception("{} unknown processor for text-to-speech".format(model_arch))

    def _get_model(self, model_id, model_arch):
        if model_arch == "speecht5":
            return AutoModelForTextToSpectrogram.from_pretrained(model_id)
        else:
            raise Exception("{} unknown model for text-to-speech".format(model_arch))

    def _get_vocoder(self, vocoder_id, model_arch):
        if model_arch == "speecht5":
            from transformers import SpeechT5HifiGan

            vocoder = SpeechT5HifiGan.from_pretrained(vocoder_id)
            return vocoder
        else:
            raise Exception("{} unknown model for text-to-speech".format(model_arch))

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        set_seed(SEED)
        text_data = self._generate_text()
        speaker_embeddings = self._generate_speaker_embedding()
        model_id = MODEL_NAMES[model_arch]

        if model_arch == "speecht5":
            # since Auto class for text-to-audio is not implemented in optimum
            # generate model classes for reference generation
            vocoder_id = "fxmarty/speecht5-hifigan-tiny"
            processor = self._get_processor(model_id, model_arch)
            model = self._get_model(model_id, model_arch)
            vocoder = self._get_vocoder(vocoder_id, model_arch)
            inputs = processor(text=text_data, return_tensors="pt")
            ref_speech = model.generate_speech(inputs["input_ids"], speaker_embeddings, vocoder=vocoder)
            ref_speech = ref_speech.unsqueeze(0) if ref_speech.dim() == 1 else ref_speech
        else:
            raise Exception("{} unknown model for text-to-speech".format(model_arch))

        ov_pipe = OVModelForTextToSpeechSeq2Seq.from_pretrained(model_id, vocoder=vocoder_id)
        ov_speech = ov_pipe.generate(input_ids=inputs["input_ids"], speaker_embeddings=speaker_embeddings)

        self.assertIsInstance(ov_pipe.config, PretrainedConfig)
        self.assertTrue(model_has_state(ov_pipe.decoder.model))
        self.assertTrue(torch.allclose(ov_speech, ref_speech, atol=1e-3))

        del vocoder
        del model
        del processor
        gc.collect()


class OVModelForZeroShotImageClassificationIntegrationTest(unittest.TestCase):
    SUPPORTED_ARCHITECTURES = ["clip"]
    if is_transformers_version(">=", "4.45"):
        SUPPORTED_ARCHITECTURES.append("siglip")
    TASK = "zero-shot-image-classification"
    IMAGE_URL = "http://images.cocodataset.org/val2017/000000039769.jpg"

    @parameterized.expand(SUPPORTED_ARCHITECTURES)
    def test_compare_to_transformers(self, model_arch):
        model_id = MODEL_NAMES[model_arch]
        set_seed(SEED)
        ov_model = OVModelForZeroShotImageClassification.from_pretrained(model_id, export=True, ov_config=F32_CONFIG)
        processor = get_preprocessor(model_id)

        self.assertIsInstance(ov_model.config, PretrainedConfig)

        IMAGE = Image.open(
            requests.get(
                self.IMAGE_URL,
                stream=True,
            ).raw
        ).convert("RGB")
        labels = ["a photo of a cat", "a photo of a dog"]
        inputs = processor(images=IMAGE, text=labels, return_tensors="pt")

        transformers_model = AutoModelForZeroShotImageClassification.from_pretrained(model_id)

        # test end-to-end inference
        ov_outputs = ov_model(**inputs)

        self.assertTrue("logits_per_image" in ov_outputs)
        self.assertIsInstance(ov_outputs.logits_per_image, torch.Tensor)
        self.assertTrue("logits_per_text" in ov_outputs)
        self.assertIsInstance(ov_outputs.logits_per_text, torch.Tensor)
        self.assertTrue("text_embeds" in ov_outputs)
        self.assertIsInstance(ov_outputs.text_embeds, torch.Tensor)
        self.assertTrue("image_embeds" in ov_outputs)
        self.assertIsInstance(ov_outputs.image_embeds, torch.Tensor)

        with torch.no_grad():
            transformers_outputs = transformers_model(**inputs)
        # Compare tensor outputs
        self.assertTrue(torch.allclose(ov_outputs.logits_per_image, transformers_outputs.logits_per_image, atol=1e-4))
        self.assertTrue(torch.allclose(ov_outputs.logits_per_text, transformers_outputs.logits_per_text, atol=1e-4))
        self.assertTrue(torch.allclose(ov_outputs.text_embeds, transformers_outputs.text_embeds, atol=1e-4))
        self.assertTrue(torch.allclose(ov_outputs.image_embeds, transformers_outputs.image_embeds, atol=1e-4))

        del transformers_model
        del ov_model
        gc.collect()
