# Copyright (c) Opendatalab. All rights reserved.

import pytest
import torch


def test_unimer_swin_modeling_imports_with_transformers_5() -> None:
    from mineru.model.mfr.unimernet.unimernet_hf.unimer_swin import modeling_unimer_swin

    assert modeling_unimer_swin.UnimerSwinModel.__name__ == "UnimerSwinModel"


def test_unimer_swin_encoder_initializes_drop_path_rates_on_meta_device(monkeypatch) -> None:
    from mineru.model.mfr.unimernet.unimernet_hf.unimer_swin.configuration_unimer_swin import UnimerSwinConfig
    from mineru.model.mfr.unimernet.unimernet_hf.unimer_swin import modeling_unimer_swin

    captured_drop_paths = []

    class FakeStage(torch.nn.Module):
        def __init__(self, **kwargs):
            super().__init__()
            captured_drop_paths.append(kwargs["drop_path"])

    monkeypatch.setattr(modeling_unimer_swin, "UnimerSwinStage", FakeStage)
    config = UnimerSwinConfig(depths=[1, 2], num_heads=[1, 1], embed_dim=8, drop_path_rate=0.2)

    with torch.device("meta"):
        modeling_unimer_swin.UnimerSwinEncoder(config, grid_size=(8, 8))

    assert captured_drop_paths == [pytest.approx([0.0]), pytest.approx([0.1, 0.2])]


def test_donut_swin_encoder_initializes_drop_path_rates_on_meta_device(monkeypatch) -> None:
    from mineru.model.utils.pytorchocr.modeling.backbones import rec_donut_swin

    captured_drop_paths = []

    class FakeStage(torch.nn.Module):
        def __init__(self, **kwargs):
            super().__init__()
            captured_drop_paths.append(kwargs["drop_path"])

    monkeypatch.setattr(rec_donut_swin, "DonutSwinStage", FakeStage)
    config = rec_donut_swin.DonutSwinConfig(depths=[1, 2], num_heads=[1, 1], embed_dim=8, drop_path_rate=0.2)

    with torch.device("meta"):
        rec_donut_swin.DonutSwinEncoder(config, grid_size=(8, 8))

    assert captured_drop_paths == [pytest.approx([0.0]), pytest.approx([0.1, 0.2])]


def test_unimer_swin_pretrained_model_provides_head_mask_fallback_for_transformers_5() -> None:
    from mineru.model.mfr.unimernet.unimernet_hf.unimer_swin.configuration_unimer_swin import UnimerSwinConfig
    from mineru.model.mfr.unimernet.unimernet_hf.unimer_swin.modeling_unimer_swin import UnimerSwinPreTrainedModel

    model = UnimerSwinPreTrainedModel(UnimerSwinConfig())

    assert model.get_head_mask(None, 3) == [None, None, None]
